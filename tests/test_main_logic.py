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
    @patch("bot.main.check_db_available", return_value=(True, None))
    @patch("bot.main.init_pool")
    @patch("bot.main.get_db")
    def test_bot_refuses_paper_startup_with_persisted_live_exposure(
        self,
        mock_get_db,
        mock_init_pool,
        mock_check_db_available,
        mock_has_live_exposure,
    ):
        del mock_init_pool, mock_check_db_available, mock_has_live_exposure
        mock_get_db.return_value = nullcontext(object())

        with patch("bot.main._resolve_effective_mode_and_trader", return_value=("paper", None, bot_main.PaperTrader())):
            with self.assertRaises(SystemExit):
                Bot()


if __name__ == "__main__":
    unittest.main()
