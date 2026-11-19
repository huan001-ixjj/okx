"""CLI 入口：全局选项、子命令注册与统一错误处理。"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from okxquant.config import load_config, load_credentials
from okxquant.context import AppContext
from okxquant.errors import OkxQuantError
from okxquant.logging_setup import setup_logging

app = typer.Typer(
    name="okxquant",
    help="OKX BTC 量化交易命令行工具（仅模拟盘）。默认 dry-run，实盘需双重确认。",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback()
def bootstrap(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="配置文件路径", show_default="config/default.toml"
    ),
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出结果，便于管道处理"),
    log_level: str | None = typer.Option(
        None, "--log-level", help="覆盖日志级别（DEBUG/INFO/WARNING/ERROR）"
    ),
    no_tunnel: bool = typer.Option(
        False, "--no-tunnel", help="禁用 SSH 代理隧道，强制直连（不影响显式代理）"
    ),
) -> None:
    """加载配置并初始化日志，任何子命令执行前完成。"""
    try:
        app_config = load_config(config)
        setup_logging(app_config.logging, console_level=log_level)
        credentials = load_credentials(required=False)
    except OkxQuantError as exc:
        Console(stderr=True).print(f"[bold red]错误[/] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    ctx.obj = AppContext(
        config=app_config,
        credentials=credentials,
        config_path=config,
        json_output=json_output,
        disable_tunnel=no_tunnel,
    )


def _register_commands() -> None:
    from okxquant.commands import register_all

    register_all(app)


_register_commands()


def main() -> None:
    """console_scripts 入口。"""
    try:
        app()
    except OkxQuantError as exc:
        Console(stderr=True).print(f"[bold red]错误[/] {exc}")
        raise SystemExit(exc.exit_code) from exc
    except KeyboardInterrupt:
        Console(stderr=True).print("[yellow]已中断[/]")
        raise SystemExit(130) from None
