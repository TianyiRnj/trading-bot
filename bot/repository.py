import uuid
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

OPEN_ORDER_STATUSES = ("open", "live", "matched", "partially_filled", "partially_matched")


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def compute_account_totals(
    cash_balance: float,
    positions_value: float,
    max_equity: float,
) -> dict[str, float]:
    normalized_cash = round(float(cash_balance or 0.0), 8)
    normalized_positions = round(max(float(positions_value or 0.0), 0.0), 8)
    equity = round(normalized_cash + normalized_positions, 8)
    updated_max_equity = round(max(float(max_equity or 0.0), equity), 8)
    drawdown = round(max(updated_max_equity - equity, 0.0), 8)
    return {
        "cash_balance": normalized_cash,
        "positions_value": normalized_positions,
        "equity": equity,
        "max_equity": updated_max_equity,
        "drawdown": drawdown,
    }


def _require_account_state(conn: psycopg.Connection, account_key: str) -> dict[str, Any]:
    account = get_account_state(conn, account_key)
    if account is None:
        raise ValueError(f"account_state missing for account_key={account_key}")
    return account


# -- mode_state ----------------------------------------------------------------

def insert_mode_run(
    conn: psycopg.Connection,
    run_label: str,
    requested_mode: str,
    effective_mode: str,
    fallback_reason: str | None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO mode_state (run_label, requested_mode, effective_mode, fallback_reason)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (run_label, requested_mode, effective_mode, fallback_reason),
    ).fetchone()
    return row[0]


def update_mode_heartbeat(conn: psycopg.Connection, mode_run_id: int) -> None:
    conn.execute(
        "UPDATE mode_state SET last_heartbeat_at = NOW() WHERE id = %s",
        (mode_run_id,),
    )


def close_mode_run(conn: psycopg.Connection, mode_run_id: int, reason: str) -> None:
    conn.execute(
        """
        UPDATE mode_state
        SET status = 'stopped', stopped_at = NOW(), last_shutdown_reason = %s
        WHERE id = %s
        """,
        (reason[:500], mode_run_id),
    )


def update_mode_run_state(
    conn: psycopg.Connection,
    mode_run_id: int,
    *,
    effective_mode: str,
    fallback_reason: str | None,
    status: str = "running",
) -> None:
    conn.execute(
        """
        UPDATE mode_state
        SET effective_mode = %s,
            fallback_reason = %s,
            status = %s,
            last_heartbeat_at = NOW()
        WHERE id = %s
        """,
        (effective_mode, fallback_reason, status, mode_run_id),
    )


def get_latest_mode_state(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            id,
            run_label,
            requested_mode,
            effective_mode,
            fallback_reason,
            status,
            started_at,
            stopped_at,
            last_heartbeat_at,
            last_shutdown_reason
        FROM mode_state
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "run_label": row[1],
        "requested_mode": row[2],
        "effective_mode": row[3],
        "fallback_reason": row[4],
        "status": row[5],
        "started_at": _isoformat(row[6]),
        "stopped_at": _isoformat(row[7]),
        "last_heartbeat_at": _isoformat(row[8]),
        "last_shutdown_reason": row[9],
    }


# -- account_state -------------------------------------------------------------

def upsert_account_state(
    conn: psycopg.Connection,
    *,
    account_key: str,
    initial_bankroll: float,
    requested_mode: str,
    effective_mode: str,
) -> None:
    conn.execute(
        """
        INSERT INTO account_state
            (account_key, initial_bankroll, requested_mode, effective_mode,
             cash_balance, positions_value, equity, max_equity)
        VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
        ON CONFLICT (account_key) DO UPDATE SET
            requested_mode = EXCLUDED.requested_mode,
            effective_mode = EXCLUDED.effective_mode,
            updated_at = NOW()
        """,
        (
            account_key,
            initial_bankroll,
            requested_mode,
            effective_mode,
            initial_bankroll,
            initial_bankroll,
            initial_bankroll,
        ),
    )
    record_equity_snapshot(conn, account_key)


