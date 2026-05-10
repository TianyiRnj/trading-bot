import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger("musashi-infra")

HIGH_LIQUIDITY_THRESHOLD = 10_000
MEDIUM_LIQUIDITY_THRESHOLD = 1_000
HIGH_VOLUME_THRESHOLD = 1_000
MEDIUM_VOLUME_THRESHOLD = 100
LOOKBACK_TOLERANCE_RATIO = 0.5
SOURCE_HEALTH_CACHE_SECONDS = 60
MARKET_CONTEXT_CACHE_SECONDS = 300

MARKET_SELECT = (
    "id,platform,platform_id,event_id,series_id,title,yes_price,no_price,"
    "volume_24h,open_interest,liquidity,status,last_snapshot_at,"
    "last_ingested_at,is_active,source_missing_at"
)
RELATED_MARKET_SELECT = (
    "platform,platform_id,title,yes_price,volume_24h,open_interest,"
    "liquidity,status,closes_at,settles_at"
)
SNAPSHOT_SELECT = "snapshot_time,yes_price"


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _minutes_since(value: str | None) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 60.0, 2)


def _freshness_age_minutes(*values: str | None) -> float | None:
    ages = [age for age in (_minutes_since(value) for value in values) if age is not None]
    if not ages:
        return None
    return min(ages)


def _format_in(values: list[str]) -> str:
    unique_values = [value for value in dict.fromkeys(values) if value]
    return f"in.({','.join(unique_values)})"


def compute_confidence_label(
    liquidity: float | None,
    volume24h: float | None,
    open_interest: float | None,
) -> str:
    liq = liquidity or 0.0
    vol = volume24h or 0.0
    oi = open_interest or 0.0

    if (liq >= HIGH_LIQUIDITY_THRESHOLD or oi >= 5_000) and vol >= HIGH_VOLUME_THRESHOLD:
        return "high"
    if liq >= MEDIUM_LIQUIDITY_THRESHOLD or oi >= MEDIUM_LIQUIDITY_THRESHOLD or vol >= MEDIUM_VOLUME_THRESHOLD:
        return "medium"
    return "low"


def compute_probability_change_24h(current_yes_price: float, snapshots: list[dict[str, Any]]) -> float | None:
    parsed_snapshots: list[tuple[datetime, dict[str, Any]]] = []
    for snapshot in snapshots:
        snapshot_time = _parse_iso(str(snapshot.get("snapshot_time") or ""))
        if snapshot_time is None:
            continue
        parsed_snapshots.append((snapshot_time, snapshot))

    if len(parsed_snapshots) < 2:
        return None

    latest_time = max(snapshot_time for snapshot_time, _ in parsed_snapshots)
    target_time = latest_time - timedelta(hours=24)
    tolerance = timedelta(hours=24 * LOOKBACK_TOLERANCE_RATIO)

    best_snapshot: dict[str, Any] | None = None
    best_diff: timedelta | None = None

    for snapshot_time, snapshot in parsed_snapshots:
        diff = abs(snapshot_time - target_time)
        if best_diff is None or diff < best_diff:
            best_snapshot = snapshot
            best_diff = diff

    if best_snapshot is None or best_diff > tolerance:
        return None

    reference_price = _as_float(best_snapshot.get("yes_price"), None)
    if reference_price is None:
        return None
    return round(float(current_yes_price) - reference_price, 6)


