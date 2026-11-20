"""增量式技术指标。

所有指标都是 O(1) 单步更新（Donchian 用 deque 线性扫描，周期通常很小），
数据不足时返回 ``None``，策略据此判断是否已就绪。
"""

from __future__ import annotations

import math
from collections import deque

from okxquant.domain import Candle


class SMA:
    """简单移动平均。"""

    __slots__ = ("_buf", "_period", "_sum", "_value")

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period 必须为正整数")
        self._period = period
        self._buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    def update(self, x: float) -> float | None:
        if len(self._buf) == self._period:
            self._sum -= self._buf[0]
        self._buf.append(x)
        self._sum += x
        if len(self._buf) == self._period:
            self._value = self._sum / self._period
        return self._value


class VolumeMA:
    """成交量均线，基准**不含当前 K 线**。

    用法固定为「先读 ``value`` 判断放量，再 ``update`` 并入当前量」::

        base = vol_ma.value                      # 前 period 根的均值
        if candle.volume_quote >= 1.2 * base: ...
        vol_ma.update(candle.volume_quote)

    这样当前放量不会抬高自己的基准（方案 §5.1 D.5 的要求）。
    """

    __slots__ = ("_buf", "_period", "_sum")

    def __init__(self, period: int = 20) -> None:
        if period <= 0:
            raise ValueError("period 必须为正整数")
        self._period = period
        self._buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    @property
    def value(self) -> float | None:
        """前 period 根（不含当前）的均值；数据不足返回 None。"""
        if len(self._buf) < self._period:
            return None
        return self._sum / self._period

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, volume: float) -> float | None:
        """并入当前 K 线的成交量，返回「当前这根应使用的基准」（即并入前的均值）。"""
        base = self.value
        if len(self._buf) == self._period:
            self._sum -= self._buf[0]
        self._buf.append(volume)
        self._sum += volume
        return base


class EMA:
    """指数移动平均，首值用前 period 根的算术均值播种。"""

    __slots__ = ("_alpha", "_count", "_period", "_seed_sum", "_value")

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period 必须为正整数")
        self._period = period
        self._alpha = 2.0 / (period + 1.0)
        self._count = 0
        self._seed_sum = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def ready(self) -> bool:
        return self._value is not None

    def update(self, x: float) -> float | None:
        if self._value is None:
            self._seed_sum += x
            self._count += 1
            if self._count >= self._period:
                self._value = self._seed_sum / self._period
            return self._value
        self._value += (x - self._value) * self._alpha
        return self._value


class RSI:
    """Wilder 平滑的相对强弱指数。"""

    __slots__ = ("_avg_gain", "_avg_loss", "_gains", "_losses", "_period", "_prev")

    def __init__(self, period: int = 14) -> None:
        if period <= 1:
            raise ValueError("period 必须大于 1")
        self._period = period
        self._prev: float | None = None
        self._gains: deque[float] = deque(maxlen=period)
        self._losses: deque[float] = deque(maxlen=period)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    @property
    def value(self) -> float | None:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0.0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    @property
    def ready(self) -> bool:
        return self._avg_gain is not None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0

        if self._avg_gain is None or self._avg_loss is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self._period:
                return None
            self._avg_gain = sum(self._gains) / self._period
            self._avg_loss = sum(self._losses) / self._period
        else:
            n = self._period
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        return self.value


class MACD:
    """MACD：DIF = EMA(fast) − EMA(slow)，DEA = EMA(signal, DIF)，柱 = DIF − DEA。"""

    __slots__ = ("_dea", "_dif", "_fast", "_hist", "_slow")

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast <= 0 or slow <= 0 or signal <= 0:
            raise ValueError("周期必须为正整数")
        if fast >= slow:
            raise ValueError("fast 必须小于 slow")
        self._fast = EMA(fast)
        self._slow = EMA(slow)
        self._dea = EMA(signal)
        self._dif: float | None = None
        self._hist: float | None = None

    @property
    def dif(self) -> float | None:
        return self._dif

    @property
    def dea(self) -> float | None:
        return self._dea.value

    @property
    def hist(self) -> float | None:
        return self._hist

    @property
    def ready(self) -> bool:
        return self._dif is not None and self._dea.value is not None

    def update(self, close: float) -> tuple[float | None, float | None, float | None]:
        fast = self._fast.update(close)
        slow = self._slow.update(close)
        if fast is None or slow is None:
            return None, None, None
        self._dif = fast - slow
        # DIF 就绪后才开始喂 DEA，避免用早期不完整的 DIF 污染信号线
        dea = self._dea.update(self._dif)
        if dea is None:
            self._hist = None
            return self._dif, None, None
        self._hist = self._dif - dea
        return self._dif, dea, self._hist


