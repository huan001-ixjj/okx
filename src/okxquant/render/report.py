"""回测报告：指标面板、净值曲线、回撤与成交明细。"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from okxquant.domain import Fill, Metrics
from okxquant.render import (
    fmt_money,
    fmt_pct,
    fmt_price,
    fmt_ts,
    pnl_style,
)
from okxquant.render.charts import drawdown_series, line_chart, sparkline

MAX_TRADES_SHOWN = 20


def _metric_grid(metrics: Metrics) -> Table:
    grid = Table.grid(padding=(0, 3))
    grid.add_column(justify="right", style="dim")
    grid.add_column(justify="left")
    grid.add_column(justify="right", style="dim")
    grid.add_column(justify="left")

    ret_style = pnl_style(metrics.total_return)
    excess = metrics.total_return - metrics.buy_hold_return

    grid.add_row(
        "总收益",
        f"[{ret_style} bold]{fmt_pct(metrics.total_return)}[/]",
        "买入持有",
        f"[{pnl_style(metrics.buy_hold_return)}]{fmt_pct(metrics.buy_hold_return)}[/]",
    )
    grid.add_row(
        "期末权益",
        f"[bold]{fmt_money(metrics.final_equity)}[/] USDT",
        "超额收益",
        f"[{pnl_style(excess)}]{fmt_pct(excess)}[/]",
    )
    grid.add_row(
        "最大回撤",
        f"[red]{fmt_pct(-metrics.max_drawdown)}[/]",
        "回撤时间",
        fmt_ts(metrics.max_drawdown_ts) if metrics.max_drawdown_ts else "-",
    )
    grid.add_row(
        "夏普比率",
        f"{metrics.sharpe:.2f}",
        "持仓占比",
        f"{metrics.exposure:.1%}",
    )
    grid.add_row(
        "胜率",
        f"{metrics.win_rate:.1%}（{metrics.win_count}/{metrics.trade_count}）",
        "盈亏比",
        f"{metrics.profit_factor:.2f}",
    )
    grid.add_row(
        "手续费",
        f"{fmt_money(metrics.total_fees)} USDT",
        "K 线根数",
        f"{metrics.bars:,}",
    )
    return grid


def render_report(
    *,
    run_id: int,
    strategy_name: str,
    inst_id: str,
    bar: str,
    metrics: Metrics,
    equity: list[float],
    fills: list[Fill],
    params: dict | None = None,
    fee_rate: float,
    slippage_bps: float,
    skipped: int = 0,
    risk_events: list[str] | None = None,
    console: Console | None = None,
) -> None:
    out = console or Console()

    header = Table.grid(padding=(0, 2))
    header.add_column(style="dim", justify="right")
    header.add_column()
    header.add_row("策略", f"[bold cyan]{strategy_name}[/]")
    header.add_row("交易对", f"{inst_id}  [dim]{bar}[/]")
    header.add_row("区间", f"{fmt_ts(metrics.start_ts)} ~ {fmt_ts(metrics.end_ts)}")
    header.add_row(
        "成本",
        f"手续费 {fee_rate:.4%}  滑点 {slippage_bps:.1f}bp",
    )
    if params:
        preview = "  ".join(f"{k}={v}" for k, v in list(params.items())[:6])
        header.add_row("参数", f"[dim]{preview}[/]")

    out.print(
        Panel(header, title=f"回测报告 #{run_id}", border_style="cyan", expand=False)
    )

    out.print(_metric_grid(metrics))
    out.print()

    if equity:
        # 终端宽度有限，图表按可用宽度裁剪，避免 rich 自动折行把图表撕碎
        chart_width = max(30, min(out.width - 18, 96))
        out.print("[bold]净值曲线[/]")
        chart = line_chart(
            equity,
            width=chart_width,
            height=11,
            fmt=lambda v: fmt_price(v),
            x_labels=(
                fmt_ts(metrics.start_ts, pattern="%m-%d %H:%M"),
                fmt_ts(metrics.end_ts, pattern="%m-%d %H:%M"),
            ),
        )
        for line in chart:
            out.print(line, soft_wrap=True)

        dd = drawdown_series(equity)
        out.print(
            f"[dim]回撤走势[/] [red]{sparkline([v * 100 for v in dd], width=chart_width)}[/]"
            f"  最低 {fmt_pct(min(dd))}",
            soft_wrap=True,
        )
        out.print()

    if fills:
        shown = min(len(fills), MAX_TRADES_SHOWN)
        table = Table(
            title=f"成交明细（共 {len(fills)} 笔，显示最近 {shown} 笔）",
            title_style="bold",
            box=None,
            pad_edge=False,
        )
        table.add_column("时间", style="dim")
        table.add_column("方向", justify="center")
        table.add_column("价格", justify="right")
        table.add_column("数量", justify="right")
        table.add_column("金额", justify="right")
        table.add_column("费用", justify="right", style="dim")

        for fill in fills[-MAX_TRADES_SHOWN:]:
            style = "green" if fill.side.sign > 0 else "red"
            label = "买入" if fill.side.sign > 0 else "卖出"
            table.add_row(
                fmt_ts(fill.ts),
                f"[{style}]{label}[/]",
                fmt_price(fill.px),
                f"{fill.qty:.6f}",
                fmt_money(fill.notional),
                f"{fill.fee:.4f}",
            )
        out.print(table)
    else:
        out.print("[yellow]本次回测没有任何成交[/]（区间太短、参数未触发或资金不足）")

    if skipped:
        out.print(f"[dim]跳过 {skipped} 次下单（资金不足、限价未触及或风控拒绝）[/]")
    if risk_events:
        out.print(f"[red]风控熔断 {len(risk_events)} 次[/]：" + "；".join(risk_events[:3]))
