"""SQLite 表结构与版本迁移。

设计要点：
- ``candles`` 用 ``(inst_id, bar, ts)`` 复合主键 + ``WITHOUT ROWID``，区间扫描走主键。
- 回测与实盘共用一个库，用 ``run_id`` 隔离，看板与报告可按 run 回看。
- ``meta`` 表记录 schema 版本，便于后续增量迁移。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candles (
    inst_id TEXT    NOT NULL,
    bar     TEXT    NOT NULL,
    ts      INTEGER NOT NULL,
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  REAL    NOT NULL,
    PRIMARY KEY (inst_id, bar, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    strategy       TEXT    NOT NULL,
    inst_id        TEXT    NOT NULL,
    bar            TEXT    NOT NULL,
    start_ts       INTEGER NOT NULL,
    end_ts         INTEGER NOT NULL,
    started_at     INTEGER NOT NULL,
    finished_at    INTEGER,
    initial_equity REAL,
    final_equity   REAL,
    params_json    TEXT    NOT NULL DEFAULT '{}',
    metrics_json   TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    run_id         INTEGER NOT NULL,
    cl_ord_id      TEXT    NOT NULL,
    inst_id        TEXT    NOT NULL,
    side           TEXT    NOT NULL,
    ord_type       TEXT    NOT NULL,
    qty            REAL    NOT NULL,
    px             REAL,
    status         TEXT    NOT NULL,
    filled_qty     REAL    NOT NULL DEFAULT 0,
    avg_px         REAL    NOT NULL DEFAULT 0,
    fee            REAL    NOT NULL DEFAULT 0,
    exchange_ord_id TEXT   NOT NULL DEFAULT '',
    reason         TEXT    NOT NULL DEFAULT '',
    created_ts     INTEGER NOT NULL DEFAULT 0,
    updated_ts     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, cl_ord_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(run_id, status);

CREATE TABLE IF NOT EXISTS fills (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    side     TEXT    NOT NULL,
    qty      REAL    NOT NULL,
    px       REAL    NOT NULL,
    fee      REAL    NOT NULL DEFAULT 0,
    cl_ord_id TEXT   NOT NULL DEFAULT '',
    ord_id   TEXT    NOT NULL DEFAULT '',
    UNIQUE (run_id, ord_id, ts, qty, px)
);

CREATE INDEX IF NOT EXISTS idx_fills_run ON fills(run_id, ts);

CREATE TABLE IF NOT EXISTS equity_points (
    run_id       INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    equity       REAL    NOT NULL,
    cash         REAL    NOT NULL,
    position_qty REAL    NOT NULL,
    price        REAL    NOT NULL,
    PRIMARY KEY (run_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS risk_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    ts            INTEGER NOT NULL,
    rule          TEXT    NOT NULL,
    message       TEXT    NOT NULL,
    trigger_value REAL
);

CREATE INDEX IF NOT EXISTS idx_risk_run ON risk_events(run_id, ts);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """创建缺失的表并记录 schema 版本。"""
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0
