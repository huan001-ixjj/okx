"""`strategy` 子命令组：列出与校验策略。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from okxquant.context import get_app_ctx
from okxquant.errors import StrategyError
from okxquant.strategy import build, builtin_names
from okxquant.strategy.registry import resolve_class

console = Console()
strategy_app = typer.Typer(help="策略列表与校验", no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(strategy_app, name="strategy")


@strategy_app.command("list")
def list_command(ctx: typer.Context) -> None:
    """列出内置策略与 strategies/ 目录下的自定义策略。"""
    app_ctx = get_app_ctx(ctx)

    rows: list[dict[str, str]] = []
    for name in builtin_names():
        cls = resolve_class(name)
        rows.append(
            {"name": name, "type": "builtin", "description": cls.description,
             "params": ", ".join(cls.default_params)}
        )

    custom_dir = Path("strategies")
    if custom_dir.is_dir():
        for path in sorted(custom_dir.glob("*.py")):
            try:
                instance = build(str(path))
            except StrategyError as exc:
                rows.append(
                    {"name": str(path), "type": "script", "description": f"[red]{exc}[/]",
                     "params": "-"}
                )
                continue
            rows.append(
                {
                    "name": str(path),
                    "type": "script",
                    "description": instance.description or instance.name,
                    "params": ", ".join(instance.default_params),
                }
            )

    if app_ctx.json_output:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    table = Table(box=None, pad_edge=False, title="可用策略", title_style="bold")
    table.add_column("名称", style="cyan")
    table.add_column("类型", style="dim")
    table.add_column("说明")
    table.add_column("参数", style="dim")
    for row in rows:
        table.add_row(row["name"], row["type"], row["description"], row["params"])
    console.print(table)


@strategy_app.command("check")
def check_command(
    ctx: typer.Context,
    spec: str = typer.Argument(..., help="策略名或 .py 文件路径"),
) -> None:
    """校验策略文件能否加载、是否实现了必需接口。"""
    app_ctx = get_app_ctx(ctx)

    try:
        instance = build(spec)
    except StrategyError as exc:
        if app_ctx.json_output:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            console.print(f"[bold red]校验失败[/] {exc}")
        raise typer.Exit(code=1) from exc

    missing = getattr(instance, "on_bar", None)
    if missing is None:
        raise StrategyError(f"{spec} 没有实现 on_bar")

    payload = {
        "ok": True,
        "name": instance.name,
        "description": instance.description,
        "params": instance.default_params,
        "source": "builtin" if not spec.endswith(".py") else "script",
    }
    if app_ctx.json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    console.print(f"[green]校验通过[/] {instance.name}（{payload['source']}）")
    if instance.description:
        console.print(f"[dim]{instance.description}[/]")
    if instance.default_params:
        preview = "  ".join(f"{k}={v}" for k, v in instance.default_params.items())
        console.print(f"[dim]默认参数：{preview}[/]")
