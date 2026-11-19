"""`download` 子命令：历史 K 线增量下载与缺口补齐。"""

from __future__ import annotations

import random
import time
from datetime import datetime

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
from rich.table import Table

from okxquant.config import bar_to_ms
from okxquant.context import AppContext, get_app_ctx
from okxquant.domain import Candle
from okxquant.errors import ConfigError
from okxquant.exchange import OkxRest
from okxquant.store import Database
from okxquant.store.candles import (
    align_ts,
    count_candles,
    find_gaps,
    range_stats,
    upsert_candles,
)

console = Console()

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")


def register(app: typer.Typer) -> None:
    app.command("download")(download_command)


def _parse_time(text: str, label: str) -> int:
    for fmt in _TIME_FORMATS:
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise ConfigError(
        f"无法解析{label}时间: {text!r}",
        hint="支持格式：2026-11-01、2026-11-01 12:30、2026-11-01 12:30:00（本地时区）",
    )


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")


def _synthetic(start_ms: int, end_ms: int, interval: int, *, seed: int = 42) -> list[Candle]:
    """离线演示用的随机游走 K 线，不反映真实行情。"""
    rng = random.Random(seed)
    price = 60_000.0
    ts = start_ms
    out: list[Candle] = []
    while ts <= end_ms:
        change = rng.gauss(0.0, 0.0009)
        open_px = price
        close_px = max(1.0, price * (1 + change))
        high = max(open_px, close_px) * (1 + abs(rng.gauss(0, 0.0004)))
        low = min(open_px, close_px) * (1 - abs(rng.gauss(0, 0.0004)))
        out.append(
            Candle(
                ts=ts,
                open=round(open_px, 1),
                high=round(high, 1),
                low=round(low, 1),
                close=round(close_px, 1),
                volume=round(rng.uniform(1.0, 20.0), 4),
                confirm=True,
            )
        )
        price = close_px
        ts += interval
    return out


