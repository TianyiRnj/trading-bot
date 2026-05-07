# Minimal Musashi x Polymarket Bot

This repository contains a minimal automated trading bot that listens to Musashi signals and routes selected opportunities to Polymarket. The goal is to provide a small, runnable baseline that is easy to understand, configure, and extend, while now keeping its main-strategy runtime state and account model in Postgres.

At a high level, the bot polls the Musashi `feed`, re-scores incoming text with `analyze-text`, filters for higher-confidence Polymarket opportunities, resolves the corresponding `token_id`, and then manages positions in either `paper` or `live` mode. In addition to opening trades, it can automatically take profit, stop loss, close positions after a maximum hold time, and exit when the signal reverses.

To make live operation more practical, the bot also tracks real fills, preserves exact share counts, handles partially filled exits, polls pending exit orders, cancels timed-out exit orders, and can re-list any remaining size. It also avoids submitting duplicate exit orders for a position that already has a pending exit order. When the bot starts, it can check the Polymarket geoblock endpoint, reconcile stale or inconsistent order state, and restore the latest account, position, and pending-order context from Postgres before trading begins.

## Current Strategy

The current main strategy is a Musashi-signal-driven Polymarket executor with explicit entry filters, position sizing rules, exit logic, and live-safety controls.

- Signal intake: the main loop fetches medium-and-higher-urgency Musashi feed items, re-analyzes each text with `analyze-text`, and only considers signals that still produce a concrete `YES` or `NO` action with sufficient confidence and edge.
- Entry filters: by default, the bot requires `confidence >= 0.76`, `edge >= 0.05`, a Polymarket venue match, `volume24h >= 20000`, and a tradable probability between `0.08` and `0.85`.
- Market selection: if multiple candidate markets match the same signal, the bot ranks them by a composite score based on Musashi confidence, estimated edge, and market-match confidence, then trades only the top candidate.
- Position sizing: the bot caps exposure with `BOT_MAX_POSITION_USD`, `BOT_MAX_TOTAL_EXPOSURE_USD`, and the remaining bankroll. Stronger signals receive a larger position tier, and `BOT_LIMIT_ONE_POSITION_PER_EVENT=true` prevents stacking multiple positions on the same event by default.
- Liquidity gate: before entering, the bot checks whether the target market has usable buy-side liquidity. If the order book is temporarily empty, it skips the entry without permanently consuming the signal so the trade can be retried later.
- Exit rules: open positions are monitored for take profit, stop loss, maximum hold time, and optional signal reversal. In live mode, exit orders can remain pending, partially fill, time out, be canceled, and be re-priced until the remaining shares are resolved.
- Safety behavior: if live trading becomes unavailable but the bot has no real live exposure, it automatically falls back to `paper`. If real live exposure already exists, it enters protection mode, pauses new entries, and focuses on getting flat before switching modes.

The optional arbitrage scanner is not the main strategy. It is a separate simulation-only sidecar that scans Polymarket versus Kalshi spreads and writes paper trade records to `bot/data/arbitrage_trades.jsonl`.

## Important Notice

This bot does not guarantee profits, and it should not be treated as a strategy for maximizing returns. It is a lightweight automation tool for signal intake and order execution, not a promise of performance.

The default exit settings are intentionally simple:

- Take profit: `18%`
- Stop loss: `10%`
- Maximum hold time: `240` minutes
- Exit on signal reversal: enabled
- Exit order timeout before re-pricing: `120` seconds

Because those defaults affect risk management from the moment the bot starts, it is worth reviewing them before you run the bot with real funds.

## Installation

Before running the bot, create a virtual environment, install the dependencies, and copy the sample environment file so you have a place to store your configuration.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Once the environment is ready, you can move directly into the minimum startup flow.

If the repository has been moved or renamed since `.venv` was created, the virtual environment scripts may still point at the old path. In that case, rebuild it before installing dependencies:

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Postgres Setup

The repository uses Postgres as its primary storage layer for the main trading strategy. The following files are required:

- `docker-compose.yml` — runs a local Postgres 16 container
- `db/init/001_init.sql` — creates all tables and indexes on a fresh volume
- `bot/db.py` — connection pool module used by the bot and dashboard
- Postgres environment variables in `.env.example`

