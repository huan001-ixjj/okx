"""`tunnel` 子命令组：SSH 代理隧道的查看与手动控制。

仅用于无法直连 OKX 的开发环境。部署到外网服务器时不需要这个命令。
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from okxquant.context import get_app_ctx
from okxquant.errors import ConfigError
from okxquant.tunnel import (
    PASSWORD_ENV,
    SshTunnel,
    TunnelSpec,
    password_from_env,
)

console = Console()
tunnel_app = typer.Typer(
    help="SSH 代理隧道管理（开发环境专用，公网服务器无需使用）",
    no_args_is_help=True,
)


def register(app: typer.Typer) -> None:
    app.add_typer(tunnel_app, name="tunnel")


def _status_payload(app_ctx: Any) -> dict[str, Any]:
    cfg = app_ctx.config.network

    if cfg.proxy.strip():
        return {"mode": "proxy", "proxy": cfg.proxy}
    if not cfg.has_tunnel:
        return {"mode": "direct", "proxy": None}

    spec = TunnelSpec.from_config(cfg, password="")
    status = SshTunnel(spec).status()
    return {
        "mode": "tunnel",
        "host": spec.host,
        "port": spec.port,
        "running": status.running,
        "owned": status.owned,
        "pid": status.pid,
        "password_configured": bool(password_from_env()),
        "detail": status.describe(),
    }


@tunnel_app.command("status")
def status_command(ctx: typer.Context) -> None:
    """查看隧道是否运行、端口与进程归属。"""
    app_ctx = get_app_ctx(ctx)
    payload = _status_payload(app_ctx)

    if app_ctx.json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    table = Table(box=None, pad_edge=False, title="代理隧道状态", title_style="bold")
    table.add_column("项目", style="dim")
    table.add_column("值")

    mode = payload["mode"]
    if mode == "direct":
        table.add_row("模式", "[green]直连[/]")
        table.add_row("说明", "未配置隧道与代理，联网命令直接连接 OKX")
    elif mode == "proxy":
        table.add_row("模式", "显式代理")
        table.add_row("地址", f"[bold]{payload['proxy']}[/]")
        table.add_row("说明", "使用 OKX_PROXY，不经过 SSH 隧道")
    else:
        table.add_row("模式", "SSH 隧道")
        table.add_row("主机", str(payload["host"]))
        table.add_row("本地端口", f"127.0.0.1:{payload['port']}")
        table.add_row(
            "状态",
            "[green]运行中[/]" if payload["running"] else "[yellow]未运行[/]",
        )
        if payload["running"]:
            table.add_row(
                "归属",
                "本工具启动" if payload["owned"] else "外部已有通道（不会清理）",
            )
            table.add_row("PID", str(payload["pid"]))
        table.add_row(
            "密码",
            "[green]已配置[/]"
            if payload["password_configured"]
            else f"[red]未配置[/] {PASSWORD_ENV}",
        )
        table.add_row("详情", str(payload["detail"]))

    console.print(table)


@tunnel_app.command("start")
def start_command(
    ctx: typer.Context,
    wait: float | None = typer.Option(None, "--wait", help="等待就绪的最长秒数"),
) -> None:
    """建立隧道（端口已就绪时直接复用）。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config.network

    if app_ctx.disable_tunnel:
        raise ConfigError("已通过 --no-tunnel 禁用隧道，无法启动")
    if not cfg.has_tunnel:
        raise ConfigError(
            "未启用 SSH 隧道",
            hint=(
                "在 .env 中设置 OKX_TUNNEL_ENABLED=true、OKX_TUNNEL_HOST=user@host、"
                f"{PASSWORD_ENV}=密码"
            ),
        )

    status = SshTunnel(TunnelSpec.from_config(cfg)).start(timeout=wait)

    if app_ctx.json_output:
        typer.echo(
            json.dumps(
                {
                    "running": status.running,
                    "port": status.port,
                    "pid": status.pid,
                    "owned": status.owned,
                },
                ensure_ascii=False,
            )
        )
        return
    console.print(f"[green]隧道就绪[/] {status.describe()}")


@tunnel_app.command("stop")
def stop_command(ctx: typer.Context) -> None:
    """停止由本工具启动的隧道（外部进程不受影响）。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config.network

    if not cfg.has_tunnel:
        raise ConfigError("未启用 SSH 隧道，没有可停止的进程")

    stopped = SshTunnel(TunnelSpec.from_config(cfg, password="")).stop()

    if app_ctx.json_output:
        typer.echo(json.dumps({"stopped": stopped}, ensure_ascii=False))
        return
    if stopped:
        console.print("[green]隧道已停止[/]")
    else:
        console.print("[dim]没有由本工具启动的隧道在运行[/]")
