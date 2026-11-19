#!/usr/bin/env python3
"""波动率诊断：判断「抓波动」这件事到底能不能做。

回答三个问题：

1. **现在有多波动**——当前 ATR/价格 在过去一年里的分位。
2. **波动会不会聚集**——高波动之后是否仍是高波动（自相关）。
   这是唯一被反复验证过的可预测性来源，比预测涨跌可靠得多。
3. **波动放大之后怎么走**——按波动率分位分组，看未来 N 根的方向与幅度。

用法::

    .venv/bin/python scripts/vol_diag.py --bar 1H --horizon 24
    .venv/bin/python scripts/vol_diag.py --bar 15m --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.table import Table

from okxquant.config import load_config

console = Console(width=None if sys.stdout.isatty() else 110)

ATR_PERIOD = 14
QUANTILES = 5


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


def true_range(series: Series) -> list[float]:
    out = [0.0] * len(series)
    for i in range(1, len(series)):
        high, low, prev = series.high[i], series.low[i], series.close[i - 1]
        out[i] = max(high - low, abs(high - prev), abs(low - prev))
    return out


def wilder_atr(trs: list[float], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder 平滑 ATR，前 period 根不产出。"""
    out: list[float | None] = [None] * len(trs)
    if len(trs) <= period:
        return out
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    for i in range(period + 1, len(trs)):
        prev = out[i - 1]
        assert prev is not None
        out[i] = (prev * (period - 1) + trs[i]) / period
    return out


def atr_pct_series(series: Series) -> list[float | None]:
    atr = wilder_atr(true_range(series))
    out: list[float | None] = []
    for i, value in enumerate(atr):
        if value is None or series.close[i] <= 0:
            out.append(None)
        else:
            out.append(value / series.close[i])
    return out


def percentile_rank(values: list[float], target: float) -> float:
    if not values:
        return 0.0
    below = sum(1 for v in values if v <= target)
    return below / len(values)


def autocorr(values: list[float], lag: int) -> float:
    n = len(values)
    if n <= lag + 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values)
    if var <= 0:
        return 0.0
    cov = sum((values[i] - mean) * (values[i + lag] - mean) for i in range(n - lag))
    return cov / var


def forward_return(series: Series, start: int, horizon: int) -> float | None:
    end = start + horizon
    if end >= len(series):
        return None
    base = series.close[start]
    if base <= 0:
        return None
    return series.close[end] / base - 1.0


@dataclass(slots=True)
class Bucket:
    label: str
    count: int = 0
    mean_abs: float = 0.0
    mean_ret: float = 0.0
    up_ratio: float = 0.0
    p90_abs: float = 0.0


