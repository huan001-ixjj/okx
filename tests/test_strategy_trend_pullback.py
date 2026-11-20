"""趋势回调策略测试：15m 聚合、入场管线、仓位与风控。"""

from __future__ import annotations

import pytest
from strategies.trend_pullback import TrendPullbackStrategy, _Aggregator

from okxquant.domain import AccountSnapshot, Candle, Side
from okxquant.strategy import BarCtx

TF15_MS = 900_000

BASE_TS = 1_700_000_000_000 - (1_700_000_000_000 % TF15_MS)
BAR_MS = 300_000


def snapshot(equity: float = 10_000.0, qty: float = 0.0) -> AccountSnapshot:
    return AccountSnapshot(
        cash=equity,
        position_qty=qty,
        avg_cost=0.0,
        last_price=1.0,
        equity=equity,
        unrealized=0.0,
        realized=0.0,
        drawdown=0.0,
        ts=BASE_TS,
    )


def bar(i: int, price: float, *, spread: float = 0.004, volume: float = 1000.0) -> Candle:
    return Candle(
        ts=BASE_TS + i * BAR_MS,
        open=price * (1 - spread / 2),
        high=price * (1 + spread),
        low=price * (1 - spread),
        close=price,
        volume=volume / 100.0 / price,
        volume_ccy=volume / price,
        volume_quote=volume,
        confirm=True,
    )


def relaxed_params(**extra) -> dict:
    """把过滤条件放到最松，只验证入场管线本身。"""
    params = {
        "adx_min": 0.0,
        "rsi_long_min": 0.0,
        "rsi_short_max": 100.0,
        "volume_multiplier": 0.0,
        "pullback_atr_distance": 5.0,
        "atr_percent_min": 0.0,
        "atr_percentile_window": 0,
        "execution_delay": 1,
    }
    params.update(extra)
    return params


class TestAggregator:
    def test_three_bars_make_one_15m(self) -> None:
        agg = _Aggregator(TF15_MS, 3)
        candles = [
            bar(0, 100.0),
            bar(1, 101.0),
            bar(2, 99.0),
        ]
        assert agg.add(candles[0]) is None
        assert agg.add(candles[1]) is None
        tf15 = agg.add(candles[2])
        assert tf15 is not None
        assert tf15.open == pytest.approx(100.0 * (1 - 0.004 / 2))
        assert tf15.close == pytest.approx(99.0)
        assert tf15.high == pytest.approx(101.0 * 1.004)
        assert tf15.low == pytest.approx(99.0 * 0.996)
        assert tf15.volume_quote == pytest.approx(3000.0)

    def test_gap_in_bucket_is_discarded(self) -> None:
        agg = _Aggregator(TF15_MS, 3)
        agg.add(bar(0, 100.0))
        # 跳过第 1 根，直接给第 2 根：桶内不连续，不能拼出 15m
        assert agg.add(bar(2, 101.0)) is None
        assert agg.add(bar(3, 102.0)) is None


class TestAggregatorNoLookahead:
    def test_only_closed_bars_are_used(self) -> None:
        """第 3 根 5m 收盘的同一时刻才产出 15m，且 ts 指向桶起点。"""
        agg = _Aggregator(TF15_MS, 3)
        out = [agg.add(bar(i, 100.0 + i)) for i in range(3)]
        assert out[:2] == [None, None]
        assert out[2] is not None
        assert out[2].ts == BASE_TS