Copy the Postgres variables from `.env.example` into `.env` before starting:

```bash
DATABASE_URL=postgresql://musashi:musashi_dev_password@127.0.0.1:5432/musashi_trading_bot
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=musashi_trading_bot
POSTGRES_USER=musashi
POSTGRES_PASSWORD=musashi_dev_password
POSTGRES_SSLMODE=disable
POSTGRES_POOL_MIN=1
POSTGRES_POOL_MAX=5
POSTGRES_CONNECT_TIMEOUT_SECONDS=10
```

Start a local Postgres instance from the repository root:

```bash
docker compose up -d postgres
docker compose ps   # wait until status is "healthy"
```

To verify the connection from Python:

```bash
python3 -c "from bot.db import check_db_available; print(check_db_available())"
# Expected: (True, None)
```

To verify that the required main-strategy tables already exist:

```bash
python3 -c "from bot.db import check_db_schema_ready; print(check_db_schema_ready())"
# Expected: (True, None)
```

The bot and dashboard now refuse to start their Postgres-backed main-strategy views if the required tables are missing, so make sure `db/init/001_init.sql` has already been applied before launching them.

> **Volume reset warning:** `db/init/001_init.sql` only runs automatically on a **fresh** Postgres volume. If you already have a `postgres-data` volume from a previous run and need to apply schema changes, you must destroy and recreate the volume. Only do this against a **disposable local instance** — it permanently deletes all data in that volume:
>
> ```bash
> docker compose down -v   # destroys postgres-data volume
> docker compose up -d postgres
> ```
>
> If preserving an existing local database matters, spin up a separate Compose project or apply the schema changes manually instead of resetting the volume.

## Quick Start

If you want the shortest path to a working local run, use the commands below from the repository root:

```bash
cd /Musashi/trading-bot
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
docker compose up -d postgres
python3 app.py
```

That starts the local database first and then launches the unified local app with whatever values are currently defined in `.env`. The Flask dashboard stays in the foreground, and the bot starts automatically in a background thread.

## Configuration

Before launching the bot in earnest, open `.env` and decide whether you want to run in paper mode or live mode. Starting in paper mode is the safest way to confirm that signal selection, logging, and position handling behave as expected.

If you plan to use the built-in Postgres runtime, make sure `DATABASE_URL` and the `POSTGRES_*` values in `.env` match the database you want to run against.

Two optional runtime flags are also available:

- `BOT_PAPER_GEO_STRICT=false` keeps paper mode geolocation checks advisory instead of fatal
- `BOT_RUN_LABEL=default` tags each runtime row written to `mode_state`

### Paper Trading

To stay in paper mode, keep the following setting:

```bash
BOT_MODE=paper
```

If Postgres still contains open `live` positions or active `live` orders from an earlier session, the bot now refuses to start in paper mode until that exposure is reconciled manually. This prevents the paper trader from fabricating fills against real live state.

Once you are comfortable with the bot's behavior in simulation, you can switch to live trading by filling in the required Polymarket credentials.

### Live Trading

To place real orders, update `.env` with values like these:

```bash
BOT_MODE=live
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_SIGNATURE_TYPE=2
POLYMARKET_FUNDER=0x...
```

`POLYMARKET_SIGNATURE_TYPE` follows the official convention:

- `0`: EOA
- `1`: POLY_PROXY
- `2`: GNOSIS_SAFE

For many Polymarket web accounts, the `FUNDER` should be the proxy wallet address, and the signature type is often `2`. If you are using a standard EOA or MetaMask wallet, the official `py-clob-client` flow may also require token allowances to be set before live orders will succeed.

This project does not automate VPN usage, browser login, or region-bypass behavior. It only reads signals and submits orders. As a result, you are responsible for confirming that your use of the bot complies with local laws, platform rules, and any restrictions that apply to your account or location.

If you request `BOT_MODE=live` but the bot cannot safely operate live, it now behaves in one of two ways:

