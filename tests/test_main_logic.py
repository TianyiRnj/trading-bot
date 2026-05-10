import os
import sys
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot.main as bot_main
from bot.main import (
    Bot,
    Decision,
    SafetyShutdown,
    cumulative_executed_value_usd,
    mark_position_to_market,
    position_market_value_usd,
)


def _make_position(
    market_id="market-1",
    shares=1.0,
    entry_prob=0.55,
    current_prob=None,
):
    return {
        "position_id": "pos-1",
        "market_id": market_id,
        "token_id": "token-1",
        "condition_id": "cond-1",
        "title": "Test market",
        "side": "YES",
        "entry_probability": entry_prob,
        "shares": shares,
        "size_usd": round(shares * entry_prob, 6),
        "opened_at": "2026-05-04T18:00:00+00:00",
        "current_probability": current_prob if current_prob is not None else entry_prob,
        "realized_pnl_usd": 0.0,
    }


def _make_pending_order(
    order_id="order-1",
    market_id="market-1",
    exit_reason="stop_loss",
    limit_price=0.44,
    shares=1.0,
):
    return {
        "market_id": market_id,
        "token_id": "token-1",
        "condition_id": "cond-1",
        "position_id": "pos-1",
        "side": "SELL",
        "exit_reason": exit_reason,
        "order_id": order_id,
        "created_at": "2026-05-04T18:05:00+00:00",
        "last_checked_at": "2026-05-04T18:05:00+00:00",
        "initial_shares": shares,
        "filled_shares": 0.0,
        "remaining_shares": shares,
        "limit_price": limit_price,
    }


def _make_bot_for_execute_trade() -> Bot:
    bot = Bot.__new__(Bot)
    bot.requested_mode = "paper"
    bot.effective_mode = "paper"
    bot.protection_mode_reason = None
    bot.positions = {}
    bot.assert_runtime_safety = Mock()
    bot.attempt_runtime_paper_fallback = Mock(return_value=False)
    bot.already_holding = Mock(return_value=False)
    bot.size_position = Mock(return_value=5.0)
    bot.gamma = Mock()
    bot.gamma.resolve_market.return_value = {
        "conditionId": "cond-1",
        "clobTokenIds": '["token-yes", "token-no"]',
    }
    return bot


def _make_signal_payload() -> dict:
    return {
        "success": True,
        "urgency": "high",
        "event_id": "signal-1",
        "signal_type": "tweet",
        "data": {
            "suggested_action": {
                "direction": "YES",
                "confidence": 0.80,
                "edge": 0.06,
                "reasoning": "Momentum setup",
            },
            "markets": [
                {
                    "confidence": 0.55,
                    "market": {
                        "platform": "polymarket",
                        "id": "poly-1",
                        "title": "Market One",
                        "yesPrice": 0.54,
                        "noPrice": 0.46,
                        "volume24h": 35_000,
                    },
                },
                {
                    "confidence": 0.75,
                    "market": {
                        "platform": "polymarket",
                        "id": "poly-2",
                        "title": "Market Two",
                        "yesPrice": 0.54,
                        "noPrice": 0.46,
                        "volume24h": 35_000,
                    },
                },
            ],
        },
    }


def _make_db_write_bot(mode: str) -> Bot:
    bot = Bot.__new__(Bot)
    bot.effective_mode = mode
    bot.requested_mode = mode
    return bot


def test_position_market_value_uses_probability_directly():
    assert position_market_value_usd(4.0, 0.67) == pytest.approx(2.68)


def test_mark_position_to_market_updates_value_and_unrealized():
    position = {
        "market_id": "market-1",
        "side": "YES",
        "entry_probability": 0.62,
        "size_usd": 2.48,
        "shares": 4.0,
        "unrealized_pnl_usd": 0.0,
    }

    marked = mark_position_to_market(position, 0.67)

    assert marked["current_probability"] == pytest.approx(0.67)
    assert marked["current_value_usd"] == pytest.approx(2.68)
    assert marked["unrealized_pnl_usd"] == pytest.approx(0.2)


