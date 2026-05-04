"""
Tests for the arbitrage response parser (_parse_opportunity).

Runs with plain unittest — no extra dependencies needed:
    python -m unittest tests.test_arbitrage -v
"""
import sys
import os
import unittest

# Make the bot package importable from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bot'))

from arbitrage_strategy import (
    _parse_opportunity,
    ArbitrageOpportunity,
    MIN_SPREAD_PERCENT,
    MIN_VOLUME_USD,
    POSITION_SIZE_USD,
)


def _make_arb(
    poly_price=0.63,
    kalshi_price=0.70,
    poly_vol=450_000,
    kalshi_vol=200_000,
    poly_id="poly-1",
    kalshi_id="kalshi-1",
    poly_title="Will Bitcoin reach $100k by June 2026?",
    kalshi_title="Bitcoin $100k by June 2026",
):
    """Build a minimal well-formed opportunity dict matching the documented API shape."""
    return {
        "polymarket": {
            "id": poly_id,
            "title": poly_title,
            "yesPrice": poly_price,
            "volume24h": poly_vol,
        },
        "kalshi": {
            "id": kalshi_id,
            "title": kalshi_title,
            "yesPrice": kalshi_price,
            "volume24h": kalshi_vol,
        },
        "spread": abs(poly_price - kalshi_price),
        "direction": "buy_poly_sell_kalshi",
        "confidence": 0.85,
    }


class TestParseOpportunity(unittest.TestCase):

    def test_documented_api_shape_parses_correctly(self):
        opp = _parse_opportunity(_make_arb())
        self.assertIsNotNone(opp)
        self.assertIsInstance(opp, ArbitrageOpportunity)
        self.assertEqual(opp.poly_market_id, "poly-1")
        self.assertEqual(opp.kalshi_market_id, "kalshi-1")
        self.assertEqual(opp.title, "Will Bitcoin reach $100k by June 2026?")
        self.assertAlmostEqual(opp.poly_price, 0.63)
        self.assertAlmostEqual(opp.kalshi_price, 0.70)
        self.assertEqual(opp.buy_platform, "polymarket")
        self.assertEqual(opp.sell_platform, "kalshi")

    def test_direction_buy_kalshi_when_kalshi_cheaper(self):
        opp = _parse_opportunity(_make_arb(poly_price=0.70, kalshi_price=0.63))
        self.assertIsNotNone(opp)
        self.assertEqual(opp.buy_platform, "kalshi")
        self.assertEqual(opp.sell_platform, "polymarket")

    def test_profit_calculation(self):
        opp = _parse_opportunity(_make_arb(poly_price=0.63, kalshi_price=0.70))
        self.assertIsNotNone(opp)
        expected_spread = abs(0.63 - 0.70)
        self.assertAlmostEqual(opp.profit_usd, expected_spread * POSITION_SIZE_USD, places=6)

    def test_title_falls_back_to_kalshi_when_poly_missing(self):
        arb = _make_arb()
        del arb["polymarket"]["title"]
        opp = _parse_opportunity(arb)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.title, "Bitcoin $100k by June 2026")

    def test_missing_polymarket_returns_none(self):
        arb = {"polymarket": {}, "kalshi": {"id": "k", "yesPrice": 0.7, "volume24h": 100_000}}
        self.assertIsNone(_parse_opportunity(arb))

    def test_missing_kalshi_returns_none(self):
        arb = {"polymarket": {"id": "p", "yesPrice": 0.6, "volume24h": 100_000}, "kalshi": {}}
        self.assertIsNone(_parse_opportunity(arb))

    def test_zero_poly_price_returns_none(self):
        self.assertIsNone(_parse_opportunity(_make_arb(poly_price=0)))

    def test_zero_kalshi_price_returns_none(self):
        self.assertIsNone(_parse_opportunity(_make_arb(kalshi_price=0)))

    def test_spread_below_threshold_returns_none(self):
        # 0.01 spread on ~0.65 base ≈ 1.5% — well below MIN_SPREAD_PERCENT (5%)
        self.assertIsNone(_parse_opportunity(_make_arb(poly_price=0.65, kalshi_price=0.66)))

    def test_low_poly_volume_returns_none(self):
        self.assertIsNone(_parse_opportunity(_make_arb(poly_vol=MIN_VOLUME_USD - 1)))

    def test_low_kalshi_volume_returns_none(self):
        self.assertIsNone(_parse_opportunity(_make_arb(kalshi_vol=MIN_VOLUME_USD - 1)))

    def test_exactly_at_volume_threshold_passes(self):
        opp = _parse_opportunity(_make_arb(poly_vol=MIN_VOLUME_USD, kalshi_vol=MIN_VOLUME_USD))
        self.assertIsNotNone(opp)

    def test_custom_min_spread_override(self):
        # Low spread that fails default but passes when threshold is lowered
        low_spread = _make_arb(poly_price=0.65, kalshi_price=0.66)
        self.assertIsNone(_parse_opportunity(low_spread))
        self.assertIsNotNone(_parse_opportunity(low_spread, min_spread=0.01))

    def test_volume24h_field_used_not_volume(self):
        # Ensure the parser reads volume24h, not a plain "volume" key
        arb = _make_arb()
        # Replace volume24h with a stale "volume" key (old API shape) — should fail volume check
        arb["polymarket"].pop("volume24h")
        arb["polymarket"]["volume"] = 500_000  # old wrong key
        self.assertIsNone(_parse_opportunity(arb))


if __name__ == "__main__":
    unittest.main()
