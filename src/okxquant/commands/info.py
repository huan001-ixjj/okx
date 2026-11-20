"""`info` / `version` 子命令：查看运行环境与配置摘要。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from okxquant import __version__
from okxquant.context import AppContext, get_app_ctx

console = Console()


def register(app: typer.Typer) -> None:
    app.command("info")(info_command)
    app.command("version")(version_command)


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _environment_label(app_ctx: AppContext) -> str:
    if app_ctx.credentials is None:
        return "[yellow]OFFLINE[/] 无凭据，仅可下载数据与回测"
    cred = app_ctx.credentials
    if cred.is_demo:
        return "[green]SIMULATED TRADING[/] 模拟盘（flag=1，不涉及真实资金）"
    return "[bold red]LIVE TRADING[/] 实盘（flag=0）"


def _collect(app_ctx: AppContext) -> dict[str, Any]:
    cfg = app_ctx.config
    db_path = str(cfg.storage.db_path)
    # db_path 可能是 SQLite 文件路径，也可能是 postgres:// URL；后者没有本地文件
    local = Path(db_path) if not db_path.startswith(("postgres://", "postgresql://")) else None
    return {
        "version": __version__,
        "environment": (
            "offline"
            if app_ctx.credentials is None
            else ("demo" if app_ctx.credentials.is_demo else "live")
        ),
        "inst_id": cfg.market.inst_id,
        "bar": cfg.market.bar,
        "config_path": str(app_ctx.config_path or "config/default.toml"),
        "db_path": db_path,
        "db_exists": local.exists() if local is not None else True,
        "db_size_bytes": (
            local.stat().st_size if local is not None and local.exists() else 0
        ),
        "credentials": app_ctx.credentials.masked() if app_ctx.credentials else None,
        "risk": cfg.risk.model_dump(),
        "execution": cfg.execution.model_dump(),
        "strategy": cfg.strategy.name,
    }


def info_command(ctx: typer.Context) -> None:
    """显示版本、运行环境与关键配置。"""
    app_ctx = get_app_ctx(ctx)
    data = _collect(app_ctx)

    if app_ctx.json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return

    cfg = app_ctx.config
    db_path = cfg.storage.db_path
    db_state = (
        f"{db_path} [dim]({_human_size(data['db_size_bytes'])})[/]"
        if data["db_exists"]
        else f"{db_path} [dim](尚未创建)[/]"
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(justify="right", style="dim")
    summary.add_column()
    summary.add_row("版本", f"okxquant [bold]{__version__}[/]")
    summary.add_row("环境", _environment_label(app_ctx))
    summary.add_row("交易对", f"[bold]{cfg.market.inst_id}[/]  周期 [bold]{cfg.market.bar}[/]")
    summary.add_row("配置文件", str(app_ctx.config_path or "config/default.toml"))
    summary.add_row("数据库", db_state)
    summary.add_row(
        "API Key",
        app_ctx.credentials.masked()["api_key"] if app_ctx.credentials else "[dim]未配置[/]",
    )
    console.print(Panel(summary, title="okxquant", border_style="cyan", expand=False))

    risk = Table(title="风控阈值", title_style="bold", box=None, pad_edge=False)
    risk.add_column("项目", style="dim")
    risk.add_column("值", justify="right", style="bold")
    risk.add_row("单笔最大金额", f"{cfg.risk.max_order_notional:,.2f} USDT")
    risk.add_row("最大持仓占比", f"{cfg.risk.max_position_ratio:.0%}")
    risk.add_row("日内最大亏损", f"{cfg.risk.max_daily_loss:,.2f} USDT")
    risk.add_row("最小下单金额", f"{cfg.risk.min_order_notional:,.2f} USDT")
    risk.add_row("下单冷却", f"{cfg.risk.order_cooldown_sec:.0f} 秒")
    risk.add_row("单日下单上限", f"{cfg.risk.max_orders_per_day} 次")
    risk.add_row("允许实盘", "[red]是[/]" if cfg.risk.allow_live else "否")
    console.print(risk)

    console.print(
        "[dim]回测成本假设：手续费 "
        f"{cfg.execution.fee_rate:.4%}，滑点 {cfg.execution.slippage_bps:.1f}bp，"
        f"初始资金 {cfg.execution.initial_cash:,.2f} USDT[/]"
    )


def version_command() -> None:
    """打印版本号。"""
    typer.echo(__version__)
