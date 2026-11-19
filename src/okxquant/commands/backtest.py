"""`backtest` 子命令：跑回测并打印终端报告。"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from okxquant.commands._common import (
    load_strategy_with_params,
    parse_overrides,
    resolve_window,
)
from okxquant.context import get_app_ctx
from okxquant.engine import RiskEngine, RiskLimits, run_backtest
from okxquant.errors import ConfigError
from okxquant.render.report import render_report
from okxquant.store import Database
from okxquant.store.candles import count_candles

console = Console()


def register(app: typer.Typer) -> None:
    app.command("backtest")(backtest_command)


def backtest_command(
    ctx: typer.Context,
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="内置策略名或 .py 策略路径"
    ),
    days: int = typer.Option(7, "--days", "-d", help="回测最近 N 天"),
    start: str | None = typer.Option(None, "--start", help="起始时间，如 2026-11-01"),
    end: str | None = typer.Option(None, "--end", help="结束时间，默认当前时间"),
    cash: float | None = typer.Option(None, "--cash", help="初始资金 USDT"),
    fee: float | None = typer.Option(None, "--fee", help="手续费率，如 0.0008"),
    slippage: float | None = typer.Option(None, "--slippage", help="滑点，单位 bp"),
    param: list[str] | None = typer.Option(
        None, "--param", "-p", help="覆盖策略参数，如 --param fast=8"
    ),
    risk: bool = typer.Option(False, "--risk", help="启用风控规则（默认关闭，只评估策略本身）"),
) -> None:
    """用本地历史 K 线跑回测，输出绩效指标与逐笔成交。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config

    spec = strategy or cfg.strategy.name
    start_ms, end_ms, interval = resolve_window(
        cfg, days=days, start=start, end=end, bar=cfg.market.bar
    )
    initial_cash = cash if cash is not None else cfg.execution.initial_cash
    fee_rate = fee if fee is not None else cfg.execution.fee_rate
    slippage_bps = slippage if slippage is not None else cfg.execution.slippage_bps

    if initial_cash <= 0:
        raise ConfigError("初始资金必须为正数")

    db = Database(cfg.storage.db_path)
    try:
        available = count_candles(
            db, cfg.market.inst_id, cfg.market.bar, start_ms, end_ms
        )
        if available == 0:
            raise ConfigError(
                f"区间内没有 K 线数据（{cfg.market.inst_id} {cfg.market.bar}）",
                hint="先运行 okxquant download --days 7，或用 --synthetic 生成离线演示数据",
            )

        strategy_impl = load_strategy_with_params(cfg, spec, parse_overrides(param))
        risk_engine = (
            RiskEngine(RiskLimits.from_config(cfg.risk)) if risk else None
        )

        expected_bars = max(1, (end_ms - start_ms) // interval + 1)
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
            # --json 时 stdout 必须只有 JSON
            disable=app_ctx.json_output,
        )

        with progress:
            task = progress.add_task(
                f"回测 {strategy_impl.name}", total=expected_bars
            )

            def on_progress(done: int) -> None:
                progress.update(task, completed=min(done, expected_bars))

            result = run_backtest(
                db=db,
                strategy=strategy_impl,
                inst_id=cfg.market.inst_id,
                bar=cfg.market.bar,
                bar_ms=interval,
                start_ts=start_ms,
                end_ts=end_ms,
                initial_cash=initial_cash,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps,
                sample_bars=cfg.render.equity_sample_bars,
                risk=risk_engine,
                on_progress=on_progress,
                params=strategy_impl.params,
            )
    finally:
        db.close()

    if app_ctx.json_output:
        payload = {
            "run_id": result.run_id,
            "strategy": strategy_impl.name,
            "inst_id": cfg.market.inst_id,
            "bar": cfg.market.bar,
            "metrics": result.metrics.to_dict(),
            "fills": len(result.fills),
            "skipped": result.skipped,
            "risk_events": result.risk_events,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    render_report(
        run_id=result.run_id,
        strategy_name=strategy_impl.name,
        inst_id=cfg.market.inst_id,
        bar=cfg.market.bar,
        metrics=result.metrics,
        equity=result.equity_series or [p.equity for p in result.equity],
        fills=result.fills,
        params=strategy_impl.params,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        skipped=result.skipped,
        risk_events=result.risk_events,
        console=console,
    )