def update_account_modes(
    conn: psycopg.Connection,
    account_key: str,
    *,
    requested_mode: str,
    effective_mode: str,
) -> None:
    conn.execute(
        """
        UPDATE account_state
        SET requested_mode = %s,
            effective_mode = %s,
            updated_at = NOW()
        WHERE account_key = %s
        """,
        (requested_mode, effective_mode, account_key),
    )


def debit_account_on_entry(
    conn: psycopg.Connection, account_key: str, amount_usd: float
) -> None:
    account = _require_account_state(conn, account_key)
    totals = compute_account_totals(
        cash_balance=float(account["cash_balance"]) - float(amount_usd),
        positions_value=float(account["positions_value"]) + float(amount_usd),
        max_equity=float(account["max_equity"]),
    )
    conn.execute(
        """
        UPDATE account_state
        SET cash_balance = %(cash_balance)s,
            positions_value = %(positions_value)s,
            equity = %(equity)s,
            max_equity = %(max_equity)s,
            drawdown = %(drawdown)s,
            updated_at = NOW()
        WHERE account_key = %(key)s
        """,
        {**totals, "key": account_key},
    )
    record_equity_snapshot(conn, account_key)


def update_account_market_state(
    conn: psycopg.Connection,
    account_key: str,
    *,
    positions_value: float,
    unrealized_pnl: float,
) -> None:
    account = _require_account_state(conn, account_key)
    totals = compute_account_totals(
        cash_balance=float(account["cash_balance"]),
        positions_value=positions_value,
        max_equity=float(account["max_equity"]),
    )
    conn.execute(
        """
        UPDATE account_state
        SET positions_value = %(positions_value)s,
            unrealized_pnl = %(unrealized_pnl)s,
            equity = %(equity)s,
            max_equity = %(max_equity)s,
            drawdown = %(drawdown)s,
            updated_at = NOW()
        WHERE account_key = %(key)s
        """,
        {
            "key": account_key,
            "positions_value": totals["positions_value"],
            "unrealized_pnl": round(unrealized_pnl, 8),
            "equity": totals["equity"],
            "max_equity": totals["max_equity"],
            "drawdown": totals["drawdown"],
        },
    )
    record_equity_snapshot(conn, account_key)


def credit_account_on_exit(
    conn: psycopg.Connection,
    account_key: str,
    cost_basis_usd: float,
    proceeds_usd: float,
    pnl_usd: float,
) -> None:
    account = _require_account_state(conn, account_key)
    totals = compute_account_totals(
        cash_balance=float(account["cash_balance"]) + float(proceeds_usd),
        positions_value=max(float(account["positions_value"]) - float(cost_basis_usd), 0.0),
        max_equity=float(account["max_equity"]),
    )
    conn.execute(
        """
        UPDATE account_state
        SET cash_balance = %(cash_balance)s,
            positions_value = %(positions_value)s,
            realized_pnl = realized_pnl + %(pnl)s,
            equity = %(equity)s,
            max_equity = %(max_equity)s,
            drawdown = %(drawdown)s,
            updated_at = NOW()
        WHERE account_key = %(key)s
        """,
        {
            "cash_balance": totals["cash_balance"],
            "positions_value": totals["positions_value"],
            "equity": totals["equity"],
            "max_equity": totals["max_equity"],
            "drawdown": totals["drawdown"],
            "pnl": pnl_usd,
            "key": account_key,
        },
    )
    record_equity_snapshot(conn, account_key)


def record_equity_snapshot(conn: psycopg.Connection, account_key: str) -> None:
    conn.execute(
        """
        INSERT INTO equity_snapshots (
            account_key,
            cash_balance,
            positions_value,
            realized_pnl,
            unrealized_pnl,
            equity
        )
        SELECT
            account_key,
            cash_balance,
            positions_value,
            realized_pnl,
            unrealized_pnl,
            equity
        FROM account_state
        WHERE account_key = %s
        """,
        (account_key,),
    )


