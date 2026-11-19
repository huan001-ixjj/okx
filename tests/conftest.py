"""测试公共 fixture。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from okxquant.domain import Candle, Instrument

BAR_MS = 60_000


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        inst_id="BTC-USDT",
        base_ccy="BTC",
        quote_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.00000001"),
        min_sz=Decimal("0.00001"),
    )


def make_candles(
    prices: list[float],
    *,
    start_ts: int = 1_700_000_000_000,
    interval_ms: int = BAR_MS,
    spread: float = 0.001,
) -> list[Candle]:
    """按收盘价序列构造 K 线，high/low 按固定比例外扩。"""
    candles: list[Candle] = []
    for i, price in enumerate(prices):
        candles.append(
            Candle(
                ts=start_ts + i * interval_ms,
                open=price,
                high=price * (1 + spread),
                low=price * (1 - spread),
                close=price,
                volume=1.0,
                confirm=True,
            )
        )
    return candles
