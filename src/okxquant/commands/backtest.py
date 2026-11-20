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
    fee: float | None = typer.Option(None, "--fee", help="手续费率（吃单），如 0.0005"),
    maker_fee: float | None = typer.Option(
        None, "--maker-fee", help="挂单费率（限价单按此计费），如 0.0002；缺省同 --fee"
    ),
    slippage: float | None = typer.Option(None, "--slippage", help="滑点，单位 bp"),
    param: list[str] | None = typer.Option(
        None, "--param", "-p", help="覆盖策略参数，如 --param fast=8"
    ),
    risk: bool = typer.Option(False, "--risk", help="启用风控规则（默认关闭，只评估策略本身）"),
    bar: str | None = typer.Option(
        None, "--bar", help="K 线周期，默认取配置（如 1m/5m/15m/1H）"
    ),
    leverage: float = typer.Option(
        1.0, "--leverage", "-l", help="杠杆倍数，1 表示现货（不放大）"
    ),
    inst_id: str | None = typer.Option(None, "--inst", help="覆盖配置里的交易对"),
    delay: int | None = typer.Option(
        None,
        "--delay",
        help="成交延迟根数：1=下一根开盘成交；缺省取策略参数 execution_delay",
    ),
) -> None:
    """用本地历史 K 线跑回测，输出绩效指标与逐笔成交。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config

    spec = strategy or cfg.strategy.name
    symbol = inst_id or cfg.market.inst_id
    period = bar or cfg.market.bar
    start_ms, end_ms, interval = resolve_window(
        cfg, days=days, start=start, end=end, bar=period
    )
    initial_cash = cash if cash is not None else cfg.execution.initial_cash
    fee_rate = fee if fee is not None else cfg.execution.fee_rate
    slippage_bps = slippage if slippage is not None else cfg.execution.slippage_bps

    if initial_cash <= 0:
        raise ConfigError("初始资金必须为正数")

    db = Database(cfg.storage.db_path)
    try:
        available = count_candles(db, symbol, period, start_ms, end_ms)
        if available == 0:
            raise ConfigError(
                f"区间内没有 K 线数据（{symbol} {period}）",
                hint=f"先运行 okxquant download --inst {symbol} --days 7 --bar {period}",
            )

        strategy_impl = load_strategy_with_params(cfg, spec, parse_overrides(param))
        risk_engine = (
            RiskEngine(RiskLimits.from_config(cfg.risk)) if risk else None
        )
        # 未显式指定 --delay 时沿用策略自带口径（趋势回调策略要求下一根开盘成交）
        execution_delay = delay
        if execution_delay is None:
            execution_delay = int(strategy_impl.param("execution_delay", 0) or 0)

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
                inst_id=symbol,
                bar=period,
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
                leverage=leverage,
                execution_delay=execution_delay,
                maker_fee_rate=maker_fee,
            )
    finally:
        db.close()

    if app_ctx.json_output:
        payload = {
            "run_id": result.run_id,
            "strategy": strategy_impl.name,
            "inst_id": cfg.market.inst_id,
            "bar": period,
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
        inst_id=symbol,
        bar=period,
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
