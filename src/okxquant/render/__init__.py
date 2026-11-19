"""终端渲染工具：控制台、数字格式化与配色。"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from rich.console import Console

console = Console()

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def use_color() -> bool:
    """是否启用颜色：尊重 NO_COLOR 与非 TTY。"""
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def fmt_price(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def fmt_qty(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def fmt_pct(value: float, *, signed: bool = True) -> str:
    sign = "+" if signed else ""
    return f"{value * 100:{sign}.2f}%"


def fmt_money(value: float, *, signed: bool = False) -> str:
    sign = "+" if signed else ""
    return f"{value:{sign},.2f}"


def fmt_ts(ts_ms: int, *, pattern: str = "%Y-%m-%d %H:%M") -> str:
    if ts_ms <= 0:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000).strftime(pattern)


def pnl_style(value: float) -> str:
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "dim"


def colored_pct(value: float) -> str:
    style = pnl_style(value)
    return f"[{style}]{fmt_pct(value)}[/]"


def downsample(values: list[float], width: int) -> list[float]:
    """把序列等距压缩到 width 个点，保留首尾。"""
    n = len(values)
    if n <= width or width <= 1:
        return list(values)
    step = (n - 1) / (width - 1)
    return [values[round(i * step)] for i in range(width)]


__all__ = [
    "SPARK_CHARS",
    "colored_pct",
    "console",
    "downsample",
    "fmt_money",
    "fmt_pct",
    "fmt_price",
    "fmt_qty",
    "fmt_ts",
    "pnl_style",
    "use_color",
]