def get_account_state(conn: psycopg.Connection, account_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            account_key,
            initial_bankroll,
            requested_mode,
            effective_mode,
            cash_balance,
            positions_value,
            realized_pnl,
            unrealized_pnl,
            equity,
            max_equity,
            drawdown,
            updated_at
        FROM account_state
        WHERE account_key = %s
        """,
        (account_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "account_key": row[0],
        "initial_bankroll": float(row[1] or 0),
        "requested_mode": row[2],
        "effective_mode": row[3],
        "cash_balance": float(row[4] or 0),
        "positions_value": float(row[5] or 0),
        "realized_pnl": float(row[6] or 0),
        "unrealized_pnl": float(row[7] or 0),
        "equity": float(row[8] or 0),
        "max_equity": float(row[9] or 0),
        "drawdown": float(row[10] or 0),
        "updated_at": _isoformat(row[11]),
    }


# -- positions -----------------------------------------------------------------

def upsert_position(
    conn: psycopg.Connection,
    position: dict[str, Any],
    *,
    requested_mode: str,
    effective_mode: str,
) -> str:
    if "position_id" not in position:
        position["position_id"] = str(uuid.uuid4())
    pid = position["position_id"]
    conn.execute(
        """
        INSERT INTO positions (
            position_id,
            market_id,
            condition_id,
            token_id,
            title,
            side,
            status,
            requested_mode,
            effective_mode,
            entry_order_id,
            entry_price,
            entry_value_usd,
            shares,
            realized_pnl,
            unrealized_pnl,
            opened_at,
            metadata,
            updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            'open', %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, NOW()
        )
        ON CONFLICT (position_id) DO UPDATE SET
            requested_mode = EXCLUDED.requested_mode,
            effective_mode = EXCLUDED.effective_mode,
            entry_order_id = EXCLUDED.entry_order_id,
            entry_price = EXCLUDED.entry_price,
            entry_value_usd = EXCLUDED.entry_value_usd,
            shares = EXCLUDED.shares,
            realized_pnl = EXCLUDED.realized_pnl,
            unrealized_pnl = EXCLUDED.unrealized_pnl,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        (
            pid,
            position.get("market_id"),
            position.get("condition_id"),
            position.get("token_id"),
            position.get("title"),
            position.get("side"),
            requested_mode,
            effective_mode,
            position.get("entry_order_id"),
            position.get("entry_probability"),
            position.get("size_usd", 0),
            position.get("shares", 0),
            position.get("realized_pnl_usd", 0),
            position.get("unrealized_pnl_usd", 0),
            position.get("opened_at"),
            Jsonb(position),
        ),
    )
    return pid


def close_position_in_db(
    conn: psycopg.Connection, position_id: str | None, realized_pnl: float
) -> None:
    if not position_id:
        return
    conn.execute(
        """
        UPDATE positions
        SET
            status = 'closed',
            closed_at = NOW(),
            shares = 0,
            entry_value_usd = 0,
            unrealized_pnl = 0,
            realized_pnl = %s,
            updated_at = NOW()
        WHERE position_id = %s
        """,
        (realized_pnl, position_id),
    )


