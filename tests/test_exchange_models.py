"""交易所数据解析与限频器测试（不依赖网络）。"""

from __future__ import annotations

import time
from decimal import Decimal

from okxquant.domain import OrderStatus, OrderType, Side
from okxquant.exchange.models import (
    as_float,
    as_int,
    parse_balance,
    parse_candles,
    parse_instrument,
    parse_order,
    parse_ticker,
)
from okxquant.exchange.rest import RateLimiter


def test_as_float_handles_empty_and_none() -> None:
    assert as_float("") == 0.0
    assert as_float(None) == 0.0
    assert as_float("1.5") == 1.5
    assert as_float(2) == 2.0
    assert as_float("abc", default=-1.0) == -1.0


def test_as_int_handles_string_float() -> None:
    assert as_int("1700000000000") == 1_700_000_000_000
    assert as_int("12.9") == 12
    assert as_int("") == 0


def test_parse_candles_sorts_ascending() -> None:
    rows = [
        ["1700000120000", "100", "101", "99", "100.5", "1.2", "1.2", "120", "1"],
        ["1700000060000", "99", "100", "98", "99.5", "1.1", "1.1", "110", "0"],
    ]
    candles = parse_candles(rows)
    assert [c.ts for c in candles] == [1_700_000_060_000, 1_700_000_120_000]
    assert candles[0].close == 99.5
    assert candles[0].confirm is False
    assert candles[1].confirm is True


def test_parse_candles_keeps_volume_calibers() -> None:
    rows = [
        [
            "1700000060000",
            "0.99",
            "1.00",
            "0.98",
            "0.995",
            "20160.82",
            "2016082",
            "1998684.01",
            "1",
        ]
    ]
    candle = parse_candles(rows)[0]
    assert candle.volume == 20160.82  # 张
    assert candle.volume_ccy == 2016082.0  # 标的币
    assert candle.volume_quote == 1998684.01  # USDT


def test_parse_candles_skips_malformed() -> None:
    rows = [["1", "2"], ["1700000060000", "99", "100", "98", "99.5", "1.1"]]
    candles = parse_candles(rows)
    assert len(candles) == 1


def test_parse_instrument_precision() -> None:
    inst = parse_instrument(
        {"instId": "BTC-USDT", "baseCcy": "BTC", "quoteCcy": "USDT",
         "tickSz": "0.1", "lotSz": "0.00000001", "minSz": "0.00001"}
    )
    assert inst.inst_id == "BTC-USDT"
    assert inst.tick_sz == Decimal("0.1")
    assert inst.min_sz == Decimal("0.00001")


def test_parse_ticker_change() -> None:
    ticker = parse_ticker({"instId": "BTC-USDT", "ts": "1", "last": "110",
                           "bidPx": "109.9", "askPx": "110.1", "open24h": "100"})
    assert ticker.last == 110.0
    assert round(ticker.change_pct_24h, 4) == 0.1


def test_parse_balance_picks_currency() -> None:
    data = [
        {
            "details": [
                {"ccy": "BTC", "availBal": "0.5", "cashBal": "0.6"},
                {"ccy": "USDT", "availBal": "1000.5", "cashBal": "1000.5"},
            ]
        }
    ]
    avail, cash = parse_balance(data, "USDT")
    assert avail == 1000.5
    assert cash == 1000.5


def test_parse_balance_missing_currency() -> None:
    assert parse_balance([], "USDT") == (0.0, 0.0)


def test_parse_order_maps_state_and_side() -> None:
    order = parse_order(
        {
            "clOrdId": "abc",
            "instId": "BTC-USDT",
            "side": "buy",
            "ordType": "limit",
            "sz": "0.01",
            "px": "60000",
            "state": "partially_filled",
            "accFillSz": "0.004",
            "avgPx": "59999",
            "fee": "-0.02",
            "ordId": "123",
            "cTime": "1700000000000",
            "uTime": "1700000001000",
        }
    )
    assert order.side is Side.BUY
    assert order.ord_type is OrderType.LIMIT
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.remaining == 0.006
    assert order.fee == 0.02
    assert order.status.is_open


def test_parse_order_unknown_state_is_pending() -> None:
    order = parse_order({"side": "sell", "ordType": "market", "sz": "1", "state": "weird"})
    assert order.status is OrderStatus.PENDING
    assert order.side is Side.SELL


def test_rate_limiter_enforces_spacing() -> None:
    limiter = RateLimiter(rate=1000, period=1.0)  # 1ms 一次
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.003
    assert elapsed < 0.5
