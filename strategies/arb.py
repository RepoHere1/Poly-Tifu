"""
Arbitrage Strategy - Cross-market price discrepancy trades.

Monitors two related markets (e.g., Up vs Down of the same
15-min event) and exploits price discrepancies when the
implied probabilities diverge from fair value.

Strategy Logic:
1. Fetch prices for both outcomes of a market
2. Compute implied probability sum
3. If sum deviates from 1.0 by threshold:
   - Buy the underpriced outcome
   - Sell the overpriced outcome (if supported)
4. Close when discrepancy normalizes

Usage:
    from strategies.arb import ArbStrategy, ArbConfig

    config = ArbConfig(coin="BTC", threshold=0.05, size=5)
    strategy = ArbStrategy(bot, config)
    await strategy.run()
"""

from dataclasses import dataclass
from typing import Dict

from strategies.base import BaseStrategy, StrategyConfig
from src.bot import TradingBot


@dataclass
class ArbConfig(StrategyConfig):
    """Arbitrage strategy configuration."""

    threshold: float = 0.05   # Min deviation from 1.0 to trigger
    size: float = 5.0         # USDC size per leg


class ArbStrategy(BaseStrategy):
    """
    Arbitrage Strategy.

    Exploits pricing discrepancies between correlated markets.
    """

    def __init__(self, bot: TradingBot, config: ArbConfig):
        super().__init__(bot, config)
        self.arb_config = config
        self.last_opportunity: Dict = {}

    async def on_tick(self, prices: Dict[str, float]) -> None:
        """Check for arb opportunity on each tick."""
        up_price = prices.get("up", 0)
        down_price = prices.get("down", 0)
        if not up_price or not down_price or up_price <= 0 or down_price <= 0:
            return

        implied_sum = up_price + down_price
        deviation = abs(implied_sum - 1.0)

        if deviation < self.arb_config.threshold:
            return

        # Determine which side is underpriced
        target_price = min(up_price, down_price)
        target_side = "up" if up_price < down_price else "down"
        target_token = self.token_ids.get(target_side, "")
        opposite_side = "down" if target_side == "up" else "up"
        opposite_token = self.token_ids.get(opposite_side, "")

        if not target_token:
            return

        # Buy the underpriced side
        result = await self.bot.place_order(
            token_id=target_token,
            price=round(target_price, 4),
            size=self.arb_config.size,
            side="BUY",
            order_type="GTC",
        )

        self.last_opportunity = {
            "time": __import__("time").strftime("%H:%M:%S"),
            "up_price": up_price,
            "down_price": down_price,
            "implied_sum": round(implied_sum, 4),
            "deviation": round(deviation, 4),
            "target_side": target_side,
            "target_price": target_price,
            "success": result.success,
            "order_id": result.order_id,
        }

        if result.success:
            self.log(
                f"ARB: buy {target_side} @ {target_price:.4f} "
                f"(sum={implied_sum:.4f} dev={deviation:.4f})",
                "trade",
            )

    def render_status(self, prices: Dict[str, float]) -> None:
        """Render arb status."""
        lines = [f"Arb Strategy | Threshold: {self.arb_config.threshold:.2%}"]
        if self.last_opportunity:
            o = self.last_opportunity
            lines.append(
                f"  Last: {o['target_side']} @ {o['target_price']:.4f} "
                f"(sum={o['implied_sum']} dev={o['deviation']}) "
                f"filled={o['success']}"
            )
        print("\n".join(lines))