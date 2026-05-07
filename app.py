"""Unified app entry point for local runs and Railway deployment.

Starts the Flask dashboard and launches the bot as a background daemon thread
in the same process.

Local development: `python3 app.py`
Cloud deployment: `gunicorn --workers 1 --threads 4 app:app`
"""

import logging
import os
import threading

from dashboard import app

_logger = logging.getLogger(__name__)

_bot_started = threading.Event()
_bot_thread: threading.Thread | None = None


def _run_bot() -> None:
    try:
        from bot.main import configure_logging, Bot  # noqa: PLC0415
        configure_logging()
        # Signal handlers must only be installed in the main thread.
        # In background-thread mode gunicorn handles SIGTERM for the process,
        # which terminates daemon threads cleanly.
        Bot(install_signal_handlers=False).run()
    except SystemExit:
        pass
    except Exception as exc:
        _logger.critical("Bot thread crashed: %s", exc, exc_info=True)


def _try_start_bot() -> bool:
    """Start bot in a background daemon thread.

    Returns True if the thread was started, False if the bot was already
    running (guards against double-start from repeated module imports or
    gunicorn pre-fork behaviour with --workers > 1).
    """
    global _bot_thread
    if _bot_started.is_set():
        _logger.warning("Bot start requested but bot is already running — skipped")
        return False
    _bot_started.set()
    _bot_thread = threading.Thread(target=_run_bot, name="bot-main", daemon=True)
    _bot_thread.start()
    _logger.info("Bot background thread started (daemon=True, thread=%s)", _bot_thread.name)
    return True


@app.route("/healthz")
def healthz():
    from flask import jsonify  # noqa: PLC0415
    if _bot_thread is None or not _bot_thread.is_alive():
        return jsonify({"ok": False, "reason": "bot_thread_not_alive"}), 503
    return jsonify({"ok": True})


_try_start_bot()


if __name__ == "__main__":
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = int(os.getenv("PORT", "5000"))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=HOST, port=PORT, debug=DEBUG)
