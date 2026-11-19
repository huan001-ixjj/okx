"""OKX v5 返回数据到内部模型的转换。

SDK 返回的是 ``{"code": "0", "msg": "", "data": [...]}`` 结构，字段值几乎全是字符串，
且可能为空串。这里集中处理，避免把防御性代码散落到业务层。
"""

from __future__ import annotations

from typing import Any

from okxquant.domain import Candle, Instrument, Order, OrderStatus, OrderType, Side, Ticker

# OKX 订单状态 → 内部状态
_ORDER_STATUS_MAP: dict[str, OrderStatus] = {
    "live": OrderStatus.LIVE,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "mmp_canceled": OrderStatus.CANCELED,
}


def as_float(value: Any, default: float = 0.0) -> float:
    """把 OKX 的字符串数值转成 float，空串/None 返回默认值。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def parse_candles(rows: list[list[str]]) -> list[Candle]:
    """OKX K 线数组 → Candle 列表（按时间升序）。

    OKX 返回的是倒序（最新在前），数组末尾的 confirm 字段仅在 1 表示已收盘。
    """
    candles: list[Candle] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            candles.append(Candle.from_okx(row))
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c.ts)
    return candles


def parse_instrument(row: dict[str, Any]) -> Instrument:
    return Instrument.from_okx({k: str(v) for k, v in row.items()})


def parse_ticker(payload: dict[str, Any]) -> Ticker:
    return Ticker(
        inst_id=str(payload.get("instId", "")),
        ts=as_int(payload.get("ts")),
        last=as_float(payload.get("last")),
        bid=as_float(payload.get("bidPx")),
        ask=as_float(payload.get("askPx")),
        open_24h=as_float(payload.get("open24h")),
    )


def parse_order(payload: dict[str, Any], *, inst_id: str = "") -> Order:
    state = str(payload.get("state", "")).strip()
    status = _ORDER_STATUS_MAP.get(state, OrderStatus.PENDING)
    ord_type_raw = str(payload.get("ordType", "market")).strip()
    ord_type = OrderType.LIMIT if ord_type_raw == "limit" else OrderType.MARKET
    side = Side.BUY if str(payload.get("side", "")).lower() == "buy" else Side.SELL

    px_text = payload.get("px")
    px = as_float(px_text, default=0.0) or None

    return Order(
        cl_ord_id=str(payload.get("clOrdId", "")),
        inst_id=str(payload.get("instId", inst_id)),
        side=side,
        ord_type=ord_type,
        qty=as_float(payload.get("sz")),
        px=px,
        status=status,
        filled_qty=as_float(payload.get("accFillSz")),
        avg_px=as_float(payload.get("avgPx")),
        fee=abs(as_float(payload.get("fee"))),
        exchange_ord_id=str(payload.get("ordId", "")),
        created_ts=as_int(payload.get("cTime")),
        updated_ts=as_int(payload.get("uTime")),
    )


def parse_balance(data: list[dict[str, Any]], ccy: str) -> tuple[float, float]:
    """从余额明细里取出 (可用余额, 现金余额)。"""
    for account in data:
        for detail in account.get("details") or []:
            if str(detail.get("ccy", "")).upper() == ccy.upper():
                avail = as_float(detail.get("availBal")) or as_float(detail.get("cashBal"))
                cash = as_float(detail.get("cashBal"))
                return avail, cash
    return 0.0, 0.0


def order_side_from_str(value: str) -> Side:
    return Side.BUY if value.strip().lower() == "buy" else Side.SELL
