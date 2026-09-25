#!/usr/bin/env python3
"""
Continuous runner for Poly-Tifu trading bot with web dashboard.
Runs the bot in a loop + Flask web server for status dashboard.
"""
import os
import sys
import asyncio
import time
import signal
import threading
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify
from src.config import Config
from src.bot import TradingBot
from scripts.run_bot import check_env_mode, load_config_from_env, get_private_key_from_env

# Global state for web dashboard
bot_state = {
    "status": "starting",
    "iterations": 0,
    "last_update": None,
    "last_price": None,
    "open_orders": [],
    "balance": None,
    "errors": []
}

bot_instance = None
running = True

def signal_handler(signum, frame):
    global running
    running = False

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============================================================
# Flask Web Dashboard
# ============================================================
app = Flask(__name__)

HTML_DASHBOARD = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Poly-Tifu Trading Bot</title>
<style>
:root { --bg:#0a0f1a; --card:#111827; --border:#1e293b; --text:#e2e8f0; --muted:#94a3b8; --green:#22c55e; --red:#ef4444; --blue:#3b82f6; }
* { box-sizing:border-box; }
body { margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:var(--bg); color:var(--text); min-height:100vh; padding:20px; }
.container { max-width:900px; margin:0 auto; }
h1 { font-size:1.5rem; font-weight:700; margin-bottom:1rem; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:1.5rem; margin-bottom:1rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:1rem; margin-bottom:1rem; }
.stat { text-align:center; }
.stat-value { font-size:2rem; font-weight:700; }
.stat-label { font-size:.875rem; color:var(--muted); margin-top:.25rem; }
.green { color:var(--green); }
.red { color:var(--red); }
.blue { color:var(--blue); }
pre { background:#020617; border:1px solid var(--border); border-radius:8px; padding:1rem; overflow:auto; font-size:.875rem; }
.refresh { background:var(--blue); color:white; border:none; padding:.75rem 1.5rem; border-radius:8px; cursor:pointer; font-weight:600; }
.refresh:hover { opacity:.9; }
</style>
</head>
<body>
<div class="container">
<h1>Poly-Tifu Trading Bot</h1>

<div class="grid">
<div class="card stat">
<div class="stat-value" id="status">Loading...</div>
<div class="stat-label">Bot Status</div>
</div>
<div class="card stat">
<div class="stat-value" id="iterations">0</div>
<div class="stat-label">Iterations</div>
</div>
<div class="card stat">
<div class="stat-value" id="lastPrice">-</div>
<div class="stat-label">Last Price</div>
</div>
<div class="card stat">
<div class="stat-value" id="openOrders">0</div>
<div class="stat-label">Open Orders</div>
</div>
</div>

<div class="card">
<h3>Recent Activity</h3>
<pre id="activity">Loading...</pre>
</div>

<div class="card">
<h3>Balance</h3>
<pre id="balance">Loading...</pre>
</div>

<button class="refresh" onclick="loadData()">Refresh Now</button>
</div>

<script>
async function loadData() {
try {
const res = await fetch('/api/status');
const data = await res.json();
document.getElementById('status').textContent = data.status;
document.getElementById('status').className = 'stat-value ' + (data.status === 'running' ? 'green' : data.status === 'error' ? 'red' : 'blue');
document.getElementById('iterations').textContent = data.iterations;
document.getElementById('lastPrice').textContent = data.last_price || '-';
document.getElementById('openOrders').textContent = data.open_orders_count;
document.getElementById('activity').textContent = JSON.stringify(data.recent_activity, null, 2);
document.getElementById('balance').textContent = JSON.stringify(data.balance, null, 2);
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

@app.route('/')
def dashboard():
    return HTML_DASHBOARD

@app.route('/api/status')
def api_status():
    return jsonify({
        "status": bot_state["status"],
        "iterations": bot_state["iterations"],
        "last_price": bot_state["last_price"],
        "open_orders_count": len(bot_state["open_orders"]),
        "open_orders": bot_state["open_orders"][:10],
        "balance": bot_state["balance"],
        "recent_activity": bot_state["errors"][-10:],
        "last_update": bot_state["last_update"]
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy" if bot_state["status"] == "running" else "degraded"})

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ============================================================
# Bot Loop
# ============================================================
async def run_bot_loop(bot: TradingBot, interval_seconds: int = 60):
    """Run the bot continuously and update state for web dashboard."""
    global bot_state, bot_instance
    bot_instance = bot
    bot_state["status"] = "running"
    
    print("\n==================================================")
    print("Continuous Trading Bot Started")
    print("==================================================")
    print(f"Interval: {interval_seconds}s | Web dashboard on PORT {os.environ.get('PORT', 8080)}\n")
    
    iteration = 0
    while running:
        iteration += 1
        bot_state["iterations"] = iteration
        bot_state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"\n--- Iteration {iteration} ---")
        
        try:
            # Show status in console
            from scripts.run_bot import print_status
            await print_status(bot)
            
            # Get market price if configured
            if bot.config.default_token_id:
                price_data = await bot.get_market_price(bot.config.default_token_id)
                if price_data:
                    bot_state["last_price"] = str(price_data)
                    print(f"  Price: {price_data}")
            
            # Check for open orders
            orders = await bot.get_open_orders()
            if orders:
                bot_state["open_orders"] = orders[:20]
                print(f"  Open orders: {len(orders)}")
            
            # Get balance
            try:
                balance = await bot.get_balance()
                if balance:
                    bot_state["balance"] = balance
            except:
                pass
            
        except Exception as e:
            error_msg = f"Iteration {iteration} error: {e}"
            bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": str(e)})
            if len(bot_state["errors"]) > 50:
                bot_state["errors"] = bot_state["errors"][-50:]
            print(f"Error: {e}")
        
        # Sleep until next iteration
        if running:
            print(f"\nSleeping {interval_seconds}s...")
            for _ in range(interval_seconds):
                if not running:
                    break
                await asyncio.sleep(1)
    
    bot_state["status"] = "stopped"
    print("\nBot stopped gracefully.")

def main():
    print("\n==================================================")
    print("Polymarket Trading Bot - Continuous + Web")
    print("==================================================\n")
    
    interval = int(os.environ.get("POLY_BOT_INTERVAL", "60"))
    
    # Check for environment variable mode
    use_env_mode = check_env_mode()
    
    if use_env_mode:
        print("Using environment variables mode")
        config = load_config_from_env()
        private_key = get_private_key_from_env()
        print(f"Configuration loaded (gasless: {config.use_gasless})")
    else:
        print("Environment variables not found, using encrypted key mode")
        config = Config.load("config.yaml")
        print(f"Configuration loaded (gasless: {config.use_gasless})")
        from scripts.run_bot import decrypt_private_key
        private_key = decrypt_private_key()
    
    # Initialize bot
    try:
        bot = TradingBot(config=config, private_key=private_key)
    except Exception as e:
        print(f"Failed to initialize bot: {e}")
        sys.exit(1)
    
    print("Bot initialized!")
    
    # Start Flask web server in background thread
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    print(f"Web dashboard started on port {os.environ.get('PORT', 8080)}")
    
    # Run bot loop in main thread
    try:
        asyncio.run(run_bot_loop(bot, interval))
    except KeyboardInterrupt:
        print("\n\nInterrupted.")
        sys.exit(0)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()