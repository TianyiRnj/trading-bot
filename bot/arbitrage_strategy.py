"""
Cross-platform arbitrage scanner (Polymarket <-> Kalshi) — SIMULATION ONLY.

Finds markets where the YES price differs between platforms, logs the
opportunity, and records simulated trade results. No live Kalshi order
placement is implemented; all "executed" trades are paper entries only.
"""
import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass

try:
    from utils import utc_now_iso                              # script: python bot/arbitrage_strategy.py
except ImportError:
    from bot.utils import utc_now_iso                          # package: import bot.arbitrage_strategy

logger = logging.getLogger("arbitrage")

# Arbitrage parameters
MIN_SPREAD_PERCENT = 0.05   # 5% minimum spread to log an opportunity
POSITION_SIZE_USD = 10.0    # $10 per leg (total $20 simulated exposure)
MIN_VOLUME_USD = 500        # $500 minimum 24h volume — Kalshi political markets often report $0 24h volume

_ARB_TRADES_FILE = Path(__file__).parent / "data" / "arbitrage_trades.jsonl"


@dataclass
class ArbitrageOpportunity:
    """Represents a cross-platform arbitrage opportunity."""
    poly_market_id: str
    kalshi_market_id: str
    title: str
    poly_price: float
    kalshi_price: float
    spread_percent: float
    buy_platform: str   # "polymarket" or "kalshi"
    sell_platform: str
    profit_usd: float
    poly_volume: float
    kalshi_volume: float


def _parse_opportunity(
    arb: dict,
    min_spread: float = MIN_SPREAD_PERCENT,
    min_volume: float = MIN_VOLUME_USD,
) -> Optional[ArbitrageOpportunity]:
    """Parse one entry from ``data.opportunities`` into an ArbitrageOpportunity.

    Returns None if required fields are absent, prices are zero, or the
    spread / volume thresholds are not met.
    """
    poly_market = arb.get("polymarket", {})
    kalshi_market = arb.get("kalshi", {})
    if not poly_market or not kalshi_market:
        return None

    poly_id = str(poly_market.get("id", ""))
    kalshi_id = str(kalshi_market.get("id", ""))
    poly_price = float(poly_market.get("yesPrice", 0))
    kalshi_price = float(kalshi_market.get("yesPrice", 0))

    if not poly_id or not kalshi_id or poly_price == 0 or kalshi_price == 0:
        return None

    spread = abs(poly_price - kalshi_price)
    spread_pct = spread / min(poly_price, kalshi_price)

    if spread_pct < min_spread:
        return None

    poly_volume = float(poly_market.get("volume24h", 0))
    kalshi_volume = float(kalshi_market.get("volume24h", 0))
    if poly_volume < min_volume or kalshi_volume < min_volume:
        return None

    buy_platform = "polymarket" if poly_price < kalshi_price else "kalshi"
    sell_platform = "kalshi" if buy_platform == "polymarket" else "polymarket"

    return ArbitrageOpportunity(
        poly_market_id=poly_id,
        kalshi_market_id=kalshi_id,
        title=poly_market.get("title", kalshi_market.get("title", "Unknown")),
        poly_price=poly_price,
        kalshi_price=kalshi_price,
        spread_percent=spread_pct,
        buy_platform=buy_platform,
        sell_platform=sell_platform,
        profit_usd=spread * POSITION_SIZE_USD,
        poly_volume=poly_volume,
        kalshi_volume=kalshi_volume,
    )


