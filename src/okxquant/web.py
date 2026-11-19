"""Web 看板与引擎托管：FastAPI 提供状态接口，交易引擎以子进程运行。

数据存储由 OKX_DB_PATH 决定：文件路径 → SQLite；postgres:// URL →
Postgres。引擎与 Web 共用同一套 store 层，读到的就是引擎实时写入的数据。

部署形态要求「单容器常驻」（Render / Railway / VPS + Docker 均可）：
引擎是常驻子进程，serverless 平台跑不了。子进程异常退出由看护线程按
指数退避自动重拉，失败原因通过 ``/health`` 暴露。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from loguru import logger

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("OKX_DATA_DIR", str(ROOT / "data")))
DB_PATH = os.getenv("OKX_DB_PATH", str(DATA_DIR / "quant.db"))
LOG_FILE = DATA_DIR / "engine.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
INDEX_FILE = Path(__file__).resolve().parent / "static" / "index.html"

INST_ID = os.getenv("OKX_INST_ID", "XRP-USDT-SWAP")
BAR = os.getenv("OKX_BAR", "15m")
CASH = os.getenv("OKX_CASH", "1.5")
MIN_NOTIONAL = os.getenv("OKX_MIN_NOTIONAL", "0.5")
STRATEGY = os.getenv("OKX_STRATEGY", str(ROOT / "strategies" / "bollinger_reversion.py"))
AUTOSTART = os.getenv("AUTOSTART_ENGINE", "1").strip().lower() in {"1", "true", "yes", "on"}
ENVIRONMENT = "LIVE" if (os.getenv("OKX_FLAG") or "1").strip() == "0" else "SIMULATED"

CREDENTIAL_KEYS = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE")
RESTART_BACKOFF = (5.0, 10.0, 20.0, 40.0, 60.0)
STABLE_SECONDS = 300.0  # 存活超过这个时长视为稳定，重启计数清零
CHECK_INTERVAL = 5.0


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int((os.getenv(name) or "").strip() or default))
    except ValueError:
        logger.warning("环境变量 {} 不是合法整数，回退为 {}", name, default)
        return default


MAX_RESTARTS = _int_env("OKX_MAX_RESTARTS", 10)


def _missing_credentials() -> list[str]:
    return [key for key in CREDENTIAL_KEYS if not (os.getenv(key) or "").strip()]


class EngineManager:
    """以子进程方式托管 ``okxquant live``，并在异常退出后自动重拉。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._supervisor: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._auto = False
        self._started_at = 0.0
        self._next_attempt = 0.0
        self._restarts = 0
        self._last_exit_code: int | None = None
        self._message = ""
        self._blocked = ""

    # ------------------------------------------------------------------ #
    # 进程控制
    # ------------------------------------------------------------------ #
    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def _command(self) -> list[str]:
        return [
            sys.executable, "-u", "-m", "okxquant", "live",
            "--inst", INST_ID, "--bar", BAR, "--cash", CASH,
            "--min-notional", MIN_NOTIONAL, "-s", STRATEGY,
        ]

    def _spawn(self) -> str:
        missing = _missing_credentials()
        if missing:
            self._blocked = "缺少 OKX 凭据：" + ", ".join(missing)
            logger.error("{}，引擎不启动", self._blocked)
            return self._blocked

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._trim_log()
        with LOG_FILE.open("ab") as log_file:
            self._proc = subprocess.Popen(
                self._command(),
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        self._started_at = time.monotonic()
        self._message = f"engine started (pid {self._proc.pid})"
        logger.info(self._message)
        return self._message

    def _terminate(self) -> None:
        proc = self._proc
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
            self._message = "engine stopped"
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            self._message = "engine killed"
        self._last_exit_code = proc.returncode
        self._proc = None
        logger.info(self._message)

    def start(self) -> str:
        """手动启动并重新开启自动看护。"""
        with self._lock:
            self._auto = True
            self._blocked = ""
            self._restarts = 0
            self._next_attempt = 0.0
            if self.is_running():
                self._message = "engine already running"
                return self._message
            return self._spawn()

    def stop(self) -> str:
        with self._lock:
            self._auto = False
            if not self.is_running():
                self._message = "engine not running"
                return self._message
            self._terminate()
            return self._message

    def shutdown(self) -> None:
        """进程退出前调用：停止看护并优雅关闭子进程。"""
        self._shutdown.set()
        thread = self._supervisor
        if thread is not None:
            thread.join(timeout=CHECK_INTERVAL + 1)
        with self._lock:
            self._auto = False
            if self.is_running():
                self._terminate()

    # ------------------------------------------------------------------ #
    # 看护线程
    # ------------------------------------------------------------------ #
    def start_supervisor(self) -> None:
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                return
            self._shutdown.clear()
            self._supervisor = threading.Thread(
                target=self._supervise, name="engine-supervisor", daemon=True
            )
            self._supervisor.start()

    def _supervise(self) -> None:
        while not self._shutdown.wait(CHECK_INTERVAL):
            try:
                self._check()
            except Exception:  # pragma: no cover - 看护线程不能死
                logger.exception("引擎看护线程异常")

    def _check(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None:
                code = proc.poll()
                if code is None:
                    return
                self._proc = None
                self._last_exit_code = code
                if time.monotonic() - self._started_at >= STABLE_SECONDS:
                    self._restarts = 0
                if self._restarts >= MAX_RESTARTS:
                    self._blocked = (
                        f"引擎连续退出 {self._restarts} 次，已停止自动重启（末次退出码 {code}），"
                        "请查 /logs"
                    )
                    logger.error(self._blocked)
                    return
                delay = RESTART_BACKOFF[min(self._restarts, len(RESTART_BACKOFF) - 1)]
                self._restarts += 1
                self._next_attempt = time.monotonic() + delay
                self._message = (
                    f"engine exited (code {code}), restart #{self._restarts} in {delay:.0f}s"
                )
                logger.warning(self._message)
                return

            if not self._auto or self._blocked:
                return
            if time.monotonic() < self._next_attempt:
                return
            self._spawn()

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def info(self) -> dict[str, object]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            wait = (
                max(0.0, self._next_attempt - time.monotonic())
                if self._auto and not running and not self._blocked
                else 0.0
            )
            return {
                "autostart": self._auto,
                "running": running,
                "pid": self._proc.pid if running and self._proc is not None else None,
                "restarts": self._restarts,
                "last_exit_code": self._last_exit_code,
                "message": self._message,
                "blocked": self._blocked,
                "next_retry_sec": round(wait, 1),
            }

    def tail_log(self, limit: int = 40) -> list[str]:
        if not LOG_FILE.exists():
            return []
        try:
            return LOG_FILE.read_text(errors="replace").splitlines()[-limit:]
        except OSError:
            return []

    def _trim_log(self) -> None:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.unlink()
        except OSError:
            logger.debug("清理引擎日志失败")


class DataReader:
    """惰性数据库读取器：SQLite/Postgres 由 OKX_DB_PATH 自动决定。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._db is None:
                from okxquant.store import Database

                self._db = Database(self._db_path)
            return self._db

    def latest_live_run(self) -> dict | None:
        from okxquant.store.runs import list_runs

        try:
            runs = list_runs(self._get(), limit=1, kind="live")
        except Exception as exc:
            logger.warning("读取运行记录失败：{}", exc)
            return None
        return runs[0] if runs else None

    def recent_fills(self, run_id: int, limit: int) -> list[dict]:
        from okxquant.store.trading import recent_fills

        try:
            fills = recent_fills(self._get(), run_id, limit=limit)
        except Exception as exc:
            logger.warning("读取成交记录失败：{}", exc)
            return []
        return [
            {"ts": f.ts, "side": f.side.value, "qty": f.qty, "px": f.px, "fee": f.fee}
            for f in fills
        ]

    def equity_curve(self, run_id: int, limit: int) -> list[dict]:
        """最新 N 个权益采样点，按时间升序返回。"""
        try:
            rows = self._get().execute(
                "SELECT ts, equity, cash, position_qty, price FROM equity_points "
                "WHERE run_id=? ORDER BY ts DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        except Exception as exc:
            logger.warning("读取权益曲线失败：{}", exc)
            return []
        return [
            {
                "ts": int(r["ts"]),
                "equity": float(r["equity"]),
                "cash": float(r["cash"]),
                "position_qty": float(r["position_qty"]),
                "price": float(r["price"]),
            }
            for r in reversed(rows)
        ]

    def recent_orders(self, run_id: int, limit: int) -> list[dict]:
        from okxquant.store.trading import load_orders

        try:
            orders = load_orders(self._get(), run_id, limit=limit)
        except Exception as exc:
            logger.warning("读取订单失败：{}", exc)
            return []
        return [
            {
                "cl_ord_id": o.cl_ord_id,
                "side": o.side.value,
                "ord_type": o.ord_type.value,
                "qty": o.qty,
                "px": o.px,
                "status": o.status.value,
                "filled_qty": o.filled_qty,
                "avg_px": o.avg_px,
                "created_ts": o.created_ts,
            }
            for o in orders
        ]

    def trade_count(self, run_id: int) -> int | None:
        from okxquant.store.trading import count_fills

        try:
            return count_fills(self._get(), run_id)
        except Exception as exc:
            logger.warning("统计成交笔数失败：{}", exc)
            return None


manager = EngineManager()
reader = DataReader(DB_PATH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.start_supervisor()
    if AUTOSTART:
        manager.start()
    yield
    manager.shutdown()


app = FastAPI(title="okxquant", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    """看板首页：纯静态单文件页面，数据全部来自下面的 JSON 接口。"""
    if not INDEX_FILE.exists():
        raise HTTPException(status_code=500, detail="static/index.html 缺失")
    return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict[str, object]:
    """保活探针：外部定时请求此接口防止实例休眠。

    返回引擎是否在跑、重启次数与阻塞原因，便于部署后一眼定位问题。
    """
    info = manager.info()
    return {"ok": True, "engine_running": info["running"], "engine": info}


@app.get("/status")
def status() -> dict[str, object]:
    run = reader.latest_live_run()
    payload: dict[str, object] = {
        "environment": ENVIRONMENT,
        "inst_id": INST_ID,
        "bar": BAR,
        "engine_running": manager.is_running(),
        "engine": manager.info(),
        "run": None,
    }
    if run is None:
        return payload

    # 运行中的 run 还没写 final_equity/metrics，用最新权益采样点推算实时表现
    run_id = run["run_id"]
    equity = run["final_equity"]
    total_return = run["metrics"].get("total_return")
    if run["status"] == "running" or equity is None:
        curve = reader.equity_curve(int(run_id), 1)
        if curve:
            equity = curve[-1]["equity"]
            initial = run.get("initial_equity")
            if initial:
                total_return = float(equity) / float(initial) - 1.0

    trade_count = run["metrics"].get("trade_count")
    if trade_count is None:
        trade_count = reader.trade_count(int(run_id))

    payload["run"] = {
        "run_id": run_id,
        "strategy": run["strategy"],
        "inst_id": run["inst_id"],
        "bar": run["bar"],
        "status": run["status"],
        "final_equity": equity,
        "bars": run["metrics"].get("bars"),
        "win_rate": run["metrics"].get("win_rate"),
        "trade_count": trade_count,
        "total_return": total_return,
    }
    return payload


@app.get("/trades")
def trades(limit: int = 20) -> dict[str, object]:
    run = reader.latest_live_run()
    if run is None:
        return {"trades": []}
    return {"trades": reader.recent_fills(run["run_id"], min(limit, 200))}


@app.get("/equity")
def equity(limit: int = 300) -> dict[str, object]:
    run = reader.latest_live_run()
    if run is None:
        return {"points": []}
    return {"run_id": run["run_id"], "points": reader.equity_curve(run["run_id"], min(limit, 2000))}


@app.get("/orders")
def orders(limit: int = 30) -> dict[str, object]:
    run = reader.latest_live_run()
    if run is None:
        return {"orders": []}
    return {"orders": reader.recent_orders(run["run_id"], min(limit, 200))}


@app.get("/logs")
def logs(limit: int = 40) -> dict[str, object]:
    return {"lines": manager.tail_log(limit)}


@app.post("/engine/start")
def engine_start() -> dict[str, str]:
    return {"message": manager.start()}


@app.post("/engine/stop")
def engine_stop() -> dict[str, str]:
    return {"message": manager.stop()}
