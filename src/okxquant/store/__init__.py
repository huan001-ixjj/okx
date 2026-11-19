"""存储层：SQLite（默认）与 Postgres 双后端。

- 传文件路径 → SQLite；传 ``postgres://`` / ``postgresql://`` URL → Postgres。
- SQL 统一用 ``?`` 占位符书写，Postgres 模式下由适配层转换为 ``%s``；
  行访问统一 ``row["col"]``（SQLite Row 与 psycopg dict_row 都支持）。
"""

from __future__ import annotations

import contextlib
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


def _translate(sql: str, backend: str) -> str:
    """把 ``?`` 占位符转换成目标方言。"""
    return sql.replace("?", "%s") if backend == "postgres" else sql


class _Runner:
    """事务内执行器：屏蔽两端方言差异。"""

    def __init__(self, conn, backend: str) -> None:
        self._conn = conn
        self._backend = backend

    def execute(self, sql: str, params=()):
        return self._conn.execute(_translate(sql, self._backend), params)

    def executemany(self, sql: str, rows):
        text = _translate(sql, self._backend)
        if self._backend == "postgres":
            # psycopg3 的 executemany 在 cursor 上，Connection 没有这个方法
            with self._conn.cursor() as cur:
                cur.executemany(text, rows)
            return None
        return self._conn.executemany(text, rows)


class Database:
    """数据库连接持有者，负责迁移、方言适配与事务。"""

    def __init__(self, path_or_url: Path | str) -> None:
        text = str(path_or_url)
        self._backend = (
            "postgres" if text.startswith(("postgres://", "postgresql://")) else "sqlite"
        )

        if self._backend == "postgres":
            self._dsn = text
            self._conn = self._connect_postgres()
            self.path = Path("postgres")
        else:
            self.path = Path(text)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._conn = sqlite3.connect(
                    str(self.path), check_same_thread=False, timeout=10.0
                )
            except sqlite3.Error as exc:
                raise StoreError(f"无法打开数据库 {self.path}: {exc}") from exc
            self._configure()

        self._lock = threading.RLock()
        migrate(self._conn, self._backend)
        logger.debug(
            "数据库就绪：{} ({} backend, schema v{})",
            self.path,
            self._backend,
            schema_version(self._conn, self._backend),
        )

    def _connect_postgres(self):
        """建立 Postgres 连接。

        - ``connect_timeout``：数据库不可达时快速失败，别让 Web 请求挂死。
        - ``prepare_threshold=None``：关闭自动 prepared statement，兼容 Supabase /
          pgBouncer 的事务池化模式（池化模式下复用 prepared statement 会报错）。
        - ``keepalives*``：云数据库/NAT 会掐空闲连接，开 TCP 保活让它活得久一点。
        """
        try:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(
                self._dsn,
                row_factory=dict_row,
                connect_timeout=10,
                prepare_threshold=None,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )
        except Exception as exc:
            raise StoreError(f"无法连接 Postgres: {exc}") from exc

    def _reconnect_if_needed(self) -> None:
        """连接被服务端/中间设备掐断后，下次访问前自动重建。"""
        if self._backend != "postgres":
            return
        conn = self._conn
        if not (getattr(conn, "closed", False) or getattr(conn, "broken", False)):
            return
        with contextlib.suppress(Exception):  # 关闭已断连接时可能抛错
            conn.close()
        logger.warning("Postgres 连接已断开，正在重连")
        self._conn = self._connect_postgres()

    def _configure(self) -> None:
        conn = self._conn
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA temp_store=MEMORY")

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def conn(self):
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[_Runner]:
        """批量写入用的事务上下文，返回支持两种方言的执行器。"""
        with self._lock:
            self._reconnect_if_needed()
            try:
                yield _Runner(self._conn, self._backend)
                self._conn.commit()
            except sqlite3.Error as exc:
                self._conn.rollback()
                raise StoreError(f"数据库写入失败: {exc}") from exc
            except Exception as exc:
                self._conn.rollback()
                if self._backend == "postgres":
                    raise StoreError(f"数据库写入失败: {exc}") from exc
                raise

    def execute(self, sql: str, params: tuple | dict = ()):
        with self._lock:
            self._reconnect_if_needed()
            return _Runner(self._conn, self._backend).execute(sql, params)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                logger.debug("关闭数据库连接时异常")

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["BATCH_SIZE", "Database", "READ_BATCH"]
