"""因子均值回归策略：z-score 与 VWAP 偏离合成打分，多空双向。

因子证据（见 scripts/factor_scan.py 输出）：

- XRP-USDT-SWAP 5m 三年样本里，价格位置类因子（``close/MA-1``、唐奇安位置、
  ``ema_slope``、``mom``）的 IC 均为负且稳定（约 -0.06 ~ -0.09），即**均值回归**
  而非动量；``vwap_gap``（close/VWAP-1）与这些位置因子正交（相关性≈0），且
  IC 同样为负（约 -0.07）。
- 量能类因子（vol_ratio / vol_zscore / vol_trend）IC 接近 0，不具预测力。

本策略把两个正交的回归因子合成打分：

- ``z``：价格相对 ``mean_period`` 均线的标准化偏离（≈ 布林 %B）。
- ``vwap_gap``：价格相对 ``vwap_period`` 量加权均价的偏离。

打分 = z + ``vwap_weight`` * vwap_gap_norm，偏离越大越反向建仓，回到均值附近平仓，
ATR 止损兜底强趋势（均值回归在单边行情里的最大风险）。
"""

from __future__ import annotations

import math
from collections import deque

from okxquant.domain import Intent, Side
from okxquant.indicators import ATR
from okxquant.strategy import BarCtx, Strategy

EPS = 1e-12
# vwap_gap 的经验波动量级（XRP 5m，约 3‰），用于把它映射到 z-score 量级
VWAP_GAP_SCALE = 0.003