class ATR:
    """平均真实波幅，Wilder 平滑。"""

    __slots__ = ("_avg", "_period", "_prev_close", "_trs")

    def __init__(self, period: int = 14) -> None:
        if period <= 0:
            raise ValueError("period 必须为正整数")
        self._period = period
        self._prev_close: float | None = None
        self._trs: deque[float] = deque(maxlen=period)
        self._avg: float | None = None

    @property
    def value(self) -> float | None:
        return self._avg

    @property
    def ready(self) -> bool:
        return self._avg is not None

    def true_range(self, candle: Candle) -> float:
        if self._prev_close is None:
            return candle.high - candle.low
        return max(
            candle.high - candle.low,
            abs(candle.high - self._prev_close),
            abs(candle.low - self._prev_close),
        )

    def update(self, candle: Candle) -> float | None:
        tr = self.true_range(candle)
        self._prev_close = candle.close
        if self._avg is None:
            self._trs.append(tr)
            if len(self._trs) < self._period:
                return None
            self._avg = sum(self._trs) / self._period
        else:
            n = self._period
            self._avg = (self._avg * (n - 1) + tr) / n
        return self._avg


class ADX:
    """平均趋向指数（Wilder 平滑），与 TA-Lib / TradingView 口径一致。

    TR、+DM、−DM 先做 Wilder 平滑（首值为前 period 根之和，之后
    ``prev - prev/n + x``），``+DI = 100·平滑+DM / 平滑TR``，
    ``DX = 100·|+DI − −DI| / (+DI + −DI)``，ADX 为 DX 的 Wilder 均值。

    ADX 只衡量趋势强度、不含方向；方向请用 :attr:`direction` 或 +DI/−DI 比较。
    """

    __slots__ = (
        "_adx",
        "_avg_minus",
        "_avg_plus",
        "_avg_tr",
        "_count",
        "_dxs",
        "_minus_di",
        "_period",
        "_plus_di",
        "_prev_close",
        "_prev_high",
        "_prev_low",
        "_seed_minus",
        "_seed_plus",
        "_seed_tr",
    )

    def __init__(self, period: int = 14) -> None:
        if period <= 1:
            raise ValueError("period 必须大于 1")
        self._period = period
        self._prev_close: float | None = None
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._seed_tr = 0.0
        self._seed_plus = 0.0
        self._seed_minus = 0.0
        self._count = 0
        self._avg_tr: float | None = None
        self._avg_plus: float | None = None
        self._avg_minus: float | None = None
        self._dxs: deque[float] = deque(maxlen=period)
        self._adx: float | None = None
        self._plus_di: float | None = None
        self._minus_di: float | None = None

    @property
    def value(self) -> float | None:
        return self._adx

    @property
    def adx(self) -> float | None:
        return self._adx

    @property
    def plus_di(self) -> float | None:
        return self._plus_di

    @property
    def minus_di(self) -> float | None:
        return self._minus_di

    @property
    def direction(self) -> int:
        """+1 多头主导 / −1 空头主导 / 0 数据不足或两线持平。"""
        if self._plus_di is None or self._minus_di is None:
            return 0
        if self._plus_di > self._minus_di:
            return 1
        if self._plus_di < self._minus_di:
            return -1
        return 0

    @property
    def ready(self) -> bool:
        return self._adx is not None

    def update(self, candle: Candle) -> float | None:
        high, low, close = candle.high, candle.low, candle.close
        if self._prev_close is None:
            self._prev_high = high
            self._prev_low = low
            self._prev_close = close
            return None

        assert self._prev_high is not None and self._prev_low is not None
        tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        up = high - self._prev_high
        down = self._prev_low - low
        plus_dm = up if up > down and up > 0.0 else 0.0
        minus_dm = down if down > up and down > 0.0 else 0.0

        self._prev_high = high
        self._prev_low = low
        self._prev_close = close

        n = self._period
        if self._avg_tr is None:
            self._seed_tr += tr
            self._seed_plus += plus_dm
            self._seed_minus += minus_dm
            self._count += 1
            if self._count < n:
                return None
            self._avg_tr = self._seed_tr
            self._avg_plus = self._seed_plus
            self._avg_minus = self._seed_minus
        else:
            self._avg_tr = self._avg_tr - self._avg_tr / n + tr
            self._avg_plus = self._avg_plus - self._avg_plus / n + plus_dm
            self._avg_minus = self._avg_minus - self._avg_minus / n + minus_dm

        assert self._avg_tr is not None
        assert self._avg_plus is not None
        assert self._avg_minus is not None

        if self._avg_tr <= 0.0:
            self._plus_di = 0.0
            self._minus_di = 0.0
            dx = 0.0
        else:
            self._plus_di = 100.0 * self._avg_plus / self._avg_tr
            self._minus_di = 100.0 * self._avg_minus / self._avg_tr
            total = self._plus_di + self._minus_di
            dx = 100.0 * abs(self._plus_di - self._minus_di) / total if total > 0 else 0.0

        if self._adx is None:
            self._dxs.append(dx)
            if len(self._dxs) < n:
                return None
            self._adx = sum(self._dxs) / n
        else:
            self._adx = (self._adx * (n - 1) + dx) / n
        return self._adx