def download_command(
    ctx: typer.Context,
    days: int = typer.Option(7, "--days", "-d", help="下载最近 N 天（未指定 --start 时生效）"),
    start: str | None = typer.Option(None, "--start", help="起始时间，如 2026-11-01"),
    end: str | None = typer.Option(None, "--end", help="结束时间，默认当前时间"),
    inst_id: str | None = typer.Option(None, "--inst", help="覆盖配置里的交易对"),
    bar: str | None = typer.Option(None, "--bar", help="覆盖配置里的 K 线周期"),
    check: bool = typer.Option(False, "--check", help="只检测缺口，不下载"),
    synthetic: bool = typer.Option(
        False, "--synthetic", help="生成合成数据（离线演示用，不代表真实行情）"
    ),
) -> None:
    """下载 BTC-USDT 历史 K 线并落库，重复执行只补缺口。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config
    symbol = inst_id or cfg.market.inst_id
    period = bar or cfg.market.bar
    interval = bar_to_ms(period)

    if days <= 0:
        raise ConfigError("--days 必须大于 0")

    end_raw = _parse_time(end, "结束") if end else int(time.time() * 1000)
    start_raw = _parse_time(start, "起始") if start else end_raw - days * 86_400_000
    end_ms = align_ts(end_raw, interval)
    start_ms = align_ts(start_raw, interval)

    if start_ms >= end_ms:
        raise ConfigError("起始时间必须早于结束时间")

    db = Database(cfg.storage.db_path)
    try:
        if synthetic:
            _download_synthetic(db, symbol, period, start_ms, end_ms, interval, check=check)
            return

        gaps = find_gaps(db, symbol, period, start_ms, end_ms, interval)
        if check:
            _print_gaps(db, symbol, period, start_ms, end_ms, gaps)
            return

        if not gaps:
            stats = range_stats(db, symbol, period)
            console.print(
                f"[green]已是最新[/] {symbol} {period} 已覆盖 "
                f"{_fmt_ts(start_ms)} ~ {_fmt_ts(end_ms)}，本地共 {stats['count']} 根"
            )
            return

        _print_gaps(db, symbol, period, start_ms, end_ms, gaps)
        _download_gaps(db, app_ctx, symbol, period, gaps, interval)
    finally:
        db.close()


def _print_gaps(
    db: Database,
    symbol: str,
    period: str,
    start_ms: int,
    end_ms: int,
    gaps: list[tuple[int, int]],
) -> None:
    stats = range_stats(db, symbol, period)
    have = count_candles(db, symbol, period, start_ms, end_ms)
    console.print(
        f"目标区间 [bold]{_fmt_ts(start_ms)}[/] ~ [bold]{_fmt_ts(end_ms)}[/]"
        f"  本地 {have} 根 / 库内合计 {stats['count']} 根"
    )
    if not gaps:
        console.print("[green]该区间无缺口[/]")
        return

    table = Table(box=None, pad_edge=False, title="待补缺口", title_style="bold")
    table.add_column("#", style="dim", justify="right")
    table.add_column("起", style="cyan")
    table.add_column("止", style="cyan")
    for idx, (gap_start, gap_end) in enumerate(gaps, start=1):
        if idx <= 20:
            table.add_row(str(idx), _fmt_ts(gap_start), _fmt_ts(gap_end))
    console.print(table)
    if len(gaps) > 20:
        console.print(f"[dim]… 另有 {len(gaps) - 20} 段缺口未显示[/]")


def _download_gaps(
    db: Database,
    app_ctx: AppContext,
    symbol: str,
    period: str,
    gaps: list[tuple[int, int]],
    interval: int,
) -> None:
    proxy = app_ctx.ensure_proxy()
    rest = OkxRest(app_ctx.credentials, proxy=proxy)
    total_expected = sum((end - start) // interval + 1 for start, end in gaps)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[inserted]} 新增"),
        TimeElapsedColumn(),
        console=console,
    )

    written = 0
    inserted_total = 0
    try:
        with progress:
            task = progress.add_task(
                f"下载 {symbol} {period}", total=max(1, total_expected), inserted=0
            )
            for gap_start, gap_end in gaps:
                for page in rest.iter_candles(
                    inst_id=symbol,
                    bar=period,
                    start_ms=gap_start,
                    end_ms=gap_end,
                ):
                    written_page, inserted_page = upsert_candles(db, symbol, period, page)
                    written += written_page
                    inserted_total += inserted_page
                    progress.update(
                        task,
                        advance=len(page),
                        inserted=f"{inserted_total}",
                    )
    finally:
        rest.close()

    stats = range_stats(db, symbol, period)
    first = _fmt_ts(stats["first_ts"]) if stats["first_ts"] else "-"
    last = _fmt_ts(stats["last_ts"]) if stats["last_ts"] else "-"
    console.print(
        f"[green]完成[/] 本次写入 {written} 根，新增 {inserted_total} 根；"
        f"本地现有 {stats['count']} 根（{first} ~ {last}）"
    )


def _download_synthetic(
    db: Database,
    symbol: str,
    period: str,
    start_ms: int,
    end_ms: int,
    interval: int,
    *,
    check: bool,
) -> None:
    existing = count_candles(db, symbol, period, start_ms, end_ms)
    expected = (end_ms - start_ms) // interval + 1
    if check:
        console.print(
            f"[yellow]合成模式[/] 区间内已有 {existing} / 预计 {expected} 根"
        )
        return

    if existing >= expected:
        console.print(f"[green]已是最新[/] 合成数据已覆盖，共 {existing} 根")
        return

    candles = _synthetic(start_ms, end_ms, interval)
    written, inserted = upsert_candles(db, symbol, period, candles)
    console.print(
        f"[yellow]已生成合成数据[/] {written} 根（新增 {inserted}），"
        f"{_fmt_ts(start_ms)} ~ {_fmt_ts(end_ms)}"
    )
    console.print("[dim]合成数据仅用于离线演示回测，不代表真实行情[/]")
