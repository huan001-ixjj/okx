"""单标的时序因子库（纯标准库，无未来函数）。

约定：
- 因子函数签名 ``fn(series, **params) -> list[float | None]``，与 series 对齐，预热期为 ``None``。
- 因子只用 ``index <= i`` 的数据（收盘后可用的量，含当前已收盘 K 线的量）。
- 量能一律用 ``volume_quote``（计价币口径），与项目内趋势回调策略一致。

供 ``scripts/factor_scan.py`` 批量检验，也可被策略脚本 import。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from okxquant.domain import Candle
from okxquant.indicators import ADX, MACD
from okxquant.store.candles import iter_candles


@dataclass(slots=True)
class Series:
    ts: list[int] = field(default_factory=list)
    open: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)
    volume: list[float] = field(default_factory=list)
    volume_quote: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.close)


def load_series(db, inst_id: str, bar: str) -> Series:
    """从数据库（SQLite 或 Postgres 均可）分块流式加载完整序列。"""
    s = Series()
    for page in iter_candles(db, inst_id, bar, 0, 10**18):
        for c in page:
            s.ts.append(c.ts)
            s.open.append(c.open)
            s.high.append(c.high)
            s.low.append(c.low)
            s.close.append(c.close)
            s.volume.append(c.volume)
            s.volume_quote.append(c.volume_quote)
    return s


# --------------------------------------------------------------------------- #
# 统计基础件
# --------------------------------------------------------------------------- #


def rolling_mean(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rolling_std(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(vals)
    s = 0.0
    sq = 0.0
    for i, v in enumerate(vals):
        s += v
        sq += v * v
        if i >= n:
            s -= vals[i - n]
            sq -= vals[i - n] ** 2
        if i >= n - 1:
            m = s / n
            var = sq / n - m * m
            out[i] = math.sqrt(var) if var > 0 else 0.0
    return out


def rolling_corr(x: list[float], y: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(x)
    sx = sy = sxx = syy = sxy = 0.0
    for i in range(len(x)):
        xi, yi = x[i], y[i]
        sx += xi
        sy += yi
        sxx += xi * xi
        syy += yi * yi
        sxy += xi * yi
        if i >= n:
            xo, yo = x[i - n], y[i - n]
            sx -= xo
            sy -= yo
            sxx -= xo * xo
            syy -= yo * yo
            sxy -= xo * yo
        if i >= n - 1:
            num = n * sxy - sx * sy
            den = math.sqrt(max(0.0, n * sxx - sx * sx) * max(0.0, n * syy - sy * sy))
            out[i] = num / den if den > 0 else 0.0
    return out


def rankdata(values: list[float]) -> list[float]:
    """带并列平均的秩（0..n-1）。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sxx = syy = 0.0
    for xi, yi in zip(x, y):
        dx = xi - mx
        dy = yi - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    den = math.sqrt(sxx * syy)
    return sxy / den if den > 0 else 0.0


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(rankdata(x), rankdata(y))


# --------------------------------------------------------------------------- #
# 因子实现
# --------------------------------------------------------------------------- #


def _candles(series: Series):
    for i in range(len(series)):
        yield Candle(
            ts=series.ts[i],
            open=series.open[i],
            high=series.high[i],
            low=series.low[i],
            close=series.close[i],
            volume=series.volume[i],
            volume_ccy=series.volume[i],
            volume_quote=series.volume_quote[i],
            confirm=True,
        )


def _rets(series: Series) -> list[float]:
    close = series.close
    out = [0.0] * len(close)
    for i in range(1, len(close)):
        out[i] = close[i] / close[i - 1] - 1.0
    return out


# ---- 动量 / 趋势 ---- #


def mom(series: Series, n: int = 24) -> list[float | None]:
    close = series.close
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        out[i] = close[i] / close[i - n] - 1.0
    return out