class Bollinger:
    """布林带：中轨 SMA，上下轨 ±k 倍标准差。"""

    __slots__ = ("_buf", "_k", "_period", "_sum", "_sumsq", "_lower", "_upper", "_mid")

    def __init__(self, period: int = 20, k: float = 2.0) -> None:
        if period <= 1:
            raise ValueError("period 必须大于 1")
        self._period = period
        self._k = k
        self._buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._sumsq = 0.0
        self._mid: float | None = None
        self._upper: float | None = None
        self._lower: float | None = None

    @property
    def mid(self) -> float | None:
        return self._mid

    @property
    def upper(self) -> float | None:
        return self._upper

    @property
    def lower(self) -> float | None:
        return self._lower

    @property
    def ready(self) -> bool:
        return self._mid is not None

    def update(self, x: float) -> tuple[float | None, float | None, float | None]:
        if len(self._buf) == self._period:
            oldest = self._buf[0]
            self._sum -= oldest
            self._sumsq -= oldest * oldest
        self._buf.append(x)
        self._sum += x
        self._sumsq += x * x
        if len(self._buf) < self._period:
            return None, None, None
        n = self._period
        mid = self._sum / n
        variance = max(0.0, self._sumsq / n - mid * mid)
        sd = math.sqrt(variance)
        self._mid = mid
        self._upper = mid + self._k * sd
        self._lower = mid - self._k * sd
        return self._upper, self._mid, self._lower


class Donchian:
    """唐奇安通道：最近 period 根的最高价与最低价。"""

    __slots__ = ("_highs", "_lows", "_period", "_high", "_low")

    def __init__(self, period: int = 20) -> None:
        if period <= 0:
            raise ValueError("period 必须为正整数")
        self._period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self._high: float | None = None
        self._low: float | None = None

    @property
    def high(self) -> float | None:
        return self._high

    @property
    def low(self) -> float | None:
        return self._low

    @property
    def ready(self) -> bool:
        return self._high is not None and len(self._highs) >= self._period

    def update(self, candle: Candle) -> tuple[float | None, float | None]:
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        if len(self._highs) < self._period:
            return None, None
        self._high = max(self._highs)
        self._low = min(self._lows)
        return self._high, self._low


class Window:
    """定长滑窗，方便策略取最近 N 个值。"""

    __slots__ = ("_buf",)

    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("size 必须为正整数")
        self._buf: deque[float] = deque(maxlen=size)

    def push(self, x: float) -> None:
        self._buf.append(x)

    def __len__(self) -> int:
        return len(self._buf)

    @property
    def last(self) -> float | None:
        return self._buf[-1] if self._buf else None

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(self._buf)
