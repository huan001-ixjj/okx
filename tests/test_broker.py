"""Broker 与对账逻辑测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from okxquant.domain import (
    AccountSnapshot,
    Instrument,
    Intent,
    Order,
    OrderStatus,
    OrderType,
    Side,
)
from okxquant.engine.broker import (
    DemoBroker,
    SimulatedBroker,
    reconcile_orders,
    simulated_fill_price,
)
from okxquant.engine.portfolio import Portfolio

NOW = 1_700_000_000_000


def make_instrument(*, min_sz: str = "0.00001") -> Instrument:
    return Instrument(
        inst_id="BTC-USDT",
        base_ccy="BTC",
        quote_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.00000001"),
        min_sz=Decimal(min_sz),
    )


def make_snapshot(portfolio: Portfolio, price: float) -> AccountSnapshot:
    return portfolio.mark(price, NOW)


class FakeRest:
    """最小可用的 REST 替身，只实现 DemoBroker 用到的方法。"""

    def __init__(self, *, demo: bool = True) -> None:
        self.is_demo = demo
        self.has_credentials = True
        self.placed: list[dict] = []
        self.pending: list[Order] = []

    def place_order(self, **kwargs) -> Order:  # type: ignore[no-untyped-def]
        self.placed.append(kwargs)
        return Order(
            cl_ord_id=kwargs.get("cl_ord_id", ""),
            inst_id=kwargs["inst_id"],
            side=kwargs["side"],
            ord_type=kwargs["ord_type"],
            qty=float(kwargs["size"]),
            px=float(kwargs["px"]) if kwargs.get("px") else None,
            status=OrderStatus.LIVE,
            exchange_ord_id="ex-1",
        )

    def pending_orders(self, inst_id: str) -> list[Order]:
        return list(self.pending)

    def cancel_order(self, **kwargs) -> str:  # type: ignore[no-untyped-def]
        return "ex-1"


class TestSimulatedBroker:
    def test_market_buy_fills(self) -> None:
        portfolio = Portfolio("BTC-USDT", 10_000.0)
        broker = SimulatedBroker(portfolio, fee_rate=0.001, slippage_bps=0, dry_run=True)

        result = broker.submit(
            inst_id="BTC-USDT",
            intent=Intent(Side.BUY, quote_qty=600.0),
            price=60_000.0,
            snapshot=make_snapshot(portfolio, 60_000.0),
            now_ms=NOW,
        )
        assert not result.rejected
        assert result.fill is not None
        assert result.order is not None
        assert result.order.status is OrderStatus.FILLED

        # Broker 本身不记账，由引擎统一 apply_fill
        portfolio.apply_fill(result.fill)
        assert portfolio.position.qty == pytest.approx(0.01)

    def test_limit_not_touched_is_rejected(self) -> None:
        portfolio = Portfolio("BTC-USDT", 10_000.0)
        broker = SimulatedBroker(portfolio, fee_rate=0.0, slippage_bps=0)
        result = broker.submit(
            inst_id="BTC-USDT",
            intent=Intent(Side.BUY, qty=0.01, px=50_000.0),
            price=60_000.0,
            snapshot=make_snapshot(portfolio, 60_000.0),
            now_ms=NOW,
        )
        assert result.rejected
        assert portfolio.position.qty == 0.0

    def test_cancel_all_is_noop(self) -> None:
        broker = SimulatedBroker(Portfolio("BTC-USDT", 100.0), fee_rate=0.0, slippage_bps=0)
        assert broker.cancel_all("BTC-USDT") == 0

    def test_name_reflects_mode(self) -> None:
        broker = SimulatedBroker(Portfolio("BTC-USDT", 100.0), fee_rate=0.0, slippage_bps=0)
        assert broker.name == "dry-run"
        assert broker.is_simulated


class TestDemoBroker:
    def test_rejects_below_min_size(self) -> None:
        rest = FakeRest()
        broker = DemoBroker(rest, make_instrument(min_sz="0.01"))  # type: ignore[arg-type]
        result = broker.submit(
            inst_id="BTC-USDT",
            intent=Intent(Side.BUY, qty=0.001),
            price=60_000.0,
            snapshot=AccountSnapshot(
                cash=10_000, position_qty=0, avg_cost=0, last_price=60_000,
                equity=10_000, unrealized=0, realized=0, drawdown=0, ts=NOW,
            ),
            now_ms=NOW,
        )
        assert result.rejected
        assert "最小下单量" in result.error
        assert rest.placed == []

    def test_rejects_below_min_notional(self) -> None:
        rest = FakeRest()
        broker = DemoBroker(rest, make_instrument(), min_notional=100.0)  # type: ignore[arg-type]
        result = broker.submit(
            inst_id="BTC-USDT",
            intent=Intent(Side.BUY, qty=0.001),
            price=60_000.0,
            snapshot=AccountSnapshot(
                cash=10_000, position_qty=0, avg_cost=0, last_price=60_000,
                equity=10_000, unrealized=0, realized=0, drawdown=0, ts=NOW,
            ),
            now_ms=NOW,
        )
        assert result.rejected
        assert "订单金额不足" in result.error

    def test_normalizes_quantity_and_price(self) -> None:
        rest = FakeRest()
        broker = DemoBroker(rest, make_instrument())  # type: ignore[arg-type]
        snapshot = AccountSnapshot(
            cash=10_000, position_qty=0, avg_cost=0, last_price=60_000,
            equity=10_000, unrealized=0, realized=0, drawdown=0, ts=NOW,
        )
        result = broker.submit(
            inst_id="BTC-USDT",
            intent=Intent(Side.BUY, qty=0.00123456789, px=60_000.456),
            price=60_000.0,
            snapshot=snapshot,
            now_ms=NOW,
        )
        assert not result.rejected
        assert rest.placed[0]["size"] == "0.00123456"
        assert rest.placed[0]["px"] == "60000.5"
        assert result.order is not None
        assert result.order.ord_type is OrderType.LIMIT

    def test_cancel_all_counts(self) -> None:
        rest = FakeRest()
        rest.pending = [
            Order(cl_ord_id="a", inst_id="BTC-USDT", side=Side.BUY,
                  ord_type=OrderType.LIMIT, qty=1, px=1, status=OrderStatus.LIVE,
                  exchange_ord_id="ex-1"),
            Order(cl_ord_id="b", inst_id="BTC-USDT", side=Side.SELL,
                  ord_type=OrderType.LIMIT, qty=1, px=1, status=OrderStatus.LIVE,
                  exchange_ord_id="ex-2"),
        ]
        broker = DemoBroker(rest, make_instrument())  # type: ignore[arg-type]
        assert broker.cancel_all("BTC-USDT") == 2


class TestReconcile:
    def _order(self, cl: str, ex: str, status: OrderStatus = OrderStatus.LIVE) -> Order:
        return Order(
            cl_ord_id=cl,
            inst_id="BTC-USDT",
            side=Side.BUY,
            ord_type=OrderType.LIMIT,
            qty=0.01,
            px=60_000.0,
            status=status,
            exchange_ord_id=ex,
        )

    def test_classifies_stale_updated_missing(self) -> None:
        local = [self._order("l1", "ex1"), self._order("l2", "ex2"), self._order("l3", "")]
        remote = [self._order("l1", "ex1"), self._order("l2", "ex2"), self._order("new", "ex9")]

        stale, updated, missing = reconcile_orders(local_open=local, remote_open=remote)
        assert [o.cl_ord_id for o in stale] == ["l3"]
        assert [o.cl_ord_id for o in updated] == ["l1", "l2"]
        assert [o.cl_ord_id for o in missing] == ["new"]

    def test_all_missing_when_nothing_local(self) -> None:
        remote = [self._order("a", "ex1")]
        stale, updated, missing = reconcile_orders(local_open=[], remote_open=remote)
        assert stale == []
        assert updated == []
        assert len(missing) == 1


def test_simulated_fill_price() -> None:
    assert simulated_fill_price(Intent(Side.BUY, qty=1.0), 100.0, 10.0) == pytest.approx(100.1)
    assert simulated_fill_price(Intent(Side.SELL, qty=1.0), 100.0, 10.0) == pytest.approx(99.9)
    assert simulated_fill_price(Intent(Side.BUY, qty=1.0, px=95.0), 100.0, 10.0) == 95.0
