from flask import Flask, render_template, jsonify
from pathlib import Path
import json
from datetime import datetime
import os

app = Flask(__name__)

DATA_DIR = Path("bot/data")
LOG_FILE = Path("bot/logs/bot.log")
ARB_TRADES_FILE = DATA_DIR / "arbitrage_trades.jsonl"

def load_arbitrage_trades():
    """Load arbitrage trades from JSONL file"""
    if not ARB_TRADES_FILE.exists():
        return []

    trades = []
    try:
        for line in ARB_TRADES_FILE.read_text(encoding='utf-8').strip().split('\n'):
            if line:
                try:
                    trades.append(json.loads(line))
                except:
                    pass
    except:
        pass

    return trades

def get_logs(limit=20):
    """Get last N lines from log file"""
    if not LOG_FILE.exists():
        return []

    try:
        lines = LOG_FILE.read_text(encoding='utf-8', errors='ignore').strip().split('\n')
        return lines[-limit:]
    except:
        return []

def format_time(iso_str):
    """Format ISO timestamp to readable time"""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.strftime('%H:%M:%S')
    except:
        return iso_str

@app.route('/')
def index():
    """Main dashboard page with arbitrage data"""
    trades = load_arbitrage_trades()
    logs = get_logs(20)

    # Calculate metrics
    total_profit = sum(float(t.get('realized_pnl', 0)) for t in trades)
    arb_count = len(trades)
    win_rate = 100.0 if arb_count > 0 else 0.0  # Arbitrage = 100% win rate (it's math)

    # Calculate average spread
    if trades:
        spreads = [float(t.get('spread_percent', 0)) * 100 for t in trades]
        avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    else:
        avg_spread = 0.0

    bankroll = 100.0  # $100 paper money
    available = bankroll  # All available since arbitrage is instant (no open positions)

    # Format recent trades for display
    recent_trades = []
    for trade in trades[-20:]:  # Last 20 trades
        recent_trades.append({
            'time': format_time(trade.get('opened_at', '')),
            'title': trade.get('title', 'Unknown'),
            'buy_platform': trade.get('buy_platform', 'unknown'),
            'sell_platform': trade.get('sell_platform', 'unknown'),
            'buy_price': float(trade.get('buy_price', 0)),
            'sell_price': float(trade.get('sell_price', 0)),
            'spread_percent': float(trade.get('spread_percent', 0)),
            'profit': float(trade.get('realized_pnl', 0))
        })

    # Reverse so newest is first
    recent_trades.reverse()

    return render_template(
        'dashboard.html',
        total_profit=total_profit,
        arb_count=arb_count,
        win_rate=win_rate,
        avg_spread=avg_spread,
        bankroll=bankroll,
        available=available,
        recent_trades=recent_trades,
        logs=logs
    )

@app.route('/api/metrics')
def api_metrics():
    """API endpoint for metrics (for future AJAX updates)"""
    trades = load_arbitrage_trades()

    total_profit = sum(float(t.get('realized_pnl', 0)) for t in trades)
    arb_count = len(trades)
    win_rate = 100.0 if arb_count > 0 else 0.0

    if trades:
        spreads = [float(t.get('spread_percent', 0)) * 100 for t in trades]
        avg_spread = sum(spreads) / len(spreads) if spreads else 0.0
    else:
        avg_spread = 0.0

    return jsonify({
        'total_profit': round(total_profit, 2),
        'arb_count': arb_count,
        'win_rate': round(win_rate, 1),
        'avg_spread': round(avg_spread, 2),
        'bankroll': 100.0,
        'available': 100.0
    })

@app.route('/api/trades')
def api_trades():
    """API endpoint for recent trades"""
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
            'profit': float(trade.get('realized_pnl', 0))
        })

    recent_trades.reverse()
    return jsonify(recent_trades)

@app.route('/api/logs')
def api_logs():
    """API endpoint for bot logs"""
    return jsonify(get_logs(20))

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
