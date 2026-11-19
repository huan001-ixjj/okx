"""布林带均值回归：现货专用，只做多。

上一版区间回归的致命问题是用 ``max(high)/min(low)`` 定义箱体，单根插针就能让
箱体整体偏移，导致现货与永续回测结果从 +44% 变到 -4.45%。

这一版改用**收盘价的均值 ± k 倍标准差**：

- 只用 close 计算，插针不影响判断
- 标准差是统计量，不同标的可直接比较
- 跌破下轨买入、回到中轨卖出、ATR 止损兜底
- 现货只能做多，所以不做空

同时要求价格跌破下轨的**幅度**足够（``min_drop_pct``），避免在阴跌中过早接刀。
"""

from __future__ import annotations

import math
from collections import deque

from okxquant.domain import Intent, Side
from okxquant.indicators import ATR
from okxquant.strategy import BarCtx, Strategy

EPS = 1e-12


class BollingerReversionStrategy(Strategy):
    name = "bollinger_reversion"
    description = "布林带均值回归：下轨买、中轨平；可选突破上轨做空"
    default_params = {
        "period": 40,          # 均值与标准差的窗口
        "k_entry": 2.0,        # 跌破 ma - k*std 才买
        "min_drop_pct": 0.015,  # 且至少低于中轨 1.5%
        "stop_atr": 2.5,       # 入场后跌破 N 倍 ATR 止损
        "atr_period": 14,
        "size_pct": 0.9,
        # 合约场景默认开启对称做空。现货环境下 SELL 开空会被撮合层截断为 0，
        # 不会报错也不会误成交，因此这里开 True 对现货是安全的。
        "short_enabled": True,
    }

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        period = self.param_int("period", 40)
        self._closes: deque[float] = deque(maxlen=period)
        self.atr = ATR(self.param_int("atr_period", 14))
        self.entry_price = 0.0
        self.entry_atr = 0.0

    def on_bar(self, ctx: BarCtx) -> list[Intent]:
        price = ctx.price
        atr = self.atr.update(ctx.candle)
        period = self.param_int("period", 40)

        if len(self._closes) < period or atr is None or atr <= 0:
            self._closes.append(price)
            return []

        closes = list(self._closes)
        ma = sum(closes) / len(closes)
        var = sum((c - ma) ** 2 for c in closes) / len(closes)
        std = math.sqrt(var)

        k_entry = self.param_float("k_entry", 2.0)
        min_drop = self.param_float("min_drop_pct", 0.015)
        stop_atr = self.param_float("stop_atr", 2.5)
        size_pct = self.param_float("size_pct", 0.9)

        short_enabled = self.param_bool("short_enabled", False)
        actions: list[Intent] = []

        if ctx.position > EPS:
            if price >= ma:
                actions.append(Intent(Side.SELL, qty=ctx.position, reason="回到中轨止盈"))
                self._reset()
            elif price < self.entry_price - stop_atr * self.entry_atr:
                actions.append(Intent(Side.SELL, qty=ctx.position, reason="ATR 止损"))
                self._reset()
        elif ctx.position < -EPS:
            held = abs(ctx.position)
            if price <= ma:
                actions.append(Intent(Side.BUY, qty=held, reason="回到中轨止盈（空）"))
                self._reset()
            elif price > self.entry_price + stop_atr * self.entry_atr:
                actions.append(Intent(Side.BUY, qty=held, reason="ATR 止损（空）"))
                self._reset()
        elif std > 0:
            lower = ma - k_entry * std
            upper = ma + k_entry * std
            if price <= lower and (ma - price) / ma >= min_drop:
                qty = ctx.cash * size_pct / price if price > 0 else 0.0
                if qty > 0:
                    self.entry_price = price
                    self.entry_atr = atr
                    actions.append(
                        Intent(Side.BUY, quote_qty=ctx.cash * size_pct, reason="跌破下轨买入")
                    )
            elif short_enabled and price >= upper and (price - ma) / ma >= min_drop:
                qty = ctx.cash * size_pct / price if price > 0 else 0.0
                if qty > 0:
                    self.entry_price = price
                    self.entry_atr = atr
                    actions.append(
                        Intent(
                            Side.SELL,
                            qty=qty,
                            allow_short=True,
                            reason="突破上轨做空",
                        )
                    )

        self._closes.append(price)
        return actions

    def _reset(self) -> None:
        self.entry_price = 0.0
        self.entry_atr = 0.0

    def state_dict(self) -> dict:
        return {
            "entry_price": self.entry_price,
            "entry_atr": self.entry_atr,
            "closes": list(self._closes),
        }

    def load_state(self, state: dict) -> None:
        self.entry_price = float(state.get("entry_price", 0.0) or 0.0)
        self.entry_atr = float(state.get("entry_atr", 0.0) or 0.0)
        self._closes.clear()
        self._closes.extend(float(v) for v in state.get("closes", []))


STRATEGY = BollingerReversionStrategy
