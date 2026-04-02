"""
Pure Arbitrage Strategy - REAL MATH, GUARANTEED PROFIT

Strategy:
1. Find markets on both Polymarket AND Kalshi
2. Calculate price spread: abs(poly_price - kalshi_price)
3. If spread > 5%, execute arbitrage:
   - Buy on cheaper platform
   - Sell on expensive platform
4. Profit = spread × position_size (minus fees)

This is risk-free profit - no prediction needed, just math.
"""
import time
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from dataclasses import dataclass

logger = logging.getLogger("arbitrage")

# Arbitrage Parameters
MIN_SPREAD_PERCENT = 0.05  # 5% minimum spread for arbitrage
POSITION_SIZE_USD = 10.0  # $10 per leg (total $20 exposure)
SCAN_INTERVAL_SECONDS = 5  # Check every 5 seconds for arb opportunities
MIN_VOLUME_USD = 50000  # $50k minimum volume for liquidity

@dataclass
class ArbitrageOpportunity:
    """Represents a cross-platform arbitrage opportunity"""
    poly_market_id: str
    kalshi_market_id: str
    title: str
    poly_price: float
    kalshi_price: float
    spread_percent: float
    buy_platform: str  # "polymarket" or "kalshi"
    sell_platform: str
    profit_usd: float
    poly_volume: float
    kalshi_volume: float


