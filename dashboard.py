from flask import Flask, render_template, jsonify
from pathlib import Path
import json
from datetime import datetime

app = Flask(__name__)

# Paths relative to this file so the server works regardless of CWD.
_ROOT = Path(__file__).resolve().parent
DATA_DIR = _ROOT / "bot" / "data"
LOG_FILE = _ROOT / "bot" / "logs" / "bot.log"
ARB_TRADES_FILE = DATA_DIR / "arbitrage_trades.jsonl"

# Paper trading starting bankroll (demo only — not linked to real funds).
PAPER_BANKROLL = 100.0


def load_arbitrage_trades():
    if not ARB_TRADES_FILE.exists():
        return []
    trades = []
    try:
        for line in ARB_TRADES_FILE.read_text(encoding='utf-8').strip().split('\n'):
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return trades


def get_logs(limit=20):
    if not LOG_FILE.exists():
        return []
    try:
        lines = LOG_FILE.read_text(encoding='utf-8', errors='ignore').strip().split('\n')
        return lines[-limit:]
    except Exception:
        return []


def format_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.strftime('%H:%M:%S')
    except Exception:
        return iso_str


def _compute_metrics(trades):
    """Return a dict of dashboard metrics derived from trade records."""
    total_profit = sum(float(t.get('realized_pnl', 0)) for t in trades)
    arb_count = len(trades)
    winning = sum(1 for t in trades if float(t.get('realized_pnl', 0)) > 0)
    win_rate = (winning / arb_count * 100) if arb_count > 0 else 0.0
    spreads = [float(t.get('spread_percent', 0)) * 100 for t in trades]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    # Current paper bankroll = starting bankroll + all simulated gains/losses
    bankroll = PAPER_BANKROLL + total_profit
    # All capital is available; arbitrage positions close immediately in simulation
    available = bankroll
    return {
        'total_profit': total_profit,
        'arb_count': arb_count,
        'win_rate': win_rate,
        'avg_spread': avg_spread,
        'bankroll': bankroll,
        'available': available,
    }


@app.route('/')
def index():
    trades = load_arbitrage_trades()
    logs = get_logs(20)
    m = _compute_metrics(trades)

    recent_trades = []
    for trade in trades[-20:]:
        recent_trades.append({
            'time': format_time(trade.get('opened_at', '')),
            'title': trade.get('title', 'Unknown'),
            'buy_platform': trade.get('buy_platform', 'unknown'),
            'sell_platform': trade.get('sell_platform', 'unknown'),
            'buy_price': float(trade.get('buy_price', 0)),
            'sell_price': float(trade.get('sell_price', 0)),
            'spread_percent': float(trade.get('spread_percent', 0)),
            'profit': float(trade.get('realized_pnl', 0)),
        })
    recent_trades.reverse()

    return render_template(
        'dashboard.html',
        total_profit=m['total_profit'],
        arb_count=m['arb_count'],
        win_rate=m['win_rate'],
        avg_spread=m['avg_spread'],
        bankroll=m['bankroll'],
        available=m['available'],
        recent_trades=recent_trades,
        logs=logs,
    )


@app.route('/api/metrics')
def api_metrics():
    trades = load_arbitrage_trades()
    m = _compute_metrics(trades)
    return jsonify({
        'total_profit': round(m['total_profit'], 2),
        'arb_count': m['arb_count'],
        'win_rate': round(m['win_rate'], 1),
        'avg_spread': round(m['avg_spread'], 2),
        'bankroll': round(m['bankroll'], 2),
        'available': round(m['available'], 2),
        'paper_mode': True,
    })


@app.route('/api/trades')
def api_trades():
    trades = load_arbitrage_trades()
    recent_trades = []
    for trade in trades[-20:]:
        recent_trades.append({
            'time': format_time(trade.get('opened_at', '')),
            'title': trade.get('title', 'Unknown'),
            'buy_platform': trade.get('buy_platform', 'unknown'),
            'sell_platform': trade.get('sell_platform', 'unknown'),
            'buy_price': float(trade.get('buy_price', 0)),
            'sell_price': float(trade.get('sell_price', 0)),
            'spread_percent': float(trade.get('spread_percent', 0)),
            'profit': float(trade.get('realized_pnl', 0)),
        })
    recent_trades.reverse()
    return jsonify(recent_trades)


@app.route('/api/logs')
def api_logs():
    return jsonify(get_logs(20))


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
