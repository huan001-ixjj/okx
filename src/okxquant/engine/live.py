"""实时交易引擎。

数据来源是公有 WS（K 线 / ticker）+ 私有 WS（订单 / 账户），二者共用一个
``queue.Queue``；引擎主循环单线程消费，保证「策略执行 → 风控 → 下单 → 记账」
的时序确定。asyncio 只存在于 WS 桥接线程内。
"""

from __future__ import annotations

import queue
import signal
import sys
import threading
import time
from pathlib import Path

from loguru import logger
from rich.console import Console
from rich.live import Live

from okxquant.config import AppConfig, Credentials
from okxquant.domain import (
    Action,
    CancelAll,
    Candle,
    Fill,
    Instrument,
    Order,
)
from okxquant.engine.broker import Broker, reconcile_orders
from okxquant.engine.portfolio import Portfolio
from okxquant.engine.risk import RiskEngine
from okxquant.errors import StrategyError
from okxquant.exchange.models import as_float, parse_order
from okxquant.exchange.rest import OkxRest
from okxquant.exchange.ws import (
    WsBridge,
    WsEvent,
    private_subscriptions,
    public_subscriptions,
    ws_urls,
)
from okxquant.render.panel import LiveState, render_state
from okxquant.store import Database, runs, trading
from okxquant.strategy.base import BarCtx, Strategy
from okxquant.strategy.registry import reload_into

PANEL_INTERVAL_SEC = 0.5
COMPACT_INTERVAL_SEC = 60.0
RELOAD_CHECK_SEC = 2.0


