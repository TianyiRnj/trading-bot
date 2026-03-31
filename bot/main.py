import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("MUSASHI_API_BASE_URL", "https://musashi-api.vercel.app").rstrip("/")
BOT_MODE = os.getenv("BOT_MODE", "paper").strip().lower()
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

POLYMARKET_HOST = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com").rstrip("/")
POLYMARKET_CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "")

DATA_DIR = Path("bot/data")
LOG_DIR = Path("bot/logs")
POSITIONS_FILE = DATA_DIR / "positions.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"
SEEN_FILE = DATA_DIR / "seen_event_ids.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger("musashi-poly-bot")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2))


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(value) + "\n")


class MusashiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def health(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/api/health", timeout=20)
        response.raise_for_status()
        return response.json()

    def get_feed(self, limit: int = 20, min_urgency: str = "high") -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/api/feed",
            params={"limit": limit, "minUrgency": min_urgency},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", {}).get("tweets", [])

    def analyze_text(self, text: str, min_confidence: float = 0.5, max_results: int = 3) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/api/analyze-text",
            json={"text": text, "minConfidence": min_confidence, "maxResults": max_results},
            timeout=30,
        )
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

    def resolve_market(self, market: dict[str, Any]) -> dict[str, Any]:
        market_id = market.get("id")
        if market_id:
            try:
                return self.get_market(str(market_id))
            except Exception:
                logger.warning("Gamma lookup by id failed for %s, falling back to slug", market_id)

        slug = extract_slug(market.get("url", ""))
        if not slug:
            raise ValueError("Unable to resolve Polymarket slug from market URL")
        return self.get_market_by_slug(slug)


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


class PaperTrader:
    def place_market_buy(self, token_id: str, amount_usd: float, meta: dict[str, Any]) -> dict[str, Any]:
        logger.info(
            "[paper] buy token=%s amount=%.2f side=%s market=%s",
            token_id,
            amount_usd,
            meta["side"],
            meta["market_title"],
        )
        return {"mode": "paper", "status": "filled", "token_id": token_id, "amount_usd": amount_usd}


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
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

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


class Bot:
    def __init__(self) -> None:
        self.musashi = MusashiClient(BASE_URL)
        self.gamma = PolymarketGammaClient()
        self.positions = load_json(POSITIONS_FILE, {})
        self.seen_event_ids = set(load_json(SEEN_FILE, []))
        self.trader = LiveTrader() if BOT_MODE == "live" else PaperTrader()

    def current_exposure(self) -> float:
        return sum(float(position.get("size_usd", 0)) for position in self.positions.values())

    def bankroll_remaining(self) -> float:
        remaining = BANKROLL_USD - self.current_exposure()
        return max(0.0, round(remaining, 2))

    def should_trade(self, signal_payload: dict[str, Any]) -> Decision | None:
        if not signal_payload.get("success"):
            return None

        action = signal_payload.get("data", {}).get("suggested_action")
        matches = signal_payload.get("data", {}).get("markets", [])
        urgency = signal_payload.get("urgency")
        event_id = signal_payload.get("event_id")

        if not action or not matches or not event_id:
            return None
        if urgency not in {"high", "critical"}:
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
        save_json(SEEN_FILE, sorted(self.seen_event_ids))

    def already_holding(self, market_id: str, event_id: str) -> bool:
        if market_id in self.positions:
            return True
        if not LIMIT_ONE_POSITION_PER_EVENT:
            return False
        return any(position.get("event_id") == event_id for position in self.positions.values())

    def execute_trade(self, decision: Decision) -> None:
        market = decision.market
        market_id = str(market["id"])

        if self.already_holding(market_id, decision.event_id):
            logger.info("Skipped %s because position already exists for market/event", market_id)
            return

        size_usd = self.size_position(decision)
        if size_usd <= 0:
            logger.info("Skipped %s because bankroll/exposure cap is full", market_id)
            return

        gamma_market = self.gamma.resolve_market(market)
        token_id = pick_token_id(gamma_market, decision.side)
        response = self.trader.place_market_buy(
            token_id=token_id,
            amount_usd=size_usd,
            meta={
                "side": decision.side,
                "market_title": market["title"],
            },
        )

        position = {
            "event_id": decision.event_id,
            "market_id": market_id,
            "title": market["title"],
            "url": market.get("url"),
            "side": decision.side,
            "token_id": token_id,
            "size_usd": size_usd,
            "entry_probability": decision.probability,
            "confidence": decision.confidence,
            "edge": decision.edge,
            "score": decision.score,
            "opened_at": utc_now_iso(),
            "mode": BOT_MODE,
            "last_response": response,
        }
        self.positions[market_id] = position
        save_json(POSITIONS_FILE, self.positions)

        append_jsonl(
            TRADES_FILE,
            {
                "timestamp": utc_now_iso(),
                "type": f"{BOT_MODE}_entry",
                "position": position,
                "reason": decision.reason,
                "signal_type": decision.signal_type,
                "urgency": decision.urgency,
            },
        )

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
        self.record_seen(str(event_id))
        decision = self.should_trade(signal)
        if not decision:
            return
        self.execute_trade(decision)

    def run(self) -> None:
        health = self.musashi.health()
        logger.info("Musashi health: %s", health.get("status"))
        logger.info(
            "Bot mode=%s bankroll=%.2f max_position=%.2f exposure_cap=%.2f",
            BOT_MODE,
            BANKROLL_USD,
            MAX_POSITION_USD,
            MAX_TOTAL_EXPOSURE_USD,
        )

        while True:
            try:
                feed = self.musashi.get_feed(limit=20, min_urgency="high")
                logger.info("Fetched %d feed items", len(feed))
                for item in feed:
                    self.handle_feed_item(item)
            except Exception as exc:
                logger.exception("Loop error: %s", exc)
            time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    Bot().run()
