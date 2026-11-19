"""`report` 子命令组：查看历史 run。"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from okxquant.context import get_app_ctx
from okxquant.domain import Metrics
from okxquant.errors import ConfigError
from okxquant.render import fmt_money, fmt_pct, fmt_ts, pnl_style
from okxquant.render.report import render_report
from okxquant.store import Database, runs, trading

console = Console()
report_app = typer.Typer(help="查看历史回测与交易记录", no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(report_app, name="report")


@report_app.command("list")
def list_command(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", help="最多显示多少条"),
    kind: str | None = typer.Option(None, "--kind", help="过滤类型：backtest / live"),
) -> None:
    """列出历史运行记录。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config

    db = Database(cfg.storage.db_path)
    try:
        records = runs.list_runs(db, limit=limit, kind=kind)
    finally:
        db.close()

    if app_ctx.json_output:
        typer.echo(json.dumps(records, ensure_ascii=False, indent=2, default=str))
        return

    if not records:
        console.print("[yellow]还没有任何运行记录[/]，先跑一次 okxquant backtest")
        return

    table = Table(box=None, pad_edge=False, title="历史运行", title_style="bold")
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("类型", style="dim")
    table.add_column("策略")
    table.add_column("区间", style="dim")
    table.add_column("收益", justify="right")
    table.add_column("回撤", justify="right")
    table.add_column("成交", justify="right", style="dim")
    table.add_column("状态", style="dim")

    for record in records:
        metrics = record.get("metrics") or {}
        total_return = float(metrics.get("total_return", 0.0))
        max_dd = float(metrics.get("max_drawdown", 0.0))
        table.add_row(
            str(record["run_id"]),
            record["kind"],
            record["strategy"],
            f"{fmt_ts(record['start_ts'], pattern='%m-%d %H:%M')} ~ "
            f"{fmt_ts(record['end_ts'], pattern='%m-%d %H:%M')}",
            f"[{pnl_style(total_return)}]{fmt_pct(total_return)}[/]",
            f"[red]{fmt_pct(-max_dd)}[/]" if max_dd else "-",
            str(metrics.get("trade_count", "-")),
            record["status"],
        )
    console.print(table)
    console.print("[dim]用 okxquant report show <ID> 查看详情[/]")


@report_app.command("show")
def show_command(
    ctx: typer.Context,
    run_id: int = typer.Argument(..., help="运行 ID"),
) -> None:
    """查看单次运行的完整报告。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config

    db = Database(cfg.storage.db_path)
    try:
        record = runs.get_run(db, run_id)
        if record is None:
            raise ConfigError(f"找不到 run {run_id}", hint="用 okxquant report list 查看可用 ID")
        equity = trading.load_equity(db, run_id)
        fills = trading.load_fills(db, run_id)
        risk_events = trading.load_risk_events(db, run_id)
    finally:
        db.close()

    metrics = Metrics.from_dict(record.get("metrics") or {})
    if metrics.start_ts == 0:
        metrics.start_ts = int(record["start_ts"])
    if metrics.end_ts == 0:
        metrics.end_ts = int(record["end_ts"])

    if app_ctx.json_output:
        payload = {
            "run": record,
            "equity_points": len(equity),
            "fills": len(fills),
            "risk_events": risk_events,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    render_report(
        run_id=run_id,
        strategy_name=str(record["strategy"]),
        inst_id=str(record["inst_id"]),
        bar=str(record["bar"]),
        metrics=metrics,
        equity=[p.equity for p in equity],
        fills=fills,
        params=record.get("params"),
        fee_rate=cfg.execution.fee_rate,
        slippage_bps=cfg.execution.slippage_bps,
        risk_events=[str(e["message"]) for e in risk_events],
        console=console,
    )
    if record.get("final_equity"):
        console.print(
            f"[dim]核算：期末权益 {fmt_money(float(record['final_equity']))} USDT[/]"
        )