class LiveEngine:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        credentials: Credentials | None,
        strategy: Strategy,
        strategy_spec: str,
        broker: Broker,
        portfolio: Portfolio,
        db: Database,
        run_id: int,
        risk: RiskEngine,
        instrument: Instrument,
        rest: OkxRest | None,
        dry_run: bool,
        console: Console | None = None,
    ) -> None:
        self.cfg = cfg
        self.credentials = credentials
        self.strategy = strategy
        self.strategy_spec = strategy_spec
        self.broker = broker
        self.portfolio = portfolio
        self.db = db
        self.run_id = run_id
        self.risk = risk
        self.instrument = instrument
        self.rest = rest
        self.dry_run = dry_run
        self.console = console or Console()

        self.state = LiveState(
            inst_id=cfg.market.inst_id,
            bar=cfg.market.bar,
            environment=credentials.environment_label if credentials else "OFFLINE",
            dry_run=dry_run,
            strategy_name=strategy.name,
        )

        self._queue: queue.Queue[WsEvent] = queue.Queue(maxsize=20_000)
        self._stop = threading.Event()
        self._reload_flag = threading.Event()
        self._public_ws: WsBridge | None = None
        self._private_ws: WsBridge | None = None
        self._live: Live | None = None
        self._last_bar_ts = 0
        self._fill_state: dict[str, tuple[float, float]] = {}
        self._sample_counter = 0
        self._last_panel = 0.0
        self._last_compact = 0.0
        self._last_reload_check = 0.0
        self._strategy_mtime = self._current_mtime()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        self._install_signals()
        self._print_banner()
        self._prime_market()
        self._sync_account()
        self._reconcile()
        self._start_ws()
        try:
            self._loop()
        finally:
            self._shutdown()

    def _install_signals(self) -> None:
        signal.signal(signal.SIGINT, self._on_stop_signal)
        signal.signal(signal.SIGTERM, self._on_stop_signal)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._on_reload_signal)

    def _on_stop_signal(self, signum: int, _frame: object) -> None:
        logger.info("收到退出信号 {}，正在停止…", signum)
        self._stop.set()

    def _on_reload_signal(self, _signum: int, _frame: object) -> None:
        logger.info("收到 SIGHUP，准备重载策略")
        self._reload_flag.set()

    def _print_banner(self) -> None:
        env = self.credentials.environment_label if self.credentials else "离线（无凭据）"
        mode = "DRY-RUN（只记录不下单）" if self.dry_run else "真实下单"
        self.console.print(
            f"[bold cyan]okxquant live[/] · {env} · {mode}\n"
            f"[dim]交易对 {self.cfg.market.inst_id} {self.cfg.market.bar} · "
            f"策略 {self.strategy.name} · Ctrl-C 退出 · kill -HUP 重载策略[/]"
        )
        if not self.dry_run and self.credentials and not self.credentials.is_demo:
            self.console.print("[bold red]⚠ 正在使用实盘账户，所有下单都会动用真实资金[/]")

    def _shutdown(self) -> None:
        self._stop.set()
        if self._live is not None:
            self._live.stop()
            self._live = None

        for bridge in (self._public_ws, self._private_ws):
            if bridge is not None:
                bridge.stop()

        self._flush_equity()

        metrics = self.portfolio.build_metrics(
            bars=self.state.bars_processed,
            first_price=self.portfolio.last_price,
            last_price=self.portfolio.last_price,
            periods_per_year=525_600.0,
        )
        status = "halted" if self.risk.halted else "finished"
        runs.update_window(self.db, self.run_id, metrics.start_ts, metrics.end_ts)
        runs.finish_run(
            self.db,
            self.run_id,
            metrics=metrics.to_dict(),
            final_equity=metrics.final_equity,
            status=status,
        )

        public_stats = self._public_ws.stats if self._public_ws else {}
        private_stats = self._private_ws.stats if self._private_ws else {}

        self.console.print()
        self.console.print(
            f"[bold]本轮结束[/] 成交 {len(self.portfolio.fills)} 笔 · "
            f"权益 {metrics.final_equity:,.2f} USDT "
            f"({metrics.total_return * 100:+.2f}%) · "
            f"手续费 {self.portfolio.fee_paid:.2f} · "
            f"策略重载 {self.state.reloads} 次"
        )
        self.console.print(
            f"[dim]行情：K 线 {self.state.bars_processed} 根 · "
            f"最新价 {self.state.last_price:,.2f} · "
            f"公有推送 {public_stats.get('messages', 0)} 条"
            f"（丢弃 {public_stats.get('dropped', 0)}） · "
            f"私有推送 {private_stats.get('messages', 0)} 条[/]"
        )
        if self.risk.halted:
            self.console.print(f"[bold red]熔断未解除：{self.risk.halt_reason}[/]")

    # ------------------------------------------------------------------ #
    # 启动准备
    # ------------------------------------------------------------------ #
    def _prime_market(self) -> None:
        """WS 只订阅 K 线（ticker 频道已不可用），启动时用 REST 补一次最新价。

        之后的价格由每根 K 线推送的收盘价持续更新，足够驱动面板与策略。
        """
        if self.rest is None:
            return
        try:
            ticker = self.rest.ticker(self.cfg.market.inst_id)
        except Exception as exc:
            logger.debug("启动时获取行情失败（不影响后续 K 线推送）：{}", exc)
            return

        if ticker.last > 0:
            self.state.last_price = ticker.last
            self.state.change_pct = ticker.change_pct_24h
            if self.portfolio.last_price <= 0:
                self.portfolio.last_price = ticker.last

    def _sync_account(self) -> None:
        """实盘模式下用交易所余额校准本地账本，避免带着旧状态下单。"""
        if self.rest is None or self.credentials is None or self.dry_run:
            return
        try:
            usdt = self.rest.available_balance("USDT")
            btc = self.rest.available_balance("BTC")
        except Exception as exc:
            logger.warning("读取账户余额失败，沿用本地账本：{}", exc)
            return

        self.portfolio.cash = usdt
        price = self.portfolio.last_price
        if price <= 0:
            try:
                price = self.rest.ticker(self.cfg.market.inst_id).last
            except Exception:
                price = 0.0
        self.portfolio.position.qty = btc
        # 交易所不返回持仓成本，用当前价近似，仅用于展示与止损参考
        self.portfolio.position.avg_cost = price if btc > 0 else 0.0
        logger.info("已按交易所余额校准：USDT {:.2f} / BTC {:.8f}", usdt, btc)

    def _reconcile(self) -> None:
        local_open = trading.open_orders(self.db, self.run_id)
        # 没有 REST 客户端就无法拉取交易所挂单，跳过对账
        if self.rest is None:
            return

        remote_open: list[Order] = []
        try:
            remote_open = self.broker.sync_open_orders(self.cfg.market.inst_id)
        except Exception as exc:
            logger.warning("拉取交易所挂单失败，跳过对账：{}", exc)
            return

        stale, updated, missing = reconcile_orders(local_open=local_open, remote_open=remote_open)
        for order in stale:
            order.status = order.status.STALE
            trading.save_order(self.db, self.run_id, order)
            self.state.push_event(f"[yellow]对账：订单标记为 stale[/] {order.cl_ord_id}")

        for order in [*updated, *missing]:
            trading.save_order(self.db, self.run_id, order)
            self._fill_state[order.cl_ord_id] = (order.filled_qty, order.fee)
            self.state.orders.insert(0, order)

        if stale or missing:
            logger.warning(
                "对账结果：本地 {} 笔，交易所 {} 笔，stale {} 笔，新增 {} 笔",
                len(local_open),
                len(remote_open),
                len(stale),
                len(missing),
            )

    def _start_ws(self) -> None:
        demo = self.credentials.is_demo if self.credentials else True
        pub_url, priv_url = ws_urls(demo=demo)
        inst_id = self.cfg.market.inst_id

        self._public_ws = WsBridge(
            url=pub_url,
            subscriptions=public_subscriptions(inst_id, self.cfg.market.bar),
            queue_obj=self._queue,
        )
        self._public_ws.start()

        if self.credentials is not None:
            self._private_ws = WsBridge(
                url=priv_url,
                subscriptions=private_subscriptions(),
                credentials=self.credentials,
                private=True,
                queue_obj=self._queue,
            )
            self._private_ws.start()

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=0.3)
            except queue.Empty:
                event = None

            if event is not None:
                try:
                    self._dispatch(event)
                except Exception:
                    logger.exception("处理行情事件失败")

            if self._reload_flag.is_set():
                self._reload_flag.clear()
                self._reload_strategy(force=True)

            self._maybe_auto_reload()
            self._tick_panel()

    def _dispatch(self, event: WsEvent) -> None:
        channel = event.channel
        if channel.startswith("candle"):
            for row in event.data:
                try:
                    candle = Candle.from_okx(row)
                except (TypeError, ValueError, IndexError):
                    continue
                if candle.confirm:
                    self._on_bar(candle)
                else:
                    self.state.last_price = candle.close
        elif channel == "ticker":
            for row in event.data:
                last = as_float(row.get("last"))
                if last > 0:
                    self.state.last_price = last
                    open_24h = as_float(row.get("open24h"))
                    if open_24h > 0:
                        self.state.change_pct = (last - open_24h) / open_24h
        elif channel == "orders":
            for row in event.data:
                self._on_order_update(row)
        elif channel == "account":
            self.state.push_event("[dim]账户信息更新[/]")

    def _on_bar(self, candle: Candle) -> None:
        if candle.ts <= self._last_bar_ts:
            return
        self._last_bar_ts = candle.ts
        self.state.bars_processed += 1
        self.state.last_price = candle.close

        snapshot = self.portfolio.mark(candle.close, candle.ts)
        self.state.snapshot = snapshot

        day_start = snapshot.equity - self.risk.day_pnl(snapshot.equity)
        self.state.day_pnl = self.risk.day_pnl(snapshot.equity)
        self.state.day_pnl_pct = (
            self.state.day_pnl / day_start if day_start > 0 else 0.0
        )

        if self.risk.update_equity(candle.ts, snapshot.equity):
            self.state.halted = True
            self.state.halt_reason = self.risk.halt_reason
            self.state.push_event(f"[bold red]熔断[/] {self.risk.halt_reason}")
            trading.record_risk_event(
                self.db,
                run_id=self.run_id,
                ts=candle.ts,
                rule="daily_loss",
                message=self.risk.halt_reason,
            )

        actions: list[Action] = self.strategy.on_bar(
            BarCtx(candle=candle, account=snapshot, bar_index=self.state.bars_processed)
        )
        if actions:
            self._execute(actions, candle)

        point = self.portfolio.record_bar(candle.ts, candle.close)
        self._sample_counter += 1
        if self._sample_counter >= self.cfg.render.equity_sample_bars:
            trading.save_equity_points(self.db, self.run_id, [point])
            self._sample_counter = 0

    def _execute(self, actions: list[Action], candle: Candle) -> None:
        for action in actions:
            if isinstance(action, CancelAll):
                cancelled = self.broker.cancel_all(self.cfg.market.inst_id)
                self.state.push_event(f"撤单 {cancelled} 笔（{action.reason}）")
                continue

            snapshot = self.portfolio.mark(candle.close, candle.ts)
            notional = (
                action.quote_qty
                if action.quote_qty is not None
                else (action.qty or 0.0) * (action.px or candle.close)
            )

            decision = self.risk.check_order(
                side=action.side,
                notional=notional,
                snapshot=snapshot,
                now_ms=candle.ts,
            )
            if not decision.allowed:
                self.state.push_event(f"[yellow]风控拒绝[/] {decision.reason}")
                logger.warning("风控拒绝下单：{}", decision.reason)
                continue

            result = self.broker.submit(
                inst_id=self.cfg.market.inst_id,
                intent=action,
                price=candle.close,
                snapshot=snapshot,
                now_ms=candle.ts,
            )
            if result.rejected:
                self.state.push_event(f"[red]下单失败[/] {result.error}")
                continue

            if result.order is not None:
                trading.save_order(self.db, self.run_id, result.order)
                self.state.orders.insert(0, result.order)
                self.state.orders = self.state.orders[:20]
                side_label = "买入" if result.order.side.sign > 0 else "卖出"
                self.state.push_event(
                    f"提交 {side_label} {result.order.qty:.6g} "
                    f"@ {result.order.px or candle.close:,.2f}"
                )

            if result.fill is not None:
                self.portfolio.apply_fill(result.fill)
                trading.save_fills(self.db, self.run_id, [result.fill])
                self.state.push_event(
                    f"[green]成交[/] {result.fill.qty:.6g} @ {result.fill.px:,.2f}"
                )

            self.risk.note_order(candle.ts, snapshot.equity)

    def _on_order_update(self, row: dict) -> None:
        order = parse_order(row)
        if not order.cl_ord_id:
            return

        prev_qty, prev_fee = self._fill_state.get(order.cl_ord_id, (0.0, 0.0))
        delta_qty = order.filled_qty - prev_qty
        if delta_qty > 1e-12 and (order.avg_px > 0 or (order.px or 0) > 0):
            delta_fee = max(0.0, order.fee - prev_fee)
            fill = Fill(
                ts=order.updated_ts or int(time.time() * 1000),
                side=order.side,
                qty=delta_qty,
                px=order.avg_px or order.px or 0.0,
                fee=delta_fee,
                cl_ord_id=order.cl_ord_id,
                ord_id=order.exchange_ord_id,
            )
            self.portfolio.apply_fill(fill)
            trading.save_fills(self.db, self.run_id, [fill])
            self.state.push_event(
                f"[green]成交回报[/] {delta_qty:.6g} @ {fill.px:,.2f} ({order.status.value})"
            )

        self._fill_state[order.cl_ord_id] = (order.filled_qty, order.fee)
        trading.save_order(self.db, self.run_id, order)

        for idx, existing in enumerate(self.state.orders):
            if existing.cl_ord_id == order.cl_ord_id:
                self.state.orders[idx] = order
                break
        else:
            self.state.orders.insert(0, order)
            self.state.orders = self.state.orders[:20]

    # ------------------------------------------------------------------ #
    # 策略热重载
    # ------------------------------------------------------------------ #
    def _current_mtime(self) -> float:
        path = Path(self.strategy_spec)
        if path.suffix != ".py" or not path.exists():
            return 0.0
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _maybe_auto_reload(self) -> None:
        now = time.monotonic()
        if now - self._last_reload_check < RELOAD_CHECK_SEC:
            return
        self._last_reload_check = now

        mtime = self._current_mtime()
        if mtime and mtime != self._strategy_mtime:
            self._reload_strategy(force=False)

    def _reload_strategy(self, *, force: bool) -> None:
        if not self.strategy_spec.endswith(".py"):
            if force:
                logger.info("内置策略无需热重载；修改参数请重启进程或使用 strategies/*.py")
                self.state.push_event("[dim]内置策略不支持热重载[/]")
            return

        try:
            new_strategy = reload_into(self.strategy, self.strategy_spec)
        except StrategyError as exc:
            logger.error("策略重载失败，继续使用旧版本：{}", exc)
            self.state.push_event(f"[red]重载失败[/] {exc}")
            return

        self.strategy = new_strategy
        self._strategy_mtime = self._current_mtime()
        self.state.reloads = new_strategy.reload_count
        self.state.strategy_name = new_strategy.name
        self.state.push_event(f"[green]策略已重载[/] 第 {new_strategy.reload_count} 次")

    # ------------------------------------------------------------------ #
    # 面板
    # ------------------------------------------------------------------ #
    def _tick_panel(self) -> None:
        now = time.monotonic()
        if now - self._last_panel < PANEL_INTERVAL_SEC:
            return
        self._last_panel = now

        self.state.connected_public = bool(self._public_ws and self._public_ws.connected)
        self.state.connected_private = bool(self._private_ws and self._private_ws.connected)

        if sys.stdout.isatty():
            renderable = render_state(self.state)
            if self._live is None:
                self._live = Live(
                    renderable,
                    console=self.console,
                    refresh_per_second=2,
                    transient=False,
                )
                self._live.start()
            else:
                self._live.update(renderable)
        elif now - self._last_compact >= COMPACT_INTERVAL_SEC:
            self._last_compact = now
            self.console.print(render_state(self.state, compact=True))

    def _flush_equity(self) -> None:
        if self.portfolio.last_price <= 0:
            return
        point = self.portfolio.record_bar(int(time.time() * 1000), self.portfolio.last_price)
        trading.save_equity_points(self.db, self.run_id, [point])


__all__ = ["LiveEngine"]