def test_mark_position_to_market_preserves_remaining_cost_basis():
    position = {
        "market_id": "market-2",
        "side": "NO",
        "entry_probability": 0.40,
        "size_usd": 1.20,
        "shares": 3.0,
    }

    marked = mark_position_to_market(position, 0.30)

    assert marked["current_value_usd"] == pytest.approx(0.9)
    assert marked["unrealized_pnl_usd"] == pytest.approx(-0.3)


def test_cumulative_executed_value_uses_total_filled_shares():
    assert cumulative_executed_value_usd(10.0, 4.0, 0.25) == pytest.approx(1.5)


def test_live_unavailable_detector_flags_balance_issues():
    response = {"success": False, "error": "insufficient balance for order"}

    assert Bot.response_indicates_live_unavailable(response) == "insufficient_balance"


def test_live_unavailable_detector_flags_invalid_credentials():
    response = {"success": False, "error": "invalid signature on request"}

    assert Bot.response_indicates_live_unavailable(response) == "invalid_credentials"


def test_live_unavailable_detector_ignores_generic_failures():
    response = {"success": False, "error": "temporary exchange hiccup"}

    assert Bot.response_indicates_live_unavailable(response) is None


def test_close_position_can_skip_safety_check_for_protection_exit():
    bot = Bot.__new__(Bot)
    bot.assert_runtime_safety = Mock()
    bot.pending_exit_order_for_market = Mock(return_value=("order-1", {"remaining_shares": 1.0}))

    bot.close_position(
        "market-1",
        {"market_id": "market-1", "shares": 1.0},
        "live_protection_exit",
        0.55,
        skip_safety_check=True,
    )

    bot.assert_runtime_safety.assert_not_called()


def test_monitor_positions_reraises_safety_shutdown():
    bot = Bot.__new__(Bot)
    bot.positions = {"market-1": {"market_id": "market-1", "current_probability": 0.61}}
    bot.protection_mode_reason = None
    bot.sync_account_market_state = Mock()
    bot.pending_exit_order_for_market = Mock(return_value=None)
    bot.latest_position_probability = Mock(return_value=0.61)
    bot.should_exit_position = Mock(return_value=("take_profit", 0.61))
    bot.reversed_signal_detected = Mock(return_value=False)
    bot.close_position = Mock(side_effect=SafetyShutdown("boom"))

    with pytest.raises(SafetyShutdown):
        bot.monitor_positions()


@patch.object(bot_main, "REQUESTED_MODE", "paper")
@patch("bot.main.repo.has_live_exposure", return_value=True)
@patch("bot.main.check_db_schema_ready", return_value=(True, None))
@patch("bot.main.check_db_available", return_value=(True, None))
@patch("bot.main.init_pool")
@patch("bot.main.get_db")
def test_bot_refuses_paper_startup_with_persisted_live_exposure(
    mock_get_db,
    mock_init_pool,
    mock_check_db_available,
    mock_check_db_schema_ready,
    mock_has_live_exposure,
):
    del mock_init_pool, mock_check_db_available, mock_check_db_schema_ready, mock_has_live_exposure
    mock_get_db.return_value = nullcontext(object())

    with patch("bot.main._resolve_effective_mode_and_trader", return_value=("paper", None, bot_main.PaperTrader())):
        with pytest.raises(SystemExit):
            Bot()


@patch.object(bot_main, "REQUESTED_MODE", "paper")
@patch("bot.main.check_db_schema_ready", return_value=(False, "Missing required tables: orders"))
@patch("bot.main.check_db_available", return_value=(True, None))
@patch("bot.main.init_pool")
def test_bot_refuses_startup_when_required_tables_are_missing(
    mock_init_pool,
    mock_check_db_available,
    mock_check_db_schema_ready,
):
    del mock_init_pool, mock_check_db_available

    with patch("bot.main._resolve_effective_mode_and_trader", return_value=("paper", None, bot_main.PaperTrader())):
        with pytest.raises(SystemExit):
            Bot()

    mock_check_db_schema_ready.assert_called_once()


