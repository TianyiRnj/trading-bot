# Minimal Musashi x Polymarket Bot

This repository contains a minimal automated trading bot that listens to Musashi signals and routes selected opportunities to Polymarket. The goal is to provide a small, runnable baseline that is easy to understand, configure, and extend.

At a high level, the bot polls the Musashi `feed`, re-scores incoming text with `analyze-text`, filters for higher-confidence Polymarket opportunities, resolves the corresponding `token_id`, and then manages positions in either `paper` or `live` mode. In addition to opening trades, it can automatically take profit, stop loss, close positions after a maximum hold time, and exit when the signal reverses.

To make live operation more practical, the bot also tracks real fills, preserves exact share counts, handles partially filled exits, polls pending exit orders, cancels timed-out exit orders, and can re-list any remaining size. It also avoids submitting duplicate exit orders for a position that already has a pending exit order. When the bot starts, it can check the Polymarket geoblock endpoint and reconcile stale or inconsistent local order state before trading begins.

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

## Postgres Preparation

The repository now includes the configuration files needed to prepare a local Postgres instance for the upcoming storage migration:

- `docker-compose.yml`
- `db/init/001_init.sql`
- Postgres environment variables in `.env.example`
- Python Postgres driver dependency in `requirements.txt`

Start a local Postgres instance from the repository root with:

```bash
docker compose up -d postgres
```

The default bootstrap values in `.env.example` are:

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

At the moment, the runtime code still reads and writes the local JSON / JSONL files described below. The Postgres files are preparatory and intended to be used by the upcoming runtime migration.

## Quick Start

If you want the shortest path to a working local run, use the commands below from the repository root:

```bash
cd /Musashi/trading-bot
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 bot/main.py
```

That will start the bot with whatever values are currently defined in `.env`, so the next step is to make sure the configuration matches the mode you want to use.

## Configuration

Before launching the bot in earnest, open `.env` and decide whether you want to run in paper mode or live mode. Starting in paper mode is the safest way to confirm that signal selection, logging, and position handling behave as expected.

If you plan to use the prepared Postgres configuration, make sure `DATABASE_URL` and the `POSTGRES_*` values in `.env` match the database you want to run against.

### Paper Trading

To stay in paper mode, keep the following setting:

```bash
BOT_MODE=paper
```

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
- `BOT_EXIT_ORDER_REPRICE=true` cancels the old exit order and re-lists the remaining shares at an updated price
- `BOT_STARTUP_RECONCILE=true` performs a geoblock check and reconciles local order state when the bot starts

With the configuration in place, you are ready to run the bot normally.

## Running the Bot

Start the bot from the project root with:

```bash
python3 bot/main.py
```

As the bot runs, it will write logs and local state files so you can inspect what it is doing over time.

## Arbitrage Scanner (Optional, Simulation Only)

The repository includes a cross-platform spread scanner that compares YES prices between Polymarket and Kalshi. It is **disabled by default** and runs in **simulation only** — no real Kalshi orders are ever placed.

To enable it, set the following in `.env`:

```bash
BOT_ENABLE_ARBITRAGE=true
```

When enabled, the scanner runs as a background thread alongside the main Musashi signal loop and writes simulated trade records to `bot/data/arbitrage_trades.jsonl`.

## Arbitrage Dashboard (Flask)

A lightweight Flask dashboard displays simulated arbitrage results. All metrics (bankroll, profit, win rate) reflect paper trades only.

Install the dashboard dependency if you have not already:

```bash
pip install -r requirements.txt  # includes flask>=3.0.0 and psycopg[binary]
```

Run from the project root (with `BOT_ENABLE_ARBITRAGE=true` and the bot running to generate trade data):

```bash
python3 dashboard.py
```

The dashboard is available at `http://127.0.0.1:5000`.

## Output Files

The current runtime still writes the following files during operation:

- `bot/logs/bot.log`
- `bot/data/positions.json`
- `bot/data/trades.jsonl`
- `bot/data/seen_event_ids.json`

Those files are useful for monitoring the current state, reviewing trade history, and understanding how the bot reached its decisions.

In addition, the repository now includes the prepared Postgres bootstrap files:

- `docker-compose.yml`
- `db/init/001_init.sql`

Those files define the local development database container and the initial schema for the planned migration to Postgres-backed runtime state.

## Running in the Background

If you want the bot to keep running after you close the terminal, start it with `nohup`:

```bash
nohup ./.venv/bin/python bot/main.py >> bot/logs/nohup.log 2>&1 &
```

That background process uses the virtual environment directly, which helps ensure the bot keeps running with the same Python interpreter and installed dependencies you used during setup.

## Stopping the Bot and Deactivating the Environment

When you are done, the exact shutdown step depends on how you started the bot. If the bot is running in the current terminal, press `Ctrl+C` to stop the process cleanly and return to the shell.

If you started the bot in the background with `nohup`, find the process and terminate it explicitly:

```bash
ps aux | grep "bot/main.py"
kill <PID>
```

If the process does not stop after a normal `kill`, you can force termination with:

```bash
kill -9 <PID>
```

After the bot has stopped, you can leave the virtual environment with:

```bash
deactivate
```

That returns your shell to the system-level Python environment and completes the local session cleanly.
