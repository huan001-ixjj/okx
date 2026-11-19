"""内置策略集合。导入即完成注册。"""

from __future__ import annotations

from okxquant.strategy.inbuilt.grid import GridStrategy
from okxquant.strategy.inbuilt.ma_cross import MaCrossStrategy

__all__ = ["GridStrategy", "MaCrossStrategy"]
