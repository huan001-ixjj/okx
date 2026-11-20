"""事件驱动回测引擎。

与实盘共用 ``Strategy.on_bar``；差别只在成交撮合：这里由 :class:`PaperBroker`
按「市价 = 收盘价 ± 滑点」「限价 = 触价成交」的规则即时成交，并扣除手续费。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

from okxquant.domain import (
    Action,
    CancelAll,
    Candle,
    EquityPoint,
    Fill,
    Intent,
    Metrics,
    Side,
)
from okxquant.engine.portfolio import Portfolio, periods_per_year
from okxquant.engine.risk import RiskEngine
from okxquant.store import Database, runs, trading
from okxquant.store.candles import iter_candles
from okxquant.strategy.base import BarCtx, Strategy


@dataclass(slots=True)
class BacktestResult:
    run_id: int
    metrics: Metrics
    fills: list[Fill] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    # 每根 bar 的权益，仅用于绘图；落库用的是上面的采样点
    equity_series: list[float] = field(default_factory=list)
    risk_events: list[str] = field(default_factory=list)
    skipped: int = 0


def _intent_notional(intent: Intent, price: float) -> float:
    if intent.quote_qty is not None:
        return intent.quote_qty
    qty = intent.qty or 0.0
    return qty * (intent.px or price)


@dataclass(slots=True)
class _Bracket:
    """持仓的保护价。``side`` 是持仓方向（BUY=多仓）。"""

    side: Side
    stop_px: float
    take_px: float | None = None


def _make_bracket(intent: Intent, signal_px: float, fill_px: float) -> _Bracket | None:
    """按信号价与成交价之差平移保护价，保证风险距离不因下一根开盘跳空而失真。"""
    if intent.stop_px is None or signal_px <= 0:
        return None
    if intent.side.sign > 0:
        stop_dist = signal_px - intent.stop_px
        take_dist = (intent.take_px - signal_px) if intent.take_px is not None else None
        stop_px = fill_px - stop_dist
        take_px = (fill_px + take_dist) if take_dist is not None else None
    else:
        stop_dist = intent.stop_px - signal_px
        take_dist = (signal_px - intent.take_px) if intent.take_px is not None else None
        stop_px = fill_px + stop_dist
        take_px = (fill_px - take_dist) if take_dist is not None else None
    if stop_px <= 0 or (take_px is not None and take_px <= 0):
        return None
    return _Bracket(side=intent.side, stop_px=stop_px, take_px=take_px)


def _bracket_exit(
    bracket: _Bracket, candle: Candle, slippage_bps: float
) -> tuple[float, str] | None:
    """按本根 high/low 判断保护价是否触发。

    同一根 K 线同时触及止损与止盈时按**止损优先**处理（方案 §7.3 的保守规则）。
    止损是市价单，跳空穿越时按开盘价成交（更差），不假设能拿到止损价。
    """
    slip = slippage_bps / 10_000.0
    if bracket.side.sign > 0:
        if candle.low <= bracket.stop_px:
            px = min(bracket.stop_px, candle.open)
            return px * (1 - slip), "止损"
        if bracket.take_px is not None and candle.high >= bracket.take_px:
            return bracket.take_px * (1 - slip), "止盈"
    else:
        if candle.high >= bracket.stop_px:
            px = max(bracket.stop_px, candle.open)
            return px * (1 + slip), "止损"
        if bracket.take_px is not None and candle.low <= bracket.take_px:
            return bracket.take_px * (1 + slip), "止盈"
    return None


def _fill_from_intent(
    intent: Intent,
    candle: Candle,
    portfolio: Portfolio,
    *,
    fee_rate: float,
    slippage_bps: float,
    tag: str,
    ref_px: float | None = None,
    maker_fee_rate: float | None = None,
) -> Fill | None:
    """把意图撮合成一笔成交；无法成交（限价未触及、资金/持仓不足）返回 None。

    ``ref_px`` 用于「下一根开盘成交」：市价单以该价格（通常是下一根开盘价）为基准。
    ``maker_fee_rate`` 给出时，限价单按挂单费率计费（市价单仍按吃单费率）。
    """
    slip = slippage_bps / 10_000.0

    if intent.is_market:
        base = ref_px if ref_px is not None else candle.close
        px = base * (1 + slip) if intent.side.sign > 0 else base * (1 - slip)
    else:
        assert intent.px is not None
        # 限价单：当根 K 线必须触及委托价才成交
        if intent.side.sign > 0 and candle.low > intent.px:
            return None
        if intent.side.sign < 0 and candle.high < intent.px:
            return None
        px = intent.px

    if px <= 0:
        return None

    qty = intent.quote_qty / px if intent.quote_qty is not None else (intent.qty or 0.0)

    # 用购买力（现金 × 杠杆）校验可负担数量，否则杠杆会被撮合层抵消
    affordable = portfolio.buying_power / (px * (1 + fee_rate))

    if intent.side.sign > 0:
        qty = min(qty, max(0.0, affordable))
    elif intent.allow_short:
        # 合约语义：允许卖出超过当前持仓，形成空头
        qty = min(qty, max(0.0, affordable))
    else:
        # 现货语义：只平多，超出持仓部分截断
        qty = min(qty, max(0.0, portfolio.position.qty))

    if qty <= 0:
        return None

    # 限价单吃的是挂单费率（maker），市价单吃吃单费率（taker）
    effective_fee = (
        maker_fee_rate
        if maker_fee_rate is not None and not intent.is_market
        else fee_rate
    )
    fee = qty * px * effective_fee
    return Fill(ts=candle.ts, side=intent.side, qty=qty, px=px, fee=fee, cl_ord_id=tag)


def run_backtest(
    *,
    db: Database,
    strategy: Strategy,
    inst_id: str,
    bar: str,
    bar_ms: int,
    start_ts: int,
    end_ts: int,
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    sample_bars: int = 60,
    risk: RiskEngine | None = None,
    on_progress: Callable[[int], None] | None = None,
    params: dict | None = None,
    leverage: float = 1.0,
    execution_delay: int = 0,
    maker_fee_rate: float | None = None,
) -> BacktestResult:
    """遍历区间内所有已收盘 K 线，逐根调用策略并撮合成交。

    ``execution_delay`` 为 0 时维持旧行为（信号当根收盘价成交）；设为 1 时
    信号在收盘后排队，**下一根 K 线开盘价**成交——这是方案 §5/§13 要求的口径，
    也是避免「信号K线内成交」这一未来函数的最低要求。
    """
    run_id = runs.create_run(
        db,
        kind="backtest",
        strategy=strategy.name,
        inst_id=inst_id,
        bar=bar,
        start_ts=start_ts,
        end_ts=end_ts,
        params=params or strategy.params,
        initial_equity=initial_cash,
    )

    portfolio = Portfolio(inst_id, initial_cash, leverage=leverage)
    fills: list[Fill] = []
    sampled: list[EquityPoint] = []
    risk_events: list[str] = []
    skipped = 0

    bar_index = 0
    first_price = 0.0
    last_price = 0.0
    # 待成交队列：(意图, 信号K线收盘价, 失效 bar_index)
    pending: list[tuple[Intent, float, int]] = []
    bracket: _Bracket | None = None

    for page in iter_candles(db, inst_id, bar, start_ts, end_ts):
        for candle in page:
            if first_price <= 0:
                first_price = candle.close

            # 1) 本根开盘：撮合上一根收盘时排队的意图（限价单可跨多根等成交）
            if pending:
                still: list[tuple[Intent, float, int]] = []
                for intent, signal_px, expire_at in pending:
                    fill = _fill_from_intent(
                        intent,
                        candle,
                        portfolio,
                        fee_rate=fee_rate,
                        slippage_bps=slippage_bps,
                        tag=f"bt{bar_index}",
                        ref_px=candle.open,
                        maker_fee_rate=maker_fee_rate,
                    )
                    if fill is None:
                        if bar_index < expire_at:
                            still.append((intent, signal_px, expire_at))
                        else:
                            skipped += 1
                        continue
                    portfolio.apply_fill(fill)
                    fills.append(fill)
                    if risk is not None:
                        risk.note_order(candle.ts, portfolio.equity())
                    if abs(portfolio.position.qty) <= 0:
                        bracket = None
                    else:
                        bracket = _make_bracket(intent, signal_px, fill.px) or bracket
                pending = still

            # 2) 持仓保护价：只按本根高低价判断，不看收盘价
            if bracket is not None:
                if abs(portfolio.position.qty) <= 0:
                    bracket = None
                else:
                    hit = _bracket_exit(bracket, candle, slippage_bps)
                    if hit is not None:
                        px, why = hit
                        qty = abs(portfolio.position.qty)
                        side = Side.SELL if portfolio.position.qty > 0 else Side.BUY
                        fill = Fill(
                            ts=candle.ts,
                            side=side,
                            qty=qty,
                            px=px,
                            fee=qty * px * fee_rate,
                            cl_ord_id=f"bt{bar_index}-{why}",
                        )
                        portfolio.apply_fill(fill)
                        fills.append(fill)
                        bracket = None

            snapshot = portfolio.mark(candle.close, candle.ts)
            if risk is not None and risk.update_equity(candle.ts, snapshot.equity):
                risk_events.append(risk.halt_reason)
                logger.warning("回测中触发熔断：{}", risk.halt_reason)

            actions: list[Action] = strategy.on_bar(
                BarCtx(candle=candle, account=snapshot, bar_index=bar_index)
            )

            for action in actions:
                if isinstance(action, CancelAll):
                    continue

                if risk is not None:
                    decision = risk.check_order(
                        side=action.side,
                        notional=_intent_notional(action, candle.close),
                        snapshot=snapshot,
                        now_ms=candle.ts,
                        allow_short=action.allow_short,
                    )
                    if not decision.allowed:
                        skipped += 1
                        logger.debug("回测跳过下单（{}）：{}", decision.reason, action.reason)
                        continue

                if execution_delay > 0:
                    pending.append(
                        (action, candle.close, bar_index + max(1, action.ttl_bars))
                    )
                    continue

                fill = _fill_from_intent(
                    action,
                    candle,
                    portfolio,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    tag=f"bt{bar_index}",
                    maker_fee_rate=maker_fee_rate,
                )
                if fill is None:
                    skipped += 1
                    continue

                portfolio.apply_fill(fill)
                fills.append(fill)
                if risk is not None:
                    risk.note_order(candle.ts, snapshot.equity)
                if abs(portfolio.position.qty) <= 0:
                    bracket = None
                else:
                    bracket = _make_bracket(action, candle.close, fill.px) or bracket

            point = portfolio.record_bar(candle.ts, candle.close)
            if sample_bars > 0 and bar_index % sample_bars == 0:
                sampled.append(point)

            last_price = candle.close
            bar_index += 1

        if on_progress is not None:
            on_progress(bar_index)

    # 确保曲线包含最后一根
    series, series_ts = portfolio.equity_series
    if series and (not sampled or sampled[-1].ts != series_ts[-1]):
        sampled.append(
            EquityPoint(
                ts=series_ts[-1],
                equity=series[-1],
                cash=portfolio.cash,
                position_qty=portfolio.position.qty,
                price=portfolio.last_price,
            )
        )

    metrics = portfolio.build_metrics(
        bars=bar_index,
        first_price=first_price,
        last_price=last_price or first_price,
        periods_per_year=periods_per_year(bar_ms),
    )
    metrics.start_ts = metrics.start_ts or start_ts
    metrics.end_ts = metrics.end_ts or end_ts

    trading.save_fills(db, run_id, fills)
    trading.save_equity_points(db, run_id, sampled)
    for reason in risk_events:
        trading.record_risk_event(
            db,
            run_id=run_id,
            ts=metrics.end_ts,
            rule="risk_halt",
            message=reason,
        )
    runs.finish_run(db, run_id, metrics=metrics.to_dict(), final_equity=metrics.final_equity)

    return BacktestResult(
        run_id=run_id,
        metrics=metrics,
        fills=fills,
        equity=sampled,
        equity_series=list(series),
        risk_events=risk_events,
        skipped=skipped,
    )
