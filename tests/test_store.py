"""存储层测试：幂等写入、缺口检测、run 生命周期、订单与权益。"""

from __future__ import annotations

from pathlib import Path

import pytest

from okxquant.domain import (
    Candle,
    EquityPoint,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Side,
)
from okxquant.store import Database, runs, trading
from okxquant.store.candles import (
    align_ts,
    count_candles,
    find_gaps,
    iter_candles,
    latest_ts,
    range_stats,
    upsert_candles,
)
from okxquant.store.schema import SCHEMA_VERSION, schema_version

BAR = "1m"
INTERVAL = 60_000
BASE_TS = 1_700_000_000_000 - (1_700_000_000_000 % INTERVAL)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def candles_at(offsets: list[int]) -> list[Candle]:
    return [
        Candle(
            ts=BASE_TS + off * INTERVAL,
            open=100.0 + off,
            high=101.0 + off,
            low=99.0 + off,
            close=100.5 + off,
            volume=1.0,
        )
        for off in offsets
    ]


def test_schema_version(db: Database) -> None:
    assert schema_version(db.conn) == SCHEMA_VERSION


def test_align_ts() -> None:
    assert align_ts(BASE_TS + 500, INTERVAL) == BASE_TS


def test_upsert_is_idempotent(db: Database) -> None:
    written, inserted = upsert_candles(db, "BTC-USDT", BAR, candles_at([0, 1, 2]))
    assert (written, inserted) == (3, 3)

    written, inserted = upsert_candles(db, "BTC-USDT", BAR, candles_at([0, 1, 2]))
    assert written == 3
    assert inserted == 0
    assert count_candles(db, "BTC-USDT", BAR, BASE_TS, BASE_TS + 2 * INTERVAL) == 3


def test_upsert_updates_existing_values(db: Database) -> None:
    upsert_candles(db, "BTC-USDT", BAR, candles_at([0]))
    updated = [Candle(ts=BASE_TS, open=1, high=2, low=0.5, close=1.5, volume=9.0)]
    upsert_candles(db, "BTC-USDT", BAR, updated)
    page = next(iter_candles(db, "BTC-USDT", BAR, BASE_TS, BASE_TS))
    assert page[0].close == 1.5
    assert page[0].volume == 9.0


def test_latest_ts_empty(db: Database) -> None:
    assert latest_ts(db, "BTC-USDT", BAR) is None


def test_iter_candles_paginates(db: Database) -> None:
    upsert_candles(db, "BTC-USDT", BAR, candles_at(list(range(10))))
    pages = list(iter_candles(db, "BTC-USDT", BAR, BASE_TS, BASE_TS + 9 * INTERVAL, batch=3))
    assert len(pages) == 4
    assert sum(len(p) for p in pages) == 10
    assert pages[0][0].ts == BASE_TS
    assert pages[-1][-1].ts == BASE_TS + 9 * INTERVAL


def test_find_gaps(db: Database) -> None:
    upsert_candles(db, "BTC-USDT", BAR, candles_at([0, 1, 3, 5]))
    gaps = find_gaps(db, "BTC-USDT", BAR, BASE_TS, BASE_TS + 5 * INTERVAL, INTERVAL)
    assert gaps == [
        (BASE_TS + 2 * INTERVAL, BASE_TS + 2 * INTERVAL),
        (BASE_TS + 4 * INTERVAL, BASE_TS + 4 * INTERVAL),
    ]


def test_find_gaps_tail(db: Database) -> None:
    upsert_candles(db, "BTC-USDT", BAR, candles_at([0]))
    gaps = find_gaps(db, "BTC-USDT", BAR, BASE_TS, BASE_TS + 2 * INTERVAL, INTERVAL)
    assert gaps == [(BASE_TS + INTERVAL, BASE_TS + 2 * INTERVAL)]


