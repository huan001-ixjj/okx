"""回测/实盘 run 的元信息与绩效读写。"""

from __future__ import annotations

import json
import time
from typing import Any

from okxquant.store import Database


def create_run(
    db: Database,
    *,
    kind: str,
    strategy: str,
    inst_id: str,
    bar: str,
    start_ts: int,
    end_ts: int,
    params: dict[str, Any] | None = None,
    initial_equity: float | None = None,
    status: str = "running",
) -> int:
    sql = (
        "INSERT INTO runs (kind, status, strategy, inst_id, bar, start_ts, end_ts, "
        "started_at, initial_equity, params_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (
        kind,
        status,
        strategy,
        inst_id,
        bar,
        start_ts,
        end_ts,
        int(time.time() * 1000),
        initial_equity,
        json.dumps(params or {}, ensure_ascii=False),
    )
    with db.transaction() as conn:
        if db.backend == "postgres":
            # Postgres 没有 lastrowid，用 RETURNING 取自增主键
            cursor = conn.execute(sql + " RETURNING run_id", params)
            run_id = cursor.fetchone()["run_id"]
        else:
            cursor = conn.execute(sql, params)
            run_id = cursor.lastrowid
    assert run_id is not None
    return int(run_id)


def finish_run(
    db: Database,
    run_id: int,
    *,
    metrics: dict[str, Any],
    final_equity: float,
    status: str = "finished",
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?, final_equity=?, metrics_json=? "
            "WHERE run_id=?",
            (
                status,
                int(time.time() * 1000),
                final_equity,
                json.dumps(metrics, ensure_ascii=False),
                run_id,
            ),
        )


def update_status(db: Database, run_id: int, status: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))


def update_window(db: Database, run_id: int, start_ts: int, end_ts: int) -> None:
    """实盘开始时不知道区间，退出时用实际处理范围回填。"""
    if start_ts <= 0 or end_ts <= 0:
        return
    with db.transaction() as conn:
        conn.execute(
            "UPDATE runs SET start_ts=?, end_ts=? WHERE run_id=?", (start_ts, end_ts, run_id)
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in ("params_json", "metrics_json"):
        raw = data.pop(key, "{}") or "{}"
        try:
            data[key.replace("_json", "")] = json.loads(raw)
        except json.JSONDecodeError:
            data[key.replace("_json", "")] = {}
    return data


def get_run(db: Database, run_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def list_runs(db: Database, *, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        rows = db.execute(
            "SELECT * FROM runs WHERE kind=? ORDER BY run_id DESC LIMIT ?", (kind, limit)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def latest_run(
    db: Database, *, kind: str = "backtest", strategy: str | None = None
) -> dict[str, Any] | None:
    if strategy:
        row = db.execute(
            "SELECT * FROM runs WHERE kind=? AND strategy=? AND status='finished' "
            "ORDER BY run_id DESC LIMIT 1",
            (kind, strategy),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM runs WHERE kind=? AND status='finished' ORDER BY run_id DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None
