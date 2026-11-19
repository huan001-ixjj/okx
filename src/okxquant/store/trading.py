"""订单、成交、权益曲线与风控事件的持久化。"""

from __future__ import annotations

from collections.abc import Iterable

from okxquant.domain import EquityPoint, Fill, Order, OrderStatus, OrderType, Side
from okxquant.store import BATCH_SIZE, Database

ORDER_UPSERT = """
INSERT INTO orders (
    run_id, cl_ord_id, inst_id, side, ord_type, qty, px, status,
    filled_qty, avg_px, fee, exchange_ord_id, reason, created_ts, updated_ts
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id, cl_ord_id) DO UPDATE SET
    status = excluded.status,
    filled_qty = excluded.filled_qty,
    avg_px = excluded.avg_px,
    fee = excluded.fee,
    exchange_ord_id = excluded.exchange_ord_id,
    updated_ts = excluded.updated_ts
"""

FILL_INSERT = """
INSERT OR IGNORE INTO fills (run_id, ts, side, qty, px, fee, cl_ord_id, ord_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

EQUITY_UPSERT = """
INSERT INTO equity_points (run_id, ts, equity, cash, position_qty, price)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id, ts) DO UPDATE SET
    equity = excluded.equity,
    cash = excluded.cash,
    position_qty = excluded.position_qty,
    price = excluded.price
"""


def _order_row(run_id: int, order: Order) -> tuple:
    return (
        run_id,
        order.cl_ord_id,
        order.inst_id,
        order.side.value,
        order.ord_type.value,
        order.qty,
        order.px,
        order.status.value,
        order.filled_qty,
        order.avg_px,
        order.fee,
        order.exchange_ord_id,
        order.reason,
        order.created_ts,
        order.updated_ts,
    )


def save_orders(db: Database, run_id: int, orders: Iterable[Order]) -> int:
    rows = [_order_row(run_id, o) for o in orders]
    if not rows:
        return 0
    with db.transaction() as conn:
        for offset in range(0, len(rows), BATCH_SIZE):
            conn.executemany(ORDER_UPSERT, rows[offset : offset + BATCH_SIZE])
    return len(rows)


def save_order(db: Database, run_id: int, order: Order) -> None:
    save_orders(db, run_id, [order])


def save_fills(db: Database, run_id: int, fills: Iterable[Fill]) -> int:
    rows = [
        (run_id, f.ts, f.side.value, f.qty, f.px, f.fee, f.cl_ord_id, f.ord_id) for f in fills
    ]
    if not rows:
        return 0
    with db.transaction() as conn:
        for offset in range(0, len(rows), BATCH_SIZE):
            conn.executemany(FILL_INSERT, rows[offset : offset + BATCH_SIZE])
    return len(rows)


def save_equity_points(db: Database, run_id: int, points: Iterable[EquityPoint]) -> int:
    rows = [
        (run_id, p.ts, p.equity, p.cash, p.position_qty, p.price) for p in points
    ]
    if not rows:
        return 0
    with db.transaction() as conn:
        for offset in range(0, len(rows), BATCH_SIZE):
            conn.executemany(EQUITY_UPSERT, rows[offset : offset + BATCH_SIZE])
    return len(rows)


def _row_to_order(row) -> Order:  # type: ignore[no-untyped-def]
    try:
        side = Side(row["side"])
    except ValueError:
        side = Side.BUY
    try:
        ord_type = OrderType(row["ord_type"])
    except ValueError:
        ord_type = OrderType.MARKET
    try:
        status = OrderStatus(row["status"])
    except ValueError:
        status = OrderStatus.PENDING
    return Order(
        cl_ord_id=str(row["cl_ord_id"]),
        inst_id=str(row["inst_id"]),
        side=side,
        ord_type=ord_type,
        qty=float(row["qty"]),
        px=float(row["px"]) if row["px"] is not None else None,
        status=status,
        filled_qty=float(row["filled_qty"]),
        avg_px=float(row["avg_px"]),
        fee=float(row["fee"]),
        exchange_ord_id=str(row["exchange_ord_id"]),
        reason=str(row["reason"]),
        created_ts=int(row["created_ts"]),
        updated_ts=int(row["updated_ts"]),
    )


def load_orders(db: Database, run_id: int, *, limit: int = 500) -> list[Order]:
    rows = db.execute(
        "SELECT * FROM orders WHERE run_id=? ORDER BY created_ts DESC, rowid DESC LIMIT ?",
        (run_id, limit),
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def open_orders(db: Database, run_id: int) -> list[Order]:
    """本地账本里仍处于挂单/未确认状态的订单，用于启动对账。"""
    rows = db.execute(
        "SELECT * FROM orders WHERE run_id=? AND status IN ('pending','live','partially_filled')",
        (run_id,),
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def load_fills(db: Database, run_id: int, *, limit: int = 1000) -> list[Fill]:
    rows = db.execute(
        "SELECT * FROM fills WHERE run_id=? ORDER BY ts ASC LIMIT ?", (run_id, limit)
    ).fetchall()
    fills: list[Fill] = []
    for row in rows:
        try:
            side = Side(row["side"])
        except ValueError:
            side = Side.BUY
        fills.append(
            Fill(
                ts=int(row["ts"]),
                side=side,
                qty=float(row["qty"]),
                px=float(row["px"]),
                fee=float(row["fee"]),
                cl_ord_id=str(row["cl_ord_id"]),
                ord_id=str(row["ord_id"]),
            )
        )
    return fills


def load_equity(db: Database, run_id: int) -> list[EquityPoint]:
    rows = db.execute(
        "SELECT ts, equity, cash, position_qty, price FROM equity_points "
        "WHERE run_id=? ORDER BY ts",
        (run_id,),
    ).fetchall()
    return [
        EquityPoint(
            ts=int(r["ts"]),
            equity=float(r["equity"]),
            cash=float(r["cash"]),
            position_qty=float(r["position_qty"]),
            price=float(r["price"]),
        )
        for r in rows
    ]


def record_risk_event(
    db: Database,
    *,
    run_id: int | None,
    ts: int,
    rule: str,
    message: str,
    trigger_value: float | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO risk_events (run_id, ts, rule, message, trigger_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, ts, rule, message, trigger_value),
        )


def load_risk_events(db: Database, run_id: int) -> list[dict[str, object]]:
    rows = db.execute(
        "SELECT ts, rule, message, trigger_value FROM risk_events "
        "WHERE run_id=? ORDER BY ts",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_fills(db: Database, run_id: int) -> int:
    row = db.execute("SELECT COUNT(*) FROM fills WHERE run_id=?", (run_id,)).fetchone()
    return int(row[0]) if row else 0