@patch("bot.main.repo.insert_trade_event")
@patch("bot.main.repo.close_order_in_db")
@patch("bot.main.get_db")
def test_process_pending_orders_clears_rejected_exit_orders(
    mock_get_db,
    mock_close_order,
    mock_insert_trade_event,
):
    bot = Bot.__new__(Bot)
    bot.requested_mode = "live"
    bot.effective_mode = "live"
    bot.user_stream = None
    bot.positions = {
        "market-1": {
            "position_id": "pos-1",
            "market_id": "market-1",
            "token_id": "token-1",
            "condition_id": "condition-1",
            "title": "Will example happen?",
            "side": "YES",
            "entry_probability": 0.55,
            "shares": 1.0,
            "opened_at": "2026-05-04T18:00:00+00:00",
        }
    }
    bot.pending_orders = {
        "order-1": {
            "market_id": "market-1",
            "token_id": "token-1",
            "condition_id": "condition-1",
            "position_id": "pos-1",
            "side": "SELL",
            "exit_reason": "stop_loss",
            "order_id": "order-1",
            "created_at": "2026-05-04T18:05:00+00:00",
            "last_checked_at": "2026-05-04T18:05:00+00:00",
            "initial_shares": 1.0,
            "filled_shares": 0.0,
            "remaining_shares": 1.0,
            "limit_price": 0.44,
        }
    }
    bot.trader = Mock()
    bot.trader.get_order_status.return_value = {
        "id": "order-1",
        "status": "rejected",
        "original_size": 1.0,
        "size_matched": 0.0,
        "remaining_size": 1.0,
        "price": 0.44,
    }
    bot.assert_runtime_safety = Mock()
    bot.response_indicates_ban_risk = Mock(return_value=False)
    bot.save_state = Mock()
    bot.close_position = Mock()

    mock_get_db.return_value = nullcontext(object())

    bot.process_pending_orders()

    assert bot.pending_orders == {}
    mock_close_order.assert_called_once()
    mock_insert_trade_event.assert_called_once()
    assert mock_insert_trade_event.call_args.kwargs["action_type"] == "rejected"
    bot.save_state.assert_called_once()


def test_apply_exit_fill_does_not_mutate_positions():
    bot = Bot.__new__(Bot)
    bot.positions = {"market-1": _make_position()}
    position = bot.positions["market-1"]

    _, updated = bot.apply_exit_fill_to_position(
        market_id="market-1",
        position=position,
        sold_shares=1.0,
        execution_price=0.60,
        exit_reason="take_profit",
        response={},
        order_status="filled",
        order_id="order-1",
    )

    assert updated is None
    assert "market-1" in bot.positions


def test_partial_exit_fill_does_not_mutate_positions():
    bot = Bot.__new__(Bot)
    bot.positions = {"market-1": _make_position(shares=2.0)}
    position = bot.positions["market-1"]

    _, updated = bot.apply_exit_fill_to_position(
        market_id="market-1",
        position=position,
        sold_shares=1.0,
        execution_price=0.60,
        exit_reason="take_profit",
        response={},
        order_status="partially_filled",
        order_id="order-1",
    )

    assert updated is not None
    assert bot.positions["market-1"]["shares"] == 2.0


