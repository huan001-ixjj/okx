#!/usr/bin/env python3
"""策略选型诊断：这批数据该做趋势还是均值回归。

不看策略，只看数据本身的性质：

1. **Hurst 指数**——>0.55 有持续性（趋势），<0.45 有反持续性（回归），0.5 附近是随机游走
2. **方差比 VR(q)**——>1 表示 q 期收益有正相关（动量），<1 表示负相关（反转）
3. **突破 vs 抄底**——同一份数据上，突破前高和跌破前低之后的收益对比
4. **持有期收益**——持有不同根数的平均收益与胜率，用来判断「拿多久」合理

用法::

    .venv/bin/python scripts/style_diag.py --bar 1H
    .venv/bin/python scripts/style_diag.py --bar 15m --json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.table import Table

from okxquant.config import load_config

console = Console(width=None if sys.stdout.isatty() else 110)

HOLD_HORIZONS = (1, 4, 12, 24, 72, 168)
LOOKBACKS = (20, 55, 120)


@dataclass(slots=True)
class Series:
    ts: list[int] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.close)


def load_series(db_path: str, inst_id: str, bar: str) -> Series:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, high, low, close FROM candles "
            "WHERE inst_id = ? AND bar = ? ORDER BY ts",
            (inst_id, bar),
        ).fetchall()
    finally:
        conn.close()

    series = Series()
    for ts, high, low, close in rows:
        series.ts.append(int(ts))
        series.high.append(float(high))
        series.low.append(float(low))
        series.close.append(float(close))
    return series


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def stdev(values: list[float]) -> float:
    return math.sqrt(variance(values))


def linreg_slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return num / den


def hurst(values: list[float], max_lag: int = 64) -> float:
    """聚合方差法：std(X(t+τ)-X(t)) ∝ τ^H，取 log-log 斜率即为 H。

    注意斜率本身就是 H（不是斜率×2）：随机游走时斜率约 0.5。
    """
    if len(values) < max_lag * 2:
        return 0.5
    points: list[tuple[float, float]] = []
    for lag in range(2, max_lag):
        diffs = [values[i + lag] - values[i] for i in range(len(values) - lag)]
        sd = stdev(diffs)
        if sd > 0:
            points.append((math.log(lag), math.log(sd)))
    if len(points) < 5:
        return 0.5
    return linreg_slope([p[0] for p in points], [p[1] for p in points])


def variance_ratio(rets: list[float], q: int) -> float:
    v1 = variance(rets)
    if v1 <= 0 or len(rets) < q * 3:
        return 1.0
    agg = [sum(rets[i : i + q]) for i in range(0, len(rets) - q + 1, q)]
    vq = variance(agg)
    return vq / (q * v1) if vq > 0 else 1.0


def autocorr(rets: list[float], lag: int) -> float:
    n = len(rets)
    if n <= lag + 2:
        return 0.0
    m = mean(rets)
    var = sum((r - m) ** 2 for r in rets)
    if var <= 0:
        return 0.0
    cov = sum((rets[i] - m) * (rets[i + lag] - m) for i in range(n - lag))
    return cov / var


@dataclass(slots=True)
class SignalStat:
    lookback: int
    horizon: int
    count: int = 0
    mean_ret: float = 0.0
    win_rate: float = 0.0
    median_ret: float = 0.0


def breakout_stat(series: Series, lookback: int, horizon: int) -> SignalStat:
    """收盘价突破前 N 根最高价后，未来 horizon 根的收益。"""
    stat = SignalStat(lookback=lookback, horizon=horizon)
    rets: list[float] = []
    for i in range(lookback, len(series) - horizon):
        prior_high = max(series.high[i - lookback : i])
        if series.close[i] > prior_high:
            rets.append(series.close[i + horizon] / series.close[i] - 1.0)
    if rets:
        ordered = sorted(rets)
        stat.count = len(rets)
        stat.mean_ret = mean(rets)
        stat.win_rate = sum(1 for r in rets if r > 0) / len(rets)
        stat.median_ret = ordered[len(ordered) // 2]
    return stat


def dip_stat(series: Series, lookback: int, horizon: int) -> SignalStat:
    """收盘价跌破前 N 根最低价后，未来 horizon 根的收益（抄底视角）。"""
    stat = SignalStat(lookback=lookback, horizon=horizon)
    rets: list[float] = []
    for i in range(lookback, len(series) - horizon):
        prior_low = min(series.low[i - lookback : i])
        if series.close[i] < prior_low:
            rets.append(series.close[i + horizon] / series.close[i] - 1.0)
    if rets:
        ordered = sorted(rets)
        stat.count = len(rets)
        stat.mean_ret = mean(rets)
        stat.win_rate = sum(1 for r in rets if r > 0) / len(rets)
        stat.median_ret = ordered[len(ordered) // 2]
    return stat


def holding_stats(series: Series, horizon: int) -> tuple[int, float, float]:
    rets = [
        series.close[i + horizon] / series.close[i] - 1.0
        for i in range(len(series) - horizon)
    ]
    if not rets:
        return 0, 0.0, 0.0
    return len(rets), mean(rets), sum(1 for r in rets if r > 0) / len(rets)


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="策略选型诊断")
    parser.add_argument("--inst", default="BTC-USDT")
    parser.add_argument("--bar", default="1H")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    series = load_series(str(cfg.storage.db_path), args.inst, args.bar)
    if len(series) < 300:
        console.print(f"[red]数据不足[/] 只有 {len(series)} 根")
        return 1

    log_close = [math.log(c) for c in series.close if c > 0]
    rets = [
        series.close[i] / series.close[i - 1] - 1.0 for i in range(1, len(series))
    ]

    h = hurst(log_close)
    vr = {q: variance_ratio(rets, q) for q in (2, 5, 10, 20)}
    ac = {lag: autocorr(rets, lag) for lag in (1, 2, 5, 10)}

    breakout = {lb: breakout_stat(series, lb, 24) for lb in LOOKBACKS}
    dips = {lb: dip_stat(series, lb, 24) for lb in LOOKBACKS}
    holds = {hz: holding_stats(series, hz) for hz in HOLD_HORIZONS}

    payload: dict[str, Any] = {
        "inst_id": args.inst,
        "bar": args.bar,
        "bars": len(series),
        "start": fmt_ts(series.ts[0]),
        "end": fmt_ts(series.ts[-1]),
        "first_close": series.close[0],
        "last_close": series.close[-1],
        "total_return": series.close[-1] / series.close[0] - 1.0,
        "hurst": h,
        "variance_ratio": vr,
        "autocorr": ac,
        "breakout": {
            str(lb): {
                "count": s.count,
                "mean_ret": s.mean_ret,
                "win_rate": s.win_rate,
                "median_ret": s.median_ret,
            }
            for lb, s in breakout.items()
        },
        "dip": {
            str(lb): {
                "count": s.count,
                "mean_ret": s.mean_ret,
                "win_rate": s.win_rate,
                "median_ret": s.median_ret,
            }
            for lb, s in dips.items()
        },
        "holding": {
            str(hz): {"count": c, "mean_ret": m, "win_rate": w}
            for hz, (c, m, w) in holds.items()
        },
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console.print(
        f"[bold]{args.inst} {args.bar}[/] {len(series):,} 根 "
        f"({fmt_ts(series.ts[0])} ~ {fmt_ts(series.ts[-1])})"
    )
    console.print(
        f"  区间涨跌：[bold]{payload['total_return']:+.2%}[/] "
        f"({series.close[0]:,.0f} → {series.close[-1]:,.0f})"
    )
    console.print()

    console.print("[bold]1. 持续性检验[/]")
    console.print(
        f"  Hurst = [bold]{h:.3f}[/]  "
        + (
            "[green]趋势性[/]"
            if h > 0.55
            else ("[cyan]均值回归[/]" if h < 0.45 else "[yellow]随机游走[/]")
        )
    )
    vt = Table(box=None, pad_edge=False)
    vt.add_column("q", style="dim")
    for q in vr:
        vt.add_column(str(q), justify="right")
    vt.add_row("VR(q)", *[f"{v:.3f}" for v in vr.values()])
    vt.add_row("自相关", *[f"{ac.get(q, 0):+.3f}" for q in vr])
    console.print(vt)
    console.print("  [dim]VR>1 动量、VR<1 反转；自相关绝对值 <0.05 视为噪声[/]")
    console.print()

    console.print("[bold]2. 突破 vs 抄底[/]（未来 24 根收益）")
    ct = Table(box=None, pad_edge=False)
    ct.add_column("信号", style="cyan")
    ct.add_column("回看", justify="right")
    ct.add_column("次数", justify="right")
    ct.add_column("平均收益", justify="right")
    ct.add_column("中位收益", justify="right")
    ct.add_column("胜率", justify="right")
    for lb, stat in breakout.items():
        ct.add_row(
            "突破前高",
            str(lb),
            str(stat.count),
            f"{stat.mean_ret:+.3%}",
            f"{stat.median_ret:+.3%}",
            f"{stat.win_rate:.1%}",
        )
    for lb, stat in dips.items():
        ct.add_row(
            "跌破前低",
            str(lb),
            str(stat.count),
            f"{stat.mean_ret:+.3%}",
            f"{stat.median_ret:+.3%}",
            f"{stat.win_rate:.1%}",
        )
    console.print(ct)
    console.print()

    console.print("[bold]3. 持有期收益[/]（平均与胜率）")
    ht = Table(box=None, pad_edge=False)
    ht.add_column("持有根数", style="cyan")
    ht.add_column("样本", justify="right")
    ht.add_column("平均收益", justify="right")
    ht.add_column("胜率", justify="right")
    for hz, (count, m, w) in holds.items():
        ht.add_row(str(hz), str(count), f"{m:+.3%}", f"{w:.1%}")
    console.print(ht)
    console.print()

    console.print("[bold]结论[/]")
    if h > 0.55:
        console.print("  Hurst > 0.55：数据有持续性，[green]趋势跟随[/]方向合理")
    elif h < 0.45:
        console.print("  Hurst < 0.45：数据有反持续性，[cyan]均值回归[/]方向合理")
    else:
        console.print("  Hurst ≈ 0.5：接近随机游走，[yellow]单靠价格序列难有优势[/]")

    best_break = max(breakout.values(), key=lambda s: s.mean_ret)
    best_dip = max(dips.values(), key=lambda s: s.mean_ret)
    if best_break.count and best_dip.count:
        if best_break.mean_ret > best_dip.mean_ret:
            console.print(
                f"  突破优于抄底：突破 {best_break.mean_ret:+.3%} vs "
                f"抄底 {best_dip.mean_ret:+.3%}"
            )
        else:
            console.print(
                f"  抄底优于突破：抄底 {best_dip.mean_ret:+.3%} vs "
                f"突破 {best_break.mean_ret:+.3%}"
            )
    console.print(
        f"  [dim]区间整体 {payload['total_return']:+.2%}，"
        "任何只做多策略都要先跑赢这个基准[/]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
