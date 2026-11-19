"""OKX REST 薄封装。

所有 SDK 调用收敛在这里，便于统一处理：
- 模拟盘 flag 注入（SDK 内部通过 ``x-simulated-trading`` 头实现）
- 令牌桶限频 + 429/50011 指数退避
- 业务错误码 → :class:`ApiError`
- K 线分页与升序归一化
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
from loguru import logger
from okx import Account, MarketData, PublicData, Trade

from okxquant.config import Credentials
from okxquant.domain import (
    Candle,
    Fill,
    Instrument,
    Order,
    OrderStatus,
    OrderType,
    Side,
    Ticker,
)
from okxquant.errors import ApiError, CredentialError, NetworkError, okx_error
from okxquant.exchange.models import (
    as_float,
    parse_balance,
    parse_candles,
    parse_instrument,
    parse_order,
    parse_ticker,
)

DEFAULT_DOMAIN = "https://www.okx.com"
NO_AUTH = "-1"

# OKX /market/history-candles 限频约 20 次 / 2 秒
CANDLE_RATE = 20.0
CANDLE_PERIOD = 2.0


class RateLimiter:
    """简单令牌桶：按固定间隔放行，线程安全。"""

    def __init__(self, rate: float, period: float) -> None:
        if rate <= 0:
            raise ValueError("rate 必须为正数")
        self._interval = period / rate
        self._next_at = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._interval


class OkxRest:
    """OKX v5 REST 客户端。

    ``credentials`` 为 ``None`` 时只能调用公共接口（行情、精度、服务器时间），
    任何私有接口都会抛 :class:`CredentialError`。
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        *,
        domain: str = DEFAULT_DOMAIN,
        debug: bool = False,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self._creds = credentials
        self._debug = debug
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._limiter = RateLimiter(CANDLE_RATE, CANDLE_PERIOD)

        if credentials is not None:
            key = credentials.api_key
            secret = credentials.api_secret
            phrase = credentials.passphrase
            self._flag = credentials.flag
        else:
            key = secret = phrase = NO_AUTH
            self._flag = "1"

        common = {
            "api_key": key,
            "api_secret_key": secret,
            "passphrase": phrase,
            "flag": self._flag,
            "domain": domain,
            "debug": debug,
        }
        self._market = MarketData.MarketAPI(**common)
        self._public = PublicData.PublicAPI(**common)
        self._account = Account.AccountAPI(**common)
        self._trade = Trade.TradeAPI(**common)

        self._apply_timeout()

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _apply_timeout(self) -> None:
        timeout = httpx.Timeout(self._timeout)
        for client in (self._market, self._public, self._account, self._trade):
            try:
                client.timeout = timeout
            except Exception:  # pragma: no cover - httpx 版本差异
                logger.debug("无法设置 httpx 超时，使用 SDK 默认值")

    @property
    def is_demo(self) -> bool:
        return self._flag != "0"

    @property
    def has_credentials(self) -> bool:
        return self._creds is not None

    def _ensure_private(self) -> None:
        if self._creds is None:
            raise CredentialError(
                "该操作需要 API 凭据",
                hint="复制 .env.example 为 .env 并填写模拟盘密钥",
            )

    def _call(
        self,
        fn: Any,
        *args: Any,
        endpoint: str,
        throttle: bool = True,
        retries: int | None = None,
        **kwargs: Any,
    ) -> dict:
        """调用 SDK 方法并统一处理重试与错误码。

        ``retries`` 可覆盖全局重试次数，启动探测类请求（精度、行情）传 1 次即可，
        避免网络不通时把启动卡住几十秒。
        """
        if throttle:
            self._limiter.acquire()

        max_attempts = max(1, retries if retries is not None else self._max_retries)
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                resp = fn(*args, **kwargs)
            except Exception as exc:  # httpx 网络异常等
                last_error = exc
                if attempt + 1 >= max_attempts:
                    raise NetworkError(
                        f"{endpoint} 请求失败: {type(exc).__name__}: {exc}",
                        hint="检查网络连通性与系统代理设置",
                    ) from exc
                sleep_for = 0.5 * (2**attempt)
                logger.warning("{} 网络异常，{}s 后重试：{}", endpoint, sleep_for, exc)
                time.sleep(sleep_for)
                continue

            if not isinstance(resp, dict):
                raise ApiError(f"{endpoint} 返回了非预期结构", endpoint=endpoint, payload=resp)

            code = str(resp.get("code", ""))
            if code == "0":
                return resp

            error = okx_error(
                code=code,
                message=str(resp.get("msg") or "未知错误"),
                endpoint=endpoint,
                payload=resp,
            )
            if error.retryable and attempt + 1 < max_attempts:
                sleep_for = 0.5 * (2**attempt)
                logger.warning("{} 触发限频（code={}），{}s 后重试", endpoint, code, sleep_for)
                time.sleep(sleep_for)
                continue
            raise error

        raise NetworkError(f"{endpoint} 重试次数耗尽") from last_error

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #
    def server_time(self) -> int:
        resp = self._call(
            self._public.get_system_time,
            endpoint="/api/v5/public/time",
            throttle=False,
            retries=1,
        )
        data = resp.get("data") or [{}]
        return int(as_float(data[0].get("ts"), default=0.0))

    def clock_offset_ms(self) -> int:
        """本地时钟与交易所的偏移（毫秒），正数表示本地偏慢。"""
        server = self.server_time()
        if server <= 0:
            return 0
        local = int(time.time() * 1000)
        return server - local

    def instrument(self, inst_id: str) -> Instrument:
        resp = self._call(
            self._public.get_instruments,
            "SPOT",
            instId=inst_id,
            endpoint="/api/v5/public/instruments",
            throttle=False,
            retries=1,
        )
        data = resp.get("data") or []
        if not data:
            raise ApiError(f"找不到交易对 {inst_id}", endpoint="/api/v5/public/instruments")
        return parse_instrument(data[0])

    def ticker(self, inst_id: str) -> Ticker:
        resp = self._call(
            self._market.get_ticker,
            instId=inst_id,
            endpoint="/api/v5/market/ticker",
            throttle=False,
            retries=1,
        )
        data = resp.get("data") or []
        if not data:
            raise ApiError(f"取不到 {inst_id} 行情", endpoint="/api/v5/market/ticker")
        return parse_ticker(data[0])

    def candles(
        self,
        *,
        inst_id: str,
        bar: str,
        after: int | None = None,
        before: int | None = None,
        limit: int = 100,
        history: bool = True,
    ) -> list[Candle]:
        """取单页 K 线，返回按时间升序的列表。

        ``after`` 表示取早于该时间戳的数据（向过去翻页），``before`` 表示取更晚的数据。
        """
        fn = self._market.get_history_candlesticks if history else self._market.get_candlesticks
        endpoint = (
            "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        )
        resp = self._call(
            fn,
            instId=inst_id,
            bar=bar,
            after=str(after) if after is not None else "",
            before=str(before) if before is not None else "",
            limit=str(limit),
            endpoint=endpoint,
        )
        return parse_candles(resp.get("data") or [])

    def iter_candles(
        self,
        *,
        inst_id: str,
        bar: str,
        start_ms: int,
        end_ms: int,
        page_limit: int = 100,
    ) -> Iterator[list[Candle]]:
        """按区间向过去分页拉取，逐页 yield 升序 K 线。

        每页 100 条是 OKX 上限；调用方负责落库与进度展示。
        """
        if start_ms >= end_ms:
            return

        # OKX 的 after 为排他上界（ts < after），加 1 才能包含 end_ms 本身
        cursor = end_ms + 1
        last_oldest: int | None = None
        while True:
            batch = self.candles(
                inst_id=inst_id,
                bar=bar,
                after=cursor,
                limit=page_limit,
                history=True,
            )
            if not batch:
                return

            window = [c for c in batch if c.ts >= start_ms]
            if window:
                yield window

            oldest = batch[0].ts
            if oldest <= start_ms:
                return
            if last_oldest is not None and oldest >= last_oldest:
                logger.warning("K 线分页没有前进（{}），提前结束以免死循环", oldest)
                return
            last_oldest = oldest
            cursor = oldest

    # ------------------------------------------------------------------ #
    # 私有接口
    # ------------------------------------------------------------------ #
    def available_balance(self, ccy: str = "USDT") -> float:
        self._ensure_private()
        resp = self._call(
            self._account.get_account_balance,
            endpoint="/api/v5/account/balance",
            throttle=False,
        )
        avail, _ = parse_balance(resp.get("data") or [], ccy)
        return avail

    def place_order(
        self,
        *,
        inst_id: str,
        side: Side,
        ord_type: OrderType,
        size: str,
        px: str | None = None,
        cl_ord_id: str = "",
        td_mode: str = "cash",
    ) -> Order:
        self._ensure_private()
        params: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side.value,
            "ordType": ord_type.value,
            "sz": size,
            "clOrdId": cl_ord_id,
        }
        if px is not None:
            params["px"] = px

        resp = self._call(self._trade.place_order, endpoint="/api/v5/trade/order", **params)
        data = (resp.get("data") or [{}])[0]
        s_code = str(data.get("sCode", "0"))
        if s_code not in {"0", ""}:
            raise okx_error(
                code=s_code,
                message=str(data.get("sMsg") or "下单被拒绝"),
                endpoint="/api/v5/trade/order",
                payload=data,
            )

        return Order(
            cl_ord_id=str(data.get("clOrdId") or cl_ord_id),
            inst_id=inst_id,
            side=side,
            ord_type=ord_type,
            qty=float(size),
            px=float(px) if px else None,
            status=_status_from_place(s_code),
            exchange_ord_id=str(data.get("ordId", "")),
        )

    def cancel_order(self, *, inst_id: str, ord_id: str = "", cl_ord_id: str = "") -> str:
        self._ensure_private()
        resp = self._call(
            self._trade.cancel_order,
            instId=inst_id,
            ordId=ord_id,
            clOrdId=cl_ord_id,
            endpoint="/api/v5/trade/cancel-order",
            throttle=False,
        )
        data = (resp.get("data") or [{}])[0]
        s_code = str(data.get("sCode", "0"))
        if s_code not in {"0", ""}:
            raise okx_error(
                code=s_code,
                message=str(data.get("sMsg") or "撤单失败"),
                endpoint="/api/v5/trade/cancel-order",
                payload=data,
            )
        return str(data.get("ordId", ord_id))

    def pending_orders(self, inst_id: str) -> list[Order]:
        self._ensure_private()
        resp = self._call(
            self._trade.get_order_list,
            instType="SPOT",
            instId=inst_id,
            state="live",
            limit="100",
            endpoint="/api/v5/trade/orders-pending",
            throttle=False,
        )
        return [parse_order(row, inst_id=inst_id) for row in resp.get("data") or []]

    def recent_fills(self, inst_id: str, limit: int = 100) -> list[Fill]:
        self._ensure_private()
        resp = self._call(
            self._trade.get_fills,
            instType="SPOT",
            instId=inst_id,
            limit=str(limit),
            endpoint="/api/v5/trade/fills",
            throttle=False,
        )
        fills: list[Fill] = []
        for row in resp.get("data") or []:
            side = Side.BUY if str(row.get("side", "")).lower() == "buy" else Side.SELL
            fills.append(
                Fill(
                    ts=int(as_float(row.get("ts"))),
                    side=side,
                    qty=as_float(row.get("fillSz")),
                    px=as_float(row.get("fillPx")),
                    fee=abs(as_float(row.get("fee"))),
                    cl_ord_id=str(row.get("clOrdId", "")),
                    ord_id=str(row.get("ordId", "")),
                )
            )
        fills.sort(key=lambda f: f.ts)
        return fills

    def order_detail(self, *, inst_id: str, ord_id: str = "", cl_ord_id: str = "") -> Order | None:
        self._ensure_private()
        resp = self._call(
            self._trade.get_order,
            instId=inst_id,
            ordId=ord_id,
            clOrdId=cl_ord_id,
            endpoint="/api/v5/trade/order",
            throttle=False,
        )
        data = resp.get("data") or []
        if not data:
            return None
        return parse_order(data[0], inst_id=inst_id)

    def close(self) -> None:
        for client in (self._market, self._public, self._account, self._trade):
            with contextlib.suppress(Exception):
                client.close()

    def __enter__(self) -> OkxRest:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _status_from_place(s_code: str) -> OrderStatus:
    return OrderStatus.LIVE if s_code == "0" else OrderStatus.FAILED
