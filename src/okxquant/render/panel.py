"""live 运行面板。

TTY 下由 rich 的 ``Live`` 原地刷新，非 TTY（重定向/CI）降级成周期性快照，
保证日志不会把输出刷爆。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from rich.console import Group
from rich.table import Table

from okxquant.domain import AccountSnapshot, Order
from okxquant.render import (
    fmt_money,
    fmt_pct,
    fmt_price,
    fmt_qty,
    fmt_ts,
    pnl_style,
)

MAX_EVENTS = 8


@dataclass(slots=True)
class LiveState:
    inst_id: str = "BTC-USDT"
    bar: str = "1m"
    environment: str = "SIMULATED TRADING"
    dry_run: bool = True
    strategy_name: str = "-"
    strategy_status: str = "running"
    last_price: float = 0.0
    change_pct: float = 0.0
    snapshot: AccountSnapshot | None = None
    day_pnl: float = 0.0
    day_pnl_pct: float = 0.0
    connected_public: bool = False
    connected_private: bool = False
    orders: list[Order] = field(default_factory=list)
    events: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    started_at: float = field(default_factory=time.time)
    halted: bool = False
    halt_reason: str = ""
    bars_processed: int = 0
    reloads: int = 0

    def push_event(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.events.append(f"[dim]{stamp}[/] {text}")

    @property
    def uptime(self) -> str:
        seconds = int(time.time() - self.started_at)
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m{secs:02d}s"


def _status_bar(state: LiveState) -> Table:
    mode = "[yellow]DRY-RUN[/]" if state.dry_run else f"[green]{state.environment}[/]"
    if state.halted:
        mode = "[bold red]HALTED[/]"

    change_style = pnl_style(state.change_pct)
    pub = "[green]●[/]" if state.connected_public else "[red]○[/]"
    priv = "[green]●[/]" if state.connected_private else "[dim]○[/]"

    bar = Table.grid(padding=(0, 2))
    bar.add_column()
    bar.add_column()
    bar.add_column()
    bar.add_column()
    bar.add_row(
        mode,
        f"[bold]{state.inst_id}[/] [dim]{state.bar}[/]",
        f"[bold]{fmt_price(state.last_price)}[/] "
        f"[{change_style}]{fmt_pct(state.change_pct)}[/]",
        f"行情{pub} 账户{priv}",
        f"[dim]{state.strategy_name}[/] · {state.uptime}",
    )
    return bar


def _account_table(state: LiveState) -> Table:
    snap = state.snapshot
    table = Table.grid(padding=(0, 3))
    table.add_column(justify="right", style="dim")
    table.add_column(justify="left")
    table.add_column(justify="right", style="dim")
    table.add_column(justify="left")

    if snap is None:
        return table

    table.add_row(
        "总权益",
        f"[bold]{fmt_money(snap.equity)}[/] USDT",
        "今日盈亏",
        f"[{pnl_style(state.day_pnl)}]{fmt_money(state.day_pnl, signed=True)}[/] "
        f"([{pnl_style(state.day_pnl_pct)}]{fmt_pct(state.day_pnl_pct)}[/])",
    )
    table.add_row(
        "可用现金",
        f"{fmt_money(snap.cash)} USDT",
        "持仓",
        f"{fmt_qty(snap.position_qty)} BTC",
    )
    table.add_row(
        "持仓均价",
        fmt_price(snap.avg_cost) if snap.position_qty > 0 else "-",
        "持仓市值",
        f"{fmt_money(snap.position_value)} USDT",
    )
    table.add_row(
        "未实现",
        f"[{pnl_style(snap.unrealized)}]{fmt_money(snap.unrealized, signed=True)}[/]",
        "回撤",
        f"[red]{fmt_pct(-snap.drawdown)}[/]" if snap.drawdown > 0 else "-",
    )
    return table


def _orders_table(state: LiveState) -> Table | None:
    open_orders = [o for o in state.orders if o.status.is_open]
    if not open_orders:
        return None

    table = Table(box=None, pad_edge=False, title="挂单", title_style="bold dim")
    table.add_column("时间", style="dim")
    table.add_column("方向", justify="center")
    table.add_column("类型", style="dim")
    table.add_column("价格", justify="right")
    table.add_column("数量", justify="right")
    table.add_column("已成交", justify="right")
    table.add_column("状态", style="dim")

    for order in open_orders[:6]:
        style = "green" if order.side.sign > 0 else "red"
        label = "买入" if order.side.sign > 0 else "卖出"
        table.add_row(
            fmt_ts(order.created_ts, pattern="%H:%M:%S"),
            f"[{style}]{label}[/]",
            order.ord_type.value,
            fmt_price(order.px) if order.px else "market",
            fmt_qty(order.qty),
            fmt_qty(order.filled_qty),
            order.status.value,
        )
    return table


def _events_block(state: LiveState) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column()
    if not state.events:
        table.add_row("[dim]等待事件…[/]")
    for item in state.events:
        table.add_row(item)
    return table


def render_state(state: LiveState, *, compact: bool = False) -> Group:
    """组装完整面板。``compact`` 用于非 TTY 的周期性快照。"""
    blocks: list[object] = [_status_bar(state), _account_table(state)]

    if state.halted:
        blocks.append(f"[bold red]⚠ 已熔断：{state.halt_reason}[/]")

    orders = _orders_table(state)
    if orders is not None:
        blocks.append(orders)

    if not compact:
        blocks.append(_events_block(state))

    return Group(*blocks)
