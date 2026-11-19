"""风控规则测试。"""

from __future__ import annotations

import pytest

from okxquant.domain import AccountSnapshot, Side
from okxquant.engine.risk import RiskEngine, RiskLimits

NOW = 1_700_000_000_000
DAY_MS = 86_400_000


def make_snapshot(
    *, cash: float = 10_000.0, qty: float = 0.0, price: float = 60_000.0
) -> AccountSnapshot:
    equity = cash + qty * price
    return AccountSnapshot(
        cash=cash,
        position_qty=qty,
        avg_cost=price if qty else 0.0,
        last_price=price,
        equity=equity,
        unrealized=0.0,
        realized=0.0,
        drawdown=0.0,
        ts=NOW,
    )


def make_engine(**overrides: float) -> RiskEngine:
    limits = RiskLimits(
        max_order_notional=overrides.get("max_order_notional", 500.0),
        max_position_ratio=overrides.get("max_position_ratio", 0.8),
        max_daily_loss=overrides.get("max_daily_loss", 300.0),
        min_order_notional=overrides.get("min_order_notional", 10.0),
        order_cooldown_sec=overrides.get("order_cooldown_sec", 0.0),
        max_orders_per_day=int(overrides.get("max_orders_per_day", 200.0)),
    )
    return RiskEngine(limits)


def test_allows_normal_order() -> None:
    engine = make_engine()
    snap = make_snapshot()
    decision = engine.check_order(side=Side.BUY, notional=100.0, snapshot=snap, now_ms=NOW)
    assert decision.allowed


def test_rejects_over_max_notional() -> None:
    engine = make_engine()
    decision = engine.check_order(
        side=Side.BUY, notional=1_000.0, snapshot=make_snapshot(), now_ms=NOW
    )
    assert not decision.allowed
    assert "单笔金额" in decision.reason


def test_rejects_under_min_notional() -> None:
    engine = make_engine()
    decision = engine.check_order(
        side=Side.BUY, notional=5.0, snapshot=make_snapshot(), now_ms=NOW
    )
    assert not decision.allowed
    assert "最小下单额" in decision.reason


def test_sell_not_limited_by_max_notional() -> None:
    engine = make_engine()
    snap = make_snapshot(qty=0.05)
    decision = engine.check_order(side=Side.SELL, notional=3_000.0, snapshot=snap, now_ms=NOW)
    assert decision.allowed


def test_rejects_position_ratio_breach() -> None:
    engine = make_engine(max_position_ratio=0.5)
    snap = make_snapshot(cash=1_000.0, qty=0.15)  # 持仓 9000 / 权益 10000
    decision = engine.check_order(side=Side.BUY, notional=100.0, snapshot=snap, now_ms=NOW)
    assert not decision.allowed
    assert "敞口占比" in decision.reason


def test_cooldown_blocks_rapid_orders() -> None:
    engine = make_engine(order_cooldown_sec=5.0)
    snap = make_snapshot()
    first = engine.check_order(side=Side.BUY, notional=100.0, snapshot=snap, now_ms=NOW)
    assert first.allowed
    engine.note_order(NOW, snap.equity)

    second = engine.check_order(
        side=Side.BUY, notional=100.0, snapshot=snap, now_ms=NOW + 1_000
    )
    assert not second.allowed
    assert "冷却" in second.reason

    third = engine.check_order(
        side=Side.BUY, notional=100.0, snapshot=snap, now_ms=NOW + 6_000
    )
    assert third.allowed


def test_daily_loss_halts() -> None:
    engine = make_engine(max_daily_loss=300.0)
    assert not engine.update_equity(NOW, 10_000.0)
    assert not engine.update_equity(NOW + 60_000, 9_800.0)
    triggered = engine.update_equity(NOW + 120_000, 9_600.0)
    assert triggered
    assert engine.halted
    assert "日内亏损" in engine.halt_reason

    decision = engine.check_order(
        side=Side.BUY, notional=100.0, snapshot=make_snapshot(), now_ms=NOW + 180_000
    )
    assert not decision.allowed
    assert decision.halted


def test_halt_persists_after_clear_only() -> None:
    engine = make_engine(max_daily_loss=100.0)
    engine.update_equity(NOW, 10_000.0)
    engine.update_equity(NOW + 60_000, 9_800.0)
    assert engine.halted

    engine.clear_halt()
    assert not engine.halted


def test_daily_counter_resets_next_day() -> None:
    engine = make_engine(max_orders_per_day=2)
    snap = make_snapshot()
    engine.note_order(NOW, snap.equity)
    engine.note_order(NOW + 1_000, snap.equity)
    assert engine.orders_today == 2

    blocked = engine.check_order(
        side=Side.BUY, notional=100.0, snapshot=snap, now_ms=NOW + 2_000
    )
    assert not blocked.allowed

    next_day = NOW + DAY_MS + 60_000
    allowed = engine.check_order(side=Side.BUY, notional=100.0, snapshot=snap, now_ms=next_day)
    assert allowed.allowed


def test_day_pnl() -> None:
    engine = make_engine()
    engine.update_equity(NOW, 10_000.0)
    engine.update_equity(NOW + 60_000, 10_150.0)
    assert engine.day_pnl(10_150.0) == pytest.approx(150.0)


def test_short_open_is_limited_by_max_notional() -> None:
    """卖出开空属于增加敞口，必须受单笔上限约束。

    现货时代卖出只能是平仓，所以旧实现完全跳过 SELL；
    合约里 SELL + allow_short 会新开空头，跳过检查等于没有风控。
    """
    engine = make_engine(max_order_notional=100.0)
    decision = engine.check_order(
        side=Side.SELL,
        notional=500.0,
        snapshot=make_snapshot(),
        now_ms=NOW,
        allow_short=True,
    )
    assert not decision.allowed
    assert "单笔金额" in decision.reason


def test_close_short_still_unlimited() -> None:
    """卖出平仓（allow_short=False）仍是降低风险，不受上限限制。"""
    engine = make_engine(max_order_notional=100.0)
    decision = engine.check_order(
        side=Side.SELL,
        notional=500.0,
        snapshot=make_snapshot(),
        now_ms=NOW,
        allow_short=False,
    )
    assert decision.allowed


def test_short_position_uses_absolute_exposure() -> None:
    """空头持仓的 position_value 为负，敞口必须按绝对值算。"""
    engine = make_engine(max_order_notional=10_000.0, max_position_ratio=0.8)
    # cash 1500 + (-5 × 100) = 1000 权益；已占用 500 敞口，再买 400 达 90%
    snap = make_snapshot(cash=1_500.0, qty=-5.0, price=100.0)
    decision = engine.check_order(
        side=Side.BUY, notional=400.0, snapshot=snap, now_ms=NOW
    )
    assert not decision.allowed
    assert "敞口占比" in decision.reason
