import os
import sys
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot.main as bot_main
from bot.main import (
    Bot,
    SafetyShutdown,
    cumulative_executed_value_usd,
    mark_position_to_market,
    position_market_value_usd,
)


class TestMainLogic(unittest.TestCase):
    def test_position_market_value_uses_probability_directly(self):
        self.assertAlmostEqual(position_market_value_usd(4.0, 0.67), 2.68)

    def test_mark_position_to_market_updates_value_and_unrealized(self):
        position = {
            "market_id": "market-1",
            "side": "YES",
            "entry_probability": 0.62,
            "size_usd": 2.48,
            "shares": 4.0,
            "unrealized_pnl_usd": 0.0,
        }

        marked = mark_position_to_market(position, 0.67)

        self.assertAlmostEqual(marked["current_probability"], 0.67)
        self.assertAlmostEqual(marked["current_value_usd"], 2.68)
        self.assertAlmostEqual(marked["unrealized_pnl_usd"], 0.2)

    def test_mark_position_to_market_preserves_remaining_cost_basis(self):
        position = {
            "market_id": "market-2",
            "side": "NO",
            "entry_probability": 0.40,
            "size_usd": 1.20,
            "shares": 3.0,
        }

        marked = mark_position_to_market(position, 0.30)

        self.assertAlmostEqual(marked["current_value_usd"], 0.9)
        self.assertAlmostEqual(marked["unrealized_pnl_usd"], -0.3)

    def test_cumulative_executed_value_uses_total_filled_shares(self):
        self.assertAlmostEqual(cumulative_executed_value_usd(10.0, 4.0, 0.25), 1.5)

    def test_live_unavailable_detector_flags_balance_issues(self):
        response = {"success": False, "error": "insufficient balance for order"}
        self.assertEqual(
            Bot.response_indicates_live_unavailable(response),
            "insufficient_balance",
        )

    def test_live_unavailable_detector_flags_invalid_credentials(self):
        response = {"success": False, "error": "invalid signature on request"}
        self.assertEqual(
            Bot.response_indicates_live_unavailable(response),
            "invalid_credentials",
        )

    def test_live_unavailable_detector_ignores_generic_failures(self):
        response = {"success": False, "error": "temporary exchange hiccup"}
        self.assertIsNone(Bot.response_indicates_live_unavailable(response))

    def test_close_position_can_skip_safety_check_for_protection_exit(self):
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

    def test_monitor_positions_reraises_safety_shutdown(self):
        bot = Bot.__new__(Bot)
        bot.positions = {"market-1": {"market_id": "market-1", "current_probability": 0.61}}
        bot.protection_mode_reason = None
        bot.sync_account_market_state = Mock()
        bot.pending_exit_order_for_market = Mock(return_value=None)
        bot.latest_position_probability = Mock(return_value=0.61)
        bot.should_exit_position = Mock(return_value=("take_profit", 0.61))
        bot.reversed_signal_detected = Mock(return_value=False)
        bot.close_position = Mock(side_effect=SafetyShutdown("boom"))

        with self.assertRaises(SafetyShutdown):
            bot.monitor_positions()

    @patch.object(bot_main, "REQUESTED_MODE", "paper")
    @patch("bot.main.repo.has_live_exposure", return_value=True)
    @patch("bot.main.check_db_schema_ready", return_value=(True, None))
    @patch("bot.main.check_db_available", return_value=(True, None))
    @patch("bot.main.init_pool")
    @patch("bot.main.get_db")
    def test_bot_refuses_paper_startup_with_persisted_live_exposure(
        self,
        mock_get_db,
        mock_init_pool,
        mock_check_db_available,
        mock_check_db_schema_ready,
        mock_has_live_exposure,
    ):
        del mock_init_pool, mock_check_db_available, mock_check_db_schema_ready, mock_has_live_exposure
        mock_get_db.return_value = nullcontext(object())

        with patch("bot.main._resolve_effective_mode_and_trader", return_value=("paper", None, bot_main.PaperTrader())):
            with self.assertRaises(SystemExit):
                Bot()

    @patch.object(bot_main, "REQUESTED_MODE", "paper")
    @patch("bot.main.check_db_schema_ready", return_value=(False, "Missing required tables: orders"))
    @patch("bot.main.check_db_available", return_value=(True, None))
    @patch("bot.main.init_pool")
    def test_bot_refuses_startup_when_required_tables_are_missing(
        self,
        mock_init_pool,
        mock_check_db_available,
        mock_check_db_schema_ready,
    ):
        del mock_init_pool, mock_check_db_available

        with patch("bot.main._resolve_effective_mode_and_trader", return_value=("paper", None, bot_main.PaperTrader())):
            with self.assertRaises(SystemExit):
                Bot()

        mock_check_db_schema_ready.assert_called_once()

    @patch("bot.main.repo.insert_trade_event")
    @patch("bot.main.repo.close_order_in_db")
    @patch("bot.main.get_db")
    def test_process_pending_orders_clears_rejected_exit_orders(
        self,
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
        bot.close_position = Mock()  # prevent attribute errors from replacement-exit path

        mock_get_db.return_value = nullcontext(object())

        bot.process_pending_orders()

        self.assertEqual(bot.pending_orders, {})
        mock_close_order.assert_called_once()
        mock_insert_trade_event.assert_called_once()
        self.assertEqual(mock_insert_trade_event.call_args.kwargs["action_type"], "rejected")
        bot.save_state.assert_called_once()


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


class TestFix1ClosePositionDbWriteFailure(unittest.TestCase):
    """Fix 1: a filled live exit must not remove the position from self.positions
    before the DB write succeeds so persist_runtime_state can still flush it."""

    def test_apply_exit_fill_does_not_mutate_positions(self):
        """apply_exit_fill_to_position is now side-effect-free on self.positions."""
        bot = Bot.__new__(Bot)
        bot.positions = {"market-1": _make_position()}
        position = bot.positions["market-1"]

        _, updated = bot.apply_exit_fill_to_position(
            market_id="market-1",
            position=position,
            sold_shares=1.0,  # full close
            execution_price=0.60,
            exit_reason="take_profit",
            response={},
            order_status="filled",
            order_id="order-1",
        )

        self.assertIsNone(updated)  # full close → updated_position is None
        # Position must still be in self.positions (caller is responsible for removal)
        self.assertIn("market-1", bot.positions)

    def test_partial_exit_fill_does_not_mutate_positions(self):
        """Partial fill also leaves mutation to the caller."""
        bot = Bot.__new__(Bot)
        bot.positions = {"market-1": _make_position(shares=2.0)}
        position = bot.positions["market-1"]

        _, updated = bot.apply_exit_fill_to_position(
            market_id="market-1",
            position=position,
            sold_shares=1.0,  # partial — 1.0 remaining
            execution_price=0.60,
            exit_reason="take_profit",
            response={},
            order_status="partially_filled",
            order_id="order-1",
        )

        self.assertIsNotNone(updated)
        # self.positions still has the ORIGINAL (stale) position; caller updates it
        self.assertEqual(bot.positions["market-1"]["shares"], 2.0)

    @patch("bot.main.repo.close_position_in_db")
    @patch("bot.main.repo.close_order_in_db")
    @patch("bot.main.repo.insert_trade_event")
    @patch("bot.main.repo.credit_account_on_exit")
    @patch("bot.main.repo.upsert_order")
    @patch("bot.main.get_db")
    def test_db_failure_after_full_close_leaves_position_in_memory(
        self,
        mock_get_db,
        mock_upsert_order,
        mock_credit,
        mock_insert_event,
        mock_close_order,
        mock_close_pos,
    ):
        """After a full close + DB failure, position stays in self.positions so
        persist_runtime_state can flush it on shutdown."""
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

        # Make the DB block raise so _db_write_critical fires
        mock_get_db.side_effect = Exception("DB down")

        with self.assertRaises(SafetyShutdown):
            bot.close_position("market-1", bot.positions["market-1"], "take_profit", 0.60)

        # Position must still be in self.positions — NOT removed before DB write
        self.assertIn("market-1", bot.positions)


class TestFix2RejectedExitRetry(unittest.TestCase):
    """Fix 2: a rejected pending exit must trigger a replacement close_position call
    rather than silently leaving the position unmanaged."""

    @patch("bot.main.repo.insert_trade_event")
    @patch("bot.main.repo.close_order_in_db")
    @patch("bot.main.get_db")
    def test_rejected_pending_exit_calls_close_position(
        self, mock_get_db, mock_close_order, mock_insert_event
    ):
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
        bot.close_position = Mock()  # capture replacement call

        mock_get_db.return_value = nullcontext(object())

        bot.process_pending_orders()

        bot.close_position.assert_called_once()
        call_args = bot.close_position.call_args
        self.assertEqual(call_args.args[0], "market-1")
        self.assertIn("rejected_retry", call_args.args[2])

    @patch("bot.main.repo.insert_trade_event")
    @patch("bot.main.repo.close_order_in_db")
    @patch("bot.main.get_db")
    def test_fully_filled_terminal_does_not_trigger_replacement(
        self, mock_get_db, mock_close_order, mock_insert_event
    ):
        """A fully-filled terminal order should not cause a replacement close."""
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


class TestFix3LiquiditySkipRetriable(unittest.TestCase):
    """Fix 3: execute_trade returns False for a liquidity skip so the feed event
    is not permanently consumed, and returns True for all terminal outcomes."""

    def _make_bot_for_execute_trade(self):
        bot = Bot.__new__(Bot)
        bot.requested_mode = "paper"
        bot.effective_mode = "paper"
        bot.protection_mode_reason = None
        bot.positions = {}
        bot.assert_runtime_safety = Mock()
        bot.already_holding = Mock(return_value=False)
        bot.size_position = Mock(return_value=5.0)
        bot.gamma = Mock()
        # Two tokens required: index 0 = YES, index 1 = NO
        bot.gamma.resolve_market.return_value = {"clobTokenIds": '["token-yes", "token-no"]'}
        return bot

    def test_execute_trade_returns_false_on_liquidity_skip(self):
        bot = self._make_bot_for_execute_trade()
        bot.trader = Mock()
        bot.trader.check_entry_liquidity.return_value = False

        decision = Mock()
        decision.market = {"id": "market-1", "title": "Test", "url": ""}
        decision.event_id = "event-1"
        decision.side = "YES"

        result = bot.execute_trade(decision)

        self.assertFalse(result)

    def test_execute_trade_returns_true_when_already_holding(self):
        bot = self._make_bot_for_execute_trade()
        bot.already_holding = Mock(return_value=True)

        decision = Mock()
        decision.market = {"id": "market-1", "title": "Test", "url": ""}
        decision.event_id = "event-1"
        decision.side = "YES"

        result = bot.execute_trade(decision)

        self.assertTrue(result)

    def test_handle_feed_item_does_not_mark_seen_on_liquidity_skip(self):
        """A liquidity skip (False return) must NOT mark the event as seen."""
        bot = Bot.__new__(Bot)
        bot.seen_event_ids = set()
        bot.musashi = Mock()
        bot.musashi.analyze_text.return_value = {"success": True}
        bot.should_trade = Mock(return_value=Mock())  # produces a decision
        bot.execute_trade = Mock(return_value=False)  # liquidity skip
        bot.record_seen = Mock()

        bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "buy now"}})

        bot.record_seen.assert_not_called()

    def test_handle_feed_item_marks_seen_on_terminal_outcome(self):
        """A terminal outcome (True return) MUST mark the event as seen."""
        bot = Bot.__new__(Bot)
        bot.seen_event_ids = set()
        bot.musashi = Mock()
        bot.musashi.analyze_text.return_value = {"success": True}
        bot.should_trade = Mock(return_value=Mock())
        bot.execute_trade = Mock(return_value=True)
        bot.record_seen = Mock()

        bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "buy now"}})

        bot.record_seen.assert_called_once_with("event-1")

    def test_handle_feed_item_marks_seen_when_no_decision(self):
        """If should_trade returns no decision, the event is still permanently seen."""
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

    def test_handle_feed_item_skips_already_seen(self):
        """An already-seen event is silently skipped without calling execute_trade."""
        bot = Bot.__new__(Bot)
        bot.seen_event_ids = {"event-1"}
        bot.execute_trade = Mock()
        bot.record_seen = Mock()

        bot.handle_feed_item({"event_id": "event-1", "tweet": {"text": "buy now"}})

        bot.execute_trade.assert_not_called()
        bot.record_seen.assert_not_called()


