"""1m 高频可行性判定。

三个问题按顺序回答，任何一个不通过就没必要继续做策略：

1. **成本墙**：1m 单根的价格变动分布有多大？完美预测下一根方向能赚多少？
   若 ``E[|r_1m|] < 往返成本``，那么即使有完美预知能力也必然是亏损，任何策略都无解。
2. **随机基准**：随机方向进出的期望收益约等于 -成本，用来校准"策略必须比随机好多少"。
3. **朴素策略**：1m 动量/均值回归各跑一遍，看是否显著优于随机基准。

用法：

    python scripts/hf_feasibility.py
    python scripts/hf_feasibility.py --inst XRP-USDT-SWAP --start 2026-09-19
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from okxquant.config import bar_to_ms, load_config  # noqa: E402
from okxquant.store import Database  # noqa: E402
from okxquant.store.candles import iter_candles, range_stats  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="1m 高频可行性判定")
    p.add_argument("--inst", default="XRP-USDT-SWAP")
    p.add_argument("--start", default=None, help="默认取库内最早时间")
    p.add_argument("--end", default=None)
    p.add_argument("--maker", type=float, default=0.0002, help="单边挂单费率")
    p.add_argument("--taker", type=float, default=0.0005, help="单边吃单费率")
    p.add_argument("--slip", type=float, default=0.0, help="单边滑点（比例）")
    return p.parse_args()


def to_ms(text: str | None) -> int | None:
    if not text:
        return None
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[int(q * (len(ordered) - 1))]


def main() -> int:
    args = parse_args()
    cfg = load_config()
    db = Database(cfg.storage.db_path)
    interval = bar_to_ms("1m")

    stats = range_stats(db, args.inst, "1m")
    if not stats["count"]:
        print(f"{args.inst} 1m 没有数据，先跑 okxquant download --bar 1m")
        return 1

    start = to_ms(args.start) or int(stats["first_ts"] or 0)
    end = to_ms(args.end) or int(stats["last_ts"] or 0)
    start -= start % interval
    end -= end % interval

    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for page in iter_candles(db, args.inst, "1m", start, end):
        for c in page:
            closes.append(c.close)
            highs.append(c.high)
            lows.append(c.low)
    db.close()

    n = len(closes)
    if n < 1000:
        print(f"样本不足：{n} 根")
        return 1

    rets = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, n)]
    rng = [(highs[i] - lows[i]) / closes[i] for i in range(n)]

    maker_cost = 2 * args.maker
    taker_cost = 2 * (args.taker + args.slip)

    first = datetime.fromtimestamp(start / 1000, tz=UTC).strftime("%Y-%m-%d")
    last = datetime.fromtimestamp(end / 1000, tz=UTC).strftime("%Y-%m-%d")
    print(f"\n=== {args.inst} 1m 可行性判定 {first} ~ {last}（{n:,} 根）===")

    # ---------- 1. 成本墙 ----------
    mean_abs = statistics.fmean(rets)
    print("\n[1] 成本墙")
    print(
        f"单根 |收益率|: 中位 {pct(rets, 0.5) * 100:.4f}%  "
        f"均值 {mean_abs * 100:.4f}%  p90 {pct(rets, 0.9) * 100:.4f}%"
    )
    print(
        f"单根振幅:     中位 {pct(rng, 0.5) * 100:.4f}%  "
        f"p90 {pct(rng, 0.9) * 100:.4f}%"
    )
    print(f"往返成本: maker {maker_cost * 100:.3f}%   taker {taker_cost * 100:.3f}%")
    print(
        f"完美预测下一根方向的期望收益 = 均值 {mean_abs * 100:.4f}% "
        f"- maker 成本 {maker_cost * 100:.3f}% = {(mean_abs - maker_cost) * 100:+.4f}%"
    )
    if mean_abs < maker_cost:
        print("  → [结论] 即使完美预知方向也覆盖不了 maker 成本，1m 高频无解")
    else:
        print("  → 完美预知下有余量，理论上存在空间（但不代表可预测）")

    for label, cost in (("maker", maker_cost), ("taker", taker_cost)):
        cover = sum(1 for r in rets if r > cost) / len(rets)
        print(f"  单根变动能覆盖 {label} 成本的比例: {cover * 100:.1f}%")

    # ---------- 2. 随机基准 ----------
    print("\n[2] 随机方向基准（每笔持 1 根）")
    print(f"  期望单笔收益 = -成本 = maker {-maker_cost * 100:+.4f}%  taker {-taker_cost * 100:+.4f}%")

    # ---------- 3. 朴素策略 ----------
    print("\n[3] 朴素 1m 策略（无止损，持有 N 根）")

    def run_hold(direction: str, hold: int, cost: float) -> tuple[float, int]:
        """direction: 'mom' 跟随上一根方向 / 'rev' 反向；返回 (累计收益, 笔数)。"""
        total = 0.0
        trades = 0
        i = 1
        while i + hold < n:
            prev = closes[i] / closes[i - 1] - 1
            if prev == 0:
                i += 1
                continue
            sign = 1.0 if prev > 0 else -1.0
            if direction == "rev":
                sign = -sign
            total += (closes[i + hold] / closes[i] - 1) * sign - cost
            trades += 1
            i += hold
        return total, trades

    for direction, name in (("mom", "动量"), ("rev", "反转")):
        for hold in (1, 3, 5):
            for label, cost in (("maker", maker_cost), ("taker", taker_cost)):
                gross, trades = run_hold(direction, hold, cost)
                if not trades:
                    continue
                print(
                    f"  {name} 持有{hold}根 {label:5s}: 累计 {gross * 100:+8.2f}%  "
                    f"单笔 {gross / trades * 100:+.4f}%  {trades:,} 笔"
                )

    print(
        "\n判定标准：单笔净收益必须显著为正且稳定；"
        "若所有朴素策略都在 -成本附近，说明 1m 上没有可捕获的方向性边际。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
