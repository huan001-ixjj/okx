#!/usr/bin/env python3
"""公有 WebSocket 端点诊断探针。

回答两个具体问题：

1. 连上了却收不到行情，是端点问题还是消费逻辑问题？
2. K 线到底该订阅哪个端点（public 还是 business）、URL 要不要带 ``:8443``？

    .venv/bin/python scripts/ws_probe.py --matrix
    .venv/bin/python scripts/ws_probe.py --seconds 20
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import ssl
import time
from dataclasses import dataclass, field

import certifi
import websockets
from rich.console import Console
from rich.table import Table

from okxquant.exchange.ws import DEMO_PUBLIC_WS_URL, PUBLIC_WS_URL

console = Console()

ENDPOINTS: dict[str, str] = {
    "prod": PUBLIC_WS_URL,
    "demo": DEMO_PUBLIC_WS_URL,
}

# 候选组合：(说明, URL, 频道列表)
# 已确认：K 线在 business 端点（443）可用；ticker/tickers 在 public 与 business
# 均返回 60018，因此不再订阅。保留下面的组合作为回归验证。
MATRIX: list[tuple[str, str, list[str]]] = [
    ("prod 443 /business candle", "wss://ws.okx.com/ws/v5/business", ["candle"]),
    ("demo 443 /business candle", "wss://wspap.okx.com/ws/v5/business", ["candle"]),
]


@dataclass
class ProbeResult:
    name: str
    url: str
    channels: list[str] = field(default_factory=list)
    connected: bool = False
    error: str = ""
    counters: dict[str, int] = field(default_factory=dict)
    samples: dict[str, str] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counters.values())

    @property
    def ok(self) -> bool:
        return self.connected and self.total > 0


def build_args(channels: list[str], inst_id: str, bar: str) -> list[dict[str, str]]:
    args: list[dict[str, str]] = []
    for channel in channels:
        resolved = f"candle{bar}" if channel == "candle" else channel
        args.append({"channel": resolved, "instId": inst_id})
    return args


async def probe(
    name: str,
    url: str,
    *,
    channels: list[str],
    seconds: float,
    inst_id: str,
    bar: str,
) -> ProbeResult:
    result = ProbeResult(name=name, url=url, channels=channels)
    ssl_context = ssl.create_default_context()
    ssl_context.load_verify_locations(certifi.where())

    try:
        websocket = await websockets.connect(
            url,
            ssl=ssl_context,
            proxy=None,
            open_timeout=12,
            ping_interval=20,
            ping_timeout=20,
        )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.connected = True
    payload = {"op": "subscribe", "args": build_args(channels, inst_id, bar)}
    try:
        await websocket.send(json.dumps(payload))
    except Exception as exc:
        result.error = f"订阅发送失败: {type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            await websocket.close()
        return result

    async def consume() -> None:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = data.get("event")
            if event:
                detail = str(event)
                if data.get("code"):
                    detail += f" code={data.get('code')}"
                if data.get("msg"):
                    detail += f" msg={str(data.get('msg'))[:80]}"
                if detail not in result.events:
                    result.events.append(detail)
                continue

            channel = str((data.get("arg") or {}).get("channel", "?"))
            result.counters[channel] = result.counters.get(channel, 0) + 1
            if channel not in result.samples:
                result.samples[channel] = raw[:200]

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(consume(), timeout=seconds)

    with contextlib.suppress(Exception):
        await websocket.close()
    return result


def render(results: list[ProbeResult]) -> None:
    table = Table(box=None, pad_edge=False, title="端点探测结果", title_style="bold")
    table.add_column("组合")
    table.add_column("连接", justify="center")
    table.add_column("消息", justify="right")
    table.add_column("频道", style="dim")
    table.add_column("事件 / 错误", style="dim", overflow="fold")

    for result in results:
        connected = "[green]✓[/]" if result.connected else "[red]✗[/]"
        channels = ", ".join(f"{k}={v}" for k, v in sorted(result.counters.items())) or "-"
        detail = "; ".join(result.events)[:150] if result.events else result.error[:150]
        table.add_row(
            result.name,
            connected,
            str(result.total),
            channels if result.total else "-",
            detail or "-",
        )
    console.print(table)

    console.print("\n[bold]结论[/]")
    for result in results:
        if not result.connected:
            console.print(f"  [red]{result.name}[/]: 连接失败 — {result.error}")
        elif result.total > 0:
            console.print(f"  [green]{result.name}[/]: 收到 {result.total} 条数据")
        else:
            reason = result.events[0] if result.events else "无任何事件"
            console.print(f"  [yellow]{result.name}[/]: 0 条 — {reason}")


async def run_matrix(*, seconds: float, inst_id: str, bar: str) -> None:
    console.print(f"[bold]矩阵探测[/] 共 {len(MATRIX)} 组，每组 {seconds:.0f}s")
    results: list[ProbeResult] = []
    for name, url, channels in MATRIX:
        console.print(f"[dim]→ {name}[/]")
        results.append(
            await probe(
                name,
                url,
                channels=channels,
                seconds=seconds,
                inst_id=inst_id,
                bar=bar,
            )
        )
    render(results)


async def run_endpoints(*, seconds: float, which: str, inst_id: str, bar: str) -> None:
    names = list(ENDPOINTS) if which == "both" else [which]
    labels = {"prod": "生产端点", "demo": "模拟盘端点"}
    console.print(
        f"探测 {len(names)} 个端点，每个 {seconds:.0f}s，"
        f"订阅 candle{bar} + ticker（{inst_id}）"
    )
    results = [
        await probe(
            labels[name],
            ENDPOINTS[name],
            channels=["candle", "ticker"],
            seconds=seconds,
            inst_id=inst_id,
            bar=bar,
        )
        for name in names
    ]
    render(results)


async def main() -> None:
    parser = argparse.ArgumentParser(description="公有 WebSocket 端点诊断")
    parser.add_argument("--seconds", type=float, default=15.0, help="每组观察时长")
    parser.add_argument(
        "--endpoint", choices=["both", *ENDPOINTS], default="both", help="端点模式使用"
    )
    parser.add_argument("--matrix", action="store_true", help="跑 URL × 频道组合矩阵")
    parser.add_argument("--inst", default="BTC-USDT", help="交易对")
    parser.add_argument("--bar", default="1m", help="K 线周期")
    args = parser.parse_args()

    started = time.monotonic()
    if args.matrix:
        await run_matrix(seconds=args.seconds, inst_id=args.inst, bar=args.bar)
    else:
        await run_endpoints(
            seconds=args.seconds, which=args.endpoint, inst_id=args.inst, bar=args.bar
        )
    console.print(f"[dim]总耗时 {time.monotonic() - started:.1f}s[/]")


if __name__ == "__main__":
    asyncio.run(main())
