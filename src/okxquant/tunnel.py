"""SSH 动态端口转发（SOCKS5 隧道）管理。

**仅用于开发环境。** 部署到能直连 OKX 的服务器时不需要、也不应该启用本模块：
``network.tunnel_enabled`` 默认为 ``False``，未启用时代码不会导入这里。

为什么用 ``-D`` 而不是 ``-L``：``-D`` 提供 SOCKS5，配合 ``socks5h`` 由远端解析域名，
可以同时绕过「本地 DNS 被污染」和「目标端口被阻断」两个问题。

密码**只从环境变量读取**，通过 ``SSH_ASKPASS`` 交给 ssh，
既不进命令行参数（``ps`` 不可见），也不进日志与配置模型。
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from okxquant.config import NetworkConfig
from okxquant.errors import OkxQuantError

PASSWORD_ENV = "OKX_TUNNEL_PASSWORD"
RUN_DIR = Path(".run")
PID_FILE = RUN_DIR / "tunnel.pid"
ASKPASS_FILE = RUN_DIR / "askpass.sh"
KNOWN_HOSTS_FILE = RUN_DIR / "known_hosts"
LOG_FILE = RUN_DIR / "tunnel.log"

CONNECT_TIMEOUT = 10
READY_POLL_INTERVAL = 0.2
LOG_TAIL_BYTES = 4096


class TunnelError(OkxQuantError):
    """隧道建立或使用失败。"""

    exit_code = 3


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    running: bool
    port: int
    pid: int | None = None
    owned: bool = False
    detail: str = ""

    def describe(self) -> str:
        if not self.running:
            return f"未运行（{self.detail}）" if self.detail else "未运行"
        if self.owned:
            return f"运行中，pid {self.pid}，本地 SOCKS5 端口 {self.port}"
        return f"端口 {self.port} 已有可用通道（非本工具启动，不持有）"


@dataclass(frozen=True, slots=True)
class TunnelSpec:
    host: str
    port: int = 1080
    ssh_port: int = 22
    timeout: float = 20.0
    password: str = field(default="", repr=False)

    @classmethod
    def from_config(cls, cfg: NetworkConfig, *, password: str | None = None) -> TunnelSpec:
        return cls(
            host=cfg.tunnel_host,
            port=cfg.tunnel_port,
            ssh_port=cfg.tunnel_ssh_port,
            timeout=cfg.tunnel_timeout_sec,
            password=password if password is not None else password_from_env(),
        )

    @property
    def has_password(self) -> bool:
        return bool(self.password)


def password_from_env() -> str:
    """隧道密码只存在于环境变量 / .env 中。"""
    return (os.getenv(PASSWORD_ENV) or "").strip()


def port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """端口是否可建立 TCP 连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid() -> int | None:
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _write_pid(pid: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{pid}\n", encoding="utf-8")


def _clear_pid() -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        PID_FILE.unlink()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, OSError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _pid_is_our_tunnel(pid: int, port: int) -> bool:
    """通过命令行确认该进程确实是本工具拉起的 ssh -D。

    只认「可执行文件名含 ssh」且「参数含 -D 与 127.0.0.1:<port>」的进程，
    避免 pid 复用时误杀无关进程。
    """
    argv = _cmdline(pid)
    if not argv:
        return False
    executable = Path(argv[0]).name
    if "ssh" not in executable:
        return False
    joined = " ".join(argv)
    return "-D" in argv and f"127.0.0.1:{port}" in joined


def _ensure_askpass(password: str) -> Path:
    """生成一次性 askpass 脚本（0700），内容只引用环境变量。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    script = "#!/bin/sh\nprintf '%s\\n' \"$" + PASSWORD_ENV + "\"\n"
    ASKPASS_FILE.write_text(script, encoding="utf-8")
    ASKPASS_FILE.chmod(0o700)
    # 密码本身不写进脚本：只有子进程环境里有
    return ASKPASS_FILE.resolve()


def _cleanup_askpass() -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        ASKPASS_FILE.unlink()


def build_ssh_args(spec: TunnelSpec) -> list[str]:
    """构造 ssh 命令参数。**不含密码。**"""
    return [
        "ssh",
        "-N",
        "-D",
        f"127.0.0.1:{spec.port}",
        "-p",
        str(spec.ssh_port),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS_FILE.resolve()}",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        spec.host,
    ]


def _tail_log() -> str:
    try:
        with LOG_FILE.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace")
    except (FileNotFoundError, OSError):
        return ""


def classify_failure(log_text: str) -> tuple[str, str]:
    """把 ssh 的输出翻译成可操作的错误与建议。"""
    text = log_text.lower()
    if "permission denied" in text:
        return (
            "SSH 认证失败",
            "检查 .env 中的 OKX_TUNNEL_PASSWORD 是否正确，以及账号是否允许密码登录",
        )
    if (
        "administratively prohibited" in text
        or "remote port forwarding failed" in text
        or "cannot listen to port" in text
    ):
        return (
            "SSH 主机拒绝 TCP 转发",
            "该主机未开放端口转发，需要换一台允许转发的机器，或用 -L + hosts 的回退方案",
        )
    if "host key verification failed" in text:
        return (
            "SSH 主机指纹校验失败",
            "删除 .run/known_hosts 后重试；若主机变更过需确认是否为预期",
        )
    if "connection refused" in text:
        return ("SSH 端口连接被拒绝", "确认主机地址与端口（默认 22）是否正确")
    if "timed out" in text or "no route to host" in text:
        return ("SSH 主机不可达", "确认网络能访问该主机，或主机是否限制了来源 IP")
    if "could not resolve hostname" in text:
        return ("SSH 主机名解析失败", "检查 OKX_TUNNEL_HOST 的拼写与本地 DNS")
    return ("SSH 隧道启动失败", "查看 .run/tunnel.log 末尾的 ssh 输出")


class SshTunnel:
    """管理一个 ``ssh -N -D`` 后台进程。"""

    def __init__(self, spec: TunnelSpec) -> None:
        self.spec = spec

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def status(self) -> TunnelStatus:
        port = self.spec.port
        if not port_open("127.0.0.1", port):
            return TunnelStatus(running=False, port=port, detail="端口未监听")

        pid = _read_pid()
        if pid is not None and _pid_alive(pid) and _pid_is_our_tunnel(pid, port):
            return TunnelStatus(running=True, port=port, pid=pid, owned=True)
        return TunnelStatus(running=True, port=port, pid=pid, owned=False)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def ensure(self, *, timeout: float | None = None) -> TunnelStatus:
        """端口已通就复用，否则建立。"""
        status = self.status()
        if status.running:
            if status.owned:
                logger.debug("复用已有 SSH 隧道（pid {}）", status.pid)
            else:
                logger.info("本地端口 {} 已有可用通道，直接复用", status.port)
            return status
        return self.start(timeout=timeout)

    def start(self, *, timeout: float | None = None) -> TunnelStatus:
        if self.status().running:
            return self.status()

        if not self.spec.has_password:
            raise TunnelError(
                "缺少 SSH 隧道密码",
                hint=f"在 .env 中设置 {PASSWORD_ENV}，或改用 OKX_PROXY 指向已有代理",
            )

        wait_seconds = timeout if timeout is not None else self.spec.timeout
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        KNOWN_HOSTS_FILE.touch(exist_ok=True)
        askpass = _ensure_askpass(self.spec.password)

        env = os.environ.copy()
        env["SSH_ASKPASS"] = str(askpass)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env[PASSWORD_ENV] = self.spec.password
        env.setdefault("DISPLAY", ":0")

        args = build_ssh_args(self.spec)
        logger.info("正在建立 SSH 隧道：{} → 本地 SOCKS5 端口 {}", self.spec.host, self.spec.port)

        with LOG_FILE.open("ab") as log_handle:
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=log_handle,
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise TunnelError(
                    "找不到 ssh 命令",
                    hint="安装 OpenSSH 客户端，或改用 OKX_PROXY 指向已有代理",
                ) from exc
            except OSError as exc:
                raise TunnelError(f"启动 ssh 失败: {exc}") from exc

        try:
            self._wait_ready(proc, wait_seconds)
        except TunnelError:
            self._terminate(proc.pid)
            _clear_pid()
            _cleanup_askpass()
            raise

        _write_pid(proc.pid)
        logger.success("SSH 隧道已就绪：127.0.0.1:{} (pid {})", self.spec.port, proc.pid)
        return TunnelStatus(running=True, port=self.spec.port, pid=proc.pid, owned=True)

    def _wait_ready(self, proc: subprocess.Popen, wait_seconds: float) -> None:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                message, hint = classify_failure(_tail_log())
                raise TunnelError(
                    f"{message}（ssh 退出码 {exit_code}）", hint=hint
                )
            if port_open("127.0.0.1", self.spec.port, timeout=0.3):
                return
            time.sleep(READY_POLL_INTERVAL)

        raise TunnelError(
            f"SSH 隧道在 {wait_seconds:.0f}s 内未就绪",
            hint="检查 .run/tunnel.log；若主机不支持端口转发，需换方案",
        )

    def stop(self) -> bool:
        """只终止本工具拉起的进程。"""
        pid = _read_pid()
        _clear_pid()
        _cleanup_askpass()

        if pid is None or not _pid_alive(pid):
            return False
        if not _pid_is_our_tunnel(pid, self.spec.port):
            logger.warning("pid {} 不是本工具的 ssh 隧道，跳过终止", pid)
            return False

        return self._terminate(pid)

    @staticmethod
    def _terminate(pid: int) -> bool:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            return False
        except OSError as exc:
            logger.warning("终止隧道进程 {} 失败：{}", pid, exc)
            return False

        for _ in range(25):
            if not _pid_alive(pid):
                return True
            time.sleep(0.2)

        # 进程可能已被回收但 kill(0) 仍返回成功，这里不再强杀，避免 pid 复用误伤
        logger.warning("隧道进程 {} 未在 5s 内退出", pid)
        return False
