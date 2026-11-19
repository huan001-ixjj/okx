"""统一异常体系。

所有对外抛出的异常都继承自 :class:`OkxQuantError`，CLI 层据此决定退出码与提示文案。
"""

from __future__ import annotations

from typing import Any

# OKX 业务错误码中属于「限频/临时故障」的部分，可安全重试
RETRYABLE_OKX_CODES = {"429", "50011", "50013", "50026"}


class OkxQuantError(Exception):
    """项目内所有异常的基类。"""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n提示: {self.hint}"
        return self.message


class ConfigError(OkxQuantError):
    """配置缺失或非法。"""

    exit_code = 2


class CredentialError(ConfigError):
    """缺少 API 凭据或凭据不适用于当前环境。"""


class LiveTradingRefused(ConfigError):
    """请求实盘交易但未通过双重确认。"""

    exit_code = 2


class ApiError(OkxQuantError):
    """交易所返回业务错误。"""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        endpoint: str | None = None,
        payload: Any = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.code = code
        self.endpoint = endpoint
        self.payload = payload

    @property
    def retryable(self) -> bool:
        return self.code is not None and str(self.code) in RETRYABLE_OKX_CODES


class NetworkError(OkxQuantError):
    """网络层失败（超时、连接重置、DNS 等）。"""


class StoreError(OkxQuantError):
    """本地数据库读写失败。"""


class StrategyError(OkxQuantError):
    """策略加载、校验或执行失败。"""


class RiskHalt(OkxQuantError):
    """风控触发熔断，交易应停止。"""

    def __init__(self, message: str, *, rule: str, trigger_value: float | None = None) -> None:
        super().__init__(message)
        self.rule = rule
        self.trigger_value = trigger_value


def okx_error(
    *,
    code: str | None,
    message: str,
    endpoint: str | None = None,
    payload: Any = None,
) -> ApiError:
    """把 OKX 的错误码与消息包装成 :class:`ApiError`，并附带排查提示。"""
    hint = None
    code_str = str(code) if code is not None else None
    if code_str == "50113":
        hint = "API Key 无效，请确认是在「模拟交易」环境创建的密钥"
    elif code_str == "50111":
        hint = "Passphrase 不正确"
    elif code_str == "51000":
        hint = "参数不合法，检查 instId / side / sz 等字段"
    elif code_str == "51008":
        hint = "账户余额不足"
    elif code_str in {"50011", "429"}:
        hint = "触发限频，程序会自动退避重试"
    elif code_str == "51603":
        hint = "订单不存在或已结束"
    return ApiError(message, code=code_str, endpoint=endpoint, payload=payload, hint=hint)
