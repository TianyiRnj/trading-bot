import json
import logging
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests import exceptions as requests_exceptions
try:
    from utils import utc_now_iso, parse_iso_datetime          # script: python bot/main.py
except ImportError:
    from bot.utils import utc_now_iso, parse_iso_datetime      # package: python -m bot.main
try:
    from db import init_pool, close_pool, get_db, check_db_available   # script path
    from db import check_db_schema_ready                                # script path
    import repository as repo                                           # script path
except ImportError:
    from bot.db import init_pool, close_pool, get_db, check_db_available  # package path
    from bot.db import check_db_schema_ready                              # package path
    import bot.repository as repo                                          # package path

load_dotenv()

BASE_URL = os.getenv("MUSASHI_API_BASE_URL", "https://musashi-api.vercel.app").rstrip("/")
REQUESTED_MODE = os.getenv("BOT_MODE", "paper").strip().lower()
ACCOUNT_KEY = "main"
PAPER_GEO_STRICT = os.getenv("BOT_PAPER_GEO_STRICT", "false").lower() == "true"
SCAN_INTERVAL_SECONDS = int(os.getenv("BOT_SCAN_INTERVAL_SECONDS", "45"))
MIN_CONFIDENCE = float(os.getenv("BOT_MIN_CONFIDENCE", "0.76"))
MIN_EDGE = float(os.getenv("BOT_MIN_EDGE", "0.05"))
MIN_VOLUME_24H = float(os.getenv("BOT_MIN_VOLUME_24H", "20000"))
MIN_PRICE = float(os.getenv("BOT_MIN_PRICE", "0.08"))
MAX_PRICE = float(os.getenv("BOT_MAX_PRICE", "0.85"))
BANKROLL_USD = float(os.getenv("BOT_BANKROLL_USD", "10"))
MAX_POSITION_USD = float(os.getenv("BOT_MAX_POSITION_USD", "3"))
MAX_TOTAL_EXPOSURE_USD = float(os.getenv("BOT_MAX_TOTAL_EXPOSURE_USD", "10"))
LIMIT_ONE_POSITION_PER_EVENT = os.getenv("BOT_LIMIT_ONE_POSITION_PER_EVENT", "true").lower() == "true"
TAKE_PROFIT_PCT = float(os.getenv("BOT_TAKE_PROFIT_PCT", "0.18"))
STOP_LOSS_PCT = float(os.getenv("BOT_STOP_LOSS_PCT", "0.10"))
MAX_HOLD_MINUTES = int(os.getenv("BOT_MAX_HOLD_MINUTES", "240"))
EXIT_ON_SIGNAL_REVERSAL = os.getenv("BOT_EXIT_ON_SIGNAL_REVERSAL", "true").lower() == "true"
EXIT_ORDER_TIMEOUT_SECONDS = int(os.getenv("BOT_EXIT_ORDER_TIMEOUT_SECONDS", "120"))
EXIT_ORDER_REPRICE = os.getenv("BOT_EXIT_ORDER_REPRICE", "true").lower() == "true"
STARTUP_RECONCILE = os.getenv("BOT_STARTUP_RECONCILE", "true").lower() == "true"
BOT_ENABLE_ARBITRAGE = os.getenv("BOT_ENABLE_ARBITRAGE", "false").lower() == "true"
POLYMARKET_WS_ENABLED = os.getenv("POLYMARKET_WS_ENABLED", "true").lower() == "true"
RESTRICTED_COUNTRIES = {
    item.strip().upper()
    for item in os.getenv(
        "BOT_RESTRICTED_COUNTRIES",
        "AU,BE,BY,BI,CF,CD,CU,DE,ET,FR,GB,IR,IQ,IT,KP,LB,LY,MM,NI,NL,PL,RU,SG,SO,SS,SD,SY,TH,TW,UM,US,VE,YE,ZW",
    ).split(",")
    if item.strip()
}
RESTRICTED_REGIONS = {
    "CA": {"ON"},
    "UA": {"43", "14", "09"},
}
MUSASHI_CONNECT_TIMEOUT_SECONDS = float(os.getenv("MUSASHI_CONNECT_TIMEOUT_SECONDS", "10"))
MUSASHI_READ_TIMEOUT_SECONDS = float(os.getenv("MUSASHI_READ_TIMEOUT_SECONDS", "30"))
POSTGRES_POOL_MIN = int(os.getenv("POSTGRES_POOL_MIN", "1"))
POSTGRES_POOL_MAX = int(os.getenv("POSTGRES_POOL_MAX", "5"))
POSTGRES_CONNECT_TIMEOUT_SECONDS = float(os.getenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "10"))

POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com").rstrip("/")
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")