def score_market_context(
    context: dict[str, Any] | None,
    decision_side: str,
    max_snapshot_age_minutes: int,
) -> tuple[float, list[str]]:
    if not context:
        return 1.0, []

    multiplier = 1.0
    reasons: list[str] = []

    if context.get("status") != "open" or not context.get("is_active", True):
        multiplier -= 0.20
        reasons.append("infra_status_not_open")

    if context.get("source_missing_at"):
        multiplier -= 0.15
        reasons.append("infra_source_missing")

    source_health = context.get("source_health") or {}
    if source_health and source_health.get("is_available") is False:
        multiplier -= 0.10
        reasons.append("infra_source_unavailable")

    snapshot_age = _as_float(context.get("snapshot_age_minutes"), None)
    if snapshot_age is None:
        multiplier -= 0.05
        reasons.append("infra_snapshot_age_unknown")
    elif snapshot_age <= max_snapshot_age_minutes:
        multiplier += 0.05
        reasons.append("infra_snapshot_fresh")
    else:
        multiplier -= 0.10
        reasons.append("infra_snapshot_stale")

    confidence_label = str(context.get("confidence_label") or "low")
    if confidence_label == "high":
        multiplier += 0.10
        reasons.append("infra_high_liquidity")
    elif confidence_label == "medium":
        multiplier += 0.05
        reasons.append("infra_medium_liquidity")

    probability_change_24h = _as_float(context.get("probability_change_24h"), None)
    if probability_change_24h is not None:
        if decision_side == "YES":
            if probability_change_24h >= 0.02:
                multiplier += 0.05
                reasons.append("infra_yes_momentum_up")
            elif probability_change_24h <= -0.02:
                multiplier -= 0.05
                reasons.append("infra_yes_momentum_down")
        else:
            if probability_change_24h <= -0.02:
                multiplier += 0.05
                reasons.append("infra_no_momentum_up")
            elif probability_change_24h >= 0.02:
                multiplier -= 0.05
                reasons.append("infra_no_momentum_down")

    related_market_count = int(context.get("related_market_count") or 0)
    if related_market_count > 0:
        multiplier += min(0.05, related_market_count * 0.01)
        reasons.append("infra_related_markets_present")

    bounded = round(max(0.55, min(multiplier, 1.35)), 3)
    return bounded, reasons


