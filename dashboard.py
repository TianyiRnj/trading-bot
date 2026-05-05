import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False
from flask import Flask, jsonify, render_template

try:
    from db import check_db_available, get_db, init_pool
    import repository as repo
except Exception:
    try:
        from bot.db import check_db_available, get_db, init_pool
        import bot.repository as repo
    except Exception as exc:  # pragma: no cover - import fallback
        check_db_available = None
        get_db = None
        init_pool = None
        repo = None
        _DB_IMPORT_ERROR: Exception | None = exc
    else:
        _DB_IMPORT_ERROR = None
else:
    _DB_IMPORT_ERROR = None

load_dotenv()

app = Flask(__name__)

_ROOT = Path(__file__).resolve().parent
DATA_DIR = _ROOT / "bot" / "data"
LOG_FILE = _ROOT / "bot" / "logs" / "bot.log"
ARB_TRADES_FILE = DATA_DIR / "arbitrage_trades.jsonl"
ACCOUNT_KEY = "main"


def load_arbitrage_trades() -> list[dict[str, Any]]:
    if not ARB_TRADES_FILE.exists():
        return []
    trades: list[dict[str, Any]] = []
    try:
        for line in ARB_TRADES_FILE.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return trades


def get_logs(limit: int = 20) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="ignore").strip().split("\n")
        return lines[-limit:]
    except Exception:
        return []


def format_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso_str


def _compute_arbitrage_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    total_profit = sum(float(t.get("realized_pnl", 0)) for t in trades)
    arb_count = len(trades)
    winning = sum(1 for t in trades if float(t.get("realized_pnl", 0)) > 0)
    win_rate = (winning / arb_count * 100) if arb_count > 0 else 0.0
    spreads = [float(t.get("spread_percent", 0)) * 100 for t in trades]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    bankroll = 100.0 + total_profit
    return {
        "total_profit": total_profit,
        "arb_count": arb_count,
        "win_rate": win_rate,
        "avg_spread": avg_spread,
        "bankroll": bankroll,
        "available": bankroll,
    }


def _ensure_db_ready() -> tuple[bool, str | None]:
    if _DB_IMPORT_ERROR is not None:
        return False, str(_DB_IMPORT_ERROR)
    if init_pool is None or check_db_available is None:
        return False, "Postgres helpers are unavailable"
    try:
        init_pool(
            min_size=int(os.getenv("POSTGRES_POOL_MIN", "1")),
            max_size=int(os.getenv("POSTGRES_POOL_MAX", "5")),
            timeout=float(os.getenv("POSTGRES_CONNECT_TIMEOUT_SECONDS", "10")),
        )
    except Exception as exc:
        return False, str(exc)
    return check_db_available()


def _load_main_dashboard_data() -> tuple[dict[str, Any], str | None]:
    ready, error = _ensure_db_ready()
    if not ready:
        return {
            "summary": {},
            "positions": [],
            "orders": [],
            "actions": [],
            "recent_trades": [],
            "equity_snapshots": [],
        }, error

    try:
        assert get_db is not None and repo is not None
        with get_db() as conn:
            payload = {
                "summary": repo.get_dashboard_summary(conn, ACCOUNT_KEY),
                "positions": repo.list_open_positions(conn, limit=25),
                "orders": repo.list_open_orders(conn, limit=25),
                "actions": repo.list_recent_trade_events(conn, limit=40),
                "recent_trades": repo.list_recent_closed_positions(conn, limit=20),
                "equity_snapshots": list(reversed(repo.list_equity_snapshots(conn, ACCOUNT_KEY, limit=20))),
            }
        return payload, None
    except Exception as exc:
        return {
            "summary": {},
            "positions": [],
            "orders": [],
            "actions": [],
            "recent_trades": [],
            "equity_snapshots": [],
        }, str(exc)


def _serialize_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(summary)
    for key in (
        "initial_bankroll",
        "cash_balance",
        "positions_value",
        "realized_pnl",
        "unrealized_pnl",
        "equity",
        "max_equity",
        "drawdown",
        "total_profit",
        "win_rate",
        "total_return_pct",
        "average_closed_pnl",
        "drawdown_pct",
    ):
        if key in metrics:
            metrics[key] = round(float(metrics.get(key, 0) or 0), 4)
    return metrics


@app.route("/")
def index():
    main_data, db_error = _load_main_dashboard_data()
    arb_trades = load_arbitrage_trades()
    arb_metrics = _compute_arbitrage_metrics(arb_trades)
    logs = get_logs(20)

    recent_arb_trades = []
    for trade in arb_trades[-10:]:
        recent_arb_trades.append(
            {
                "time": format_time(trade.get("opened_at", "")),
                "title": trade.get("title", "Unknown"),
                "buy_platform": trade.get("buy_platform", "unknown"),
                "sell_platform": trade.get("sell_platform", "unknown"),
                "buy_price": float(trade.get("buy_price", 0)),
                "sell_price": float(trade.get("sell_price", 0)),
                "spread_percent": float(trade.get("spread_percent", 0)),
                "profit": float(trade.get("realized_pnl", 0)),
            }
        )
    recent_arb_trades.reverse()

    return render_template(
        "dashboard.html",
        summary=main_data["summary"],
        positions=main_data["positions"],
        orders=main_data["orders"],
        actions=main_data["actions"],
        recent_trades=main_data["recent_trades"],
        equity_snapshots=main_data["equity_snapshots"],
        arbitrage_metrics=arb_metrics,
        arbitrage_trades=recent_arb_trades,
        logs=logs,
        db_error=db_error,
    )


@app.route("/api/metrics")
def api_metrics():
    main_data, db_error = _load_main_dashboard_data()
    if db_error:
        return jsonify({"ok": False, "error": db_error}), 503
    return jsonify({"ok": True, "data": _serialize_metrics(main_data["summary"])})


@app.route("/api/positions")
def api_positions():
    main_data, db_error = _load_main_dashboard_data()
    if db_error:
        return jsonify({"ok": False, "error": db_error}), 503
    return jsonify({"ok": True, "data": main_data["positions"]})


@app.route("/api/orders")
def api_orders():
    main_data, db_error = _load_main_dashboard_data()
    if db_error:
        return jsonify({"ok": False, "error": db_error}), 503
    return jsonify({"ok": True, "data": main_data["orders"]})


@app.route("/api/actions")
def api_actions():
    main_data, db_error = _load_main_dashboard_data()
    if db_error:
        return jsonify({"ok": False, "error": db_error}), 503
    return jsonify({"ok": True, "data": main_data["actions"]})


@app.route("/api/recent-events")
def api_recent_events():
    return api_actions()


@app.route("/api/trades")
def api_trades():
    main_data, db_error = _load_main_dashboard_data()
    if db_error:
        return jsonify({"ok": False, "error": db_error}), 503
    return jsonify({"ok": True, "data": main_data["recent_trades"]})


@app.route("/api/arbitrage-trades")
def api_arbitrage_trades():
    trades = load_arbitrage_trades()
    return jsonify({"ok": True, "data": trades[-20:]})


@app.route("/api/logs")
def api_logs():
    return jsonify({"ok": True, "data": get_logs(20)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
