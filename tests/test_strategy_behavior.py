"""内置策略行为测试：用合成 K 线验证信号与边界。"""

from __future__ import annotations

from okxquant.domain import AccountSnapshot, CancelAll, Candle, Intent, Side
from okxquant.strategy import BarCtx, build
from tests.conftest import make_candles


def make_ctx(
    candle: Candle, *, position: float = 0.0, cash: float = 10_000.0, index: int = 0
) -> BarCtx:
    equity = cash + position * candle.close
    account = AccountSnapshot(
        cash=cash,
        position_qty=position,
        avg_cost=candle.close if position else 0.0,
        last_price=candle.close,
        equity=equity,
        unrealized=0.0,
        realized=0.0,
        drawdown=0.0,
        ts=candle.ts,
    )
    return BarCtx(candle=candle, account=account, bar_index=index)


def feed(strategy, prices: list[float], *, position: float = 0.0, cash: float = 10_000.0):  # type: ignore[no-untyped-def]
    """逐根喂入，返回全部动作（按 bar 聚合）。"""
    results = []
    for i, candle in enumerate(make_candles(prices)):
        results.append(strategy.on_bar(make_ctx(candle, position=position, cash=cash, index=i)))
    return results


class TestMaCross:
    def test_golden_cross_buys(self) -> None:
        strategy = build("ma_cross", {"fast": 2, "slow": 3, "size_pct": 0.5, "atr_stop_mult": 0})
        actions = feed(strategy, [10.0, 10.0, 10.0, 9.0, 9.0, 10.0])
        flat = [a for group in actions for a in group]
        assert any(isinstance(a, Intent) and a.side is Side.BUY for a in flat)

    def test_no_action_before_ready(self) -> None:
        strategy = build("ma_cross", {"fast": 5, "slow": 10})
        actions = feed(strategy, [10.0, 10.1])
        assert all(not group for group in actions)

    def test_death_cross_sells_all(self) -> None:
        strategy = build("ma_cross", {"fast": 2, "slow": 3, "size_pct": 0.5, "atr_stop_mult": 0})
        # 先涨出金叉，再跌出死叉
        actions = feed(
            strategy,
            [10.0, 10.0, 10.0, 12.0, 12.0, 9.0, 8.0],
            position=0.01,
        )
        flat = [a for group in actions for a in group]
        sells = [a for a in flat if isinstance(a, Intent) and a.side is Side.SELL]
        assert sells, "应产生卖出动作"

    def test_rejects_fast_not_less_than_slow(self) -> None:
        import pytest

        with pytest.raises(Exception, match="fast"):
            build("ma_cross", {"fast": 10, "slow": 5})


class TestGrid:
    def test_buys_on_dip(self) -> None:
        strategy = build(
            "grid",
            {
                "lower": 80.0,
                "upper": 120.0,
                "grids": 4,
                "qty_per_grid": 0.001,
                "max_inventory": 0.01,
            },
        )
        actions = feed(strategy, [100.0, 95.0])
        flat = [a for group in actions for a in group]
        buys = [a for a in flat if isinstance(a, Intent) and a.side is Side.BUY]
        assert buys and buys[0].qty == 0.001

    def test_sells_on_rise(self) -> None:
        strategy = build(
            "grid",
            {"lower": 80.0, "upper": 120.0, "grids": 4, "qty_per_grid": 0.001},
        )
        # 80~120 分 4 档，步长 10：100 在第 3 档，115 升到第 4 档
        actions = feed(strategy, [100.0, 115.0], position=0.005)
        flat = [a for group in actions for a in group]
        sells = [a for a in flat if isinstance(a, Intent) and a.side is Side.SELL]
        assert sells and sells[0].qty == 0.001

    def test_respects_max_inventory(self) -> None:
        strategy = build(
            "grid",
            {
                "lower": 80.0,
                "upper": 120.0,
                "grids": 4,
                "qty_per_grid": 0.01,
                "max_inventory": 0.005,
            },
        )
        actions = feed(strategy, [100.0, 85.0], position=0.004)
        flat = [a for group in actions for a in group]
        buys = [a for a in flat if isinstance(a, Intent) and a.side is Side.BUY]
        assert buys and buys[0].qty == 0.001  # 只剩 0.001 的空间

    def test_stop_loss_liquidates(self) -> None:
        strategy = build(
            "grid",
            {
                "lower": 80.0,
                "upper": 120.0,
                "grids": 4,
                "stop_loss_px": 85.0,
            },
        )
        actions = feed(strategy, [100.0, 84.0], position=0.01)
        flat = [a for group in actions for a in group]
        assert any(isinstance(a, CancelAll) for a in flat)
        assert any(isinstance(a, Intent) and a.side is Side.SELL for a in flat)

    def test_out_of_range_no_buy(self) -> None:
        strategy = build(
            "grid",
            {"lower": 80.0, "upper": 120.0, "grids": 4, "qty_per_grid": 0.001},
        )
        actions = feed(strategy, [100.0, 70.0])
        flat = [a for group in actions for a in group]
        assert not [a for a in flat if isinstance(a, Intent) and a.side is Side.BUY]


class TestScriptStrategy:
    def test_user_script_loads(self) -> None:
        strategy = build("strategies/bollinger_reversion.py", {"period": 20})
        assert strategy.name == "bollinger_reversion"
        assert strategy.param_int("period") == 20
