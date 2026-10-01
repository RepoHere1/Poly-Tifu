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
import functools
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, request, Response
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config["PREFERRED_URL_SCHEME"] = "https"

API_KEY = os.environ.get("POLY_DASHBOARD_KEY", "")
DEFAULT_COIN = os.environ.get("POLY_DEFAULT_COIN", "BTC")
PAPER_SETTLE_INTERVAL = float(os.environ.get("POLY_PAPER_SETTLE_INTERVAL", "10"))

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
    "strategy_config": {},
    "strategy_running": False,
    "strategy_error": None,
    "recent_trades": [],
    "alerts": [],
    "market": None,
}

running = True
bot_instance = None
bot_loop_loop = None

strategy_instance = None
strategy_future = None
strategy_loop = None
strategy_lock = threading.Lock()
GAMMA_CLIENT_CLS = None

paper_instance = None
TRADING_MODE = "live"
_previous_token_ids = None


def _warm_imports():
    """Import the src package once, on the main thread, before any concurrency.

    src/__init__.py eagerly imports bot, client, crypto and the websocket stack.
    Letting two worker threads trigger those imports at once deadlocks on the
    per-module import locks, so it is done up front and serialized.
    """
    global GAMMA_CLIENT_CLS, paper_instance
    try:
        from src.gamma_client import GammaClient
        GAMMA_CLIENT_CLS = GammaClient
        print("[web_dashboard] src package imported")
    except Exception as e:
        print(f"[web_dashboard] src import failed, market polling disabled: {e}")
        return
    try:
        from src.paper_bot import PaperTradingBot

        paper_instance = PaperTradingBot(
            starting_cash=float(os.environ.get("POLY_PAPER_CASH", "350"))
        )
        threading.Thread(
            target=paper_instance.start_autosave, daemon=True, name="paper-autosave"
        ).start()
        print(
            f"[web_dashboard] Paper engine ready "
            f"(cash ${paper_instance.starting_cash:.2f}, state {paper_instance.state_path})"
        )
    except Exception as e:
        print(f"[web_dashboard] Paper engine failed to start: {e}")

STRATEGY_CATALOG = {
    "none": {
        "label": "None (idle)",
        "module": None,
        "summary": "No automated strategy. The bot only polls status and serves this dashboard.",
        "detail": "Read-only mode. Safe default. Order placement still works from the Order panel.",
        "risk": "None",
        "params": {},
    },
    "flash_crash": {
        "label": "Flash Crash",
        "module": "strategies/flash_crash.py",
        "summary": "Buys the side whose probability just collapsed, betting on a snap-back.",
        "detail": (
            "Streams the live orderbook for both outcomes of the current 15-minute market. "
            "When either probability falls by the drop threshold inside the lookback window, "
            "it market-buys the crashed side and exits on take-profit or stop-loss."
        ),
        "risk": "High -- buys falling knives",
        "params": {"drop_threshold": 0.30, "lookback": 10, "take_profit": 0.10, "stop_loss": 0.05},
    },
    "grid": {
        "label": "Grid",
        "module": "strategies/grid.py",
        "summary": "Places a ladder of limit orders around the mid price to farm oscillation.",
        "detail": (
            "Computes mid from the Up/Down pair, then rebuilds a two-sided ladder across a "
            "percentage range on every tick: buys below mid, sells above. Each rebuild cancels "
            "the previous ladder first, so exposure stays bounded."
        ),
        "risk": "Medium -- many small orders, spread/fee drag",
        "params": {"levels": 5, "range_pct": 2.0, "size": 10.0, "price_offset_pct": 0.1},
    },
    "arb": {
        "label": "Arb",
        "module": "strategies/arb.py",
        "summary": "Trades the Up+Down pair when the two prices stop summing to 1.00.",
        "detail": (
            "For a binary market the two outcomes must total 1.00. When the observed sum drifts "
            "past the threshold the pair is mispriced, so it buys the cheap leg and exits when "
            "the discrepancy normalises."
        ),
        "risk": "Medium -- thin 15m books, fees can eat the edge",
        "params": {"threshold": 0.05, "size": 5.0},
    },
}

MODULE_MAP = [
    ("src/bot.py", "TradingBot", "High-level trading facade. Signs and submits orders, cancels, reads balances/trades."),
    ("src/signer.py", "OrderSigner", "EIP-712 signing against the CLOB V2 exchange domain, with the builder field stamped in."),
    ("src/client.py", "ClobClient / RelayerClient", "Talks to clob.polymarket.com for orders and relayer-v2.polymarket.com for gasless txs."),
    ("src/config.py", "Config / BuilderConfig", "Layered config: env vars beat config.yaml beat defaults."),
    ("src/crypto.py", "KeyManager", "PBKDF2 + Fernet encryption so the private key is never stored in plaintext."),
    ("src/gamma_client.py", "GammaClient", "Discovers the current 15-minute Up/Down market for a coin and parses its token ids and prices."),
    ("src/websocket_client.py", "WebSocketClient", "Live Polymarket market-channel orderbook feed with snapshot + delta handling."),
    ("lib/market_manager.py", "MarketManager", "Keeps the active market and token ids fresh; auto-switches at each 15-minute boundary."),
    ("lib/price_tracker.py", "PriceTracker", "Rolling mid-price history and short-window volatility for strategy signals."),
    ("lib/position_manager.py", "PositionManager", "Tracks open positions and enforces take-profit / stop-loss / max-position limits."),
    ("strategies/", "FlashCrash / Grid / Arb", "The three automated strategies. Each implements on_tick(prices) over the shared base class."),
    ("web_dashboard.py", "Flask app", "This page plus the JSON API, SSE alert stream, and the background bot loop."),
]

