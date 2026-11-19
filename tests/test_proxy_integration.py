"""真实网络连通性测试。

默认跳过；需要显式提供代理地址才会执行：

    OKX_IT_PROXY=socks5h://127.0.0.1:1080 uv run pytest -m integration

仅用于开发环境验证隧道是否真的绕过了 DNS 污染与端口封锁。
"""

from __future__ import annotations

import os

import pytest

from okxquant.exchange.rest import OkxRest

PROXY_ENV = "OKX_IT_PROXY"


@pytest.mark.integration
def test_public_time_reachable() -> None:
    proxy = os.getenv(PROXY_ENV, "").strip() or None
    if proxy is None:
        pytest.skip(f"未设置 {PROXY_ENV}，跳过真实网络测试")

    client = OkxRest(None, proxy=proxy, timeout=20.0, max_retries=1)
    try:
        server_time = client.server_time()
        instrument = client.instrument("BTC-USDT")
    finally:
        client.close()

    assert server_time > 0
    assert instrument.inst_id == "BTC-USDT"
    assert instrument.tick_sz > 0