- If there is no real live exposure yet, it automatically falls back to `paper` mode and keeps running
- If live exposure already exists, it stays out of simulated replacement trading, enters a protection flow, and pauses new entries until the live account is flat

### Risk Management Settings

After you choose the trading mode, review the exit settings so the bot manages positions the way you expect:

```bash
BOT_TAKE_PROFIT_PCT=0.18
BOT_STOP_LOSS_PCT=0.10
BOT_MAX_HOLD_MINUTES=240
BOT_EXIT_ON_SIGNAL_REVERSAL=true
BOT_EXIT_ORDER_TIMEOUT_SECONDS=120
BOT_EXIT_ORDER_REPRICE=true
BOT_STARTUP_RECONCILE=true
```

These values control how the bot responds after a position is opened:

- `BOT_TAKE_PROFIT_PCT=0.18` closes a position after an `18%` favorable move relative to the entry probability
- `BOT_STOP_LOSS_PCT=0.10` closes a position after a `10%` adverse move relative to the entry probability
- `BOT_MAX_HOLD_MINUTES` forces an exit when the holding limit is reached
- `BOT_EXIT_ON_SIGNAL_REVERSAL=true` exits when a later Musashi analysis flips the position direction
- `BOT_EXIT_ORDER_TIMEOUT_SECONDS` sets how long the bot waits before treating an exit order as stale
- `BOT_EXIT_ORDER_REPRICE=true` cancels the old exit order, records the timeout/cancel/reprice event chain, and re-lists the remaining shares at an updated price
- `BOT_STARTUP_RECONCILE=true` performs a geoblock check and reconciles local order state when the bot starts

With the configuration in place, you are ready to run the local app normally.

## Running Locally

Start the unified app from the project root with:

```bash
python3 app.py
```

The dashboard is available at `http://127.0.0.1:5000`. As the app runs, the bot writes its main-strategy account state, positions, orders, trade events, seen events, and equity snapshots to Postgres. Text logs still go to `bot/logs/bot.log`.

The account model is now mark-to-market:

- `cash_balance` decreases on entry fills and increases on exit fills
- `positions_value` tracks the current marked value of open positions
- `unrealized_pnl` tracks open-position movement relative to remaining cost basis
- `equity` tracks `cash_balance + positions_value`
- `max_equity` and `drawdown` are updated as the bot records new marks

## Arbitrage Scanner (Optional, Simulation Only)

The repository includes a cross-platform spread scanner that compares YES prices between Polymarket and Kalshi. It is **disabled by default** and runs in **simulation only** — no real Kalshi orders are ever placed.

To enable it, set the following in `.env`:

```bash
BOT_ENABLE_ARBITRAGE=true
```

When enabled, the scanner runs as a background thread alongside the main Musashi signal loop and writes simulated trade records to `bot/data/arbitrage_trades.jsonl`.

## Main Strategy Dashboard (Flask)

A lightweight Flask dashboard displays the main strategy from Postgres and keeps the arbitrage simulation as a clearly labeled side panel.

Install the dashboard dependency if you have not already:

```bash
pip install -r requirements.txt  # includes flask>=3.0.0 and psycopg[binary,pool]
```

Run from the project root after Postgres is up:

```bash
python3 app.py
```

That starts the dashboard and the bot together. The dashboard is available at `http://127.0.0.1:5000`.

Set `HOST` and `PORT` env vars to override the listen address:

```bash
HOST=0.0.0.0 PORT=8080 python3 app.py
```

The main strategy page reads:

- account metrics from `account_state`
- current positions from `positions`
- active orders from `orders`
- action timeline rows from `trade_events`

The dashboard now refreshes its sections in place every `3` seconds instead of reloading the whole page. The bot log panel is session-local: it starts empty on page load, appends only new lines that appear after you opened the page, keeps growing without a hard UI cap, and clears again when you refresh the browser tab.

The JSON API also exposes:

- `/healthz`
- `/api/dashboard-state`
- `/api/metrics`
- `/api/positions`
- `/api/orders`
- `/api/actions`
- `/api/recent-events`
- `/api/trades`
- `/api/logs`
- `/api/log-stream`