class TestLiquidityGuard(unittest.TestCase):
    """check_entry_liquidity on PaperTrader and LiveTrader."""

    def test_paper_trader_always_allows_entry(self):
        trader = bot_main.PaperTrader()
        self.assertTrue(trader.check_entry_liquidity("token-1", 10.0))

    def test_live_trader_blocks_when_order_book_empty(self):
        trader = bot_main.LiveTrader.__new__(bot_main.LiveTrader)
        trader._client = Mock()
        trader._client.get_price.return_value = None
        self.assertFalse(trader.check_entry_liquidity("token-1", 10.0))
        trader._client.get_price.assert_called_once_with("token-1", side="BUY")

    def test_live_trader_allows_when_price_exists(self):
        trader = bot_main.LiveTrader.__new__(bot_main.LiveTrader)
        trader._client = Mock()
        trader._client.get_price.return_value = 0.55
        self.assertTrue(trader.check_entry_liquidity("token-1", 10.0))

    def test_live_trader_fails_open_on_price_check_exception(self):
        trader = bot_main.LiveTrader.__new__(bot_main.LiveTrader)
        trader._client = Mock()
        trader._client.get_price.side_effect = RuntimeError("network error")
        # Should fail open (True) rather than silently block all trades
        self.assertTrue(trader.check_entry_liquidity("token-1", 10.0))


