import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import dashboard
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local env
    dashboard = None
    _DASHBOARD_IMPORT_ERROR = exc
else:
    _DASHBOARD_IMPORT_ERROR = None


pytestmark = pytest.mark.skipif(
    dashboard is None,
    reason=f"dashboard import unavailable: {_DASHBOARD_IMPORT_ERROR}",
)


def _sample_main_payload():
    return {
        "summary": {
            "account_key": "main",
            "requested_mode": "paper",
            "effective_mode": "paper",
            "initial_bankroll": 10.0,
            "cash_balance": 7.5,
            "positions_value": 2.5,
            "realized_pnl": 0.6,
            "unrealized_pnl": 0.2,
            "equity": 10.6,
            "max_equity": 10.8,
            "drawdown": 0.2,
            "drawdown_pct": 1.85,
            "total_profit": 0.8,
            "total_return_pct": 8.0,
            "open_positions": 1,
            "open_orders": 1,
            "pending_orders": 1,
            "trade_event_count": 4,
            "closed_positions": 1,
            "winning_positions": 1,
            "win_rate": 100.0,
            "average_closed_pnl": 0.6,
            "updated_at": "2026-05-04T18:30:00+00:00",
            "mode": {
                "run_label": "default",
                "status": "running",
                "fallback_reason": None,
            },
        },
        "positions": [
            {
                "position_id": "pos-1",
                "market_id": "market-1",
                "title": "Will example happen?",
                "side": "YES",
                "shares": 4.0,
                "entry_price": 0.62,
                "entry_value_usd": 2.48,
                "current_probability": 0.67,
                "current_value_usd": 2.68,
                "unrealized_pnl": 0.2,
                "opened_at": "2026-05-04T18:00:00+00:00",
            }
        ],
        "orders": [
            {
                "order_id": "order-1",
                "status": "open",
                "execution_mode": "quoted_execution",
                "remaining_shares": 2.0,
                "requested_value_usd": 1.2,
            }
        ],
        "actions": [
            {
                "event_id": "evt-1",
                "action_type": "buy_filled",
                "order_id": "order-1",
                "status": "filled",
                "executed_value_usd": 2.48,
                "requested_value_usd": 2.5,
                "created_at": "2026-05-04T18:01:00+00:00",
            }
        ],
        "recent_trades": [
            {
                "position_id": "pos-0",
                "title": "Closed example",
                "side": "YES",
                "realized_pnl": 0.6,
                "closed_at": "2026-05-04T17:45:00+00:00",
            }
        ],
        "equity_snapshots": [
            {
                "equity": 10.0,
                "cash_balance": 10.0,
                "positions_value": 0.0,
                "captured_at": "2026-05-04T17:30:00+00:00",
            },
            {
                "equity": 10.6,
                "cash_balance": 7.5,
                "positions_value": 2.5,
                "captured_at": "2026-05-04T18:30:00+00:00",
            },
        ],
    }


@pytest.fixture()
def dashboard_client():
    dashboard_mod = importlib.import_module("dashboard")
    return dashboard_mod.app.test_client()


@patch("dashboard._load_main_dashboard_data")
def test_metrics_endpoint_returns_summary(mock_loader, dashboard_client):
    mock_loader.return_value = (_sample_main_payload(), None)

    response = dashboard_client.get("/api/metrics")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["requested_mode"] == "paper"
    assert payload["data"]["open_positions"] == 1
    assert payload["data"]["equity"] == 10.6
    assert payload["data"]["total_return_pct"] == 8.0
    assert payload["data"]["drawdown_pct"] == 1.85


@patch("dashboard._load_main_dashboard_data")
def test_actions_endpoint_returns_service_unavailable_on_db_error(mock_loader, dashboard_client):
    mock_loader.return_value = (
        {
            "summary": {},
            "positions": [],
            "orders": [],
            "actions": [],
            "recent_trades": [],
            "equity_snapshots": [],
        },
        "db unavailable",
    )

    response = dashboard_client.get("/api/actions")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["ok"] is False
    assert "db unavailable" in payload["error"]


@patch("dashboard._load_main_dashboard_data")
def test_recent_events_endpoint_aliases_actions(mock_loader, dashboard_client):
    mock_loader.return_value = (_sample_main_payload(), None)

    response = dashboard_client.get("/api/recent-events")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"][0]["action_type"] == "buy_filled"


@patch("dashboard.load_arbitrage_trades")
@patch("dashboard._load_main_dashboard_data")
def test_dashboard_state_endpoint_returns_combined_payload(mock_loader, mock_arb, dashboard_client):
    mock_loader.return_value = (_sample_main_payload(), None)
    mock_arb.return_value = [
        {
            "opened_at": "2026-05-04T18:05:00+00:00",
            "title": "Arb example",
            "buy_platform": "Polymarket",
            "sell_platform": "Kalshi",
            "buy_price": 0.48,
            "sell_price": 0.51,
            "spread_percent": 0.03,
            "realized_pnl": 1.25,
        }
    ]

    response = dashboard_client.get("/api/dashboard-state")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["summary"]["requested_mode"] == "paper"
    assert payload["data"]["positions"][0]["position_id"] == "pos-1"
    assert payload["data"]["arbitrage_trades"][0]["title"] == "Arb example"


@patch("dashboard._load_main_dashboard_data")
def test_dashboard_state_endpoint_keeps_200_on_db_error(mock_loader, dashboard_client):
    mock_loader.return_value = (
        {
            "summary": {},
            "positions": [],
            "orders": [],
            "actions": [],
            "recent_trades": [],
            "equity_snapshots": [],
        },
        "db unavailable",
    )

    response = dashboard_client.get("/api/dashboard-state")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["data"]["db_error"] == "db unavailable"


@patch("dashboard.get_logs")
def test_log_stream_bootstrap_current_skips_existing_lines(mock_logs, dashboard_client):
    mock_logs.return_value = ["line 1", "line 2", "line 3"]

    response = dashboard_client.get("/api/log-stream?bootstrap=current")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["data"]["lines"] == []
    assert payload["data"]["next_offset"] == 3
    assert payload["data"]["reset"] is False


@patch("dashboard.get_logs")
def test_log_stream_returns_incremental_lines(mock_logs, dashboard_client):
    mock_logs.return_value = ["line 1", "line 2", "line 3"]

    response = dashboard_client.get("/api/log-stream?offset=1")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["data"]["lines"] == ["line 2", "line 3"]
    assert payload["data"]["next_offset"] == 3
    assert payload["data"]["reset"] is False


@patch("dashboard.get_logs")
@patch("dashboard.load_arbitrage_trades")
@patch("dashboard._load_main_dashboard_data")
def test_index_renders_main_strategy_sections(mock_loader, mock_arb, mock_logs, dashboard_client):
    mock_loader.return_value = (_sample_main_payload(), None)
    mock_arb.return_value = []
    mock_logs.return_value = ["example log line"]

    response = dashboard_client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Musashi Main Strategy" in html
    assert "Open Positions" in html
    assert "Action Timeline" in html
    assert "Arbitrage Sidecar" in html
    assert "Total PnL" in html
    assert "Drawdown" in html
