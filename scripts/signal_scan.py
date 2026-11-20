"""Step 0：只统计信号，不撮合、不记账。

目的：在写任何回测结论之前，先回答「这个策略一年到底有多少笔信号」。
策略始终被当作空仓（不模拟持仓、不模拟冷却与熔断），因此得到的是**理论上限**，
真实可成交笔数只会更少。

用法（在项目根目录）：

    python scripts/signal_scan.py
    python scripts/signal_scan.py --start 2023-11-19 --fee 0.0005 --slip 2
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.trend_pullback import TrendPullbackStrategy  # noqa: E402

from okxquant.config import bar_to_ms, load_config  # noqa: E402
from okxquant.domain import AccountSnapshot  # noqa: E402
from okxquant.store import Database  # noqa: E402
from okxquant.store.candles import iter_candles, range_stats  # noqa: E402
from okxquant.strategy import BarCtx  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="趋势回调策略信号计数")
    parser.add_argument("--inst", default="XRP-USDT-SWAP")
    parser.add_argument("--bar", default="5m")
    parser.add_argument("--start", default=None, help="如 2023-11-19")
    parser.add_argument("--end", default=None)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--fee", type=float, default=0.0005, help="单边吃单费率")
    parser.add_argument("--slip", type=float, default=2.0, help="单边滑点（bp）")
    return parser.parse_args()


def to_ms(text: str | None, default: int) -> int:
    if not text:
        return default
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def load_params() -> dict:
    path = ROOT / "config/strategies/trend_pullback.toml"
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def main() -> int:
    args = parse_args()
    cfg = load_config()
    db = Database(cfg.storage.db_path)
    interval = bar_to_ms(args.bar)

    stats = range_stats(db, args.inst, args.bar)
    if stats["count"] == 0:
        print(f"{args.inst} {args.bar} 没有数据，先跑 okxquant download")
        return 1

    start = to_ms(args.start, int(stats["first_ts"] or 0))
    end = to_ms(args.end, int(stats["last_ts"] or 0))
    start = start - start % interval
    end = end - end % interval

    strategy = TrendPullbackStrategy(load_params())
    account = AccountSnapshot(
        cash=args.equity,
        position_qty=0.0,
        avg_cost=0.0,
        last_price=0.0,
        equity=args.equity,
        unrealized=0.0,
        realized=0.0,
        drawdown=0.0,
        ts=0,
    )

    total = 0
    bullish = bearish = sideways = 0
    sides: Counter[str] = Counter()
    months: Counter[str] = Counter()
    atr_pcts: list[float] = []
    cost_rs: list[float] = []

    cost_rate = 2 * args.fee + 2 * args.slip / 10_000.0
    stop_mult = float(strategy.param("stop_atr_mult", 1.5))
    rr = float(strategy.param("take_profit_r", 2.0))

    bar_index = 0
    for page in iter_candles(db, args.inst, args.bar, start, end):
        for candle in page:
            actions = strategy.on_bar(
                BarCtx(candle=candle, account=account, bar_index=bar_index)
            )
            bar_index += 1

            regime = strategy._regime  # noqa: SLF001 - 诊断用途
            if regime > 0:
                bullish += 1
            elif regime < 0:
                bearish += 1
            else:
                sideways += 1

            for intent in actions:
                total += 1
                sides["多" if intent.side.sign > 0 else "空"] += 1
                month = datetime.fromtimestamp(candle.ts / 1000, tz=UTC).strftime("%Y-%m")
                months[month] += 1
                atr = strategy._atr.value or 0.0  # noqa: SLF001
                if atr > 0 and candle.close > 0:
                    atr_pct = atr / candle.close
                    atr_pcts.append(atr_pct)
                    cost_rs.append(cost_rate / (stop_mult * atr_pct))

    db.close()

    print(f"\n=== 信号统计 {args.inst} {args.bar} {fmt_ts(start)} ~ {fmt_ts(end)} ===")
    print(
        f"遍历 {bar_index:,} 根 K 线   多头趋势 {bullish:,}   "
        f"空头趋势 {bearish:,}   震荡 {sideways:,}"
    )
    print(
        f"理论上限信号 {total:,} 笔（多 {sides['多']} / 空 {sides['空']}）"
        f"   年均 {total / max(1, bar_index) * 105_120:.0f} 笔/年"
        if args.bar == "5m"
        else f"理论上限信号 {total:,} 笔（多 {sides['多']} / 空 {sides['空']}）"
    )

    if months:
        active = sum(1 for v in months.values() if v > 0)
        print(f"有信号的月份 {active}/{len(months)}   单月最多 {max(months.values())} 笔")

    if atr_pcts:
        print(
            f"信号时 ATR%: p10 {pct(atr_pcts, 0.1) * 100:.3f}%  "
            f"中位 {statistics.median(atr_pcts) * 100:.3f}%  "
            f"p90 {pct(atr_pcts, 0.9) * 100:.3f}%"
        )
        print(
            f"成本/1R: 中位 {statistics.median(cost_rs) * 100:.1f}%  "
            f"p10 {pct(cost_rs, 0.1) * 100:.1f}%  p90 {pct(cost_rs, 0.9) * 100:.1f}%"
        )
        breakeven = [(1 + c) / (1 + rr) for c in cost_rs]
        print(
            f"{rr:.1f}R 盈亏比下的保本胜率: 中位 {statistics.median(breakeven) * 100:.1f}%  "
            f"区间 {pct(breakeven, 0.1) * 100:.1f}% ~ {pct(breakeven, 0.9) * 100:.1f}%"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
