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
