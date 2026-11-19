"""风控引擎：下单前置校验与熔断。

规则集中在这里，回测（可选启用）与实盘（强制启用）走同一套判断，
避免「回测能过、实盘被拒」的口径差异。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from loguru import logger

from okxquant.config import RiskConfig
from okxquant.domain import AccountSnapshot, Side


@dataclass(slots=True)
class RiskLimits:
    max_order_notional: float = 500.0
    max_position_ratio: float = 0.8
    max_daily_loss: float = 300.0
    min_order_notional: float = 10.0
    order_cooldown_sec: float = 5.0
    max_orders_per_day: int = 200

    @classmethod
    def from_config(cls, cfg: RiskConfig) -> RiskLimits:
        return cls(
            max_order_notional=cfg.max_order_notional,
            max_position_ratio=cfg.max_position_ratio,
            max_daily_loss=cfg.max_daily_loss,
            min_order_notional=cfg.min_order_notional,
            order_cooldown_sec=cfg.order_cooldown_sec,
            max_orders_per_day=cfg.max_orders_per_day,
        )


@dataclass(slots=True, frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    halted: bool = False


def _day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


class RiskEngine:
    """有状态的日级风控。熔断后必须显式 :meth:`clear_halt` 才能恢复。"""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._halted = False
        self._halt_reason = ""
        self._last_order_ts = 0
        self._orders_today = 0
        self._day = ""
        self._day_start_equity = 0.0
        self._day_low_equity = 0.0

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def orders_today(self) -> int:
        return self._orders_today

    def halt(self, reason: str) -> None:
        if not self._halted:
            logger.error("风控熔断：{}", reason)
        self._halted = True
        self._halt_reason = reason

    def clear_halt(self) -> None:
        self._halted = False
        self._halt_reason = ""

    # ------------------------------------------------------------------ #
    # 日切与权益跟踪
    # ------------------------------------------------------------------ #
    def _roll_day(self, ts_ms: int, equity: float) -> None:
        key = _day_key(ts_ms)
        if key == self._day:
            return
        self._day = key
        self._orders_today = 0
        self._day_start_equity = equity
        self._day_low_equity = equity
        logger.debug("风控日切：{} 起始权益 {:.2f}", key, equity)

    def update_equity(self, ts_ms: int, equity: float) -> bool:
        """每根 bar 调用，返回是否刚刚触发熔断。"""
        self._roll_day(ts_ms, equity)
        if self._day_start_equity <= 0:
            self._day_start_equity = equity
        self._day_low_equity = min(self._day_low_equity or equity, equity)

        loss = self._day_start_equity - equity
        if loss > self.limits.max_daily_loss and not self._halted:
            self.halt(
                f"日内亏损 {loss:.2f} USDT 超过上限 {self.limits.max_daily_loss:.2f} USDT"
            )
            return True
        return False

    def day_pnl(self, equity: float) -> float:
        if self._day_start_equity <= 0:
            return 0.0
        return equity - self._day_start_equity

    # ------------------------------------------------------------------ #
    # 下单校验
    # ------------------------------------------------------------------ #
    def check_order(
        self,
        *,
        side: Side,
        notional: float,
        snapshot: AccountSnapshot,
        now_ms: int,
    ) -> RiskDecision:
        if self._halted:
            return RiskDecision(False, f"已熔断：{self._halt_reason}", halted=True)

        self._roll_day(now_ms, snapshot.equity)

        cooldown = self.limits.order_cooldown_sec
        if cooldown > 0 and self._last_order_ts:
            elapsed = (now_ms - self._last_order_ts) / 1000.0
            if elapsed < cooldown:
                return RiskDecision(
                    False, f"下单冷却中（{elapsed:.1f}s < {cooldown:.1f}s）"
                )

        if self._orders_today >= self.limits.max_orders_per_day:
            return RiskDecision(
                False,
                f"当日下单次数 {self._orders_today} 已达上限 {self.limits.max_orders_per_day}",
            )

        if notional <= 0:
            return RiskDecision(False, "订单金额为 0")

        if notional < self.limits.min_order_notional:
            return RiskDecision(
                False,
                f"订单金额 {notional:.2f} 低于最小下单额 {self.limits.min_order_notional:.2f} USDT",
            )

        # 卖出是降低风险的方向，不限制单笔上限与仓位占比
        if side is Side.BUY:
            if notional > self.limits.max_order_notional:
                return RiskDecision(
                    False,
                    f"单笔金额 {notional:.2f} 超过上限 {self.limits.max_order_notional:.2f} USDT",
                )
            equity = snapshot.equity
            if equity > 0:
                projected = (snapshot.position_value + notional) / equity
                if projected > self.limits.max_position_ratio:
                    return RiskDecision(
                        False,
                        f"买入后持仓占比 {projected:.1%} 超过上限 "
                        f"{self.limits.max_position_ratio:.0%}",
                    )

        return RiskDecision(True)

    def note_order(self, now_ms: int, equity: float = 0.0) -> None:
        """记录一次成功下单，用于冷却与频率统计。"""
        self._roll_day(now_ms, equity)
        self._last_order_ts = now_ms
        self._orders_today += 1
