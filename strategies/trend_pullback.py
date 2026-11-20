"""XRP Trend Pullback V1.2。

高周期识别趋势方向与强度，信号周期等待回调后确认，然后**回撤挂限价**入场；
止损放在回调结构位外侧（受 ATR 上下限约束），止盈按固定 R 倍数，仓位按风险预算反推。

相对 V1.0 的结构性修改（针对「18/19 笔被止损」的诊断）：

1. **限价回撤入场**：V1.0 在「放量确认K线」收盘市价追入，等于在段末端买入/卖出，
   下一根反弹/回落直接打掉止损。现在改为在 EMA20 附近挂限价等回撤。
2. **结构位止损**：取「回调区间极值外侧 + 缓冲」，而不是固定 1.5×ATR，
   并用 ATR 上下限夹住。
3. **波动率分位过滤**：低波动期成本占 1R 比重过高时不做。
4. **周期可配**：``entry_bar``/``trend_bar`` 可换成 15m/1H，用于放大单笔 R。

三条防止未来函数的约定：

1. 高周期 K 线一律由**已收盘**的低周期 K 线聚合，桶内不连续则丢弃；
2. 信号只在信号周期收盘时产生，回测按下一根撮合（``execution_delay=1``）；
3. 量能基准 ``VolumeMA`` 不含当前 K 线。

数量口径：按「张」计算后换成币数量下单（``qty = 张数 × ctVal``），
使账本名义金额与真实合约一致，同时保留最小变动单位约束。
"""

from __future__ import annotations

import bisect
import math
from collections import deque

from okxquant.domain import Candle, Intent, Side
from okxquant.indicators import ADX, ATR, EMA, MACD, RSI, VolumeMA
from okxquant.strategy import BarCtx, Strategy

EPS = 1e-12
MS_PER_DAY = 86_400_000
BASE_MS = 300_000  # 回测/实盘驱动周期：5m
TF_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
}


class _Aggregator:
    """把已收盘的低周期 K 线聚合成高周期 K 线，凑满一个桶才输出。

    桶内必须连续且根数正好，否则丢弃该桶——宁可少一根高周期 K 线，
    也不用缺数据的聚合值污染趋势判断。
    """

    def __init__(self, bucket_ms: int, expected: int) -> None:
        if bucket_ms <= 0 or expected <= 0:
            raise ValueError("bucket_ms 与 expected 必须为正数")
        self._bucket_ms = bucket_ms
        self._expected = expected
        self._step_ms = bucket_ms // expected
        self._bucket: int | None = None
        self._bars: list[Candle] = []

    def add(self, candle: Candle) -> Candle | None:
        bucket = candle.ts - candle.ts % self._bucket_ms
        if self._bucket != bucket:
            self._bars = []
            self._bucket = bucket
        self._bars.append(candle)
        if len(self._bars) < self._expected:
            return None

        bars, self._bars = self._bars, []
        prev_ts = bars[0].ts
        for bar in bars[1:]:
            if bar.ts - prev_ts != self._step_ms:
                return None
            prev_ts = bar.ts

        return Candle(
            ts=bars[0].ts,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
            volume_ccy=sum(b.volume_ccy for b in bars),
            volume_quote=sum(b.volume_quote for b in bars),
            confirm=True,
        )


