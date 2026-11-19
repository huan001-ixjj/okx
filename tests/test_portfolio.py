"""组合记账与绩效指标测试。"""

from __future__ import annotations

import pytest

from okxquant.domain import Fill, Side
from okxquant.engine.portfolio import Portfolio, periods_per_year


def make_portfolio() -> Portfolio:
    return Portfolio("BTC-USDT", 10_000.0)


def test_buy_reduces_cash_and_sets_avg_cost() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.48))
    assert p.cash == pytest.approx(9_399.52)
    assert p.position.qty == pytest.approx(0.01)
    assert p.position.avg_cost == pytest.approx(60_000.0)
    assert p.fee_paid == pytest.approx(0.48)


def test_weighted_average_cost() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.48))
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.01, px=70_000.0, fee=0.56))
    assert p.position.avg_cost == pytest.approx(65_000.0)
    assert p.position.qty == pytest.approx(0.02)


def test_sell_realizes_pnl_and_clears_position() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.48))
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.01, px=70_000.0, fee=0.56))
    p.apply_fill(Fill(ts=3, side=Side.SELL, qty=0.02, px=66_000.0, fee=1.056))

    assert p.position.qty == 0.0
    assert p.position.avg_cost == 0.0
    assert p.realized == pytest.approx(18.944)
    assert p.cash == pytest.approx(10_017.904)
    assert p.trade_pnls == [pytest.approx(18.944)]


def test_equity_includes_position_value() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.48))
    p.mark(61_000.0, ts=2)
    assert p.equity() == pytest.approx(9_399.52 + 610.0)


def test_drawdown_tracks_peak() -> None:
    p = make_portfolio()
    p.record_bar(1, 60_000.0)
    p.apply_fill(Fill(ts=2, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.0))
    p.record_bar(3, 66_000.0)
    peak = p.equity()
    p.record_bar(4, 60_000.0)
    assert p.peak_equity == pytest.approx(peak)
    assert p.max_drawdown == pytest.approx((peak - 10_000.0) / peak)


def test_snapshot_immutable_values() -> None:
    p = make_portfolio()
    p.mark(100.0, ts=1)
    snap = p.snapshot(ts=1)
    assert snap.cash == 10_000.0
    assert snap.position_qty == 0.0
    assert snap.equity == 10_000.0
    assert snap.drawdown == 0.0


def test_metrics_basic_math() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.0))
    for i in range(2, 12):
        p.record_bar(i, 61_000.0)

    metrics = p.build_metrics(
        bars=11, first_price=60_000.0, last_price=61_000.0, periods_per_year=525_600.0
    )
    assert metrics.total_return == pytest.approx((10_010.0 / 10_000.0) - 1)
    assert metrics.buy_hold_return == pytest.approx(1 / 60)
    assert metrics.final_equity == pytest.approx(10_010.0)
    assert metrics.bars == 11


def test_win_rate_counts_only_closed_trades() -> None:
    p = make_portfolio()
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.0))
    p.apply_fill(Fill(ts=2, side=Side.SELL, qty=0.01, px=61_000.0, fee=0.0))
    p.apply_fill(Fill(ts=3, side=Side.BUY, qty=0.01, px=62_000.0, fee=0.0))
    p.apply_fill(Fill(ts=4, side=Side.SELL, qty=0.01, px=61_000.0, fee=0.0))

    metrics = p.build_metrics(
        bars=4, first_price=60_000.0, last_price=61_000.0, periods_per_year=1.0
    )
    assert metrics.trade_count == 2
    assert metrics.win_count == 1
    assert metrics.loss_count == 1
    assert metrics.win_rate == pytest.approx(0.5)


def test_periods_per_year() -> None:
    assert periods_per_year(60_000) == pytest.approx(525_600.0)
    assert periods_per_year(0) == 1.0
