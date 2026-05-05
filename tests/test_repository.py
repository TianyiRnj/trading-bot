import os
import sys
import unittest

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


class TestRepositoryLogic(unittest.TestCase):
    def test_compute_account_totals_uses_mark_to_market_equity(self):
        totals = repository.compute_account_totals(
            cash_balance=40.0,
            positions_value=65.0,
            max_equity=105.0,
        )

        self.assertEqual(totals["cash_balance"], 40.0)
        self.assertEqual(totals["positions_value"], 65.0)
        self.assertEqual(totals["equity"], 105.0)
        self.assertEqual(totals["max_equity"], 105.0)
        self.assertEqual(totals["drawdown"], 0.0)

    def test_upsert_order_preserves_executed_value_when_omitted(self):
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
        self.assertIn("executed_value_usd = COALESCE(%s, orders.executed_value_usd)", sql)
        self.assertIsNone(params[-2])


if __name__ == "__main__":
    unittest.main()
