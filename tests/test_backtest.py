"""回测引擎测试：撮合规则、确定性、落库。"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from okxquant.domain import Candle, Intent, Side
from okxquant.engine.backtest import (
    _Bracket,
    _bracket_exit,
    _fill_from_intent,
    _make_bracket,
    run_backtest,
)
from okxquant.engine.portfolio import Portfolio
from okxquant.store import Database, trading
from okxquant.store.candles import upsert_candles
from okxquant.strategy import Strategy, build
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


class TestBracket:
    """保护价：按信号价算出距离，再平移到实际成交价。"""

    def test_long_distances_shift_to_fill(self) -> None:
        intent = Intent(Side.BUY, qty=1.0, stop_px=95.0, take_px=110.0)
        bracket = _make_bracket(intent, signal_px=100.0, fill_px=102.0)
        assert bracket is not None
        assert bracket.stop_px == pytest.approx(97.0)
        assert bracket.take_px == pytest.approx(112.0)

    def test_short_distances_shift_to_fill(self) -> None:
        intent = Intent(Side.SELL, qty=1.0, allow_short=True, stop_px=105.0, take_px=90.0)
        bracket = _make_bracket(intent, signal_px=100.0, fill_px=98.0)
        assert bracket is not None
        assert bracket.stop_px == pytest.approx(103.0)
        assert bracket.take_px == pytest.approx(88.0)

    def test_no_stop_means_no_bracket(self) -> None:
        assert _make_bracket(Intent(Side.BUY, qty=1.0), 100.0, 101.0) is None

    def _candle(self, *, open_: float, high: float, low: float) -> Candle:
        return Candle(
            ts=1, open=open_, high=high, low=low, close=open_, volume=1.0
        )

    def test_long_stop_hit_at_stop_price(self) -> None:
        bracket = _Bracket(side=Side.BUY, stop_px=95.0, take_px=110.0)
        hit = _bracket_exit(bracket, self._candle(open_=100.0, high=105.0, low=94.0), 0.0)
        assert hit == (95.0, "止损")

    def test_long_stop_gap_fills_at_open(self) -> None:
        bracket = _Bracket(side=Side.BUY, stop_px=95.0, take_px=110.0)
        hit = _bracket_exit(bracket, self._candle(open_=90.0, high=92.0, low=88.0), 0.0)
        assert hit == (90.0, "止损")

    def test_long_take_profit(self) -> None:
        bracket = _Bracket(side=Side.BUY, stop_px=95.0, take_px=110.0)
        hit = _bracket_exit(bracket, self._candle(open_=100.0, high=111.0, low=99.0), 0.0)
        assert hit == (110.0, "止盈")

    def test_same_bar_prefers_stop(self) -> None:
        """同根同时触及止盈与止损时按止损优先（保守规则）。"""
        bracket = _Bracket(side=Side.BUY, stop_px=95.0, take_px=110.0)
        hit = _bracket_exit(bracket, self._candle(open_=100.0, high=120.0, low=90.0), 0.0)
        assert hit is not None and hit[1] == "止损"

    def test_short_stop_gap_fills_at_open(self) -> None:
        bracket = _Bracket(side=Side.SELL, stop_px=105.0, take_px=90.0)
        hit = _bracket_exit(bracket, self._candle(open_=110.0, high=112.0, low=108.0), 0.0)
        assert hit == (110.0, "止损")


class _BuyOnce(Strategy):
    """第一根就发一次买单，用于验证成交时点。"""

    name = "buy_once"

    def __init__(self) -> None:
        super().__init__()
        self._done = False

    def on_bar(self, ctx):  # type: ignore[no-untyped-def]
        if self._done:
            return []
        self._done = True
        return [Intent(Side.BUY, qty=1.0)]


class TestNextBarFill:
    def test_signal_fills_at_next_open(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "delay.db")
        candles = [
            Candle(ts=BASE_TS, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0),
            Candle(
                ts=BASE_TS + INTERVAL,
                open=102.0,
                high=103.0,
                low=101.0,
                close=102.5,
                volume=1.0,
            ),
        ]
        try:
            upsert_candles(db, "BTC-USDT", "1m", candles)
            result = run_backtest(
                db=db,
                strategy=_BuyOnce(),
                inst_id="BTC-USDT",
                bar="1m",
                bar_ms=INTERVAL,
                start_ts=BASE_TS,
                end_ts=BASE_TS + INTERVAL,
                initial_cash=10_000.0,
                fee_rate=0.0,
                slippage_bps=0.0,
                execution_delay=1,
            )
            assert len(result.fills) == 1
            assert result.fills[0].px == pytest.approx(102.0)  # 下一根开盘，而非信号收盘 100
        finally:
            db.close()

    def test_zero_delay_keeps_legacy_close_fill(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "nodly.db")
        candles = [
            Candle(ts=BASE_TS, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0),
            Candle(
                ts=BASE_TS + INTERVAL,
                open=102.0,
                high=103.0,
                low=101.0,
                close=102.5,
                volume=1.0,
            ),
        ]
        try:
            upsert_candles(db, "BTC-USDT", "1m", candles)
            result = run_backtest(
                db=db,
                strategy=_BuyOnce(),
                inst_id="BTC-USDT",
                bar="1m",
                bar_ms=INTERVAL,
                start_ts=BASE_TS,
                end_ts=BASE_TS + INTERVAL,
                initial_cash=10_000.0,
                fee_rate=0.0,
                slippage_bps=0.0,
            )
            assert result.fills[0].px == pytest.approx(100.0)
        finally:
            db.close()


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