def test_range_stats(db: Database) -> None:
    upsert_candles(db, "BTC-USDT", BAR, candles_at([0, 1]))
    stats = range_stats(db, "BTC-USDT", BAR)
    assert stats["count"] == 2
    assert stats["first_ts"] == BASE_TS
    assert stats["last_ts"] == BASE_TS + INTERVAL


def test_run_lifecycle(db: Database) -> None:
    run_id = runs.create_run(
        db,
        kind="backtest",
        strategy="ma_cross",
        inst_id="BTC-USDT",
        bar=BAR,
        start_ts=BASE_TS,
        end_ts=BASE_TS + INTERVAL,
        params={"fast": 3},
        initial_equity=10_000.0,
    )
    assert run_id > 0

    runs.finish_run(db, run_id, metrics={"total_return": 0.05}, final_equity=10_500.0)
    record = runs.get_run(db, run_id)
    assert record is not None
    assert record["status"] == "finished"
    assert record["metrics"]["total_return"] == 0.05
    assert record["params"]["fast"] == 3

    listed = runs.list_runs(db)
    assert [r["run_id"] for r in listed] == [run_id]
    assert runs.latest_run(db)["run_id"] == run_id


def test_orders_upsert_and_open(db: Database) -> None:
    run_id = runs.create_run(
        db,
        kind="live",
        strategy="grid",
        inst_id="BTC-USDT",
        bar=BAR,
        start_ts=0,
        end_ts=0,
    )
    order = Order(
        cl_ord_id="c1",
        inst_id="BTC-USDT",
        side=Side.BUY,
        ord_type=OrderType.MARKET,
        qty=0.01,
        px=None,
        status=OrderStatus.LIVE,
    )
    trading.save_order(db, run_id, order)
    assert len(trading.open_orders(db, run_id)) == 1

    order.status = OrderStatus.FILLED
    order.filled_qty = 0.01
    trading.save_order(db, run_id, order)
    assert trading.open_orders(db, run_id) == []

    loaded = trading.load_orders(db, run_id)
    assert loaded[0].status is OrderStatus.FILLED


def test_fills_dedup(db: Database) -> None:
    run_id = runs.create_run(
        db, kind="live", strategy="grid", inst_id="BTC-USDT", bar=BAR, start_ts=0, end_ts=0
    )
    fill = Fill(ts=BASE_TS, side=Side.BUY, qty=0.01, px=60_000.0, fee=0.48, ord_id="o1")
    trading.save_fills(db, run_id, [fill])
    trading.save_fills(db, run_id, [fill])
    assert trading.count_fills(db, run_id) == 1


def test_equity_points_ordered(db: Database) -> None:
    run_id = runs.create_run(
        db, kind="backtest", strategy="grid", inst_id="BTC-USDT", bar=BAR, start_ts=0, end_ts=0
    )
    points = [
        EquityPoint(
            ts=BASE_TS + 2 * INTERVAL, equity=102.0, cash=50.0, position_qty=0.001, price=100.0
        ),
        EquityPoint(ts=BASE_TS, equity=100.0, cash=100.0, position_qty=0.0, price=100.0),
        EquityPoint(
            ts=BASE_TS + INTERVAL, equity=101.0, cash=80.0, position_qty=0.002, price=100.5
        ),
    ]
    trading.save_equity_points(db, run_id, points)
    loaded = trading.load_equity(db, run_id)
    assert [p.ts for p in loaded] == [BASE_TS, BASE_TS + INTERVAL, BASE_TS + 2 * INTERVAL]
    assert loaded[-1].equity == 102.0


def test_risk_events(db: Database) -> None:
    run_id = runs.create_run(
        db, kind="live", strategy="grid", inst_id="BTC-USDT", bar=BAR, start_ts=0, end_ts=0
    )
    trading.record_risk_event(
        db,
        run_id=run_id,
        ts=BASE_TS,
        rule="max_daily_loss",
        message="日内亏损超限",
        trigger_value=-310.0,
    )
    events = trading.load_risk_events(db, run_id)
    assert len(events) == 1
    assert events[0]["rule"] == "max_daily_loss"