def ema_slope(series: Series, fast: int = 20, k: int = 5) -> list[float | None]:
    close = series.close
    alpha = 2.0 / (fast + 1.0)
    ema: list[float | None] = [None] * len(close)
    seed = 0.0
    val: float | None = None
    for i, v in enumerate(close):
        if val is None:
            seed += v
            if i >= fast - 1:
                val = seed / fast
        else:
            val += (v - val) * alpha
        ema[i] = val

    out: list[float | None] = [None] * len(close)
    for i in range(len(close)):
        if i >= k and ema[i] is not None and ema[i - k] is not None and close[i] > 0:
            out[i] = (ema[i] - ema[i - k]) / close[i]
    return out


def macd_hist_norm(series: Series, fast: int = 12, slow: int = 26, signal: int = 9) -> list[float | None]:
    ind = MACD(fast, slow, signal)
    close = series.close
    out: list[float | None] = [None] * len(close)
    for i in range(len(close)):
        _, _, hist = ind.update(close[i])
        if hist is not None and close[i] > 0:
            out[i] = hist / close[i]
    return out


def donchian_pos(series: Series, n: int = 55) -> list[float | None]:
    high, low, close = series.high, series.low, series.close
    out: list[float | None] = [None] * len(close)
    for i in range(n - 1, len(close)):
        h = max(high[i - n + 1 : i + 1])
        l = min(low[i - n + 1 : i + 1])
        rng = h - l
        out[i] = (close[i] - l) / rng if rng > 0 else 0.5
    return out


def di_diff(series: Series, period: int = 14) -> list[float | None]:
    ind = ADX(period)
    out: list[float | None] = [None] * len(series)
    for i, c in enumerate(_candles(series)):
        ind.update(c)
        p, m = ind.plus_di, ind.minus_di
        if p is not None and m is not None:
            out[i] = (p - m) / 100.0
    return out


def adx(series: Series, period: int = 14) -> list[float | None]:
    ind = ADX(period)
    out: list[float | None] = [None] * len(series)
    for i, c in enumerate(_candles(series)):
        out[i] = ind.update(c)
    return out


def newhigh_count(series: Series, n: int = 20) -> list[float | None]:
    close = series.close
    out: list[float | None] = [None] * len(close)
    for i in range(n - 1, len(close)):
        cnt = 0
        for j in range(i - n + 1, i + 1):
            # 当根收盘价高于其前 n 根的最高价视为一次创新高
            lo = max(0, j - n)
            if j > 0 and close[j] > max(close[lo:j]):
                cnt += 1
        out[i] = cnt
    return out


def ma_trend(series: Series, n: int = 50) -> list[float | None]:
    close = series.close
    rm = rolling_mean(close, n)
    out: list[float | None] = [None] * len(close)
    for i in range(len(close)):
        if rm[i] is not None and rm[i] > 0:
            out[i] = close[i] / rm[i] - 1.0
    return out


# ---- 量能 / 量价 ---- #


def vol_ratio(series: Series, n: int = 20) -> list[float | None]:
    vq = series.volume_quote
    rm = rolling_mean(vq, n)
    out: list[float | None] = [None] * len(vq)
    for i in range(len(vq)):
        if rm[i] is not None and rm[i] > 0:
            out[i] = vq[i] / rm[i]
    return out


def vol_zscore(series: Series, n: int = 100) -> list[float | None]:
    vq = series.volume_quote
    rm = rolling_mean(vq, n)
    rs = rolling_std(vq, n)
    out: list[float | None] = [None] * len(vq)
    for i in range(len(vq)):
        if rm[i] is not None and rs[i] is not None and rs[i] > 0:
            out[i] = (vq[i] - rm[i]) / rs[i]
    return out


def obv_roc(series: Series, n: int = 20) -> list[float | None]:
    close = series.close
    vq = series.volume_quote
    obv = [0.0] * len(close)
    run = 0.0
    for i in range(len(close)):
        if i > 0:
            if close[i] > close[i - 1]:
                run += vq[i]
            elif close[i] < close[i - 1]:
                run -= vq[i]
        obv[i] = run
    out: list[float | None] = [None] * len(close)
    for i in range(n, len(close)):
        base = obv[i - n]
        out[i] = (obv[i] - base) / abs(base) if abs(base) > 1e-9 else 0.0
    return out


