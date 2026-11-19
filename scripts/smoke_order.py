#!/usr/bin/env python3
"""模拟盘下单链路冒烟测试。

验证四件事：API 签名是否正确、余额能否读取、下单是否被接受、订单状态能否回查。
金额取交易所允许的最小值，成交后立刻反向平仓，不留持仓。

**仅用于模拟盘。** 若 ``OKX_FLAG=0``，脚本会直接退出。

    .venv/bin/python scripts/smoke_order.py --quote 15
"""

from __future__ import annotations

import argparse
import time
from decimal import ROUND_CEILING, Decimal

from rich.console import Console
from rich.table import Table

from okxquant.config import load_credentials
from okxquant.domain import OrderType, Side, normalize_qty, qty_str
from okxquant.errors import OkxQuantError
from okxquant.exchange.rest import OkxRest

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟盘下单链路冒烟测试")
    parser.add_argument("--inst", default="BTC-USDT", help="交易对")
    parser.add_argument(
        "--quote", type=float, default=15.0, help="下单名义金额（USDT），尽量小"
    )
    args = parser.parse_args()

    creds = load_credentials(required=True)
    assert creds is not None
    if not creds.is_demo:
        console.print("[bold red]拒绝执行[/] 当前是实盘凭据（OKX_FLAG=0），本脚本只用于模拟盘")
        return 2

    console.print(f"[bold]下单冒烟测试[/] {args.inst} · 名义金额约 {args.quote} USDT")

    with OkxRest(creds, timeout=15.0, max_retries=1) as rest:
        table = Table(box=None, pad_edge=False)
        table.add_column("步骤")
        table.add_column("结果")

        try:
            instrument = rest.instrument(args.inst)
            table.add_row("交易对精度", f"tick={instrument.tick_sz} lot={instrument.lot_sz} "
                                     f"min={instrument.min_sz}")

            usdt = rest.available_balance("USDT")
            table.add_row("USDT 可用", f"{usdt:,.2f}")

            price = rest.ticker(args.inst).last
            if price <= 0:
                raise OkxQuantError("取不到最新价")
            table.add_row("最新价", f"{price:,.2f}")

            raw_qty = Decimal(str(args.quote)) / Decimal(str(price))
            # 向上取整到最小下单量，保证金额达标
            qty = normalize_qty(float(raw_qty), instrument)
            if qty < instrument.min_sz:
                qty = (raw_qty / instrument.min_sz).to_integral_value(
                    rounding=ROUND_CEILING
                ) * instrument.min_sz
            notional = qty * Decimal(str(price))
            table.add_row("下单数量", f"{qty_str(qty)}（约 {notional:,.2f} USDT）")

            buy = rest.place_order(
                inst_id=args.inst,
                side=Side.BUY,
                ord_type=OrderType.MARKET,
                size=qty_str(qty),
                cl_ord_id=f"smoke{int(time.time())}",
            )
            table.add_row(
                "市价买入",
                f"[green]已提交[/] ordId={buy.exchange_ord_id or '-'} "
                f"clOrdId={buy.cl_ord_id}",
            )

            time.sleep(2.0)
            detail = rest.order_detail(inst_id=args.inst, cl_ord_id=buy.cl_ord_id)
            if detail is None:
                table.add_row("查询订单", "[yellow]查不到，可能尚未落账[/]")
            else:
                table.add_row(
                    "查询订单",
                    f"status={detail.status.value} 成交={qty_str(detail.filled_qty)} "
                    f"均价={detail.avg_px:,.2f}",
                )

                if detail.filled_qty > 0:
                    sell = rest.place_order(
                        inst_id=args.inst,
                        side=Side.SELL,
                        ord_type=OrderType.MARKET,
                        size=qty_str(detail.filled_qty),
                        cl_ord_id=f"smkout{int(time.time())}",
                    )
                    table.add_row("市价平仓", f"[green]已提交[/] ordId={sell.exchange_ord_id}")

                    time.sleep(2.0)
                    after = rest.available_balance("USDT")
                    table.add_row("平仓后 USDT", f"{after:,.2f}（差额 {after - usdt:+.4f}）")
                else:
                    table.add_row("平仓", "[yellow]未成交，跳过[/]")

        except OkxQuantError as exc:
            table.add_row("[red]失败[/]", str(exc))
            console.print(table)
            return 1

        console.print(table)

    console.print("[dim]说明：成交与手续费由模拟盘真实撮合产生，差额含手续费与滑点[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
