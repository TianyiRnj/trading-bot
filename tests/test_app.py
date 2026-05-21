"""Pytest coverage for app.py health and startup behavior."""

import importlib
import os
import sys
import threading
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_app_module():
    sys.modules.pop("app", None)
    sys.modules.pop("dashboard", None)
    with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
        return importlib.import_module("app")


def _fresh_app_client():
    app_mod = _fresh_app_module()
    app_mod.app.config["TESTING"] = True
    return app_mod, app_mod.app.test_client()


def _fresh_guard_module():
    app_mod = _fresh_app_module()
    app_mod._bot_started.clear()
    app_mod._bot_thread = None
    return app_mod


def _make_bot_patches():
    import bot.main as bot_main

    return [
        patch.object(bot_main, "MusashiClient", MagicMock()),
        patch.object(bot_main, "PolymarketPublicClient", MagicMock()),
        patch.object(bot_main, "GeolocationClient", MagicMock()),
        patch.object(bot_main, "PolymarketGammaClient", MagicMock()),
        patch.object(
            bot_main,
            "_resolve_effective_mode_and_trader",
            MagicMock(return_value=("paper", None, MagicMock())),
        ),
        patch.object(bot_main, "init_pool", MagicMock()),
        patch.object(bot_main, "check_db_available", MagicMock(return_value=(True, None))),
        patch.object(bot_main, "check_db_schema_ready", MagicMock(return_value=(True, None))),
        patch.object(bot_main, "get_db", MagicMock()),
        patch.object(
            bot_main,
            "repo",
            MagicMock(
                has_live_exposure=MagicMock(return_value=False),
                insert_mode_run=MagicMock(return_value=1),
                upsert_account_state=MagicMock(),
                load_open_positions=MagicMock(return_value={}),
                load_pending_orders=MagicMock(return_value={}),
                load_seen_events=MagicMock(return_value=set()),
                get_account_state=MagicMock(return_value={"account_key": "main"}),
            ),
        ),
        patch.object(bot_main, "PolymarketMarketStream", MagicMock()),
        patch.object(bot_main, "PolymarketUserStream", MagicMock()),
    ]


def test_healthz_bot_alive_returns_200():
    app_mod, client = _fresh_app_client()
    app_mod._bot_thread = MagicMock(is_alive=MagicMock(return_value=True))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_healthz_bot_crashed_returns_503():
    app_mod, client = _fresh_app_client()
    app_mod._bot_thread = MagicMock(is_alive=MagicMock(return_value=False))

    response = client.get("/healthz")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["ok"] is False
    assert "reason" in payload


def test_healthz_bot_not_started_returns_503():
    app_mod, client = _fresh_app_client()
    app_mod._bot_thread = None

    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.get_json()["ok"] is False


def test_module_import_starts_bot_once():
    imported = _fresh_app_module()

    assert imported._bot_started.is_set() is True
    assert imported._bot_thread is not None


def test_try_start_bot_returns_false_when_already_started():
    app_mod = _fresh_guard_module()

    def fake_run():
        return None

    with patch.object(app_mod, "_run_bot", fake_run):
        with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
            first = app_mod._try_start_bot()
            second = app_mod._try_start_bot()

    assert first is True
    assert second is False


def test_try_start_bot_sets_started_flag():
    app_mod = _fresh_guard_module()

    def fake_run():
        return None

    with patch.object(app_mod, "_run_bot", fake_run):
        with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
            app_mod._try_start_bot()

    assert app_mod._bot_started.is_set() is True


def test_try_start_bot_stores_thread_reference():
    app_mod = _fresh_guard_module()

    def fake_run():
        return None

    with patch.object(app_mod, "_run_bot", fake_run):
        with patch.object(threading.Thread, "start", autospec=True, side_effect=lambda self: None):
            app_mod._try_start_bot()

    assert app_mod._bot_thread is not None
    assert isinstance(app_mod._bot_thread, threading.Thread)


def test_install_signal_handlers_false_skips_signal_registration():
    import bot.main as bot_main

    mock_signal = MagicMock()
    patches = _make_bot_patches()
    with patch.object(bot_main.signal, "signal", mock_signal):
        for patcher in patches:
            patcher.start()
        try:
            bot_main.Bot(install_signal_handlers=False)
        finally:
            for patcher in patches:
                patcher.stop()

    mock_signal.assert_not_called()


def test_install_signal_handlers_true_registers_sigterm():
    import signal
    import bot.main as bot_main

    mock_signal = MagicMock()
    patches = _make_bot_patches()
    with patch.object(bot_main.signal, "signal", mock_signal):
        for patcher in patches:
            patcher.start()
        try:
            bot = bot_main.Bot(install_signal_handlers=True)
        finally:
            for patcher in patches:
                patcher.stop()

    mock_signal.assert_called_once_with(signal.SIGTERM, bot._handle_sigterm)
