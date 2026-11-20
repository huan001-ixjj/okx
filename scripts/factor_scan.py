#!/usr/bin/env python3
"""单标的因子批量检验：IC / ICIR / 分位收益 / 单调性 / 换手代理。

对每个因子 × 前视 horizon，计算时间序列 Spearman IC、分块 ICIR、五分位收益
（top-bottom 价差与单调性）、样本内/样本外 IC，以及因子一阶自相关（换手代理）。

用法::

    .venv/bin/python scripts/factor_scan.py --list
    .venv/bin/python scripts/factor_scan.py --inst XRP-USDT-SWAP --bar 5m \
        --horizons 6,24,72,288
    .venv/bin/python scripts/factor_scan.py --inst XRP-USDT-SWAP --bar 15m \
        --horizons 4,24,96,384 --json

无未来函数：factor[i] 只用 <= i 的数据，forward_ret[i] 从 i 起算未来 h 根。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from okxquant.config import load_config  # noqa: E402
from okxquant.store import Database  # noqa: E402

from scripts.factors import (  # noqa: E402
    FACTORS,
    load_series,
    pearson,
    rankdata,
    spearman,
)

console = Console(width=None if sys.stdout.isatty() else 140)

TRAIN_RATIO = 0.7
QUANTILES = 5
ICIR_BLOCKS = 20


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


def forward_returns(close: list[float], h: int) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    for i in range(len(close) - h):
        if close[i] > 0:
            out[i] = close[i + h] / close[i] - 1.0
    return out


def valid_idx(factor: list[float | None], ret: list[float | None]) -> list[int]:
    return [i for i in range(len(factor)) if factor[i] is not None and ret[i] is not None]


def ic_block(factor: list[float | None], ret: list[float | None], idxs: list[int]) -> tuple[float, float, int]:
    """分块 ICIR：把有效样本按时间切成块，块内 IC 的 mean/std，返回 (icir, t, nblocks)。"""
    n = len(idxs)
    if n < ICIR_BLOCKS * 5:
        return 0.0, 0.0, 0
    ics: list[float] = []
    for b in range(ICIR_BLOCKS):
        lo = n * b // ICIR_BLOCKS
        hi = n * (b + 1) // ICIR_BLOCKS
        sub = idxs[lo:hi]
        if len(sub) < 5:
            continue
        x = [factor[i] for i in sub]  # type: ignore[index]
        y = [ret[i] for i in sub]  # type: ignore[index]
        ics.append(spearman(x, y))
    if len(ics) < 3:
        return 0.0, 0.0, 0
    m = sum(ics) / len(ics)
    sd = math.sqrt(sum((v - m) ** 2 for v in ics) / len(ics))
    icir = m / sd if sd > 0 else 0.0
    t = icir * math.sqrt(len(ics)) if sd > 0 else 0.0
    return icir, t, len(ics)


def quantile_stats(factor: list[float | None], ret: list[float | None], idxs: list[int]):
    """五分位：各组平均收益、top-bottom 价差、单调性、top 组胜率。"""
    pairs = [(factor[i], ret[i]) for i in idxs]  # type: ignore[index]
    pairs.sort(key=lambda p: p[0])
    n = len(pairs)
    groups: list[list[float]] = [[] for _ in range(QUANTILES)]
    for k, (_, r) in enumerate(pairs):
        g = min(QUANTILES - 1, k * QUANTILES // n)
        groups[g].append(r)

    means = [sum(g) / len(g) if g else 0.0 for g in groups]
    spread = means[-1] - means[0]
    mono = spearman([float(i) for i in range(QUANTILES)], means)
    top_win = sum(1 for r in groups[-1] if r > 0) / len(groups[-1]) if groups[-1] else 0.0
    return means, spread, mono, top_win


def factor_autocorr(factor: list[float | None], idxs: list[int]) -> float:
    """因子一阶自相关（换手代理：越高换手越低）。"""
    xs: list[float] = []
    ys: list[float] = []
    for k in range(len(idxs) - 1):
        a, b = idxs[k], idxs[k + 1]
        if b == a + 1:  # 相邻 bar 才有意义
            xs.append(factor[a])  # type: ignore[arg-type]
            ys.append(factor[b])  # type: ignore[arg-type]
    if len(xs) < 10:
        return 0.0
    return pearson(xs, ys)


def list_inventory(db: Database) -> None:
    rows = db.execute(
        "SELECT inst_id, bar, COUNT(*) n, MIN(ts) lo, MAX(ts) hi "
        "FROM candles GROUP BY inst_id, bar ORDER BY inst_id, bar"
    ).fetchall()
    table = Table(box=None, pad_edge=False, title="数据盘面", title_style="bold")
    table.add_column("标的", style="cyan")
    table.add_column("周期", style="cyan")
    table.add_column("根数", justify="right")
    table.add_column("起", justify="right")
    table.add_column("止", justify="right")
    for r in rows:
        table.add_row(
            str(r["inst_id"]), str(r["bar"]), f"{r['n']:,}",
            fmt_ts(int(r["lo"])), fmt_ts(int(r["hi"])),
        )
    console.print(table)


def main() -> int:
    parser = argparse.ArgumentParser(description="单标的因子批量检验")
    parser.add_argument("--inst", default="XRP-USDT-SWAP")
    parser.add_argument("--bar", default="5m")
    parser.add_argument("--horizons", default="6,24,72,288", help="前视根数，逗号分隔")
    parser.add_argument("--factors", default="", help="只测指定因子，逗号分隔；空=全部")
    parser.add_argument("--list", action="store_true", help="只看数据盘面")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    db = Database(cfg.storage.db_path)
    try:
        if args.list:
            list_inventory(db)
            return 0

        series = load_series(db, args.inst, args.bar)
        if len(series) < 500:
            console.print(f"[red]数据不足[/] {args.inst} {args.bar} 只有 {len(series)} 根")
            return 1
    finally:
        db.close()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    want = {f.strip() for f in args.factors.split(",") if f.strip()}
    chosen = [f for f in FACTORS if not want or f[0] in want]

    # 每个 horizon 的 forward return 只算一次
    ret_cache = {h: forward_returns(series.close, h) for h in horizons}
    split_idx = int(len(series) * TRAIN_RATIO)

    rows: list[dict] = []
    for name, fn, params in chosen:
        try:
            factor = fn(series, **params)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]因子 {name} 计算失败：{exc}[/]")
            continue
        for h in horizons:
            ret = ret_cache[h]
            idxs = valid_idx(factor, ret)
            if len(idxs) < 100:
                continue
            ic = spearman([factor[i] for i in idxs], [ret[i] for i in idxs])  # type: ignore[index]
            icir, t, nblocks = ic_block(factor, ret, idxs)
            means, spread, mono, top_win = quantile_stats(factor, ret, idxs)

            tr = [i for i in idxs if i < split_idx]
            te = [i for i in idxs if i >= split_idx]
            ic_train = spearman([factor[i] for i in tr], [ret[i] for i in tr]) if len(tr) >= 100 else 0.0  # type: ignore[index]
            ic_test = spearman([factor[i] for i in te], [ret[i] for i in te]) if len(te) >= 100 else 0.0  # type: ignore[index]
            ac1 = factor_autocorr(factor, idxs)

            rows.append(
                {
                    "factor": name,
                    "horizon": h,
                    "n": len(idxs),
                    "ic": ic,
                    "icir": icir,
                    "t": t,
                    "spread": spread,
                    "mono": mono,
                    "top_win": top_win,
                    "top_ret": means[-1],
                    "bottom_ret": means[0],
                    "ic_train": ic_train,
                    "ic_test": ic_test,
                    "ac1": ac1,
                }
            )

    if not rows:
        console.print("[yellow]没有产生任何有效样本[/]")
        return 1

    # 排序：|IC| 降序
    rows.sort(key=lambda r: abs(r["ic"]), reverse=True)

    if args.json:
        payload = {
            "inst_id": args.inst,
            "bar": args.bar,
            "bars": len(series),
            "start": fmt_ts(series.ts[0]),
            "end": fmt_ts(series.ts[-1]),
            "horizons": horizons,
            "rows": rows,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console.print(
        f"[bold]{args.inst} {args.bar}[/] {len(series):,} 根 "
        f"({fmt_ts(series.ts[0])} ~ {fmt_ts(series.ts[-1])})"
    )
    console.print(
        f"[dim]IC=全样本秩相关  ICIR=分块稳定性  spread=top-bottom 分位收益差  "
        f"mono=单调性  ac1=一阶自相关(低=换手高)[/]"
    )

    table = Table(box=None, pad_edge=False, title="因子检验排序（按 |IC| 降序）", title_style="bold")
    table.add_column("因子", style="cyan")
    table.add_column("h", justify="right")
    table.add_column("n", justify="right")
    table.add_column("IC", justify="right")
    table.add_column("ICIR", justify="right")
    table.add_column("t", justify="right")
    table.add_column("spread", justify="right")
    table.add_column("mono", justify="right")
    table.add_column("IC_train", justify="right")
    table.add_column("IC_test", justify="right")
    table.add_column("ac1", justify="right")

    for r in rows:
        def ic_color(v: float) -> str:
            return f"[green]{v:+.3f}[/]" if v > 0 else f"[red]{v:+.3f}[/]"

        table.add_row(
            r["factor"],
            str(r["horizon"]),
            f"{r['n']:,}",
            ic_color(r["ic"]),
            f"{r['icir']:+.2f}",
            f"{r['t']:+.1f}",
            f"{r['spread']:+.4%}",
            f"{r['mono']:+.2f}",
            ic_color(r["ic_train"]),
            ic_color(r["ic_test"]),
            f"{r['ac1']:.2f}",
        )
    console.print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
