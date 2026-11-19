"""Web 层引擎托管的看护逻辑测试。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from okxquant import web

SHORT_LIVED = [sys.executable, "-c", "import time; time.sleep(0.2)"]


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def fast_supervisor(monkeypatch, tmp_path: Path):
    """把看护线程的检查间隔与退避压到毫秒级，避免测试空等。"""
    monkeypatch.setattr(web, "CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(web, "RESTART_BACKOFF", (0.05, 0.05))
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "LOG_FILE", tmp_path / "engine.log")
    return tmp_path


@pytest.fixture
def managers():
    created: list[web.EngineManager] = []

    def factory() -> web.EngineManager:
        mgr = web.EngineManager()
        created.append(mgr)
        return mgr

    yield factory
    for mgr in created:
        mgr.shutdown()


def test_missing_credentials_does_not_spawn(monkeypatch, managers):
    for key in web.CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)

    mgr = managers()
    message = mgr.start()

    assert "缺少 OKX 凭据" in message
    assert mgr.is_running() is False
    info = mgr.info()
    assert info["running"] is False
    assert info["blocked"]


def test_supervisor_restarts_then_gives_up(monkeypatch, fast_supervisor, managers):
    for key in web.CREDENTIAL_KEYS:
        monkeypatch.setenv(key, "dummy")
    monkeypatch.setattr(web, "MAX_RESTARTS", 1)

    mgr = managers()
    monkeypatch.setattr(mgr, "_command", lambda: list(SHORT_LIVED))
    mgr.start()
    mgr.start_supervisor()

    assert _wait_until(lambda: mgr.info()["restarts"] >= 1), mgr.info()
    assert _wait_until(lambda: bool(mgr.info()["blocked"])), mgr.info()

    info = mgr.info()
    assert info["last_exit_code"] == 0
    assert info["running"] is False


def test_manual_start_clears_blocked(monkeypatch, fast_supervisor, managers):
    for key in web.CREDENTIAL_KEYS:
        monkeypatch.setenv(key, "dummy")
    monkeypatch.setattr(web, "MAX_RESTARTS", 0)

    mgr = managers()
    monkeypatch.setattr(mgr, "_command", lambda: list(SHORT_LIVED))
    mgr.start()
    mgr.start_supervisor()

    assert _wait_until(lambda: bool(mgr.info()["blocked"])), mgr.info()

    message = mgr.start()
    assert "started" in message
    assert mgr.info()["blocked"] == ""
    assert mgr.is_running() is True