@patch("bot.main.repo.close_position_in_db")
@patch("bot.main.repo.close_order_in_db")
@patch("bot.main.repo.insert_trade_event")
@patch("bot.main.repo.credit_account_on_exit")
@patch("bot.main.repo.upsert_order")
@patch("bot.main.get_db")
def test_db_failure_after_full_close_leaves_position_in_memory(
    mock_get_db,
    mock_upsert_order,
    mock_credit,
    mock_insert_event,
    mock_close_order,
    mock_close_pos,
):
    del mock_upsert_order, mock_credit, mock_insert_event, mock_close_order, mock_close_pos
    bot = Bot.__new__(Bot)
    bot.requested_mode = "live"
    bot.effective_mode = "live"
    bot.positions = {"market-1": _make_position()}
    bot.pending_orders = {}
    bot.protection_mode_reason = None
    bot.trader = Mock()
    bot.trader.get_exit_price.return_value = 0.60
    bot.trader.close_position.return_value = {
        "success": True,
        "status": "filled",
        "size_matched": 1.0,
        "price": 0.60,
        "order_id": "exit-order-1",
        "takerAmount": 0.60,
    }
    bot.assert_runtime_safety = Mock()
    bot.response_indicates_ban_risk = Mock(return_value=False)
    bot.save_state = Mock()
    bot.sync_account_market_state = Mock()
    bot.pending_exit_order_for_market = Mock(return_value=None)
    bot.current_exit_quote = Mock(return_value=None)
    bot.market_stream = None

    mock_get_db.side_effect = Exception("DB down")

    with pytest.raises(SafetyShutdown):
        bot.close_position("market-1", bot.positions["market-1"], "take_profit", 0.60)

    assert "market-1" in bot.positions


@patch("bot.main.repo.insert_trade_event")
@patch("bot.main.repo.close_order_in_db")
@patch("bot.main.get_db")
def test_rejected_pending_exit_calls_close_position(
    mock_get_db,
    mock_close_order,
    mock_insert_event,
):
    del mock_close_order, mock_insert_event
    bot = Bot.__new__(Bot)
    bot.requested_mode = "live"
    bot.effective_mode = "live"
    bot.user_stream = None
    bot.positions = {"market-1": _make_position()}
    bot.pending_orders = {"order-1": _make_pending_order()}
    bot.trader = Mock()
    bot.trader.get_order_status.return_value = {
        "id": "order-1",
        "status": "rejected",
        "original_size": 1.0,
        "size_matched": 0.0,
        "remaining_size": 1.0,
        "price": 0.44,
    }
    bot.assert_runtime_safety = Mock()
    bot.response_indicates_ban_risk = Mock(return_value=False)
    bot.save_state = Mock()
    bot.close_position = Mock()

    mock_get_db.return_value = nullcontext(object())

    bot.process_pending_orders()

    bot.close_position.assert_called_once()
    call_args = bot.close_position.call_args
    assert call_args.args[0] == "market-1"
    assert "rejected_retry" in call_args.args[2]


@patch("bot.main.repo.insert_trade_event")
@patch("bot.main.repo.close_order_in_db")
@patch("bot.main.get_db")
def test_fully_filled_terminal_does_not_trigger_replacement(
    mock_get_db,
    mock_close_order,
    mock_insert_event,
):
    del mock_close_order, mock_insert_event
    bot = Bot.__new__(Bot)
    bot.requested_mode = "live"
    bot.effective_mode = "live"
    bot.user_stream = None
    bot.positions = {"market-1": _make_position()}
    bot.pending_orders = {"order-1": _make_pending_order()}
    bot.trader = Mock()
    bot.trader.get_order_status.return_value = {
        "id": "order-1",
        "status": "filled",
        "original_size": 1.0,
        "size_matched": 1.0,
        "remaining_size": 0.0,
        "price": 0.50,
    }
    bot.assert_runtime_safety = Mock()
    bot.response_indicates_ban_risk = Mock(return_value=False)
    bot.save_state = Mock()
    bot.close_position = Mock()
    bot.sync_account_market_state = Mock()

    mock_get_db.return_value = nullcontext(object())

    bot.process_pending_orders()

    bot.close_position.assert_not_called()


