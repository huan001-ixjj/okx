"""双向持仓（合约语义）的账本测试。

现货只做多，空头是这次为合约新增的能力，必须单独锁住：
开空、浮动盈亏、平空、反手、加仓，任何一种算错都会让回测结果失真。
"""

from __future__ import annotations

import pytest

from okxquant.domain import Fill, Side
from okxquant.engine.portfolio import Portfolio


def make_portfolio(cash: float = 10_000.0) -> Portfolio:
    return Portfolio("BTC-USDT", cash)


def test_sell_without_position_opens_short() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))

    assert p.position.qty == pytest.approx(-0.01)
    assert p.position.avg_cost == pytest.approx(60_000.0)
    # 保证金模型：开空占用 600 保证金，现金减少但权益不变
    assert p.cash == pytest.approx(9_400.0)
    assert p.equity() == pytest.approx(10_000.0)


def test_short_unrealized_profits_when_price_falls() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))
    p.mark(57_000.0, ts=2)

    assert p.position.unrealized(57_000.0) == pytest.approx(30.0)
    assert p.equity() == pytest.approx(10_030.0)


def test_short_loses_when_price_rises() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))
    p.mark(63_000.0, ts=2)

    assert p.position.unrealized(63_000.0) == pytest.approx(-30.0)
    assert p.equity() == pytest.approx(9_970.0)


def test_buy_back_closes_short_and_realizes_pnl() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.01, px=57_000.0, fee=0.0))

    assert p.position.qty == 0.0
    assert p.position.avg_cost == 0.0
    assert p.realized == pytest.approx(30.0)
    assert p.cash == pytest.approx(10_030.0)
    assert p.trade_pnls == [pytest.approx(30.0)]


def test_reverse_from_short_to_long() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))
    # 买回 0.02：先平掉 0.01 空头，再开出 0.01 多头
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.02, px=57_000.0, fee=0.0))

    assert p.position.qty == pytest.approx(0.01)
    assert p.position.avg_cost == pytest.approx(57_000.0)
    assert p.realized == pytest.approx(30.0)
    assert p.equity() == pytest.approx(10_030.0)


def test_reverse_from_long_to_short() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.0))
    p.apply_fill(Fill(ts=2, side=Side.SELL, qty=0.02, px=63_000.0, fee=0.0))

    assert p.position.qty == pytest.approx(-0.01)
    assert p.position.avg_cost == pytest.approx(63_000.0)
    assert p.realized == pytest.approx(30.0)


def test_add_to_short_averages_cost() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))
    p.apply_fill(Fill(ts=2, side=Side.SELL, qty=0.01, px=59_000.0, fee=0.0))

    assert p.position.qty == pytest.approx(-0.02)
    assert p.position.avg_cost == pytest.approx(59_500.0)
    # 两笔空单共占用 1190 保证金
    assert p.cash == pytest.approx(8_810.0)
    # 旧仓从 60000 跌到 59000 已浮盈 10，故权益为 10010
    assert p.equity() == pytest.approx(10_010.0)


def test_partial_cover_realizes_proportion() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.02, px=60_000.0, fee=0.0))
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.01, px=55_000.0, fee=0.0))

    assert p.position.qty == pytest.approx(-0.01)
    assert p.position.avg_cost == pytest.approx(60_000.0)
    assert p.realized == pytest.approx(50.0)
    assert len(p.trade_pnls) == 1


def test_short_fee_hits_cash_not_realized() -> None:
    """与多头一致：开仓手续费只从现金扣除，已实现盈亏里只含平仓那一笔的手续费。"""
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=6.0))
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.01, px=57_000.0, fee=0.0))

    assert p.realized == pytest.approx(30.0)
    assert p.fee_paid == pytest.approx(6.0)
    # 10000 + 600 - 6 - 570 = 10024，净赚 24（毛利润 30 扣掉开仓费 6）
    assert p.cash == pytest.approx(10_024.0)
    assert p.equity() == pytest.approx(10_024.0)


def test_exposure_counts_short_position() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.SELL, qty=0.01, px=60_000.0, fee=0.0))
    p.record_bar(2, 60_000.0)
    p.record_bar(3, 60_000.0)

    metrics = p.build_metrics(
        bars=2, first_price=60_000.0, last_price=60_000.0, periods_per_year=1.0
    )
    assert metrics.exposure == pytest.approx(1.0)