def _select_primary_market(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("_select_primary_market requires at least one row")

    def sort_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
        liquidity = float(row.get("liquidity") or -1)
        open_interest = float(row.get("open_interest") or -1)
        volume_24h = float(row.get("volume_24h") or 0)
        platform_id = str(row.get("platform_id") or row.get("id") or "")
        return (liquidity, open_interest, volume_24h, platform_id)

    return sorted(rows, key=sort_key, reverse=True)[0]


def _derive_opportunities_from_market_rows(
    rows: list[dict[str, Any]],
    min_spread: float = MIN_SPREAD_PERCENT,
    min_volume: float = MIN_VOLUME_USD,
) -> list[ArbitrageOpportunity]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for row in rows:
        event_id = str(row.get("event_id") or "").strip()
        series_id = str(row.get("series_id") or "").strip()
        cluster_id = event_id or (f"series:{series_id}" if series_id else "")
        platform = str(row.get("platform") or "").strip().lower()
        if not cluster_id or platform not in {"polymarket", "kalshi"}:
            continue

        bucket = grouped.setdefault(cluster_id, {"polymarket": [], "kalshi": []})
        bucket[platform].append(row)

    opportunities: list[ArbitrageOpportunity] = []
    for bucket in grouped.values():
        if not bucket["polymarket"] or not bucket["kalshi"]:
            continue

        poly = _select_primary_market(bucket["polymarket"])
        kalshi = _select_primary_market(bucket["kalshi"])
        candidate = {
            "polymarket": {
                "id": poly.get("platform_id") or poly.get("id"),
                "title": poly.get("title"),
                "yesPrice": float(poly.get("yes_price") or 0),
                "volume24h": float(poly.get("volume_24h") or 0),
            },
            "kalshi": {
                "id": kalshi.get("platform_id") or kalshi.get("id"),
                "title": kalshi.get("title"),
                "yesPrice": float(kalshi.get("yes_price") or 0),
                "volume24h": float(kalshi.get("volume_24h") or 0),
            },
        }
        opportunity = _parse_opportunity(candidate, min_spread=min_spread, min_volume=min_volume)
        if opportunity is not None:
            opportunities.append(opportunity)

    opportunities.sort(key=lambda item: item.profit_usd, reverse=True)
    return opportunities


def _arb_key(opportunity: ArbitrageOpportunity) -> str:
    return f"{opportunity.poly_market_id}:{opportunity.kalshi_market_id}"


class ArbitrageStrategy:
    """
    Cross-platform arbitrage scanner — simulation only.

    Polls the Musashi API for price spreads between Polymarket and Kalshi,
    logs opportunities, and records simulated paper trades. No real orders
    are placed on Kalshi.
    """

    def __init__(
        self,
        gamma_client,
        musashi_client,
        trader,
        positions,
        save_state_callback,
        market_intelligence=None,
        scan_interval: int = 30,
    ):
        self.gamma = gamma_client
        self.musashi = musashi_client
        self.trader = trader
        self.positions = positions
        self.save_state = save_state_callback
        self.market_intelligence = market_intelligence
        self.scan_interval = max(1, int(scan_interval))
        self.executed_arbs: OrderedDict[str, None] = OrderedDict()
        self._stop_event = threading.Event()
        self.total_profit = 0.0
        self.arb_count = 0

    def stop(self) -> None:
        """Signal the scanner loop to exit on the next iteration."""
        self._stop_event.set()

    def _mark_executed(self, key: str) -> None:
        """Record key as executed and evict oldest entries if the dedup set is too large."""
        self.executed_arbs[key] = None
        if len(self.executed_arbs) > 500:
            for _ in range(250):
                self.executed_arbs.popitem(last=False)

    def _fallback_opportunities(self) -> list[ArbitrageOpportunity]:
        if self.market_intelligence is None:
            return []

        rows = self.market_intelligence.list_cross_platform_markets(min_volume=MIN_VOLUME_USD)
        if not rows:
            return []

        opportunities = _derive_opportunities_from_market_rows(
            rows,
            min_spread=MIN_SPREAD_PERCENT,
            min_volume=MIN_VOLUME_USD,
        )
        opportunities = [
            opportunity
            for opportunity in opportunities
            if _arb_key(opportunity) not in self.executed_arbs
        ]
        if opportunities:
            logger.info(
                "Using musashi-infra Supabase fallback for arbitrage discovery (%d opportunity/ies)",
                len(opportunities),
            )
        return opportunities

    def find_arbitrage_opportunities(self) -> list[ArbitrageOpportunity]:
        """Poll Musashi API and return filtered ArbitrageOpportunity list."""
        try:
            response = self.musashi.get_arbitrage(min_spread=MIN_SPREAD_PERCENT)

            if not response or not response.get("success"):
                logger.debug("No arbitrage data from Musashi")
                return self._fallback_opportunities()

            raw_opps = response.get("data", {}).get("opportunities", [])
            if not raw_opps:
                return self._fallback_opportunities()

            opportunities: list[ArbitrageOpportunity] = []
            for arb in raw_opps:
                try:
                    opp = _parse_opportunity(arb)
                    if opp is None:
                        continue

                    if _arb_key(opp) in self.executed_arbs:
                        continue

                    opportunities.append(opp)
                except Exception as exc:
                    logger.debug("Error parsing arbitrage entry: %s", exc)

            opportunities.sort(key=lambda x: x.profit_usd, reverse=True)
            return opportunities or self._fallback_opportunities()

        except Exception as exc:
            logger.error("Failed to find arbitrage: %s", exc)
            return self._fallback_opportunities()

    def execute_arbitrage(self, opportunity: ArbitrageOpportunity) -> None:
        """Log a simulation-only arbitrage trade and record it to disk."""
        try:
            logger.info("=" * 70)
            logger.info("ARBITRAGE OPPORTUNITY (simulation)")
            logger.info("Market: %s", opportunity.title[:60])
            logger.info("Polymarket: %.1f¢", opportunity.poly_price * 100)
            logger.info("Kalshi:     %.1f¢", opportunity.kalshi_price * 100)
            logger.info("Spread:     %.1f%%", opportunity.spread_percent * 100)
            logger.info("BUY  on %s", opportunity.buy_platform.upper())
            logger.info("SELL on %s", opportunity.sell_platform.upper())
            logger.info("Simulated profit: $%.2f", opportunity.profit_usd)
            logger.info("=" * 70)

            self._mark_executed(_arb_key(opportunity))

            buy_price = (
                opportunity.poly_price
                if opportunity.buy_platform == "polymarket"
                else opportunity.kalshi_price
            )
            sell_price = (
                opportunity.kalshi_price
                if opportunity.buy_platform == "polymarket"
                else opportunity.poly_price
            )
            buy_market_id = (
                opportunity.poly_market_id
                if opportunity.buy_platform == "polymarket"
                else opportunity.kalshi_market_id
            )
            sell_market_id = (
                opportunity.kalshi_market_id
                if opportunity.buy_platform == "polymarket"
                else opportunity.poly_market_id
            )

            shares = POSITION_SIZE_USD / buy_price
            realized_profit = (sell_price - buy_price) * shares

            trade_record = {
                "opened_at": utc_now_iso(),
                "closed_at": utc_now_iso(),
                "strategy": "arbitrage_simulation",
                "poly_market_id": opportunity.poly_market_id,
                "kalshi_market_id": opportunity.kalshi_market_id,
                "buy_market_id": buy_market_id,
                "sell_market_id": sell_market_id,
                "title": opportunity.title,
                "buy_platform": opportunity.buy_platform,
                "sell_platform": opportunity.sell_platform,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "spread_percent": opportunity.spread_percent,
                "position_size_usd": POSITION_SIZE_USD,
                "shares": shares,
                "realized_pnl": realized_profit,
                "poly_volume": opportunity.poly_volume,
                "kalshi_volume": opportunity.kalshi_volume,
            }

            _ARB_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _ARB_TRADES_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(trade_record) + "\n")

            self.total_profit += realized_profit
            self.arb_count += 1

            logger.info(
                "SIMULATED: buy %.2f shares @ %.1f¢ on %s, sell @ %.1f¢ on %s",
                shares,
                buy_price * 100,
                opportunity.buy_platform,
                sell_price * 100,
                opportunity.sell_platform,
            )
            logger.info(
                "Simulated profit: $%.2f | Session total: $%.2f (%d trades)",
                realized_profit,
                self.total_profit,
                self.arb_count,
            )

        except Exception as exc:
            logger.exception("Failed to execute arbitrage: %s", exc)

    def run_scanner(self) -> None:
        """Continuous loop: scan for opportunities, log and record the best one."""
        logger.info("=" * 70)
        logger.info("ARBITRAGE SCANNER STARTED (simulation only)")
        logger.info("Min spread:     %.0f%%", MIN_SPREAD_PERCENT * 100)
        logger.info("Position size:  $%.0f per leg", POSITION_SIZE_USD)
        logger.info("Scan interval:  %ds", self.scan_interval)
        logger.info("Platforms:      Polymarket <-> Kalshi")
        logger.info("NOTE: No real orders are placed. All trades are paper-only.")
        logger.info("=" * 70)

        while not self._stop_event.is_set():
            try:
                opportunities = self.find_arbitrage_opportunities()
                if opportunities:
                    logger.info("Found %d opportunity/ies", len(opportunities))
                    self.execute_arbitrage(opportunities[0])
                else:
                    logger.debug("No arbitrage opportunities found this scan")

                self._stop_event.wait(self.scan_interval)

            except Exception as exc:
                logger.exception("Arbitrage scanner error: %s", exc)
                self._stop_event.wait(10)
