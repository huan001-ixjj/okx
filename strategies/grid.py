"""示例策略：用百分比步长做网格。

与内置 grid 的区别：这里不依赖绝对价格区间，而是以「策略启动时的首根收盘价」
为基准，按 step_pct 划分买卖档位，适合不知道具体点位时快速试用。

用法：
    okxquant backtest --strategy strategies/grid.py --days 7
    okxquant live --strategy strategies/grid.py --dry-run

参数：
    step_pct       每档价格间距（百分比），如 0.5 表示 0.5%
    qty_per_grid   每档交易数量（BTC）
    max_inventory  最大持仓（BTC）
    max_grids      单次最多跨越的档数，防止跳空时一次性下大单
"""

from __future__ import annotations

from typing import Any

from okxquant.domain import Intent, Side
from okxquant.strategy import BarCtx, Strategy


class PercentGridStrategy(Strategy):
    name = "percent_grid_script"
    description = "脚本示例：以首根价格为基准的百分比网格"
    default_params: dict[str, Any] = {
        "step_pct": 0.5,
        "qty_per_grid": 0.001,
        "max_inventory": 0.02,
        "max_grids": 3,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.step_pct = self.param_float("step_pct") / 100.0
        self.qty_per_grid = self.param_float("qty_per_grid")
        self.max_inventory = self.param_float("max_inventory")
        self.max_grids = self.param_int("max_grids")

        if self.step_pct <= 0:
            raise ValueError("step_pct 必须为正数")
        if self.qty_per_grid <= 0:
            raise ValueError("qty_per_grid 必须为正数")

        self._base: float | None = None
        self._level: int | None = None

    def state_dict(self) -> dict[str, Any]:
        return {"base": self._base, "level": self._level}

    def load_state(self, state: dict[str, Any]) -> None:
        base = state.get("base")
        level = state.get("level")
        if isinstance(base, (int, float)) and base > 0:
            self._base = float(base)
        if isinstance(level, int):
            self._level = level

    def _level_of(self, price: float) -> int:
        assert self._base is not None
        return int((price / self._base - 1.0) / self.step_pct)

    def on_bar(self, ctx: BarCtx) -> list[Intent]:
        price = ctx.price
        if self._base is None:
            self._base = price
            self._level = 0
            return []

        level = self._level_of(price)
        if self._level is None:
            self._level = level
            return []

        delta = level - self._level
        if delta == 0:
            return []

        steps = min(abs(delta), self.max_grids)
        self._level = level

        if delta < 0:
            room = self.max_inventory - ctx.position
            qty = min(steps * self.qty_per_grid, room)
            if qty > 0:
                return [
                    Intent(
                        Side.BUY,
                        qty=qty,
                        reason=f"下跌 {steps} 档（{self.step_pct:.2%}/档）",
                    )
                ]
            return []

        qty = min(steps * self.qty_per_grid, ctx.position)
        if qty > 0:
            return [
                Intent(
                    Side.SELL,
                    qty=qty,
                    reason=f"上涨 {steps} 档（{self.step_pct:.2%}/档）",
                )
            ]
        return []


STRATEGY = PercentGridStrategy
