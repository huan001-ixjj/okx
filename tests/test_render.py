"""终端渲染测试：字符图形形状与格式化。"""

from __future__ import annotations

import re

import pytest

from okxquant.engine.portfolio import Portfolio
from okxquant.render import (
    downsample,
    fmt_money,
    fmt_pct,
    fmt_qty,
    pnl_style,
)
from okxquant.render.charts import drawdown_series, line_chart, sparkline
from okxquant.render.panel import LiveState, render_state


class TestSparkline:
    def test_empty(self) -> None:
        assert sparkline([]) == ""

    def test_flat_series(self) -> None:
        result = sparkline([5.0, 5.0, 5.0])
        assert len(result) == 3
        assert len(set(result)) == 1

    def test_rising_series_is_monotonic(self) -> None:
        chars = "▁▂▃▄▅▆▇█"
        result = sparkline([1.0, 2.0, 3.0, 4.0])
        indexes = [chars.index(c) for c in result]
        assert indexes == sorted(indexes)

    def test_respects_width(self) -> None:
        result = sparkline([float(i) for i in range(100)], width=20)
        assert len(result) == 20


class TestLineChart:
    def test_empty(self) -> None:
        assert line_chart([]) == ["(无数据)"]

    def test_height_matches_request(self) -> None:
        lines = line_chart([1.0, 2.0, 3.0, 2.0], width=20, height=8)
        # 8 行数据 + 1 行坐标轴
        assert len(lines) == 9

    def test_contains_axis(self) -> None:
        lines = line_chart([1.0, 5.0, 3.0], width=20, height=5)
        assert any("└" in line for line in lines)

    def test_x_labels_appended(self) -> None:
        lines = line_chart([1.0, 2.0], width=20, height=5, x_labels=("START", "END"))
        assert "START" in lines[-1] and "END" in lines[-1]

    def test_plain_series_does_not_crash(self) -> None:
        lines = line_chart([100.0] * 10, width=20, height=6)
        assert len(lines) >= 2


class TestDrawdown:
    def test_new_highs_are_zero(self) -> None:
        assert drawdown_series([1.0, 2.0, 3.0]) == [0.0, 0.0, 0.0]

    def test_drawdown_negative(self) -> None:
        series = drawdown_series([100.0, 80.0])
        assert series[1] == pytest.approx(-0.2)

    def test_empty(self) -> None:
        assert drawdown_series([]) == []


class TestFormatting:
    def test_fmt_pct(self) -> None:
        assert fmt_pct(0.0123) == "+1.23%"
        assert fmt_pct(-0.05) == "-5.00%"

    def test_fmt_money(self) -> None:
        assert fmt_money(1234.5) == "1,234.50"
        assert fmt_money(-5.0, signed=True) == "-5.00"

    def test_fmt_qty(self) -> None:
        assert fmt_qty(0.00100000) == "0.001"
        assert fmt_qty(0.0) == "0"

    def test_pnl_style(self) -> None:
        assert pnl_style(1.0) == "green"
        assert pnl_style(-1.0) == "red"
        assert pnl_style(0.0) == "dim"

    def test_downsample_keeps_ends(self) -> None:
        values = [float(i) for i in range(100)]
        result = downsample(values, 10)
        assert len(result) == 10
        assert result[0] == 0.0
        assert result[-1] == 99.0

    def test_downsample_noop_when_short(self) -> None:
        assert downsample([1.0, 2.0], 10) == [1.0, 2.0]


class TestPanel:
    def test_render_without_snapshot(self) -> None:
        state = LiveState(inst_id="BTC-USDT", last_price=60_000.0, change_pct=0.0123)
        group = render_state(state)
        assert group is not None

    def test_render_with_snapshot_and_orders(self) -> None:
        from okxquant.domain import Order, OrderStatus, OrderType, Side

        portfolio = Portfolio("BTC-USDT", 10_000.0)
        state = LiveState(
            inst_id="BTC-USDT",
            last_price=60_000.0,
            snapshot=portfolio.mark(60_000.0, 1),
            orders=[
                Order(
                    cl_ord_id="x",
                    inst_id="BTC-USDT",
                    side=Side.BUY,
                    ord_type=OrderType.LIMIT,
                    qty=0.01,
                    px=59_000.0,
                    status=OrderStatus.LIVE,
                )
            ],
        )
        state.push_event("测试事件")
        group = render_state(state)
        assert group is not None

    def test_uptime_format(self) -> None:
        state = LiveState()
        state.started_at = 0.0
        assert re.match(r"\d+h\d{2}m|\d+m\d{2}s", state.uptime)
