"""子命令共用的小工具：时间解析、区间推导、策略装配。"""

from __future__ import annotations

import time
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

from okxquant.config import AppConfig, bar_to_ms
from okxquant.errors import ConfigError
from okxquant.store.candles import align_ts
from okxquant.strategy import Strategy, build

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")


def parse_time(text: str, label: str) -> int:
    for fmt in _TIME_FORMATS:
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise ConfigError(
        f"无法解析{label}时间: {text!r}",
        hint="支持格式：2026-11-01、2026-11-01 12:30、2026-11-01 12:30:00（本地时区）",
    )


def resolve_window(
    cfg: AppConfig,
    *,
    days: int | None,
    start: str | None,
    end: str | None,
    bar: str | None = None,
) -> tuple[int, int, int]:
    """返回 (start_ms, end_ms, interval_ms)，均已按周期对齐。"""
    period = bar or cfg.market.bar
    interval = bar_to_ms(period)

    if start:
        start_raw = parse_time(start, "起始")
    else:
        span_days = 7 if days is None else days
        if span_days <= 0:
            raise ConfigError("--days 必须大于 0")
        end_ref = parse_time(end, "结束") if end else int(time.time() * 1000)
        start_raw = end_ref - span_days * 86_400_000

    end_raw = parse_time(end, "结束") if end else int(time.time() * 1000)
    start_ms = align_ts(start_raw, interval)
    end_ms = align_ts(end_raw, interval)

    if start_ms >= end_ms:
        raise ConfigError("起始时间必须早于结束时间")
    return start_ms, end_ms, interval


def parse_overrides(values: list[str] | None) -> dict[str, Any]:
    """把 ``--param fast=3`` 解析成参数字典，自动推断类型。"""
    result: dict[str, Any] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ConfigError(f"参数格式应为 key=value，收到 {raw!r}")
        key, _, value = raw.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"参数名不能为空：{raw!r}")
        result[key] = _auto_type(value)
    return result


def _auto_type(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_strategy_with_params(
    cfg: AppConfig, spec: str, overrides: dict[str, Any] | None = None
) -> Strategy:
    """按优先级合并参数：内置参数文件 < 全局配置 < 命令行覆盖。"""
    params: dict[str, Any] = {}
    if not spec.endswith(".py") and "/" not in spec:
        param_file = Path("config/strategies") / f"{spec}.toml"
        if param_file.exists():
            try:
                with param_file.open("rb") as fh:
                    params.update(tomllib.load(fh))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"策略参数文件格式错误 {param_file}: {exc}") from exc

    params.update(cfg.strategy.params)
    if overrides:
        params.update(overrides)
    return build(spec, params)
