"""回测引擎测试：撮合规则、确定性、落库。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from okxquant.domain import Candle, Intent, Side
from okxquant.engine.backtest import _fill_from_intent, run_backtest
from okxquant.engine.portfolio import Portfolio
from okxquant.store import Database, trading
from okxquant.store.candles import upsert_candles
from okxquant.strategy import build
from tests.conftest import make_candles

INTERVAL = 60_000
BASE_TS = 1_700_000_000_000 - (1_700_000_000_000 % INTERVAL)


def make_db(tmp_path: Path, prices: list[float], bar: str = "1m") -> Database:
    db = Database(tmp_path / "bt.db")
    candles = make_candles(prices, start_ts=BASE_TS, interval_ms=INTERVAL)
    upsert_candles(db, "BTC-USDT", bar, candles)
    return db


class TestFillRules:
    def test_market_buy_applies_slippage_and_fee(self) -> None:
        candle = Candle(ts=1, open=100, high=101, low=99, close=100, volume=1)
        portfolio = Portfolio("BTC-USDT", 10_000.0)
        fill = _fill_from_intent(
            Intent(Side.BUY, quote_qty=100.0),
            candle,
            portfolio,
            fee_rate=0.001,
            slippage_bps=10,
            tag="t",
        )
        assert fill is not None
        assert fill.px == pytest.approx(100.1)
        assert fill.qty == pytest.approx(100 / 100.1)
        assert fill.fee == pytest.approx(100 * 0.001)

    def test_market_sell_applies_negative_slippage(self) -> None:
        candle = Candle(ts=1, open=100, high=101, low=99, close=100, volume=1)
        portfolio = Portfolio("BTC-USDT", 1.0)
        portfolio.position.qty = 0.1
        portfolio.position.avg_cost = 90.0
        fill = _fill_from_intent(
            Intent(Side.SELL, qty=0.05),
            candle,
            portfolio,
            fee_rate=0.0,
            slippage_bps=10,
            tag="t",
        )
        assert fill is not None
        assert fill.px == pytest.approx(99.9)

    def test_limit_buy_not_triggered_when_price_above(self) -> None:
        candle = Candle(ts=1, open=100, high=101, low=99, close=100, volume=1)
        portfolio = Portfolio("BTC-USDT", 10_000.0)
        fill = _fill_from_intent(
            Intent(Side.BUY, qty=0.01, px=98.0),
            candle,
            portfolio,
            fee_rate=0.001,
            slippage_bps=0,
            tag="t",
        )
        assert fill is None

    def test_limit_buy_triggered_at_limit_price(self) -> None:
        candle = Candle(ts=1, open=100, high=101, low=99, close=100, volume=1)
        portfolio = Portfolio("BTC-USDT", 10_000.0)
        fill = _fill_from_intent(
            Intent(Side.BUY, qty=0.01, px=99.5),
            candle,
            portfolio,
            fee_rate=0.0,
            slippage_bps=0,
            tag="t",
        )
        assert fill is not None
        assert fill.px == pytest.approx(99.5)

    def test_buy_shrinks_when_cash_insufficient(self) -> None:
        candle = Candle(ts=1, open=100, high=101, low=99, close=100, volume=1)
        portfolio = Portfolio("BTC-USDT", 50.0)
        fill = _fill_from_intent(
            Intent(Side.BUY, qty=1.0),
            candle,
            portfolio,
            fee_rate=0.0,
            slippage_bps=0,
            tag="t",
        )
        assert fill is not None
        assert fill.qty == pytest.approx(0.5)

    def test_sell_shrinks_to_position(self) -> None:
        candle = Candle(ts=1, open=100, high=101, low=99, close=100, volume=1)
        portfolio = Portfolio("BTC-USDT", 1.0)
        portfolio.position.qty = 0.02
        portfolio.position.avg_cost = 50.0
        fill = _fill_from_intent(
            Intent(Side.SELL, qty=1.0),
            candle,
            portfolio,
            fee_rate=0.0,
            slippage_bps=0,
            tag="t",
        )
        assert fill is not None
        assert fill.qty == pytest.approx(0.02)


def sine_prices(n: int = 120) -> list[float]:
    return [60_000 + 3_000 * math.sin(i / 8) for i in range(n)]


class TestBacktestRun:
    def test_deterministic_results(self, tmp_path: Path) -> None:
        prices = sine_prices()
        db = make_db(tmp_path, prices)
        try:
            end_ts = BASE_TS + (len(prices) - 1) * INTERVAL

            def once():  # type: ignore[no-untyped-def]
                strategy = build("ma_cross", {"fast": 3, "slow": 8, "atr_stop_mult": 0})
                return run_backtest(
                    db=db,
                    strategy=strategy,
                    inst_id="BTC-USDT",
                    bar="1m",
                    bar_ms=INTERVAL,
                    start_ts=BASE_TS,
                    end_ts=end_ts,
                    initial_cash=10_000.0,
                    fee_rate=0.0008,
                    slippage_bps=2.0,
                    sample_bars=10,
                )

            first = once()
            second = once()

            assert first.metrics.total_return == pytest.approx(second.metrics.total_return)
            assert len(first.fills) == len(second.fills)
            assert first.metrics.bars == len(prices)
            assert len(first.fills) > 0
        finally:
            db.close()

    def test_persists_fills_and_equity(self, tmp_path: Path) -> None:
        prices = sine_prices()
        db = make_db(tmp_path, prices)
        try:
            result = run_backtest(
                db=db,
                strategy=build("ma_cross", {"fast": 3, "slow": 8}),
                inst_id="BTC-USDT",
                bar="1m",
                bar_ms=INTERVAL,
                start_ts=BASE_TS,
                end_ts=BASE_TS + (len(prices) - 1) * INTERVAL,
                initial_cash=10_000.0,
                fee_rate=0.0008,
                slippage_bps=0.0,
                sample_bars=10,
            )
            assert trading.count_fills(db, result.run_id) == len(result.fills)
            equity = trading.load_equity(db, result.run_id)
            assert len(equity) > 1
            assert equity[-1].equity == pytest.approx(result.metrics.final_equity)
        finally:
            db.close()

    def test_empty_range_returns_zero_bars(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "empty.db")
        try:
            result = run_backtest(
                db=db,
                strategy=build("ma_cross", {"fast": 3, "slow": 8}),
                inst_id="BTC-USDT",
                bar="1m",
                bar_ms=INTERVAL,
                start_ts=BASE_TS,
                end_ts=BASE_TS + 10 * INTERVAL,
                initial_cash=1_000.0,
                fee_rate=0.0,
                slippage_bps=0.0,
            )
            assert result.metrics.bars == 0
            assert result.fills == []
            assert result.metrics.final_equity == pytest.approx(1_000.0)
        finally:
            db.close()

    def test_risk_engine_can_block_orders(self, tmp_path: Path) -> None:
        from okxquant.engine.risk import RiskEngine, RiskLimits

        prices = sine_prices()
        db = make_db(tmp_path, prices)
        try:
            risk = RiskEngine(RiskLimits(max_order_notional=1.0, order_cooldown_sec=0.0))
            result = run_backtest(
                db=db,
                strategy=build("ma_cross", {"fast": 3, "slow": 8}),
                inst_id="BTC-USDT",
                bar="1m",
                bar_ms=INTERVAL,
                start_ts=BASE_TS,
                end_ts=BASE_TS + (len(prices) - 1) * INTERVAL,
                initial_cash=10_000.0,
                fee_rate=0.0,
                slippage_bps=0.0,
                risk=risk,
            )
            assert result.fills == []
            assert result.skipped > 0
            assert result.metrics.final_equity == pytest.approx(10_000.0)
        finally:
            db.close()
