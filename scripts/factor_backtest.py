#!/usr/bin/env python3
"""因子均值回归策略回测矩阵。

复用 run_backtest，对 factor_reversion 策略做参数网格 + 样本内/样本外切分，
回答「这个因子信号扣掉手续费后到底赚不赚、多空各贡献多少」。

    .venv/bin/python scripts/factor_backtest.py
    .venv/bin/python scripts/factor_backtest.py --bar 5m --inst XRP-USDT-SWAP
"""

from __future__ import annotations

import argparse
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
from okxquant.engine import run_backtest  # noqa: E402
from okxquant.store import Database  # noqa: E402
from okxquant.store.candles import earliest_ts, latest_ts  # noqa: E402
from strategies.factor_reversion import FactorReversionStrategy  # noqa: E402

console = Console(width=None if sys.stdout.isatty() else 130)

TRAIN_RATIO = 0.7


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


def run_one(db, *, inst_id, bar, bar_ms, start_ts, end_ts, params, fee, slip, cash, lev, delay=1):
    strat = FactorReversionStrategy(dict(params))
    result = run_backtest(
        db=db,
        strategy=strat,
        inst_id=inst_id,
        bar=bar,
        bar_ms=bar_ms,
        start_ts=start_ts,
        end_ts=end_ts,
        initial_cash=cash,
        fee_rate=fee,
        slippage_bps=slip,
        sample_bars=0,
        params=strat.params,
        leverage=lev,
        execution_delay=delay,
    )
    m = result.metrics
    years = max((end_ts - start_ts) / 86_400_000 / 365.0, 1e-6)
    total = m.total_return
    annual = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1.0 else -1.0
    return {
        "return": m.total_return,
        "annual": annual,
        "mdd": m.max_drawdown,
        "sharpe": m.sharpe,
        "win": m.win_rate,
        "pf": m.profit_factor,
        "trades": m.trade_count,
        "fees": m.total_fees,
        "exposure": m.exposure,
        "bh": m.buy_hold_return,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst", default="XRP-USDT-SWAP")
    parser.add_argument("--bar", default="5m")
    parser.add_argument("--fee", type=float, default=0.0005)
    parser.add_argument("--slip", type=float, default=2.0)
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--lev", type=float, default=1.0)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    db = Database(cfg.storage.db_path)
    try:
        start_ts = int(earliest_ts(db, args.inst, args.bar) or 0)
        end_ts = int(latest_ts(db, args.inst, args.bar) or 0)
        interval = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1H": 3_600_000, "4H": 14_400_000}[args.bar]
        split = start_ts + int((end_ts - start_ts) * TRAIN_RATIO)

        grids = [
            {"short_enabled": False, "entry_k": 1.5, "stop_atr_mult": 3.0, "size_pct": 0.3},
            {"short_enabled": False, "entry_k": 1.5, "stop_atr_mult": 3.0, "size_pct": 0.5},
            {"short_enabled": False, "entry_k": 1.5, "stop_atr_mult": 3.0, "size_pct": 1.0},
            {"short_enabled": False, "entry_k": 2.0, "stop_atr_mult": 3.0, "size_pct": 1.0},
            {"short_enabled": False, "entry_k": 1.5, "stop_atr_mult": 0.0, "size_pct": 1.0},
        ]

        console.print(
            f"[bold]{args.inst} {args.bar}[/] 全样本 {fmt_ts(start_ts)} ~ {fmt_ts(end_ts)} · "
            f"费 {args.fee:.4%} · 滑点 {args.slip:.1f}bp · 本金 {args.cash:,.0f} · 杠杆 {args.lev} · 次根开盘成交"
        )
        table = Table(box=None, pad_edge=False, title="全样本回测（做多均值回归，按收益排序）", title_style="bold")
        for col in ("参数", "总收益", "年化", "回撤", "夏普", "胜率", "盈亏比", "笔数", "持仓率", "buy&hold"):
            table.add_column(col, justify="right" if col not in ("参数",) else "left")

        results = []
        for g in grids:
            full = run_one(db, inst_id=args.inst, bar=args.bar, bar_ms=interval,
                           start_ts=start_ts, end_ts=end_ts, params=g,
                           fee=args.fee, slip=args.slip, cash=args.cash, lev=args.lev)
            test = run_one(db, inst_id=args.inst, bar=args.bar, bar_ms=interval,
                           start_ts=split, end_ts=end_ts, params=g,
                           fee=args.fee, slip=args.slip, cash=args.cash, lev=args.lev)
            g["test_return"] = test["return"]
            g["test_mdd"] = test["mdd"]
            results.append((g, full))

        def label_of(g: dict) -> str:
            short = "long-only" if not g.get("short_enabled", True) else f"short<MA{g.get('short_trend_period', 288)}"
            return f"{short}·k={g['entry_k']}·stop={g['stop_atr_mult']}·sz={g.get('size_pct', 1.0)}"

        results.sort(key=lambda r: r[1]["return"], reverse=True)
        for g, full in results:
            label = label_of(g)
            table.add_row(
                label,
                f"{full['return']:+.2%}",
                f"{full['annual']:+.2%}",
                f"{full['mdd']:.2%}",
                f"{full['sharpe']:.2f}",
                f"{full['win']:.1%}",
                f"{full['pf']:.2f}",
                str(full["trades"]),
                f"{full['exposure']:.0%}",
                f"{full['bh']:+.2%}",
            )

        console.print(table)

        console.print("\n[bold]样本外（最近 30%：{} ~ {}）[/]".format(fmt_ts(split), fmt_ts(end_ts)))
        ot = Table(box=None, pad_edge=False)
        ot.add_column("参数", style="cyan")
        ot.add_column("样本内收益", justify="right")
        ot.add_column("样本外收益", justify="right")
        ot.add_column("样本外回撤", justify="right")
        for g, full in results:
            label = label_of(g)
            ot.add_row(label, f"{full['return']:+.2%}", f"{g['test_return']:+.2%}", f"{g['test_mdd']:.2%}")
        console.print(ot)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