def bucket_analysis(
    series: Series, atr_pct: list[float | None], horizon: int
) -> tuple[list[Bucket], list[tuple[float, float]]]:
    """按历史分位把 ATR% 分组，统计每组的未来收益。"""
    samples: list[tuple[float, float]] = []  # (atr_pct, forward_return)
    for i, value in enumerate(atr_pct):
        if value is None:
            continue
        ret = forward_return(series, i, horizon)
        if ret is None:
            continue
        samples.append((value, ret))

    if not samples:
        return [], []

    ordered = sorted(v for v, _ in samples)
    cuts = [ordered[int(len(ordered) * q / QUANTILES)] for q in range(1, QUANTILES)]

    def bucket_of(value: float) -> int:
        for idx, cut in enumerate(cuts):
            if value <= cut:
                return idx
        return QUANTILES - 1

    grouped: list[list[float]] = [[] for _ in range(QUANTILES)]
    for value, ret in samples:
        grouped[bucket_of(value)].append(ret)

    buckets: list[Bucket] = []
    for idx, members in enumerate(grouped):
        if not members:
            buckets.append(Bucket(label=f"Q{idx + 1}"))
            continue
        abs_returns = sorted(abs(r) for r in members)
        p90_index = min(int(len(abs_returns) * 0.9), len(abs_returns) - 1)
        buckets.append(
            Bucket(
                label=f"Q{idx + 1}",
                count=len(members),
                mean_abs=sum(abs_returns) / len(abs_returns),
                mean_ret=sum(members) / len(members),
                up_ratio=sum(1 for r in members if r > 0) / len(members),
                p90_abs=abs_returns[p90_index],
            )
        )
    return buckets, samples


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="波动率诊断")
    parser.add_argument("--inst", default="BTC-USDT")
    parser.add_argument("--bar", default="1H")
    parser.add_argument("--horizon", type=int, default=24, help="前视根数")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    series = load_series(str(cfg.storage.db_path), args.inst, args.bar)
    if len(series) < ATR_PERIOD * 10:
        console.print(f"[red]数据不足[/] 只有 {len(series)} 根，先运行 download")
        return 1

    atr_pct = atr_pct_series(series)
    valid = [v for v in atr_pct if v is not None]
    current = valid[-1] if valid else 0.0
    rank = percentile_rank(valid, current)

    ac = {lag: autocorr(valid, lag) for lag in (1, 2, 5, 10, 20)}
    buckets, _ = bucket_analysis(series, atr_pct, args.horizon)

    payload: dict[str, Any] = {
        "inst_id": args.inst,
        "bar": args.bar,
        "bars": len(series),
        "start": fmt_ts(series.ts[0]),
        "end": fmt_ts(series.ts[-1]),
        "horizon": args.horizon,
        "current_atr_pct": current,
        "current_percentile": rank,
        "autocorr": ac,
        "buckets": [
            {
                "label": b.label,
                "count": b.count,
                "mean_abs": b.mean_abs,
                "mean_ret": b.mean_ret,
                "up_ratio": b.up_ratio,
                "p90_abs": b.p90_abs,
            }
            for b in buckets
        ],
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console.print(
        f"[bold]{args.inst} {args.bar}[/] {len(series):,} 根 "
        f"({fmt_ts(series.ts[0])} ~ {fmt_ts(series.ts[-1])})，"
        f"前视 {args.horizon} 根"
    )
    console.print()

    console.print("[bold]1. 当前波动水平[/]")
    console.print(
        f"   ATR({ATR_PERIOD})/价格 = [bold]{current:.4%}[/]，"
        f"处于样本内 [bold]{rank:.1%}[/] 分位"
    )
    console.print(f"   均值 {sum(valid) / len(valid):.4%}，最大 {max(valid):.4%}")
    console.print()

    console.print("[bold]2. 波动率是否聚集[/]（自相关，越接近 1 越持续）")
    table = Table(box=None, pad_edge=False)
    table.add_column("滞后阶数")
    for lag in ac:
        table.add_column(f"{lag}", justify="right")
    table.add_row("自相关", *[f"{v:+.3f}" for v in ac.values()])
    console.print(table)
    console.print()

    console.print(f"[bold]3. 波动分位 vs 未来 {args.horizon} 根收益[/]")
    bt = Table(box=None, pad_edge=False)
    bt.add_column("波动分组", style="cyan")
    bt.add_column("样本", justify="right")
    bt.add_column("平均|收益|", justify="right")
    bt.add_column("P90|收益|", justify="right")
    bt.add_column("平均收益", justify="right")
    bt.add_column("上涨占比", justify="right")
    for b in buckets:
        bt.add_row(
            b.label,
            str(b.count),
            f"{b.mean_abs:.2%}",
            f"{b.p90_abs:.2%}",
            f"{b.mean_ret:+.3%}",
            f"{b.up_ratio:.1%}",
        )
    console.print(bt)
    console.print()

    console.print("[bold]结论[/]")
    strong_abs = ac.get(1, 0) > 0.3 and ac.get(5, 0) > 0.2
    verdict = (
        "[green]显著[/]（可用波动率做仓位/入场过滤）"
        if strong_abs
        else "[yellow]弱[/]（波动率难以预测，突破策略收益有限）"
    )
    console.print(f"  波动聚集：{verdict}")
    if buckets:
        low, high = buckets[0], buckets[-1]
        if low.mean_abs > 0:
            ratio = high.mean_abs / low.mean_abs
            console.print(f"  高波动组的平均波动幅度是低波动组的 [bold]{ratio:.2f}×[/]")
    console.print(
        "  [dim]注意：平均收益为负不代表做空能赚，现货只能做多；"
        "关键看高波动组的上涨占比是否明显高于 50%[/]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
