"""`live` 子命令：启动模拟盘自动交易。"""

from __future__ import annotations

import contextlib
from decimal import Decimal

import typer
from rich.console import Console

from okxquant.commands._common import load_strategy_with_params, parse_overrides
from okxquant.config import AppConfig
from okxquant.context import AppContext, get_app_ctx
from okxquant.domain import Instrument
from okxquant.engine.broker import DemoBroker, SimulatedBroker
from okxquant.engine.live import LiveEngine
from okxquant.engine.portfolio import Portfolio
from okxquant.engine.risk import RiskEngine, RiskLimits
from okxquant.errors import CredentialError, LiveTradingRefused
from okxquant.exchange.rest import OkxRest
from okxquant.store import Database, runs

console = Console()


def register(app: typer.Typer) -> None:
    app.command("live")(live_command)


def _default_instrument(inst_id: str) -> Instrument:
    return Instrument(
        inst_id=inst_id,
        base_ccy="BTC",
        quote_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.00000001"),
        min_sz=Decimal("0.00001"),
    )


def _guard_environment(
    app_ctx: AppContext, *, dry_run: bool, accept_real_money: bool
) -> None:
    creds = app_ctx.credentials
    if creds is None:
        if dry_run:
            return
        raise CredentialError(
            "需要 API 凭据才能下单",
            hint="配置 .env 后重试，或加 --dry-run 只观察信号（不下单）",
        )

    if creds.is_demo:
        return

    cfg: AppConfig = app_ctx.config
    if not cfg.risk.allow_live:
        raise LiveTradingRefused(
            "配置文件禁止实盘交易（risk.allow_live = false）",
            hint="如确实要实盘，请在配置中显式打开 allow_live 并加 --i-accept-real-money",
        )
    if not accept_real_money:
        raise LiveTradingRefused(
            "检测到实盘 API Key（OKX_FLAG=0），缺少确认参数",
            hint="确认风险后加 --i-accept-real-money 才会真正下单",
        )


def live_command(
    ctx: typer.Context,
    strategy: str | None = typer.Option(
        None, "--strategy", "-s", help="内置策略名或 .py 策略路径"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只跑信号与记账，不向交易所下单（建议首次使用）"
    ),
    accept_real_money: bool = typer.Option(
        False, "--i-accept-real-money", help="确认使用真实资金（仅 OKX_FLAG=0 时需要）"
    ),
    cash: float | None = typer.Option(None, "--cash", help="本地账本初始资金 USDT"),
    param: list[str] | None = typer.Option(
        None, "--param", "-p", help="覆盖策略参数，如 --param fast=8"
    ),
) -> None:
    """连接 OKX 模拟盘，实时订阅行情并自动交易。"""
    app_ctx = get_app_ctx(ctx)
    cfg = app_ctx.config
    _guard_environment(app_ctx, dry_run=dry_run, accept_real_money=accept_real_money)

    spec = strategy or cfg.strategy.name
    strategy_impl = load_strategy_with_params(cfg, spec, parse_overrides(param))
    initial_cash = cash if cash is not None else cfg.execution.initial_cash

    # 纯离线 dry-run（无凭据）不需要任何 REST 调用，直接靠 WS 行情驱动；
    # 有凭据时要拉精度、余额与挂单，用于下单和对账。
    need_network = app_ctx.credentials is not None or not dry_run

    rest: OkxRest | None = None
    instrument = _default_instrument(cfg.market.inst_id)
    portfolio = Portfolio(cfg.market.inst_id, initial_cash)

    if need_network:
        console.print("[dim]正在初始化交易所连接与交易对精度…[/]")
        try:
            rest = OkxRest(app_ctx.credentials, timeout=5.0, max_retries=1)
        except Exception as exc:  # 仅影响精度与对账，不阻断启动
            console.print(f"[yellow]初始化 REST 客户端失败：{exc}[/]")

        if rest is not None:
            try:
                instrument = rest.instrument(cfg.market.inst_id)
            except Exception as exc:
                console.print(f"[yellow]获取交易对精度失败，使用默认值：{exc}[/]")
            with contextlib.suppress(Exception):
                portfolio.last_price = rest.ticker(cfg.market.inst_id).last

    if dry_run or app_ctx.credentials is None:
        broker = SimulatedBroker(
            portfolio,
            fee_rate=cfg.execution.fee_rate,
            slippage_bps=cfg.execution.slippage_bps,
            dry_run=True,
        )
    else:
        if rest is None:
            raise CredentialError("无法初始化交易所客户端")
        broker = DemoBroker(rest, instrument, min_notional=cfg.risk.min_order_notional)

    risk = RiskEngine(RiskLimits.from_config(cfg.risk))

    db = Database(cfg.storage.db_path)
    run_id = runs.create_run(
        db,
        kind="live",
        strategy=strategy_impl.name,
        inst_id=cfg.market.inst_id,
        bar=cfg.market.bar,
        start_ts=0,
        end_ts=0,
        params=strategy_impl.params,
        initial_equity=initial_cash,
    )

    engine = LiveEngine(
        cfg=cfg,
        credentials=app_ctx.credentials,
        strategy=strategy_impl,
        strategy_spec=spec,
        broker=broker,
        portfolio=portfolio,
        db=db,
        run_id=run_id,
        risk=risk,
        instrument=instrument,
        rest=rest,
        dry_run=dry_run or app_ctx.credentials is None,
        console=console,
    )

    try:
        engine.run()
    finally:
        if rest is not None:
            rest.close()
        db.close()