class TrendPullbackStrategy(Strategy):
    name = "trend_pullback"
    description = "高周期趋势过滤 + 信号周期回调限价入场，结构位止损 / 固定 R 止盈"
    default_params = {
        # 周期（可改 15m/1H 放大单笔 R）
        "entry_bar": "5m",
        "trend_bar": "15m",
        # 成交口径：信号收盘后下一根开始撮合
        "execution_delay": 1,
        # 方向开关：三年样本里空头是净亏损源，可单独关闭做对照
        "short_enabled": True,
        # 空头新鲜度：只在趋势建立后 N 根信号K线内做空（0 = 不限制）
        "short_max_regime_age": 0,
        # 趋势周期
        "ema_trend_fast": 50,
        "ema_trend_slow": 200,
        "adx_period": 14,
        "adx_min": 20.0,
        # 信号周期
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_period": 14,
        "rsi_long_min": 50.0,
        "rsi_short_max": 50.0,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        # 回调
        "pullback_atr_distance": 0.3,
        "pullback_valid_bars": 9,
        "ema50_break_atr": 0.2,
        # 入场方式
        "entry_mode": "limit",  # limit（回撤挂单）/ market（追确认K线）
        "limit_atr_offset": 0.05,
        "limit_ttl_bars": 3,
        # 量能
        "volume_ma_period": 20,
        "volume_multiplier": 1.2,
        # 出场
        "atr_period": 14,
        "stop_atr_mult": 1.5,
        "stop_structural_atr": 0.2,
        "stop_min_atr": 1.2,
        "stop_max_atr": 3.0,
        "take_profit_r": 2.0,
        # 风险
        "risk_per_trade": 0.005,
        "max_daily_loss_pct": 0.02,
        "max_consecutive_losses": 3,
        "cooldown_bars_after_stop": 3,
        "cooldown_bars_after_tp": 1,
        "leverage_cap": 2.0,
        # 波动率
        "atr_percent_min": 0.002,
        "atr_percentile_window": 500,
        "atr_percentile_min": 0.40,
        "atr_percentile_cap": 0.95,
        # 合约规格（实盘应改成运行时从 OKX 读取）
        "ct_val": 100.0,
        "lot_sz": 1.0,
        "min_sz": 1.0,
    }

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        self._signal_ms = self._resolve_ms(self.param("entry_bar", "5m"), "entry_bar")
        self._trend_ms = self._resolve_ms(self.param("trend_bar", "15m"), "trend_bar")
        if self._trend_ms <= self._signal_ms or self._trend_ms % self._signal_ms != 0:
            raise ValueError(
                f"trend_bar({self.param('trend_bar')}) 必须是 entry_bar"
                f"({self.param('entry_bar')}) 的整数倍且更大"
            )

        self._agg_signal = _Aggregator(self._signal_ms, self._signal_ms // BASE_MS)
        self._agg_trend = _Aggregator(self._trend_ms, self._trend_ms // self._signal_ms)

        # 趋势周期
        self._ema_tf_fast = EMA(self.param_int("ema_trend_fast", 50))
        self._ema_tf_slow = EMA(self.param_int("ema_trend_slow", 200))
        self._adx = ADX(self.param_int("adx_period", 14))

        # 信号周期
        self._ema_fast = EMA(self.param_int("ema_fast", 20))
        self._ema_slow = EMA(self.param_int("ema_slow", 50))
        self._rsi = RSI(self.param_int("rsi_period", 14))
        self._macd = MACD(
            self.param_int("macd_fast", 12),
            self.param_int("macd_slow", 26),
            self.param_int("macd_signal", 9),
        )
        self._atr = ATR(self.param_int("atr_period", 14))
        self._vol_ma = VolumeMA(self.param_int("volume_ma_period", 20))

        # 状态
        self._regime = 0
        self._regime_age = 0
        self._pullback_seen = False
        self._pullback_age = 0
        self._pullback_low = 0.0
        self._pullback_high = 0.0
        self._entry_wait = 0
        self._cooldown = 0
        self._prev_qty = 0.0
        self._entry_equity = 0.0
        self._loss_streak = 0
        self._day_key: int | None = None
        self._day_start_equity = 0.0
        self._blocked_day = -1
        self._atr_pct_sorted: list[float] = []
        self._atr_pct_buf: deque[float] = deque()
        self.last_reason = ""

    @staticmethod
    def _resolve_ms(spec: str, label: str) -> int:
        ms = TF_MS.get(str(spec))
        if ms is None:
            raise ValueError(f"不支持的 {label}={spec!r}，可选 {sorted(TF_MS)}")
        return ms

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def on_bar(self, ctx: BarCtx) -> list[Intent]:
        candle = ctx.candle

        # 1) 持仓跟踪：每根基础 K 线都要做，离场由执行层撮合
        if abs(ctx.position) > EPS:
            if self._entry_equity <= 0:
                self._entry_equity = ctx.equity
            self._prev_qty = ctx.position
            return []
        if abs(self._prev_qty) > EPS:
            self._on_position_closed(ctx)
        self._prev_qty = 0.0

        # 2) 只在信号周期收盘时推进
        sig = self._signal_bar(candle)
        if sig is None:
            return []

        # 3) 趋势周期：由信号周期 K 线聚合
        trend = self._agg_trend.add(sig)
        if trend is not None:
            self._ema_tf_fast.update(trend.close)
            self._ema_tf_slow.update(trend.close)
            self._set_regime(trend.close, self._adx.update(trend))

        # 4) 信号周期指标（vol_base 必须在 update 之前取，才不含当前 K 线）
        ema20 = self._ema_fast.update(sig.close)
        ema50 = self._ema_slow.update(sig.close)
        atr = self._atr.update(sig)
        rsi = self._rsi.update(sig.close)
        dif, dea, _ = self._macd.update(sig.close)
        vol_base = self._vol_ma.value
        self._vol_ma.update(sig.volume_quote)

        if self._cooldown > 0:
            self._cooldown -= 1
            return []

        # 5) 已有挂单在等成交时不重复下单
        if self._entry_wait > 0:
            self._entry_wait -= 1
            return []

        # 6) 日损 / 连亏熔断
        if not self._risk_allows(ctx, sig):
            return []

        # 7) 指标就绪
        if (
            ema20 is None
            or ema50 is None
            or atr is None
            or atr <= 0
            or rsi is None
            or dif is None
            or dea is None
            or vol_base is None
            or self._regime == 0
        ):
            self._reset_pullback()
            return []

        # 8) 波动率：绝对下限 + 滚动分位
        atr_pct = atr / sig.close
        if atr_pct < self.param_float("atr_percent_min", 0.002):
            self._reset_pullback()
            return []
        rank = self._atr_rank(atr_pct)
        if rank is not None:
            if rank > self.param_float("atr_percentile_cap", 0.95):
                return []
            floor = self.param_float("atr_percentile_min", 0.40)
            if floor > 0 and rank < floor:
                return []

        # 9) 方向
        if self._regime > 0:
            intent = self._long_signal(ctx, sig, ema20, ema50, atr, rsi, dif, dea, vol_base)
        elif self.param_bool("short_enabled", True):
            fresh = self.param_int("short_max_regime_age", 0)
            if fresh > 0 and self._regime_age > fresh:
                return []
            intent = self._short_signal(ctx, sig, ema20, ema50, atr, rsi, dif, dea, vol_base)
        else:
            return []
        return [intent] if intent is not None else []

    def _signal_bar(self, candle: Candle) -> Candle | None:
        """信号周期 K 线；entry_bar=5m 时直接返回驱动 K 线。"""
        if self._signal_ms == BASE_MS:
            return candle
        return self._agg_signal.add(candle)

    # ------------------------------------------------------------------ #
    # 信号
    # ------------------------------------------------------------------ #
    def _long_signal(self, ctx, candle, ema20, ema50, atr, rsi, dif, dea, vol_base):
        dist_mult = self.param_float("pullback_atr_distance", 0.3)
        break_mult = self.param_float("ema50_break_atr", 0.2)

        touched = (
            candle.low <= ema20 + dist_mult * atr
            and candle.close > ema50 - break_mult * atr
        )
        self._track_pullback(touched, candle)

        if not self._pullback_seen:
            return None
        if candle.close <= ema20:
            return None
        if (candle.close - ema20) / atr > dist_mult:
            return None
        if candle.close <= candle.open:
            return None
        if rsi <= self.param_float("rsi_long_min", 50.0):
            return None
        if dif <= dea:
            return None
        if candle.volume_quote < self.param_float("volume_multiplier", 1.2) * vol_base:
            return None

        entry_px = self._limit_price(candle, ema20, atr, Side.BUY)
        stop_dist = self._stop_distance(
            entry_px, min(self._pullback_low or entry_px, ema20), atr, Side.BUY
        )
        return self._build_entry(ctx, candle, entry_px, stop_dist, Side.BUY)

    def _short_signal(self, ctx, candle, ema20, ema50, atr, rsi, dif, dea, vol_base):
        dist_mult = self.param_float("pullback_atr_distance", 0.3)
        break_mult = self.param_float("ema50_break_atr", 0.2)

        touched = (
            candle.high >= ema20 - dist_mult * atr
            and candle.close < ema50 + break_mult * atr
        )
        self._track_pullback(touched, candle)

        if not self._pullback_seen:
            return None
        if candle.close >= ema20:
            return None
        if (ema20 - candle.close) / atr > dist_mult:
            return None
        if candle.close >= candle.open:
            return None
        if rsi >= self.param_float("rsi_short_max", 50.0):
            return None
        if dif >= dea:
            return None
        if candle.volume_quote < self.param_float("volume_multiplier", 1.2) * vol_base:
            return None

        entry_px = self._limit_price(candle, ema20, atr, Side.SELL)
        stop_dist = self._stop_distance(
            entry_px, max(self._pullback_high or entry_px, ema20), atr, Side.SELL
        )
        return self._build_entry(ctx, candle, entry_px, stop_dist, Side.SELL)

    def _limit_price(self, candle: Candle, ema20: float, atr: float, side: Side) -> float:
        """回撤挂单价：多单挂在 EMA20 上方一点，空单挂在下方一点。"""
        if self.param("entry_mode", "limit") != "limit":
            return candle.close
        offset = self.param_float("limit_atr_offset", 0.05) * atr
        if side.sign > 0:
            return min(candle.close, ema20 + offset)
        return max(candle.close, ema20 - offset)

    def _stop_distance(self, entry_px: float, struct_px: float, atr: float, side: Side) -> float:
        """止损距离：结构位外侧，再用 ATR 上下限夹住。"""
        buffer = self.param_float("stop_structural_atr", 0.2) * atr
        raw = (
            entry_px - (struct_px - buffer)
            if side.sign > 0
            else (struct_px + buffer) - entry_px
        )

        low = self.param_float("stop_min_atr", 1.2) * atr
        high = self.param_float("stop_max_atr", 3.0) * atr
        if low > high:
            low = high
        return max(low, min(raw, high))

    def _build_entry(
        self, ctx, candle: Candle, entry_px: float, stop_dist: float, side: Side
    ) -> Intent | None:
        if stop_dist <= 0 or entry_px <= 0:
            return None
        qty = self._size(ctx, entry_px, stop_dist)
        if qty <= 0:
            return None

        rr = self.param_float("take_profit_r", 2.0)
        signal_px = candle.close
        # 保护价以信号价为基准给出，执行层会按实际成交价平移
        if side.sign > 0:
            stop_px, take_px = signal_px - stop_dist, signal_px + rr * stop_dist
            reason = "LONG_ENTRY 回调限价"
        else:
            stop_px, take_px = signal_px + stop_dist, signal_px - rr * stop_dist
            reason = "SHORT_ENTRY 反弹限价"

        limit = self.param("entry_mode", "limit") == "limit"
        ttl = max(1, self.param_int("limit_ttl_bars", 3)) if limit else 1
        self._entry_wait = ttl
        self.last_reason = reason
        self._reset_pullback()
        return Intent(
            side,
            qty=qty,
            px=entry_px if limit else None,
            reason=reason,
            allow_short=side.sign < 0,
            stop_px=stop_px,
            take_px=take_px,
            ttl_bars=ttl,
        )

    # ------------------------------------------------------------------ #
    # 仓位
    # ------------------------------------------------------------------ #
    def _size(self, ctx, price: float, stop_dist: float) -> float:
        """按风险预算反推张数，再换算成币数量；返回 0 表示无法下单。"""
        equity = ctx.equity
        if equity <= 0 or price <= 0 or stop_dist <= 0:
            return 0.0

        budget = equity * self.param_float("risk_per_trade", 0.005)
        ct_val = self.param_float("ct_val", 100.0)
        lot = max(self.param_float("lot_sz", 1.0), 1e-9)
        min_sz = self.param_float("min_sz", 1.0)
        lev = max(self.param_float("leverage_cap", 2.0), 1e-9)

        contracts = math.floor(budget / (stop_dist * ct_val) / lot) * lot
        max_contracts = math.floor((equity * 0.95 * lev) / (price * ct_val) / lot) * lot
        contracts = min(contracts, max_contracts)

        if contracts < min_sz:
            return 0.0
        return contracts * ct_val

    # ------------------------------------------------------------------ #
    # 状态辅助
    # ------------------------------------------------------------------ #
    def _set_regime(self, close: float, adx: float | None) -> None:
        fast = self._ema_tf_fast.value
        slow = self._ema_tf_slow.value
        threshold = self.param_float("adx_min", 20.0)
        regime = 0
        if fast is not None and slow is not None and adx is not None:
            if fast > slow and close > slow and adx >= threshold:
                regime = 1
            elif fast < slow and close < slow and adx >= threshold:
                regime = -1
        if regime != self._regime:
            self._reset_pullback()
            self._regime_age = 0
        else:
            self._regime_age += 1
        self._regime = regime

    def _track_pullback(self, touched: bool, candle: Candle) -> None:
        valid = self.param_int("pullback_valid_bars", 9)
        if touched:
            if self._pullback_seen:
                self._pullback_low = min(self._pullback_low, candle.low)
                self._pullback_high = max(self._pullback_high, candle.high)
            else:
                self._pullback_low = candle.low
                self._pullback_high = candle.high
            self._pullback_seen = True
            self._pullback_age = 0
            return
        if self._pullback_seen:
            self._pullback_age += 1
            if self._pullback_age > valid:
                self._reset_pullback()

    def _reset_pullback(self) -> None:
        self._pullback_seen = False
        self._pullback_age = 0
        self._pullback_low = 0.0
        self._pullback_high = 0.0

    def _on_position_closed(self, ctx) -> None:
        """任何离场都按「止损冷却」处理（保守），并更新连亏计数。"""
        if self._entry_equity > 0:
            if ctx.equity < self._entry_equity:
                self._loss_streak += 1
            else:
                self._loss_streak = 0
        self._entry_equity = 0.0
        self._cooldown = max(
            self.param_int("cooldown_bars_after_stop", 3),
            self.param_int("cooldown_bars_after_tp", 1),
        )
        self._reset_pullback()

    def _risk_allows(self, ctx, candle: Candle) -> bool:
        day = candle.ts // MS_PER_DAY
        if day != self._day_key:
            # 跨 UTC 日：重置日损基准，并解除上一日的熔断（规则化解锁）
            self._day_key = day
            self._day_start_equity = ctx.equity
            self._blocked_day = -1
            self._loss_streak = 0
        if self._blocked_day == day:
            return False

        max_daily = self.param_float("max_daily_loss_pct", 0.02)
        if self._day_start_equity > 0:
            drawdown = (self._day_start_equity - ctx.equity) / self._day_start_equity
            if drawdown >= max_daily:
                self._blocked_day = day
                return False

        if self._loss_streak >= self.param_int("max_consecutive_losses", 3):
            self._blocked_day = day
            return False
        return True

    def _atr_rank(self, atr_pct: float) -> float | None:
        """当前 ATR% 在滚动窗口中的分位（0~1），窗口不足一半时返回 None。"""
        window = self.param_int("atr_percentile_window", 500)
        size = len(self._atr_pct_sorted)

        rank = None
        if window > 0 and size >= max(50, window // 2):
            rank = bisect.bisect_left(self._atr_pct_sorted, atr_pct) / size

        bisect.insort(self._atr_pct_sorted, atr_pct)
        self._atr_pct_buf.append(atr_pct)
        if window > 0 and len(self._atr_pct_buf) > window:
            oldest = self._atr_pct_buf.popleft()
            pos = bisect.bisect_left(self._atr_pct_sorted, oldest)
            if pos < len(self._atr_pct_sorted):
                self._atr_pct_sorted.pop(pos)
        return rank


STRATEGY = TrendPullbackStrategy