class MusashiInfraClient:
    def __init__(
        self,
        *,
        supabase_url: str,
        api_key: str,
        timeout: float,
        enable_market_intelligence: bool,
        enable_arbitrage_fallback: bool,
        max_snapshot_age_minutes: int,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_snapshot_age_minutes = max_snapshot_age_minutes
        self._enable_market_intelligence = enable_market_intelligence
        self._enable_arbitrage_fallback = enable_arbitrage_fallback
        self.rest_url = f"{self.supabase_url}/rest/v1" if self.supabase_url else ""
        self.session = requests.Session()
        if self.supabase_url and self.api_key:
            self.session.headers.update(
                {
                    "apikey": self.api_key,
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                }
            )

        self._source_health_cache: dict[str, dict[str, Any]] | None = None
        self._source_health_cached_at = 0.0
        self._market_context_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def is_configured(self) -> bool:
        return bool(self.supabase_url and self.api_key)

    def market_intelligence_enabled(self) -> bool:
        return self.is_configured() and self._enable_market_intelligence

    def arbitrage_fallback_enabled(self) -> bool:
        return self.is_configured() and self._enable_arbitrage_fallback

    def get_polymarket_contexts(self, candidate_markets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not self.market_intelligence_enabled():
            return {}

        platform_ids = [
            str(market.get("id"))
            for market in candidate_markets
            if market.get("id") not in (None, "")
        ]
        if not platform_ids:
            return {}

        contexts: dict[str, dict[str, Any]] = {}
        uncached_ids: list[str] = []
        now = time.time()

        for platform_id in platform_ids:
            cached = self._market_context_cache.get(platform_id)
            if cached and now - cached[0] < MARKET_CONTEXT_CACHE_SECONDS:
                contexts[platform_id] = cached[1]
            else:
                uncached_ids.append(platform_id)

        if not uncached_ids:
            return contexts

        try:
            rows = self._select(
                "markets",
                {
                    "select": MARKET_SELECT,
                    "platform": "eq.polymarket",
                    "platform_id": _format_in(uncached_ids),
                },
            )
            source_health = self.get_source_health()
            for row in rows:
                context = self._build_market_context(row, source_health.get("polymarket"))
                platform_id = str(row.get("platform_id") or "")
                if not platform_id:
                    continue
                contexts[platform_id] = context
                self._market_context_cache[platform_id] = (now, context)
        except Exception as exc:
            logger.warning("Failed to load musashi-infra market contexts: %s", exc)

        return contexts

    def list_cross_platform_markets(self, *, min_volume: float, limit: int = 400) -> list[dict[str, Any]]:
        if not self.arbitrage_fallback_enabled():
            return []

        try:
            rows = self._select(
                "markets",
                {
                    "select": (
                        "id,platform,platform_id,event_id,series_id,title,yes_price,"
                        "volume_24h,open_interest,liquidity,status,is_active,"
                        "last_snapshot_at,last_ingested_at,source_missing_at,closes_at"
                    ),
                    "platform": "in.(polymarket,kalshi)",
                    "status": "eq.open",
                    "is_active": "eq.true",
                    "source_missing_at": "is.null",
                    "volume_24h": f"gte.{float(min_volume)}",
                    "limit": str(min(limit * 3, 500)),
                },
            )
        except Exception as exc:
            logger.warning("Failed to load musashi-infra arbitrage rows: %s", exc)
            return []

        fresh_rows: list[dict[str, Any]] = []
        for row in rows:
            age_minutes = _freshness_age_minutes(
                row.get("last_snapshot_at"),
                row.get("last_ingested_at"),
            )
            if age_minutes is None or age_minutes > self.max_snapshot_age_minutes:
                continue
            fresh_rows.append(row)
        fresh_rows.sort(
            key=lambda row: (
                _as_float(row.get("volume_24h"), 0.0) or 0.0,
                _as_float(row.get("liquidity"), 0.0) or 0.0,
                str(row.get("platform_id") or row.get("id") or ""),
            ),
            reverse=True,
        )
        return fresh_rows[:limit]

    def get_source_health(self) -> dict[str, dict[str, Any]]:
        if not self.is_configured():
            return {}
        now = time.time()
        if (
            self._source_health_cache is not None
            and now - self._source_health_cached_at < SOURCE_HEALTH_CACHE_SECONDS
        ):
            return self._source_health_cache

        rows = self._select(
            "source_health",
            {
                "select": "source,is_available,last_successful_fetch,last_error,last_error_at,updated_at",
            },
        )
        self._source_health_cache = {
            str(row.get("source")): dict(row)
            for row in rows
            if row.get("source")
        }
        self._source_health_cached_at = now
        return self._source_health_cache

    def _select(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        if not self.is_configured():
            return []
        response = self.session.get(
            f"{self.rest_url}/{table}",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        return []

    def _build_market_context(
        self,
        row: dict[str, Any],
        source_health: dict[str, Any] | None,
    ) -> dict[str, Any]:
        canonical_market_id = str(row.get("id") or "")
        snapshots = self._fetch_recent_snapshots(canonical_market_id)
        related_markets = self._fetch_related_markets(row)
        current_yes_price = _as_float(row.get("yes_price"), 0.0) or 0.0
        confidence_label = compute_confidence_label(
            _as_float(row.get("liquidity"), None),
            _as_float(row.get("volume_24h"), 0.0),
            _as_float(row.get("open_interest"), None),
        )

        return {
            "canonical_market_id": canonical_market_id,
            "platform_id": str(row.get("platform_id") or ""),
            "event_id": row.get("event_id"),
            "series_id": row.get("series_id"),
            "title": row.get("title"),
            "yes_price": current_yes_price,
            "no_price": _as_float(row.get("no_price"), 0.0),
            "volume_24h": _as_float(row.get("volume_24h"), 0.0),
            "open_interest": _as_float(row.get("open_interest"), None),
            "liquidity": _as_float(row.get("liquidity"), None),
            "status": row.get("status"),
            "is_active": bool(row.get("is_active", False)),
            "last_snapshot_at": row.get("last_snapshot_at"),
            "last_ingested_at": row.get("last_ingested_at"),
            "snapshot_age_minutes": _freshness_age_minutes(
                row.get("last_snapshot_at"),
                row.get("last_ingested_at"),
            ),
            "source_missing_at": row.get("source_missing_at"),
            "confidence_label": confidence_label,
            "probability_change_24h": compute_probability_change_24h(current_yes_price, snapshots),
            "related_market_count": len(related_markets),
            "related_markets": related_markets[:5],
            "source_health": source_health,
        }

    def _fetch_recent_snapshots(self, canonical_market_id: str) -> list[dict[str, Any]]:
        if not canonical_market_id:
            return []
        since = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        return self._select(
            "market_snapshots",
            {
                "select": SNAPSHOT_SELECT,
                "market_id": f"eq.{canonical_market_id}",
                "snapshot_time": f"gte.{since}",
                "order": "snapshot_time.asc",
                "limit": "48",
            },
        )

    def _fetch_related_markets(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        canonical_market_id = str(row.get("id") or "")
        event_id = str(row.get("event_id") or "").strip()
        series_id = str(row.get("series_id") or "").strip()
        if event_id:
            filter_key = "event_id"
            filter_value = event_id
        elif series_id:
            filter_key = "series_id"
            filter_value = series_id
        else:
            return []

        return self._select(
            "markets",
            {
                "select": RELATED_MARKET_SELECT,
                filter_key: f"eq.{filter_value}",
                "id": f"neq.{canonical_market_id}",
                "is_active": "eq.true",
                "order": "liquidity.desc.nullslast,volume_24h.desc.nullslast",
                "limit": "5",
            },
        )
