"""CLI 运行期上下文，供各子命令共享。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from okxquant.config import AppConfig, Credentials
from okxquant.errors import OkxQuantError


@dataclass(slots=True)
class AppContext:
    config: AppConfig
    credentials: Credentials | None
    config_path: Path | None
    json_output: bool
    disable_tunnel: bool = False

    @property
    def is_offline(self) -> bool:
        """无凭据时只能做数据下载与回测。"""
        return self.credentials is None

    def ensure_proxy(self, *, autostart: bool = True) -> str | None:
        """按配置返回代理地址；未配置时返回 None（直连）。"""
        from okxquant.network import resolve_proxy

        return resolve_proxy(
            self.config.network,
            autostart=autostart,
            disable_tunnel=self.disable_tunnel,
        )


def get_app_ctx(ctx: typer.Context) -> AppContext:
    obj = ctx.obj
    if not isinstance(obj, AppContext):  # pragma: no cover - 仅在异常调用路径出现
        raise OkxQuantError("内部错误：CLI 上下文未初始化")
    return obj
