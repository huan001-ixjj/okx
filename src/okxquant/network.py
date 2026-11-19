"""网络出口策略：直连 / 显式代理 / SSH 隧道。

默认返回 ``None``（直连）。只有配置了 ``proxy`` 或开启了 ``tunnel_enabled``
才会返回代理地址；隧道模块用惰性导入，直连部署下不会加载。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from okxquant.config import NetworkConfig
from okxquant.errors import NetworkError

PROXY_SCHEME = "socks5h"
DEFAULT_TUNNEL_HOST = "127.0.0.1"


def build_proxy_url(port: int, *, host: str = DEFAULT_TUNNEL_HOST) -> str:
    """``socks5h`` 的 ``h`` 表示由代理解析域名，绕过本地 DNS 污染。"""
    return f"{PROXY_SCHEME}://{host}:{port}"


def proxy_from_config(cfg: NetworkConfig) -> str | None:
    """静态推导代理地址，不触发任何网络动作。"""
    explicit = cfg.proxy.strip()
    if explicit:
        return explicit
    if cfg.has_tunnel:
        return build_proxy_url(cfg.tunnel_port)
    return None


def resolve_proxy(
    cfg: NetworkConfig,
    *,
    autostart: bool = True,
    disable_tunnel: bool = False,
) -> str | None:
    """返回可用的代理地址；``None`` 表示直连。

    ``autostart`` 为真且配置了隧道时，会确保本地 SOCKS5 端口就绪。
    ``disable_tunnel`` 对应命令行 ``--no-tunnel``，只禁用隧道，不影响显式代理。
    """
    explicit = cfg.proxy.strip()

    if disable_tunnel or not cfg.has_tunnel:
        return explicit or None

    url = build_proxy_url(cfg.tunnel_port)
    if explicit:
        return explicit

    if not autostart:
        return url

    # 惰性导入：未启用隧道时不会触碰 subprocess / socket 相关逻辑
    from okxquant.tunnel import SshTunnel, TunnelError, TunnelSpec

    spec = TunnelSpec.from_config(cfg)
    try:
        status = SshTunnel(spec).ensure()
        logger.debug("代理隧道状态：{}", status.describe())
    except TunnelError as exc:
        raise NetworkError(
            f"代理隧道不可用：{exc.message}",
            hint=exc.hint or "检查 .env 中的 OKX_TUNNEL_* 配置",
        ) from exc

    logger.debug("使用代理 {}", url)
    return url


def proxy_status(cfg: NetworkConfig) -> dict[str, Any]:
    """供 ``info`` 展示，不建立任何连接。"""
    explicit = cfg.proxy.strip()
    if explicit:
        return {"mode": "proxy", "proxy": explicit, "detail": "使用显式代理"}
    if not cfg.has_tunnel:
        return {"mode": "direct", "proxy": None, "detail": "直连"}

    from okxquant.tunnel import SshTunnel, TunnelSpec

    status = SshTunnel(TunnelSpec.from_config(cfg)).status()
    return {
        "mode": "tunnel",
        "proxy": build_proxy_url(cfg.tunnel_port),
        "detail": status.describe(),
        "running": status.running,
        "owned": status.owned,
        "pid": status.pid,
        "port": cfg.tunnel_port,
    }


def describe_proxy(url: str | None) -> str:
    """日志/面板用的简短描述，避免反复拼接。"""
    if not url:
        return "直连"
    return url