ENV_VARS = [
    ("POLY_PRIVATE_KEY", "required to trade", "Wallet private key. Without it the dashboard runs read-only."),
    ("POLY_SAFE_ADDRESS", "required to trade", "Your Polymarket Safe/proxy address -- the maker on every order."),
    ("POLY_BUILDER_CODE", "attribution", "bytes32 code stamped into the signed builder field on every order."),
    ("POLY_BUILDER_API_KEY / _SECRET / _PASSPHRASE", "gasless", "HMAC credentials used by the relayer for gasless cancels/approvals."),
    ("POLY_CHAIN_ID", "137", "Polygon chain id. 137 = mainnet."),
    ("POLY_RPC_URL", "polygon-rpc.com", "Polygon RPC endpoint for on-chain reads."),
    ("POLY_DASHBOARD_KEY", "unset", "When set, every /api/* route requires it as X-API-Key (or ?key= for the SSE stream)."),
    ("POLY_BOT_INTERVAL", "60", "Seconds between bot status poll iterations."),
    ("POLY_DEFAULT_COIN", "BTC", "Coin whose 15-minute market the Market Selector loads first."),
    ("PORT", "8080", "HTTP port. Railway injects this automatically."),
]

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
<title>Poly-Tifu -- Polymarket Trading Bot</title>
<style>
:root { --bg:#0a0f1a; --card:#111827; --card2:#0b1220; --border:#1e293b;
        --text:#e2e8f0; --muted:#94a3b8; --green:#22c55e; --red:#ef4444;
        --blue:#3b82f6; --yellow:#eab308; --purple:#a855f7; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:var(--bg); color:var(--text); min-height:100vh; padding:24px; }
.container { max-width:1180px; margin:0 auto; }
h1 { font-size:1.75rem; font-weight:700; margin-bottom:.25rem; }
.subtitle { color:var(--muted); margin-bottom:1.25rem; font-size:.9rem; }
h2.sec { font-size:1rem; font-weight:600; color:#fff; margin-bottom:.75rem; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px;
        padding:1.25rem; margin-bottom:1rem; }
.card p { font-size:.88rem; line-height:1.6; color:#cbd5e1; }
.card p + p { margin-top:.6rem; }
.card ul { margin:.6rem 0 0 1.1rem; }
.card li { font-size:.87rem; line-height:1.6; color:#cbd5e1; margin-bottom:.3rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
        gap:1rem; margin-bottom:1rem; }
.stat { text-align:center; background:var(--card); border:1px solid var(--border);
        border-radius:12px; padding:1rem; }
.stat-value { font-size:1.5rem; font-weight:700; }
.stat-label { font-size:.75rem; color:var(--muted); margin-top:.35rem;
              text-transform:uppercase; letter-spacing:.04em; }
.green { color:var(--green); } .red { color:var(--red); } .blue { color:var(--blue); }
.yellow { color:var(--yellow); } .purple { color:var(--purple); } .muted { color:var(--muted); }
.badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:.7rem;
         font-weight:700; letter-spacing:.03em; }
.badge.run { background:#064e3b; color:var(--green); }
.badge.idle { background:#1e293b; color:var(--muted); }
.badge.error { background:#450a0a; color:var(--red); }
.badge.warn { background:#3b2e0a; color:var(--yellow); }
code, .mono { background:#020617; padding:2px 6px; border-radius:4px;
              font-size:.8rem; font-family:ui-monospace,Menlo,Consolas,monospace;
              word-break:break-all; }
table { width:100%; border-collapse:collapse; font-size:.84rem; }
th { text-align:left; padding:.5rem; color:var(--muted); font-size:.7rem;
     text-transform:uppercase; letter-spacing:.04em; border-bottom:2px solid var(--border); }
td { padding:.5rem; border-bottom:1px solid var(--border); vertical-align:top; }
tbody tr:hover { background:#0d1526; }
.btn { background:var(--blue); color:#fff; border:none; padding:.5rem 1rem;
       border-radius:8px; cursor:pointer; font-weight:600; font-size:.8rem; }
.btn:hover { opacity:.9; }
.btn:disabled { opacity:.4; cursor:not-allowed; }
.btn.danger { background:var(--red); } .btn.success { background:var(--green); }
.btn.ghost { background:#1e293b; }
.form-row { display:flex; gap:.6rem; flex-wrap:wrap; align-items:flex-end; }
.form-group { display:flex; flex-direction:column; gap:.25rem; }
.form-group label { font-size:.72rem; color:var(--muted); text-transform:uppercase;
                    letter-spacing:.04em; }
.form-group input, .form-group select { background:#020617; border:1px solid var(--border);
  color:var(--text); padding:.45rem .6rem; border-radius:6px; font-size:.85rem; }
.form-group input:focus, .form-group select:focus { border-color:var(--blue); outline:none; }
pre.out { background:#020617; border:1px solid var(--border); border-radius:8px;
          padding:1rem; overflow:auto; font-size:.78rem; margin-top:.5rem;
          max-height:220px; white-space:pre-wrap; }
.alert { padding:.5rem .75rem; border-radius:6px; font-size:.8rem; margin-bottom:.4rem; }
.alert.info { background:#12253f; border-left:3px solid var(--blue); }
.alert.warn { background:#3b2e0a; border-left:3px solid var(--yellow); }
.alert.error { background:#450a0a; border-left:3px solid var(--red); }
.banner { border-radius:10px; padding:.85rem 1rem; margin-bottom:1rem; font-size:.85rem;
          line-height:1.5; border-left:4px solid; }
.banner.live { background:#12253f; border-color:var(--blue); }
.banner.armed { background:#450a0a; border-color:var(--red); }
.banner b { color:#fff; }
.flow li { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.78rem; }
.chip { display:inline-block; padding:2px 8px; border-radius:12px; font-size:.7rem;
        font-weight:600; background:#1e293b; color:var(--muted); margin-right:.3rem; }
.chip.risk-high { background:#450a0a; color:var(--red); }
.chip.risk-medium { background:#3b2e0a; color:var(--yellow); }
details { border-top:1px solid var(--border); padding:.6rem 0; }
summary { cursor:pointer; font-size:.88rem; color:#fff; font-weight:600; }
footer { margin-top:2rem; text-align:center; color:var(--muted); font-size:.75rem; }
.note { font-size:.78rem; color:var(--muted); margin-top:.5rem; line-height:1.5; }
.masthead { display:flex; justify-content:space-between; align-items:flex-start;
            gap:1.25rem; flex-wrap:nowrap; margin-bottom:1rem; }
.masthead > div:first-child { flex:1 1 auto; min-width:0; }
.masthead h1 { margin-bottom:.25rem; }
.subtitle { color:var(--muted); margin-bottom:0; font-size:.9rem; }
.mode-toggle { display:flex; align-items:center; gap:.6rem; background:var(--card);
   border:1px solid var(--border); border-radius:12px; padding:.7rem .9rem;
   flex:0 0 auto; margin-left:auto; }
.mode-toggle .lbl { font-size:.7rem; color:var(--muted); text-transform:uppercase;
   letter-spacing:.05em; }
.mode-btn { display:flex; align-items:center; gap:.45rem; border:none; cursor:pointer;
   padding:.5rem .95rem; border-radius:9px; font-weight:700; font-size:.8rem;
   letter-spacing:.04em; transition:opacity .15s; }
.mode-btn:hover { opacity:.85; }
.mode-btn.live { background:#450a0a; color:#fca5a5; }
.mode-btn.live.on { background:var(--red); color:#fff; }
.mode-btn.dry { background:#064e3b; color:#86efac; }
.mode-btn.dry.on { background:var(--green); color:#04231a; }
.mode-btn:disabled { opacity:.35; cursor:not-allowed; }
.mode-state { font-size:.68rem; color:var(--muted); margin-top:.35rem; text-align:right; }
</style>
</head>
<body>
<div class="container">

<div class="masthead">
  <div>
    <h1>Poly-Tifu</h1>
    <p class="subtitle">Automated trading bot for Polymarket 15-minute crypto Up/Down markets &middot; CLOB V2 &middot; EIP-712 &middot; optional gasless</p>
  </div>
  <div class="mode-toggle">
    <div>
      <div class="lbl">Trading mode</div>
      <div class="mode-state" id="modeState">connecting...</div>
    </div>
    <button class="mode-btn dry" id="btnDry" onclick="setMode('dry')">DRY &middot; simulated</button>
    <button class="mode-btn live" id="btnLive" onclick="setMode('live')">LIVE &middot; real money</button>
  </div>
</div>

<div id="modeBanner" class="banner live">Checking credentials...</div>

<div class="grid">
  <div class="stat"><div class="stat-value"><span class="badge idle" id="statusBadge">IDLE</span></div><div class="stat-label">Bot</div></div>
  <div class="stat"><div class="stat-value" id="creds">--</div><div class="stat-label">Can Trade</div></div>
  <div class="stat"><div class="stat-value" id="lastPrice">--</div><div class="stat-label">Up Price</div></div>
  <div class="stat"><div class="stat-value" id="openCount">0</div><div class="stat-label">Open Orders</div></div>
  <div class="stat"><div class="stat-value" id="tradeCount">0</div><div class="stat-label">Trades</div></div>
  <div class="stat"><div class="stat-value" id="strategyVal">none</div><div class="stat-label">Strategy</div></div>
</div>

<div class="card">
  <h2 class="sec">What this thing actually is</h2>
  <p>Polymarket runs fast binary markets like <em>&quot;Bitcoin Up or Down, 6:15PM&ndash;6:30PM ET&quot;</em>. Each one asks a single yes/no question: did the Chainlink BTC/USD TWAP at the end of the window end up higher than it started? Exactly one side pays $1.00, the other pays $0.00.</p>
  <p>A share's price between $0 and $1 <strong>is</strong> the crowd's live estimate of that chance. Buy at $0.45 and the outcome resolving your way turns it into $1.00; the other way it is worth nothing. So the entire game is buying outcomes that are mispriced relative to real information.</p>
  <p>Poly-Tifu is the machine that watches for those mispricings and acts on them. It keeps a live WebSocket feed of both orderbooks, computes mid-prices and short-window volatility, and runs a trading strategy that places and cancels real limit orders on the Polymarket CLOB.</p>
  <p><strong>Where the edge comes from:</strong> these markets expire every 15 minutes and are thin, so orderbooks routinely lag, cross, and gap right before a boundary. A strategy that reacts faster than a human clicking in a browser can capture part of that.</p>
  <p><strong>The two halves:</strong> reading market data is free and public (Gamma API + WebSocket, no credentials). Placing orders requires your wallet &mdash; a private key and your Polymarket Safe address &mdash; and moves real USDC.</p>
  <p><strong>DRY and LIVE:</strong> the toggle at the top right switches where orders go. <strong>DRY</strong> routes them to a simulator that starts with $350 of fake cash and fills your orders against the <em>real</em> Polymarket order book, so you can watch exactly how a strategy behaves without risking anything &mdash; and it keeps that balance across restarts. <strong>LIVE</strong> routes the identical orders to the real CLOB with your real wallet. Market data is real and identical in both modes; only the money is fake.</p>
  <p><strong>Not financial advice.</strong> These are short-dated binary bets on an asset price, the books are thin, and most retail attempts lose money. Run it small, and get comfortable in DRY first.</p>
</div>

<div class="card">
  <h2 class="sec">Live market</h2>
  <div class="form-row">
    <div class="form-group">
      <label>Coin</label>
      <select id="marketCoin">
        <option>BTC</option><option>ETH</option><option>SOL</option><option>XRP</option>
      </select>
    </div>
    <button class="btn ghost" onclick="loadMarkets()">Refresh</button>
  </div>
  <div id="marketsTable" style="margin-top:.85rem"></div>
  <p class="note">Token IDs come straight from Polymarket. Click one to load it into the order form below.</p>
</div>

<div class="card">
  <h2 class="sec">Place a manual order</h2>
  <div class="form-row">
    <div class="form-group"><label>Side</label>
      <select id="orderSide"><option>BUY</option><option>SELL</option></select></div>
    <div class="form-group"><label>Token ID</label>
      <input id="orderToken" style="width:300px" placeholder="pick from the market above"></div>
    <div class="form-group"><label>Price (0-1)</label>
      <input id="orderPrice" type="number" step="0.01" min="0" max="1" value="0.50" style="width:90px"></div>
    <div class="form-group"><label>Size</label>
      <input id="orderSize" type="number" step="1" min="5" value="10" style="width:90px"></div>
    <div class="form-group"><label>Type</label>
      <select id="orderType"><option>GTC</option><option>GTD</option><option>FOK</option></select></div>
    <div class="form-group"><label>Neg Risk</label>
      <select id="orderNegRisk"><option value="false">No</option><option value="true">Yes</option></select></div>
    <button class="btn success" id="btnPlace" onclick="placeOrder()">Place</button>
    <button class="btn danger" id="btnCancel" onclick="cancelOrder()">Cancel</button>
    <button class="btn ghost" id="btnCancelAll" onclick="cancelAllOrders()">Cancel all</button>
  </div>
  <p class="note">Signed locally with your private key via EIP-712 against the CLOB V2 exchange domain. Min size is 5 shares. Neg Risk switches the order to the Neg Risk exchange address.</p>
  <pre class="out" id="orderResult">idle</pre>
</div>

<div class="card">
  <h2 class="sec">Automated strategy</h2>
  <div class="form-row">
    <div class="form-group"><label>Strategy</label>
      <select id="strategySelect" onchange="syncStrategyForm()">
        <option value="none">none</option>
        <option value="flash_crash">Flash Crash</option>
        <option value="grid">Grid</option>
        <option value="arb">Arb</option>
      </select></div>
    <div class="form-group" id="fCoin" style="display:none"><label>Coin</label>
      <select id="stratCoin"><option>BTC</option><option>ETH</option><option>SOL</option><option>XRP</option></select></div>
    <div class="form-group" id="fLevels" style="display:none"><label>Grid Levels</label>
      <input id="gridLevels" type="number" value="5" min="1" style="width:80px"></div>
    <div class="form-group" id="fRange" style="display:none"><label>Range %</label>
      <input id="gridRange" type="number" step="0.1" value="2" style="width:80px"></div>
    <div class="form-group" id="fDrop" style="display:none"><label>Drop Threshold</label>
      <input id="fcDrop" type="number" step="0.01" value="0.30" style="width:90px"></div>
    <div class="form-group" id="fLookback" style="display:none"><label>Lookback (s)</label>
      <input id="fcLookback" type="number" value="10" style="width:90px"></div>
    <div class="form-group" id="fThresh" style="display:none"><label>Arb Threshold</label>
      <input id="arbThresh" type="number" step="0.01" value="0.05" style="width:90px"></div>
    <button class="btn" onclick="setStrategy()">Apply</button>
  </div>
  <p class="note" id="strategyNote">No strategy running.</p>
</div>

<div class="card">
  <h2 class="sec">Open orders</h2>
  <div id="openOrdersTable"></div>
</div>

<div class="card">
  <h2 class="sec">Recent trades</h2>
  <div id="tradesTable"></div>
</div>

<div class="card">
  <h2 class="sec">Balance &amp; simulated account</h2>
  <div class="form-row" style="margin-bottom:.6rem">
    <button class="btn ghost" onclick="resetPaper()">Reset simulated account</button>
    <span class="note" style="margin:0">Only affects DRY mode. The simulated balance, positions, resting orders and fills persist across restarts and redeploys.</span>
  </div>
  <pre class="out" id="balance">no balance yet</pre>
</div>

<div class="card">
  <h2 class="sec">Event stream (SSE)</h2>
  <div id="alertLog"><p class="note">connecting...</p></div>
  <p class="note">Server-sent events pushed every 5 seconds, plus one-off alerts for strategy and bot state changes.</p>
</div>

<div class="card">
  <h2 class="sec">Activity log</h2>
  <pre class="out" id="activity">no activity yet</pre>
</div>

<div class="card">
  <h2 class="sec">How the code is wired</h2>
  <ol class="flow">
    <li>1. Gamma API (<span class="mono">gamma-api.polymarket.com</span>) tells us which 15-minute market is live and what its two token IDs are.</li>
    <li>2. WebSocket market channel streams orderbook snapshots and deltas for both tokens.</li>
    <li>3. <span class="mono">lib/price_tracker.py</span> keeps rolling mid-prices; <span class="mono">lib/market_manager.py</span> rolls over to the next market on the 15-minute boundary.</li>
    <li>4. The strategy's <span class="mono">on_tick(prices)</span> decides something is mispriced.</li>
    <li>5. <span class="mono">src/bot.py</span> builds an order, <span class="mono">src/signer.py</span> EIP-712 signs it, <span class="mono">src/client.py</span> POSTs it to <span class="mono">clob.polymarket.com</span>.</li>
    <li>6. The exchange matches it against the book; your Safe fills it against your USDC balance.</li>
    <li>7. Every step above is also reported back here over the JSON API and the SSE stream.</li>
  </ol>
</div>

<div class="card">
  <h2 class="sec">Modules</h2>
  <div id="moduleTable"></div>
</div>

<div class="card">
  <h2 class="sec">Strategies</h2>
  <div id="strategyTable"></div>
</div>

<div class="card">
  <h2 class="sec">Environment variables</h2>
  <div id="envTable"></div>
</div>

<div class="card">
  <h2 class="sec">Run it locally</h2>
  <pre class="out">pip install -r requirements.txt
python web_dashboard.py            # this page + bot loop, no creds needed

# with trading, add to .env:
POLY_PRIVATE_KEY=0x...
POLY_SAFE_ADDRESS=0x...
POLY_BUILDER_CODE=0x...            # bytes32 attribution on every order</pre>
</div>

<footer>Poly-Tifu &middot; Polymarket CLOB V2 &middot; Polygon chain 137 &middot; Not financial advice</footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const AUTH = new URLSearchParams(location.search).get('key') || '';
const H = AUTH ? { 'X-API-Key': AUTH } : {};
const J = { ...H, 'Content-Type': 'application/json' };

function txt(id, v) { $(id).textContent = (v === null || v === undefined || v === '') ? '--' : String(v); }
function say(id, v) { $(id).textContent = typeof v === 'string' ? v : JSON.stringify(v, null, 2); }

async function loadData() {
  try {
    const d = await (await fetch('/api/status', { headers: H })).json();
    if (d.error) { say('activity', d.error); return; }

    const badge = $('statusBadge');
    badge.textContent = (d.status || 'idle').toUpperCase();
    badge.className = 'badge ' + (d.status === 'running' ? 'run' : d.status === 'error' ? 'error' : 'idle');

    txt('creds', d.has_credentials ? 'YES' : 'NO');
    $('creds').className = 'stat-value ' + (d.has_credentials ? 'green' : 'yellow');
    txt('lastPrice', d.last_price);
    txt('openCount', d.open_orders_count || 0);
    txt('tradeCount', (d.recent_trades || []).length);
    txt('strategyVal', d.strategy || 'none');
    $('strategyVal').className = 'stat-value ' + (d.strategy_running ? 'green' : 'purple');

    const armed = d.has_credentials;
    const mode = d.mode || 'dry';
    $('btnDry').classList.toggle('on', mode === 'dry');
    $('btnLive').classList.toggle('on', mode === 'live');
    $('btnLive').disabled = !armed;
    $('btnLive').title = armed ? 'Place real orders' : 'No wallet credentials loaded';
    $('modeState').textContent = mode === 'live'
      ? 'LIVE orders -- real USDC'
      : 'DRY orders -- simulated, ' + (d.paper ? ('$' + Number(d.paper.starting_cash).toFixed(0)) : '');

    if (mode === 'live') {
      $('modeBanner').className = 'banner armed';
      $('modeBanner').innerHTML = '<b>LIVE -- real money.</b> Orders are signed with your wallet and sent to the Polymarket CLOB. Strategies you start below trade real USDC.';
    } else if (armed) {
      $('modeBanner').className = 'banner live';
      $('modeBanner').innerHTML = '<b>DRY -- simulated.</b> Wallet credentials are loaded, so you can switch to LIVE at any time. Orders placed now are simulated against real market data and cost nothing. Starting cash $' + (d.paper ? Number(d.paper.starting_cash).toFixed(2) : '350') + '.';
    } else {
      $('modeBanner').className = 'banner live';
      $('modeBanner').innerHTML = '<b>DRY -- read-only.</b> No <span class="mono">POLY_PRIVATE_KEY</span> / <span class="mono">POLY_SAFE_ADDRESS</span>, so LIVE is unavailable and orders are simulated against real market data with a fake balance.';
    }
    ['btnPlace', 'btnCancel', 'btnCancelAll'].forEach(b => { if ($(b)) $(b).disabled = !armed; });

    $('openOrdersTable').innerHTML = (d.open_orders && d.open_orders.length)
      ? '<table><thead><tr><th>Order ID</th><th>Side</th><th>Price</th><th>Size</th><th>Status</th></tr></thead><tbody>'
        + d.open_orders.map(o => '<tr><td><code>' + (o.id || '-') + '</code></td><td>' + (o.side || '-')
        + '</td><td>' + (o.price ?? '-') + '</td><td>' + (o.original_size ?? o.size ?? '-')
        + '</td><td>' + (o.status || '-') + '</td></tr>').join('') + '</tbody></table>'
      : '<p class="note">none</p>';

    $('tradesTable').innerHTML = (d.recent_trades && d.recent_trades.length)
      ? '<table><thead><tr><th>Time</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th></tr></thead><tbody>'
        + d.recent_trades.map(t => '<tr><td>' + (t.timestamp || t.created_at || '-') + '</td><td>'
        + (t.market || t.title || t.token_id || '-') + '</td><td>' + (t.side || '-') + '</td><td>'
        + (t.price ?? '-') + '</td><td>' + (t.size ?? '-') + '</td></tr>').join('') + '</tbody></table>'
      : '<p class="note">none</p>';

    say('balance', d.balance ? d.balance : 'no balance yet');

    const act = d.recent_activity || [];
    say('activity', act.length ? act.map(e => (e.time || '') + '  ' + (e.error || '')).join(String.fromCharCode(10)) : 'no activity yet');

    const det = d.strategy_detail || {};
    $('strategyNote').textContent = det.detail
      ? (d.strategy + ': ' + det.summary + (d.strategy_error ? ' -- ' + d.strategy_error : (d.strategy_running ? ' -- running.' : ' -- selected, not running.')))
      : 'No strategy running.';
  } catch (e) { say('activity', 'status fetch failed: ' + e.message); }
}

async function setMode(mode) {
  const btn = mode === 'live' ? $('btnLive') : $('btnDry');
  if (mode === 'live') {
    const msg = 'LIVE mode places REAL orders with real USDC.'
      + String.fromCharCode(10) + String.fromCharCode(10)
      + 'Are you sure?';
    if (!confirm(msg)) return;
  }
  btn.disabled = true;
  try {
    const r = await fetch('/api/mode', { method: 'POST', headers: J, body: JSON.stringify({ mode }) });
    const d = await r.json();
    if (!r.ok || !d.success) alert(d.message || 'mode switch failed');
  } catch (e) { alert('mode switch failed: ' + e.message); }
  btn.disabled = false;
  loadData();
}

async function resetPaper() {
  if (!confirm('Reset the simulated account back to its starting cash?')) return;
  try {
    const r = await fetch('/api/paper/reset', { method: 'POST', headers: H });
    const d = await r.json();
    if (!r.ok) alert(d.message || 'reset failed');
  } catch (e) { alert('reset failed: ' + e.message); }
  loadData();
}

async function loadMarkets() {
  const coin = $('marketCoin').value;
  const el = $('marketsTable');
  el.innerHTML = '<p class="note">loading ' + coin + ' market...</p>';
  try {
    const r = await fetch('/api/markets?coin=' + coin, { headers: H });
    const m = await r.json();
    if (!r.ok || m.error) { el.innerHTML = '<p class="note">' + (m.error || 'no market') + '</p>'; return; }
    const line = (name, o) => '<tr><td>' + name + '</td><td><code>' + (o.id || '-')
      + '</code></td><td>' + (o.price ?? '-') + '</td><td>' + (o.last_trade ?? '-')
      + '</td><td><button class="btn ghost" data-token="'
      + (o.id || '') + '">use</button></td></tr>';
    el.innerHTML =
      '<p style="font-size:.85rem;margin-bottom:.6rem"><strong>' + (m.question || '-') + '</strong></p>'
      + '<table><thead><tr><th>Outcome</th><th>Token ID</th><th>Mark</th><th>Last trade</th><th></th></tr></thead><tbody>'
      + line('UP', m.up || {}) + line('DOWN', m.down || {}) + '</tbody></table>'
      + '<p class="note">Book top: bid ' + (m.best_bid ?? '-') + ' / ask ' + (m.best_ask ?? '-')
      + ' &middot; spread ' + (m.spread ?? '-') + ' &middot; ends ' + (m.end_date || '-')
      + ' &middot; volume ' + (m.volume ?? '-') + ' &middot; accepting orders: '
      + (m.accepting_orders ? 'yes' : 'no')
      + ' &middot; mark and last trade come from Gamma and can lag the live book, so price limits off the bid/ask.</p>';
  } catch (e) { el.innerHTML = '<p class="note">market fetch failed: ' + e.message + '</p>'; }
}

function useToken(id) {
  if (!id) return;
  $('orderToken').value = id;
  $('orderToken').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

$('marketsTable').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-token]');
  if (btn) useToken(btn.dataset.token);
});

async function placeOrder() {
  say('orderResult', 'signing and submitting...');
  const body = {
    side: $('orderSide').value,
    token_id: $('orderToken').value,
    price: parseFloat($('orderPrice').value),
    size: parseFloat($('orderSize').value),
    order_type: $('orderType').value,
    neg_risk: $('orderNegRisk').value === 'true'
  };
  try {
    const r = await fetch('/api/order', { method: 'POST', headers: J, body: JSON.stringify(body) });
    say('orderResult', await r.json());
  } catch (e) { say('orderResult', 'failed: ' + e.message); }
  loadData();
}

async function cancelOrder() {
  const id = prompt('Order ID to cancel:');
  if (!id) return;
  try {
    const r = await fetch('/api/order/' + encodeURIComponent(id) + '/cancel', { method: 'POST', headers: H });
    say('orderResult', await r.json());
  } catch (e) { say('orderResult', 'failed: ' + e.message); }
  loadData();
}

async function cancelAllOrders() {
  if (!confirm('Cancel every open order?')) return;
  try {
    const r = await fetch('/api/orders/cancel-all', { method: 'POST', headers: H });
    say('orderResult', await r.json());
  } catch (e) { say('orderResult', 'failed: ' + e.message); }
  loadData();
}

function syncStrategyForm() {
  const s = $('strategySelect').value;
  $('fCoin').style.display = s === 'none' ? 'none' : 'flex';
  $('fLevels').style.display = s === 'grid' ? 'flex' : 'none';
  $('fRange').style.display = s === 'grid' ? 'flex' : 'none';
  $('fDrop').style.display = s === 'flash_crash' ? 'flex' : 'none';
  $('fLookback').style.display = s === 'flash_crash' ? 'flex' : 'none';
  $('fThresh').style.display = s === 'arb' ? 'flex' : 'none';
}

async function setStrategy() {
  const s = $('strategySelect').value;
  const cfg = { coin: $('stratCoin').value };
  if (s === 'grid') { cfg.levels = parseInt($('gridLevels').value, 10); cfg.range = parseFloat($('gridRange').value); }
  if (s === 'flash_crash') { cfg.drop_threshold = parseFloat($('fcDrop').value); cfg.lookback = parseInt($('fcLookback').value, 10); }
  if (s === 'arb') { cfg.threshold = parseFloat($('arbThresh').value); }
  try {
    const r = await fetch('/api/strategy', { method: 'POST', headers: J, body: JSON.stringify({ strategy: s, config: cfg }) });
    const d = await r.json();
    $('strategyNote').textContent = d.message || JSON.stringify(d);
  } catch (e) { $('strategyNote').textContent = 'failed: ' + e.message; }
  loadData();
}

async function loadSystem() {
  try {
    const s = await (await fetch('/api/system')).json();
    $('moduleTable').innerHTML = '<table><thead><tr><th>Path</th><th>Contains</th><th>Role</th></tr></thead><tbody>'
      + s.modules.map(m => '<tr><td><code>' + m.path + '</code></td><td>' + m.symbols + '</td><td>' + m.purpose + '</td></tr>').join('')
      + '</tbody></table>';

    $('strategyTable').innerHTML = '<table><thead><tr><th>Name</th><th>What it does</th><th>Risk</th></tr></thead><tbody>'
      + Object.entries(s.strategies).filter(([k]) => k !== 'none').map(([k, v]) =>
        '<tr><td><strong>' + v.label + '</strong><br><code>' + (v.module || '') + '</code></td><td>'
        + v.detail + '<br><span class="chip">' + Object.entries(v.params).map(([a, b]) => a + '=' + b).join(' ') + '</span></td><td>'
        + v.risk + '</td></tr>').join('')
      + '</tbody></table>';

    $('envTable').innerHTML = '<table><thead><tr><th>Variable</th><th>Default</th><th>Purpose</th></tr></thead><tbody>'
      + s.env.map(e => '<tr><td><code>' + e.name + '</code></td><td>' + e.default + '</td><td>' + e.purpose + '</td></tr>').join('')
      + '</tbody></table>';
  } catch (e) { /* system panel is optional */ }
}

const es = new EventSource('/api/stream' + (AUTH ? '?key=' + encodeURIComponent(AUTH) : ''));
es.onmessage = (e) => {
  let data; try { data = JSON.parse(e.data); } catch (_) { return; }
  const log = $('alertLog');
  if (log.firstElementChild && log.firstElementChild.tagName === 'P') log.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'alert ' + (data.level || 'info');
  div.textContent = (data.time || '') + '  [' + (data.level || 'info') + ']  ' + (data.msg || '');
  log.prepend(div);
  while (log.children.length > 25) log.removeChild(log.lastChild);
};
es.onerror = () => { $('alertLog').insertAdjacentHTML('afterbegin', '<p class="note">stream reconnecting...</p>'); };

setInterval(loadData, 5000);
loadData();
loadSystem();
loadMarkets();
syncStrategyForm();
</script>
</body>
</html>
"""

# ============================================================
def require_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if API_KEY:
            key = request.headers.get("X-API-Key", "") or request.args.get("key", "")
            if key != API_KEY:
                return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# Trading engine facade
# ============================================================
MODE_FILE_ENV = "POLY_TRADING_MODE"
_mode_lock = threading.Lock()


def _mode_state_path() -> Path:
    explicit = os.environ.get("POLY_PAPER_STATE")
    if explicit:
        return Path(explicit).with_name("trading_mode.json")
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return Path(mount) / "trading_mode.json"
    return Path(__file__).parent / "data" / "trading_mode.json"


def _load_mode() -> str:
    default = os.environ.get(MODE_FILE_ENV, "live").lower()
    if default not in ("live", "dry"):
        default = "live"
    try:
        path = _mode_state_path()
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8")).get("mode")
            if saved in ("live", "dry"):
                return saved
    except Exception:
        pass
    return default


def _save_mode(mode: str) -> None:
    try:
        path = _mode_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"mode": mode}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def active_bot():
    """
    Return the engine that should service the next order.

    DRY mode routes to the paper simulator, LIVE mode to the real CLOB bot.
    Falls back to paper if the live bot is missing or uninitialized, so the
    page never silently pretends to trade for real when it cannot.
    """
    with _mode_lock:
        if TRADING_MODE == "live" and bot_instance is not None and bot_state["bot_initialized"]:
            return bot_instance
        return paper_instance


def active_mode() -> str:
    """Return 'live' only when a real, initialized bot is actually serving."""
    with _mode_lock:
        if TRADING_MODE == "live" and bot_instance is not None and bot_state["bot_initialized"]:
            return "live"
        return "dry"


# ============================================================
# Routes
# ============================================================
@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@app.route("/api/system")
def api_system():
    return jsonify(
        {
            "name": "Poly-Tifu",
            "version": "2",
            "exchange": "Polymarket CLOB V2",
            "chain_id": 137,
            "builder_program": bool(os.environ.get("POLY_BUILDER_CODE")),
            "auth_required": bool(API_KEY),
            "modules": [
                {"path": p, "symbols": s, "purpose": d} for p, s, d in MODULE_MAP
            ],
            "strategies": {
                key: {
                    "label": v["label"],
                    "module": v["module"],
                    "summary": v["summary"],
                    "detail": v["detail"],
                    "risk": v["risk"],
                    "params": v["params"],
                }
                for key, v in STRATEGY_CATALOG.items()
            },
            "env": [{"name": n, "default": d, "purpose": p} for n, d, p in ENV_VARS],
        }
    )


@app.route("/api/status")
@require_auth
def api_status():
    if active_bot() is paper_instance:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_refresh_engine_state())
        except Exception:
            pass
        finally:
            loop.close()
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
            "strategy_running": bot_state["strategy_running"],
            "strategy_config": bot_state["strategy_config"],
            "strategy_error": bot_state["strategy_error"],
            "strategy_detail": STRATEGY_CATALOG.get(bot_state["strategy"], {}),
            "recent_trades": bot_state["recent_trades"][:10],
            "market": bot_state["market"],
            "mode": active_mode(),
            "requested_mode": TRADING_MODE,
            "live_available": bool(bot_state["bot_initialized"]),
            "paper": {
                "starting_cash": paper_instance.starting_cash if paper_instance else None,
                "state_path": str(paper_instance.state_path) if paper_instance else None,
            },
        }
    )


@app.route("/api/strategy", methods=["POST"])
@require_auth
def api_set_strategy():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("strategy", "none")).lower()
    config = data.get("config", {}) or {}

    if name not in STRATEGY_CATALOG:
        return jsonify({"success": False, "strategy": name,
                        "message": f"Unknown strategy. Pick one of: {', '.join(STRATEGY_CATALOG)}"}), 400

    if name == "none":
        _stop_strategy()
        _set_strategy({"strategy": "none", "config": {}})
        return jsonify({"success": True, "strategy": "none", "running": False,
                        "message": "Strategy stopped."})

    if active_bot() is None:
        _set_strategy({"strategy": name, "config": config})
        bot_state["strategy_error"] = "Selected but NOT running: no trading engine available."
        return jsonify({"success": False, "strategy": name, "running": False,
                        "message": bot_state["strategy_error"]}), 409

    try:
        _start_strategy(name, config)
    except Exception as e:
        bot_state["strategy_error"] = str(e)
        return jsonify({"success": False, "strategy": name, "running": False,
                        "message": str(e)}), 500

    return jsonify({"success": True, "strategy": name, "running": bot_state["strategy_running"],
                    "config": bot_state["strategy_config"], "message": f"{name} strategy running."})


@app.route("/api/mode", methods=["POST"])
@require_auth
def api_set_mode():
    """Switch between LIVE (real orders) and DRY (simulated orders)."""
    global TRADING_MODE
    data = request.get_json(force=True, silent=True) or {}
    requested = str(data.get("mode", "")).lower()
    if requested not in ("live", "dry"):
        return jsonify({"success": False, "message": "mode must be 'live' or 'dry'"}), 400

    if requested == "live" and not bot_state["bot_initialized"]:
        return jsonify(
            {
                "success": False,
                "mode": active_mode(),
                "message": (
                    "Cannot go LIVE: no wallet credentials are loaded, so real orders "
                    "cannot be signed. Set POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS."
                ),
            }
        ), 409

    with _mode_lock:
        TRADING_MODE = requested
    _save_mode(requested)
    _push_alert(f"Mode switched to {requested.upper()}.", "warn")

    return jsonify(
        {
            "success": True,
            "mode": active_mode(),
            "requested": requested,
            "paper_cash": paper_instance.starting_cash if paper_instance else None,
        }
    )


@app.route("/api/paper/reset", methods=["POST"])
@require_auth
def api_paper_reset():
    """Reset the simulated account back to its starting balance."""
    if paper_instance is None:
        return jsonify({"success": False, "message": "Paper engine unavailable"}), 503
    paper_instance.reset()
    _push_alert("Paper account reset to starting cash.", "warn")
    return jsonify({"success": True, "cash": paper_instance.cash})


@app.route("/api/order", methods=["POST"])
@require_auth
def api_place_order():
    data = request.get_json(force=True, silent=True) or {}
    bot = active_bot()
    if bot is None:
        return jsonify({"success": False, "message": "No trading engine available"}), 400
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
            bot.place_order(
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
            {"time": time.strftime("%H:%M:%S"),
             "error": f"[{active_mode().upper()}] Order {side} {size}@{price} on {token_id[:16]}..."}
        )
        return jsonify(result.__dict__ if hasattr(result, "__dict__") else result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/order/<order_id>/cancel", methods=["POST"])
@require_auth
def api_cancel_order(order_id):
    bot = active_bot()
    if bot is None:
        return jsonify({"success": False, "message": "No trading engine available"}), 400
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(bot.cancel_order(order_id))
        loop.close()
        return jsonify(result.__dict__ if hasattr(result, "__dict__") else result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/orders/cancel-all", methods=["POST"])
@require_auth
def api_cancel_all():
    bot = active_bot()
    if bot is None:
        return jsonify({"success": False, "message": "No trading engine available"}), 400
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(bot.cancel_all_orders())
        loop.close()
        return jsonify(result.__dict__ if hasattr(result, "__dict__") else result)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/markets")
@require_auth
def api_markets():
    coin = request.args.get("coin", DEFAULT_COIN).upper()
    if GAMMA_CLIENT_CLS is None:
        return jsonify({"error": "Market client unavailable"}), 503
    try:
        client = GAMMA_CLIENT_CLS()
        info = client.get_market_info(coin)
        if not info:
            return jsonify({"error": f"No active {coin} 15-minute market right now."}), 404
        raw = info.get("raw", {})
        token_ids = info.get("token_ids", {})
        prices = info.get("prices", {})
        return jsonify(
            {
                "coin": coin,
                "question": info.get("question"),
                "slug": info.get("slug"),
                "end_date": info.get("end_date"),
                "accepting_orders": info.get("accepting_orders"),
                "best_bid": info.get("best_bid"),
                "best_ask": info.get("best_ask"),
                "spread": info.get("spread"),
                "volume": raw.get("volume"),
                "liquidity": raw.get("liquidity"),
                "condition_id": raw.get("conditionId"),
                "up": {"id": token_ids.get("up"), "price": prices.get("up"),
                       "last_trade": raw.get("lastTradePrice")},
                "down": {"id": token_ids.get("down"), "price": prices.get("down"),
                         "last_trade": raw.get("lastTradePrice")},
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trades")
@require_auth
def api_trades():
    bot = active_bot()
    if bot is None:
        return jsonify([])
    token_id = request.args.get("token_id", None)
    limit = int(request.args.get("limit", 20))
    try:
        loop = asyncio.new_event_loop()
        trades = loop.run_until_complete(bot.get_trades(token_id, limit))
        loop.close()
        return jsonify(trades)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stream")
@require_auth
def api_stream():
    def generate():
        last_price = bot_state["last_price"]
        last_status = bot_state["status"]
        sent_alerts = 0
        while running:
            price = bot_state["last_price"]
            status = bot_state["status"]
            alerts = bot_state["alerts"]
            frame = {
                "time": time.strftime("%H:%M:%S"),
                "price": price,
                "status": status,
                "strategy": bot_state["strategy"],
                "level": "info",
                "msg": "price={} status={} strategy={}".format(price, status, bot_state["strategy"]),
            }
            if price != last_price or status != last_status:
                frame["level"] = "warn" if status == "error" else "info"
            last_price = price
            last_status = status
            yield f"data: {json.dumps(frame)}\n\n"

            if len(alerts) > sent_alerts:
                for alert in alerts[sent_alerts:]:
                    yield f"data: {json.dumps(alert)}\n\n"
                sent_alerts = len(alerts)
            elif len(alerts) < sent_alerts:
                sent_alerts = len(alerts)

            time.sleep(5)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


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

        bot_loop_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop_loop)
        threading.Thread(target=_market_loop, daemon=True, name="market-loop").start()
        threading.Thread(target=_status_loop, daemon=True, name="status-loop").start()

        from scripts.run_bot import check_env_mode, load_config_from_env, get_private_key_from_env
        from src.bot import TradingBot

        if not check_env_mode():
            print("[web_dashboard] No credentials -- read-only mode")
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

        bot_loop_loop.run_until_complete(_bot_loop(bot))
    except Exception as e:
        bot_state["status"] = "error"
        bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": f"Bot init failed: {e}"})
        print(f"[web_dashboard] Bot not started: {e}")


def _market_loop():
    """Polls the public 15-minute market. Runs with or without credentials."""
    asyncio.run(_market_loop_async())


async def _market_loop_async():
    global _previous_token_ids
    coin = DEFAULT_COIN
    client_cls = GAMMA_CLIENT_CLS
    if client_cls is None:
        return
    current_slug = None
    while running:
        try:
            def _fetch():
                return client_cls().get_market_info(coin)

            info = await asyncio.to_thread(_fetch)
            if info:
                prices = info.get("prices", {})
                up = prices.get("up")
                bot_state["last_price"] = str(up) if up is not None else None
                token_ids = info.get("token_ids", {})
                slug = info.get("slug")
                bot_state["market"] = {
                    "coin": coin,
                    "question": info.get("question"),
                    "slug": slug,
                    "end_date": info.get("end_date"),
                    "up_price": up,
                    "down_price": prices.get("down"),
                    "best_bid": info.get("best_bid"),
                    "best_ask": info.get("best_ask"),
                    "token_ids": token_ids,
                }

                if paper_instance is not None and token_ids:
                    if current_slug and slug and slug != current_slug:
                        stale = _previous_token_ids
                        if stale:
                            res = paper_instance.expire_market(stale)
                            _push_alert(
                                "Market rolled {}: paper cancelled {} order(s), settled {} position(s).".format(
                                    slug, res["cancelled"], res["settled"]
                                ),
                                "info",
                            )
                        _previous_token_ids = None

                    _previous_token_ids = token_ids
                    current_slug = slug
                    await _settle_paper(token_ids)

        except Exception as e:
            bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": f"Market poll: {e}"})
            if len(bot_state["errors"]) > 50:
                bot_state["errors"] = bot_state["errors"][-50:]
        await asyncio.sleep(PAPER_SETTLE_INTERVAL)


async def _settle_paper(token_ids):
    """Price resting paper orders off the real CLOB books and fill the crosses."""
    if not token_ids:
        return

    def _books():
        out = {}
        for key, tid in token_ids.items():
            if not tid:
                continue
            book = paper_instance.fetch_book(tid)
            quote = paper_instance.top_of_book(book)
            if quote.get("bid") is not None or quote.get("ask") is not None:
                out[tid] = quote
        return out

    books = await asyncio.to_thread(_books)
    if not books:
        return
    fills = paper_instance.settle(books)
    for f in fills:
        _push_alert(
            "PAPER FILL {} {} @ {} ({} shares)".format(
                f["side"], f["token_id"][:10], f["price"], f["size"]
            ),
            "info",
        )


def _status_loop():
    """Polls the active engine for orders, balance and fills.

    Runs in DRY mode too, so the simulated account is always reflected.
    """
    asyncio.run(_status_loop_async())


async def _status_loop_async():
    interval = float(os.environ.get("POLY_STATUS_INTERVAL", "10"))
    iteration = 0
    while running:
        iteration += 1
        bot_state["iterations"] = iteration
        bot_state["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
        await _refresh_engine_state()
        await asyncio.sleep(interval)


async def _refresh_engine_state():
    """Pull open orders, balance and trades from whichever engine is active."""
    engine = active_bot()
    if engine is None:
        return
    try:
        orders = await engine.get_open_orders()
        bot_state["open_orders"] = (orders or [])[:20]
    except Exception:
        pass
    try:
        balance = await engine.get_balance()
        if balance:
            bot_state["balance"] = balance
    except Exception:
        pass
    try:
        trades = await engine.get_trades(limit=10)
        bot_state["recent_trades"] = (trades or [])[:10]
    except Exception:
        pass


async def _bot_loop(bot):
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
        except Exception as e:
            bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": str(e)})
            if len(bot_state["errors"]) > 50:
                bot_state["errors"] = bot_state["errors"][-50:]
        for _ in range(interval):
            if not running:
                break
            await asyncio.sleep(1)
    bot_state["status"] = "stopped"


def _push_alert(msg, level="info"):
    with strategy_lock:
        bot_state["alerts"].insert(0, {"time": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        bot_state["alerts"] = bot_state["alerts"][:20]
        bot_state["errors"].append({"time": time.strftime("%H:%M:%S"), "error": msg})
        if len(bot_state["errors"]) > 50:
            bot_state["errors"] = bot_state["errors"][-50:]


def _set_strategy(cfg):
    strategy = cfg.get("strategy", "none")
    bot_state["strategy"] = strategy
    bot_state["strategy_config"] = cfg.get("config", {})
    if strategy != "none":
        _push_alert("Strategy set to {}".format(strategy))


def _ensure_strategy_loop():
    global strategy_loop
    if strategy_loop is not None:
        return strategy_loop
    ready = threading.Event()

    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        global strategy_loop
        strategy_loop = loop
        ready.set()
        loop.run_forever()

    threading.Thread(target=_runner, daemon=True, name="strategy-loop").start()
    if not ready.wait(timeout=5):
        raise RuntimeError("Strategy event loop failed to start")
    return strategy_loop


def _stop_strategy():
    global strategy_instance, strategy_future
    with strategy_lock:
        instance, future = strategy_instance, strategy_future
        strategy_instance, strategy_future = None, None
        bot_state["strategy_running"] = False

    if instance is not None:
        try:
            instance.running = False
        except Exception:
            pass
        _push_alert("Strategy stopped.", "info")

    if future is not None:
        try:
            future.cancel()
        except Exception:
            pass


def _build_strategy(name, config):
    config = dict(config or {})
    coin = str(config.pop("coin", DEFAULT_COIN)).upper()
    engine = active_bot()

    if name == "grid":
        from strategies.grid import GridStrategy, GridConfig
        cfg = GridConfig(
            coin=coin,
            levels=int(config.get("levels", 5)),
            range_pct=float(config.get("range", config.get("range_pct", 2.0))),
            size=float(config.get("size", 10.0)),
        )
        return GridStrategy(bot=engine, config=cfg)

    if name == "flash_crash":
        from strategies.flash_crash import FlashCrashStrategy, FlashCrashConfig
        cfg = FlashCrashConfig(
            coin=coin,
            size=float(config.get("size", 5.0)),
            drop_threshold=float(config.get("drop_threshold", 0.30)),
            price_lookback_seconds=int(config.get("lookback", 10)),
            take_profit=float(config.get("take_profit", 0.10)),
            stop_loss=float(config.get("stop_loss", 0.05)),
        )
        return FlashCrashStrategy(bot=engine, config=cfg)

    if name == "arb":
        from strategies.arb import ArbStrategy, ArbConfig
        cfg = ArbConfig(
            coin=coin,
            threshold=float(config.get("threshold", 0.05)),
            size=float(config.get("size", 5.0)),
        )
        return ArbStrategy(bot=engine, config=cfg)

    raise ValueError("Unknown strategy: {}".format(name))


def _start_strategy(name, config):
    global strategy_instance, strategy_future

    _stop_strategy()
    loop = _ensure_strategy_loop()
    instance = _build_strategy(name, config)

    async def _main():
        try:
            await instance.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            bot_state["strategy_running"] = False
            bot_state["strategy_error"] = str(e)
            _push_alert("Strategy {} crashed: {}".format(name, e), "error")
        else:
            bot_state["strategy_running"] = False

    with strategy_lock:
        strategy_instance = instance
        bot_state["strategy"] = name
        bot_state["strategy_config"] = dict(config or {})
        bot_state["strategy_error"] = None
        bot_state["strategy_running"] = True

    future = asyncio.run_coroutine_threadsafe(_main(), loop)
    with strategy_lock:
        strategy_future = future
    _push_alert("Strategy {} started.".format(name), "info")


def main():
    import signal

    def handle_signal(signum, frame):
        global running
        running = False
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Strip em dashes from HTML for Python 3 compat
    global DASHBOARD_HTML, TRADING_MODE
    DASHBOARD_HTML = DASHBOARD_HTML.replace("\u2014", "--")

    TRADING_MODE = _load_mode()
    print(f"[web_dashboard] Trading mode: {TRADING_MODE}")

    _warm_imports()

    bot_thread = threading.Thread(target=_try_start_bot_loop, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    print(f"[web_dashboard] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()