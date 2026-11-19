"""WebSocket 桥接层。

``python-okx`` 0.4.5 的 WS 客户端是 asyncio 模型（``WsPublicAsync`` /
``WsPrivateAsync``），而引擎主循环是同步的。这里的做法是：

    专属线程持有 asyncio event loop → 运行 SDK 的 async 客户端
      → 回调里把解析后的事件 put 进 ``queue.Queue``
        → 主线程同步消费，时序确定、无锁竞争

对外只暴露 ``queue`` 与 ``connected``，调用方完全不需要知道 asyncio 的存在。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import ssl
import threading
from dataclasses import dataclass, field
from typing import Any

import certifi
import websockets
from loguru import logger
from okx.websocket.WsPrivateAsync import WsPrivateAsync
from okx.websocket.WsPublicAsync import WsPublicAsync

from okxquant.config import Credentials
from okxquant.errors import CredentialError, NetworkError
from okxquant.exchange.rest import DEFAULT_DOMAIN

# 8443 端口已被 OKX 公告退役（notice 64010: migrate to port 443），统一用 443。
# 行情走 business 端点：K 线频道（candle*）归属 business，
# 在 public 端点上订阅会返回 60018（channel doesn't exist）。
PUBLIC_WS_URL = "wss://ws.okx.com/ws/v5/business"
PRIVATE_WS_URL = "wss://ws.okx.com/ws/v5/private"
DEMO_PUBLIC_WS_URL = "wss://wspap.okx.com/ws/v5/business"
DEMO_PRIVATE_WS_URL = "wss://wspap.okx.com/ws/v5/private"

RECONNECT_MIN_SEC = 1.0
RECONNECT_MAX_SEC = 30.0

WS_OPEN_TIMEOUT = 15.0
WS_PING_INTERVAL = 20.0
WS_PING_TIMEOUT = 20.0


@dataclass(slots=True, frozen=True)
class WsEvent:
    channel: str
    action: str
    data: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def ws_urls(*, demo: bool) -> tuple[str, str]:
    """返回 (行情地址, 私有地址)。模拟盘走 wspap 域名。"""
    if demo:
        return DEMO_PUBLIC_WS_URL, DEMO_PRIVATE_WS_URL
    return PUBLIC_WS_URL, PRIVATE_WS_URL


class WsBridge:
    """把 async WS 客户端适配成「同步队列 + 后台线程」。"""

    def __init__(
        self,
        *,
        url: str,
        subscriptions: list[dict[str, str]],
        credentials: Credentials | None = None,
        private: bool = False,
        debug: bool = False,
        queue_maxsize: int = 20_000,
        queue_obj: queue.Queue[WsEvent] | None = None,
    ) -> None:
        if private and credentials is None:
            raise CredentialError("私有 WebSocket 需要 API 凭据")
        self._url = url
        self._subscriptions = subscriptions
        self._creds = credentials
        self._private = private
        self._debug = debug

        # 允许公有/私有共用同一个队列，线程安全
        self.queue: queue.Queue[WsEvent] = queue_obj or queue.Queue(maxsize=queue_maxsize)
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._dropped = 0
        self._messages = 0

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name=f"okx-ws-{'private' if self._private else 'public'}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            # 关闭连接会让消费循环退出、事件循环随之结束，
            # 因此 is_running() 与 call_soon_threadsafe() 之间存在竞争，统一兜住。
            try:
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._close_client(), loop
                    ).result(timeout=timeout)
                    loop.call_soon_threadsafe(loop.stop)
            except (RuntimeError, TimeoutError):
                logger.debug("WS 事件循环已结束，跳过优雅停止")
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def stats(self) -> dict[str, int]:
        return {"messages": self._messages, "dropped": self._dropped}

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._run())
        except RuntimeError as exc:
            if "Event loop stopped" not in str(exc):
                logger.error("WebSocket 线程异常退出: {}", exc)
        except Exception:
            logger.exception("WebSocket 线程异常退出")
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            self._loop = None

    async def _run(self) -> None:
        backoff = RECONNECT_MIN_SEC
        while not self._stop.is_set():
            try:
                await self._connect_and_consume()
                backoff = RECONNECT_MIN_SEC
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._connected.clear()
                self._client = None
                if self._stop.is_set():
                    break
                logger.warning("WebSocket 断开：{}；{:.0f}s 后重连", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SEC)

    async def _connect_and_consume(self) -> None:
        """自己建连并驱动消费循环。

        不用 SDK 的 ``connect()/start()``：前者把 ``websockets.connect(url, ssl=ctx)``
        写死了，无法注入代理；后者在连接失败时仍会 ``create_task(consume())``，
        留下一个必然抛 TypeError 的孤儿任务。
        """
        client = self._build_client()
        self._client = client

        websocket = await self._open_websocket()
        # SDK 的 subscribe() 用 self.websocket 发送，stop() 走 factory.websocket 关闭，
        # 自建连接后两处都要回填，否则「能订阅但关不掉」。
        client.websocket = websocket
        factory = getattr(client, "factory", None)
        if factory is not None:
            factory.websocket = websocket

        await client.subscribe(self._subscriptions, self._on_message)
        self._connected.set()
        logger.info(
            "WebSocket 已连接（{}）：{}",
            "私有" if self._private else "公有",
            self._url,
        )

        try:
            async for message in client.websocket:
                self._on_message(message)
        finally:
            self._connected.clear()

    async def _open_websocket(self) -> Any:
        ssl_context = ssl.create_default_context()
        ssl_context.load_verify_locations(certifi.where())
        try:
            return await websockets.connect(
                self._url,
                ssl=ssl_context,
                # websockets 会默认读取 HTTPS_PROXY 等环境变量；显式传 None
                # 表示始终直连，避免被外部环境变量意外接管
                proxy=None,
                open_timeout=WS_OPEN_TIMEOUT,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
            )
        except Exception as exc:
            raise NetworkError(
                f"WebSocket 连接失败: {self._url} ({type(exc).__name__}: {exc})",
                hint="检查网络连通性与防火墙设置",
            ) from exc

    def _build_client(self) -> Any:
        if self._private:
            assert self._creds is not None
            return WsPrivateAsync(
                apiKey=self._creds.api_key,
                passphrase=self._creds.passphrase,
                secretKey=self._creds.api_secret,
                url=self._url,
                debug=self._debug,
            )
        return WsPublicAsync(self._url, debug=self._debug)

    async def _close_client(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.stop()
        except Exception:
            logger.debug("关闭 WS 连接时异常（可忽略）")

    def _on_message(self, message: str) -> None:
        """SDK 回调运行在 event loop 线程里，这里只做解析与入队。"""
        try:
            payload = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            logger.debug("忽略无法解析的 WS 消息")
            return

        event = payload.get("event")
        if event == "error":
            logger.warning("WS 返回错误：code={} msg={}", payload.get("code"), payload.get("msg"))
            return
        if event == "subscribe":
            logger.debug("WS 订阅确认：{}", payload.get("arg"))
            return
        if event == "login":
            logger.debug("WS 登录成功")
            return

        arg = payload.get("arg") or {}
        channel = str(arg.get("channel", ""))
        if not channel:
            return

        self._messages += 1
        if self._messages <= 10:
            data = payload.get("data") or []
            logger.debug(
                "WS 收到第 {} 条消息：channel={} frames={} 首帧={}",
                self._messages,
                channel,
                len(data),
                str(data[0])[:120] if data else "-",
            )

        item = WsEvent(
            channel=channel,
            action=str(payload.get("action", "")),
            data=payload.get("data") or [],
            raw=payload,
        )
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning(
                    "WS 队列已满，累计丢弃 {} 条（消费速度跟不上行情）", self._dropped
                )


def public_subscriptions(inst_id: str, bar: str) -> list[dict[str, str]]:
    """行情频道：只订阅 K 线。

    OKX 的 ``ticker`` / ``tickers`` 在 public 与 business 端点都返回 60018，
    而 K 线推送的收盘价足以驱动策略与面板，故不再单独订阅 ticker。
    24 小时涨跌幅改由 REST 一次性获取。
    """
    return [{"channel": f"candle{bar}", "instId": inst_id}]


def private_subscriptions() -> list[dict[str, str]]:
    """私有频道：订单与账户。

    ``orders`` 只接受 ``instType``（SPOT），传 ``instId`` 会返回 60018；
    ``account`` 不接收任何过滤参数。
    """
    return [
        {"channel": "orders", "instType": "SPOT"},
        {"channel": "account"},
    ]


__all__ = [
    "DEMO_PRIVATE_WS_URL",
    "DEMO_PUBLIC_WS_URL",
    "PRIVATE_WS_URL",
    "PUBLIC_WS_URL",
    "DEFAULT_DOMAIN",
    "WsBridge",
    "WsEvent",
    "private_subscriptions",
    "public_subscriptions",
    "ws_urls",
]