class FactorReversionStrategy(Strategy):
    name = "factor_reversion"
    description = "因子均值回归：z-score + VWAP 偏离合成打分，多空反向，ATR 止损"
    default_params = {
        "mean_period": 50,
        "entry_k": 1.5,          # z-score 入场阈值（|score| 超过才反向建仓）
        "exit_k": 0.0,           # 回到该 |score| 以下平仓（0 = 回归均值）
        "vwap_period": 20,
        "vwap_weight": 0.0,      # VWAP 偏离权重（0 = 纯 z-score）
        "stop_atr_mult": 3.0,    # ATR 止损倍数，0 表示关闭
        "atr_period": 14,
        "size_pct": 1.0,         # 每次建仓占用权益比例
        "short_enabled": True,   # 是否允许做空（牛市里做空均值回归风险极大）
        "short_trend_period": 288,  # 做空趋势过滤：仅在 close < SMA(该周期) 时做空（0=不过滤）
        "long_trend_period": 96,    # 做多趋势过滤周期（0=不过滤）；15m 下 96=1 天
        "long_filter_exit": False,  # 做多持仓跌破长期均线时是否提前平仓
        "long_filter_mode": "rising",  # above=价格在均线上方才做多；rising=均线上行才做多（推荐，防接飞刀）
        "long_slope_lag": 24,       # rising 模式下比较的均线滞后根数（15m 下 24=6 小时）
    }

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        mp = self.param_int("mean_period", 50)
        self._closes: deque[float] = deque(maxlen=mp)
        self._sum = 0.0
        self._sumsq = 0.0

        vp = self.param_int("vwap_period", 20)
        self._typ: deque[float] = deque(maxlen=vp)
        self._vol: deque[float] = deque(maxlen=vp)
        self._tv_sum = 0.0
        self._v_sum = 0.0

        tp = self.param_int("short_trend_period", 288)
        self._trend: deque[float] = deque(maxlen=tp) if tp > 0 else deque(maxlen=1)
        self._trend_sum = 0.0

        ltp = self.param_int("long_trend_period", 288)
        self._long_trend: deque[float] = deque(maxlen=ltp) if ltp > 0 else deque(maxlen=1)
        self._long_trend_sum = 0.0
        self._long_ma_hist: deque[float] = deque(maxlen=64)

        self._atr = ATR(self.param_int("atr_period", 14))
        self._entry_px = 0.0
        self._entry_atr = 0.0

    # ------------------------------------------------------------------ #
    # 因子打分
    # ------------------------------------------------------------------ #
    def _zscore(self) -> float | None:
        mp = self.param_int("mean_period", 50)
        if len(self._closes) < mp:
            return None
        mean = self._sum / mp
        var = max(0.0, self._sumsq / mp - mean * mean)
        std = math.sqrt(var)
        if std <= EPS:
            return 0.0
        return (self._closes[-1] - mean) / std

    def _vwap_gap(self) -> float | None:
        vp = self.param_int("vwap_period", 20)
        if len(self._vol) < vp or self._v_sum <= EPS:
            return None
        return self._closes[-1] / (self._tv_sum / self._v_sum) - 1.0

    def _score(self) -> float | None:
        z = self._zscore()
        if z is None:
            return None
        w = self.param_float("vwap_weight", 0.0)
        if w == 0.0:
            return z
        vgap = self._vwap_gap()
        if vgap is None:
            return z
        return z + w * (vgap / VWAP_GAP_SCALE)

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def on_bar(self, ctx: BarCtx) -> list[Intent]:
        c = ctx.candle
        price = ctx.price
        atr = self._atr.update(c)

        # 先更新滚动窗口，再算分（只用到 <= 当前收盘的信息，无未来函数）
        if len(self._closes) == self._closes.maxlen:
            old = self._closes[0]
            self._sum -= old
            self._sumsq -= old * old
        self._closes.append(price)
        self._sum += price
        self._sumsq += price * price

        typical = (c.high + c.low + c.close) / 3.0
        if len(self._typ) == self._typ.maxlen:
            self._tv_sum -= self._typ[0] * self._vol[0]
            self._v_sum -= self._vol[0]
        self._typ.append(typical)
        self._vol.append(c.volume_quote)
        self._tv_sum += typical * c.volume_quote
        self._v_sum += c.volume_quote

        tp = self.param_int("short_trend_period", 288)
        if tp > 0:
            if len(self._trend) == self._trend.maxlen:
                self._trend_sum -= self._trend[0]
            self._trend.append(price)
            self._trend_sum += price

        ltp = self.param_int("long_trend_period", 288)
        if ltp > 0:
            if len(self._long_trend) == self._long_trend.maxlen:
                self._long_trend_sum -= self._long_trend[0]
            self._long_trend.append(price)
            self._long_trend_sum += price
            if len(self._long_trend) == ltp:
                self._long_ma_hist.append(self._long_trend_sum / ltp)

        score = self._score()
        if score is None or atr is None or atr <= 0:
            return []

        entry_k = self.param_float("entry_k", 1.5)
        exit_k = self.param_float("exit_k", 0.0)
        stop_mult = self.param_float("stop_atr_mult", 3.0)
        size_pct = self.param_float("size_pct", 1.0)

        # 持仓管理
        if ctx.position > EPS:
            if score >= exit_k:
                return [Intent(Side.SELL, qty=ctx.position, reason="回归均值平多")]
            if stop_mult > 0 and price < self._entry_px - stop_mult * self._entry_atr:
                return [Intent(Side.SELL, qty=ctx.position, reason="ATR 止损平多")]
            if self.param_bool("long_filter_exit", True) and not self._uptrend():
                return [Intent(Side.SELL, qty=ctx.position, reason="跌破长期均线平多")]
            return []
        if ctx.position < -EPS:
            held = abs(ctx.position)
            if score <= -exit_k:
                return [Intent(Side.BUY, qty=held, reason="回归均值平空")]
            if stop_mult > 0 and price > self._entry_px + stop_mult * self._entry_atr:
                return [Intent(Side.BUY, qty=held, reason="ATR 止损平空")]
            return []

        # 反向建仓
        notional = ctx.equity * size_pct
        if notional <= 0:
            return []
        if score <= -entry_k and self._uptrend():
            self._entry_px = price
            self._entry_atr = atr
            return [Intent(Side.BUY, quote_qty=notional, reason="超卖做多")]
        if score >= entry_k and self.param_bool("short_enabled", True):
            if self._short_allowed(price):
                self._entry_px = price
                self._entry_atr = atr
                qty = notional / price if price > 0 else 0.0
                if qty > 0:
                    return [Intent(Side.SELL, qty=qty, allow_short=True, reason="超买做空")]
        return []

    def _short_allowed(self, price: float) -> bool:
        """做空趋势过滤：仅在 close 低于长期 SMA 时允许做空，避开牛市单边。"""
        tp = self.param_int("short_trend_period", 288)
        if tp <= 0:
            return True
        if len(self._trend) < tp:
            return False
        return price < self._trend_sum / tp

    def _uptrend(self) -> bool:
        """做多趋势过滤：防止在单边下跌里接飞刀。

        两种模式：
        - ``above``：close >= 长期 SMA 才做多（严格，会砍掉不少均值回归 alpha）；
        - ``rising``：长期 SMA 相对 ``long_slope_lag`` 前上行才做多（更贴合「回调买入」）。
        """
        ltp = self.param_int("long_trend_period", 288)
        if ltp <= 0:
            return True
        mode = self.param("long_filter_mode", "above")
        if mode == "rising":
            lag = self.param_int("long_slope_lag", 48)
            if len(self._long_ma_hist) <= lag:
                return True
            return self._long_ma_hist[-1] >= self._long_ma_hist[-1 - lag]
        if len(self._long_trend) < ltp:
            return True
        return self._closes[-1] >= self._long_trend_sum / ltp

    def state_dict(self) -> dict:
        return {
            "entry_px": self._entry_px,
            "entry_atr": self._entry_atr,
            "closes": list(self._closes),
        }

    def load_state(self, state: dict) -> None:
        self._entry_px = float(state.get("entry_px", 0.0) or 0.0)
        self._entry_atr = float(state.get("entry_atr", 0.0) or 0.0)
        self._closes.clear()
        for v in state.get("closes", []):
            self._closes.append(float(v))
        self._sum = sum(self._closes)
        self._sumsq = sum(v * v for v in self._closes)


STRATEGY = FactorReversionStrategy