def test_execute_trade_returns_false_on_liquidity_skip():
    bot = _make_bot_for_execute_trade()
    bot.trader = Mock()
    bot.trader.check_entry_liquidity.return_value = False

    decision = Mock()
    decision.market = {"id": "market-1", "title": "Test", "url": ""}
    decision.event_id = "event-1"
    decision.side = "YES"

    result = bot.execute_trade(decision)

    assert result is False


def test_execute_trade_returns_true_when_already_holding():
    bot = _make_bot_for_execute_trade()
    bot.already_holding = Mock(return_value=True)

    decision = Mock()
    decision.market = {"id": "market-1", "title": "Test", "url": ""}
    decision.event_id = "event-1"
    decision.side = "YES"

    result = bot.execute_trade(decision)

    assert result is True


def test_handle_feed_item_does_not_mark_seen_on_liquidity_skip():
    bot = Bot.__new__(Bot)
    bot.seen_event_ids = set()
    bot.musashi = Mock()
    bot.musashi.analyze_text.return_value = {"success": True}
    bot.should_trade = Mock(return_value=Mock())
    bot.execute_trade = Mock(return_value=False)
    bot.record_seen = Mock()

    bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "buy now"}})

    bot.record_seen.assert_not_called()


def test_handle_feed_item_marks_seen_on_terminal_outcome():
    bot = Bot.__new__(Bot)
    bot.seen_event_ids = set()
    bot.musashi = Mock()
    bot.musashi.analyze_text.return_value = {"success": True}
    bot.should_trade = Mock(return_value=Mock())
    bot.execute_trade = Mock(return_value=True)
    bot.record_seen = Mock()

    bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "buy now"}})

    bot.record_seen.assert_called_once_with("event-1")


def test_handle_feed_item_marks_seen_when_no_decision():
    bot = Bot.__new__(Bot)
    bot.seen_event_ids = set()
    bot.musashi = Mock()
    bot.musashi.analyze_text.return_value = {}
    bot.should_trade = Mock(return_value=None)
    bot.execute_trade = Mock()
    bot.record_seen = Mock()

    bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "irrelevant"}})

    bot.record_seen.assert_called_once_with("event-1")
    bot.execute_trade.assert_not_called()


def test_handle_feed_item_skips_already_seen():
    bot = Bot.__new__(Bot)
    bot.seen_event_ids = {"event-1"}
    bot.execute_trade = Mock()
    bot.record_seen = Mock()

    bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "buy now"}})

    bot.execute_trade.assert_not_called()
    bot.record_seen.assert_not_called()


@patch("bot.main.score_market_context")
def test_should_trade_prefers_market_with_stronger_infra_multiplier(mock_score_market_context):
    bot = Bot.__new__(Bot)
    bot.market_intelligence = Mock()
    bot.market_intelligence.get_polymarket_contexts.return_value = {
        "poly-1": {"platform_id": "poly-1", "confidence_label": "high"},
        "poly-2": {"platform_id": "poly-2", "confidence_label": "low"},
    }

    def score_side_effect(context, decision_side, max_snapshot_age_minutes):
        del decision_side, max_snapshot_age_minutes
        if context and context.get("platform_id") == "poly-1":
            return 1.35, ["infra_high_liquidity"]
        if context and context.get("platform_id") == "poly-2":
            return 0.8, ["infra_snapshot_stale"]
        return 1.0, []

    mock_score_market_context.side_effect = score_side_effect

    decision = bot.should_trade(_make_signal_payload())

    assert decision is not None
    assert decision.market["id"] == "poly-1"
    assert decision.infra_context is not None
    assert decision.infra_context["score_multiplier"] == 1.35
    assert "infra_high_liquidity" in decision.infra_context["score_reasons"]


