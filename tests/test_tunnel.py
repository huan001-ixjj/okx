"""SSH 隧道管理测试（不依赖真实网络与真实 ssh）。"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from okxquant import tunnel
from okxquant.config import NetworkConfig
from okxquant.tunnel import (
    PASSWORD_ENV,
    SshTunnel,
    TunnelSpec,
    build_ssh_args,
    classify_failure,
    password_from_env,
    port_open,
)

SECRET = "not-a-real-secret"


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".run"
    target.mkdir()
    monkeypatch.setattr(tunnel, "RUN_DIR", target)
    monkeypatch.setattr(tunnel, "PID_FILE", target / "tunnel.pid")
    monkeypatch.setattr(tunnel, "ASKPASS_FILE", target / "askpass.sh")
    monkeypatch.setattr(tunnel, "KNOWN_HOSTS_FILE", target / "known_hosts")
    monkeypatch.setattr(tunnel, "LOG_FILE", target / "tunnel.log")
    return target


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def listening_socket() -> tuple[socket.socket, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, int(sock.getsockname()[1])


class TestSshArgs:
    def test_args_contain_forwarding_and_liveness(self, run_dir: Path) -> None:
        spec = TunnelSpec(host="user@example.com", port=1080, ssh_port=2222, password=SECRET)
        args = build_ssh_args(spec)

        assert args[0] == "ssh"
        assert "-N" in args
        assert "-D" in args
        assert "127.0.0.1:1080" in args
        assert "2222" in args
        assert "ExitOnForwardFailure=yes" in args
        assert "ServerAliveInterval=30" in args
        assert args[-1] == "user@example.com"

    def test_password_never_in_argv(self, run_dir: Path) -> None:
        spec = TunnelSpec(host="user@example.com", password=SECRET)
        assert SECRET not in " ".join(build_ssh_args(spec))

    def test_known_hosts_isolated(self, run_dir: Path) -> None:
        spec = TunnelSpec(host="user@example.com")
        joined = " ".join(build_ssh_args(spec))
        assert str((run_dir / "known_hosts").resolve()) in joined
        assert "/root/.ssh/known_hosts" not in joined


class TestSpec:
    def test_repr_hides_password(self) -> None:
        spec = TunnelSpec(host="u@h", password=SECRET)
        assert SECRET not in repr(spec)
        assert SECRET not in str(spec)

    def test_from_config_reads_env_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(PASSWORD_ENV, SECRET)
        cfg = NetworkConfig(tunnel_enabled=True, tunnel_host="u@h", tunnel_port=1081)
        spec = TunnelSpec.from_config(cfg)
        assert spec.host == "u@h"
        assert spec.port == 1081
        assert spec.password == SECRET

    def test_password_from_env_strips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PASSWORD_ENV, "  abc  ")
        assert password_from_env() == "abc"


class TestAskpass:
    def test_script_reads_env_and_is_private(self, run_dir: Path) -> None:
        path = tunnel._ensure_askpass(SECRET)
        content = path.read_text(encoding="utf-8")
        assert PASSWORD_ENV in content
        assert SECRET not in content
        assert path.stat().st_mode & 0o777 == 0o700

    def test_cleanup_removes_script(self, run_dir: Path) -> None:
        path = tunnel._ensure_askpass(SECRET)
        assert path.exists()
        tunnel._cleanup_askpass()
        assert not path.exists()

    def test_cleanup_is_idempotent(self, run_dir: Path) -> None:
        tunnel._cleanup_askpass()
        tunnel._cleanup_askpass()


class TestPortProbe:
    def test_open_port_detected(self) -> None:
        sock, port = listening_socket()
        try:
            assert port_open("127.0.0.1", port, timeout=1.0)
        finally:
            sock.close()

    def test_closed_port_detected(self) -> None:
        port = free_port()
        assert not port_open("127.0.0.1", port, timeout=0.3)


class TestStatus:
    def test_not_running_when_port_closed(self, run_dir: Path) -> None:
        spec = TunnelSpec(host="u@h", port=free_port())
        status = SshTunnel(spec).status()
        assert not status.running
        assert status.pid is None

    def test_reuses_existing_port(self, run_dir: Path) -> None:
        sock, port = listening_socket()
        try:
            spec = TunnelSpec(host="u@h", port=port)
            status = SshTunnel(spec).ensure()
            assert status.running
            assert not status.owned  # pid 文件不存在，说明不是本工具启动的
            assert "复用" in status.describe() or "已有" in status.describe()
        finally:
            sock.close()

    def test_ensure_does_not_spawn_when_port_open(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sock, port = listening_socket()

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("端口已就绪时不应启动新进程")

        monkeypatch.setattr(tunnel.subprocess, "Popen", boom)
        try:
            status = SshTunnel(TunnelSpec(host="u@h", port=port)).ensure()
            assert status.running
        finally:
            sock.close()


class TestStop:
    def test_stop_without_pid_returns_false(self, run_dir: Path) -> None:
        assert SshTunnel(TunnelSpec(host="u@h", port=free_port())).stop() is False

    def test_stop_does_not_kill_foreign_process(self, run_dir: Path) -> None:
        """pid 文件指向当前进程时，因命令行不是 ssh -D 而拒绝终止。"""
        (run_dir / "tunnel.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert SshTunnel(TunnelSpec(host="u@h", port=free_port())).stop() is False
        # 进程仍然活着
        assert tunnel._pid_alive(os.getpid())

    def test_stop_clears_pid_file(self, run_dir: Path) -> None:
        (run_dir / "tunnel.pid").write_text("999999\n", encoding="utf-8")
        SshTunnel(TunnelSpec(host="u@h", port=free_port())).stop()
        assert not (run_dir / "tunnel.pid").exists()


class TestPidHelpers:
    def test_missing_cmdline_returns_empty(self) -> None:
        assert tunnel._cmdline(999_999) == []

    def test_read_pid_ignores_garbage(self, run_dir: Path) -> None:
        (run_dir / "tunnel.pid").write_text("abc\n", encoding="utf-8")
        assert tunnel._read_pid() is None

    def test_pid_is_our_tunnel_rejects_foreign(self) -> None:
        assert not tunnel._pid_is_our_tunnel(os.getpid(), 1080)


class TestFailureClassification:
    @pytest.mark.parametrize(
        ("log_text", "keyword"),
        [
            ("Permission denied (password).", "认证失败"),
            ("remote port forwarding failed for listen port", "拒绝 TCP 转发"),
            ("Host key verification failed.", "指纹"),
            ("ssh: connect to host x port 22: Connection refused", "拒绝"),
            ("connect to host x port 22: Connection timed out", "不可达"),
            ("ssh: Could not resolve hostname x", "解析失败"),
            ("something unexpected", "启动失败"),
        ],
    )
    def test_messages_are_actionable(self, log_text: str, keyword: str) -> None:
        message, hint = classify_failure(log_text)
        assert keyword in message
        assert hint


class TestStart:
    def test_missing_password_raises(self, run_dir: Path) -> None:
        spec = TunnelSpec(host="u@h", port=free_port(), password="")
        with pytest.raises(tunnel.TunnelError) as excinfo:
            SshTunnel(spec).start()
        assert PASSWORD_ENV in str(excinfo.value)

    def test_failure_reports_ssh_exit(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (run_dir / "tunnel.log").write_text("Permission denied (password).\n", encoding="utf-8")

        class FakeProc:
            pid = 424242

            def poll(self) -> int:
                return 255

        monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **kw: FakeProc())
        monkeypatch.setattr(SshTunnel, "_terminate", staticmethod(lambda pid: True))

        spec = TunnelSpec(host="u@h", port=free_port(), password=SECRET)
        with pytest.raises(tunnel.TunnelError) as excinfo:
            SshTunnel(spec).start()
        assert "认证失败" in str(excinfo.value)
        assert not (run_dir / "tunnel.pid").exists()
