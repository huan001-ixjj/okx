"""K 线读写：幂等 upsert、分块流式读取、缺口检测。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from okxquant.domain import Candle
from okxquant.store import BATCH_SIZE, READ_BATCH, Database

UPSERT_SQL = """
INSERT INTO candles
    (inst_id, bar, ts, open, high, low, close, volume, volume_ccy, volume_quote)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(inst_id, bar, ts) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low  = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    volume_ccy = excluded.volume_ccy,
    volume_quote = excluded.volume_quote
"""


def align_ts(ts: int, interval_ms: int) -> int:
    """把时间戳向下对齐到 K 线周期边界。"""
    if interval_ms <= 0:
        return ts
    return ts - (ts % interval_ms)


def count_candles(db: Database, inst_id: str, bar: str, start_ts: int, end_ts: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM candles WHERE inst_id=? AND bar=? AND ts BETWEEN ? AND ?",
        (inst_id, bar, start_ts, end_ts),
    ).fetchone()
    return int(row["n"]) if row else 0


def upsert_candles(
    db: Database, inst_id: str, bar: str, candles: Iterable[Candle]
) -> tuple[int, int]:
    """幂等写入，返回 (写入行数, 新增行数)。"""
    items = list(candles)
    if not items:
        return 0, 0

    start_ts = min(c.ts for c in items)
    end_ts = max(c.ts for c in items)
    before = count_candles(db, inst_id, bar, start_ts, end_ts)

    rows = [
        (
            inst_id,
            bar,
            c.ts,
            c.open,
            c.high,
            c.low,
            c.close,
            c.volume,
            c.volume_ccy,
            c.volume_quote,
        )
        for c in items
    ]
    with db.transaction() as conn:
        for offset in range(0, len(rows), BATCH_SIZE):
            conn.executemany(UPSERT_SQL, rows[offset : offset + BATCH_SIZE])

    after = count_candles(db, inst_id, bar, start_ts, end_ts)
    return len(rows), max(0, after - before)


def latest_ts(db: Database, inst_id: str, bar: str) -> int | None:
    row = db.execute(
        "SELECT MAX(ts) AS v FROM candles WHERE inst_id=? AND bar=?", (inst_id, bar)
    ).fetchone()
    value = row["v"] if row else None
    return int(value) if value is not None else None


def earliest_ts(db: Database, inst_id: str, bar: str) -> int | None:
    row = db.execute(
        "SELECT MIN(ts) AS v FROM candles WHERE inst_id=? AND bar=?", (inst_id, bar)
    ).fetchone()
    value = row["v"] if row else None
    return int(value) if value is not None else None


def iter_candles(
    db: Database,
    inst_id: str,
    bar: str,
    start_ts: int,
    end_ts: int,
    batch: int = READ_BATCH,
) -> Iterator[list[Candle]]:
    """按时间升序分块读取，避免一次性把几十万根 K 线读进内存。"""
    cursor_ts = start_ts - 1
    while True:
        rows = db.execute(
            "SELECT ts, open, high, low, close, volume, volume_ccy, volume_quote "
            "FROM candles WHERE inst_id=? AND bar=? AND ts > ? AND ts <= ? "
            "ORDER BY ts LIMIT ?",
            (inst_id, bar, cursor_ts, end_ts, batch),
        ).fetchall()
        if not rows:
            return

        yield [
            Candle(
                ts=int(r["ts"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                volume_ccy=float(r["volume_ccy"] or 0.0),
                volume_quote=float(r["volume_quote"] or 0.0),
                confirm=True,
            )
            for r in rows
        ]

        cursor_ts = int(rows[-1]["ts"])
        if len(rows) < batch:
            return


def find_gaps(
    db: Database,
    inst_id: str,
    bar: str,
    start_ts: int,
    end_ts: int,
    interval_ms: int,
) -> list[tuple[int, int]]:
    """返回 [start, end] 形式的缺失区间（闭区间，已按周期对齐）。"""
    if interval_ms <= 0 or start_ts > end_ts:
        return []

    rows = db.execute(
        "SELECT ts FROM candles WHERE inst_id=? AND bar=? AND ts BETWEEN ? AND ? ORDER BY ts",
        (inst_id, bar, start_ts, end_ts),
    ).fetchall()

    gaps: list[tuple[int, int]] = []
    expected = start_ts
    for row in rows:
        ts = int(row["ts"])
        if ts > expected:
            gaps.append((expected, ts - interval_ms))
        expected = ts + interval_ms

    if expected <= end_ts:
        gaps.append((expected, end_ts))
    return gaps


def range_stats(db: Database, inst_id: str, bar: str) -> dict[str, int | None]:
    row = db.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi "
        "FROM candles WHERE inst_id=? AND bar=?",
        (inst_id, bar),
    ).fetchone()
    if row is None:
        return {"count": 0, "first_ts": None, "last_ts": None}
    return {
        "count": int(row["n"]),
        "first_ts": int(row["lo"]) if row["lo"] is not None else None,
        "last_ts": int(row["hi"]) if row["hi"] is not None else None,
    }
