"""配置加载：TOML 文件 + 环境变量 + .env 凭据。"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from okxquant.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("config/default.toml")

# OKX 支持的 K 线周期标识（注意大小写：分钟小写 m，小时/天/周大写）
VALID_BARS = {
    "1m", "3m", "5m", "15m", "30m",
    "1H", "2H", "4H", "6H", "12H",
    "1D", "2D", "3D", "1W", "1M",
}

BAR_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "2H": 7_200_000,
    "4H": 14_400_000,
    "6H": 21_600_000,
    "12H": 43_200_000,
    "1D": 86_400_000,
    "2D": 172_800_000,
    "3D": 259_200_000,
    "1W": 604_800_000,
    "1M": 2_592_000_000,
}


def bar_to_ms(bar: str) -> int:
    """把 OKX 的 bar 标识换算成毫秒数。"""
    try:
        return BAR_MS[bar]
    except KeyError as exc:  # pragma: no cover - 已被配置校验拦截
        raise ConfigError(f"不支持的 K 线周期: {bar}") from exc


class MarketConfig(BaseModel):
    inst_id: str = "BTC-USDT"
    bar: str = "1m"

    @field_validator("bar")
    @classmethod
    def _check_bar(cls, value: str) -> str:
        if value not in VALID_BARS:
            raise ValueError(f"bar 必须是 {sorted(VALID_BARS)} 之一，当前为 {value!r}")
        return value

    @field_validator("inst_id")
    @classmethod
    def _check_inst(cls, value: str) -> str:
        if not value or "-" not in value:
            raise ValueError("inst_id 形如 BTC-USDT")
        return value


class StorageConfig(BaseModel):
    db_path: Path = Path("data/quant.db")

    def ensure_parent(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: Path = Path("logs")
    retention_days: int = 14

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"未知日志级别: {value}")
        return level


class ExecutionConfig(BaseModel):
    fee_rate: float = Field(default=0.0008, ge=0, le=0.01)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)
    initial_cash: float = Field(default=10_000.0, gt=0)


class RiskConfig(BaseModel):
    max_order_notional: float = Field(default=500.0, gt=0)
    max_position_ratio: float = Field(default=0.8, gt=0, le=1)
    max_daily_loss: float = Field(default=300.0, gt=0)
    min_order_notional: float = Field(default=10.0, gt=0)
    order_cooldown_sec: float = Field(default=5.0, ge=0)
    max_orders_per_day: int = Field(default=200, ge=1)
    allow_live: bool = False


class RenderConfig(BaseModel):
    equity_sample_bars: int = Field(default=60, ge=1)
    color: bool = True


class StrategyConfig(BaseModel):
    name: str = "ma_cross"
    params: dict[str, Any] = Field(default_factory=dict)


class NetworkConfig(BaseModel):
    """网络出口配置。

    默认全部关闭，走直连 —— 部署到能直接访问 OKX 的服务器时不需要任何改动。
    SSH 隧道只用于开发机这类无法直连的环境，密码不在这里，见 ``tunnel`` 模块。
    """

    proxy: str = ""
    tunnel_enabled: bool = False
    tunnel_host: str = ""
    tunnel_port: int = Field(default=1080, ge=1, le=65535)
    tunnel_ssh_port: int = Field(default=22, ge=1, le=65535)
    tunnel_timeout_sec: float = Field(default=20.0, gt=0, le=120)

    @field_validator("tunnel_host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        text = value.strip()
        if text and "@" not in text:
            raise ValueError("tunnel_host 形如 user@host")
        return text

    @property
    def has_tunnel(self) -> bool:
        return self.tunnel_enabled and bool(self.tunnel_host)


class AppConfig(BaseModel):
    market: MarketConfig = Field(default_factory=MarketConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    @property
    def bar_ms(self) -> int:
        return bar_to_ms(self.market.bar)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"找不到配置文件: {path}",
            hint="从仓库根目录运行，或用 --config 指定配置文件路径",
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件格式错误: {path}\n{exc}") from exc


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: Path | None = None) -> AppConfig:
    """加载 TOML 配置，支持环境变量覆盖少量字段。"""
    # 先加载 .env，保证其中的网络配置在配置校验阶段就可见
    load_dotenv(override=False)

    env_path = os.getenv("OKXQUANT_CONFIG")
    config_path = path or (Path(env_path) if env_path else DEFAULT_CONFIG_PATH)

    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = _read_toml(config_path)
    elif path is not None or env_path:
        # 显式指定了路径却不存在，属于错误；默认路径缺失则用内置默认值
        raise ConfigError(
            f"找不到配置文件: {config_path}",
            hint="确认路径正确，或去掉 --config 使用默认配置",
        )

    logging_raw = raw.setdefault("logging", {})
    env_level = os.getenv("OKXQUANT_LOG_LEVEL")
    if env_level:
        logging_raw["level"] = env_level

    network_raw = raw.setdefault("network", {})
    for env_name, field_name, caster in (
        ("OKX_PROXY", "proxy", str),
        ("OKX_TUNNEL_ENABLED", "tunnel_enabled", _as_bool),
        ("OKX_TUNNEL_HOST", "tunnel_host", str),
        ("OKX_TUNNEL_PORT", "tunnel_port", int),
        ("OKX_TUNNEL_SSH_PORT", "tunnel_ssh_port", int),
    ):
        value = os.getenv(env_name)
        if value is not None and value.strip() != "":
            network_raw[field_name] = caster(value.strip())

    try:
        return AppConfig.model_validate(raw)
    except ConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"配置校验失败:\n{exc}") from exc


class Credentials(BaseModel):
    """OKX API 凭据。flag 为 "1" 表示模拟盘，"0" 表示实盘。"""

    api_key: str
    api_secret: str
    passphrase: str
    flag: str = "1"

    @property
    def is_demo(self) -> bool:
        return self.flag != "0"

    @property
    def environment_label(self) -> str:
        return "SIMULATED TRADING" if self.is_demo else "LIVE TRADING"

    def masked(self) -> dict[str, str]:
        """用于日志/展示的脱敏版本。"""
        key = self.api_key
        masked_key = f"{key[:4]}***{key[-4:]}" if len(key) > 8 else "***"
        return {"api_key": masked_key, "passphrase": "***", "flag": self.flag}


def load_credentials(*, required: bool = False) -> Credentials | None:
    """从环境变量 / .env 读取凭据。

    缺失时返回 ``None``（只读模式），除非 ``required=True`` 则抛错。
    """
    load_dotenv(override=False)

    api_key = (os.getenv("OKX_API_KEY") or "").strip()
    api_secret = (os.getenv("OKX_API_SECRET") or "").strip()
    passphrase = (os.getenv("OKX_PASSPHRASE") or "").strip()
    flag = (os.getenv("OKX_FLAG") or "1").strip()

    missing = [
        name
        for name, value in (
            ("OKX_API_KEY", api_key),
            ("OKX_API_SECRET", api_secret),
            ("OKX_PASSPHRASE", passphrase),
        )
        if not value
    ]

    if missing:
        if required:
            raise ConfigError(
                "缺少 API 凭据: " + ", ".join(missing),
                hint="复制 .env.example 为 .env 并填写模拟盘密钥",
            )
        return None

    if flag not in {"0", "1"}:
        raise ConfigError(f"OKX_FLAG 只能是 0 或 1，当前为 {flag!r}")

    return Credentials(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        flag=flag,
    )
