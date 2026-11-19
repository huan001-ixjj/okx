#!/usr/bin/env python3
"""批量回测矩阵：策略 × 周期。

复用 CLI 同一条回测内核（``run_backtest``），逐组跑完并汇总成一张对比表，
用于回答「哪个策略在哪个周期上表现更好」。

    .venv/bin/python scripts/backtest_matrix.py --days 60
    .venv/bin/python scripts/backtest_matrix.py --bars 5m,15m --strategies ma_cross
    .venv/bin/python scripts/backtest_matrix.py --days 30 --json

注意：矩阵只改变周期与参数，不改变策略逻辑；结论只对该交易对、该段时间生效。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table

from okxquant.commands._common import load_strategy_with_params, resolve_window
from okxquant.config import bar_to_ms, load_config
from okxquant.domain import Metrics
from okxquant.engine import run_backtest
from okxquant.render import fmt_money, fmt_pct, pnl_style
from okxquant.store import Database
from okxquant.store.candles import count_candles

# 非 TTY（远程管道）下 rich 默认按 80 列渲染，会把多列表格挤成一团
console = Console(width=None if sys.stdout.isatty() else 110)

DEFAULT_BARS = ["1m", "5m", "15m", "1H"]
DEFAULT_STRATEGIES = ["ma_cross", "grid"]

# 网格必须覆盖当前价格区间，否则价格越界会直接暂停开仓，导致零交易
GRID_DEFAULTS: dict[str, Any] = {
    "lower": 50_000,
    "upper": 80_000,
    "grids": 20,
    "qty_per_grid": 0.001,
    "max_inventory": 0.02,
}

MIN_SAMPLE = 10


@dataclass(slots=True)
class Cell:
    strategy: str
    bar: str
    run_id: int = 0
    metrics: Metrics | None = None
    error: str = ""
    fee_ratio: float = 0.0
    bars_available: int = 0

    @property
    def expectancy(self) -> float:
        if self.metrics is None or self.metrics.trade_count == 0:
            return 0.0
        return self.metrics.total_return / self.metrics.trade_count

    @property
    def sample_note(self) -> str:
        if self.metrics is None:
            return ""
        if self.metrics.trade_count == 0:
            return "未触发"
        if self.metrics.trade_count < MIN_SAMPLE:
            return "样本少"
        return ""

    def to_row(self) -> dict[str, Any]:
        m = self.metrics
        if m is None:
            return {
                "strategy": self.strategy,
                "bar": self.bar,
                "error": self.error,
                "bars_available": self.bars_available,
            }
        return {
            "strategy": self.strategy,
            "bar": self.bar,
            "run_id": self.run_id,
            "trades": m.trade_count,
            "win_rate": round(m.win_rate, 4),
            "profit_factor": round(m.profit_factor, 4),
            "expectancy": round(self.expectancy, 6),
            "total_return": round(m.total_return, 6),
            "buy_hold_return": round(m.buy_hold_return, 6),
            "max_drawdown": round(m.max_drawdown, 6),
            "fee_ratio": round(self.fee_ratio, 6),
            "bars": m.bars,
            "note": self.sample_note,
        }


@dataclass(slots=True)
class Matrix:
    cells: list[Cell] = field(default_factory=list)
    days: int = 0
    inst_id: str = ""

    def best_by_bar(self) -> dict[str, Cell]:
        best: dict[str, Cell] = {}
        for cell in self.cells:
            if cell.metrics is None or cell.metrics.trade_count < MIN_SAMPLE:
                continue
            current = best.get(cell.bar)
            if current is None or cell.metrics.total_return > current.metrics.total_return:
                best[cell.bar] = cell
        return best


def build_params(strategy: str, overrides: dict[str, Any]) -> dict[str, Any]:
    base = dict(GRID_DEFAULTS) if strategy == "grid" else {}
    base.update(overrides)
    return base


def run_one(
    *,
    db: Database,
    cfg: Any,
    strategy_name: str,
    bar: str,
    days: int,
    inst_id: str,
    overrides: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    cash: float,
    leverage: float = 1.0,
) -> Cell:
    cell = Cell(strategy=strategy_name, bar=bar)
    try:
        start_ms, end_ms, interval = resolve_window(
            cfg, days=days, start=None, end=None, bar=bar
        )
    except Exception as exc:  # 配置类错误
        cell.error = str(exc)
        return cell

    cell.bars_available = count_candles(db, inst_id, bar, start_ms, end_ms)
    if cell.bars_available == 0:
        cell.error = f"缺少 {bar} 数据"
        return cell

    strategy = load_strategy_with_params(cfg, strategy_name, build_params(strategy_name, overrides))
    result = run_backtest(
        db=db,
        strategy=strategy,
        inst_id=inst_id,
        bar=bar,
        bar_ms=interval,
        start_ts=start_ms,
        end_ts=end_ms,
        initial_cash=cash,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        sample_bars=cfg.render.equity_sample_bars,
        params=strategy.params,
        leverage=leverage,
    )
    cell.run_id = result.run_id
    cell.metrics = result.metrics
    cell.fee_ratio = result.metrics.total_fees / cash if cash > 0 else 0.0
    return cell


def render_table(matrix: Matrix) -> None:
    table = Table(box=None, pad_edge=False, title="策略 × 周期 回测对比", title_style="bold")
    table.add_column("策略")
    table.add_column("周期", style="cyan")
    table.add_column("交易", justify="right")
    table.add_column("胜率", justify="right")
    table.add_column("盈亏比", justify="right")
    table.add_column("期望", justify="right")
    table.add_column("收益", justify="right")
    table.add_column("超额", justify="right")
    table.add_column("回撤", justify="right")
    table.add_column("费用", justify="right", style="dim")
    table.add_column("备注", style="dim")

    for cell in matrix.cells:
        if cell.metrics is None:
            table.add_row(
                cell.strategy, cell.bar, "-", "-", "-", "-", "-", "-", "-", "-",
                f"[red]{cell.error}[/]",
            )
            continue

        m = cell.metrics
        excess = m.total_return - m.buy_hold_return
        table.add_row(
            cell.strategy,
            cell.bar,
            str(m.trade_count),
            f"{m.win_rate:.1%}",
            f"{m.profit_factor:.2f}",
            f"{cell.expectancy:+.3%}",
            f"[{pnl_style(m.total_return)}]{fmt_pct(m.total_return)}[/]",
            f"[{pnl_style(excess)}]{fmt_pct(excess)}[/]",
            f"[red]{fmt_pct(-m.max_drawdown)}[/]" if m.max_drawdown else "-",
            f"{cell.fee_ratio:.2%}",
            cell.sample_note,
        )
    console.print(table)


def render_conclusion(matrix: Matrix, *, fee_rate: float) -> None:
    console.print("\n[bold]结论[/]")
    best = matrix.best_by_bar()
    if not best:
        console.print("  [yellow]没有任何组合产生足够样本，无法给出结论[/]")
        return

    for bar, cell in best.items():
        m = cell.metrics
        assert m is not None
        console.print(
            f"  {bar:>3} 周期：{cell.strategy} 最优，"
            f"收益 {fmt_pct(m.total_return)}，胜率 {m.win_rate:.1%}，"
            f"盈亏比 {m.profit_factor:.2f}，{m.trade_count} 笔"
        )

    insample = [c for c in matrix.cells if c.metrics and c.metrics.trade_count < MIN_SAMPLE]
    if insample:
        pairs = "、".join(f"{c.strategy}/{c.bar}" for c in insample)
        console.print(f"  [yellow]样本不足（<{MIN_SAMPLE} 笔）：{pairs}[/]")

    high_fee = [c for c in matrix.cells if c.fee_ratio > 0.02]
    if high_fee:
        pairs = "、".join(f"{c.strategy}/{c.bar}({c.fee_ratio:.1%})" for c in high_fee)
        console.print(f"  [yellow]手续费超过初始资金 2%：{pairs}[/]")
        console.print(
            f"  [dim]当前手续费 {fee_rate:.4%}；周期越短交易越频繁，"
            f"费用对结果的侵蚀越明显[/]"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="策略 × 周期 批量回测")
    parser.add_argument("--days", type=int, default=60, help="回测天数")
    parser.add_argument("--bars", default=",".join(DEFAULT_BARS), help="周期，逗号分隔")
    parser.add_argument(
        "--strategies", default=",".join(DEFAULT_STRATEGIES), help="策略，逗号分隔"
    )
    parser.add_argument("--param", default="", help="额外策略参数，形如 k=v,k2=v2")
    parser.add_argument("--fee", type=float, default=None, help="手续费率")
    parser.add_argument("--slippage", type=float, default=None, help="滑点（bp）")
    parser.add_argument("--cash", type=float, default=None, help="初始资金 USDT")
    parser.add_argument("--leverage", type=float, default=1.0, help="杠杆倍数，1 表示现货")
    parser.add_argument("--inst", default=None, help="交易对，默认取配置")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    # 独立脚本不初始化项目日志，默认级别会把 DB 调试信息打到 stderr
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    cfg = load_config()
    inst = args.inst or cfg.market.inst_id
    overrides: dict[str, Any] = {}
    for pair in filter(None, (p.strip() for p in args.param.split(","))):
        key, _, value = pair.partition("=")
        overrides[key.strip()] = _auto_type(value.strip())

    fee_rate = args.fee if args.fee is not None else cfg.execution.fee_rate
    slippage_bps = (
        args.slippage if args.slippage is not None else cfg.execution.slippage_bps
    )
    cash = args.cash if args.cash is not None else cfg.execution.initial_cash
    bars = [b.strip() for b in args.bars.split(",") if b.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    for bar in bars:  # 提前校验周期，避免跑一半才报错
        bar_to_ms(bar)

    matrix = Matrix(days=args.days, inst_id=inst)
    total = len(bars) * len(strategies)

    db = Database(cfg.storage.db_path)
    try:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            console=console,
            transient=True,
            disable=args.json,
        )
        with progress:
            task = progress.add_task("回测矩阵", total=total)
            for bar in bars:
                for strategy_name in strategies:
                    progress.update(
                        task, description=f"{strategy_name} @ {bar}"
                    )
                    matrix.cells.append(
                        run_one(
                            db=db,
                            cfg=cfg,
                            strategy_name=strategy_name,
                            bar=bar,
                            days=args.days,
                            inst_id=inst,
                            overrides=overrides,
                            fee_rate=fee_rate,
                            slippage_bps=slippage_bps,
                            cash=cash,
                            leverage=args.leverage,
                        )
                    )
                    progress.advance(task)
    finally:
        db.close()

    if args.json:
        payload = {
            "inst_id": inst,
            "days": args.days,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "initial_cash": cash,
            "cells": [cell.to_row() for cell in matrix.cells],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    console.print(
        f"[dim]{cfg.market.inst_id} · 最近 {args.days} 天 · "
        f"手续费 {fee_rate:.4%} · 滑点 {slippage_bps:.1f}bp · "
        f"初始资金 {fmt_money(cash)} USDT[/]"
    )
    render_table(matrix)
    render_conclusion(matrix, fee_rate=fee_rate)
    console.print(
        "\n[dim]用 okxquant report show <运行编号> 可查看单组详情与逐笔成交[/]"
    )
    return 0


def _auto_type(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


if __name__ == "__main__":
    raise SystemExit(main())
