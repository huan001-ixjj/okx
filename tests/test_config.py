"""配置加载与凭据校验测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from okxquant.config import bar_to_ms, load_config, load_credentials
from okxquant.errors import ConfigError


def test_builtin_defaults_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.market.inst_id == "BTC-USDT"
    assert cfg.market.bar == "1m"
    assert cfg.risk.allow_live is False
    assert cfg.bar_ms == 60_000


def test_explicit_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load_config(tmp_path / "nope.toml")


def test_repo_config_loads() -> None:
    cfg = load_config(Path("config/default.toml"))
    assert cfg.market.inst_id == "BTC-USDT"
    assert 0 < cfg.execution.fee_rate < 0.01


def test_invalid_bar_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[market]\nbar = "2x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_invalid_toml_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[market\nbar = 1m", encoding="utf-8")
    with pytest.raises(ConfigError, match="格式错误"):
        load_config(path)


def test_bar_to_ms() -> None:
    assert bar_to_ms("1m") == 60_000
    assert bar_to_ms("1H") == 3_600_000
    assert bar_to_ms("1D") == 86_400_000


def test_credentials_missing_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # 屏蔽 .env 加载：否则工作目录里存在 .env 时，凭据会被重新读进来，
    # 结果取决于运行环境而不是被测逻辑。
    monkeypatch.setattr("okxquant.config.load_dotenv", lambda **_: None)
    for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    assert load_credentials() is None


def test_credentials_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("okxquant.config.load_dotenv", lambda **_: None)
    for name in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="缺少 API 凭据"):
        load_credentials(required=True)


def test_credentials_demo_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_API_KEY", "abcdefghijk")
    monkeypatch.setenv("OKX_API_SECRET", "secret")
    monkeypatch.setenv("OKX_PASSPHRASE", "pass")
    monkeypatch.setenv("OKX_FLAG", "1")
    cred = load_credentials(required=True)
    assert cred is not None
    assert cred.is_demo
    assert cred.environment_label == "SIMULATED TRADING"
    assert cred.masked()["passphrase"] == "***"
    assert cred.masked()["api_key"] == "abcd***hijk"


def test_credentials_reject_bad_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_API_SECRET", "s")
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    monkeypatch.setenv("OKX_FLAG", "2")
    with pytest.raises(ConfigError, match="OKX_FLAG"):
        load_credentials(required=True)
