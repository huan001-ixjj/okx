"""领域模型：行情、订单、成交、持仓、账户快照、绩效指标，以及精度工具。

设计约定：
- 时间戳一律为毫秒整数（UTC）。
- 价格与数量内部用 float 参与计算，只在构造真实订单前用 Decimal 做步长归一化。
- 策略只产出 :class:`Intent` / :class:`CancelAll`，不直接接触网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """买 +1，卖 -1，用于持仓增减计算。"""
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    PENDING = "pending"  # 已提交本地账本但交易所未确认
    LIVE = "live"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    FAILED = "failed"
    STALE = "stale"  # 启动对账时发现的悬空订单

    @property
    def is_open(self) -> bool:
        return self in {OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED}

    @property
    def is_final(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.FAILED,
            OrderStatus.STALE,
        }


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    confirm: bool = True

    @classmethod
    def from_okx(cls, row: list[str]) -> Candle:
        """OKX K 线数组格式：[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]。"""
        return cls(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            confirm=len(row) > 8 and str(row[8]) == "1",
        )

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(slots=True, frozen=True)
class Ticker:
    inst_id: str
    ts: int
    last: float
    bid: float
    ask: float
    open_24h: float

    @property
    def change_pct_24h(self) -> float:
        if self.open_24h <= 0:
            return 0.0
        return (self.last - self.open_24h) / self.open_24h


@dataclass(slots=True, frozen=True)
class Instrument:
    """交易对精度约束。"""

    inst_id: str
    base_ccy: str
    quote_ccy: str
    tick_sz: Decimal
    lot_sz: Decimal
    min_sz: Decimal

    @classmethod
    def from_okx(cls, row: dict[str, str]) -> Instrument:
        return cls(
            inst_id=row["instId"],
            base_ccy=row.get("baseCcy", "BTC"),
            quote_ccy=row.get("quoteCcy", "USDT"),
            tick_sz=Decimal(str(row.get("tickSz") or "0.1")),
            lot_sz=Decimal(str(row.get("lotSz") or "0.00000001")),
            min_sz=Decimal(str(row.get("minSz") or "0.00001")),
        )


# --------------------------------------------------------------------------- #
# 策略意图
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class Intent:
    """下单意图。``qty`` 与 ``quote_qty`` 二选一；``px`` 为空表示市价。"""

    side: Side
    qty: float | None = None
    quote_qty: float | None = None
    px: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if (self.qty is None) == (self.quote_qty is None):
            raise ValueError("Intent 必须且只能指定 qty 或 quote_qty 之一")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("qty 必须为正数")
        if self.quote_qty is not None and self.quote_qty <= 0:
            raise ValueError("quote_qty 必须为正数")
        if self.px is not None and self.px <= 0:
            raise ValueError("px 必须为正数")

    @property
    def is_market(self) -> bool:
        return self.px is None


@dataclass(slots=True, frozen=True)
class CancelAll:
    """撤销该交易对全部挂单。"""

    reason: str = ""


Action = Intent | CancelAll


# --------------------------------------------------------------------------- #
# 账户与订单
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Order:
    cl_ord_id: str
    inst_id: str
    side: Side
    ord_type: OrderType
    qty: float
    px: float | None
    status: OrderStatus
    filled_qty: float = 0.0
    avg_px: float = 0.0
    fee: float = 0.0
    exchange_ord_id: str = ""
    created_ts: int = 0
    updated_ts: int = 0
    reason: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def notional(self) -> float:
        px = self.avg_px or self.px or 0.0
        return self.qty * px


@dataclass(slots=True, frozen=True)
class Fill:
    ts: int
    side: Side
    qty: float
    px: float
    fee: float
    cl_ord_id: str = ""
    ord_id: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.px


@dataclass(slots=True)
class Position:
    inst_id: str = ""
    qty: float = 0.0
    avg_cost: float = 0.0
    realized_pnl: float = 0.0

    def market_value(self, price: float) -> float:
        return self.qty * price

    def unrealized(self, price: float) -> float:
        if self.qty <= 0:
            return 0.0
        return (price - self.avg_cost) * self.qty


@dataclass(slots=True, frozen=True)
class AccountSnapshot:
    """策略可见的账户视图，不可变副本。"""

    cash: float
    position_qty: float
    avg_cost: float
    last_price: float
    equity: float
    unrealized: float
    realized: float
    drawdown: float
    ts: int = 0

    @property
    def position_value(self) -> float:
        return self.position_qty * self.last_price

    @property
    def position_ratio(self) -> float:
        return self.position_value / self.equity if self.equity > 0 else 0.0


@dataclass(slots=True, frozen=True)
class EquityPoint:
    ts: int
    equity: float
    cash: float
    position_qty: float
    price: float


# --------------------------------------------------------------------------- #
# 绩效指标
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Metrics:
    start_ts: int = 0
    end_ts: int = 0
    bars: int = 0
    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_ts: int = 0
    sharpe: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    total_fees: float = 0.0
    buy_hold_return: float = 0.0
    exposure: float = 0.0  # 有持仓的 bar 占比

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Metrics:
        """从数据库里的 JSON 还原，忽略多余字段。"""
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def to_dict(self) -> dict[str, float | int]:
        return {
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "bars": self.bars,
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_ts": self.max_drawdown_ts,
            "sharpe": self.sharpe,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_fees": self.total_fees,
            "buy_hold_return": self.buy_hold_return,
            "exposure": self.exposure,
        }


# --------------------------------------------------------------------------- #
# 精度工具
# --------------------------------------------------------------------------- #


def _dec(value: float | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def floor_to_step(value: float | str | Decimal, step: Decimal) -> Decimal:
    """向下取整到步长整数倍。"""
    v = _dec(value)
    if step <= 0:
        return v
    return (v / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_to_step(value: float | str | Decimal, step: Decimal) -> Decimal:
    """四舍五入到步长整数倍。"""
    v = _dec(value)
    if step <= 0:
        return v
    return (v / step).to_integral_value(rounding=ROUND_HALF_UP) * step


def normalize_qty(qty: float, instrument: Instrument) -> Decimal:
    """数量按 lotSz 向下取整，避免超出持仓或余额。"""
    return floor_to_step(qty, instrument.lot_sz)


def normalize_px(px: float, instrument: Instrument) -> Decimal:
    """价格按 tickSz 四舍五入。"""
    return round_to_step(px, instrument.tick_sz)


def qty_str(qty: Decimal | float) -> str:
    """把数量格式化为交易所接受的字符串（去掉多余的 0）。"""
    d = _dec(qty).normalize()
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(slots=True)
class OrderBookLevel:
    px: float
    sz: float


@dataclass(slots=True)
class OrderBook:
    inst_id: str
    ts: int
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
