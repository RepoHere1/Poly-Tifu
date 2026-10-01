"""
Analytics - equity history and trade ledger for live and simulated trading.

Both trading engines feed the same two things:

- :class:`EquityTracker` samples equity, cash and PnL over time so the dashboard
  can draw curves. Samples are keyed by mode so LIVE and DRY keep separate
  histories, and are persisted so a redeploy does not wipe the curve.
- :func:`build_ledger` turns raw fills, resting orders and positions into OPEN
  and CLOSED trade rows using FIFO lot matching, which is how a position book
  is normally read.
"""

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

MAX_SAMPLES = 720
MAX_CLOSED = 400


def default_history_path() -> Path:
    explicit = os.environ.get("POLY_PAPER_STATE")
    if explicit:
        return Path(explicit).with_name("equity_history.json")
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return Path(mount) / "equity_history.json"
    return Path(__file__).resolve().parent.parent / "data" / "equity_history.json"


class EquityTracker:
    """
    Rolling equity history, one series per trading mode.

    Each sample records the numbers a trader actually watches: cash, position
    exposure, equity, realized and unrealized PnL.
    """

    def __init__(self, path: Optional[Path] = None, max_samples: int = MAX_SAMPLES):
        self.path = Path(path) if path else default_history_path()
        self.max_samples = max_samples
        self._lock = threading.RLock()
        self._series: Dict[str, Deque[Dict[str, Any]]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        for mode, samples in (raw or {}).items():
            if isinstance(samples, list):
                self._series[mode] = deque(samples[-self.max_samples:], maxlen=self.max_samples)

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({k: list(v) for k, v in self._series.items()}), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            pass
        finally:
            self._dirty = False

    def record(self, mode: str, balance: Dict[str, Any]) -> None:
        """Append one sample for a mode."""
        if not balance:
            return
        with self._lock:
            series = self._series.setdefault(mode, deque(maxlen=self.max_samples))
            series.append(
                {
                    "t": time.time(),
                    "cash": _f(balance.get("cash")),
                    "positions_value": _f(balance.get("positions_value")),
                    "equity": _f(balance.get("equity")),
                    "realized_pnl": _f(balance.get("realized_pnl")),
                    "unrealized_pnl": _f(
                        sum(
                            _f(p.get("unrealized_pnl"))
                            for p in (balance.get("positions") or [])
                        )
                    ),
                }
            )
            self._dirty = True

    def series(self, mode: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._series.get(mode, []))

    def summary(self, mode: str) -> Dict[str, Any]:
        """Headline figures plus the series for charting."""
        samples = self.series(mode)
        if not samples:
            return {"samples": [], "starting": None, "peak": None, "trough": None, "change": 0.0}

        equities = [s["equity"] for s in samples]
        first, last = samples[0], samples[-1]
        return {
            "samples": samples,
            "starting": first["equity"],
            "current": last["equity"],
            "peak": max(equities),
            "trough": min(equities),
            "change": round(last["equity"] - first["equity"], 6),
            "duration_seconds": round(last["t"] - first["t"], 1),
        }

    def start_autosave(self, interval: float = 5.0) -> None:
        while True:
            time.sleep(interval)
            if self._dirty:
                with self._lock:
                    self._flush()

    def reset(self, mode: str) -> None:
        with self._lock:
            self._series.pop(mode, None)
            self._dirty = True
            self._flush()


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm_fills(fills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Normalise engine-specific fills into one shape, oldest first.

    Ordering must be stable even when several fills share a timestamp, because
    FIFO lot matching depends on it. Engines return trades newest-first, and a
    simulator can easily fill several orders inside the same second, so the
    sequence number is the tiebreaker rather than an engine's own ordering.
    """
    out = []
    for arrival, f in enumerate(fills or []):
        if not isinstance(f, dict):
            continue
        ts = f.get("ts")
        if ts is None:
            raw = f.get("timestamp", f.get("created_at", f.get("time")))
            if isinstance(raw, (int, float)):
                ts = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
            elif isinstance(raw, str):
                try:
                    ts = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    ).timestamp()
                except ValueError:
                    ts = time.time()
            else:
                ts = time.time()
        try:
            seq = float(f.get("seq", arrival))
        except (TypeError, ValueError):
            seq = float(arrival)
        out.append(
            {
                "id": f.get("id") or f.get("order_id") or f"f{arrival}",
                "ts": float(ts),
                "seq": seq,
                "side": str(f.get("side", "")).upper(),
                "price": _f(f.get("price")),
                "size": _f(f.get("size")),
                "token_id": str(f.get("token_id") or f.get("asset") or ""),
                "market": f.get("market") or f.get("title") or f.get("slug") or "",
                "outcome": f.get("outcome") or "",
                "status": f.get("status") or "MATCHED",
            }
        )
    out.sort(key=lambda x: (x["ts"], x["seq"]))
    return out


def build_ledger(
    fills: List[Dict[str, Any]],
    open_orders: List[Dict[str, Any]],
    marks: Optional[Dict[str, float]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Split engine state into OPEN and CLOSED trade rows using FIFO lot matching.

    Buys open lots. Each SELL consumes the oldest lots first, and every
    consumed lot becomes one closed trade with entry, exit and realised PnL.
    Lots never sold stay open and are marked to market. Resting orders that
    have not filled are listed as open rows in a PENDING state.

    Args:
        fills: executed fills from either engine
        open_orders: resting orders from either engine
        marks: {token_id: current price} for marking open lots

    Returns:
        {"open_trades": [...], "closed_trades": [...]}
    """
    marks = marks or {}
    events = _norm_fills(fills)

    lots: Dict[str, List[Dict[str, Any]]] = {}
    closed: List[Dict[str, Any]] = []

    for e in events:
        if e["status"] == "EXPIRED":
            continue
        if e["side"] == "BUY":
            lots.setdefault(e["token_id"], []).append(
                {
                    "id": e["id"],
                    "token_id": e["token_id"],
                    "size": e["size"],
                    "entry": e["price"],
                    "opened_at": e["ts"],
                    "market": e["market"],
                    "outcome": e["outcome"],
                }
            )
            continue

        if e["side"] != "SELL":
            continue

        queue = lots.setdefault(e["token_id"], [])
        remaining = e["size"]
        exit_price = e["price"]

        while remaining > 1e-9 and queue:
            lot = queue[0]
            take = min(remaining, lot["size"])
            entry = lot["entry"]
            pnl = round((exit_price - entry) * take, 6)
            closed.append(
                {
                    "id": f"{lot['id']}-{e['id']}",
                    "token_id": e["token_id"],
                    "market": e["market"] or lot["market"],
                    "outcome": e["outcome"] or lot["outcome"],
                    "side": "LONG",
                    "size": round(take, 6),
                    "entry": entry,
                    "exit": exit_price,
                    "pnl": pnl,
                    "pnl_pct": round((exit_price / entry - 1) * 100, 4) if entry else 0.0,
                    "status": "CLOSED",
                    "opened_at": lot["opened_at"],
                    "closed_at": e["ts"],
                    "hold_seconds": round(e["ts"] - lot["opened_at"], 1),
                }
            )
            lot["size"] = round(lot["size"] - take, 6)
            remaining = round(remaining - take, 6)
            if lot["size"] <= 1e-9:
                queue.pop(0)

    open_trades: List[Dict[str, Any]] = []
    for token_id, queue in lots.items():
        mark = _f(marks.get(token_id))
        for lot in queue:
            if lot["size"] <= 1e-9:
                continue
            value = round(lot["size"] * mark, 6)
            unrealized = round((mark - lot["entry"]) * lot["size"], 6)
            open_trades.append(
                {
                    "id": lot["id"],
                    "token_id": token_id,
                    "market": lot["market"],
                    "outcome": lot["outcome"],
                    "side": "LONG",
                    "size": lot["size"],
                    "entry": lot["entry"],
                    "mark": mark,
                    "value": value,
                    "unrealized": unrealized,
                    "unrealized_pct": round((mark / lot["entry"] - 1) * 100, 4) if lot["entry"] else 0.0,
                    "status": "OPEN",
                    "opened_at": lot["opened_at"],
                    "age_seconds": round(time.time() - lot["opened_at"], 1),
                }
            )

    for o in open_orders or []:
        if not isinstance(o, dict):
            continue
        size = _f(o.get("original_size", o.get("size")))
        matched = _f(o.get("size_matched"))
        open_trades.append(
            {
                "id": o.get("id"),
                "token_id": o.get("token_id", ""),
                "market": o.get("market", ""),
                "outcome": o.get("outcome", ""),
                "side": o.get("side", ""),
                "size": size,
                "filled": matched,
                "resting": round(size - matched, 6),
                "entry": _f(o.get("price")),
                "status": "PENDING" if matched < size else "PARTIAL",
                "opened_at": o.get("created_at_ts", o.get("created_at")),
                "age_seconds": o.get("age_seconds"),
                "simulated": bool(o.get("simulated")),
            }
        )

    closed.sort(key=lambda x: x["closed_at"], reverse=True)
    open_trades.sort(key=lambda x: (x.get("status") != "OPEN", -(x.get("unrealized") or 0)))
    closed = closed[:MAX_CLOSED]

    wins = sum(1 for c in closed if c["pnl"] > 0)
    losses = sum(1 for c in closed if c["pnl"] < 0)
    gross_win = sum(c["pnl"] for c in closed if c["pnl"] > 0)
    gross_loss = abs(sum(c["pnl"] for c in closed if c["pnl"] < 0))

    return {
        "open_trades": open_trades,
        "closed_trades": closed,
        "stats": {
            "open_count": len(open_trades),
            "closed_count": len(closed),
            "wins": wins,
            "losses": losses,
            "flat": len(closed) - wins - losses,
            "win_rate": round(wins / len(closed) * 100, 2) if closed else 0.0,
            "gross_win": round(gross_win, 6),
            "gross_loss": round(gross_loss, 6),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "avg_win": round(gross_win / wins, 6) if wins else 0.0,
            "avg_loss": round(gross_loss / losses, 6) if losses else 0.0,
            "best": max((c["pnl"] for c in closed), default=0.0),
            "worst": min((c["pnl"] for c in closed), default=0.0),
            "total_realized": round(sum(c["pnl"] for c in closed), 6),
            "avg_hold_seconds": round(
                sum(c["hold_seconds"] for c in closed) / len(closed), 1
            )
            if closed
            else 0.0,
        },
    }
