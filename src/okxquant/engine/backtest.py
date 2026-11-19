"""事件驱动回测引擎。

与实盘共用 ``Strategy.on_bar``；差别只在成交撮合：这里由 :class:`PaperBroker`
按「市价 = 收盘价 ± 滑点」「限价 = 触价成交」的规则即时成交，并扣除手续费。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from loguru import logger

from okxquant.domain import Action, CancelAll, Candle, EquityPoint, Fill, Intent, Metrics
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


def _fill_from_intent(
    intent: Intent,
    candle: Candle,
    portfolio: Portfolio,
    *,
    fee_rate: float,
    slippage_bps: float,
    tag: str,
) -> Fill | None:
    """把意图撮合成一笔成交；无法成交（限价未触及、资金/持仓不足）返回 None。"""
    slip = slippage_bps / 10_000.0

    if intent.is_market:
        px = candle.close * (1 + slip) if intent.side.sign > 0 else candle.close * (1 - slip)
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

    if intent.side.sign > 0:
        affordable = portfolio.cash / (px * (1 + fee_rate))
        qty = min(qty, max(0.0, affordable))
    else:
        qty = min(qty, portfolio.position.qty)

    if qty <= 0:
        return None

    fee = qty * px * fee_rate
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
) -> BacktestResult:
    """遍历区间内所有已收盘 K 线，逐根调用策略并撮合成交。"""
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

    portfolio = Portfolio(inst_id, initial_cash)
    fills: list[Fill] = []
    sampled: list[EquityPoint] = []
    risk_events: list[str] = []
    skipped = 0

    bar_index = 0
    first_price = 0.0
    last_price = 0.0

    for page in iter_candles(db, inst_id, bar, start_ts, end_ts):
        for candle in page:
            if first_price <= 0:
                first_price = candle.close

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
                    )
                    if not decision.allowed:
                        skipped += 1
                        logger.debug("回测跳过下单（{}）：{}", decision.reason, action.reason)
                        continue

                fill = _fill_from_intent(
                    action,
                    candle,
                    portfolio,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    tag=f"bt{bar_index}",
                )
                if fill is None:
                    skipped += 1
                    continue

                portfolio.apply_fill(fill)
                fills.append(fill)
                if risk is not None:
                    risk.note_order(candle.ts, snapshot.equity)

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
