"""代理策略层测试。"""

from __future__ import annotations

import socket

import pytest

from okxquant.config import NetworkConfig
from okxquant.network import (
    build_proxy_url,
    proxy_from_config,
    proxy_status,
    resolve_proxy,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestBuildProxyUrl:
    def test_uses_socks5h(self) -> None:
        assert build_proxy_url(1080) == "socks5h://127.0.0.1:1080"

    def test_custom_host(self) -> None:
        assert build_proxy_url(1081, host="10.0.0.2") == "socks5h://10.0.0.2:1081"


class TestProxyFromConfig:
    def test_default_is_direct(self) -> None:
        assert proxy_from_config(NetworkConfig()) is None

    def test_explicit_proxy_wins(self) -> None:
        cfg = NetworkConfig(
            proxy="socks5h://192.168.1.5:7890",
            tunnel_enabled=True,
            tunnel_host="u@h",
        )
        assert proxy_from_config(cfg) == "socks5h://192.168.1.5:7890"

    def test_tunnel_derives_proxy(self) -> None:
        cfg = NetworkConfig(tunnel_enabled=True, tunnel_host="u@h", tunnel_port=1082)
        assert proxy_from_config(cfg) == "socks5h://127.0.0.1:1082"

    def test_tunnel_without_host_is_direct(self) -> None:
        cfg = NetworkConfig(tunnel_enabled=True)
        assert proxy_from_config(cfg) is None


class TestResolveProxy:
    def test_direct_when_nothing_configured(self) -> None:
        assert resolve_proxy(NetworkConfig()) is None

    def test_disable_tunnel_without_explicit_proxy_is_direct(self) -> None:
        cfg = NetworkConfig(tunnel_enabled=True, tunnel_host="u@h")
        assert resolve_proxy(cfg, disable_tunnel=True) is None

    def test_disable_tunnel_keeps_explicit_proxy(self) -> None:
        cfg = NetworkConfig(proxy="socks5h://x:1", tunnel_enabled=True, tunnel_host="u@h")
        assert resolve_proxy(cfg, disable_tunnel=True) == "socks5h://x:1"

    def test_autostart_false_does_not_start_tunnel(self) -> None:
        cfg = NetworkConfig(tunnel_enabled=True, tunnel_host="u@h", tunnel_port=free_port())
        # 端口未监听，但 autostart=False 时不应尝试建连
        assert resolve_proxy(cfg, autostart=False) == f"socks5h://127.0.0.1:{cfg.tunnel_port}"

    def test_explicit_proxy_skips_tunnel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("有显式代理时不应启动隧道")

        monkeypatch.setattr("okxquant.tunnel.SshTunnel.ensure", boom)
        cfg = NetworkConfig(proxy="socks5h://x:1", tunnel_enabled=True, tunnel_host="u@h")
        assert resolve_proxy(cfg) == "socks5h://x:1"


class TestProxyStatus:
    def test_direct(self) -> None:
        status = proxy_status(NetworkConfig())
        assert status["mode"] == "direct"
        assert status["proxy"] is None

    def test_explicit_proxy(self) -> None:
        status = proxy_status(NetworkConfig(proxy="socks5h://x:1"))
        assert status["mode"] == "proxy"
        assert status["proxy"] == "socks5h://x:1"

    def test_tunnel_not_running(self) -> None:
        cfg = NetworkConfig(tunnel_enabled=True, tunnel_host="u@h", tunnel_port=free_port())
        status = proxy_status(cfg)
        assert status["mode"] == "tunnel"
        assert status["running"] is False
        assert status["port"] == cfg.tunnel_port


class TestRestProxyInjection:
    def test_all_clients_receive_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from okxquant.exchange import rest

        captured: dict[str, dict] = {}

        class FakeClient:
            def __init__(self, name: str, **kwargs: object) -> None:
                captured[name] = dict(kwargs)
                self.timeout = None

            def close(self) -> None:
                pass

        monkeypatch.setattr(rest.MarketData, "MarketAPI", lambda **kw: FakeClient("market", **kw))
        monkeypatch.setattr(rest.PublicData, "PublicAPI", lambda **kw: FakeClient("public", **kw))
        monkeypatch.setattr(rest.Account, "AccountAPI", lambda **kw: FakeClient("account", **kw))
        monkeypatch.setattr(rest.Trade, "TradeAPI", lambda **kw: FakeClient("trade", **kw))

        client = rest.OkxRest(None, proxy="socks5h://127.0.0.1:1080")
        client.close()

        assert set(captured) == {"market", "public", "account", "trade"}
        for kwargs in captured.values():
            assert kwargs["proxy"] == "socks5h://127.0.0.1:1080"

    def test_no_proxy_passes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from okxquant.exchange import rest

        captured: list[dict] = []

        class FakeClient:
            def __init__(self, **kwargs: object) -> None:
                captured.append(dict(kwargs))
                self.timeout = None

            def close(self) -> None:
                pass

        monkeypatch.setattr(rest.MarketData, "MarketAPI", lambda **kw: FakeClient(**kw))
        monkeypatch.setattr(rest.PublicData, "PublicAPI", lambda **kw: FakeClient(**kw))
        monkeypatch.setattr(rest.Account, "AccountAPI", lambda **kw: FakeClient(**kw))
        monkeypatch.setattr(rest.Trade, "TradeAPI", lambda **kw: FakeClient(**kw))

        rest.OkxRest(None).close()
        assert all(item["proxy"] is None for item in captured)


class TestWsProxy:
    def test_bridge_stores_proxy(self) -> None:
        from okxquant.exchange.ws import WsBridge

        bridge = WsBridge(
            url="wss://example.invalid/ws",
            subscriptions=[],
            proxy="socks5h://127.0.0.1:1080",
        )
        assert bridge._proxy == "socks5h://127.0.0.1:1080"

    def test_bridge_defaults_to_direct(self) -> None:
        from okxquant.exchange.ws import WsBridge

        bridge = WsBridge(url="wss://example.invalid/ws", subscriptions=[])
        assert bridge._proxy is None
