"""双均线策略：快线上穿慢线买入，下穿清仓，可选 ATR 追踪止损。"""

from __future__ import annotations

from typing import Any, ClassVar

from okxquant.domain import Action, Intent, Side
from okxquant.indicators import ATR, EMA
from okxquant.strategy.base import BarCtx, Strategy
from okxquant.strategy.registry import register


@register
class MaCrossStrategy(Strategy):
    name: ClassVar[str] = "ma_cross"
    description: ClassVar[str] = "双均线交叉：金叉建仓、死叉清仓，可叠加 ATR 追踪止损"
    default_params: ClassVar[dict[str, Any]] = {
        "fast": 10,
        "slow": 30,
        "size_pct": 0.95,
        "atr_period": 14,
        "atr_stop_mult": 2.0,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.fast = self.param_int("fast")
        self.slow = self.param_int("slow")
        self.size_pct = self.param_float("size_pct")
        self.atr_period = self.param_int("atr_period")
        self.atr_stop_mult = self.param_float("atr_stop_mult")

        if self.fast <= 0 or self.slow <= 0:
            raise ValueError("fast / slow 必须为正整数")
        if self.fast >= self.slow:
            raise ValueError(f"fast({self.fast}) 必须小于 slow({self.slow})")
        if not 0 < self.size_pct <= 1:
            raise ValueError("size_pct 必须在 (0, 1] 区间")

        self._fast_ema = EMA(self.fast)
        self._slow_ema = EMA(self.slow)
        self._atr = ATR(self.atr_period) if self.atr_stop_mult > 0 else None
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None
        self._peak = 0.0

    def state_dict(self) -> dict[str, Any]:
        return {
            "prev_fast": self._prev_fast,
            "prev_slow": self._prev_slow,
            "peak": self._peak,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        if isinstance(state.get("prev_fast"), (int, float)):
            self._prev_fast = float(state["prev_fast"])
        if isinstance(state.get("prev_slow"), (int, float)):
            self._prev_slow = float(state["prev_slow"])
        if isinstance(state.get("peak"), (int, float)):
            self._peak = float(state["peak"])

    def on_bar(self, ctx: BarCtx) -> list[Action]:
        price = ctx.price
        fast = self._fast_ema.update(price)
        slow = self._slow_ema.update(price)
        atr = self._atr.update(ctx.candle) if self._atr is not None else None

        if fast is None or slow is None:
            return []

        if self._prev_fast is None or self._prev_slow is None:
            self._prev_fast, self._prev_slow = fast, slow
            self._peak = price
            return []

        cross_up = self._prev_fast <= self._prev_slow and fast > slow
        cross_down = self._prev_fast >= self._prev_slow and fast < slow
        self._prev_fast, self._prev_slow = fast, slow

        actions: list[Action] = []

        # ATR 追踪止损优先于信号
        if ctx.position > 0:
            if price > self._peak:
                self._peak = price
            if (
                self.atr_stop_mult > 0
                and atr is not None
                and price < self._peak - self.atr_stop_mult * atr
            ):
                actions.append(
                    Intent(
                        Side.SELL,
                        qty=ctx.position,
                        reason=f"ATR 追踪止损 峰值{self._peak:.2f} ATR{atr:.2f}",
                    )
                )
                self._peak = price
                return actions

        if cross_up and ctx.position <= 0:
            budget = ctx.cash * self.size_pct
            if budget > 0:
                actions.append(
                    Intent(
                        Side.BUY,
                        quote_qty=budget,
                        reason=f"金叉 fast{self.fast}={fast:.2f} > slow{self.slow}={slow:.2f}",
                    )
                )
                self._peak = price
        elif cross_down and ctx.position > 0:
            actions.append(
                Intent(
                    Side.SELL,
                    qty=ctx.position,
                    reason=f"死叉 fast{self.fast}={fast:.2f} < slow{self.slow}={slow:.2f}",
                )
            )
            self._peak = price

        return actions
