"""区间网格策略。

在 ``[lower, upper]`` 内划分 ``grids`` 档，价格每下穿一档买入 ``qty_per_grid``，
每上穿一档卖出同样数量；库存受 ``max_inventory`` 限制，跌破 ``stop_loss_px``
则清仓。价格越界后停止开新仓，只允许卖出。
"""

from __future__ import annotations

from typing import Any, ClassVar

from loguru import logger

from okxquant.domain import Action, CancelAll, Intent, Side
from okxquant.strategy.base import BarCtx, Strategy
from okxquant.strategy.registry import register


@register
class GridStrategy(Strategy):
    name: ClassVar[str] = "grid"
    description: ClassVar[str] = "区间网格：低买高卖，越界停手，跌破止损清仓"
    default_params: ClassVar[dict[str, Any]] = {
        "lower": 0.0,
        "upper": 0.0,
        "grids": 20,
        "qty_per_grid": 0.001,
        "max_inventory": 0.05,
        "stop_loss_px": 0.0,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._level: int | None = None
        self._warned_range = False
        self.lower = self.param_float("lower")
        self.upper = self.param_float("upper")
        self.grids = self.param_int("grids")
        self.qty_per_grid = self.param_float("qty_per_grid")
        self.max_inventory = self.param_float("max_inventory")
        self.stop_loss_px = self.param_float("stop_loss_px")

        if self.grids <= 0:
            raise ValueError("grids 必须为正整数")
        if self.qty_per_grid <= 0:
            raise ValueError("qty_per_grid 必须为正数")

    @property
    def step(self) -> float:
        return (self.upper - self.lower) / self.grids

    def _level_of(self, price: float) -> int:
        raw = int((price - self.lower) // self.step)
        return min(max(raw, 0), self.grids - 1)

    def state_dict(self) -> dict[str, Any]:
        return {"level": self._level}

    def load_state(self, state: dict[str, Any]) -> None:
        level = state.get("level")
        if isinstance(level, int):
            self._level = level

    def on_bar(self, ctx: BarCtx) -> list[Action]:
        price = ctx.price
        actions: list[Action] = []

        if self.upper <= self.lower:
            if not self._warned_range:
                logger.warning(
                    "网格区间未配置（lower={} upper={}），策略空转；请在配置中设置区间",
                    self.lower,
                    self.upper,
                )
                self._warned_range = True
            return actions

        # 止损优先：撤单并清仓
        if self.stop_loss_px > 0 and price <= self.stop_loss_px and ctx.position > 0:
            logger.warning("网格触发止损：现价 {} <= 止损价 {}", price, self.stop_loss_px)
            return [
                CancelAll(reason="grid stop loss"),
                Intent(Side.SELL, qty=ctx.position, reason="stop loss"),
            ]

        level = self._level_of(price)
        if self._level is None:
            self._level = level
            return actions

        if price < self.lower or price > self.upper:
            if not self._warned_range:
                logger.warning(
                    "价格 {} 超出网格区间 [{}, {}]，暂停开仓", price, self.lower, self.upper
                )
                self._warned_range = True
        else:
            self._warned_range = False

        delta = level - self._level
        if delta < 0:
            # 价格下跌 delta 档 → 买入
            if price < self.lower:
                self._level = level
                return actions
            wanted = abs(delta) * self.qty_per_grid
            room = self.max_inventory - ctx.position
            qty = min(wanted, max(0.0, room))
            self._level = level
            if qty > 0:
                actions.append(
                    Intent(Side.BUY, qty=qty, reason=f"grid dip {abs(delta)} 档 @ {price:.2f}")
                )
        elif delta > 0:
            # 价格上涨 delta 档 → 卖出
            qty = min(abs(delta) * self.qty_per_grid, ctx.position)
            self._level = level
            if qty > 0:
                actions.append(
                    Intent(Side.SELL, qty=qty, reason=f"grid rise {abs(delta)} 档 @ {price:.2f}")
                )

        return actions
