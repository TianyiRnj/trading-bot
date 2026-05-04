CREATE TABLE IF NOT EXISTS mode_state (
  id BIGSERIAL PRIMARY KEY,
  run_label TEXT NOT NULL DEFAULT 'default',
  requested_mode TEXT NOT NULL CHECK (requested_mode IN ('paper', 'live')),
  effective_mode TEXT NOT NULL CHECK (effective_mode IN ('paper', 'live')),
  fallback_reason TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  stopped_at TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_shutdown_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_mode_state_started_at
  ON mode_state (started_at DESC);

CREATE TABLE IF NOT EXISTS account_state (
  account_key TEXT PRIMARY KEY,
  requested_mode TEXT NOT NULL CHECK (requested_mode IN ('paper', 'live')),
  effective_mode TEXT NOT NULL CHECK (effective_mode IN ('paper', 'live')),
  cash_balance NUMERIC(20, 8) NOT NULL DEFAULT 0,
  positions_value NUMERIC(20, 8) NOT NULL DEFAULT 0,
  realized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
  unrealized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
  equity NUMERIC(20, 8) NOT NULL DEFAULT 0,
  max_equity NUMERIC(20, 8) NOT NULL DEFAULT 0,
  drawdown NUMERIC(20, 8) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS positions (
  id BIGSERIAL PRIMARY KEY,
  position_id TEXT NOT NULL UNIQUE,
  market_id TEXT NOT NULL,
  condition_id TEXT,
  token_id TEXT NOT NULL,
  title TEXT,
  side TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
  status TEXT NOT NULL DEFAULT 'open',
  requested_mode TEXT NOT NULL CHECK (requested_mode IN ('paper', 'live')),
  effective_mode TEXT NOT NULL CHECK (effective_mode IN ('paper', 'live')),
  entry_order_id TEXT,
  entry_price NUMERIC(12, 6),
  entry_value_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
  shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  realized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
  unrealized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
  opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_positions_market_id
  ON positions (market_id);

CREATE INDEX IF NOT EXISTS idx_positions_status
  ON positions (status);

CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  order_id TEXT NOT NULL UNIQUE,
  parent_order_id TEXT,
  position_id TEXT REFERENCES positions(position_id) ON DELETE SET NULL,
  market_id TEXT NOT NULL,
  condition_id TEXT,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
  execution_mode TEXT NOT NULL CHECK (execution_mode IN ('direct_execution', 'quoted_execution')),
  status TEXT NOT NULL,
  requested_mode TEXT NOT NULL CHECK (requested_mode IN ('paper', 'live')),
  effective_mode TEXT NOT NULL CHECK (effective_mode IN ('paper', 'live')),
  requested_price NUMERIC(12, 6),
  executed_price NUMERIC(12, 6),
  requested_value_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
  executed_value_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
  requested_shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  filled_shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  remaining_shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  fallback_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  closed_at TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_orders_market_id
  ON orders (market_id);

CREATE INDEX IF NOT EXISTS idx_orders_status
  ON orders (status);

CREATE INDEX IF NOT EXISTS idx_orders_created_at
  ON orders (created_at DESC);

CREATE TABLE IF NOT EXISTS trade_events (
  id BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  order_id TEXT REFERENCES orders(order_id) ON DELETE SET NULL,
  position_id TEXT REFERENCES positions(position_id) ON DELETE SET NULL,
  market_id TEXT,
  condition_id TEXT,
  token_id TEXT,
  action_type TEXT NOT NULL,
  status TEXT,
  requested_mode TEXT CHECK (requested_mode IN ('paper', 'live')),
  effective_mode TEXT CHECK (effective_mode IN ('paper', 'live')),
  execution_mode TEXT CHECK (execution_mode IN ('direct_execution', 'quoted_execution')),
  requested_price NUMERIC(12, 6),
  executed_price NUMERIC(12, 6),
  requested_value_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
  executed_value_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
  requested_shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  filled_shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  remaining_shares NUMERIC(20, 8) NOT NULL DEFAULT 0,
  reason TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_events_order_id
  ON trade_events (order_id);

CREATE INDEX IF NOT EXISTS idx_trade_events_action_type
  ON trade_events (action_type);

CREATE INDEX IF NOT EXISTS idx_trade_events_created_at
  ON trade_events (created_at DESC);

CREATE TABLE IF NOT EXISTS seen_events (
  source_event_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL DEFAULT 'musashi_feed',
  signal_type TEXT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
  id BIGSERIAL PRIMARY KEY,
  account_key TEXT NOT NULL REFERENCES account_state(account_key) ON DELETE CASCADE,
  cash_balance NUMERIC(20, 8) NOT NULL DEFAULT 0,
  positions_value NUMERIC(20, 8) NOT NULL DEFAULT 0,
  realized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
  unrealized_pnl NUMERIC(20, 8) NOT NULL DEFAULT 0,
  equity NUMERIC(20, 8) NOT NULL DEFAULT 0,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_equity_snapshots_captured_at
  ON equity_snapshots (captured_at DESC);
