"""Pytest coverage for the arbitrage parser and Supabase fallback path."""

import os
import sys
import threading
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

from arbitrage_strategy import (
    MIN_SPREAD_PERCENT,
    MIN_VOLUME_USD,
    POSITION_SIZE_USD,
    ArbitrageOpportunity,
    ArbitrageStrategy,
    _derive_opportunities_from_market_rows,
    _parse_opportunity,
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


def _make_fallback_rows() -> list[dict[str, object]]:
    return [
        {
            "id": "musashi-poly-1",
            "platform": "polymarket",
            "platform_id": "poly-1",
            "event_id": "evt-1",
            "series_id": None,
            "title": "Will X happen?",
            "yes_price": 0.61,
            "volume_24h": 200_000,
            "liquidity": 20_000,
            "open_interest": 5_000,
        },
        {
            "id": "musashi-kalshi-1",
            "platform": "kalshi",
            "platform_id": "kalshi-1",
            "event_id": "evt-1",
            "series_id": None,
            "title": "Will X happen?",
            "yes_price": 0.70,
            "volume_24h": 150_000,
            "liquidity": 12_000,
            "open_interest": 4_000,
        },
    ]


def test_documented_api_shape_parses_correctly():
    opp = _parse_opportunity(_make_arb())

    assert opp is not None
    assert isinstance(opp, ArbitrageOpportunity)
    assert opp.poly_market_id == "poly-1"
    assert opp.kalshi_market_id == "kalshi-1"
    assert opp.title == "Will Bitcoin reach $100k by June 2026?"
    assert opp.poly_price == 0.63
    assert opp.kalshi_price == 0.70
    assert opp.buy_platform == "polymarket"
    assert opp.sell_platform == "kalshi"


def test_direction_buy_kalshi_when_kalshi_cheaper():
    opp = _parse_opportunity(_make_arb(poly_price=0.70, kalshi_price=0.63))

    assert opp is not None
    assert opp.buy_platform == "kalshi"
    assert opp.sell_platform == "polymarket"


def test_profit_calculation():
    opp = _parse_opportunity(_make_arb(poly_price=0.63, kalshi_price=0.70))
    expected_spread = abs(0.63 - 0.70)

    assert opp is not None
    assert opp.profit_usd == expected_spread * POSITION_SIZE_USD


def test_title_falls_back_to_kalshi_when_poly_missing():
    arb = _make_arb()
    del arb["polymarket"]["title"]

    opp = _parse_opportunity(arb)

    assert opp is not None
    assert opp.title == "Bitcoin $100k by June 2026"


def test_missing_polymarket_returns_none():
    arb = {"polymarket": {}, "kalshi": {"id": "k", "yesPrice": 0.7, "volume24h": 100_000}}

    assert _parse_opportunity(arb) is None


def test_missing_kalshi_returns_none():
    arb = {"polymarket": {"id": "p", "yesPrice": 0.6, "volume24h": 100_000}, "kalshi": {}}

    assert _parse_opportunity(arb) is None


def test_zero_poly_price_returns_none():
    assert _parse_opportunity(_make_arb(poly_price=0)) is None


def test_zero_kalshi_price_returns_none():
    assert _parse_opportunity(_make_arb(kalshi_price=0)) is None


def test_spread_below_threshold_returns_none():
    assert _parse_opportunity(_make_arb(poly_price=0.65, kalshi_price=0.66)) is None


def test_low_poly_volume_returns_none():
    assert _parse_opportunity(_make_arb(poly_vol=MIN_VOLUME_USD - 1)) is None


def test_low_kalshi_volume_returns_none():
    assert _parse_opportunity(_make_arb(kalshi_vol=MIN_VOLUME_USD - 1)) is None


def test_exactly_at_volume_threshold_passes():
    opp = _parse_opportunity(_make_arb(poly_vol=MIN_VOLUME_USD, kalshi_vol=MIN_VOLUME_USD))

    assert opp is not None


def test_custom_min_spread_override():
    low_spread = _make_arb(poly_price=0.65, kalshi_price=0.66)

    assert _parse_opportunity(low_spread) is None
    assert _parse_opportunity(low_spread, min_spread=0.01) is not None


def test_volume24h_field_used_not_volume():
    arb = _make_arb()
    arb["polymarket"].pop("volume24h")
    arb["polymarket"]["volume"] = 500_000

    assert _parse_opportunity(arb) is None


def test_derive_opportunities_from_market_rows_groups_by_event_id():
    opportunities = _derive_opportunities_from_market_rows(_make_fallback_rows())

    assert len(opportunities) == 1
    assert opportunities[0].poly_market_id == "poly-1"
    assert opportunities[0].kalshi_market_id == "kalshi-1"
    assert opportunities[0].buy_platform == "polymarket"


def test_find_arbitrage_uses_supabase_fallback_when_api_is_empty():
    musashi_client = Mock()
    musashi_client.get_arbitrage.return_value = {"success": True, "data": {"opportunities": []}}
    market_intelligence = Mock()
    market_intelligence.list_cross_platform_markets.return_value = _make_fallback_rows()
    strategy = ArbitrageStrategy(
        gamma_client=None,
        musashi_client=musashi_client,
        trader=None,
        positions={},
        save_state_callback=lambda: None,
        market_intelligence=market_intelligence,
    )

    opportunities = strategy.find_arbitrage_opportunities()

    assert len(opportunities) == 1
    assert opportunities[0].kalshi_market_id == "kalshi-1"
    market_intelligence.list_cross_platform_markets.assert_called_once_with(min_volume=MIN_VOLUME_USD)


def test_find_arbitrage_fallback_skips_previously_executed_pairs():
    musashi_client = Mock()
    musashi_client.get_arbitrage.return_value = {"success": True, "data": {"opportunities": []}}
    market_intelligence = Mock()
    market_intelligence.list_cross_platform_markets.return_value = _make_fallback_rows()
    strategy = ArbitrageStrategy(
        gamma_client=None,
        musashi_client=musashi_client,
        trader=None,
        positions={},
        save_state_callback=lambda: None,
        market_intelligence=market_intelligence,
    )
    strategy._mark_executed("poly-1:kalshi-1")

    opportunities = strategy.find_arbitrage_opportunities()

    assert opportunities == []


def test_executed_arbs_eviction_preserves_recent_keys():
    strategy = ArbitrageStrategy(
        gamma_client=None,
        musashi_client=Mock(),
        trader=None,
        positions={},
        save_state_callback=lambda: None,
    )
    for i in range(501):
        strategy._mark_executed(f"key-{i}")

    # 501 inserts: threshold crossed at 501st insert, oldest 250 evicted → 251 remain
    assert len(strategy.executed_arbs) == 251
    assert "key-0" not in strategy.executed_arbs    # oldest, must be evicted
    assert "key-500" in strategy.executed_arbs      # newest, must be kept


def test_run_scanner_stop_event_exits_loop():
    """Verify the scanner exits promptly when stop() is called.

    The test proves that time.sleep was replaced with Event.wait: with a 60s
    scan interval, the scanner would hang for ~60s if sleep were used, causing
    scanner_thread.join(timeout=5) to time out and the final assert to fail.
    """
    scan_completed = threading.Event()
    musashi_client = Mock()

    def _signal_and_return(**_kwargs):
        scan_completed.set()
        return {"success": True, "data": {"opportunities": []}}

    musashi_client.get_arbitrage.side_effect = _signal_and_return
    strategy = ArbitrageStrategy(
        gamma_client=None,
        musashi_client=musashi_client,
        trader=None,
        positions={},
        save_state_callback=lambda: None,
        scan_interval=60,
    )

    scanner_thread = threading.Thread(target=strategy.run_scanner, daemon=True)
    scanner_thread.start()

    # Wait until the first scan has completed; scanner is now in Event.wait(60).
    # Calling stop() either pre-arms the event (if we beat the wait) or wakes a
    # blocked wait — both paths cause run_scanner to return promptly.
    assert scan_completed.wait(timeout=5), "scanner did not complete first scan in time"
    strategy.stop()

    scanner_thread.join(timeout=5)
    assert not scanner_thread.is_alive(), "scanner should exit promptly when stopped"


def test_scan_interval_passed_to_strategy():
    strategy = ArbitrageStrategy(
        gamma_client=None,
        musashi_client=Mock(),
        trader=None,
        positions={},
        save_state_callback=lambda: None,
        scan_interval=99,
    )
    assert strategy.scan_interval == 99


def test_scan_interval_clamped_to_minimum():
    strategy = ArbitrageStrategy(
        gamma_client=None,
        musashi_client=Mock(),
        trader=None,
        positions={},
        save_state_callback=lambda: None,
        scan_interval=0,
    )
    assert strategy.scan_interval == 1
