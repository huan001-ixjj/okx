"""精度归一化与数量校验测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from okxquant.domain import (
    Intent,
    OrderType,
    Side,
    floor_to_step,
    normalize_px,
    normalize_qty,
    qty_str,
    round_to_step,
)


def test_floor_to_step() -> None:
    assert floor_to_step(0.123456789, Decimal("0.00001")) == Decimal("0.12345")
    assert floor_to_step(1.999, Decimal("1")) == Decimal("1")
    assert floor_to_step(5, Decimal("0.1")) == Decimal("5")


def test_round_to_step_uses_half_up() -> None:
    assert round_to_step(100.05, Decimal("0.1")) == Decimal("100.1")
    assert round_to_step(100.04, Decimal("0.1")) == Decimal("100.0")


def test_normalize_qty_and_px(instrument) -> None:  # type: ignore[no-untyped-def]
    assert normalize_qty(0.00123456789, instrument) == Decimal("0.00123456")
    assert normalize_px(64123.456, instrument) == Decimal("64123.5")


def test_qty_str_strips_trailing_zeros() -> None:
    assert qty_str(Decimal("0.001000")) == "0.001"
    assert qty_str(Decimal("1.50000000")) == "1.5"
    assert qty_str(2.0) == "2"


def test_intent_requires_exactly_one_quantity() -> None:
    with pytest.raises(ValueError):
        Intent(Side.BUY)
    with pytest.raises(ValueError):
        Intent(Side.BUY, qty=1.0, quote_qty=100.0)


def test_intent_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        Intent(Side.BUY, qty=0.0)
    with pytest.raises(ValueError):
        Intent(Side.BUY, qty=1.0, px=-5.0)


def test_intent_market_detection() -> None:
    assert Intent(Side.BUY, qty=1.0).is_market
    assert not Intent(Side.BUY, qty=1.0, px=100.0).is_market


def test_side_helpers() -> None:
    assert Side.BUY.opposite is Side.SELL
    assert Side.BUY.sign == 1
    assert Side.SELL.sign == -1
    assert OrderType.MARKET.value == "market"
