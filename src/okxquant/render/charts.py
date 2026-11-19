"""纯字符图形：sparkline 与折线图。

不依赖任何绘图库，输出是普通字符串，便于在测试里断言形状。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from okxquant.render import SPARK_CHARS, downsample


def sparkline(values: Sequence[float], *, width: int | None = None) -> str:
    """用 ``▁▂▃▄▅▆▇█`` 表示走势，宽度不够时先等距压缩。"""
    data = list(values)
    if not data:
        return ""
    if width is not None and width > 0:
        data = downsample(data, width)

    lo = min(data)
    hi = max(data)
    if hi - lo <= 0:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(data)

    span = hi - lo
    last = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[int((v - lo) / span * last)] for v in data)


def line_chart(
    values: Sequence[float],
    *,
    width: int = 60,
    height: int = 12,
    fmt: Callable[[float], str] = lambda v: f"{v:,.2f}",
    label_width: int = 12,
    x_labels: tuple[str, str] | None = None,
    grid_char: str = "·",
    line_char: str = "█",
) -> list[str]:
    """绘制字符折线图，返回可直接打印的多行字符串。

    - 纵向按 ``height`` 行分档，横向压缩到 ``width`` 列。
    - 相邻列之间的落差用 ``grid_char`` 填出垂直线，视觉上连续。
    """
    data = list(values)
    if not data:
        return ["(无数据)"]

    height = max(3, height)
    width = max(10, width)
    data = downsample(data, width)

    lo = min(data)
    hi = max(data)
    if hi - lo <= 0:
        lo, hi = lo - 1.0, hi + 1.0
    span = hi - lo

    def y_of(value: float) -> int:
        return int((value - lo) / span * (height - 1))

    grid = [[" "] * len(data) for _ in range(height)]
    prev: int | None = None
    for x, value in enumerate(data):
        y = y_of(value)
        if prev is not None and y != prev:
            step = 1 if y > prev else -1
            for yy in range(prev, y, step):
                row = height - 1 - yy
                if 0 <= row < height:
                    grid[row][x] = grid_char
        grid[height - 1 - y][x] = line_char
        prev = y

    lines: list[str] = []
    label_every = max(1, (height - 1) // 4)
    for row_index, row in enumerate(grid):
        value = lo + (1 - row_index / (height - 1)) * span
        if row_index % label_every == 0 or row_index == height - 1:
            label = fmt(value).rjust(label_width)
        else:
            label = " " * label_width
        lines.append(f"[dim]{label}[/] │{''.join(row)}")

    axis = " " * label_width + " └" + "─" * len(data)
    lines.append(f"[dim]{axis}[/]")

    if x_labels is not None:
        left, right = x_labels
        total = len(data)
        gap = max(1, total - len(left) - len(right))
        lines.append(" " * (label_width + 2) + left + " " * gap + right)

    return lines


def drawdown_series(equity: Sequence[float]) -> list[float]:
    """把权益序列转成回撤序列（0 表示新高，负值表示回撤）。"""
    peak = 0.0
    out: list[float] = []
    for value in equity:
        peak = max(peak, value)
        out.append((value - peak) / peak if peak > 0 else 0.0)
    return out
