"""
Grid Trading Strategy - DCA-style grid for range-bound markets.

Places limit orders at regular price intervals (grid levels) above
and below the current price, profiting from mean reversion within
the defined range.

Strategy Logic:
1. Fetch current market price
2. Compute grid levels from current_price +/- range_pct
3. Place BUY limit orders at lower grid levels
4. Place SELL limit orders at upper grid levels
5. When an order fills, place the next grid order on the other side

Usage:
    from strategies.grid import GridStrategy, GridConfig

    config = GridConfig(coin="ETH", levels=5, range_pct=2.0, size=10)
    strategy = GridStrategy(bot, config)
    await strategy.run()
"""

from dataclasses import dataclass, field
from typing import Dict, List

from strategies.base import BaseStrategy, StrategyConfig
from src.bot import TradingBot


@dataclass
class GridConfig(StrategyConfig):
    """Grid strategy configuration."""

    levels: int = 5          # Number of grid levels per side
    range_pct: float = 2.0   # Price range as % of current price
    size: float = 10.0       # USDC size per order
    price_offset_pct: float = 0.1  # Offset from mid for order placement


class GridStrategy(BaseStrategy):
    """
    Grid Trading Strategy.

    Places a grid of buy/sell limit orders around the current price
    to profit from oscillating markets.
    """

    def __init__(self, bot: TradingBot, config: GridConfig):
        super().__init__(bot, config)
        self.grid_config = config
        self.grid_orders: List[Dict] = []
        self._mid_price: float = 0

    async def on_tick(self, prices: Dict[str, float]) -> None:
        """Rebuild grid on each tick."""
        up_price = prices.get("up", 0)
        down_price = prices.get("down", 0)
        mid = (up_price + down_price) / 2 if up_price and down_price else up_price or down_price
        if not mid or mid <= 0:
            return

        self._mid_price = mid
        await self._place_grid(mid)

    async def _place_grid(self, mid_price: float) -> None:
        """Place buy/sell grid levels around mid_price."""
        cfg = self.grid_config
        range_val = mid_price * (cfg.range_pct / 100)
        step = (2 * range_val) / max(cfg.levels, 1)

        # Cancel existing grid orders
        for o in self.grid_orders:
            try:
                await self.bot.cancel_order(o["id"])
            except Exception:
                pass
        self.grid_orders.clear()

        # Place BUY orders below mid
        for i in range(cfg.levels):
            price = mid_price - range_val + step * (i + 0.5)
            if price <= 0:
                continue
            token_id = self.token_ids.get("down", "")
            if not token_id:
                continue
            result = await self.bot.place_order(
                token_id=token_id,
                price=round(price, 4),
                size=cfg.size,
                side="BUY",
                order_type="GTC",
            )
            if result.success:
                self.grid_orders.append({"id": result.order_id, "side": "BUY", "price": price})

        # Place SELL orders above mid
        for i in range(cfg.levels):
            price = mid_price + range_val - step * (i + 0.5)
            if price <= 0:
                continue
            token_id = self.token_ids.get("up", "")
            if not token_id:
                continue
            result = await self.bot.place_order(
                token_id=token_id,
                price=round(price, 4),
                size=cfg.size,
                side="SELL",
                order_type="GTC",
            )
            if result.success:
                self.grid_orders.append({"id": result.order_id, "side": "SELL", "price": price})

    def render_status(self, prices: Dict[str, float]) -> None:
        """Render grid status."""
        lines = [f"Grid Strategy | Mid: {self._mid_price:.4f} | Levels: {len(self.grid_orders)}"]
        for o in self.grid_orders[:10]:
            lines.append(f"  {o['side']} @ {o['price']:.4f}  ID: {o['id'][:8]}")
        print("\n".join(lines))