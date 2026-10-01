"""
Paper Trading Engine - Simulated Polymarket execution.

Mirrors the async order API of :class:`src.bot.TradingBot` so the dashboard and
the strategies can run against either engine without changing. Market data is
REAL: fills are decided from the live CLOB order books. Only the money, the
positions and the fills are simulated.

Start with a fixed cash balance, place the same orders the live bot would, and
watch a resting limit order fill when the real book crosses its price.

State is persisted to JSON so the balance, positions, resting orders and trade
history survive restarts and redeploys.
"""

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.bot import OrderResult
from src.http import ThreadLocalSessionMixin

DEFAULT_STARTING_CASH = 350.0
CLOB_HOST = "https://clob.polymarket.com"
SAVE_DEBOUNCE_SECONDS = 2.0


def default_state_path() -> Path:
    """Resolve where paper state is persisted.

    Prefers an explicit POLY_PAPER_STATE, then the Railway volume mount so
    state survives redeploys, then a local ./data directory.
    """
    explicit = os.environ.get("POLY_PAPER_STATE")
    if explicit:
        return Path(explicit)

    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return Path(mount) / "poly_tifu_paper_state.json"

    return Path(__file__).resolve().parent.parent / "data" / "paper_state.json"


class PaperTradingBot(ThreadLocalSessionMixin):
    """
    Simulated trading engine with a live-market fill model.

    Implements the same async surface the dashboard and strategies use:
    place_order, cancel_order, cancel_all_orders, get_open_orders, get_order,
    get_trades, get_balance and get_market_price.
    """

    def __init__(
        self,
        starting_cash: float = DEFAULT_STARTING_CASH,
        state_path: Optional[Path] = None,
        host: str = CLOB_HOST,
    ):
        super().__init__()
        self.starting_cash = float(starting_cash)
        self.state_path = Path(state_path) if state_path else default_state_path()
        self.host = host.rstrip("/")
        self.timeout = 10

        self._lock = threading.RLock()
        self._dirty = False

        self.cash: float = self.starting_cash
        self.positions: Dict[str, Dict[str, float]] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.trades: List[Dict[str, Any]] = []
        self.realized_pnl: float = 0.0
        self.last_marks: Dict[str, float] = {}
        self.fills: int = 0

        self._load()

    # ============================================================
    # Persistence
    # ============================================================
    def _load(self) -> None:
        try:
            if not self.state_path.exists():
                return
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return

        self.cash = float(data.get("cash", self.starting_cash))
        self.realized_pnl = float(data.get("realized_pnl", 0.0))
        self.fills = int(data.get("fills", 0))
        self.last_marks = {
            str(k): float(v) for k, v in (data.get("last_marks") or {}).items()
        }
        self.positions = {
            str(k): {
                "size": float(v.get("size", 0.0)),
                "cost": float(v.get("cost", 0.0)),
            }
            for k, v in (data.get("positions") or {}).items()
        }
        self.orders = {str(k): dict(v) for k, v in (data.get("orders") or {}).items()}
        self.trades = [dict(t) for t in (data.get("trades") or [])]

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "fills": self.fills,
            "last_marks": self.last_marks,
            "positions": self.positions,
            "orders": self.orders,
            "trades": self.trades[-500:],
        }

    def _flush(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._snapshot(), indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:
            pass
        finally:
            self._dirty = False

    def save(self) -> None:
        """Force a synchronous save."""
        with self._lock:
            self._flush()

    def mark_dirty(self) -> None:
        self._dirty = True

    def start_autosave(self, interval: float = SAVE_DEBOUNCE_SECONDS) -> None:
        """Flush to disk at most once per interval while there are changes."""
        while True:
            time.sleep(interval)
            if self._dirty:
                with self._lock:
                    self._flush()

    def reset(self) -> None:
        """Wipe the account back to a fresh starting balance."""
        with self._lock:
            self.cash = self.starting_cash
            self.positions = {}
            self.orders = {}
            self.trades = []
            self.realized_pnl = 0.0
            self.last_marks = {}
            self.fills = 0
            self._flush()

    # ============================================================
    # Helpers
    # ============================================================
    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    @staticmethod
    def _order_size(o: Dict[str, Any]) -> float:
        """Order quantity. Stored as original_size to match the live bot shape."""
        return float(o.get("original_size", o.get("size", 0)) or 0)

    def _reserved_cash(self, exclude_order: Optional[str] = None) -> float:
        total = 0.0
        for oid, o in self.orders.items():
            if oid == exclude_order:
                continue
            if o.get("side") == "BUY":
                total += float(o.get("price", 0)) * self._order_size(o)
        return total

    def _reserved_shares(self, token_id: str, exclude_order: Optional[str] = None) -> float:
        total = 0.0
        for oid, o in self.orders.items():
            if oid == exclude_order:
                continue
            if o.get("side") == "SELL" and o.get("token_id") == token_id:
                total += self._order_size(o)
        return total

    def _mark(self, token_id: str) -> float:
        if token_id in self.last_marks:
            return self.last_marks[token_id]
        pos = self.positions.get(token_id)
        if pos and pos["size"] > 0:
            return pos["cost"] / pos["size"]
        return 0.0

    def fetch_book(self, token_id: str) -> Dict[str, Any]:
        """
        Fetch the real CLOB book for a token.

        This endpoint is public and needs no credentials, so DRY mode still
        prices off live production data.
        """
        try:
            r = self.session.get(
                f"{self.host}/book", params={"token_id": token_id}, timeout=self.timeout
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    @staticmethod
    def top_of_book(book: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """
        Extract best bid, best ask and the size resting at each, from a CLOB book.

        Polymarket returns each side sorted worst-price-first, so the tightest
        level is the LAST entry, not the first. Size at that level matters:
        without it a 100-share order would "fill" against a 3-share level.
        """
        def best(levels, reverse):
            best_price = None
            best_size = 0.0
            for lv in levels or []:
                try:
                    price = float(lv["price"])
                    size = float(lv.get("size", 0) or 0)
                except Exception:
                    continue
                if best_price is None or (price < best_price if reverse else price > best_price):
                    best_price, best_size = price, size
            return best_price, best_size

        bid, bid_size = best(book.get("bids"), reverse=False)
        ask, ask_size = best(book.get("asks"), reverse=True)
        return {
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
        }

    # ============================================================
    # Fill engine driven by real market data
    # ============================================================
    def settle(self, books: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Fill resting orders that the real book has crossed, size permitting.

        A fill only happens for as much size as is actually resting at the
        crossing level, so the simulator cannot report fills that the live
        book could not have produced.

        Args:
            books: {token_id: {"bid","ask","bid_size","ask_size"}}

        Returns:
            List of fill events that happened on this pass.
        """
        fills: List[Dict[str, Any]] = []
        with self._lock:
            for token_id, quote in (books or {}).items():
                bid, ask = quote.get("bid"), quote.get("ask")
                if bid is not None:
                    self.last_marks[token_id] = float(bid)
                elif ask is not None:
                    self.last_marks[token_id] = float(ask)

            for oid in list(self.orders.keys()):
                order = self.orders.get(oid)
                if not order or order.get("status") != "live":
                    continue

                quote = books.get(order["token_id"]) or {}
                bid, ask = quote.get("bid"), quote.get("ask")

                remaining = round(
                    self._order_size(order) - float(order.get("size_matched", 0) or 0), 6
                )
                if remaining <= 1e-9:
                    self.orders.pop(oid, None)
                    continue

                fill_price = None
                available = 0.0
                if (
                    order["side"] == "BUY"
                    and ask is not None
                    and float(ask) <= float(order["price"])
                ):
                    fill_price = float(order["price"])
                    available = float(quote.get("ask_size") or 0.0)
                elif (
                    order["side"] == "SELL"
                    and bid is not None
                    and float(bid) >= float(order["price"])
                ):
                    fill_price = float(order["price"])
                    available = float(quote.get("bid_size") or 0.0)

                if fill_price is None or available <= 0:
                    continue

                fill_size = min(remaining, available)
                if fill_size <= 1e-9:
                    continue

                fills.append(self._apply_fill(oid, fill_size, fill_price))

            if fills:
                self._trim()
                self._dirty = True
        return fills

    def _apply_fill(self, order_id: str, fill_size: float, fill_price: float) -> Dict[str, Any]:
        """Apply a fill of fill_size at fill_price, allowing partial fills."""
        order = self.orders[order_id]
        size = self._order_size(order)
        token_id = order["token_id"]
        notional = round(fill_price * fill_size, 6)

        pos = self.positions.setdefault(token_id, {"size": 0.0, "cost": 0.0})

        if order["side"] == "BUY":
            self.cash = round(self.cash - notional, 6)
            pos["size"] = round(pos["size"] + fill_size, 6)
            pos["cost"] = round(pos["cost"] + notional, 6)
        else:
            avg = pos["cost"] / pos["size"] if pos["size"] > 0 else fill_price
            self.cash = round(self.cash + notional, 6)
            self.realized_pnl = round(self.realized_pnl + (fill_price - avg) * fill_size, 6)
            pos["size"] = round(pos["size"] - fill_size, 6)
            pos["cost"] = round(pos["cost"] - avg * fill_size, 6)
            if pos["size"] <= 1e-9:
                pos["size"] = 0.0
                pos["cost"] = 0.0

        matched = round(float(order.get("size_matched", 0) or 0) + fill_size, 6)
        order["size_matched"] = matched
        self.fills += 1

        trade = {
            "id": f"paper-{uuid.uuid4().hex[:16]}",
            "order_id": order_id,
            "created_at": self._now(),
            "timestamp": self._now(),
            "side": order["side"],
            "price": fill_price,
            "size": fill_size,
            "token_id": token_id,
            "status": "MATCHED",
            "simulated": True,
            "notional": notional,
        }
        self.trades.append(trade)

        if matched >= size - 1e-9:
            self.orders.pop(order_id, None)
            order["status"] = "matched"
            order["filled_at"] = self._now()

        return trade

    def expire_market(self, token_ids) -> Dict[str, int]:
        """
        Cancel resting orders on markets that have rolled over.

        The exchange cancels orders when a market closes, so the simulator must
        too, otherwise stale orders pile up forever and keep cash reserved.
        Positions in expired markets are settled at their last real mark.

        Args:
            token_ids: Token IDs of the expired market

        Returns:
            Counts of cancelled orders and settled positions
        """
        tokens = {t for t in (token_ids or []) if t}
        if not tokens:
            return {"cancelled": 0, "settled": 0}

        cancelled = 0
        settled = 0
        with self._lock:
            for oid in [
                o for o, v in self.orders.items() if v.get("token_id") in tokens
            ]:
                self.orders.pop(oid, None)
                cancelled += 1

            for token_id in list(tokens):
                pos = self.positions.get(token_id)
                if not pos or pos["size"] <= 1e-9:
                    continue
                mark = self.last_marks.get(token_id, 0.0)
                proceeds = round(pos["size"] * mark, 6)
                avg = pos["cost"] / pos["size"] if pos["size"] else 0.0
                self.cash = round(self.cash + proceeds, 6)
                self.realized_pnl = round(
                    self.realized_pnl + (mark - avg) * pos["size"], 6
                )
                self.trades.append(
                    {
                        "id": f"paper-{uuid.uuid4().hex[:16]}",
                        "order_id": None,
                        "created_at": self._now(),
                        "timestamp": self._now(),
                        "side": "SETTLE",
                        "price": mark,
                        "size": pos["size"],
                        "token_id": token_id,
                        "status": "EXPIRED",
                        "simulated": True,
                        "notional": proceeds,
                    }
                )
                self.fills += 1
                pos["size"] = 0.0
                pos["cost"] = 0.0
                settled += 1

            if cancelled or settled:
                self._trim()
                self._dirty = True

        return {"cancelled": cancelled, "settled": settled}

    def _trim(self) -> None:
        if len(self.trades) > 500:
            self.trades = self.trades[-500:]

    # ============================================================
    # Async order API (mirrors TradingBot)
    # ============================================================
    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
        neg_risk: Optional[bool] = None,
        builder_code: Optional[str] = None,
    ) -> OrderResult:
        """Place a simulated limit order. Mirrors TradingBot.place_order."""
        import asyncio

        side = str(side).upper()
        price = float(price)
        size = float(size)

        if not token_id:
            return OrderResult(success=False, message="token_id required")
        if side not in ("BUY", "SELL"):
            return OrderResult(success=False, message=f"Invalid side: {side}")
        if not 0 < price < 1:
            return OrderResult(success=False, message=f"Price out of range: {price}")
        if size <= 0:
            return OrderResult(success=False, message=f"Invalid size: {size}")

        notional = round(price * size, 6)
        order_id = f"0xpaper{uuid.uuid4().hex[:24]}"

        with self._lock:
            if side == "BUY":
                available = round(self.cash - self._reserved_cash(), 6)
                if notional > available:
                    self._dirty = True
                    return OrderResult(
                        success=False,
                        message=(
                            f"Insufficient simulated cash: need ${notional:.2f}, "
                            f"have ${available:.2f} free"
                        ),
                        data={"cash": self.cash, "reserved": self._reserved_cash()},
                    )
            else:
                pos = self.positions.get(token_id, {"size": 0.0})
                available = round(pos["size"] - self._reserved_shares(token_id), 6)
                if size > available:
                    self._dirty = True
                    return OrderResult(
                        success=False,
                        message=(
                            f"Insufficient simulated position: need {size}, "
                            f"have {available}"
                        ),
                        data={"position": pos["size"]},
                    )

            order = {
                "id": order_id,
                "status": "live",
                "side": side,
                "price": price,
                "original_size": size,
                "size_matched": 0,
                "token_id": token_id,
                "order_type": order_type,
                "neg_risk": neg_risk,
                "created_at": self._now(),
                "simulated": True,
            }
            self.orders[order_id] = order
            self._dirty = True

        if order_type.upper() == "FOK":
            book = await asyncio.to_thread(self.fetch_book, token_id)
            quote = self.top_of_book(book)
            self.settle({token_id: quote})
            with self._lock:
                final = self.orders.get(order_id)
                if final and final["status"] == "live":
                    self.orders.pop(order_id, None)
                    self._dirty = True
                    return OrderResult(
                        success=False,
                        order_id=order_id,
                        message="FOK order not immediately fillable; killed",
                    )
            return OrderResult(
                success=True,
                order_id=order_id,
                status="matched",
                message="FOK order filled immediately",
            )

        return OrderResult(
            success=True,
            order_id=order_id,
            status="live",
            message="Simulated order placed",
            data=dict(order),
        )

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a simulated order. Mirrors TradingBot.cancel_order."""
        with self._lock:
            order = self.orders.get(order_id)
            if not order:
                return OrderResult(
                    success=False, order_id=order_id, message=f"No open order {order_id}"
                )
            self.orders.pop(order_id, None)
            self._dirty = True
            return OrderResult(
                success=True,
                order_id=order_id,
                status="canceled",
                message="Simulated order canceled",
                data=dict(order),
            )

    async def cancel_all_orders(self) -> OrderResult:
        """Cancel every simulated order. Mirrors TradingBot.cancel_all_orders."""
        with self._lock:
            count = len(self.orders)
            self.orders.clear()
            self._dirty = True
            return OrderResult(
                success=True,
                message=f"Canceled {count} simulated order(s)",
                data={"canceled": count},
            )

    async def cancel_market_orders(self, token_id: str) -> OrderResult:
        """Cancel simulated orders on one market."""
        with self._lock:
            doomed = [o for o, v in self.orders.items() if v.get("token_id") == token_id]
            for oid in doomed:
                self.orders.pop(oid, None)
            self._dirty = True
            return OrderResult(
                success=True,
                message=f"Canceled {len(doomed)} simulated order(s) on {token_id}",
                data={"canceled": len(doomed)},
            )

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Return simulated resting orders in the live bot's shape."""
        with self._lock:
            return [dict(o) for o in self.orders.values()]

    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            order = self.orders.get(order_id)
            return dict(order) if order else None

    async def get_trades(
        self, token_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return simulated fills, newest first."""
        with self._lock:
            trades = list(reversed(self.trades))
        if token_id:
            trades = [t for t in trades if t.get("token_id") == token_id]
        return trades[:limit]

    async def get_balance(self) -> Dict[str, Any]:
        """Return the simulated account summary."""
        with self._lock:
            positions_value = 0.0
            positions = []
            for token_id, pos in self.positions.items():
                if pos["size"] <= 0:
                    continue
                mark = self._mark(token_id)
                value = round(pos["size"] * mark, 6)
                positions_value += value
                positions.append(
                    {
                        "token_id": token_id,
                        "size": pos["size"],
                        "avg_price": round(pos["cost"] / pos["size"], 6) if pos["size"] else 0.0,
                        "mark": mark,
                        "value": value,
                        "unrealized_pnl": round(
                            (mark - (pos["cost"] / pos["size"])) * pos["size"], 6
                        )
                        if pos["size"]
                        else 0.0,
                    }
                )

            positions_value = round(positions_value, 6)
            reserved = round(self._reserved_cash(), 6)
            equity = round(self.cash + positions_value, 6)
            return {
                "simulated": True,
                "cash": round(self.cash, 6),
                "free_cash": round(self.cash - reserved, 6),
                "reserved_cash": reserved,
                "positions_value": positions_value,
                "equity": equity,
                "starting_cash": self.starting_cash,
                "total_pnl": round(equity - self.starting_cash, 6),
                "realized_pnl": self.realized_pnl,
                "fills": self.fills,
                "open_orders": len(self.orders),
                "positions": positions,
                "state_path": str(self.state_path),
            }

    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        import asyncio

        return await asyncio.to_thread(self.fetch_book, token_id)

    async def get_market_price(self, token_id: str) -> Dict[str, Any]:
        """Return the real mid for a token, plus the simulated mark."""
        import asyncio

        book = await asyncio.to_thread(self.fetch_book, token_id)
        quote = self.top_of_book(book)
        bid, ask = quote.get("bid"), quote.get("ask")
        mid = None
        if bid is not None and ask is not None:
            mid = round((bid + ask) / 2, 6)
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask

        if mid is not None:
            with self._lock:
                self.last_marks[token_id] = mid
                self._dirty = True

        return {
            "token_id": token_id,
            "mid": mid,
            "bid": bid,
            "ask": ask,
            "simulated": True,
        }

    async def deploy_safe_if_needed(self) -> bool:
        """No-op: the simulated account needs no on-chain deployment."""
        return True