def load_open_positions(
    conn: psycopg.Connection,
    effective_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    if effective_mode is None:
        rows = conn.execute(
            "SELECT position_id, market_id, metadata FROM positions WHERE status = 'open'"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT position_id, market_id, metadata
            FROM positions
            WHERE status = 'open' AND effective_mode = %s
            """,
            (effective_mode,),
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for position_id, market_id, metadata in rows:
        entry = {**(metadata or {}), "position_id": position_id}
        result[str(market_id)] = entry
    return result


def list_open_positions(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            position_id,
            market_id,
            title,
            side,
            shares,
            entry_price,
            entry_value_usd,
            realized_pnl,
            unrealized_pnl,
            requested_mode,
            effective_mode,
            opened_at,
            metadata
        FROM positions
        WHERE status = 'open'
        ORDER BY opened_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        metadata = row[12] or {}
        results.append(
            {
                "position_id": row[0],
                "market_id": str(row[1]),
                "title": row[2] or metadata.get("title"),
                "side": row[3],
                "shares": float(row[4] or 0),
                "entry_price": float(row[5] or 0),
                "entry_value_usd": float(row[6] or 0),
                "realized_pnl": float(row[7] or 0),
                "unrealized_pnl": float(row[8] or 0),
                "requested_mode": row[9],
                "effective_mode": row[10],
                "opened_at": _isoformat(row[11]),
                "current_probability": float(metadata.get("current_probability") or 0),
                "current_value_usd": float(metadata.get("current_value_usd") or 0),
                "metadata": metadata,
            }
        )
    return results


def list_recent_closed_positions(conn: psycopg.Connection, limit: int = 25) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            position_id,
            market_id,
            title,
            side,
            entry_price,
            realized_pnl,
            opened_at,
            closed_at,
            requested_mode,
            effective_mode,
            metadata
        FROM positions
        WHERE status = 'closed'
        ORDER BY closed_at DESC NULLS LAST, id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        metadata = row[10] or {}
        results.append(
            {
                "position_id": row[0],
                "market_id": str(row[1]),
                "title": row[2] or metadata.get("title"),
                "side": row[3],
                "entry_price": float(row[4] or 0),
                "realized_pnl": float(row[5] or 0),
                "opened_at": _isoformat(row[6]),
                "closed_at": _isoformat(row[7]),
                "requested_mode": row[8],
                "effective_mode": row[9],
                "metadata": metadata,
            }
        )
    return results


# -- orders --------------------------------------------------------------------

def upsert_order(
    conn: psycopg.Connection,
    *,
    order_id: str,
    side: str,
    execution_mode: str,
    status: str,
    requested_mode: str,
    effective_mode: str,
    position_id: str | None = None,
    market_id: str | None = None,
    condition_id: str | None = None,
    token_id: str | None = None,
    client_order_id: str | None = None,
    venue_order_id: str | None = None,
    parent_order_id: str | None = None,
    requested_price: float | None = None,
    executed_price: float | None = None,
    requested_value_usd: float = 0.0,
    executed_value_usd: float | None = None,
    requested_shares: float = 0.0,
    filled_shares: float = 0.0,
    remaining_shares: float = 0.0,
    fallback_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO orders (
            order_id,
            client_order_id,
            venue_order_id,
            parent_order_id,
            position_id,
            market_id,
            condition_id,
            token_id,
            side,
            execution_mode,
            status,
            requested_mode,
            effective_mode,
            requested_price,
            executed_price,
            requested_value_usd,
            executed_value_usd,
            requested_shares,
            filled_shares,
            remaining_shares,
            fallback_reason,
            created_at,
            closed_at,
            metadata,
            updated_at
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, COALESCE(%s, 0),
            %s, %s, %s,
            %s,
            COALESCE(%s, NOW()),
            CASE
                WHEN %s IN ('filled', 'cancelled', 'canceled', 'expired', 'rejected')
                    THEN NOW()
                ELSE NULL
            END,
            %s,
            NOW()
        )
        ON CONFLICT (order_id) DO UPDATE SET
            client_order_id = COALESCE(EXCLUDED.client_order_id, orders.client_order_id),
            venue_order_id = COALESCE(EXCLUDED.venue_order_id, orders.venue_order_id),
            parent_order_id = COALESCE(EXCLUDED.parent_order_id, orders.parent_order_id),
            position_id = COALESCE(EXCLUDED.position_id, orders.position_id),
            market_id = COALESCE(EXCLUDED.market_id, orders.market_id),
            condition_id = COALESCE(EXCLUDED.condition_id, orders.condition_id),
            token_id = COALESCE(EXCLUDED.token_id, orders.token_id),
            side = EXCLUDED.side,
            execution_mode = EXCLUDED.execution_mode,
            status = EXCLUDED.status,
            requested_mode = EXCLUDED.requested_mode,
            effective_mode = EXCLUDED.effective_mode,
            requested_price = EXCLUDED.requested_price,
            executed_price = COALESCE(EXCLUDED.executed_price, orders.executed_price),
            requested_value_usd = EXCLUDED.requested_value_usd,
            executed_value_usd = COALESCE(%s, orders.executed_value_usd),
            requested_shares = EXCLUDED.requested_shares,
            filled_shares = EXCLUDED.filled_shares,
            remaining_shares = EXCLUDED.remaining_shares,
            fallback_reason = COALESCE(EXCLUDED.fallback_reason, orders.fallback_reason),
            closed_at = CASE
                WHEN EXCLUDED.status IN ('filled', 'cancelled', 'canceled', 'expired', 'rejected')
                    THEN COALESCE(orders.closed_at, NOW())
                ELSE orders.closed_at
            END,
            metadata = EXCLUDED.metadata,
            updated_at = NOW()
        """,
        (
            order_id,
            client_order_id,
            venue_order_id,
            parent_order_id,
            position_id,
            market_id,
            condition_id,
            token_id,
            side,
            execution_mode,
            status,
            requested_mode,
            effective_mode,
            requested_price,
            executed_price,
            requested_value_usd,
            executed_value_usd,
            requested_shares,
            filled_shares,
            remaining_shares,
            fallback_reason,
            created_at,
            status,
            executed_value_usd,
            Jsonb(metadata or {}),
        ),
    )


def upsert_pending_order(
    conn: psycopg.Connection,
    order: dict[str, Any],
    *,
    requested_mode: str,
    effective_mode: str,
) -> None:
    order_id = order["order_id"]
    initial_shares = float(order.get("initial_shares") or 0)
    filled_shares = float(order.get("filled_shares") or max(initial_shares - float(order.get("remaining_shares") or 0), 0.0))
    remaining_shares = float(order.get("remaining_shares") or 0)
    limit_price = float(order.get("limit_price") or 0)
    executed_value_usd = float(order.get("executed_value_usd") or 0)
    upsert_order(
        conn,
        order_id=order_id,
        position_id=order.get("position_id"),
        market_id=order.get("market_id"),
        condition_id=order.get("condition_id"),
        token_id=order.get("token_id"),
        side="SELL",
        execution_mode="quoted_execution",
        status="open",
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        requested_price=limit_price,
        requested_value_usd=round(initial_shares * limit_price, 6),
        requested_shares=initial_shares,
        executed_value_usd=executed_value_usd,
        filled_shares=filled_shares,
        remaining_shares=remaining_shares,
        metadata=order,
        created_at=order.get("created_at"),
    )


def update_order_remaining(
    conn: psycopg.Connection, order_id: str, remaining_shares: float
) -> None:
    conn.execute(
        """
        UPDATE orders
        SET remaining_shares = %s,
            filled_shares = GREATEST(requested_shares - %s, 0),
            status = CASE WHEN %s > 0 THEN status ELSE 'filled' END,
            updated_at = NOW()
        WHERE order_id = %s
        """,
        (remaining_shares, remaining_shares, remaining_shares, order_id),
    )


def close_order_in_db(conn: psycopg.Connection, order_id: str, status: str) -> None:
    conn.execute(
        """
        UPDATE orders
        SET status = %s, closed_at = NOW(), updated_at = NOW()
        WHERE order_id = %s
        """,
        (status, order_id),
    )


def load_pending_orders(
    conn: psycopg.Connection,
    effective_mode: str | None = None,
) -> dict[str, dict[str, Any]]:
    if effective_mode is None:
        rows = conn.execute(
            """
            SELECT order_id, metadata
            FROM orders
            WHERE status = ANY(%s)
            ORDER BY created_at ASC, id ASC
            """,
            (list(OPEN_ORDER_STATUSES),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT order_id, metadata
            FROM orders
            WHERE status = ANY(%s) AND effective_mode = %s
            ORDER BY created_at ASC, id ASC
            """,
            (list(OPEN_ORDER_STATUSES), effective_mode),
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for order_id, metadata in rows:
        entry = {**(metadata or {}), "order_id": order_id}
        result[str(order_id)] = entry
    return result


def list_open_orders(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            order_id,
            client_order_id,
            venue_order_id,
            parent_order_id,
            position_id,
            market_id,
            condition_id,
            token_id,
            side,
            execution_mode,
            status,
            requested_mode,
            effective_mode,
            requested_price,
            executed_price,
            requested_value_usd,
            executed_value_usd,
            requested_shares,
            filled_shares,
            remaining_shares,
            fallback_reason,
            created_at,
            updated_at,
            metadata
        FROM orders
        WHERE status = ANY(%s)
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (list(OPEN_ORDER_STATUSES), limit),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "order_id": row[0],
                "client_order_id": row[1],
                "venue_order_id": row[2],
                "parent_order_id": row[3],
                "position_id": row[4],
                "market_id": str(row[5]) if row[5] is not None else None,
                "condition_id": row[6],
                "token_id": row[7],
                "side": row[8],
                "execution_mode": row[9],
                "status": row[10],
                "requested_mode": row[11],
                "effective_mode": row[12],
                "requested_price": float(row[13] or 0),
                "executed_price": float(row[14] or 0),
                "requested_value_usd": float(row[15] or 0),
                "executed_value_usd": float(row[16] or 0),
                "requested_shares": float(row[17] or 0),
                "filled_shares": float(row[18] or 0),
                "remaining_shares": float(row[19] or 0),
                "fallback_reason": row[20],
                "created_at": _isoformat(row[21]),
                "updated_at": _isoformat(row[22]),
                "metadata": row[23] or {},
            }
        )
    return results


# -- trade_events --------------------------------------------------------------

def insert_trade_event(
    conn: psycopg.Connection,
    *,
    action_type: str,
    order_id: str | None = None,
    client_order_id: str | None = None,
    venue_order_id: str | None = None,
    parent_order_id: str | None = None,
    position_id: str | None = None,
    market_id: str | None = None,
    condition_id: str | None = None,
    token_id: str | None = None,
    status: str | None = None,
    requested_mode: str | None = None,
    effective_mode: str | None = None,
    execution_mode: str | None = None,
    requested_price: float | None = None,
    executed_price: float | None = None,
    requested_value_usd: float = 0.0,
    executed_value_usd: float = 0.0,
    requested_shares: float = 0.0,
    filled_shares: float = 0.0,
    remaining_shares: float = 0.0,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    event_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO trade_events (
            event_id, order_id, client_order_id, venue_order_id, parent_order_id,
            position_id, market_id, condition_id, token_id,
            action_type, status,
            requested_mode, effective_mode, execution_mode,
            requested_price, executed_price,
            requested_value_usd, executed_value_usd,
            requested_shares, filled_shares, remaining_shares,
            reason, metadata
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s
        )
        """,
        (
            event_id,
            order_id,
            client_order_id,
            venue_order_id,
            parent_order_id,
            position_id,
            market_id,
            condition_id,
            token_id,
            action_type,
            status,
            requested_mode,
            effective_mode,
            execution_mode,
            requested_price,
            executed_price,
            requested_value_usd,
            executed_value_usd,
            requested_shares,
            filled_shares,
            remaining_shares,
            reason,
            Jsonb(metadata or {}),
        ),
    )
    return event_id


def list_recent_trade_events(conn: psycopg.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            event_id,
            order_id,
            client_order_id,
            venue_order_id,
            parent_order_id,
            position_id,
            market_id,
            condition_id,
            token_id,
            action_type,
            status,
            requested_mode,
            effective_mode,
            execution_mode,
            requested_price,
            executed_price,
            requested_value_usd,
            executed_value_usd,
            requested_shares,
            filled_shares,
            remaining_shares,
            reason,
            metadata,
            created_at
        FROM trade_events
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "event_id": row[0],
                "order_id": row[1],
                "client_order_id": row[2],
                "venue_order_id": row[3],
                "parent_order_id": row[4],
                "position_id": row[5],
                "market_id": str(row[6]) if row[6] is not None else None,
                "condition_id": row[7],
                "token_id": row[8],
                "action_type": row[9],
                "status": row[10],
                "requested_mode": row[11],
                "effective_mode": row[12],
                "execution_mode": row[13],
                "requested_price": float(row[14] or 0),
                "executed_price": float(row[15] or 0),
                "requested_value_usd": float(row[16] or 0),
                "executed_value_usd": float(row[17] or 0),
                "requested_shares": float(row[18] or 0),
                "filled_shares": float(row[19] or 0),
                "remaining_shares": float(row[20] or 0),
                "reason": row[21],
                "metadata": row[22] or {},
                "created_at": _isoformat(row[23]),
            }
        )
    return results


# -- seen_events ---------------------------------------------------------------

def insert_seen_event(conn: psycopg.Connection, source_event_id: str) -> None:
    conn.execute(
        "INSERT INTO seen_events (source_event_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (source_event_id,),
    )


def load_seen_events(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT source_event_id FROM seen_events").fetchall()
    return {row[0] for row in rows}


def list_equity_snapshots(
    conn: psycopg.Connection, account_key: str, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT cash_balance, positions_value, realized_pnl, unrealized_pnl, equity, captured_at
        FROM equity_snapshots
        WHERE account_key = %s
        ORDER BY captured_at DESC, id DESC
        LIMIT %s
        """,
        (account_key, limit),
    ).fetchall()
    return [
        {
            "cash_balance": float(row[0] or 0),
            "positions_value": float(row[1] or 0),
            "realized_pnl": float(row[2] or 0),
            "unrealized_pnl": float(row[3] or 0),
            "equity": float(row[4] or 0),
            "captured_at": _isoformat(row[5]),
        }
        for row in rows
    ]


# -- helper --------------------------------------------------------------------

def has_live_exposure(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM positions WHERE status = 'open' AND effective_mode = 'live'
        UNION ALL
        SELECT 1 FROM orders WHERE status = ANY(%s) AND effective_mode = 'live'
        LIMIT 1
        """,
        (list(OPEN_ORDER_STATUSES),),
    ).fetchone()
    return row is not None


def get_dashboard_summary(conn: psycopg.Connection, account_key: str) -> dict[str, Any]:
    account = get_account_state(conn, account_key) or {
        "account_key": account_key,
        "initial_bankroll": 0.0,
        "requested_mode": "paper",
        "effective_mode": "paper",
        "cash_balance": 0.0,
        "positions_value": 0.0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "equity": 0.0,
        "max_equity": 0.0,
        "drawdown": 0.0,
        "updated_at": None,
    }
    latest_mode = get_latest_mode_state(conn)
    counts = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM positions WHERE status = 'open'),
            (SELECT COUNT(*) FROM orders WHERE status = ANY(%s)),
            (SELECT COUNT(*) FROM orders WHERE status = ANY(%s) AND execution_mode = 'quoted_execution'),
            (SELECT COUNT(*) FROM trade_events),
            (SELECT COUNT(*) FROM positions WHERE status = 'closed'),
            (SELECT COUNT(*) FROM positions WHERE status = 'closed' AND realized_pnl > 0),
            (SELECT AVG(realized_pnl) FROM positions WHERE status = 'closed')
        """,
        (list(OPEN_ORDER_STATUSES), list(OPEN_ORDER_STATUSES)),
    ).fetchone()
    closed_positions = int(counts[4] or 0)
    winning_positions = int(counts[5] or 0)
    average_closed_pnl = float(counts[6] or 0)
    win_rate = round((winning_positions / closed_positions) * 100, 1) if closed_positions else 0.0
    total_profit = float(account["realized_pnl"]) + float(account["unrealized_pnl"])
    initial_bankroll = float(account["initial_bankroll"] or 0)
    max_equity = float(account["max_equity"] or 0)
    drawdown = float(account["drawdown"] or 0)
    drawdown_pct = round((drawdown / max_equity) * 100, 2) if max_equity > 0 else 0.0
    total_return_pct = round((total_profit / initial_bankroll) * 100, 2) if initial_bankroll > 0 else 0.0
    return {
        **account,
        "total_profit": total_profit,
        "total_return_pct": total_return_pct,
        "open_positions": int(counts[0] or 0),
        "open_orders": int(counts[1] or 0),
        "pending_orders": int(counts[2] or 0),
        "trade_event_count": int(counts[3] or 0),
        "closed_positions": closed_positions,
        "winning_positions": winning_positions,
        "win_rate": win_rate,
        "average_closed_pnl": average_closed_pnl,
        "drawdown_pct": drawdown_pct,
        "mode": latest_mode,
    }