LOG_DIR = Path("bot/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("musashi-poly-bot")


def configure_logging() -> None:
    """Configure root logger with file (append) and stream handlers.

    Must be called from the runtime entry path only, not on import.
    Uses force=True so the setup is deterministic even if another module
    touched logging earlier.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "bot.log", mode="a"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def extract_health_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    for key in ("status", "message", "ok"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("status", "message", "ok"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
    return "unknown"


def extract_condition_id(payload: dict[str, Any]) -> str | None:
    for key in ("conditionId", "condition_id", "conditionID", "market"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


class GeolocationClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "musashi-poly-bot/1.0"})

    def locate(self) -> dict[str, Any]:
        providers = [
            ("https://ipinfo.io/json", self._parse_ipinfo),
            ("https://ipapi.co/json/", self._parse_ipapi),
        ]
        for url, parser in providers:
            try:
                response = self.session.get(url, timeout=10)
                response.raise_for_status()
                payload = response.json()
                location = parser(payload)
                if location:
                    return {
                        "provider": url,
                        "ip": location.get("ip"),
                        "city": location.get("city"),
                        "region": location.get("region"),
                        "country": location.get("country"),
                        "loc": location.get("loc"),
                        "timezone": location.get("timezone"),
                        "raw": payload,
                    }
            except Exception as exc:
                logger.warning("Geolocation lookup failed via %s: %s", url, exc)
        return {}

    @staticmethod
    def _parse_ipinfo(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "ip": payload.get("ip"),
            "city": payload.get("city"),
            "region": payload.get("region"),
            "country": payload.get("country"),
            "loc": payload.get("loc"),
            "timezone": payload.get("timezone"),
        }

    @staticmethod
    def _parse_ipapi(payload: dict[str, Any]) -> dict[str, Any]:
        loc = None
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        if latitude not in (None, "") and longitude not in (None, ""):
            loc = f"{latitude},{longitude}"
        return {
            "ip": payload.get("ip"),
            "city": payload.get("city"),
            "region": payload.get("region"),
            "country": payload.get("country_name") or payload.get("country"),
            "loc": loc,
            "timezone": payload.get("timezone"),
        }


class SafetyShutdown(RuntimeError):
    pass


class PolymarketMarketStream:
    def __init__(self, enabled: bool = True) -> None:
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        self.enabled = enabled
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._quotes: dict[str, dict[str, Any]] = {}
        self._asset_ids: set[str] = set()
        self._ws: Any = None

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="polymarket-market-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def set_assets(self, asset_ids: set[str]) -> None:
        with self._lock:
            self._asset_ids = {str(asset_id) for asset_id in asset_ids if asset_id}

    def get_quote(self, asset_id: str) -> dict[str, Any] | None:
        with self._lock:
            quote = self._quotes.get(str(asset_id))
            return dict(quote) if quote else None

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            logger.warning("websocket-client is not installed; Polymarket market WebSocket disabled")
            self.enabled = False
            return

        while not self._stop_event.is_set():
            asset_ids = self._snapshot_assets()
            if not asset_ids:
                time.sleep(1)
                continue

            try:
                ws = websocket.create_connection(self.url, timeout=10)
                ws.settimeout(1)
                self._ws = ws
                subscribed_assets = set(asset_ids)
                ws.send(json.dumps({"assets_ids": sorted(subscribed_assets), "type": "market"}))
                last_ping_at = time.time()

                while not self._stop_event.is_set():
                    current_assets = self._snapshot_assets()
                    self._sync_asset_subscription(ws, subscribed_assets, current_assets)
                    subscribed_assets = current_assets
                    if time.time() - last_ping_at >= 8:
                        ws.send("PING")
                        last_ping_at = time.time()
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not message or message == "PONG":
                        continue
                    self._handle_message(message)
            except Exception as exc:
                logger.warning("Polymarket market WebSocket reconnecting after error: %s", exc)
                time.sleep(2)
            finally:
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

    def _snapshot_assets(self) -> set[str]:
        with self._lock:
            return set(self._asset_ids)

    def _sync_asset_subscription(self, ws: Any, subscribed_assets: set[str], current_assets: set[str]) -> None:
        add_assets = sorted(current_assets - subscribed_assets)
        remove_assets = sorted(subscribed_assets - current_assets)
        if add_assets:
            ws.send(json.dumps({"operation": "subscribe", "assets_ids": add_assets}))
        if remove_assets:
            ws.send(json.dumps({"operation": "unsubscribe", "assets_ids": remove_assets}))

    def _handle_message(self, message: str) -> None:
        payload = json.loads(message)
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            asset_id = str(event.get("asset_id") or event.get("assetId") or "")
            if not asset_id:
                continue
            quote_update = self._parse_quote_event(event)
            if quote_update:
                with self._lock:
                    previous = self._quotes.get(asset_id, {})
                    self._quotes[asset_id] = {**previous, **quote_update, "updated_at": utc_now_iso()}

    def _parse_quote_event(self, event: dict[str, Any]) -> dict[str, Any]:
        bids = event.get("bids") or []
        asks = event.get("asks") or []
        best_bid = self._best_price(bids, prefer_max=True)
        best_ask = self._best_price(asks, prefer_max=False)
        last_trade = as_float(
            event.get("last_trade_price"),
            as_float(event.get("lastTradePrice"), as_float(event.get("price"))),
        )
        mid = None
        if best_bid > 0 and best_ask > 0:
            mid = round((best_bid + best_ask) / 2, 4)
        reference_price = mid or last_trade or best_bid or best_ask
        if reference_price <= 0:
            return {}
        return {
            "best_bid": clamp_price(best_bid) if best_bid > 0 else None,
            "best_ask": clamp_price(best_ask) if best_ask > 0 else None,
            "last_trade_price": clamp_price(last_trade) if last_trade > 0 else None,
            "mid_price": clamp_price(mid) if mid else None,
            "reference_price": clamp_price(reference_price),
        }

    @staticmethod
    def _best_price(levels: list[dict[str, Any]], prefer_max: bool) -> float:
        prices = [as_float(level.get("price")) for level in levels if as_float(level.get("price")) > 0]
        if not prices:
            return 0.0
        return max(prices) if prefer_max else min(prices)


class PolymarketUserStream:
    def __init__(self, api_creds: dict[str, str], enabled: bool = True) -> None:
        self.url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
        self.enabled = enabled
        self.api_creds = api_creds
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._condition_ids: set[str] = set()
        self._order_events: dict[str, dict[str, Any]] = {}
        self._ws: Any = None

    def start(self) -> None:
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="polymarket-user-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def set_markets(self, condition_ids: set[str]) -> None:
        with self._lock:
            self._condition_ids = {str(condition_id) for condition_id in condition_ids if condition_id}

    def has_update_for_order(self, order_id: str) -> bool:
        with self._lock:
            return str(order_id) in self._order_events

    def pop_order_event(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._order_events.pop(str(order_id), None)

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            logger.warning("websocket-client is not installed; Polymarket user WebSocket disabled")
            self.enabled = False
            return

        while not self._stop_event.is_set():
            condition_ids = self._snapshot_condition_ids()
            if not condition_ids:
                time.sleep(1)
                continue

            try:
                ws = websocket.create_connection(self.url, timeout=10)
                ws.settimeout(1)
                self._ws = ws
                subscribed_ids = set(condition_ids)
                ws.send(json.dumps({**self.api_creds, "markets": sorted(subscribed_ids), "type": "user"}))
                last_ping_at = time.time()

                while not self._stop_event.is_set():
                    current_ids = self._snapshot_condition_ids()
                    self._sync_condition_subscription(ws, subscribed_ids, current_ids)
                    subscribed_ids = current_ids
                    if time.time() - last_ping_at >= 8:
                        ws.send("PING")
                        last_ping_at = time.time()
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not message or message == "PONG":
                        continue
                    self._handle_message(message)
            except Exception as exc:
                logger.warning("Polymarket user WebSocket reconnecting after error: %s", exc)
                time.sleep(2)
            finally:
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

    def _snapshot_condition_ids(self) -> set[str]:
        with self._lock:
            return set(self._condition_ids)

    def _sync_condition_subscription(self, ws: Any, subscribed_ids: set[str], current_ids: set[str]) -> None:
        add_ids = sorted(current_ids - subscribed_ids)
        remove_ids = sorted(subscribed_ids - current_ids)
        if add_ids:
            ws.send(json.dumps({"operation": "subscribe", "markets": add_ids}))
        if remove_ids:
            ws.send(json.dumps({"operation": "unsubscribe", "markets": remove_ids}))

    def _handle_message(self, message: str) -> None:
        payload = json.loads(message)
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            order_id = event.get("id") or event.get("orderID") or event.get("orderId")
            if order_id:
                with self._lock:
                    self._order_events[str(order_id)] = event


class MusashiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = (MUSASHI_CONNECT_TIMEOUT_SECONDS, MUSASHI_READ_TIMEOUT_SECONDS)

    def health(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/api/health", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def get_feed(self, limit: int = 20, min_urgency: str = "high") -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/api/feed",
            params={"limit": limit, "minUrgency": min_urgency},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", {}).get("tweets", [])

    def analyze_text(self, text: str, min_confidence: float = 0.5, max_results: int = 3) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/analyze-text",
            json={"text": text, "minConfidence": min_confidence, "maxResults": max_results},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def get_arbitrage(self, min_spread: float = 0.05, limit: int = 20) -> dict[str, Any]:
        """Get arbitrage opportunities between Polymarket and Kalshi"""
        response = self.session.get(
            f"{self.base_url}/api/markets/arbitrage",
            params={"minSpread": min_spread, "limit": limit},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


class PolymarketPublicClient:
    def __init__(self) -> None:
        self.session = requests.Session()

    def geoblock(self) -> dict[str, Any]:
        response = self.session.get("https://polymarket.com/api/geoblock", timeout=20)
        response.raise_for_status()
        return response.json()


class PolymarketGammaClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.base_url = "https://gamma-api.polymarket.com"

    def get_market(self, market_id: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/markets/{market_id}", timeout=20)
        response.raise_for_status()
        return response.json()

    def get_market_by_slug(self, slug: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/markets/slug/{slug}", timeout=20)
        response.raise_for_status()
        return response.json()

    def search_markets(self, query: str) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/markets",
            params={"search": query, "limit": 10},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
        return []

    def resolve_market(self, market: dict[str, Any]) -> dict[str, Any]:
        market_id = market.get("id")
        if market_id:
            try:
                return self.get_market(str(market_id))
            except Exception:
                logger.warning("Gamma lookup by id failed for %s, falling back to slug", market_id)

        slug = extract_slug(market.get("url", ""))
        if not slug:
            candidates = self.search_markets(market.get("title", ""))
            if candidates:
                return candidates[0]
            raise ValueError("Unable to resolve Polymarket slug from market URL")
        try:
            return self.get_market_by_slug(slug)
        except Exception:
            candidates = self.search_markets(market.get("title", ""))
            if candidates:
                return candidates[0]
            raise


def extract_slug(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    if not parts:
        return None
    if "event" in parts:
        idx = parts.index("event")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[-1]


def parse_token_ids(gamma_market: dict[str, Any]) -> list[str]:
    raw = gamma_market.get("clobTokenIds")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass
    return []


def pick_token_id(gamma_market: dict[str, Any], decision_side: str) -> str:
    token_ids = parse_token_ids(gamma_market)
    if len(token_ids) < 2:
        raise ValueError("Gamma market did not return Yes/No token ids")
    return token_ids[0] if decision_side == "YES" else token_ids[1]


def current_probability(market: dict[str, Any], decision_side: str) -> float:
    if decision_side == "YES":
        return float(market.get("yesPrice", 0))
    return float(market.get("noPrice", 0))


@dataclass
class Decision:
    event_id: str
    market: dict[str, Any]
    side: str
    confidence: float
    edge: float
    reason: str
    urgency: str
    signal_type: str | None
    probability: float
    score: float


@dataclass
class FillResult:
    success: bool
    status: str
    order_id: str | None
    filled_shares: float
    filled_value_usd: float
    avg_price: float
    raw_response: dict[str, Any]


@dataclass
class OrderStatusResult:
    order_id: str | None
    status: str
    filled_shares: float
    original_shares: float
    remaining_shares: float
    avg_price: float
    is_open: bool
    is_terminal: bool
    raw_response: dict[str, Any]


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_status(value: Any) -> str:
    return str(value or "unknown").strip().lower().replace("-", "_").replace(" ", "_")


def parse_fill_result(
    response: dict[str, Any],
    fallback_price: float,
    requested_value_usd: float,
    requested_shares: float,
) -> FillResult:
    making_amount = abs(as_float(response.get("makingAmount")))
    taking_amount = abs(as_float(response.get("takingAmount")))

    filled_shares = max(making_amount, taking_amount)
    filled_value_usd = min(making_amount, taking_amount)

    if filled_shares <= 0 and requested_shares > 0 and response.get("success") is True:
        filled_shares = requested_shares
    if filled_value_usd <= 0 and requested_value_usd > 0 and response.get("success") is True:
        filled_value_usd = requested_value_usd

    avg_price = fallback_price
    if filled_shares > 0 and filled_value_usd > 0:
        avg_price = clamp_price(filled_value_usd / filled_shares)

    return FillResult(
        success=bool(response.get("success", True)) and filled_shares > 0,
        status=normalize_status(response.get("status")),
        order_id=response.get("orderID") or response.get("orderId") or response.get("id"),
        filled_shares=round(filled_shares, 6),
        filled_value_usd=round(filled_value_usd, 6),
        avg_price=avg_price,
        raw_response=response,
    )


def parse_order_status(response: dict[str, Any], fallback_price: float) -> OrderStatusResult:
    order_id = response.get("id") or response.get("orderID") or response.get("orderId")
    status = normalize_status(response.get("status"))
    original_shares = as_float(
        response.get("original_size"),
        as_float(response.get("size"), as_float(response.get("initialSize"))),
    )
    filled_shares = as_float(
        response.get("size_matched"),
        as_float(response.get("filledSize"), as_float(response.get("matchedAmount"))),
    )
    remaining_shares = as_float(
        response.get("remaining_size"),
        max(original_shares - filled_shares, 0.0),
    )
    avg_price = clamp_price(
        as_float(response.get("avg_price"), as_float(response.get("price"), fallback_price))
    )
    is_terminal = status in {"filled", "cancelled", "canceled", "expired", "rejected"}
    is_open = status in {"live", "open", "partially_filled", "partially_matched", "matched"} and remaining_shares > 0
    return OrderStatusResult(
        order_id=order_id,
        status=status,
        filled_shares=round(filled_shares, 6),
        original_shares=round(original_shares, 6),
        remaining_shares=round(max(remaining_shares, 0.0), 6),
        avg_price=avg_price,
        is_open=is_open,
        is_terminal=is_terminal,
        raw_response=response,
    )


class PaperTrader:
    def place_market_buy(self, token_id: str, amount_usd: float, meta: dict[str, Any]) -> dict[str, Any]:
        probability = clamp_price(as_float(meta.get("probability"), 0.5))
        shares = estimate_shares(amount_usd, probability)
        logger.info(
            "[paper] buy token=%s amount=%.2f side=%s market=%s",
            token_id,
            amount_usd,
            meta["side"],
            meta["market_title"],
        )
        return {
            "mode": "paper",
            "success": True,
            "status": "filled",
            "token_id": token_id,
            "orderID": f"paper-buy-{int(time.time() * 1000)}",
            "amount_usd": amount_usd,
            "makingAmount": shares,
            "takingAmount": amount_usd,
        }

    def close_position(self, token_id: str, shares: float, limit_price: float, meta: dict[str, Any]) -> dict[str, Any]:
        filled_shares = round(min(shares, as_float(meta.get("available_shares"), shares)), 6)
        proceeds = round(filled_shares * limit_price, 6)
        logger.info(
            "[paper] close token=%s shares=%.4f price=%.4f reason=%s market=%s",
            token_id,
            filled_shares,
            limit_price,
            meta["exit_reason"],
            meta["market_title"],
        )
        return {
            "mode": "paper",
            "success": True,
            "status": "filled",
            "token_id": token_id,
            "shares": filled_shares,
            "price": limit_price,
            "exit_reason": meta["exit_reason"],
            "orderID": f"paper-sell-{int(time.time() * 1000)}",
            "makingAmount": filled_shares,
            "takingAmount": proceeds,
        }

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "status": "filled",
            "size_matched": 0,
            "remaining_size": 0,
        }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"success": True, "canceled": [order_id]}

    def check_entry_liquidity(self, token_id: str, size_usd: float) -> bool:  # noqa: ARG002
        # Paper mode has no real order book — always allow entry.
        return True


class LiveTrader:
    def __init__(self) -> None:
        if not POLYMARKET_PRIVATE_KEY:
            raise ValueError("Missing POLYMARKET_PRIVATE_KEY")
        if not POLYMARKET_FUNDER:
            raise ValueError("Missing POLYMARKET_FUNDER")

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderType

        self._order_type = OrderType
        self._host = POLYMARKET_HOST
        self._client = ClobClient(
            self._host,
            key=POLYMARKET_PRIVATE_KEY,
            chain_id=POLYMARKET_CHAIN_ID,
            signature_type=POLYMARKET_SIGNATURE_TYPE,
            funder=POLYMARKET_FUNDER,
        )
        self._api_creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(self._api_creds)

    def ws_auth_payload(self) -> dict[str, str]:
        return {
            "api_key": self._api_creds.api_key,
            "secret": self._api_creds.api_secret,
            "passphrase": self._api_creds.api_passphrase,
        }

    def get_exit_price(self, token_id: str, fallback_probability: float) -> float:
        try:
            quoted = self._client.get_price(token_id, side="SELL")
            if quoted is not None:
                return clamp_price(float(quoted))
        except Exception as exc:
            logger.warning("Failed to fetch SELL quote for %s: %s", token_id, exc)
        return clamp_price(fallback_probability)

    def check_entry_liquidity(self, token_id: str, size_usd: float) -> bool:  # noqa: ARG002
        # get_price returns the best ask, or None when the order book has no sellers.
        # A None result means even a small FOK order would find no counterparty, so
        # we skip rather than move the price significantly on entry.
        try:
            quoted = self._client.get_price(token_id, side="BUY")
            if quoted is None:
                logger.warning(
                    "[liquidity] No BUY price for token %s — order book empty, skipping entry",
                    token_id,
                )
                return False
            return True
        except Exception as exc:
            # Fail open: if the price check errors, allow the entry rather than
            # silently blocking all live trades.
            logger.warning("[liquidity] Could not fetch BUY price for %s: %s — allowing entry", token_id, exc)
            return True

    def place_market_buy(self, token_id: str, amount_usd: float, meta: dict[str, Any]) -> dict[str, Any]:
        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.order_builder.constants import BUY

        market_order = MarketOrderArgs(
            token_id=token_id,
            amount=float(round(amount_usd, 2)),
            side=BUY,
            order_type=self._order_type.FOK,
        )
        signed = self._client.create_market_order(market_order)
        response = self._client.post_order(signed, self._order_type.FOK)
        logger.info(
            "[live] buy token=%s amount=%.2f side=%s market=%s response=%s",
            token_id,
            amount_usd,
            meta["side"],
            meta["market_title"],
            response,
        )
        return response

    def close_position(self, token_id: str, shares: float, limit_price: float, meta: dict[str, Any]) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import SELL

        order = OrderArgs(
            token_id=token_id,
            price=clamp_price(limit_price),
            size=round(max(shares, 0.0), 6),
            side=SELL,
        )
        signed = self._client.create_order(order)
        response = self._client.post_order(signed, self._order_type.GTC)
        logger.info(
            "[live] close token=%s shares=%.4f price=%.4f reason=%s market=%s response=%s",
            token_id,
            shares,
            limit_price,
            meta["exit_reason"],
            meta["market_title"],
            response,
        )
        return response

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        if hasattr(self._client, "get_order"):
            return self._client.get_order(order_id)
        if hasattr(self._client, "get_orders"):
            response = self._client.get_orders({"ids": [order_id]})
            if isinstance(response, list) and response:
                return response[0]
            if isinstance(response, dict):
                data = response.get("data")
                if isinstance(data, list) and data:
                    return data[0]
        raise AttributeError("ClobClient does not expose get_order/get_orders")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if hasattr(self._client, "cancel"):
            return self._client.cancel(order_id)
        if hasattr(self._client, "cancel_orders"):
            return self._client.cancel_orders([order_id])
        raise AttributeError("ClobClient does not expose cancel/cancel_orders")


def clamp_price(value: float) -> float:
    return round(min(max(value, 0.01), 0.99), 4)


def estimate_shares(size_usd: float, probability: float) -> float:
    if probability <= 0:
        return 0.0
    return round(size_usd / probability, 6)


def realized_pnl_usd(entry_probability: float, current_probability: float, shares: float) -> float:
    return round((current_probability - entry_probability) * shares, 4)


def position_market_value_usd(shares: float, current_probability: float) -> float:
    bounded_probability = min(max(as_float(current_probability), 0.0), 1.0)
    return round(max(shares, 0.0) * bounded_probability, 6)


def mark_position_to_market(position: dict[str, Any], current_probability: float) -> dict[str, Any]:
    shares = current_position_shares(position)
    current_probability = min(max(as_float(current_probability), 0.0), 1.0)
    current_value_usd = position_market_value_usd(shares, current_probability)
    remaining_cost_basis = round(as_float(position.get("size_usd"), position_cost_basis_usd(position, shares)), 6)
    unrealized = round(current_value_usd - remaining_cost_basis, 6)
    return {
        **position,
        "current_probability": current_probability,
        "current_value_usd": current_value_usd,
        "unrealized_pnl_usd": unrealized,
    }


def current_position_shares(position: dict[str, Any]) -> float:
    return as_float(position.get("shares"), as_float(position.get("estimated_shares")))


def position_cost_basis_usd(position: dict[str, Any], shares: float) -> float:
    entry_probability = as_float(position.get("entry_probability"))
    return round(max(shares, 0.0) * entry_probability, 6)


def cumulative_executed_value_usd(
    requested_shares: float,
    remaining_shares: float,
    execution_price: float,
) -> float:
    filled_shares = max(as_float(requested_shares) - max(as_float(remaining_shares), 0.0), 0.0)
    return round(filled_shares * as_float(execution_price), 6)


def _resolve_effective_mode_and_trader(
    requested_mode: str,
    public_client: "PolymarketPublicClient",
) -> "tuple[str, str | None, PaperTrader | LiveTrader]":
    if requested_mode != "live":
        return requested_mode, None, PaperTrader()

    if not POLYMARKET_PRIVATE_KEY or not POLYMARKET_FUNDER:
        return "paper", "missing_credentials", PaperTrader()

    try:
        geo = public_client.geoblock()
        if bool(geo.get("blocked")):
            return "paper", "geoblock_blocked", PaperTrader()
        country = str(geo.get("country") or "").strip().upper()
        if country in RESTRICTED_COUNTRIES:
            return "paper", f"geoblock_restricted_country:{country}", PaperTrader()
    except Exception as exc:
        return "paper", f"geoblock_check_failed:{type(exc).__name__}", PaperTrader()

    try:
        trader = LiveTrader()
        return "live", None, trader
    except Exception as exc:
        return "paper", f"live_trader_init_failed:{exc}", PaperTrader()


class Bot:
    def __init__(self, *, install_signal_handlers: bool = True) -> None:
        self.requested_mode = REQUESTED_MODE
        self.musashi = MusashiClient(BASE_URL)
        self.polymarket_public = PolymarketPublicClient()
        self.geolocation = GeolocationClient()
        self.gamma = PolymarketGammaClient()

        self.startup_geo_profile: dict[str, str] | None = None
        self._shutdown_requested = False
        self.mode_run_id: int | None = None
        self.protection_mode_reason: str | None = None
        self.pending_fallback_reason: str | None = None

        self.effective_mode, self.fallback_reason, self.trader = (
            _resolve_effective_mode_and_trader(REQUESTED_MODE, self.polymarket_public)
        )

        try:
            init_pool(
                min_size=POSTGRES_POOL_MIN,
                max_size=POSTGRES_POOL_MAX,
                timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
            )
        except RuntimeError as exc:
            logger.critical("Cannot init DB pool: %s", exc)
            raise SystemExit(1) from exc

        ok, error = check_db_available()
        if not ok:
            logger.critical("Database not available: %s", error)
            raise SystemExit(1)
        ok, error = check_db_schema_ready()
        if not ok:
            logger.critical("Database schema not ready: %s", error)
            raise SystemExit(1)

        if self.effective_mode == "paper":
            try:
                with get_db() as conn:
                    if repo.has_live_exposure(conn):
                        logger.critical(
                            "Cannot start with effective paper mode while live exposure exists in DB. "
                            "Reconcile manually before restarting. requested_mode=%s fallback_reason=%s",
                            self.requested_mode,
                            self.fallback_reason,
                        )
                        raise SystemExit(1)
            except SystemExit:
                raise
            except Exception as exc:
                logger.critical("Failed to verify live exposure before startup: %s", exc)
                raise SystemExit(1) from exc
        if self.requested_mode == "live" and self.effective_mode == "paper":
            logger.warning(
                "Degrading from live to paper mode. fallback_reason=%s",
                self.fallback_reason,
            )

        run_label = os.getenv("BOT_RUN_LABEL", "default")
        try:
            with get_db() as conn:
                self.mode_run_id = repo.insert_mode_run(
                    conn, run_label, self.requested_mode, self.effective_mode, self.fallback_reason
                )
                repo.upsert_account_state(
                    conn,
                    account_key=ACCOUNT_KEY,
                    initial_bankroll=BANKROLL_USD,
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                )
        except Exception as exc:
            logger.critical("DB setup writes failed (mode_run / account_state): %s", exc)
            raise SystemExit(1) from exc

        try:
            with get_db() as conn:
                self.positions = repo.load_open_positions(conn, effective_mode=self.effective_mode)
                self.pending_orders = repo.load_pending_orders(conn, effective_mode=self.effective_mode)
                self.seen_event_ids = repo.load_seen_events(conn)
                self.account_state = repo.get_account_state(conn, ACCOUNT_KEY) or {}
        except Exception as exc:
            logger.critical("Failed to load state from DB: %s", exc)
            raise SystemExit(1) from exc

        if not self.account_state:
            logger.critical("account_state is missing after DB initialization")
            raise SystemExit(1)

        logger.info(
            "Bot requested_mode=%s effective_mode=%s fallback_reason=%s "
            "positions=%d pending_orders=%d seen_events=%d",
            self.requested_mode, self.effective_mode, self.fallback_reason,
            len(self.positions), len(self.pending_orders), len(self.seen_event_ids),
        )

        self.market_stream = PolymarketMarketStream(enabled=POLYMARKET_WS_ENABLED)
        self.user_stream = (
            PolymarketUserStream(self.trader.ws_auth_payload(), enabled=POLYMARKET_WS_ENABLED)
            if isinstance(self.trader, LiveTrader)
            else None
        )

        self.arbitrage_strategy = None
        self.arbitrage_thread = None
        if BOT_ENABLE_ARBITRAGE:
            try:
                from arbitrage_strategy import ArbitrageStrategy      # script
            except ImportError:
                from bot.arbitrage_strategy import ArbitrageStrategy   # package
            self.arbitrage_strategy = ArbitrageStrategy(
                gamma_client=self.gamma,
                musashi_client=self.musashi,
                trader=self.trader,
                positions=self.positions,
                save_state_callback=self.save_state,
            )

        if install_signal_handlers:
            signal.signal(signal.SIGTERM, self._handle_sigterm)

    def _db_write_critical(self, label: str, exc: Exception) -> None:
        """Handle a failed DB write on a path where data loss is unrecoverable.

        In live mode the position state in Postgres is now inconsistent with
        reality, so we raise SafetyShutdown to force a clean restart rather
        than let the bot continue with phantom or missing positions.
        In paper mode there is no real money at risk, so we log and continue.
        """
        if self.effective_mode == "live":
            raise SafetyShutdown(
                f"Critical DB write failed ({label}) in live mode — "
                f"position state is inconsistent; terminating for safety: {exc}"
            ) from exc
        logger.error("DB write failed (%s) [paper mode — continuing]: %s", label, exc)

    def current_exposure(self) -> float:
        return round(
            sum(
                as_float(position.get("current_value_usd"), as_float(position.get("size_usd"), 0.0))
                for position in self.positions.values()
            ),
            6,
        )

    def bankroll_remaining(self) -> float:
        cash_balance = as_float(self.account_state.get("cash_balance"), BANKROLL_USD)
        return max(0.0, round(cash_balance, 2))

    def save_state(self) -> None:
        self.sync_realtime_subscriptions()

    def refresh_account_state_from_db(self) -> dict[str, Any]:
        try:
            with get_db() as conn:
                account_state = repo.get_account_state(conn, ACCOUNT_KEY)
                if account_state is not None:
                    self.account_state = account_state
        except Exception as exc:
            logger.warning("Failed to refresh account_state from DB: %s", exc)
        return self.account_state

    def persist_runtime_state(self, reason: str = "runtime") -> None:
        self.sync_realtime_subscriptions()
        try:
            with get_db() as conn:
                for position in self.positions.values():
                    repo.upsert_position(
                        conn,
                        position,
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                    )
                for pending_order in self.pending_orders.values():
                    repo.upsert_pending_order(
                        conn,
                        pending_order,
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                    )
                if self.mode_run_id is not None:
                    repo.update_mode_heartbeat(conn, self.mode_run_id)
        except Exception as exc:
            logger.warning("Runtime flush failed (%s): %s", reason, exc)

    def sync_account_market_state(
        self,
        *,
        refresh_prices: bool,
        persist_positions: bool = True,
    ) -> dict[str, Any]:
        updated_positions: dict[str, dict[str, Any]] = {}
        total_value = 0.0
        total_unrealized = 0.0

        for market_id, position in list(self.positions.items()):
            try:
                if refresh_prices:
                    probability = self.latest_position_probability(position)
                else:
                    probability = as_float(
                        position.get("current_probability"),
                        as_float(position.get("entry_probability"), 0.5),
                    )
                marked = mark_position_to_market(position, probability)
                updated_positions[market_id] = marked
                total_value += as_float(marked.get("current_value_usd"))
                total_unrealized += as_float(marked.get("unrealized_pnl_usd"))
            except Exception as exc:
                logger.warning("Failed to mark position %s to market: %s", market_id, exc)
                updated_positions[market_id] = position
                total_value += as_float(position.get("current_value_usd"), as_float(position.get("size_usd")))
                total_unrealized += as_float(position.get("unrealized_pnl_usd"))

        self.positions = updated_positions

        try:
            with get_db() as conn:
                if persist_positions:
                    for position in self.positions.values():
                        repo.upsert_position(
                            conn,
                            position,
                            requested_mode=self.requested_mode,
                            effective_mode=self.effective_mode,
                        )
                repo.update_account_market_state(
                    conn,
                    ACCOUNT_KEY,
                    positions_value=round(total_value, 6),
                    unrealized_pnl=round(total_unrealized, 6),
                )
            self.refresh_account_state_from_db()
        except Exception as exc:
            logger.warning("Account mark-to-market sync failed: %s", exc)

        return self.account_state

    def has_live_exposure(self) -> bool:
        try:
            with get_db() as conn:
                return repo.has_live_exposure(conn)
        except Exception as exc:
            logger.warning("Failed to inspect live exposure: %s", exc)
        return bool(self.positions or self.pending_orders)

    def activate_paper_mode(self, reason: str, context: str) -> None:
        if self.effective_mode == "paper":
            return
        self.effective_mode = "paper"
        self.fallback_reason = f"{context}:{reason}"[:500]
        self.pending_fallback_reason = None
        self.protection_mode_reason = None
        self.trader = PaperTrader()
        if self.user_stream:
            self.user_stream.stop()
        self.user_stream = None
        if self.mode_run_id is not None:
            try:
                with get_db() as conn:
                    repo.update_mode_run_state(
                        conn,
                        self.mode_run_id,
                        effective_mode=self.effective_mode,
                        fallback_reason=self.fallback_reason,
                    )
                    repo.update_account_modes(
                        conn,
                        ACCOUNT_KEY,
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                    )
            except Exception as exc:
                logger.warning("Failed to persist paper fallback: %s", exc)
        self.refresh_account_state_from_db()
        self.save_state()
        logger.warning(
            "Runtime mode fallback activated: requested_mode=%s effective_mode=%s reason=%s",
            self.requested_mode,
            self.effective_mode,
            self.fallback_reason,
        )

    def enter_live_protection(self, reason: str, context: str) -> None:
        protection_reason = f"{context}:{reason}"[:500]
        if self.protection_mode_reason == protection_reason:
            return
        self.protection_mode_reason = protection_reason
        self.pending_fallback_reason = reason
        logger.critical(
            "Live protection mode active; new entries paused until live exposure is flat. reason=%s",
            protection_reason,
        )
        if self.mode_run_id is not None:
            try:
                with get_db() as conn:
                    repo.update_mode_run_state(
                        conn,
                        self.mode_run_id,
                        effective_mode=self.effective_mode,
                        fallback_reason=protection_reason,
                        status="protecting",
                    )
            except Exception as exc:
                logger.warning("Failed to persist protection-mode state: %s", exc)

    def attempt_runtime_paper_fallback(self, reason: str, context: str) -> bool:
        if self.requested_mode != "live" or self.effective_mode != "live":
            return False
        if self.has_live_exposure():
            self.enter_live_protection(reason, context)
            return False
        self.activate_paper_mode(reason, context)
        return True

    def evaluate_live_protection_transition(self) -> None:
        if not self.protection_mode_reason:
            return
        if self.has_live_exposure():
            return
        fallback_reason = self.pending_fallback_reason or "live_protection_complete"
        logger.warning("Live exposure is flat; completing fallback to paper mode")
        self.activate_paper_mode(fallback_reason, "live_protection")

    def _log_geo(self, prefix: str, location: dict[str, Any]) -> None:
        logger.info(
            "%s ip=%s city=%s region=%s country=%s loc=%s timezone=%s provider=%s",
            prefix,
            location.get("ip"),
            location.get("city"),
            location.get("region"),
            location.get("country"),
            location.get("loc"),
            location.get("timezone"),
            location.get("provider"),
        )

    def _normalize_geo_profile(self, location: dict[str, Any]) -> dict[str, str]:
        return {
            "ip": str(location.get("ip") or "").strip(),
            "country": str(location.get("country") or "").strip().upper(),
            "region": str(location.get("region") or "").strip().upper(),
        }

    def _assert_location_profile_allowed(self, location: dict[str, Any], context: str) -> dict[str, str]:
        profile = self._normalize_geo_profile(location)
        country = profile["country"]
        region = profile["region"]

        if not profile["ip"] or not country:
            raise SafetyShutdown(f"{context}: geolocation incomplete; refusing to continue")
        if country in RESTRICTED_COUNTRIES:
            raise SafetyShutdown(f"{context}: restricted country detected ({country}); terminating")
        if region and region in RESTRICTED_REGIONS.get(country, set()):
            raise SafetyShutdown(f"{context}: restricted region detected ({country}-{region}); terminating")
        return profile

    def assert_runtime_safety(self, context: str, *, log_checks: bool = False) -> None:
        if self.effective_mode == "paper" and not PAPER_GEO_STRICT:
            try:
                location = self.geolocation.locate()
                if location:
                    if log_checks:
                        self._log_geo("Paper mode geolocation (advisory):", location)
                    self._assert_location_profile_allowed(location, context)
            except SafetyShutdown as exc:
                logger.warning("[paper] safety advisory (non-fatal): %s", exc)
            return

        location = self.geolocation.locate()
        if not location:
            raise SafetyShutdown(f"{context}: geolocation unavailable; refusing to continue")
        if log_checks:
            self._log_geo("Runtime geolocation:", location)
        current_profile = self._assert_location_profile_allowed(location, context)

        if self.startup_geo_profile is None:
            self.startup_geo_profile = current_profile
        else:
            for key in ("ip", "country", "region"):
                previous = self.startup_geo_profile.get(key, "")
                current = current_profile.get(key, "")
                if previous and current and previous != current:
                    raise SafetyShutdown(
                        f"{context}: detected {key} change from {previous} to {current}; terminating"
                    )

        geo = self.polymarket_public.geoblock()
        if log_checks:
            logger.info("Polymarket geoblock: %s", geo)
        if bool(geo.get("blocked")):
            raise SafetyShutdown(f"{context}: Polymarket geoblock reports blocked=true; terminating")

        geo_country = str(geo.get("country") or "").strip().upper()
        geo_region = str(geo.get("region") or "").strip().upper()
        geo_ip = str(geo.get("ip") or "").strip()
        if geo_country in RESTRICTED_COUNTRIES:
            raise SafetyShutdown(f"{context}: Polymarket geoblock country is restricted ({geo_country}); terminating")
        if geo_region and geo_region in RESTRICTED_REGIONS.get(geo_country, set()):
            raise SafetyShutdown(f"{context}: Polymarket geoblock region is restricted ({geo_country}-{geo_region}); terminating")
        if self.startup_geo_profile:
            startup_ip = self.startup_geo_profile.get("ip", "")
            if startup_ip and geo_ip and geo_ip != startup_ip:
                raise SafetyShutdown(f"{context}: geoblock IP changed from {startup_ip} to {geo_ip}; terminating")

    def sync_realtime_subscriptions(self) -> None:
        asset_ids = {
            str(item.get("token_id"))
            for item in [*self.positions.values(), *self.pending_orders.values()]
            if item.get("token_id")
        }
        self.market_stream.set_assets(asset_ids)

        if self.user_stream:
            condition_ids = {
                str(item.get("condition_id"))
                for item in [*self.positions.values(), *self.pending_orders.values()]
                if item.get("condition_id")
            }
            self.user_stream.set_markets(condition_ids)

    def start_realtime_streams(self) -> None:
        self.sync_realtime_subscriptions()
        self.market_stream.start()
        if self.user_stream:
            self.user_stream.start()

    def current_quote_probability(self, token_id: str) -> float | None:
        quote = self.market_stream.get_quote(token_id)
        if not quote:
            return None
        reference_price = as_float(quote.get("reference_price"))
        if reference_price <= 0:
            return None
        return clamp_price(reference_price)

    def current_exit_quote(self, token_id: str) -> float | None:
        quote = self.market_stream.get_quote(token_id)
        if not quote:
            return None
        for key in ("best_bid", "mid_price", "last_trade_price", "reference_price"):
            price = as_float(quote.get(key))
            if price > 0:
                return clamp_price(price)
        return None

    def latest_position_probability(self, position: dict[str, Any]) -> float:
        token_id = str(position.get("token_id", ""))
        quote_probability = self.current_quote_probability(token_id)
        if quote_probability is not None:
            return quote_probability
        market = self.latest_market_snapshot(position)
        return self.current_position_probability(position, market)

    def has_realtime_pending_updates(self) -> bool:
        if not self.user_stream:
            return False
        return any(self.user_stream.has_update_for_order(order_id) for order_id in self.pending_orders)

    def idle_until_next_scan(self, started_at: float) -> None:
        deadline = started_at + SCAN_INTERVAL_SECONDS
        while time.time() < deadline:
            if self.has_realtime_pending_updates():
                try:
                    self.process_pending_orders()
                except SafetyShutdown:
                    raise
                except Exception as exc:
                    logger.exception("Realtime pending order monitor error: %s", exc)
            time.sleep(1)

    @staticmethod
    def response_indicates_ban_risk(response: dict[str, Any]) -> bool:
        if not isinstance(response, dict):
            return False
        text = json.dumps(response).lower()
        risk_markers = (
            "geoblock",
            "blocked",
            "forbidden",
            "restricted",
            "compliance",
            "sanction",
            "location",
            "region",
            "country",
            "not allowed",
            "not eligible",
            "prohibited",
        )
        return any(marker in text for marker in risk_markers)

    @staticmethod
    def response_indicates_live_unavailable(response: dict[str, Any]) -> str | None:
        if not isinstance(response, dict):
            return None
        text = json.dumps(response).lower()
        markers = {
            "insufficient balance": "insufficient_balance",
            "not enough balance": "insufficient_balance",
            "insufficient funds": "insufficient_balance",
            "balance too low": "insufficient_balance",
            "allowance": "allowance_missing_or_invalid",
            "unauthorized": "invalid_credentials",
            "invalid api key": "invalid_credentials",
            "invalid signature": "invalid_credentials",
            "authentication": "invalid_credentials",
            "credential": "invalid_credentials",
        }
        for marker, reason in markers.items():
            if marker in text:
                return reason
        return None

    def pending_exit_order_for_market(self, market_id: str) -> tuple[str, dict[str, Any]] | None:
        for order_id, pending in self.pending_orders.items():
            if str(pending.get("market_id")) == str(market_id):
                return order_id, pending
        return None

    def assert_live_trading_allowed(self) -> None:
        if isinstance(self.trader, LiveTrader):
            self.trader.ws_auth_payload()
        self.assert_runtime_safety("startup", log_checks=True)

    def should_trade(self, signal_payload: dict[str, Any]) -> Decision | None:
        if not signal_payload.get("success"):
            return None

        action = signal_payload.get("data", {}).get("suggested_action")
        matches = signal_payload.get("data", {}).get("markets", [])
        urgency = signal_payload.get("urgency")
        event_id = signal_payload.get("event_id")

        if not action or not matches or not event_id:
            return None
        if urgency not in {"medium", "high", "critical"}:
            return None
        if action.get("direction") not in {"YES", "NO"}:
            return None
        if float(action.get("confidence", 0)) < MIN_CONFIDENCE:
            return None
        if float(action.get("edge", 0)) < MIN_EDGE:
            return None

        ranked_markets = []
        for match in matches:
            market = match.get("market", {})
            if market.get("platform") != "polymarket":
                continue
            probability = current_probability(market, action["direction"])
            if float(market.get("volume24h", 0)) < MIN_VOLUME_24H:
                continue
            if probability <= MIN_PRICE or probability >= MAX_PRICE:
                continue
            score = float(action["confidence"]) * float(action["edge"]) * max(float(match.get("confidence", 0.5)), 0.25)
            ranked_markets.append((score, market))

        if not ranked_markets:
            return None

        ranked_markets.sort(key=lambda item: item[0], reverse=True)
        score, best_market = ranked_markets[0]
        return Decision(
            event_id=str(event_id),
            market=best_market,
            side=str(action["direction"]),
            confidence=float(action["confidence"]),
            edge=float(action["edge"]),
            reason=str(action.get("reasoning", "")),
            urgency=str(urgency),
            signal_type=signal_payload.get("signal_type"),
            probability=current_probability(best_market, str(action["direction"])),
            score=score,
        )

    def size_position(self, decision: Decision) -> float:
        remaining = min(self.bankroll_remaining(), MAX_TOTAL_EXPOSURE_USD - self.current_exposure())
        if remaining <= 0:
            return 0.0

        if decision.confidence >= 0.88 and decision.edge >= 0.10:
            size = min(MAX_POSITION_USD, 4.0)
        elif decision.confidence >= 0.82 and decision.edge >= 0.07:
            size = min(MAX_POSITION_USD, 3.0)
        else:
            size = min(MAX_POSITION_USD, 2.0)

        return round(max(0.0, min(size, remaining)), 2)

    def record_seen(self, event_id: str) -> None:
        self.seen_event_ids.add(event_id)
        try:
            with get_db() as conn:
                repo.insert_seen_event(conn, event_id)
        except Exception as exc:
            logger.warning("Failed to persist seen event %s: %s", event_id, exc)

    def apply_exit_fill_to_position(
        self,
        market_id: str,
        position: dict[str, Any],
        sold_shares: float,
        execution_price: float,
        exit_reason: str,
        response: dict[str, Any],
        order_status: str,
        order_id: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        current_shares = current_position_shares(position)
        sold_shares = min(max(sold_shares, 0.0), current_shares)
        remaining_shares = round(max(current_shares - sold_shares, 0.0), 6)
        pnl_usd = realized_pnl_usd(float(position["entry_probability"]), execution_price, sold_shares)
        total_realized_pnl_usd = round(as_float(position.get("realized_pnl_usd")) + pnl_usd, 6)

        closed_event = {
            **position,
            "closed_at": utc_now_iso(),
            "exit_reason": exit_reason,
            "exit_probability": execution_price,
            "sold_shares": sold_shares,
            "remaining_shares": remaining_shares,
            "realized_pnl_usd_estimate": pnl_usd,
            "total_realized_pnl_usd": total_realized_pnl_usd,
            "close_order_id": order_id,
            "close_order_status": order_status,
            "close_response": response,
        }

        updated_position = None
        if remaining_shares > 0:
            updated_position = {
                **position,
                "shares": remaining_shares,
                "estimated_shares": remaining_shares,
                "size_usd": round(remaining_shares * float(position["entry_probability"]), 6),
                "last_response": response,
                "realized_pnl_usd": total_realized_pnl_usd,
                "current_probability": execution_price,
                "current_value_usd": round(remaining_shares * execution_price, 6),
                "unrealized_pnl_usd": round(
                    (execution_price - float(position["entry_probability"])) * remaining_shares,
                    6,
                ),
            }
        # Callers are responsible for applying self.positions mutations so that the
        # in-memory removal/update only happens after any critical DB writes succeed.
        return closed_event, updated_position

    def already_holding(self, market_id: str, event_id: str) -> bool:
        if market_id in self.positions:
            return True
        if not LIMIT_ONE_POSITION_PER_EVENT:
            return False
        return any(position.get("event_id") == event_id for position in self.positions.values())

    def execute_trade(self, decision: Decision) -> bool:
        """Place a trade for *decision*.

        Returns True for every terminal outcome (trade placed, explicitly skipped,
        entry rejected) so the caller can mark the feed event as permanently seen.
        Returns False only for *transient* failures — currently the single case of
        insufficient order-book liquidity — so the caller can leave the event unseen
        and retry it on the next scan cycle.
        """
        if self.protection_mode_reason:
            logger.warning("Skipped new entry because protection mode is active: %s", self.protection_mode_reason)
            return True
        try:
            self.assert_runtime_safety("pre-entry")
        except SafetyShutdown as exc:
            if self.attempt_runtime_paper_fallback("startup_or_runtime_safety_failed", "pre_entry"):
                return self.execute_trade(decision)
            raise
        market = decision.market
        market_id = str(market["id"])
        client_order_id = f"entry-{uuid.uuid4()}"

        if self.already_holding(market_id, decision.event_id):
            logger.info("Skipped %s because position already exists for market/event", market_id)
            return True

        size_usd = self.size_position(decision)
        if size_usd <= 0:
            logger.info("Skipped %s because bankroll/exposure cap is full", market_id)
            return True

        gamma_market = self.gamma.resolve_market(market)
        token_id = pick_token_id(gamma_market, decision.side)
        condition_id = extract_condition_id(gamma_market) or extract_condition_id(market)

        if not self.trader.check_entry_liquidity(token_id, size_usd):
            logger.warning("Skipped %s — insufficient order book liquidity for $%.2f entry", market_id, size_usd)
            # Transient: empty order book. Return False so handle_feed_item leaves the
            # event unseen and retries it when liquidity returns.
            return False

        response = self.trader.place_market_buy(
            token_id=token_id,
            amount_usd=size_usd,
            meta={
                "side": decision.side,
                "market_title": market["title"],
                "probability": decision.probability,
            },
        )
        if self.response_indicates_ban_risk(response):
            if self.attempt_runtime_paper_fallback("compliance_or_geoblock", "pre_entry_response"):
                return self.execute_trade(decision)
            raise SafetyShutdown(f"pre-entry: Polymarket response indicates compliance/geoblock risk; terminating: {response}")  # noqa: E501
        fill = parse_fill_result(
            response=response,
            fallback_price=decision.probability,
            requested_value_usd=size_usd,
            requested_shares=estimate_shares(size_usd, decision.probability),
        )
        order_id = fill.order_id or client_order_id
        requested_shares = estimate_shares(size_usd, decision.probability)
        if not fill.success:
            logger.warning("Entry order for %s returned no fill: %s", market_id, response)
            try:
                with get_db() as conn:
                    repo.upsert_order(
                        conn,
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        market_id=market_id,
                        condition_id=condition_id,
                        token_id=token_id,
                        side="BUY",
                        execution_mode="direct_execution",
                        status=fill.status or "rejected",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        requested_price=decision.probability,
                        requested_value_usd=size_usd,
                        requested_shares=requested_shares,
                        filled_shares=fill.filled_shares,
                        executed_value_usd=fill.filled_value_usd,
                        remaining_shares=max(requested_shares - fill.filled_shares, 0.0),
                        fallback_reason=decision.reason,
                        metadata={"response": response, "signal_type": decision.signal_type, "urgency": decision.urgency},
                    )
                    repo.insert_trade_event(
                        conn,
                        action_type="buy_submitted",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        market_id=market_id,
                        token_id=token_id,
                        condition_id=condition_id,
                        status="submitted",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="direct_execution",
                        requested_price=decision.probability,
                        requested_value_usd=size_usd,
                        requested_shares=requested_shares,
                        reason=decision.reason,
                        metadata={"signal_type": decision.signal_type, "urgency": decision.urgency},
                    )
                    repo.insert_trade_event(
                        conn,
                        action_type="rejected",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        market_id=market_id,
                        token_id=token_id,
                        condition_id=condition_id,
                        status=fill.status or "rejected",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="direct_execution",
                        requested_price=decision.probability,
                        requested_value_usd=size_usd,
                        requested_shares=requested_shares,
                        filled_shares=fill.filled_shares,
                        remaining_shares=max(requested_shares - fill.filled_shares, 0.0),
                        reason=decision.reason,
                        metadata={"response": response, "signal_type": decision.signal_type, "urgency": decision.urgency},
                    )
            except Exception as exc:
                logger.warning("DB write failed (entry_rejected): %s", exc)
            fallback_reason = self.response_indicates_live_unavailable(response)
            if fallback_reason and self.attempt_runtime_paper_fallback(fallback_reason, "entry_rejected"):
                return self.execute_trade(decision)
            return True  # entry rejected — terminal, mark event seen

        current_value_usd = round(fill.filled_shares * fill.avg_price, 6)
        position = {
            "position_id": str(uuid.uuid4()),
            "event_id": decision.event_id,
            "market_id": market_id,
            "title": market["title"],
            "url": market.get("url"),
            "side": decision.side,
            "token_id": token_id,
            "condition_id": condition_id,
            "size_usd": fill.filled_value_usd,
            "shares": fill.filled_shares,
            "estimated_shares": fill.filled_shares,
            "entry_probability": fill.avg_price,
            "confidence": decision.confidence,
            "edge": decision.edge,
            "score": decision.score,
            "opened_at": utc_now_iso(),
            "mode": self.effective_mode,
            "entry_order_id": order_id,
            "entry_order_status": fill.status,
            "last_response": fill.raw_response,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "current_probability": fill.avg_price,
            "current_value_usd": current_value_usd,
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
        }
        self.positions[market_id] = position
        self.save_state()

        try:
            with get_db() as conn:
                repo.upsert_order(
                    conn,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    position_id=position["position_id"],
                    market_id=market_id,
                    condition_id=condition_id,
                    token_id=token_id,
                    side="BUY",
                    execution_mode="direct_execution",
                    status=fill.status or "filled",
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    requested_price=decision.probability,
                    executed_price=fill.avg_price,
                    requested_value_usd=size_usd,
                    executed_value_usd=fill.filled_value_usd,
                    requested_shares=requested_shares,
                    filled_shares=fill.filled_shares,
                    remaining_shares=max(requested_shares - fill.filled_shares, 0.0),
                    fallback_reason=decision.reason,
                    metadata={"response": fill.raw_response, "signal_type": decision.signal_type, "urgency": decision.urgency},
                )
                repo.upsert_position(
                    conn, position,
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                )
                repo.insert_trade_event(
                    conn,
                    action_type="buy_submitted",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    position_id=position["position_id"],
                    market_id=market_id,
                    token_id=token_id,
                    condition_id=condition_id,
                    status="submitted",
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    execution_mode="direct_execution",
                    requested_price=decision.probability,
                    requested_value_usd=size_usd,
                    requested_shares=requested_shares,
                    reason=decision.reason,
                    metadata={"signal_type": decision.signal_type, "urgency": decision.urgency},
                )
                repo.insert_trade_event(
                    conn,
                    action_type="buy_filled",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    position_id=position["position_id"],
                    market_id=market_id,
                    token_id=token_id,
                    condition_id=condition_id,
                    status=fill.status or "filled",
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    execution_mode="direct_execution",
                    requested_price=decision.probability,
                    executed_price=fill.avg_price,
                    requested_value_usd=size_usd,
                    executed_value_usd=fill.filled_value_usd,
                    requested_shares=requested_shares,
                    filled_shares=fill.filled_shares,
                    remaining_shares=max(requested_shares - fill.filled_shares, 0.0),
                    reason=decision.reason,
                    metadata={"signal_type": decision.signal_type, "urgency": decision.urgency},
                )
                repo.debit_account_on_entry(conn, ACCOUNT_KEY, fill.filled_value_usd)
            self.refresh_account_state_from_db()
        except Exception as exc:
            self._db_write_critical("buy_filled", exc)
        return True  # trade placed — terminal

    def current_position_probability(self, position: dict[str, Any], market: dict[str, Any]) -> float:
        side = str(position.get("side", "YES"))
        return current_probability(market, side)

    def latest_market_snapshot(self, position: dict[str, Any]) -> dict[str, Any]:
        stub_market = {
            "id": position.get("market_id"),
            "url": position.get("url"),
            "title": position.get("title"),
        }
        return self.gamma.resolve_market(stub_market)

    def should_exit_position(self, position: dict[str, Any], market: dict[str, Any]) -> tuple[str | None, float]:
        current_prob = self.current_position_probability(position, market)
        entry_prob = float(position.get("entry_probability", 0))
        if entry_prob <= 0:
            return "invalid_entry_probability", current_prob

        opened_at = parse_iso_datetime(str(position["opened_at"]))
        hold_minutes = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
        if current_prob >= entry_prob * (1 + TAKE_PROFIT_PCT):
            return "take_profit", current_prob
        if current_prob <= entry_prob * (1 - STOP_LOSS_PCT):
            return "stop_loss", current_prob
        if MAX_HOLD_MINUTES > 0 and hold_minutes >= MAX_HOLD_MINUTES:
            return "max_hold", current_prob
        return None, current_prob

    def reversed_signal_detected(self, position: dict[str, Any]) -> bool:
        if not EXIT_ON_SIGNAL_REVERSAL:
            return False
        title = str(position.get("title", "")).strip()
        if not title:
            return False
        try:
            signal = self.musashi.analyze_text(title, min_confidence=0.5, max_results=3)
        except Exception as exc:
            logger.warning("Failed to analyze reversal for %s: %s", position.get("market_id"), exc)
            return False
        action = signal.get("data", {}).get("suggested_action") or {}
        new_side = action.get("direction")
        confidence = float(action.get("confidence", 0))
        if new_side not in {"YES", "NO"}:
            return False
        return new_side != position.get("side") and confidence >= MIN_CONFIDENCE

    def close_position(
        self,
        market_id: str,
        position: dict[str, Any],
        exit_reason: str,
        current_prob: float,
        *,
        parent_order_id: str | None = None,
        replacement_context: dict[str, Any] | None = None,
        skip_safety_check: bool = False,
    ) -> None:
        if not skip_safety_check:
            self.assert_runtime_safety("pre-exit")
        existing_pending = self.pending_exit_order_for_market(market_id)
        client_order_id = f"exit-{uuid.uuid4()}"
        if existing_pending:
            pending_order_id, pending = existing_pending
            logger.info(
                "Skipped new exit for %s because pending exit order %s already exists with %.4f shares remaining",
                market_id,
                pending_order_id,
                as_float(pending.get("remaining_shares")),
            )
            return

        shares = current_position_shares(position)
        if shares <= 0:
            logger.warning("Skipped close for %s because shares is missing", market_id)
            return

        limit_price = clamp_price(current_prob)
        ws_exit_price = self.current_exit_quote(str(position.get("token_id", "")))
        if ws_exit_price is not None:
            limit_price = ws_exit_price
        if isinstance(self.trader, LiveTrader):
            limit_price = self.trader.get_exit_price(str(position["token_id"]), current_prob)

        response = self.trader.close_position(
            token_id=str(position["token_id"]),
            shares=shares,
            limit_price=limit_price,
            meta={
                "market_title": position["title"],
                "exit_reason": exit_reason,
                "available_shares": shares,
            },
        )
        if self.response_indicates_ban_risk(response):
            self.enter_live_protection("compliance_or_geoblock", "pre_exit_response")
            return
        fill = parse_fill_result(
            response=response,
            fallback_price=limit_price,
            requested_value_usd=round(shares * limit_price, 6),
            requested_shares=shares,
        )
        order_id = fill.order_id or client_order_id
        requested_value_usd = round(shares * limit_price, 6)
        if not fill.success:
            logger.warning("Exit order for %s returned no fill: %s", market_id, response)
            try:
                with get_db() as conn:
                    repo.upsert_order(
                        conn,
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        parent_order_id=parent_order_id,
                        position_id=position.get("position_id"),
                        market_id=market_id,
                        condition_id=position.get("condition_id"),
                        token_id=position["token_id"],
                        side="SELL",
                        execution_mode="quoted_execution",
                        status=fill.status or "rejected",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        requested_price=limit_price,
                        requested_value_usd=requested_value_usd,
                        requested_shares=shares,
                        filled_shares=fill.filled_shares,
                        executed_value_usd=fill.filled_value_usd,
                        remaining_shares=max(shares - fill.filled_shares, 0.0),
                        fallback_reason=exit_reason,
                        metadata={"response": response, "exit_reason": exit_reason},
                    )
                    repo.insert_trade_event(
                        conn,
                        action_type="quote_submitted",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        parent_order_id=parent_order_id,
                        position_id=position.get("position_id"),
                        market_id=market_id,
                        token_id=position["token_id"],
                        condition_id=position.get("condition_id"),
                        status="submitted",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        requested_price=limit_price,
                        requested_value_usd=requested_value_usd,
                        requested_shares=shares,
                        reason=exit_reason,
                    )
                    repo.insert_trade_event(
                        conn,
                        action_type="sell_submitted",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        parent_order_id=parent_order_id,
                        position_id=position.get("position_id"),
                        market_id=market_id,
                        token_id=position["token_id"],
                        condition_id=position.get("condition_id"),
                        status="submitted",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        requested_price=limit_price,
                        requested_value_usd=requested_value_usd,
                        requested_shares=shares,
                        reason=exit_reason,
                    )
                    repo.insert_trade_event(
                        conn,
                        action_type="rejected",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        position_id=position.get("position_id"),
                        market_id=market_id,
                        token_id=position["token_id"],
                        condition_id=position.get("condition_id"),
                        status=fill.status or "rejected",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        requested_price=limit_price,
                        requested_value_usd=requested_value_usd,
                        requested_shares=shares,
                        filled_shares=fill.filled_shares,
                        remaining_shares=max(shares - fill.filled_shares, 0.0),
                        reason=exit_reason,
                        metadata={"response": response},
                    )
            except Exception as exc:
                logger.warning("DB write failed (exit_rejected): %s", exc)
            fallback_reason = self.response_indicates_live_unavailable(response)
            if fallback_reason:
                self.enter_live_protection(fallback_reason, "exit_rejected")
            return

        position_id = position.get("position_id")
        closed, updated_position = self.apply_exit_fill_to_position(
            market_id=market_id,
            position=position,
            sold_shares=fill.filled_shares,
            execution_price=fill.avg_price,
            exit_reason=exit_reason,
            response=fill.raw_response,
            order_status=fill.status,
            order_id=fill.order_id,
        )

        pending_order: dict[str, Any] | None = None
        if updated_position and order_id and fill.status in {"live", "open", "matched", "partially_filled", "partially_matched"}:
            pending_order = {
                "market_id": market_id,
                "token_id": position["token_id"],
                "condition_id": position.get("condition_id"),
                "position_id": position_id,
                "side": "SELL",
                "exit_reason": exit_reason,
                "order_id": order_id,
                "created_at": utc_now_iso(),
                "last_checked_at": utc_now_iso(),
                "initial_shares": shares,
                "filled_shares": fill.filled_shares,
                "remaining_shares": current_position_shares(updated_position),
                "limit_price": limit_price,
                "executed_value_usd": round(fill.filled_shares * fill.avg_price, 6),
                "parent_order_id": parent_order_id,
                "root_order_id": parent_order_id or order_id,
            }
            self.pending_orders[order_id] = pending_order

        self.save_state()

        try:
            with get_db() as conn:
                repo.upsert_order(
                    conn,
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    parent_order_id=parent_order_id,
                    position_id=position_id,
                    market_id=market_id,
                    condition_id=position.get("condition_id"),
                    token_id=position["token_id"],
                    side="SELL",
                    execution_mode="quoted_execution",
                    status="open" if pending_order is not None else (fill.status or "filled"),
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    requested_price=limit_price,
                    executed_price=fill.avg_price,
                    requested_value_usd=requested_value_usd,
                    executed_value_usd=fill.filled_shares * fill.avg_price,
                    requested_shares=shares,
                    filled_shares=fill.filled_shares,
                    remaining_shares=current_position_shares(updated_position) if updated_position else 0.0,
                    fallback_reason=exit_reason,
                    metadata=(pending_order or {"response": fill.raw_response, "exit_reason": exit_reason}),
                )
                repo.insert_trade_event(
                    conn,
                    action_type="quote_submitted",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    parent_order_id=parent_order_id,
                    position_id=position_id,
                    market_id=market_id,
                    token_id=position["token_id"],
                    condition_id=position.get("condition_id"),
                    status="submitted",
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    execution_mode="quoted_execution",
                    requested_price=limit_price,
                    requested_value_usd=requested_value_usd,
                    requested_shares=shares,
                    reason=exit_reason,
                )
                repo.insert_trade_event(
                    conn,
                    action_type="sell_submitted",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    parent_order_id=parent_order_id,
                    position_id=position_id,
                    market_id=market_id,
                    token_id=position["token_id"],
                    condition_id=position.get("condition_id"),
                    status="submitted",
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    execution_mode="quoted_execution",
                    requested_price=limit_price,
                    requested_value_usd=requested_value_usd,
                    requested_shares=shares,
                    reason=exit_reason,
                )
                if parent_order_id and replacement_context:
                    previous_shares = as_float(replacement_context.get("previous_requested_shares"))
                    if previous_shares > 0 and abs(previous_shares - shares) > 1e-6:
                        repo.insert_trade_event(
                            conn,
                            action_type="amount_modified",
                            order_id=order_id,
                            client_order_id=client_order_id,
                            venue_order_id=fill.order_id,
                            parent_order_id=parent_order_id,
                            position_id=position_id,
                            market_id=market_id,
                            token_id=position["token_id"],
                            condition_id=position.get("condition_id"),
                            status="amount_modified",
                            requested_mode=self.requested_mode,
                            effective_mode=self.effective_mode,
                            execution_mode="quoted_execution",
                            requested_price=limit_price,
                            requested_value_usd=requested_value_usd,
                            requested_shares=shares,
                            remaining_shares=current_position_shares(updated_position) if updated_position else 0.0,
                            reason=exit_reason,
                            metadata={
                                "replaced_order_id": parent_order_id,
                                "previous_requested_shares": previous_shares,
                            },
                        )
                    repo.insert_trade_event(
                        conn,
                        action_type="repriced",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        parent_order_id=parent_order_id,
                        position_id=position_id,
                        market_id=market_id,
                        token_id=position["token_id"],
                        condition_id=position.get("condition_id"),
                        status="repriced",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        requested_price=limit_price,
                        requested_value_usd=requested_value_usd,
                        requested_shares=shares,
                        remaining_shares=current_position_shares(updated_position) if updated_position else 0.0,
                        reason=exit_reason,
                        metadata={
                            "replaced_order_id": parent_order_id,
                            "previous_price": replacement_context.get("previous_price"),
                        },
                    )
                if updated_position is None:
                    repo.close_position_in_db(
                        conn, position_id,
                        closed.get("total_realized_pnl_usd", 0),
                    )
                else:
                    repo.upsert_position(
                        conn, updated_position,
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                    )
                repo.credit_account_on_exit(
                    conn,
                    ACCOUNT_KEY,
                    position_cost_basis_usd(position, fill.filled_shares),
                    fill.filled_shares * fill.avg_price,
                    closed.get("realized_pnl_usd_estimate", 0),
                )
                if pending_order is not None:
                    repo.upsert_pending_order(
                        conn, pending_order,
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                    )
                repo.insert_trade_event(
                    conn,
                    action_type="sell_filled" if updated_position is None else "partial_fill",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    venue_order_id=fill.order_id,
                    position_id=position_id,
                    market_id=market_id,
                    token_id=position["token_id"],
                    condition_id=position.get("condition_id"),
                    status=fill.status or ("filled" if updated_position is None else "partially_filled"),
                    requested_mode=self.requested_mode,
                    effective_mode=self.effective_mode,
                    execution_mode="quoted_execution",
                    requested_price=limit_price,
                    executed_price=fill.avg_price,
                    executed_value_usd=fill.filled_shares * fill.avg_price,
                    requested_value_usd=requested_value_usd,
                    requested_shares=shares,
                    filled_shares=fill.filled_shares,
                    remaining_shares=(
                        current_position_shares(updated_position) if updated_position else 0.0
                    ),
                    reason=exit_reason,
                )
                if pending_order is not None:
                    repo.insert_trade_event(
                        conn,
                        action_type="pending",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        position_id=position_id,
                        market_id=market_id,
                        token_id=position["token_id"],
                        condition_id=position.get("condition_id"),
                        status="open",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        requested_price=limit_price,
                        requested_value_usd=requested_value_usd,
                        requested_shares=shares,
                        filled_shares=fill.filled_shares,
                        remaining_shares=current_position_shares(updated_position),
                        reason=exit_reason,
                    )
                else:
                    repo.insert_trade_event(
                        conn,
                        action_type="position_closed",
                        order_id=order_id,
                        client_order_id=client_order_id,
                        venue_order_id=fill.order_id,
                        position_id=position_id,
                        market_id=market_id,
                        token_id=position["token_id"],
                        condition_id=position.get("condition_id"),
                        status="closed",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        executed_price=fill.avg_price,
                        executed_value_usd=fill.filled_shares * fill.avg_price,
                        filled_shares=fill.filled_shares,
                        remaining_shares=0.0,
                        reason=exit_reason,
                        metadata={"total_realized_pnl_usd": closed.get("total_realized_pnl_usd", 0)},
                    )
        except Exception as exc:
            self._db_write_critical("close_position", exc)
        # DB write succeeded (or paper mode logged and continued).
        # Only now apply in-memory mutations so that a crash between the exchange
        # fill and the DB write leaves the position still visible in self.positions,
        # which persist_runtime_state() can flush to Postgres before exiting.
        if updated_position is None:
            self.positions.pop(market_id, None)
        else:
            self.positions[market_id] = updated_position
        self.sync_account_market_state(refresh_prices=False)

    def monitor_positions(self) -> None:
        if self.positions:
            self.sync_account_market_state(refresh_prices=True)
        for market_id, position in list(self.positions.items()):
            try:
                if self.pending_exit_order_for_market(market_id):
                    continue
                current_prob = as_float(
                    position.get("current_probability"),
                    self.latest_position_probability(position),
                )
                if self.protection_mode_reason:
                    exit_reason = "live_protection_exit"
                else:
                    exit_reason, current_prob = self.should_exit_position(
                        position,
                        {"yesPrice": current_prob, "noPrice": current_prob},
                    )
                    if not exit_reason and self.reversed_signal_detected(position):
                        exit_reason = "signal_reversal"
                if exit_reason:
                    self.close_position(
                        market_id,
                        position,
                        exit_reason,
                        current_prob,
                        skip_safety_check=(exit_reason == "live_protection_exit"),
                    )
            except SafetyShutdown:
                raise
            except Exception as exc:
                logger.exception("Position monitor error for %s: %s", market_id, exc)

    def process_pending_orders(self) -> None:
        if not self.pending_orders:
            return

        now = datetime.now(timezone.utc)
        for order_id, pending in list(self.pending_orders.items()):
            market_id = str(pending.get("market_id"))
            position = self.positions.get(market_id)
            if not position:
                self.pending_orders.pop(order_id, None)
                continue

            try:
                self.assert_runtime_safety(f"pending-order:{order_id}")
                if self.user_stream:
                    self.user_stream.pop_order_event(order_id)
                status_response = self.trader.get_order_status(order_id)
                if self.response_indicates_ban_risk(status_response):
                    self.enter_live_protection("compliance_or_geoblock", f"pending_order:{order_id}")
                    continue
                fallback_price = as_float(pending.get("limit_price"), as_float(position.get("entry_probability"), 0.5))
                status = parse_order_status(status_response, fallback_price=fallback_price)
                pending["last_checked_at"] = utc_now_iso()

                initial_shares = as_float(pending.get("initial_shares"))
                previously_remaining = as_float(pending.get("remaining_shares"), current_position_shares(position))
                newly_filled = round(max(previously_remaining - status.remaining_shares, 0.0), 6)
                cumulative_filled_shares = max(initial_shares - status.remaining_shares, 0.0)
                cumulative_executed_value = cumulative_executed_value_usd(
                    initial_shares,
                    status.remaining_shares,
                    status.avg_price,
                )
                pending["filled_shares"] = cumulative_filled_shares
                pending["executed_value_usd"] = cumulative_executed_value

                pos_position_id = position.get("position_id") if position else None

                if newly_filled > 0:
                    closed, updated_position = self.apply_exit_fill_to_position(
                        market_id=market_id,
                        position=position,
                        sold_shares=newly_filled,
                        execution_price=status.avg_price,
                        exit_reason=str(pending.get("exit_reason", "pending_exit_fill")),
                        response=status.raw_response,
                        order_status=status.status,
                        order_id=status.order_id,
                    )
                    try:
                        with get_db() as conn:
                            repo.upsert_order(
                                conn,
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                condition_id=pending.get("condition_id"),
                                token_id=str(pending.get("token_id", "")),
                                side="SELL",
                                execution_mode="quoted_execution",
                                status=status.status or "open",
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                requested_price=as_float(pending.get("limit_price")),
                                executed_price=status.avg_price,
                                requested_value_usd=round(
                                    initial_shares * as_float(pending.get("limit_price")),
                                    6,
                                ),
                                executed_value_usd=cumulative_executed_value,
                                requested_shares=initial_shares,
                                filled_shares=cumulative_filled_shares,
                                remaining_shares=status.remaining_shares,
                                fallback_reason=str(pending.get("exit_reason", "pending_exit_fill")),
                                metadata={**pending, "last_status": status.raw_response},
                            )
                            if updated_position is None:
                                repo.close_position_in_db(
                                    conn, pos_position_id,
                                    closed.get("total_realized_pnl_usd", 0),
                                )
                            else:
                                repo.upsert_position(
                                    conn, updated_position,
                                    requested_mode=self.requested_mode,
                                    effective_mode=self.effective_mode,
                                )
                            repo.credit_account_on_exit(
                                conn,
                                ACCOUNT_KEY,
                                position_cost_basis_usd(position, newly_filled),
                                newly_filled * status.avg_price,
                                closed.get("realized_pnl_usd_estimate", 0),
                            )
                            repo.insert_trade_event(
                                conn,
                                action_type="sell_filled" if updated_position is None else "partial_fill",
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                status=status.status,
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                execution_mode="quoted_execution",
                                requested_price=as_float(pending.get("limit_price")),
                                executed_price=status.avg_price,
                                executed_value_usd=newly_filled * status.avg_price,
                                requested_value_usd=round(
                                    initial_shares * as_float(pending.get("limit_price")),
                                    6,
                                ),
                                requested_shares=initial_shares,
                                filled_shares=newly_filled,
                                remaining_shares=status.remaining_shares,
                                reason=str(pending.get("exit_reason", "pending_exit_fill")),
                            )
                            if updated_position is None:
                                repo.insert_trade_event(
                                    conn,
                                    action_type="position_closed",
                                    order_id=order_id,
                                    position_id=pos_position_id,
                                    market_id=market_id,
                                    token_id=str(pending.get("token_id", "")),
                                    condition_id=pending.get("condition_id"),
                                    status="closed",
                                    requested_mode=self.requested_mode,
                                    effective_mode=self.effective_mode,
                                    execution_mode="quoted_execution",
                                    executed_price=status.avg_price,
                                    executed_value_usd=newly_filled * status.avg_price,
                                    filled_shares=newly_filled,
                                    remaining_shares=0.0,
                                    reason=str(pending.get("exit_reason", "pending_exit_fill")),
                                    metadata={"total_realized_pnl_usd": closed.get("total_realized_pnl_usd", 0)},
                                )
                    except Exception as exc:
                        logger.warning("DB write failed (exit_fill_update): %s", exc)
                    # Apply in-memory mutations now (non-critical path — continue on DB failure)
                    if updated_position is None:
                        self.positions.pop(market_id, None)
                    else:
                        self.positions[market_id] = updated_position
                    position = updated_position
                    self.sync_account_market_state(refresh_prices=False, persist_positions=False)

                if status.remaining_shares <= 0 or status.is_terminal:
                    self.pending_orders.pop(order_id, None)
                    try:
                        with get_db() as conn:
                            repo.close_order_in_db(conn, order_id, status.status)
                            if status.status == "rejected":
                                repo.insert_trade_event(
                                    conn,
                                    action_type="rejected",
                                    order_id=order_id,
                                    position_id=pos_position_id,
                                    market_id=market_id,
                                    token_id=str(pending.get("token_id", "")),
                                    condition_id=pending.get("condition_id"),
                                    status=status.status,
                                    requested_mode=self.requested_mode,
                                    effective_mode=self.effective_mode,
                                    execution_mode="quoted_execution",
                                    requested_price=as_float(pending.get("limit_price")),
                                    requested_value_usd=round(
                                        initial_shares * as_float(pending.get("limit_price")),
                                        6,
                                    ),
                                    requested_shares=initial_shares,
                                    remaining_shares=status.remaining_shares,
                                    reason=str(pending.get("exit_reason", "pending_exit")),
                                    metadata={"last_status": status.raw_response},
                                )
                    except Exception as exc:
                        logger.warning("DB write failed (close_order terminal): %s", exc)
                    self.save_state()
                    # A rejected exit leaves the position unmanaged.  Immediately
                    # resubmit so the position is not silently stranded open.
                    if status.status == "rejected":
                        live_position = self.positions.get(market_id)
                        if live_position and current_position_shares(live_position) > 0:
                            replacement_prob = as_float(
                                live_position.get("current_probability"),
                                as_float(live_position.get("entry_probability"), 0.5),
                            )
                            logger.warning(
                                "[rejected-retry] Exit for %s was rejected — resubmitting "
                                "(original reason: %s, replacement prob: %.4f)",
                                market_id, pending.get("exit_reason"), replacement_prob,
                            )
                            try:
                                self.close_position(
                                    market_id,
                                    live_position,
                                    f"rejected_retry:{pending.get('exit_reason', 'pending_exit')}",
                                    replacement_prob,
                                )
                            except SafetyShutdown:
                                raise
                            except Exception as exc:
                                logger.exception(
                                    "[rejected-retry] Failed to resubmit exit for %s: %s",
                                    market_id, exc,
                                )
                    continue

                pending["remaining_shares"] = status.remaining_shares
                created_at = parse_iso_datetime(str(pending["created_at"]))
                age_seconds = (now - created_at).total_seconds()

                if EXIT_ORDER_REPRICE and age_seconds >= EXIT_ORDER_TIMEOUT_SECONDS:
                    try:
                        with get_db() as conn:
                            repo.insert_trade_event(
                                conn,
                                action_type="quote_expired",
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                status="expired",
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                execution_mode="quoted_execution",
                                requested_price=as_float(pending.get("limit_price")),
                                requested_value_usd=round(
                                    initial_shares * as_float(pending.get("limit_price")),
                                    6,
                                ),
                                requested_shares=initial_shares,
                                remaining_shares=status.remaining_shares,
                                reason="exit_quote_timeout",
                            )
                            repo.insert_trade_event(
                                conn,
                                action_type="cancel_requested",
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                status="cancel_requested",
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                execution_mode="quoted_execution",
                                requested_price=as_float(pending.get("limit_price")),
                                requested_value_usd=round(
                                    initial_shares * as_float(pending.get("limit_price")),
                                    6,
                                ),
                                requested_shares=initial_shares,
                                remaining_shares=status.remaining_shares,
                                reason="exit_cancel_reprice",
                            )
                    except Exception as exc:
                        logger.warning("DB write failed (pre_cancel_reprice): %s", exc)

                    cancel_response = self.trader.cancel_order(order_id)
                    self.pending_orders.pop(order_id, None)
                    try:
                        with get_db() as conn:
                            repo.close_order_in_db(conn, order_id, "canceled")
                            repo.insert_trade_event(
                                conn,
                                action_type="canceled",
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                reason="exit_cancel_reprice",
                                metadata={"cancel_response": cancel_response},
                            )
                            repo.insert_trade_event(
                                conn,
                                action_type="reprice_requested",
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                status="reprice_requested",
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                execution_mode="quoted_execution",
                                requested_price=as_float(pending.get("limit_price")),
                                requested_value_usd=round(
                                    initial_shares * as_float(pending.get("limit_price")),
                                    6,
                                ),
                                requested_shares=initial_shares,
                                remaining_shares=status.remaining_shares,
                                reason="exit_cancel_reprice",
                                metadata={"cancel_response": cancel_response},
                            )
                    except Exception as exc:
                        logger.warning("DB write failed (cancel_reprice): %s", exc)

                    refreshed_position = self.positions.get(market_id)
                    if not refreshed_position:
                        self.save_state()
                        continue

                    latest_market = self.latest_market_snapshot(refreshed_position)
                    current_prob = self.current_position_probability(refreshed_position, latest_market)
                    self.close_position(
                        market_id=market_id,
                        position=refreshed_position,
                        exit_reason=f"{pending.get('exit_reason', 'pending_exit')}_reprice",
                        current_prob=current_prob,
                        parent_order_id=order_id,
                        replacement_context={
                            "previous_requested_shares": pending.get("initial_shares"),
                            "previous_price": pending.get("limit_price"),
                        },
                    )
                    self.save_state()
                    continue

                self.pending_orders[order_id] = pending
                try:
                    with get_db() as conn:
                        repo.upsert_order(
                            conn,
                            order_id=order_id,
                            position_id=pos_position_id,
                            market_id=market_id,
                            condition_id=pending.get("condition_id"),
                            token_id=str(pending.get("token_id", "")),
                            side="SELL",
                            execution_mode="quoted_execution",
                            status=status.status or "open",
                            requested_mode=self.requested_mode,
                            effective_mode=self.effective_mode,
                            requested_price=as_float(pending.get("limit_price")),
                            executed_price=status.avg_price,
                            requested_value_usd=round(
                                initial_shares * as_float(pending.get("limit_price")),
                                6,
                            ),
                            executed_value_usd=cumulative_executed_value,
                            requested_shares=initial_shares,
                            filled_shares=cumulative_filled_shares,
                            remaining_shares=status.remaining_shares,
                            fallback_reason=str(pending.get("exit_reason", "pending_exit")),
                            metadata={**pending, "last_status": status.raw_response},
                        )
                        repo.insert_trade_event(
                            conn,
                            action_type="pending",
                            order_id=order_id,
                            position_id=pos_position_id,
                            market_id=market_id,
                            token_id=str(pending.get("token_id", "")),
                            condition_id=pending.get("condition_id"),
                            status=status.status,
                            requested_mode=self.requested_mode,
                            effective_mode=self.effective_mode,
                            execution_mode="quoted_execution",
                            requested_price=as_float(pending.get("limit_price")),
                            requested_value_usd=round(
                                initial_shares * as_float(pending.get("limit_price")),
                                6,
                            ),
                            requested_shares=initial_shares,
                            filled_shares=cumulative_filled_shares,
                            remaining_shares=status.remaining_shares,
                            reason=str(pending.get("exit_reason", "pending_exit")),
                        )
                except Exception as exc:
                    logger.warning("DB write failed (update_remaining): %s", exc)
                self.save_state()
            except SafetyShutdown:
                raise
            except Exception as exc:
                logger.exception("Pending order monitor error for %s: %s", order_id, exc)

    def reconcile_startup_state(self) -> None:
        if self.effective_mode == "live":
            self.assert_live_trading_allowed()
        else:
            if not STARTUP_RECONCILE:
                return
            # Paper mode: clean up zero-share positions; skip live API calls
            for market_id, pos in list(self.positions.items()):
                if current_position_shares(pos) <= 0:
                    self.positions.pop(market_id, None)
                    try:
                        with get_db() as conn:
                            repo.close_position_in_db(conn, pos.get("position_id"), 0)
                    except Exception as exc:
                        logger.warning("DB write failed (paper startup reconcile): %s", exc)
            self.sync_account_market_state(refresh_prices=False)
            self.save_state()
            return

        if not STARTUP_RECONCILE:
            return

        logger.info(
            "Startup reconcile: %d positions, %d pending orders",
            len(self.positions),
            len(self.pending_orders),
        )

        changed = False

        for order_id, pending in list(self.pending_orders.items()):
            market_id = str(pending.get("market_id"))
            position = self.positions.get(market_id)

            if not position:
                logger.warning("Removing zombie pending order %s because market %s has no local position", order_id, market_id)
                self.pending_orders.pop(order_id, None)
                try:
                    with get_db() as conn:
                        repo.close_order_in_db(conn, order_id, "orphaned")
                except Exception as exc:
                    logger.warning("DB write failed (orphaned startup order): %s", exc)
                changed = True
                continue

            try:
                self.assert_runtime_safety(f"startup-reconcile:{order_id}")
                status_response = self.trader.get_order_status(order_id)
                if self.response_indicates_ban_risk(status_response):
                    self.enter_live_protection("compliance_or_geoblock", f"startup_reconcile:{order_id}")
                    continue
                fallback_price = as_float(pending.get("limit_price"), as_float(position.get("entry_probability"), 0.5))
                status = parse_order_status(status_response, fallback_price=fallback_price)
            except SafetyShutdown:
                raise
            except Exception as exc:
                logger.warning("Could not reconcile pending order %s: %s", order_id, exc)
                continue

            previously_remaining = as_float(pending.get("remaining_shares"), current_position_shares(position))
            newly_filled = round(max(previously_remaining - status.remaining_shares, 0.0), 6)
            initial_shares = as_float(pending.get("initial_shares"))
            cumulative_filled_shares = max(initial_shares - status.remaining_shares, 0.0)
            cumulative_executed_value = cumulative_executed_value_usd(
                initial_shares,
                status.remaining_shares,
                status.avg_price,
            )
            pending["filled_shares"] = cumulative_filled_shares
            pending["executed_value_usd"] = cumulative_executed_value

            if newly_filled > 0:
                pos_position_id = position.get("position_id")
                closed, updated_position = self.apply_exit_fill_to_position(
                    market_id=market_id,
                    position=position,
                    sold_shares=newly_filled,
                    execution_price=status.avg_price,
                    exit_reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                    response=status.raw_response,
                    order_status=status.status,
                    order_id=status.order_id,
                )
                try:
                    with get_db() as conn:
                        repo.upsert_order(
                            conn,
                            order_id=order_id,
                            position_id=pos_position_id,
                            market_id=market_id,
                            condition_id=pending.get("condition_id"),
                            token_id=str(pending.get("token_id", "")),
                            side="SELL",
                            execution_mode="quoted_execution",
                            status=status.status or "open",
                            requested_mode=self.requested_mode,
                            effective_mode=self.effective_mode,
                            requested_price=as_float(pending.get("limit_price")),
                            executed_price=status.avg_price,
                            requested_value_usd=round(
                                initial_shares * as_float(pending.get("limit_price")),
                                6,
                            ),
                            executed_value_usd=cumulative_executed_value,
                            requested_shares=initial_shares,
                            filled_shares=cumulative_filled_shares,
                            remaining_shares=status.remaining_shares,
                            fallback_reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                            metadata={**pending, "last_status": status.raw_response},
                        )
                        if updated_position is None:
                            repo.close_position_in_db(
                                conn, pos_position_id,
                                closed.get("total_realized_pnl_usd", 0),
                            )
                        else:
                            repo.upsert_position(
                                conn, updated_position,
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                            )
                        repo.credit_account_on_exit(
                            conn,
                            ACCOUNT_KEY,
                            position_cost_basis_usd(position, newly_filled),
                            newly_filled * status.avg_price,
                            closed.get("realized_pnl_usd_estimate", 0),
                        )
                        repo.insert_trade_event(
                            conn,
                            action_type="sell_filled" if updated_position is None else "partial_fill",
                            order_id=order_id,
                            position_id=pos_position_id,
                            market_id=market_id,
                            token_id=str(pending.get("token_id", "")),
                            condition_id=pending.get("condition_id"),
                            status=status.status,
                            requested_mode=self.requested_mode,
                            effective_mode=self.effective_mode,
                            execution_mode="quoted_execution",
                            requested_price=as_float(pending.get("limit_price")),
                            executed_price=status.avg_price,
                            executed_value_usd=newly_filled * status.avg_price,
                            requested_value_usd=round(
                                initial_shares * as_float(pending.get("limit_price")),
                                6,
                            ),
                            requested_shares=initial_shares,
                            filled_shares=newly_filled,
                            remaining_shares=status.remaining_shares,
                            reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                        )
                        if updated_position is None:
                            repo.insert_trade_event(
                                conn,
                                action_type="position_closed",
                                order_id=order_id,
                                position_id=pos_position_id,
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                status="closed",
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                execution_mode="quoted_execution",
                                executed_price=status.avg_price,
                                executed_value_usd=newly_filled * status.avg_price,
                                filled_shares=newly_filled,
                                remaining_shares=0.0,
                                reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                                metadata={"total_realized_pnl_usd": closed.get("total_realized_pnl_usd", 0)},
                            )
                except Exception as exc:
                    logger.warning("DB write failed (startup_reconcile_fill): %s", exc)
                # Apply in-memory mutations now (startup path — continue on DB failure)
                if updated_position is None:
                    self.positions.pop(market_id, None)
                else:
                    self.positions[market_id] = updated_position
                position = updated_position
                changed = True

            refreshed_position = self.positions.get(market_id)
            if not refreshed_position:
                self.pending_orders.pop(order_id, None)
                changed = True
                continue

            if status.remaining_shares <= 0 or status.is_terminal:
                logger.info("Removing terminal pending order %s with status=%s", order_id, status.status)
                self.pending_orders.pop(order_id, None)
                try:
                    with get_db() as conn:
                        repo.close_order_in_db(conn, order_id, status.status)
                        if status.status == "rejected":
                            repo.insert_trade_event(
                                conn,
                                action_type="rejected",
                                order_id=order_id,
                                position_id=position.get("position_id"),
                                market_id=market_id,
                                token_id=str(pending.get("token_id", "")),
                                condition_id=pending.get("condition_id"),
                                status=status.status,
                                requested_mode=self.requested_mode,
                                effective_mode=self.effective_mode,
                                execution_mode="quoted_execution",
                                requested_price=as_float(pending.get("limit_price")),
                                requested_value_usd=round(
                                    initial_shares * as_float(pending.get("limit_price")),
                                    6,
                                ),
                                requested_shares=initial_shares,
                                remaining_shares=status.remaining_shares,
                                reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                                metadata={"last_status": status.raw_response},
                            )
                except Exception as exc:
                    logger.warning("DB write failed (terminal startup order): %s", exc)
                changed = True
                # A rejected exit at startup leaves the position unmanaged.
                # Resubmit immediately so startup reconcile doesn't leave it stranded.
                if status.status == "rejected":
                    live_position = self.positions.get(market_id)
                    if live_position and current_position_shares(live_position) > 0:
                        replacement_prob = as_float(
                            live_position.get("current_probability"),
                            as_float(live_position.get("entry_probability"), 0.5),
                        )
                        logger.warning(
                            "[rejected-retry] Startup reconcile: exit for %s was rejected — "
                            "resubmitting (original reason: %s, prob: %.4f)",
                            market_id, pending.get("exit_reason"), replacement_prob,
                        )
                        try:
                            self.close_position(
                                market_id,
                                live_position,
                                f"rejected_startup:{pending.get('exit_reason', 'startup_reconcile')}",
                                replacement_prob,
                            )
                        except SafetyShutdown:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "[rejected-retry] Failed to resubmit startup exit for %s: %s",
                                market_id, exc,
                            )
                continue

            self.pending_orders[order_id]["remaining_shares"] = status.remaining_shares
            self.pending_orders[order_id]["last_checked_at"] = utc_now_iso()
            try:
                with get_db() as conn:
                    repo.upsert_order(
                        conn,
                        order_id=order_id,
                        position_id=position.get("position_id"),
                        market_id=market_id,
                        condition_id=pending.get("condition_id"),
                        token_id=str(pending.get("token_id", "")),
                        side="SELL",
                        execution_mode="quoted_execution",
                        status=status.status or "open",
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        requested_price=as_float(pending.get("limit_price")),
                        executed_price=status.avg_price,
                        requested_value_usd=round(
                            initial_shares * as_float(pending.get("limit_price")),
                            6,
                        ),
                        executed_value_usd=cumulative_executed_value,
                        requested_shares=initial_shares,
                        filled_shares=cumulative_filled_shares,
                        remaining_shares=status.remaining_shares,
                        fallback_reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                        metadata={**pending, "last_status": status.raw_response},
                    )
                    repo.insert_trade_event(
                        conn,
                        action_type="pending",
                        order_id=order_id,
                        position_id=position.get("position_id"),
                        market_id=market_id,
                        token_id=str(pending.get("token_id", "")),
                        condition_id=pending.get("condition_id"),
                        status=status.status,
                        requested_mode=self.requested_mode,
                        effective_mode=self.effective_mode,
                        execution_mode="quoted_execution",
                        requested_price=as_float(pending.get("limit_price")),
                        requested_value_usd=round(
                            initial_shares * as_float(pending.get("limit_price")),
                            6,
                        ),
                        requested_shares=initial_shares,
                        filled_shares=cumulative_filled_shares,
                        remaining_shares=status.remaining_shares,
                        reason=f"{pending.get('exit_reason', 'startup_reconcile')}_startup_reconcile",
                    )
            except Exception as exc:
                logger.warning("DB write failed (startup pending update): %s", exc)
            changed = True

        pending_market_ids = {str(pending.get("market_id")) for pending in self.pending_orders.values()}
        for market_id, position in list(self.positions.items()):
            shares = current_position_shares(position)
            if shares <= 0:
                logger.warning("Removing inconsistent local position %s because remaining shares <= 0", market_id)
                self.positions.pop(market_id, None)
                changed = True
                continue

            if market_id in pending_market_ids:
                continue

        if changed:
            self.save_state()
        self.sync_account_market_state(refresh_prices=bool(self.positions))

    def handle_feed_item(self, item: dict[str, Any]) -> None:
        event_id = item.get("event_id")
        if not event_id or event_id in self.seen_event_ids:
            return

        tweet = item.get("tweet", {})
        tweet_text = tweet.get("text", "")
        if not tweet_text or not tweet_text.strip():
            self.record_seen(str(event_id))
            return

        signal = self.musashi.analyze_text(tweet_text, min_confidence=0.5, max_results=3)
        decision = self.should_trade(signal)
        if not decision:
            # No actionable signal — permanently consume the event.
            self.record_seen(str(event_id))
            return
        # execute_trade returns False for transient skips (e.g. empty order book).
        # Only mark the event seen for terminal outcomes so that a temporary
        # liquidity gap does not permanently suppress a valid signal.
        if self.execute_trade(decision):
            self.record_seen(str(event_id))

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        logger.info("SIGTERM received — initiating clean shutdown")
        self._shutdown_requested = True

    def _shutdown(self, reason: str = "normal") -> None:
        self.persist_runtime_state(reason=reason)
        if self.positions:
            self.sync_account_market_state(refresh_prices=False)
        if self.mode_run_id is not None:
            try:
                with get_db() as conn:
                    repo.close_mode_run(conn, self.mode_run_id, reason[:500])
            except Exception as exc:
                logger.warning("Failed to close mode_run on shutdown: %s", exc)
        try:
            close_pool()
        except Exception as exc:
            logger.warning("Failed to close DB pool: %s", exc)

    def run(self) -> None:
        shutdown_reason = "normal"
        try:
            try:
                health = self.musashi.health()
                logger.info("Musashi health: %s", bool(health.get("success")))
            except Exception as exc:
                logger.warning("Musashi health check failed: %s", exc)
            logger.info(
                "Bot requested_mode=%s effective_mode=%s fallback_reason=%s "
                "bankroll=%.2f max_position=%.2f exposure_cap=%.2f",
                self.requested_mode,
                self.effective_mode,
                self.fallback_reason,
                BANKROLL_USD,
                MAX_POSITION_USD,
                MAX_TOTAL_EXPOSURE_USD,
            )
            self.start_realtime_streams()
            self.reconcile_startup_state()
            if self.positions:
                self.sync_account_market_state(refresh_prices=False)

            if BOT_ENABLE_ARBITRAGE and self.arbitrage_strategy:
                logger.info("Starting arbitrage scanner (simulation-only mode)")
                self.arbitrage_thread = threading.Thread(
                    target=self.arbitrage_strategy.run_scanner,
                    name="arbitrage-scanner",
                    daemon=True,
                )
                self.arbitrage_thread.start()
            else:
                logger.info(
                    "Arbitrage scanner disabled (set BOT_ENABLE_ARBITRAGE=true to enable)"
                )

            logger.info("Bot entered main loop (effective_mode=%s)", self.effective_mode)

            while not self._shutdown_requested:
                loop_started_at = time.time()
                try:
                    self.process_pending_orders()
                    self.monitor_positions()
                    self.evaluate_live_protection_transition()
                    feed = self.musashi.get_feed(limit=20, min_urgency="medium")
                    logger.info("Fetched %d feed items", len(feed))
                    for item in feed:
                        self.handle_feed_item(item)
                except SafetyShutdown:
                    raise
                except requests_exceptions.ConnectTimeout:
                    logger.warning(
                        "Musashi feed connect timeout after %.1fs; retrying next cycle",
                        MUSASHI_CONNECT_TIMEOUT_SECONDS,
                    )
                except requests_exceptions.ReadTimeout:
                    logger.warning(
                        "Musashi feed read timeout after %.1fs (connect timeout %.1fs); retrying next cycle",
                        MUSASHI_READ_TIMEOUT_SECONDS,
                        MUSASHI_CONNECT_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    logger.exception("Loop error: %s", exc)
                if self.mode_run_id is not None:
                    try:
                        with get_db() as conn:
                            repo.update_mode_heartbeat(conn, self.mode_run_id)
                    except Exception as exc:
                        logger.warning("Heartbeat update failed: %s", exc)
                self.idle_until_next_scan(loop_started_at)

            shutdown_reason = "sigterm"

        except SafetyShutdown as exc:
            logger.critical("Safety shutdown: %s", exc)
            shutdown_reason = f"safety_shutdown:{str(exc)[:200]}"
            raise SystemExit(1) from exc
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down")
            shutdown_reason = "keyboard_interrupt"
            raise
        finally:
            self._shutdown(reason=shutdown_reason)
            self.market_stream.stop()
            if self.user_stream:
                self.user_stream.stop()


if __name__ == "__main__":
    configure_logging()
    Bot().run()
