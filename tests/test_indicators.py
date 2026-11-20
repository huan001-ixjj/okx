"""指标数值正确性测试。"""

from __future__ import annotations

import pytest

from okxquant.indicators import (
    ADX,
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    Bollinger,
    Donchian,
    VolumeMA,
    Window,
)
from tests.conftest import make_candles


class TestSMA:
    def test_known_values(self) -> None:
        sma = SMA(3)
        assert sma.update(1) is None
        assert sma.update(2) is None
        assert sma.update(3) == pytest.approx(2.0)
        assert sma.update(4) == pytest.approx(3.0)
        assert sma.update(5) == pytest.approx(4.0)
        assert sma.ready

    def test_rejects_zero_period(self) -> None:
        with pytest.raises(ValueError):
            SMA(0)


class TestEMA:
    def test_seed_then_smooth(self) -> None:
        ema = EMA(3)
        assert ema.update(1) is None
        assert ema.update(2) is None
        assert ema.update(3) == pytest.approx(2.0)  # 种子为均值
        assert ema.update(4) == pytest.approx(3.0)  # 2 + (4-2)*0.5
        assert ema.update(5) == pytest.approx(4.0)


class TestRSI:
    def test_all_up_is_100(self) -> None:
        rsi = RSI(14)
        result = None
        for i in range(1, 16):
            result = rsi.update(float(i))
        assert result == pytest.approx(100.0)

    def test_all_down_is_0(self) -> None:
        rsi = RSI(14)
        result = None
        for i in range(20, 4, -1):
            result = rsi.update(float(i))
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_not_ready_before_period(self) -> None:
        rsi = RSI(14)
        for i in range(1, 5):
            assert rsi.update(float(i)) is None


class TestATR:
    def test_constant_range(self) -> None:
        atr = ATR(3)
        candles = make_candles([100.0] * 5, spread=0.01)
        assert atr.update(candles[0]) is None
        assert atr.update(candles[1]) is None
        value = atr.update(candles[2])
        assert value == pytest.approx(2.0, rel=1e-6)
        assert atr.ready


class TestBollinger:
    def test_flat_series_has_zero_band(self) -> None:
        bb = Bollinger(5, 2.0)
        upper = mid = lower = None
        for _ in range(5):
            upper, mid, lower = bb.update(100.0)
        assert mid == pytest.approx(100.0)
        assert upper == pytest.approx(100.0)
        assert lower == pytest.approx(100.0)

    def test_band_widens_with_volatility(self) -> None:
        bb = Bollinger(4, 2.0)
        for value in (1.0, 3.0, 1.0, 3.0):
            upper, mid, lower = bb.update(value)
        assert upper is not None and lower is not None and mid is not None
        assert upper > mid > lower


class TestDonchian:
    def test_high_low_window(self) -> None:
        don = Donchian(3)
        candles = make_candles([100.0, 102.0, 99.0, 101.0])
        assert don.update(candles[0]) == (None, None)
        assert don.update(candles[1]) == (None, None)
        high, low = don.update(candles[2])
        assert high == pytest.approx(102.0 * 1.001, rel=1e-6)
        assert low == pytest.approx(99.0 * 0.999, rel=1e-6)


class TestWindow:
    def test_keeps_last_n(self) -> None:
        win = Window(3)
        for value in (1, 2, 3, 4):
            win.push(float(value))
        assert win.values == (2.0, 3.0, 4.0)
        assert win.last == 4.0
        assert len(win) == 3


class TestVolumeMA:
    def test_base_excludes_current_bar(self) -> None:
        vol = VolumeMA(3)
        assert vol.value is None
        assert vol.update(1.0) is None
        assert vol.update(2.0) is None
        assert vol.update(3.0) is None
        # 前 3 根（1,2,3）的均值是 2.0，作为第 4 根的基准
        assert vol.value == pytest.approx(2.0)
        assert vol.update(6.0) == pytest.approx(2.0)
        assert vol.value == pytest.approx((2 + 3 + 6) / 3)

    def test_rejects_zero_period(self) -> None:
        with pytest.raises(ValueError):
            VolumeMA(0)


class TestMACD:
    def test_flat_series_gives_zero(self) -> None:
        macd = MACD(3, 6, 3)
        for _ in range(12):
            dif, dea, hist = macd.update(100.0)
        assert dif == pytest.approx(0.0)
        assert dea == pytest.approx(0.0)
        assert hist == pytest.approx(0.0)
        assert macd.ready

    def test_uptrend_has_positive_dif(self) -> None:
        macd = MACD(3, 6, 3)
        for i in range(1, 20):
            dif, _, _ = macd.update(float(i) * 10)
        assert dif is not None and dif > 0

    def test_not_ready_before_slow_period(self) -> None:
        macd = MACD(12, 26, 9)
        for i in range(1, 20):
            assert macd.update(float(i)) == (None, None, None)

    def test_rejects_fast_ge_slow(self) -> None:
        with pytest.raises(ValueError):
            MACD(26, 12, 9)


class TestADX:
    def test_uptrend_is_strong_and_positive(self) -> None:
        adx = ADX(14)
        candles = make_candles([100.0 + i for i in range(50)], spread=0.002)
        value = None
        for candle in candles:
            value = adx.update(candle)
        assert value is not None
        assert adx.ready
        assert value > 50.0
        assert adx.direction == 1
        assert adx.minus_di == pytest.approx(0.0)

    def test_downtrend_direction_negative(self) -> None:
        adx = ADX(14)
        candles = make_candles([200.0 - i for i in range(50)], spread=0.002)
        for candle in candles:
            adx.update(candle)
        assert adx.ready
        assert adx.direction == -1
        assert adx.plus_di == pytest.approx(0.0)

    def test_flat_series_has_zero_adx(self) -> None:
        adx = ADX(5)
        candles = make_candles([100.0] * 20, spread=0.0)
        value = None
        for candle in candles:
            value = adx.update(candle)
        assert value == pytest.approx(0.0)
        assert adx.plus_di == pytest.approx(0.0)
        assert adx.minus_di == pytest.approx(0.0)

    def test_not_ready_before_two_periods(self) -> None:
        adx = ADX(14)
        candles = make_candles([100.0 + i for i in range(20)], spread=0.002)
        for candle in candles:
            assert adx.update(candle) is None
        assert not adx.ready

    def test_rejects_small_period(self) -> None:
        with pytest.raises(ValueError):
            ADX(1)
