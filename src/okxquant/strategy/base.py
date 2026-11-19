"""策略抽象基类与上下文。

策略是纯同步函数：只读行情与账户快照，返回下单意图，不碰网络、不碰数据库。
回测与实盘共用同一个 ``on_bar`` 调用路径，差异只在 Broker 实现。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from okxquant.domain import AccountSnapshot, Action, Candle
from okxquant.errors import StrategyError


@dataclass(slots=True, frozen=True)
class BarCtx:
    """单根 K 线收盘时交给策略的全部信息。"""

    candle: Candle
    account: AccountSnapshot
    bar_index: int

    @property
    def price(self) -> float:
        return self.candle.close

    @property
    def position(self) -> float:
        return self.account.position_qty

    @property
    def cash(self) -> float:
        return self.account.cash

    @property
    def equity(self) -> float:
        return self.account.equity


class Strategy(ABC):
    """所有策略的基类。

    子类至少要实现 :meth:`on_bar`；:attr:`name` 与 :attr:`default_params`
    用于展示和参数默认值合并。
    """

    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    default_params: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = {**self.default_params, **(params or {})}
        self.reload_count = 0

    # ------------------------------------------------------------------ #
    # 参数读取
    # ------------------------------------------------------------------ #
    def has_param(self, key: str) -> bool:
        return key in self.params

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def param_float(self, key: str, default: float = 0.0) -> float:
        value = self.params.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise StrategyError(f"参数 {key} 需要数值，当前为 {value!r}") from exc

    def param_int(self, key: str, default: int = 0) -> int:
        value = self.params.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise StrategyError(f"参数 {key} 需要整数，当前为 {value!r}") from exc

    def param_bool(self, key: str, default: bool = False) -> bool:
        value = self.params.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> list[Action]:
        """策略启动时调用一次，可返回初始动作。"""
        return []

    @abstractmethod
    def on_bar(self, ctx: BarCtx) -> list[Action]:
        """每根已收盘 K 线调用一次。"""

    def on_stop(self) -> list[Action]:
        """策略停止时调用一次。"""
        return []

    # ------------------------------------------------------------------ #
    # 热重载时的状态迁移
    # ------------------------------------------------------------------ #
    def state_dict(self) -> dict[str, Any]:
        """需要跨重载保留的内部状态。默认不保留任何状态。"""
        return {}

    def load_state(self, state: dict[str, Any]) -> None:  # noqa: B027
        """从旧实例迁移状态，新实例应能容忍缺字段。"""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
