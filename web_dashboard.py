#!/usr/bin/env python3
"""
Poly-Tifu Web Dashboard — standalone Flask server.

Shows what Poly-Tifu is, lists available strategies and modules,
and displays live bot status if credentials are configured.
Safe to run without any Polymarket credentials — falls back to
a read-only info dashboard.
"""
import os
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

bot_state = {
    "status": "idle",
    "bot_initialized": False,
    "iterations": 0,
    "last_update": None,
    "last_price": None,
    "open_orders": [],
    "balance": None,
    "errors": [],
    "has_credentials": False,
}

bot_instance = None
running = True

DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Poly-Tifu — Polymarket Trading Bot Dashboard</title>
<style>
:root { --bg:#0a0f1a; --card:#111827; --border:#1e293b; --text:#e2e8f0;
         --muted:#94a3b8; --green:#22c55e; --red:#ef4444; --blue:#3b82f6;
         --yellow:#eab308; --purple:#a855f7; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:var(--bg); color:var(--text); min-height:100vh; padding:24px; }
.container { max-width:1100px; margin:0 auto; }
h1 { font-size:1.75rem; font-weight:700; margin-bottom:.25rem; }
.subtitle { color:var(--muted); margin-bottom:1.5rem; font-size:.9rem; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px;
         padding:1.25rem; margin-bottom:1rem; }
.card h2 { font-size:1rem; font-weight:600; margin-bottom:.75rem; color:#fff; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
        gap:1rem; margin-bottom:1rem; }
.stat { text-align:center; background:var(--card); border:1px solid var(--border);
        border-radius:12px; padding:1rem; }
.stat-value { font-size:1.6rem; font-weight:700; }
.stat-label { font-size:.8rem; color:var(--muted); margin-top:.35rem; }
.green { color:var(--green); } .red { color:var(--red); }
.blue { color:var(--blue); } .yellow { color:var(--yellow); }
.purple { color:var(--purple); }
ul { list-style:none; padding:0; }
li { padding:.5rem 0; border-bottom:1px solid var(--border); font-size:.9rem; }
li:last-child { border-bottom:none; }
code { background:#020617; padding:2px 6px; border-radius:4px; font-size:.85rem; }
.badge { display:inline-block; padding:2px 8px; border-radius:12px; font-size:.7rem;
         font-weight:600; }
.badge.run { background:#064e3b; color:var(--green); }
.badge.idle { background:#1e293b; color:var(--muted); }
.badge.error { background:#450a0a; color:var(--red); }
.strategy-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1rem; }
.strategy { background:#0b1220; border:1px solid var(--border); border-radius:8px;
            padding:1rem; }
.strategy h3 { font-size:.95rem; color:#fff; margin-bottom:.4rem; }
.strategy p { font-size:.8rem; color:var(--muted); margin-bottom:.6rem; line-height:1.4; }
.strategy .cmd { font-size:.75rem; color:var(--blue); font-family:monospace; }
table { width:100%; border-collapse:collapse; font-size:.85rem; }
th { text-align:left; padding:.5rem; color:var(--muted); font-size:.75rem;
     text-transform:uppercase; border-bottom:2px solid var(--border); }
td { padding:.5rem; border-bottom:1px solid var(--border); }
.refresh-btn { background:var(--blue); color:white; border:none; padding:.6rem 1.2rem;
               border-radius:8px; cursor:pointer; font-weight:600; font-size:.85rem; }
.refresh-btn:hover { opacity:.9; }
footer { margin-top:2rem; text-align:center; color:var(--muted); font-size:.75rem; }
</style>
</head>
<body>
<div class="container">
<h1>🤖 Poly-Tifu</h1>
<p class="subtitle">Polymarket Trading Bot — CLOB V2 · Gasless · Flash Crash Strategy · WebSocket Orderbook</p>

<div class="grid">
<div class="stat">
<div class="stat-value" id="status"><span class="badge idle" id="statusBadge">IDLE</span></div>
<div class="stat-label">Bot Status</div>
</div>
<div class="stat">
<div class="stat-value" id="iterations">0</div>
<div class="stat-label">Iterations</div>
</div>
<div class="stat">
<div class="stat-value" id="creds">No</div>
<div class="stat-label">Credentials Set</div>
</div>
<div class="stat">
<div class="stat-value" id="lastPrice">-</div>
<div class="stat-label">Last Price</div>
</div>
</div>

<div class="card">
<h2>📦 What Poly-Tifu Does</h2>
<p style="color:var(--muted); font-size:.9rem; line-height:1.6; margin-bottom:.75rem;">
Poly-Tifu is a beginner-friendly Polymarket trading bot with gasless transactions (Builder Program),
real-time WebSocket orderbook data, and a flash-crash volatility strategy for 15-minute
BTC/ETH/SOL/XRP Up/Down markets. It signs orders against the CLOB V2 exchange domain.
</p>
<table>
<tr><th>Component</th><th>File</th><th>Purpose</th></tr>
<tr><td>Trading Bot Core</td><td><code>src/bot.py</code></td><td>Main bot class — orders, balance, market data</td></tr>
<tr><td>CLOB Client</td><td><code>src/client.py</code></td><td>Polymarket CLOB API interactions</td></tr>
<tr><td>Gamma Client</td><td><code>src/gamma_client.py</code></td><td>Gamma API — market discovery</td></tr>
<tr><td>Order Signer</td><td><code>src/signer.py</code></td><td>CLOB V2 order signing with builder attribution</td></tr>
<tr><td>Flash Crash</td><td><code>strategies/flash_crash.py</code></td><td>Volatility dip-buying strategy</td></tr>
<tr><td>Market Manager</td><td><code>lib/market_manager.py</code></td><td>Tracks active markets and orderbooks</td></tr>
<tr><td>Position Manager</td><td><code>lib/position_manager.py</code></td><td>Tracks positions and P&amp;L</td></tr>
<tr><td>Orderbook TUI</td><td><code>apps/orderbook_tui.py</code></td><td>Terminal orderbook viewer</td></tr>
</table>
</div>

<div class="card">
<h2>📈 Strategies</h2>
<div class="strategy-grid">
<div class="strategy">
<h3>⚡ Flash Crash</h3>
<p>Monitors 15-minute Up/Down markets for sudden probability drops and buys the crashed side.
Configurable drop threshold, size, take-profit, and stop-loss.</p>
<div class="cmd">python strategies/flash_crash.py --coin BTC --drop 0.30 --size 5</div>
</div>
<div class="strategy">
<h3>🔧 Basic Trading</h3>
<p>Simple example showing how to place orders, check positions, and manage balances through the bot API.
Good starting point for building your own strategy.</p>
<div class="cmd">python examples/basic_trading.py</div>
</div>
<div class="strategy">
<h3>📋 Quickstart</h3>
<p>Minimal example that initializes the bot, fetches markets, and demonstrates the core API surface.
Run this first to verify your setup works.</p>
<div class="cmd">python examples/quickstart.py</div>
</div>
</div>
</div>

<div class="card">
<h2>📊 Live Activity</h2>
<pre id="activity" style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem; max-height:200px;">Waiting for bot data...</pre>
</div>

<div class="card">
<h2>💰 Balance</h2>
<pre id="balance" style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem;">Not available — set POLY_PRIVATE_KEY + POLY_SAFE_ADDRESS env vars</pre>
</div>

<div class="card">
<h2>🔧 Run Locally</h2>
<pre style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem;">
pip install -r requirements.txt
export POLY_PRIVATE_KEY=your_key
export POLY_SAFE_ADDRESS=0xYourSafeAddress
python scripts/run_continuous.py  # dashboard + bot loop
python web_dashboard.py           # dashboard only (safe, no creds needed)
</pre>
</div>

<button class="refresh-btn" onclick="loadData()">↻ Refresh</button>

<footer>
Poly-Tifu · Polymarket CLOB V2 Trading Bot · Not financial advice
</footer>
</div>

<script>
async function loadData() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    const badge = document.getElementById('statusBadge');
    badge.textContent = data.status.toUpperCase();
    badge.className = 'badge ' + (data.status === 'running' ? 'run' :
      data.status === 'error' ? 'error' : 'idle');
    document.getElementById('iterations').textContent = data.iterations || 0;
    document.getElementById('creds').textContent = data.has_credentials ? 'Yes' : 'No';
    document.getElementById('creds').className = data.has_credentials ? 'stat-value green' : 'stat-value';
    document.getElementById('lastPrice').textContent = data.last_price || '-';
    document.getElementById('activity').textContent = data.recent_activity && data.recent_activity.length
      ? data.recent_activity.map(e => `${e.time} — ${e.error}`).join('\\n')
      : 'No activity yet.';
    document.getElementById('balance').textContent = data.balance
      ? JSON.stringify(data.balance, null, 2)
      : 'Not available — set POLY_PRIVATE_KEY + POLY_SAFE_ADDRESS env vars';
  } catch(e) {
    console.error(e);
  }
}
setInterval(loadData, 5000);
loadData();
</script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "status": bot_state["status"],
            "bot_initialized": bot_state["bot_initialized"],
            "iterations": bot_state["iterations"],
            "last_price": bot_state["last_price"],
            "open_orders_count": len(bot_state["open_orders"]),
            "open_orders": bot_state["open_orders"][:10],
            "balance": bot_state["balance"],
            "recent_activity": bot_state["errors"][-10:],
            "last_update": bot_state["last_update"],
            "has_credentials": bot_state["has_credentials"],
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _try_start_bot_loop():
    """Attempt to start the trading bot loop if credentials are present."""
    global bot_instance, bot_state
    try:
        from dotenv import load_dotenv

        load_dotenv()

        from scripts.run_bot import check_env_mode, load_config_from_env, get_private_key_from_env
        from src.bot import TradingBot

        if not check_env_mode():
            print("[web_dashboard] No credentials found — running in info-only mode")
            bot_state["status"] = "idle"
            return

        bot_state["has_credentials"] = True
        config = load_config_from_env()
        private_key = get_private_key_from_env()
        bot = TradingBot(config=config, private_key=private_key)
        bot_instance = bot
        bot_state["bot_initialized"] = True
        bot_state["status"] = "running"
        print("[web_dashboard] Bot initialized — running trading loop")

        import asyncio

        asyncio.run(_bot_loop(bot))
    except Exception as e:
        bot_state["status"] = "error"
        bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": f"Bot init failed: {e}"})
        print(f"[web_dashboard] Bot not started: {e} — running in info-only mode")


async def _bot_loop(bot):
    """Lightweight bot status loop — no actual trading, just monitoring."""
    global bot_state
    interval = int(os.environ.get("POLY_BOT_INTERVAL", "60"))
    iteration = 0
    while running:
        iteration += 1
        bot_state["iterations"] = iteration
        bot_state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            if bot.config.default_token_id:
                price = await bot.get_market_price(bot.config.default_token_id)
                if price:
                    bot_state["last_price"] = str(price)
            try:
                orders = await bot.get_open_orders()
                if orders:
                    bot_state["open_orders"] = orders[:20]
            except Exception:
                pass
            try:
                balance = await bot.get_balance()
                if balance:
                    bot_state["balance"] = balance
            except Exception:
                pass
        except Exception as e:
            bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": str(e)})
            if len(bot_state["errors"]) > 50:
                bot_state["errors"] = bot_state["errors"][-50:]
        for _ in range(interval):
            if not running:
                break
            await asyncio.sleep(1)
    bot_state["status"] = "stopped"


def main():
    import signal

    def handle_signal(signum, frame):
        global running
        running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bot_thread = threading.Thread(target=_try_start_bot_loop, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"[web_dashboard] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
