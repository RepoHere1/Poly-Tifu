#!/usr/bin/env python3
"""
Poly-Tifu Web Dashboard -- standalone Flask server.

Shows what Poly-Tifu is, lists available strategies and modules,
and displays live bot status if credentials are configured.
Safe to run without any Polymarket credentials -- falls back to
a read-only info dashboard.
"""
import os
import sys
import time
import json
import threading
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, request, render_template_string, Response
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config["PREFERRED_URL_SCHEME"] = "https"

API_KEY = os.environ.get("POLY_DASHBOARD_KEY", "")
DEFAULT_COIN = os.environ.get("POLY_DEFAULT_COIN", "BTC")

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
    "neg_risk": False,
    "strategy": "none",
    "recent_trades": [],
    "alerts": [],
}

running = True
bot_instance = None
bot_loop_loop = None

ALLOWED_HOSTS = [
    "*",
    "poly-tifu.railway.internal",
    "poly-tifu.up.railway.app",
    "localhost",
    "127.0.0.1",
]

DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Poly-Tifu -- Polymarket Trading Bot Dashboard</title>
<style>
:root { --bg:#0a0f1a; --card:#111827; --border:#1e293b; --text:#e2e8f0;
         --muted:#94a3b8; --green:#22c55e; --red:#ef4444; --blue:#3b82f6;
         --yellow:#eab308; --purple:#a855f7; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:var(--bg); color:var(--text); min-height:100vh; padding:24px; }
.container { max-width:1200px; margin:0 auto; }
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
.form-row { display:flex; gap:.5rem; flex-wrap:wrap; align-items:end; margin-bottom:1rem; }
.form-group { display:flex; flex-direction:column; gap:.25rem; }
.form-group label { font-size:.75rem; color:var(--muted); }
.form-group input, .form-group select { background:#020617; border:1px solid var(--border);
  color:var(--text); padding:.4rem .6rem; border-radius:6px; font-size:.85rem; }
.form-group input:focus, .form-group select:focus { border-color:var(--blue); outline:none; }
.btn { background:var(--blue); color:white; border:none; padding:.5rem 1rem;
       border-radius:8px; cursor:pointer; font-weight:600; font-size:.8rem; }
.btn:hover { opacity:.9; }
.btn.danger { background:var(--red); }
.btn.success { background:var(--green); }
.alert { padding:.5rem .75rem; border-radius:8px; font-size:.8rem; margin-bottom:.5rem; }
.alert.info { background:#1e3a5f; border-left:3px solid var(--blue); }
.alert.warn { background:#3b2e0a; border-left:3px solid var(--yellow); }
.alert.error { background:#450a0a; border-left:3px solid var(--red); }
.column { display:flex; flex-direction:column; gap:.25rem; }
</style>
</head>
<body>
<div class="container">
<h1>&#x1F916; Poly-Tifu</h1>
<p class="subtitle">Polymarket Trading Bot -- CLOB V2 &middot; Gasless &middot; Flash Crash &middot; Grid/Arb &middot; WebSocket Orderbook &middot; SSE Stream</p>

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
<div class="stat-label">Credentials</div>
</div>
<div class="stat">
<div class="stat-value" id="lastPrice">-</div>
<div class="stat-label">Last Price</div>
</div>
<div class="stat">
<div class="stat-value" id="strategy">none</div>
<div class="stat-label">Strategy</div>
</div>
<div class="stat">
<div class="stat-value" id="negRisk">Off</div>
<div class="stat-label">Neg Risk</div>
</div>
</div>

<div id="alerts"></div>

<div class="card">
<h2>&#x1F4C6; Place Order</h2>
<div class="form-row">
<div class="form-group">
<label>Side</label>
<select id="orderSide"><option>BUY</option><option>SELL</option></select>
</div>
<div class="form-group">
<label>Token ID</label>
<input id="orderToken" style="width:260px" placeholder="0x...">
</div>
<div class="form-group">
<label>Price (0-1)</label>
<input id="orderPrice" type="number" step="0.01" min="0" max="1" value="0.50" style="width:100px">
</div>
<div class="form-group">
<label>Size</label>
<input id="orderSize" type="number" step="0.1" min="0.1" value="10" style="width:100px">
</div>
<div class="form-group">
<label>Type</label>
<select id="orderType"><option>GTC</option><option>GTD</option><option>FOK</option></select>
</div>
<div class="form-group">
<label>Neg Risk</label>
<select id="orderNegRisk"><option value="false">No</option><option value="true">Yes</option></select>
</div>
<button class="btn success" onclick="placeOrder()">&#x2795; Place</button>
<button class="btn danger" onclick="cancelOrder()">&#x2796; Cancel</button>
<button class="btn" onclick="cancelAllOrders()">&#x2797; Cancel All</button>
</div>
<div id="orderResult" style="font-size:.85rem; margin-top:.5rem;"></div>
</div>

<div class="card">
<h2>&#x1F4C8; Market Selector</h2>
<div class="form-row">
<div class="form-group">
<label>Coin</label>
<select id="marketCoin" onchange="loadMarkets()">
<option>BTC</option><option>ETH</option><option>SOL</option><option>XRP</option>
</select>
</div>
<button class="btn" onclick="loadMarkets()">&#x1F504; Refresh</button>
</div>
<div id="marketsTable"></div>
</div>

<div class="card">
<h2>&#x1F3C1; Strategy</h2>
<div class="form-row">
<div class="form-group">
<label>Strategy</label>
<select id="strategySelect" onchange="setStrategy()">
<option value="none">none</option>
<option value="flash_crash">Flash Crash</option>
<option value="grid">Grid</option>
<option value="arb">Arb</option>
</select>
</div>
<div class="form-group" id="gridParams" style="display:none">
<label>Grid Levels</label>
<input id="gridLevels" type="number" value="5" style="width:80px">
<label>Range %</label>
<input id="gridRange" type="number" step="0.1" value="2" style="width:80px">
</div>
<button class="btn" onclick="setStrategy()">Apply</button>
</div>
</div>

<div class="card">
<h2>&#x1F4CA; Live Activity</h2>
<pre id="activity" style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem; max-height:200px;">Waiting for bot data...</pre>
</div>

<div class="card">
<h2>&#x1F4B0; Balance</h2>
<pre id="balance" style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem;">Not available -- set POLY_PRIVATE_KEY + POLY_SAFE_ADDRESS env vars</pre>
</div>

<div class="card">
<h2>&#x1F4B6; Recent Trades</h2>
<div id="tradesTable"></div>
</div>

<div class="card">
<h2>&#x1F514; Alerts (SSE)</h2>
<div id="alertLog" style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem; max-height:150px;">Listening for alerts...</div>
</div>

<div class="card">
<h2>&#x1F527; Run Locally</h2>
<pre style="background:#020617; border:1px solid var(--border); border-radius:8px;
     padding:1rem; overflow:auto; font-size:.8rem;">
pip install -r requirements.txt
export POLY_PRIVATE_KEY=your_key
export POLY_SAFE_ADDRESS=0xYourSafeAddress
python scripts/run_continuous.py  # dashboard + bot loop
python web_dashboard.py           # dashboard only (safe, no creds needed)
</pre>
</div>

<button class="refresh-btn" onclick="loadData()">&#x21BB; Refresh</button>

<footer>
Poly-Tifu &middot; Polymarket CLOB V2 Trading Bot &middot; Not financial advice
</footer>
</div>

<script>
async function loadData() {
  try {
    const res = await fetch('/api/status');
    const d = await res.json();
    const badge = document.getElementById('statusBadge');
    badge.textContent = (d.status||'idle').toUpperCase();
    badge.className = 'badge ' + (d.status==='running'?'run':d.status==='error'?'error':'idle');
    document.getElementById('iterations').textContent = d.iterations||0;
    document.getElementById('creds').textContent = d.has_credentials?'Yes':'No';
    document.getElementById('creds').className = d.has_credentials?'stat-value green':'stat-value';
    document.getElementById('lastPrice').textContent = d.last_price||'-';
    document.getElementById('strategy').textContent = d.strategy||'none';
    document.getElementById('negRisk').textContent = d.neg_risk?'On':'Off';
    document.getElementById('negRisk').className = d.neg_risk?'stat-value yellow':'stat-value';
    document.getElementById('activity').textContent = d.recent_activity&&d.recent_activity.length
      ? d.recent_activity.map(e=>e.time+' -- '+e.error).join('\\n')
      : 'No activity yet.';
    document.getElementById('balance').textContent = d.balance
      ? JSON.stringify(d.balance,null,2)
      : 'Not available -- set POLY_PRIVATE_KEY + POLY_SAFE_ADDRESS env vars';
  } catch(e) { console.error(e); }
}
setInterval(loadData, 5000);
loadData();

async function loadMarkets() {
  const coin = document.getElementById('marketCoin').value;
  const res = await fetch('/api/markets?coin='+coin);
  const m = await res.json();
  const el = document.getElementById('marketsTable');
  if (!m || !m.question) { el.innerHTML='<p>No market found</p>'; return; }
  const up = m.outcomes&&m.outcomes[0]||{};
  const dn = m.outcomes&&m.outcomes[1]||{};
  el.innerHTML = '<table><tr><th>Outcome</th><th>Token ID</th><th>Price</th><th>Volume</th></tr>'+
    '<tr><td>UP</td><td><code>'+(up.id||'-')+'</code></td><td>'+(up.price||'-')+'</td><td>'+(up.volume||'-')+'</td></tr>'+
    '<tr><td>DOWN</td><td><code>'+(dn.id||'-')+'</code></td><td>'+(dn.price||'-')+'</td><td>'+(dn.volume||'-')+'</td></tr>'+
    '</table><p style="font-size:.8rem;color:var(--muted);margin-top:.5rem">Accepting orders: '+(m.acceptingOrders?'Yes':'No')+' &middot; End Date: '+(m.endDate||'-')+'</p>';
}

async function placeOrder() {
  const side = document.getElementById('orderSide').value;
  const token = document.getElementById('orderToken').value;
  const price = parseFloat(document.getElementById('orderPrice').value);
  const size = parseFloat(document.getElementById('orderSize').value);
  const type = document.getElementById('orderType').value;
  const neg = document.getElementById('orderNegRisk').value==='true';
  const r = await fetch('/api/order', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({side,token_id:token,price,size,order_type:type,neg_risk:neg})});
  const d = await r.json();
  document.getElementById('orderResult').textContent = JSON.stringify(d,null,2);
}

async function cancelOrder() {
  const id = prompt('Order ID to cancel:');
  if(!id) return;
  const r = await fetch('/api/order/'+encodeURIComponent(id)+'/cancel', {method:'POST'});
  const d = await r.json();
  document.getElementById('orderResult').textContent = JSON.stringify(d,null,2);
}

async function cancelAllOrders() {
  if(!confirm('Cancel all orders?')) return;
  const r = await fetch('/api/orders/cancel-all', {method:'POST'});
  const d = await r.json();
  document.getElementById('orderResult').textContent = JSON.stringify(d,null,2);
}

async function setStrategy() {
  const s = document.getElementById('strategySelect').value;
  const cfg = s==='grid' ? {levels:parseInt(document.getElementById('gridLevels').value),
    range:parseFloat(document.getElementById('gridRange').value)} : {};
  const r = await fetch('/api/strategy', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({strategy:s, config:cfg})});
  const d = await r.json();
  alert('Strategy set: '+(d.strategy||'none'));
}

// SSE alerts
const evtSource = new EventSource('/api/stream');
evtSource.onmessage = (e) => {
  const data = JSON.parse(e.data);
  const log = document.getElementById('alertLog');
  const div = document.createElement('div');
  div.className = 'alert '+(data.level||'info');
  div.textContent = new Date(data.time).toLocaleTimeString()+' ['+data.level+'] '+data.msg;
  log.prepend(div);
  while(log.children.length>20) log.removeChild(log.lastChild);
};
evtSource.onerror = () => { setTimeout(()=>{evtSource.close();location.reload();},3000); };

loadMarkets();
</script>
</body>
</html>
"""

# ============================================================
# Auth decorator
# ============================================================
def require_auth(f):
    def wrapper(*args, **kwargs):
        if API_KEY:
            key = request.headers.get("X-API-Key", "")
            if key != API_KEY:
                return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


# ============================================================
# Routes
# ============================================================
@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/status")
@require_auth
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
            "neg_risk": bot_state["neg_risk"],
            "strategy": bot_state["strategy"],
            "recent_trades": bot_state["recent_trades"][:10],
        }
    )


@app.route("/api/order", methods=["POST"])
@require_auth
def api_place_order():
    data = request.get_json(force=True, silent=True) or {}
    if not bot_instance or not bot_state["bot_initialized"]:
        return jsonify({"success": False, "message": "Bot not initialized"}), 400
    token_id = data.get("token_id", "")
    price = data.get("price", 0.5)
    size = data.get("size", 10)
    side = data.get("side", "BUY").upper()
    order_type = data.get("order_type", "GTC")
    neg_risk = data.get("neg_risk", bot_state["neg_risk"])
    if not token_id:
        return jsonify({"success": False, "message": "token_id required"}), 400
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(
            bot_instance.place_order(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side,
                order_type=order_type,
                neg_risk=neg_risk,
            )
        )
        loop.close()
        bot_state["errors"].append(
            {"time": time.strftime("%H:%M:%S"), "error": f"Order {side} {size}@{price} on {token_id[:16]}..."}
        )
        return jsonify(result.__dict__ if hasattr(result, "__dict__") else result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/order/<order_id>/cancel", methods=["POST"])
@require_auth
def api_cancel_order(order_id):
    if not bot_instance:
        return jsonify({"success": False, "message": "Bot not initialized"}), 400
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(bot_instance.cancel_order(order_id))
        loop.close()
        return jsonify(result.__dict__ if hasattr(result, "__dict__") else result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/orders/cancel-all", methods=["POST"])
@require_auth
def api_cancel_all():
    if not bot_instance:
        return jsonify({"success": False, "message": "Bot not initialized"}), 400
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(bot_instance.cancel_all_orders())
        loop.close()
        return jsonify(result.__dict__ if hasattr(result, "__dict__") else result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/markets")
@require_auth
def api_markets():
    from src.gamma_client import GammaClient
    coin = request.args.get("coin", DEFAULT_COIN).upper()
    try:
        client = GammaClient()
        market = client.get_current_15m_market(coin)
        return jsonify(market or {"error": "No market found"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
@require_auth
def api_trades():
    if not bot_instance:
        return jsonify([])
    token_id = request.args.get("token_id", None)
    limit = int(request.args.get("limit", 20))
    try:
        loop = asyncio.new_event_loop()
        trades = loop.run_until_complete(bot_instance.get_trades(token_id, limit))
        loop.close()
        return jsonify(trades)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stream")
@require_auth
def api_stream():
    def generate():
        last_price = None
        while running:
            yield f"data: {json.dumps({'time': time.strftime('%H:%M:%S'), 'price': bot_state['last_price'], 'status': bot_state['status'], 'level': 'info', 'msg': f'price={bot_state['last_price']} status={bot_state['status']}'})}\n\n"
            for alert in bot_state.get("alerts", []):
                yield f"data: {json.dumps(alert)}\n\n"
            time.sleep(5)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ============================================================
# Bot loop
# ============================================================
def _try_start_bot_loop():
    global bot_instance, bot_state, bot_loop_loop
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from scripts.run_bot import check_env_mode, load_config_from_env, get_private_key_from_env
        from src.bot import TradingBot

        if not check_env_mode():
            print("[web_dashboard] No credentials -- info-only mode")
            bot_state["status"] = "idle"
            return

        bot_state["has_credentials"] = True
        config = load_config_from_env()
        bot_state["neg_risk"] = config.clob.neg_risk
        private_key = get_private_key_from_env()
        bot = TradingBot(config=config, private_key=private_key)
        bot_instance = bot
        bot_state["bot_initialized"] = True
        bot_state["status"] = "running"
        print("[web_dashboard] Bot initialized")

        bot_loop_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop_loop)
        bot_loop_loop.run_until_complete(_bot_loop(bot))
    except Exception as e:
        bot_state["status"] = "error"
        bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": f"Bot init failed: {e}"})
        print(f"[web_dashboard] Bot not started: {e}")


async def _bot_loop(bot):
    global bot_state, bot_instance
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
            try:
                trades = await bot.get_trades(limit=10)
                if trades:
                    bot_state["recent_trades"] = trades[:10]
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


def _set_strategy(cfg):
    strategy = cfg.get("strategy", "none")
    bot_state["strategy"] = strategy
    bot_state["alerts"].insert(0, {
        "time": time.strftime("%H:%M:%S"),
        "level": "info",
        "msg": f"Strategy set to {strategy}",
    })
    bot_state["alerts"] = bot_state["alerts"][:20]


def main():
    import signal

    def handle_signal(signum, frame):
        global running
        running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Strip em dashes from HTML for Python 3 compat
    global DASHBOARD_HTML
    DASHBOARD_HTML = DASHBOARD_HTML.replace("\u2014", "--")

    bot_thread = threading.Thread(target=_try_start_bot_loop, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"[web_dashboard] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()