class TestDbWriteCriticalPolicy(unittest.TestCase):
    """_db_write_critical: SafetyShutdown in live mode, log-and-continue in paper mode."""

    def _make_bot(self, mode: str) -> Bot:
        bot = Bot.__new__(Bot)
        bot.effective_mode = mode
        bot.requested_mode = mode
        return bot

    def test_raises_safety_shutdown_in_live_mode(self):
        bot = self._make_bot("live")
        exc = Exception("connection refused")
        with self.assertRaises(SafetyShutdown) as ctx:
            bot._db_write_critical("buy_filled", exc)
        self.assertIn("buy_filled", str(ctx.exception))
        self.assertIn("live mode", str(ctx.exception))

    def test_does_not_raise_in_paper_mode(self):
        bot = self._make_bot("paper")
        exc = Exception("connection refused")
        # Should not raise — just log
        try:
            bot._db_write_critical("buy_filled", exc)
        except SafetyShutdown:
            self.fail("_db_write_critical raised SafetyShutdown in paper mode")

    def test_paper_mode_error_message_contains_label(self):
        bot = self._make_bot("paper")
        exc = Exception("timeout")
        with self.assertLogs("musashi-poly-bot", level="ERROR") as log_ctx:
            bot._db_write_critical("close_position", exc)
        self.assertTrue(any("close_position" in line for line in log_ctx.output))


if __name__ == "__main__":
    unittest.main()
