"""
Strategies - Trading Strategy Implementations

This package contains trading strategy implementations:

- base: Base class for all strategies
- flash_crash: Flash crash volatility strategy
- grid: Grid trading strategy (range-bound markets)
- arb: Arbitrage strategy (cross-market discrepancies)

Usage:
    from strategies.base import BaseStrategy, StrategyConfig
    from strategies.flash_crash import FlashCrashStrategy, FlashCrashConfig
    from strategies.grid import GridStrategy, GridConfig
    from strategies.arb import ArbStrategy, ArbConfig
"""

from strategies.base import BaseStrategy, StrategyConfig
from strategies.flash_crash import FlashCrashStrategy, FlashCrashConfig
from strategies.grid import GridStrategy, GridConfig
from strategies.arb import ArbStrategy, ArbConfig

__all__ = [
    "BaseStrategy",
    "StrategyConfig",
    "FlashCrashStrategy",
    "FlashCrashConfig",
    "GridStrategy",
    "GridConfig",
    "ArbStrategy",
    "ArbConfig",
]