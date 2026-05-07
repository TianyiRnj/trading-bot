"""Tests for app.py: /healthz liveness semantics and bot start guard."""
import importlib
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_app_module():
    sys.modules.pop("app", None)
    sys.modules.pop("dashboard", None)
    with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
        return importlib.import_module("app")


class TestHealthzEndpoint(unittest.TestCase):
    """The /healthz route is registered by app.py and reflects bot liveness."""

    def setUp(self):
        self.app_mod = _fresh_app_module()
        self.app_mod.app.config["TESTING"] = True
        self.client = self.app_mod.app.test_client()

    def test_healthz_bot_alive_returns_200(self):
        self.app_mod._bot_thread = MagicMock(is_alive=MagicMock(return_value=True))

        resp = self.client.get("/healthz")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_healthz_bot_crashed_returns_503(self):
        self.app_mod._bot_thread = MagicMock(is_alive=MagicMock(return_value=False))

        resp = self.client.get("/healthz")

        self.assertEqual(resp.status_code, 503)
        data = resp.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("reason", data)

    def test_healthz_bot_not_started_returns_503(self):
        self.app_mod._bot_thread = None

        resp = self.client.get("/healthz")

        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json()["ok"])


class TestBotStartGuard(unittest.TestCase):
    def setUp(self):
        self.app_mod = _fresh_app_module()
        self.app_mod._bot_started.clear()
        self.app_mod._bot_thread = None

    def test_module_import_starts_bot_once(self):
        imported = _fresh_app_module()

        self.assertTrue(imported._bot_started.is_set())
        self.assertIsNotNone(imported._bot_thread)

    def test_try_start_bot_returns_false_when_already_started(self):
        def fake_run():
            pass

        with patch.object(self.app_mod, "_run_bot", fake_run):
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
                first = self.app_mod._try_start_bot()
                second = self.app_mod._try_start_bot()

        self.assertTrue(first)
        self.assertFalse(second)

    def test_try_start_bot_sets_started_flag(self):
        def fake_run():
            pass

        with patch.object(self.app_mod, "_run_bot", fake_run):
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
                self.app_mod._try_start_bot()

        self.assertTrue(self.app_mod._bot_started.is_set())

    def test_try_start_bot_stores_thread_reference(self):
        def fake_run():
            pass

        with patch.object(self.app_mod, "_run_bot", fake_run):
            with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
                self.app_mod._try_start_bot()

        self.assertIsNotNone(self.app_mod._bot_thread)
        self.assertIsInstance(self.app_mod._bot_thread, threading.Thread)


class TestSignalHandlerSafeInThread(unittest.TestCase):
    """Bot(install_signal_handlers=False) must not call signal.signal()."""

    def _make_bot_patches(self):
        import bot.main as bot_main
        return [
            patch.object(bot_main, "MusashiClient", MagicMock()),
            patch.object(bot_main, "PolymarketPublicClient", MagicMock()),
            patch.object(bot_main, "GeolocationClient", MagicMock()),
            patch.object(bot_main, "PolymarketGammaClient", MagicMock()),
            patch.object(bot_main, "_resolve_effective_mode_and_trader",
                         MagicMock(return_value=("paper", None, MagicMock()))),
            patch.object(bot_main, "init_pool", MagicMock()),
            patch.object(bot_main, "check_db_available", MagicMock(return_value=(True, None))),
            patch.object(bot_main, "check_db_schema_ready", MagicMock(return_value=(True, None))),
            patch.object(bot_main, "get_db", MagicMock()),
            patch.object(bot_main, "repo", MagicMock(
                has_live_exposure=MagicMock(return_value=False),
                insert_mode_run=MagicMock(return_value=1),
                upsert_account_state=MagicMock(),
                load_open_positions=MagicMock(return_value={}),
                load_pending_orders=MagicMock(return_value={}),
                load_seen_events=MagicMock(return_value=set()),
                get_account_state=MagicMock(return_value={"account_key": "main"}),
            )),
            patch.object(bot_main, "PolymarketMarketStream", MagicMock()),
            patch.object(bot_main, "PolymarketUserStream", MagicMock()),
        ]

    def test_install_signal_handlers_false_skips_signal_registration(self):
        import bot.main as bot_main

        mock_signal = MagicMock()
        patches = self._make_bot_patches()
        with patch.object(bot_main.signal, "signal", mock_signal):
            for p in patches:
                p.start()
            try:
                bot_main.Bot(install_signal_handlers=False)
            finally:
                for p in patches:
                    p.stop()

        mock_signal.assert_not_called()

    def test_install_signal_handlers_true_registers_sigterm(self):
        import signal
        import bot.main as bot_main

        mock_signal = MagicMock()
        patches = self._make_bot_patches()
        with patch.object(bot_main.signal, "signal", mock_signal):
            for p in patches:
                p.start()
            try:
                bot = bot_main.Bot(install_signal_handlers=True)
            finally:
                for p in patches:
                    p.stop()

        mock_signal.assert_called_once_with(signal.SIGTERM, bot._handle_sigterm)


if __name__ == "__main__":
    unittest.main()
