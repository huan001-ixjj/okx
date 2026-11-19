"""Broker 抽象与实现。

- :class:`SimulatedBroker`：本地撮合（回测与 ``--dry-run`` 都用它），不下真单。
- :class:`DemoBroker`：走 OKX 模拟盘 REST 下单，成交由私有 WS 回报。

策略与风控只产出 :class:`Intent`，具体怎么下单完全由 Broker 决定，
这是「回测与实盘同源」的关键分界。
"""

from __future__ import annotations

import itertools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from loguru import logger

from okxquant.domain import (
    AccountSnapshot,
    Candle,
    Fill,
    Instrument,
    Intent,
    Order,
    OrderStatus,
    OrderType,
    Side,
    normalize_px,
    normalize_qty,
    qty_str,
)
from okxquant.engine.backtest import _fill_from_intent
from okxquant.engine.portfolio import Portfolio
from okxquant.errors import ApiError, CredentialError
from okxquant.exchange.rest import OkxRest


@dataclass(slots=True)
class BrokerResult:
    order: Order | None = None
    fill: Fill | None = None
    error: str = ""
    rejected: bool = False


class Broker(ABC):
    """下单与撤单的统一接口。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def is_simulated(self) -> bool: ...

    @abstractmethod
    def submit(
        self,
        *,
        inst_id: str,
        intent: Intent,
        price: float,
        snapshot: AccountSnapshot,
        now_ms: int,
    ) -> BrokerResult: ...

    @abstractmethod
    def cancel_all(self, inst_id: str) -> int: ...

    def sync_open_orders(self, inst_id: str) -> list[Order]:
        return []


class SimulatedBroker(Broker):
    """本地撮合：市价按现价 ± 滑点成交，限价要求触价。"""

    def __init__(
        self,
        portfolio: Portfolio,
        *,
        fee_rate: float,
        slippage_bps: float,
        dry_run: bool = True,
    ) -> None:
        self._portfolio = portfolio
        self._fee_rate = fee_rate
        self._slippage_bps = slippage_bps
        self._dry_run = dry_run
        self._seq = itertools.count(1)

    @property
    def name(self) -> str:
        return "dry-run" if self._dry_run else "paper"

    @property
    def is_simulated(self) -> bool:
        return True

    def submit(
        self,
        *,
        inst_id: str,
        intent: Intent,
        price: float,
        snapshot: AccountSnapshot,
        now_ms: int,
    ) -> BrokerResult:
        candle = Candle(
            ts=now_ms, open=price, high=price, low=price, close=price, volume=0.0, confirm=True
        )
        fill = _fill_from_intent(
            intent,
            candle,
            self._portfolio,
            fee_rate=self._fee_rate,
            slippage_bps=self._slippage_bps,
            tag=f"sim{next(self._seq)}",
        )
        if fill is None:
            return BrokerResult(error="本地撮合失败（资金或持仓不足 / 限价未触及）", rejected=True)

        order = Order(
            cl_ord_id=fill.cl_ord_id,
            inst_id=inst_id,
            side=fill.side,
            ord_type=OrderType.MARKET if intent.is_market else OrderType.LIMIT,
            qty=fill.qty,
            px=None if intent.is_market else intent.px,
            status=OrderStatus.FILLED,
            filled_qty=fill.qty,
            avg_px=fill.px,
            fee=fill.fee,
            created_ts=now_ms,
            updated_ts=now_ms,
            reason=intent.reason,
        )
        return BrokerResult(order=order, fill=fill)

    def cancel_all(self, inst_id: str) -> int:
        return 0


class DemoBroker(Broker):
    """OKX 模拟盘下单。数量按 lotSz 归一化，价格按 tickSz 归一化。"""

    def __init__(
        self,
        rest: OkxRest,
        instrument: Instrument,
        *,
        min_notional: float = 10.0,
    ) -> None:
        if not rest.has_credentials:
            raise CredentialError("模拟盘下单需要 API 凭据")
        self._rest = rest
        self._instrument = instrument
        self._min_notional = min_notional
        self._seq = itertools.count(1)

    @property
    def name(self) -> str:
        return "okx-demo" if self._rest.is_demo else "okx-live"

    @property
    def is_simulated(self) -> bool:
        return self._rest.is_demo

    def _next_cl_ord_id(self) -> str:
        return f"oq{int(time.time())}{next(self._seq):04d}"[:32]

    def submit(
        self,
        *,
        inst_id: str,
        intent: Intent,
        price: float,
        snapshot: AccountSnapshot,
        now_ms: int,
    ) -> BrokerResult:
        if price <= 0:
            return BrokerResult(error="无效价格", rejected=True)

        if intent.quote_qty is not None:
            raw_qty = intent.quote_qty / (intent.px or price)
        else:
            raw_qty = intent.qty or 0.0

        qty = normalize_qty(raw_qty, self._instrument)
        if qty < self._instrument.min_sz:
            return BrokerResult(
                error=f"数量 {qty} 低于交易所最小下单量 {self._instrument.min_sz}",
                rejected=True,
            )

        ref_px = intent.px or price
        if qty * Decimal(str(ref_px)) < Decimal(str(self._min_notional)):
            return BrokerResult(
                error=f"订单金额不足 {self._min_notional} USDT", rejected=True
            )

        px_str: str | None = None
        if intent.px is not None:
            px_str = qty_str(normalize_px(intent.px, self._instrument))

        cl_ord_id = self._next_cl_ord_id()
        ord_type = OrderType.MARKET if intent.is_market else OrderType.LIMIT

        try:
            order = self._rest.place_order(
                inst_id=inst_id,
                side=intent.side,
                ord_type=ord_type,
                size=qty_str(qty),
                px=px_str,
                cl_ord_id=cl_ord_id,
            )
        except ApiError as exc:
            logger.error("下单被交易所拒绝：{}", exc)
            return BrokerResult(error=str(exc), rejected=True)

        order.reason = intent.reason
        order.created_ts = now_ms
        order.updated_ts = now_ms
        logger.info(
            "已提交 {} {} {} @ {}（clOrdId={}）",
            intent.side.value,
            qty_str(qty),
            inst_id,
            px_str or "market",
            cl_ord_id,
        )
        return BrokerResult(order=order)

    def cancel_all(self, inst_id: str) -> int:
        cancelled = 0
        for order in self._rest.pending_orders(inst_id):
            try:
                self._rest.cancel_order(inst_id=inst_id, ord_id=order.exchange_ord_id)
                cancelled += 1
            except ApiError as exc:
                logger.warning("撤单失败 {}: {}", order.exchange_ord_id, exc)
        return cancelled

    def sync_open_orders(self, inst_id: str) -> list[Order]:
        return self._rest.pending_orders(inst_id)


def reconcile_orders(
    *, local_open: list[Order], remote_open: list[Order]
) -> tuple[list[Order], list[Order], list[Order]]:
    """启动对账。

    返回 (需标记 stale 的本地订单, 需要更新的远程订单, 本地缺失的远程订单)。
    """
    remote_ids = {o.exchange_ord_id for o in remote_open if o.exchange_ord_id}
    local_by_exchange = {o.exchange_ord_id: o for o in local_open if o.exchange_ord_id}

    stale: list[Order] = []
    for order in local_open:
        if order.exchange_ord_id and order.exchange_ord_id not in remote_ids:
            stale.append(order)
        elif not order.exchange_ord_id:
            # 本地有记录但从未拿到交易所订单号（提交中断）
            stale.append(order)

    updated = [o for o in remote_open if o.exchange_ord_id in local_by_exchange]
    missing = [o for o in remote_open if o.exchange_ord_id not in local_by_exchange]
    return stale, updated, missing


def simulated_fill_price(intent: Intent, price: float, slippage_bps: float) -> float:
    """供面板/日志估算成交价。"""
    slip = slippage_bps / 10_000.0
    if intent.px is not None:
        return intent.px
    return price * (1 + slip) if intent.side is Side.BUY else price * (1 - slip)
