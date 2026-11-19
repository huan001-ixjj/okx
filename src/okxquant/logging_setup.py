"""日志初始化：stderr 人类可读 + 文件按天轮转。

日志中不会打印密钥、passphrase 或签名串；loguru 的 diagnose 已关闭以避免
异常回溯把局部变量（可能含凭据）写入文件。
"""

from __future__ import annotations

import sys

from loguru import logger

from okxquant.config import LoggingConfig

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> "
    "<level>{level: <7}</level> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} | {message}"
)


def setup_logging(cfg: LoggingConfig, *, console_level: str | None = None) -> None:
    """重建 loguru 的 sink，确保重复调用不会叠加处理器。"""
    logger.remove()

    logger.add(
        sys.stderr,
        level=console_level or cfg.level,
        format=_CONSOLE_FORMAT,
        colorize=sys.stderr.isatty(),
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )

    cfg.dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        cfg.dir / "okxquant_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format=_FILE_FORMAT,
        rotation="00:00",
        retention=f"{cfg.retention_days} days",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
