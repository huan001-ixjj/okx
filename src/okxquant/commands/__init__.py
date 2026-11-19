"""子命令注册入口。每新增一个命令模块，就在此登记。"""

from __future__ import annotations

import typer


def register_all(app: typer.Typer) -> None:
    from okxquant.commands import (
        backtest,
        download,
        info,
        live,
        report,
        strategy_cmd,
        tunnel_cmd,
    )

    info.register(app)
    download.register(app)
    backtest.register(app)
    report.register(app)
    strategy_cmd.register(app)
    live.register(app)
    tunnel_cmd.register(app)