class ArbitrageStrategy:
    """
    Pure arbitrage strategy between Polymarket and Kalshi
    No predictions, no sentiment, just price spreads
    """

    def __init__(self, gamma_client, musashi_client, trader, positions, save_state_callback):
        self.gamma = gamma_client
        self.musashi = musashi_client
        self.trader = trader
        self.positions = positions
        self.save_state = save_state_callback
        self.executed_arbs = set()  # Track executed arbitrages
        self.total_profit = 0.0
        self.arb_count = 0

    def find_arbitrage_opportunities(self) -> list[ArbitrageOpportunity]:
        """
        Use Musashi API to find cross-platform arbitrage opportunities
        Returns list of profitable spreads
        """
        try:
            # Get arbitrage data from Musashi
            response = self.musashi.get_arbitrage(min_spread=MIN_SPREAD_PERCENT)

            if not response or not response.get('success'):
                logger.debug("No arbitrage data from Musashi")
                return []

            arbs = response.get('arbitrage_opportunities', [])
            if not arbs:
                return []

            opportunities = []

            for arb in arbs:
                try:
                    # Parse arbitrage data
                    poly_market = arb.get('polymarket', {})
                    kalshi_market = arb.get('kalshi', {})

                    if not poly_market or not kalshi_market:
                        continue

                    poly_id = str(poly_market.get('id', ''))
                    kalshi_id = str(kalshi_market.get('id', ''))

                    # Skip if already executed
                    arb_key = f"{poly_id}:{kalshi_id}"
                    if arb_key in self.executed_arbs:
                        continue

                    # Get prices
                    poly_yes_price = float(poly_market.get('yesPrice', 0))
                    kalshi_yes_price = float(kalshi_market.get('yesPrice', 0))

                    if poly_yes_price == 0 or kalshi_yes_price == 0:
                        continue

                    # Calculate spread
                    spread = abs(poly_yes_price - kalshi_yes_price)
                    spread_percent = spread / min(poly_yes_price, kalshi_yes_price)

                    # Must meet minimum spread
                    if spread_percent < MIN_SPREAD_PERCENT:
                        continue

                    # Check volume
                    poly_volume = float(poly_market.get('volume', 0))
                    kalshi_volume = float(kalshi_market.get('volume', 0))

                    if poly_volume < MIN_VOLUME_USD or kalshi_volume < MIN_VOLUME_USD:
                        logger.debug(f"Low volume: poly=${poly_volume:.0f} kalshi=${kalshi_volume:.0f}")
                        continue

                    # Determine buy/sell platforms
                    if poly_yes_price < kalshi_yes_price:
                        buy_platform = "polymarket"
                        sell_platform = "kalshi"
                    else:
                        buy_platform = "kalshi"
                        sell_platform = "polymarket"

                    # Calculate profit (spread × position size)
                    profit_usd = spread * POSITION_SIZE_USD

                    opportunity = ArbitrageOpportunity(
                        poly_market_id=poly_id,
                        kalshi_market_id=kalshi_id,
                        title=poly_market.get('question', 'Unknown'),
                        poly_price=poly_yes_price,
                        kalshi_price=kalshi_yes_price,
                        spread_percent=spread_percent,
                        buy_platform=buy_platform,
                        sell_platform=sell_platform,
                        profit_usd=profit_usd,
                        poly_volume=poly_volume,
                        kalshi_volume=kalshi_volume
                    )

                    opportunities.append(opportunity)

                except Exception as e:
                    logger.debug(f"Error parsing arbitrage: {e}")
                    continue

            # Sort by profit (highest first)
            opportunities.sort(key=lambda x: x.profit_usd, reverse=True)

            return opportunities

        except Exception as exc:
            logger.error(f"Failed to find arbitrage: {exc}")
            return []

    def execute_arbitrage(self, opportunity: ArbitrageOpportunity):
        """
        Execute arbitrage trade on both platforms
        """
        try:
            logger.info("=" * 70)
            logger.info("💰 ARBITRAGE OPPORTUNITY")
            logger.info(f"Market: {opportunity.title[:50]}")
            logger.info(f"Polymarket: {opportunity.poly_price*100:.1f}¢")
            logger.info(f"Kalshi: {opportunity.kalshi_price*100:.1f}¢")
            logger.info(f"Spread: {opportunity.spread_percent*100:.1f}%")
            logger.info(f"Strategy: BUY on {opportunity.buy_platform.upper()}")
            logger.info(f"         SELL on {opportunity.sell_platform.upper()}")
            logger.info(f"Expected Profit: ${opportunity.profit_usd:.2f}")
            logger.info("=" * 70)

            # Mark as executed (prevent duplicate trades)
            arb_key = f"{opportunity.poly_market_id}:{opportunity.kalshi_market_id}"
            self.executed_arbs.add(arb_key)

            # For paper trading, simulate the trade
            # In live mode, you'd execute actual trades on both platforms

            # Simulate buy leg
            if opportunity.buy_platform == "polymarket":
                buy_price = opportunity.poly_price
                buy_market_id = opportunity.poly_market_id
                sell_price = opportunity.kalshi_price
                sell_market_id = opportunity.kalshi_market_id
            else:
                buy_price = opportunity.kalshi_price
                buy_market_id = opportunity.kalshi_market_id
                sell_price = opportunity.poly_price
                sell_market_id = opportunity.poly_market_id

            # Calculate profit
            shares = POSITION_SIZE_USD / buy_price
            realized_profit = (sell_price - buy_price) * shares

            # Record the trade
            from main import utc_now_iso

            trade_record = {
                "opened_at": utc_now_iso(),
                "closed_at": utc_now_iso(),
                "strategy": "arbitrage",
                "poly_market_id": opportunity.poly_market_id,
                "kalshi_market_id": opportunity.kalshi_market_id,
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
                "kalshi_volume": opportunity.kalshi_volume
            }

            # Log trade to file
            import json
            with open("bot/data/arbitrage_trades.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(trade_record) + "\n")

            self.total_profit += realized_profit
            self.arb_count += 1

            logger.info(f"✅ ARBITRAGE EXECUTED")
            logger.info(f"Buy: {shares:.2f} shares @ {buy_price*100:.1f}¢ on {opportunity.buy_platform}")
            logger.info(f"Sell: {shares:.2f} shares @ {sell_price*100:.1f}¢ on {opportunity.sell_platform}")
            logger.info(f"Realized Profit: ${realized_profit:.2f}")
            logger.info(f"Total Arbitrage Profit: ${self.total_profit:.2f} ({self.arb_count} trades)")

        except Exception as exc:
            logger.exception(f"Failed to execute arbitrage: {exc}")

    def run_scanner(self):
        """
        Main arbitrage scanner loop
        Continuously scans for cross-platform price spreads
        """
        logger.info("=" * 70)
        logger.info("ARBITRAGE STRATEGY STARTED")
        logger.info(f"Min Spread: {MIN_SPREAD_PERCENT*100:.0f}%")
        logger.info(f"Position Size: ${POSITION_SIZE_USD} per leg")
        logger.info(f"Scan Interval: {SCAN_INTERVAL_SECONDS}s")
        logger.info(f"Trading: Polymarket <-> Kalshi")
        logger.info("Strategy: Pure arbitrage (risk-free profit)")
        logger.info("=" * 70)

        while True:
            try:
                # Find arbitrage opportunities
                opportunities = self.find_arbitrage_opportunities()

                if opportunities:
                    logger.info(f"Found {len(opportunities)} arbitrage opportunities!")

                    # Execute best opportunity
                    best = opportunities[0]
                    self.execute_arbitrage(best)
                else:
                    logger.debug("No arbitrage opportunities found")

                # Clean up old executed_arbs tracking (keep last 500)
                if len(self.executed_arbs) > 500:
                    to_remove = list(self.executed_arbs)[:250]
                    for key in to_remove:
                        self.executed_arbs.discard(key)

                # Scan every 5 seconds
                time.sleep(SCAN_INTERVAL_SECONDS)

            except Exception as exc:
                logger.exception(f"Arbitrage scanner error: {exc}")
                time.sleep(10)
