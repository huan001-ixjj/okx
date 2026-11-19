"""杠杆与爆仓测试。

三条事实在测试里钉死：
- 满仓时「名义 = 本金 × 杠杆」，因此收益率按杠杆**线性放大**
- 手续费按**名义额**计，杠杆越高、相对本金的费用占比越高
- 反向波动 1/杠杆 即爆仓（10x 跌 10%，100x 跌 1%）
"""

from __future__ import annotations

import pytest

from okxquant.domain import Fill, Side
from okxquant.engine.portfolio import Portfolio

CASH = 100.0
PRICE = 1_000.0


def open_long(leverage: float, fee: float = 0.0) -> Portfolio:
    """满仓开多：名义 = 本金 × 杠杆。"""
    p = Portfolio("BTC-USDT", CASH, leverage=leverage)
    qty = CASH * leverage / PRICE
    p.apply_fill(Fill(ts=1, side=Side.BUY, qty=qty, px=PRICE, fee=fee))
    return p


def test_full_position_occupies_entire_margin() -> None:
    p = open_long(leverage=10.0)

    assert p.position.qty == pytest.approx(1.0)  # 名义 1000 / 价格 1000
    assert p.cash == pytest.approx(0.0)  # 100 本金全部作为保证金
    assert p.equity() == pytest.approx(CASH)


def test_leverage_amplifies_return_linearly() -> None:
    one = open_long(leverage=1.0)
    one.mark(1_100.0, ts=2)  # +10%
    assert one.equity() == pytest.approx(110.0)  # 收益率 +10%

    ten = open_long(leverage=10.0)
    ten.mark(1_100.0, ts=2)  # +10%
    assert ten.equity() == pytest.approx(200.0)  # 收益率 +100%，放大 10 倍


def test_liquidation_at_inverse_leverage_move() -> None:
    p = open_long(leverage=10.0)
    p.mark(900.0, ts=2)  # -10% 正好亏光

    assert p.liquidations == 1
    assert p.position.qty == 0.0
    assert p.equity() == pytest.approx(0.0)


def test_high_leverage_liquidates_on_tiny_move() -> None:
    """100x 只需 1% 反向波动，而 BTC 4H 常见波动远超 1%。"""
    p = open_long(leverage=100.0)
    p.mark(990.0, ts=2)  # -1%

    assert p.liquidations == 1
    assert p.equity() == pytest.approx(0.0)


def test_low_leverage_survives_the_same_move() -> None:
    """同样的 -1%，10x 只是亏 10%，不会爆仓。"""
    p = open_long(leverage=10.0)
    p.mark(990.0, ts=2)

    assert p.liquidations == 0
    assert p.equity() == pytest.approx(90.0)


def test_fee_is_charged_on_notional() -> None:
    """同样 100 本金、同样 0.08% 费率，杠杆越高，手续费占本金越多。"""
    low = open_long(leverage=1.0, fee=0.08)  # 名义 100
    high = open_long(leverage=10.0, fee=0.8)  # 名义 1000

    assert low.fee_paid == pytest.approx(0.08)  # 占本金 0.08%
    assert high.fee_paid == pytest.approx(0.8)  # 占本金 0.80%
