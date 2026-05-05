"""
Cross-platform arbitrage scanner (Polymarket <-> Kalshi) — SIMULATION ONLY.

Finds markets where the YES price differs between platforms, logs the
opportunity, and records simulated trade results. No live Kalshi order
placement is implemented; all "executed" trades are paper entries only.
"""
import json
import logging
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

try:
    from utils import utc_now_iso                              # script: python bot/arbitrage_strategy.py
except ImportError:
    from bot.utils import utc_now_iso                          # package: import bot.arbitrage_strategy

logger = logging.getLogger("arbitrage")

# Arbitrage parameters
MIN_SPREAD_PERCENT = 0.05   # 5% minimum spread to log an opportunity
POSITION_SIZE_USD = 10.0    # $10 per leg (total $20 simulated exposure)
SCAN_INTERVAL_SECONDS = 5   # seconds between API polls
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


class ArbitrageStrategy:
    """
    Cross-platform arbitrage scanner — simulation only.

    Polls the Musashi API for price spreads between Polymarket and Kalshi,
    logs opportunities, and records simulated paper trades. No real orders
    are placed on Kalshi.
    """

    def __init__(self, gamma_client, musashi_client, trader, positions, save_state_callback):
        self.gamma = gamma_client
        self.musashi = musashi_client
        self.trader = trader
        self.positions = positions
        self.save_state = save_state_callback
        self.executed_arbs: set[str] = set()
        self.total_profit = 0.0
        self.arb_count = 0

    def find_arbitrage_opportunities(self) -> list[ArbitrageOpportunity]:
        """Poll Musashi API and return filtered ArbitrageOpportunity list."""
        try:
            response = self.musashi.get_arbitrage(min_spread=MIN_SPREAD_PERCENT)

            if not response or not response.get("success"):
                logger.debug("No arbitrage data from Musashi")
                return []

            raw_opps = response.get("data", {}).get("opportunities", [])
            if not raw_opps:
                return []

            opportunities: list[ArbitrageOpportunity] = []
            for arb in raw_opps:
                try:
                    opp = _parse_opportunity(arb)
                    if opp is None:
                        continue

                    arb_key = f"{opp.poly_market_id}:{opp.kalshi_market_id}"
                    if arb_key in self.executed_arbs:
                        continue

                    opportunities.append(opp)
                except Exception as exc:
                    logger.debug("Error parsing arbitrage entry: %s", exc)

            opportunities.sort(key=lambda x: x.profit_usd, reverse=True)
            return opportunities

        except Exception as exc:
            logger.error("Failed to find arbitrage: %s", exc)
            return []

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

            arb_key = f"{opportunity.poly_market_id}:{opportunity.kalshi_market_id}"
            self.executed_arbs.add(arb_key)

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
        logger.info("Scan interval:  %ds", SCAN_INTERVAL_SECONDS)
        logger.info("Platforms:      Polymarket <-> Kalshi")
        logger.info("NOTE: No real orders are placed. All trades are paper-only.")
        logger.info("=" * 70)

        while True:
            try:
                opportunities = self.find_arbitrage_opportunities()
                if opportunities:
                    logger.info("Found %d opportunity/ies", len(opportunities))
                    self.execute_arbitrage(opportunities[0])
                else:
                    logger.debug("No arbitrage opportunities found this scan")

                # Cap dedup set to avoid unbounded growth
                if len(self.executed_arbs) > 500:
                    for key in list(self.executed_arbs)[:250]:
                        self.executed_arbs.discard(key)

                time.sleep(SCAN_INTERVAL_SECONDS)

            except Exception as exc:
                logger.exception("Arbitrage scanner error: %s", exc)
                time.sleep(10)