If `BOT_ENABLE_ARBITRAGE=true`, the page also shows the existing arbitrage simulation side panel sourced from `bot/data/arbitrage_trades.jsonl`.

## Runtime Storage

The main strategy writes its runtime state to Postgres tables:

- `account_state`
- `positions`
- `orders`
- `trade_events`
- `seen_events`
- `mode_state`
- `equity_snapshots`

The `trade_events` stream now includes the main order-lifecycle actions used by the dashboard and replay tooling, including:

- `buy_submitted`
- `buy_filled`
- `quote_submitted`
- `sell_submitted`
- `pending`
- `partial_fill`
- `cancel_requested`
- `canceled`
- `reprice_requested`
- `repriced`
- `amount_modified`
- `quote_expired`
- `rejected`
- `position_closed`

Local files are now limited to:

- `bot/logs/bot.log` for text logs
- `bot/data/arbitrage_trades.jsonl` for the optional arbitrage simulation sidecar

The Postgres bootstrap files remain:

- `docker-compose.yml`
- `db/init/001_init.sql`

## Running in the Background

If you want the local app to keep running after you close the terminal, start it with `nohup`:

```bash
nohup ./.venv/bin/python app.py >> bot/logs/nohup.log 2>&1 &
```

That background process uses the virtual environment directly, which helps ensure the dashboard and bot keep running with the same Python interpreter and installed dependencies you used during setup.

## Stopping the Bot and Deactivating the Environment

When you are done, the exact shutdown step depends on how you started the app. If the app is running in the current terminal, press `Ctrl+C` to stop the process cleanly and return to the shell.

If you started the app in the background with `nohup`, find the process and terminate it explicitly:

```bash
ps aux | grep "app.py"
kill <PID>
```

If the process does not stop after a normal `kill`, you can force termination with:

```bash
kill -9 <PID>
```

`Ctrl+C`, normal process exit, and `kill <PID>` (`SIGTERM`) give the bot thread a chance to flush its latest Postgres-backed runtime state before shutdown. `kill -9` (`SIGKILL`) does not; for that path the bot can only rely on state that was already written during normal runtime updates.

After the bot has stopped, you can leave the virtual environment with:

```bash
deactivate
```

That returns your shell to the system-level Python environment and completes the local session cleanly.

## Railway Deployment

The repository ships with a unified entry point (`app.py`) and a `railway.toml` that target a single Railway Web Service plus a Railway PostgreSQL add-on.

### How it works

`app.py` imports the Flask dashboard and launches the bot as a background daemon thread in the same process.

### Required environment variables on Railway

| Variable | Description |
|---|---|
| `DATABASE_URL` | Railway-provided Postgres connection string (set automatically when you add the PostgreSQL plugin) |
| `BOT_MODE` | `paper` (default) or `live` |
| `MUSASHI_API_BASE_URL` | Base URL of the Musashi signal API |
| `PORT` | HTTP listen port — Railway sets this automatically |

All other bot env vars (`BOT_BANKROLL_USD`, `POLYMARKET_PRIVATE_KEY`, etc.) are optional and fall back to their defaults if not set.

### Single-instance constraint

**The service must run with exactly one replica and one worker.** The `railway.toml` already sets `--workers 1` in the gunicorn start command. Do not scale the service to multiple replicas unless you first redesign bot startup, because each replica would start its own bot thread writing to the same Postgres database.

### Deploying

1. Create a new Railway project and link this repository.
2. Add the Railway PostgreSQL plugin — Railway will inject `DATABASE_URL` automatically.
3. Set `BOT_MODE=paper` and any other required env vars in the Railway service settings.
4. Deploy. Railway will build with nixpacks and run:
   ```
   gunicorn --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
   ```
5. The dashboard becomes publicly accessible at the Railway-assigned URL.
6. Verify the service is healthy by checking `/healthz`:
   - Returns `200 {"ok": true}` when the bot thread is alive.
   - Returns `503 {"ok": false, "reason": "bot_thread_not_alive"}` if the bot thread has died or crashed.

### Local startup

```bash
python3 app.py
```

Use `HOST` and `PORT` if you want the local app to listen on a different address or port.
