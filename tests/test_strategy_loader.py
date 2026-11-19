"""策略加载、校验与热重载测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from okxquant.domain import Side
from okxquant.errors import StrategyError
from okxquant.strategy import build, builtin_names, reload_into, validate
from okxquant.strategy.registry import load_module, strategy_class_from_module


def test_builtin_listed() -> None:
    names = builtin_names()
    assert "ma_cross" in names
    assert "grid" in names


def test_build_builtin_with_params() -> None:
    strategy = build("ma_cross", {"fast": 3, "slow": 7, "size_pct": 0.5})
    assert strategy.name == "ma_cross"
    assert strategy.param_int("fast") == 3
    assert strategy.param_float("size_pct") == 0.5


def test_unknown_strategy_message() -> None:
    with pytest.raises(StrategyError, match="未知策略"):
        build("does_not_exist")


def test_load_user_file() -> None:
    strategy = build("strategies/bollinger_reversion.py")
    assert strategy.name == "bollinger_reversion"
    assert strategy.param_int("period") == 40


def test_load_user_file_with_override() -> None:
    strategy = build("strategies/bollinger_reversion.py", {"k_entry": 1.5})
    assert strategy.param_float("k_entry") == 1.5


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(StrategyError, match="找不到策略文件"):
        build(str(tmp_path / "absent.py"))


def test_missing_strategy_symbol(tmp_path: Path) -> None:
    path = tmp_path / "no_symbol.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    module = load_module(path)
    with pytest.raises(StrategyError, match="缺少 STRATEGY"):
        strategy_class_from_module(module, source=path.name)


def test_strategy_must_subclass(tmp_path: Path) -> None:
    path = tmp_path / "wrong_base.py"
    path.write_text("class STRATEGY:\n    pass\n", encoding="utf-8")
    module = load_module(path)
    with pytest.raises(StrategyError, match="必须继承"):
        strategy_class_from_module(module, source=path.name)


def test_syntax_error_reports_location(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def on_bar(:\n    pass\n", encoding="utf-8")
    with pytest.raises(StrategyError, match="语法错误"):
        load_module(path)


def test_runtime_error_on_import(tmp_path: Path) -> None:
    path = tmp_path / "boom.py"
    path.write_text("raise RuntimeError('导入就炸')\n", encoding="utf-8")
    with pytest.raises(StrategyError, match="加载失败"):
        load_module(path)


def test_validate_returns_metadata() -> None:
    name, description, params = validate("ma_cross")
    assert name == "ma_cross"
    assert "双均线" in description
    assert "fast" in params


def test_reload_preserves_state() -> None:
    old = build("ma_cross", {"fast": 3, "slow": 7})
    old._prev_fast = 100.0
    old._prev_slow = 99.0
    old._peak = 123.0

    new = reload_into(old, "ma_cross")
    assert new is not old
    assert new._prev_fast == 100.0
    assert new._prev_slow == 99.0
    assert new._peak == 123.0
    assert new.reload_count == 1


def test_reload_failure_keeps_old_instance(tmp_path: Path) -> None:
    old = build("ma_cross", {"fast": 3, "slow": 7})
    broken = tmp_path / "broken.py"
    broken.write_text("class STRATEGY(:\n", encoding="utf-8")
    with pytest.raises(StrategyError):
        reload_into(old, str(broken))
    # 旧实例仍可用
    assert old.name == "ma_cross"
    assert old.param_int("fast") == 3


def test_side_enum_values() -> None:
    assert Side.BUY == "buy"
