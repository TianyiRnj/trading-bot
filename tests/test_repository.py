import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot import repository


class _RecordingCursor:
    def fetchone(self):
        return None


class _RecordingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        return _RecordingCursor()


def test_compute_account_totals_uses_mark_to_market_equity():
    totals = repository.compute_account_totals(
        cash_balance=40.0,
        positions_value=65.0,
        max_equity=105.0,
    )

    assert totals["cash_balance"] == 40.0
    assert totals["positions_value"] == 65.0
    assert totals["equity"] == 105.0
    assert totals["max_equity"] == 105.0
    assert totals["drawdown"] == 0.0


def test_upsert_order_preserves_executed_value_when_omitted():
    conn = _RecordingConn()

    repository.upsert_order(
        conn,
        order_id="order-1",
        side="SELL",
        execution_mode="quoted_execution",
        status="open",
        requested_mode="paper",
        effective_mode="paper",
        requested_shares=10.0,
        filled_shares=4.0,
        remaining_shares=6.0,
        executed_value_usd=None,
    )

    sql, params = conn.calls[0]
    assert "executed_value_usd = COALESCE(%s, orders.executed_value_usd)" in sql
    # The final positional arg corresponds to the ON CONFLICT executed_value_usd
    # COALESCE placeholder; passing None preserves the existing DB value.
    assert params[-1] is None


def test_upsert_order_passes_metadata_jsonb_and_executed_value_in_correct_slots():
    # Regression for an args-tuple ordering bug where the metadata JSONB and the
    # ON CONFLICT executed_value_usd numeric were swapped, causing every order
    # write to fail with "column metadata is of type jsonb but expression is of
    # type double precision" (and aborting the whole transaction with it).
    from psycopg.types.json import Jsonb

    conn = _RecordingConn()

    repository.upsert_order(
        conn,
        order_id="order-1",
        side="BUY",
        execution_mode="direct_execution",
        status="filled",
        requested_mode="paper",
        effective_mode="paper",
        executed_value_usd=12.34,
        metadata={"signal_type": "tweet", "urgency": "high"},
    )

    _, params = conn.calls[0]
    # Second-to-last arg fills the INSERT VALUES metadata (jsonb) column.
    assert isinstance(params[-2], Jsonb)
    # Last arg fills the ON CONFLICT executed_value_usd COALESCE placeholder.
    assert params[-1] == 12.34