def test_should_trade_without_market_intelligence_still_returns_best_base_score():
    bot = Bot.__new__(Bot)
    bot.market_intelligence = Mock(get_polymarket_contexts=Mock(return_value={}))

    decision = bot.should_trade(_make_signal_payload())

    assert decision is not None
    assert decision.market["id"] == "poly-2"
    assert decision.infra_context is None


def test_execute_trade_persists_infra_context_on_position_and_db_write():
    bot = _make_bot_for_execute_trade()
    bot.trader = Mock()
    bot.trader.check_entry_liquidity.return_value = True
    bot.trader.place_market_buy.return_value = {
        "success": True,
        "status": "filled",
        "orderID": "venue-order-1",
        "makingAmount": 10.0,
        "takingAmount": 5.0,
    }
    bot.response_indicates_ban_risk = Mock(return_value=False)
    bot.response_indicates_live_unavailable = Mock(return_value=None)
    bot.save_state = Mock()
    bot.refresh_account_state_from_db = Mock()
    decision = Decision(
        event_id="event-1",
        market={"id": "market-1", "title": "Test market", "url": ""},
        side="YES",
        confidence=0.80,
        edge=0.06,
        reason="Momentum setup",
        urgency="high",
        signal_type="tweet",
        probability=0.5,
        score=1.2,
        infra_context={
            "canonical_market_id": "canonical-1",
            "score_multiplier": 1.1,
            "score_reasons": ["infra_snapshot_fresh"],
        },
    )

    with patch("bot.main.get_db", return_value=nullcontext(object())):
        with patch("bot.main.repo.upsert_order"):
            with patch("bot.main.repo.upsert_position") as mock_upsert_position:
                with patch("bot.main.repo.insert_trade_event"):
                    with patch("bot.main.repo.debit_account_on_entry"):
                        result = bot.execute_trade(decision)

    assert result is True
    assert bot.positions["market-1"]["infra_context"] == decision.infra_context
    persisted_position = mock_upsert_position.call_args.args[1]
    assert persisted_position["infra_context"] == decision.infra_context


def test_paper_trader_always_allows_entry():
    trader = bot_main.PaperTrader()

    assert trader.check_entry_liquidity("token-1", 10.0) is True


def test_live_trader_blocks_when_order_book_empty():
    trader = bot_main.LiveTrader.__new__(bot_main.LiveTrader)
    trader._client = Mock()
    trader._client.get_price.return_value = None

    assert trader.check_entry_liquidity("token-1", 10.0) is False
    trader._client.get_price.assert_called_once_with("token-1", side="BUY")


def test_live_trader_allows_when_price_exists():
    trader = bot_main.LiveTrader.__new__(bot_main.LiveTrader)
    trader._client = Mock()
    trader._client.get_price.return_value = 0.55

    assert trader.check_entry_liquidity("token-1", 10.0) is True


def test_live_trader_fails_open_on_price_check_exception():
    trader = bot_main.LiveTrader.__new__(bot_main.LiveTrader)
    trader._client = Mock()
    trader._client.get_price.side_effect = RuntimeError("network error")

    assert trader.check_entry_liquidity("token-1", 10.0) is True


def test_raises_safety_shutdown_in_live_mode():
    bot = _make_db_write_bot("live")
    exc = Exception("connection refused")

    with pytest.raises(SafetyShutdown) as raised:
        bot._db_write_critical("buy_filled", exc)

    assert "buy_filled" in str(raised.value)
    assert "live mode" in str(raised.value)


def test_does_not_raise_in_paper_mode():
    bot = _make_db_write_bot("paper")
    exc = Exception("connection refused")

    bot._db_write_critical("buy_filled", exc)


def test_paper_mode_error_message_contains_label(caplog):
    bot = _make_db_write_bot("paper")
    exc = Exception("timeout")

    with caplog.at_level("ERROR", logger="musashi-poly-bot"):
        bot._db_write_critical("close_position", exc)

    assert any("close_position" in message for message in caplog.messages)
