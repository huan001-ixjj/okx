"""示例策略：双均线交叉 + ATR 追踪止损。

这是一个「用户可自由修改」的策略文件，与内置的 ma_cross 各自独立：
修改本文件后，运行中的 `live` 会自动热重载，无需重启进程。

用法：
    okxquant backtest --strategy strategies/ma_cross.py --days 30
    okxquant live --strategy strategies/ma_cross.py --dry-run

约定：
- 必须导出名为 STRATEGY 的类，且继承 okxquant.strategy.Strategy。
- on_bar 返回下单意图列表；返回空列表表示本次不动。
- 策略里不要做网络请求或写数据库，这些交给引擎。
"""

from __future__ import annotations

from typing import Any

from okxquant.domain import Intent, Side
from okxquant.indicators import ATR, EMA
from okxquant.strategy import BarCtx, Strategy


class DoubleMaStrategy(Strategy):
    name = "double_ma_script"
    description = "脚本示例：EMA 金叉买入、死叉清仓，ATR 追踪止损"
    default_params: dict[str, Any] = {
        "fast": 12,
        "slow": 48,
        "size_pct": 0.95,
        "atr_period": 14,
        "atr_mult": 2.5,
    }

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.fast_ema = EMA(self.param_int("fast"))
        self.slow_ema = EMA(self.param_int("slow"))
        self.atr = ATR(self.param_int("atr_period"))
        self.size_pct = self.param_float("size_pct")
        self.atr_mult = self.param_float("atr_mult")

        self._prev_fast: float | None = None
        self._prev_slow: float | None = None
        self._peak = 0.0
        self._signals = 0

    # 热重载时保留的状态
    def state_dict(self) -> dict[str, Any]:
        return {
            "prev_fast": self._prev_fast,
            "prev_slow": self._prev_slow,
            "peak": self._peak,
            "signals": self._signals,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self._prev_fast = state.get("prev_fast", self._prev_fast)
        self._prev_slow = state.get("prev_slow", self._prev_slow)
        self._peak = float(state.get("peak", self._peak))
        self._signals = int(state.get("signals", self._signals))

    def on_bar(self, ctx: BarCtx) -> list[Intent]:
        price = ctx.price
        fast = self.fast_ema.update(price)
        slow = self.slow_ema.update(price)
        atr = self.atr.update(ctx.candle)

        if fast is None or slow is None:
            return []

        if self._prev_fast is None or self._prev_slow is None:
            self._prev_fast, self._prev_slow = fast, slow
            self._peak = price
            return []

        golden = self._prev_fast <= self._prev_slow and fast > slow
        death = self._prev_fast >= self._prev_slow and fast < slow
        self._prev_fast, self._prev_slow = fast, slow

        # ATR 追踪止损
        if ctx.position > 0:
            self._peak = max(self._peak, price)
            if atr is not None and self.atr_mult > 0:
                stop = self._peak - self.atr_mult * atr
                if price < stop:
                    self._signals += 1
                    return [
                        Intent(
                            Side.SELL,
                            qty=ctx.position,
                            reason=f"脚本止损：{price:.1f} < {stop:.1f}",
                        )
                    ]

        if golden and ctx.position <= 0:
            self._signals += 1
            return [
                Intent(
                    Side.BUY,
                    quote_qty=ctx.cash * self.size_pct,
                    reason="脚本金叉",
                )
            ]

        if death and ctx.position > 0:
            self._signals += 1
            return [Intent(Side.SELL, qty=ctx.position, reason="脚本死叉")]

        return []


# 引擎通过这个符号找到策略类，名字必须叫 STRATEGY
STRATEGY = DoubleMaStrategy