def corr_vr(series: Series, n: int = 20) -> list[float | None]:
    return rolling_corr(series.volume_quote, _rets(series), n)


def corr_va(series: Series, n: int = 20) -> list[float | None]:
    abs_ret = [abs(r) for r in _rets(series)]
    return rolling_corr(series.volume_quote, abs_ret, n)


def vwap_gap(series: Series, n: int = 20) -> list[float | None]:
    close, high, low, vq = series.close, series.high, series.low, series.volume_quote
    typical = [(high[i] + low[i] + close[i]) / 3.0 for i in range(len(close))]
    out: list[float | None] = [None] * len(close)
    num = 0.0
    den = 0.0
    for i in range(len(close)):
        num += typical[i] * vq[i]
        den += vq[i]
        if i >= n:
            num -= typical[i - n] * vq[i - n]
            den -= vq[i - n]
        if i >= n - 1 and den > 0:
            vwap = num / den
            out[i] = close[i] / vwap - 1.0
    return out


def money_flow(series: Series, n: int = 20) -> list[float | None]:
    close, open_, vq = series.close, series.open, series.volume_quote
    out: list[float | None] = [None] * len(close)
    num = 0.0
    den = 0.0
    for i in range(len(close)):
        sign = 1.0 if close[i] > open_[i] else (-1.0 if close[i] < open_[i] else 0.0)
        num += sign * vq[i]
        den += vq[i]
        if i >= n:
            prev_sign = 1.0 if close[i - n] > open_[i - n] else (-1.0 if close[i - n] < open_[i - n] else 0.0)
            num -= prev_sign * vq[i - n]
            den -= vq[i - n]
        if i >= n - 1 and den > 0:
            out[i] = num / den
    return out


def vol_trend(series: Series, short: int = 5, long: int = 20) -> list[float | None]:
    vq = series.volume_quote
    rs = rolling_mean(vq, short)
    rl = rolling_mean(vq, long)
    out: list[float | None] = [None] * len(vq)
    for i in range(len(vq)):
        if rs[i] is not None and rl[i] is not None and rl[i] > 0:
            out[i] = rs[i] / rl[i]
    return out


# --------------------------------------------------------------------------- #
# 因子注册表
# --------------------------------------------------------------------------- #

FACTORS: list[tuple[str, callable, dict]] = [
    # 动量 / 趋势
    ("mom_6", mom, {"n": 6}),
    ("mom_24", mom, {"n": 24}),
    ("mom_72", mom, {"n": 72}),
    ("mom_288", mom, {"n": 288}),
    ("ema_slope", ema_slope, {"fast": 20, "k": 5}),
    ("macd_hist", macd_hist_norm, {"fast": 12, "slow": 26, "signal": 9}),
    ("donchian_pos", donchian_pos, {"n": 55}),
    ("di_diff", di_diff, {"period": 14}),
    ("adx", adx, {"period": 14}),
    ("newhigh_count", newhigh_count, {"n": 20}),
    ("ma_trend", ma_trend, {"n": 50}),
    # 量能 / 量价
    ("vol_ratio", vol_ratio, {"n": 20}),
    ("vol_zscore", vol_zscore, {"n": 100}),
    ("obv_roc", obv_roc, {"n": 20}),
    ("corr_vr", corr_vr, {"n": 20}),
    ("corr_va", corr_va, {"n": 20}),
    ("vwap_gap", vwap_gap, {"n": 20}),
    ("money_flow", money_flow, {"n": 20}),
    ("vol_trend", vol_trend, {"short": 5, "long": 20}),
]


FACTOR_BY_NAME: dict[str, tuple[callable, dict]] = {name: (fn, params) for name, fn, params in FACTORS}