class TestEntryPipeline:
    def _drive(self, strategy: TrendPullbackStrategy, candles: list[Candle]):
        """返回 [(bar_index, intent)]，便于按信号所在 K 线做断言。"""
        out = []
        for idx, candle in enumerate(candles):
            ctx = BarCtx(candle=candle, account=snapshot(), bar_index=idx)
            out.extend((idx, intent) for intent in strategy.on_bar(ctx))
        return out

    def test_uptrend_produces_long_with_atr_bracket(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params())
        # 0.2%/根 的稳定上行，足够让 15m EMA200 预热完成
        candles = [bar(i, 1.0 * (1.002**i)) for i in range(700)]
        signals = self._drive(strategy, candles)

        assert signals, "上行趋势中应产生做多信号"
        idx, first = signals[0]
        price = candles[idx].close
        assert first.side is Side.BUY
        assert first.allow_short is False
        assert first.stop_px is not None and first.take_px is not None
        # 止损在下、止盈在上，且止盈距离 = 2 × 止损距离
        assert first.stop_px < price < first.take_px
        assert pytest.approx(first.take_px - price) == pytest.approx((price - first.stop_px) * 2)

    def test_sideways_regime_never_trades(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params())
        candles = [bar(i, 1.0) for i in range(700)]
        assert self._drive(strategy, candles) == []

    def test_position_blocks_new_signals(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params())
        candles = [bar(i, 1.0 * (1.002**i)) for i in range(700)]

        first_at = None
        for idx, candle in enumerate(candles):
            if strategy.on_bar(BarCtx(candle=candle, account=snapshot(), bar_index=idx)):
                first_at = idx
                break
        assert first_at is not None

        extra = 0
        for idx in range(first_at + 1, first_at + 21):
            candle = bar(idx, 1.0 * (1.002**idx))
            extra += len(
                strategy.on_bar(
                    BarCtx(candle=candle, account=snapshot(qty=1000.0), bar_index=idx)
                )
            )
        assert extra == 0, "持仓期间不应再产生信号"


class TestSizing:
    def test_qty_follows_risk_budget(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params())
        qty = strategy._size(snapshot(equity=10_000.0), price=1.0, stop_dist=0.01)
        # 风险预算 50 USDT / (0.01 × 100 XRP/张) = 50 张 → 5000 XRP
        assert qty == pytest.approx(5000.0)

    def test_min_lot_blocks_undersized_account(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params())
        # 权益 100 USDT：0.5% = 0.5 USDT，1 张风险 = 0.01×100 = 1 USDT，下不了单
        assert strategy._size(snapshot(equity=100.0), price=1.0, stop_dist=0.01) == 0.0

    def test_margin_cap_limits_size(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params(risk_per_trade=0.5))
        qty = strategy._size(snapshot(equity=1_000.0), price=1.0, stop_dist=0.001)
        # 杠杆 2 倍、权益 1000 → 名义上限 1900 USDT → 1900 XRP
        assert qty == pytest.approx(1900.0)

    def test_consecutive_loss_breaker_unlocks_next_day(self) -> None:
        strategy = TrendPullbackStrategy(relaxed_params())
        today = bar(0, 1.0)
        today_ctx = BarCtx(candle=today, account=snapshot(), bar_index=0)
        # 第一次调用只建立当日权益基准
        assert strategy._risk_allows(today_ctx, today) is True

        strategy._loss_streak = 3  # noqa: SLF001
        assert strategy._risk_allows(today_ctx, today) is False

        tomorrow = Candle(
            ts=BASE_TS + 86_400_000,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
        )
        ctx = BarCtx(candle=tomorrow, account=snapshot(), bar_index=1)
        # 跨日必须解锁，否则熔断会变成永久停机
        assert strategy._risk_allows(ctx, tomorrow) is True

    def test_atr_floor_blocks_low_volatility(self) -> None:
        strategy = TrendPullbackStrategy(
            relaxed_params(atr_percent_min=0.5)  # 要求 ATR 占价格 50%，正常行情不可能满足
        )
        candles = [bar(i, 1.0 * (1.002**i)) for i in range(700)]
        actions = []
        for idx, candle in enumerate(candles):
            ctx = BarCtx(candle=candle, account=snapshot(), bar_index=idx)
            actions.extend(strategy.on_bar(ctx))
        assert actions == []
