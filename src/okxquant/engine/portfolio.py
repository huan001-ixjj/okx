"""组合记账与绩效指标。

回测与实盘共用：回测用 PaperBroker 产出的 Fill，实盘用交易所回报的 Fill，
最终都落到同一个 :class:`Portfolio` 上，保证两条路径的记账口径一致。
"""

from __future__ import annotations

import math
from array import array

from okxquant.domain import (
    AccountSnapshot,
    EquityPoint,
    Fill,
    Metrics,
    Position,
    Side,
)

EPS = 1e-12


class Portfolio:
    """现金 + 单币种持仓的账本。"""

    def __init__(self, inst_id: str, initial_cash: float) -> None:
        if initial_cash <= 0:
            raise ValueError("初始资金必须为正数")
        self.inst_id = inst_id
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.position = Position(inst_id=inst_id)
        self.fills: list[Fill] = []
        self.realized = 0.0
        self.fee_paid = 0.0
        self.trade_pnls: list[float] = []
        self.last_price = 0.0
        self.peak_equity = initial_cash
        self.max_drawdown = 0.0
        self.max_drawdown_ts = 0
        self._equity_series: array[float] = array("d")
        self._equity_ts: list[int] = []
        self._exposure_bars = 0
        self._total_bars = 0

    # ------------------------------------------------------------------ #
    # 记账
    # ------------------------------------------------------------------ #
    def apply_fill(self, fill: Fill) -> None:
        cost = fill.qty * fill.px
        if fill.side is Side.BUY:
            self.cash -= cost + fill.fee
            new_qty = self.position.qty + fill.qty
            if new_qty > EPS:
                self.position.avg_cost = (
                    self.position.avg_cost * self.position.qty + cost
                ) / new_qty
            self.position.qty = new_qty
        else:
            realized = (fill.px - self.position.avg_cost) * fill.qty - fill.fee
            self.cash += cost - fill.fee
            self.position.qty = max(0.0, self.position.qty - fill.qty)
            if self.position.qty <= EPS:
                self.position.qty = 0.0
                self.position.avg_cost = 0.0
            self.realized += realized
            self.trade_pnls.append(realized)

        self.fee_paid += fill.fee
        self.fills.append(fill)
        self.last_price = fill.px

    def mark(self, price: float, ts: int = 0) -> AccountSnapshot:
        """按最新价重估，更新峰值与回撤，返回不可变快照。"""
        if price > 0:
            self.last_price = price

        equity = self.equity()
        if equity > self.peak_equity:
            self.peak_equity = equity

        drawdown = 0.0
        if self.peak_equity > 0:
            drawdown = max(0.0, (self.peak_equity - equity) / self.peak_equity)
            if drawdown > self.max_drawdown:
                self.max_drawdown = drawdown
                self.max_drawdown_ts = ts

        return self.snapshot(ts, equity=equity, drawdown=drawdown)

    def snapshot(
        self, ts: int = 0, *, equity: float | None = None, drawdown: float | None = None
    ) -> AccountSnapshot:
        eq = self.equity() if equity is None else equity
        dd = 0.0
        if drawdown is not None:
            dd = drawdown
        elif self.peak_equity > 0:
            dd = max(0.0, (self.peak_equity - eq) / self.peak_equity)

        return AccountSnapshot(
            cash=self.cash,
            position_qty=self.position.qty,
            avg_cost=self.position.avg_cost,
            last_price=self.last_price,
            equity=eq,
            unrealized=self.position.unrealized(self.last_price),
            realized=self.realized,
            drawdown=dd,
            ts=ts,
        )

    def equity(self) -> float:
        return self.cash + self.position.qty * self.last_price

    def record_bar(self, ts: int, price: float) -> EquityPoint:
        """每根 bar 收盘后调用一次，用于曲线与夏普计算。"""
        snap = self.mark(price, ts)
        self._equity_series.append(snap.equity)
        self._equity_ts.append(ts)
        self._total_bars += 1
        if self.position.qty > 0:
            self._exposure_bars += 1
        return EquityPoint(
            ts=ts,
            equity=snap.equity,
            cash=snap.cash,
            position_qty=snap.position_qty,
            price=price,
        )

    # ------------------------------------------------------------------ #
    # 绩效
    # ------------------------------------------------------------------ #
    def build_metrics(
        self,
        *,
        bars: int,
        first_price: float,
        last_price: float,
        periods_per_year: float,
    ) -> Metrics:
        initial = self.initial_cash
        final = self.equity()
        total_return = (final / initial - 1.0) if initial > 0 else 0.0
        buy_hold = (last_price / first_price - 1.0) if first_price > 0 else 0.0

        wins = [p for p in self.trade_pnls if p > 0]
        losses = [p for p in self.trade_pnls if p < 0]
        gains = sum(wins)
        gross_loss = abs(sum(losses))
        trade_count = len(self.trade_pnls)

        metrics = Metrics(
            bars=bars,
            initial_equity=initial,
            final_equity=final,
            total_return=total_return,
            max_drawdown=self.max_drawdown,
            max_drawdown_ts=self.max_drawdown_ts,
            sharpe=self._sharpe(periods_per_year),
            win_rate=(len(wins) / trade_count) if trade_count else 0.0,
            profit_factor=(gains / gross_loss) if gross_loss > 0 else (gains if gains else 0.0),
            trade_count=trade_count,
            win_count=len(wins),
            loss_count=len(losses),
            total_fees=self.fee_paid,
            buy_hold_return=buy_hold,
            exposure=(self._exposure_bars / self._total_bars) if self._total_bars else 0.0,
        )
        if self._equity_ts:
            metrics.start_ts = self._equity_ts[0]
            metrics.end_ts = self._equity_ts[-1]
        return metrics

    def _sharpe(self, periods_per_year: float) -> float:
        values = self._equity_series
        n = len(values)
        if n < 3:
            return 0.0

        returns: list[float] = []
        for i in range(1, n):
            prev = values[i - 1]
            if prev > 0:
                returns.append(values[i] / prev - 1.0)
        if len(returns) < 2:
            return 0.0

        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        if std <= 0:
            return 0.0
        return mean / std * math.sqrt(periods_per_year)

    @property
    def equity_series(self) -> tuple[array[float], list[int]]:
        return self._equity_series, self._equity_ts


def periods_per_year(bar_ms: int) -> float:
    """把任意周期换算成每年多少根 bar。"""
    if bar_ms <= 0:
        return 1.0
    return 365.0 * 24.0 * 60.0 * 60.0 * 1000.0 / bar_ms
