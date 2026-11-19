"""策略注册与加载。

- 内置策略按名字解析（``ma_cross`` / ``grid``）。
- 自定义策略是普通 ``.py`` 文件，须定义继承 :class:`Strategy` 的 ``STRATEGY`` 类。
- 每次加载都产生新的模块对象，避免 ``sys.modules`` 缓存旧代码，从而支持热重载。
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from okxquant.errors import StrategyError
from okxquant.strategy.base import Strategy

_BUILTIN: dict[str, type[Strategy]] = {}
_LOAD_COUNTER = itertools.count(1)


def register(cls: type[Strategy]) -> type[Strategy]:
    """装饰器：把策略类登记为内置策略。"""
    if not issubclass(cls, Strategy):
        raise TypeError(f"{cls!r} 必须继承 Strategy")
    _BUILTIN[cls.name] = cls
    return cls


def _ensure_builtins() -> None:
    if _BUILTIN:
        return
    from okxquant.strategy.inbuilt import grid, ma_cross  # noqa: F401


def builtin_names() -> list[str]:
    _ensure_builtins()
    return sorted(_BUILTIN)


def builtin_registry() -> dict[str, type[Strategy]]:
    _ensure_builtins()
    return dict(_BUILTIN)


def _looks_like_path(spec: str) -> bool:
    return spec.endswith(".py") or "/" in spec or spec in {".", ".."}


def load_module(path: Path) -> Any:
    """按文件路径加载策略模块，每次返回全新的模块对象。"""
    if not path.exists():
        raise StrategyError(
            f"找不到策略文件: {path}",
            hint="确认路径正确，或用内置策略名（ma_cross / grid）",
        )
    if not path.is_file():
        raise StrategyError(f"策略路径不是文件: {path}")

    module_name = f"okxquant_user_{path.stem}_{next(_LOAD_COUNTER)}"
    spec_obj = importlib.util.spec_from_file_location(module_name, path)
    if spec_obj is None or spec_obj.loader is None:
        raise StrategyError(f"无法加载策略文件: {path}")

    module = importlib.util.module_from_spec(spec_obj)
    sys.modules[module_name] = module
    try:
        spec_obj.loader.exec_module(module)
    except SyntaxError as exc:
        sys.modules.pop(module_name, None)
        raise StrategyError(
            f"策略语法错误 {path.name}:{exc.lineno}: {exc.msg}"
        ) from exc
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise StrategyError(
            f"策略加载失败 {path.name}: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def strategy_class_from_module(module: Any, *, source: str = "") -> type[Strategy]:
    cls = getattr(module, "STRATEGY", None)
    if cls is None:
        raise StrategyError(
            f"{source or '策略文件'} 缺少 STRATEGY 定义",
            hint="策略文件必须导出一个继承 Strategy 的 STRATEGY 类",
        )
    if not isinstance(cls, type) or not issubclass(cls, Strategy):
        raise StrategyError(
            f"{source or '策略文件'} 的 STRATEGY 必须继承 okxquant.strategy.Strategy"
        )
    if cls is Strategy:
        raise StrategyError(f"{source or '策略文件'} 的 STRATEGY 不能直接使用基类")
    return cls


def resolve_class(spec: str) -> type[Strategy]:
    """把策略名或文件路径解析成策略类。"""
    _ensure_builtins()
    if _looks_like_path(spec):
        path = Path(spec).expanduser()
        module = load_module(path)
        return strategy_class_from_module(module, source=path.name)

    cls = _BUILTIN.get(spec)
    if cls is None:
        available = ", ".join(builtin_names())
        raise StrategyError(
            f"未知策略: {spec}",
            hint=f"可用内置策略: {available}；或用 .py 文件路径",
        )
    return cls


def build(spec: str, params: dict[str, Any] | None = None) -> Strategy:
    """实例化策略，参数与默认值合并。"""
    cls = resolve_class(spec)
    try:
        instance = cls(params or {})
    except StrategyError:
        raise
    except Exception as exc:
        raise StrategyError(f"策略 {spec} 初始化失败: {type(exc).__name__}: {exc}") from exc
    return instance


def validate(spec: str) -> tuple[str, str, dict[str, Any]]:
    """校验策略并返回 (name, description, default_params)。"""
    instance = build(spec)
    return instance.name, type(instance).description, dict(instance.default_params)


def reload_into(old: Strategy, spec: str, params: dict[str, Any] | None = None) -> Strategy:
    """热重载：构建新实例并迁移旧实例状态。

    新文件校验失败时抛 :class:`StrategyError`，调用方应保留旧实例继续运行。
    """
    merged = dict(old.params)
    if params:
        merged.update(params)
    new_instance = build(spec, merged)
    try:
        new_instance.load_state(old.state_dict())
    except Exception as exc:  # 状态迁移失败不应导致策略不可用
        logger.warning("策略重载时状态迁移失败，将以空状态启动: {}", exc)
    new_instance.reload_count = old.reload_count + 1
    return new_instance
