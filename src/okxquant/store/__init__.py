"""SQLite 存储层。

单进程使用一个写连接（``check_same_thread=False`` + 显式锁），
WAL 模式下读写不互相阻塞。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger

from okxquant.errors import StoreError
from okxquant.store.schema import migrate, schema_version

BATCH_SIZE = 500
READ_BATCH = 5_000


class Database:
    """数据库连接持有者，负责 PRAGMA、迁移与事务。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=10.0
            )
        except sqlite3.Error as exc:
            raise StoreError(f"无法打开数据库 {self.path}: {exc}") from exc

        self._lock = threading.RLock()
        self._configure()
        migrate(self._conn)
        logger.debug("数据库就绪：{} (schema v{})", self.path, schema_version(self._conn))

    def _configure(self) -> None:
        conn = self._conn
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """批量写入用的事务上下文。"""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"数据库写入失败: {exc}") from exc
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                logger.debug("关闭数据库连接时异常")

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["BATCH_SIZE", "Database", "READ_BATCH"]
