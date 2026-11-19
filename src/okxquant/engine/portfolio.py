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

    def __init__(
        self, inst_id: str, initial_cash: float, *, leverage: float = 1.0
    ) -> None:
        if initial_cash <= 0:
            raise ValueError("初始资金必须为正数")
        if leverage <= 0:
            raise ValueError("杠杆必须为正数")
        self.inst_id = inst_id
        self.initial_cash = initial_cash
        self.leverage = leverage
        self.liquidations = 0
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
        """支持多空双向持仓：BUY 增仓、SELL 减仓，跨越 0 即反手。

        现货与合约共用这一套记账：``qty`` 为正表示多头、为负表示空头，
        因此 ``equity = cash + qty * price`` 对空头自动成立（价格下跌则权益上升）。
        """
        old_qty = self.position.qty
        old_avg = self.position.avg_cost
        old_margin = abs(old_qty) * old_avg / self.leverage

        signed_qty = fill.qty if fill.side is Side.BUY else -fill.qty
        new_qty = old_qty + signed_qty

        gross = 0.0
        closed = False

        if abs(old_qty) <= EPS:
            # 空仓开出新仓
            self.position.avg_cost = fill.px
        elif old_qty * new_qty > 0 and abs(new_qty) > abs(old_qty):
            # 同向加仓：加权平均成本
            self.position.avg_cost = (old_avg * abs(old_qty) + fill.px * fill.qty) / abs(
                new_qty
            )
        elif old_qty * new_qty > 0:
            # 同向减仓（部分平仓）
            closed = True
            direction = 1.0 if old_qty > 0 else -1.0
            gross = (fill.px - old_avg) * fill.qty * direction
        elif abs(new_qty) <= EPS:
            # 刚好全部平掉
            closed = True
            direction = 1.0 if old_qty > 0 else -1.0
            gross = (fill.px - old_avg) * abs(old_qty) * direction
        else:
            # 反手：先按旧成本结算全部旧仓，再以新价建反向仓
            closed = True
            direction = 1.0 if old_qty > 0 else -1.0
            gross = (fill.px - old_avg) * abs(old_qty) * direction
            self.position.avg_cost = fill.px

        if abs(new_qty) <= EPS:
            self.position.qty = 0.0
            self.position.avg_cost = 0.0
        else:
            self.position.qty = new_qty

        # 保证金模型：现金只被占用「名义/杠杆」，平仓时返还并结算盈亏。
        # leverage=1 时退化为现货口径，与旧实现完全一致。
        new_margin = abs(self.position.qty) * self.position.avg_cost / self.leverage
        self.cash += old_margin - new_margin + gross - fill.fee

        if closed:
            net = gross - fill.fee
            self.realized += net
            self.trade_pnls.append(net)

        self.fee_paid += fill.fee
        self.fills.append(fill)
        self.last_price = fill.px

    def _liquidate(self, price: float) -> None:
        """保证金亏光：强制平仓，本金归零。"""
        self.cash = 0.0
        self.position.qty = 0.0
        self.position.avg_cost = 0.0
        self.last_price = price
        self.liquidations += 1

    def mark(self, price: float, ts: int = 0) -> AccountSnapshot:
        """按最新价重估，更新峰值与回撤，返回不可变快照。

        杠杆下若权益归零则触发爆仓（强平），此后本金不再变化。
        """
        if price > 0:
            self.last_price = price

        equity = self.equity()
        if equity <= 0 and abs(self.position.qty) > EPS:
            self._liquidate(self.last_price)
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
            cash=self.buying_power,
            position_qty=self.position.qty,
            avg_cost=self.position.avg_cost,
            last_price=self.last_price,
            equity=eq,
            unrealized=self.position.unrealized(self.last_price),
            realized=self.realized,
            drawdown=dd,
            ts=ts,
        )

    @property
    def buying_power(self) -> float:
        """可开仓的购买力 = 可用保证金 × 杠杆。

        策略通过 ``ctx.cash`` 看到的是这个值，因此杠杆会真正放大仓位，
        而不是仅仅减少保证金占用。``leverage=1`` 时等于现金。
        """
        return max(0.0, self.cash) * self.leverage

    def equity(self) -> float:
        """权益 = 可用现金 + 占用保证金 + 浮动盈亏。

        保证金模型下现金只被占用了「名义/杠杆」，因此必须把保证金加回来，
        否则杠杆交易会被严重低估。``leverage=1`` 时等价于 ``cash + qty*price``。
        """
        margin = abs(self.position.qty) * self.position.avg_cost / self.leverage
        return self.cash + margin + self.position.unrealized(self.last_price)

    def record_bar(self, ts: int, price: float) -> EquityPoint:
        """每根 bar 收盘后调用一次，用于曲线与夏普计算。"""
        snap = self.mark(price, ts)
        self._equity_series.append(snap.equity)
        self._equity_ts.append(ts)
        self._total_bars += 1
        if abs(self.position.qty) > EPS:
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
