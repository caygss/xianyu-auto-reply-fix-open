from pathlib import Path

import yaml

import config
from XianyuAutoAsync import XianyuLive


ROOT = Path(__file__).resolve().parents[1]
GLOBAL_CONFIG = ROOT / "global_config.yml"
REPLY_SERVER = ROOT / "reply_server.py"
XIANYU_ASYNC = ROOT / "XianyuAutoAsync.py"
WINDOWS_GUIDE = ROOT / "docs" / "windows-distribution.md"


def test_qr_login_grace_defaults_to_three_minutes(monkeypatch):
    settings = yaml.safe_load(GLOBAL_CONFIG.read_text(encoding="utf-8"))
    assert settings["RISK_CONTROL"]["qr_login_grace_minutes"] == 3

    get_minutes = getattr(config, "get_qr_login_grace_minutes", None)
    assert callable(get_minutes)
    monkeypatch.setitem(config.RISK_CONTROL, "qr_login_grace_minutes", 3)

    assert get_minutes() == 3
    assert XianyuLive.get_qr_login_grace_ttl_seconds() == 180


def test_qr_login_grace_allows_a_one_minute_minimum(monkeypatch):
    get_minutes = getattr(config, "get_qr_login_grace_minutes", None)
    assert callable(get_minutes)
    monkeypatch.setitem(config.RISK_CONTROL, "qr_login_grace_minutes", 1)

    assert get_minutes() == 1
    assert XianyuLive.get_qr_login_grace_ttl_seconds() == 60


def test_qr_login_paths_share_the_central_grace_policy():
    reply_source = REPLY_SERVER.read_text(encoding="utf-8")
    async_source = XIANYU_ASYNC.read_text(encoding="utf-8")

    assert "qr_login_grace_minutes = get_qr_login_grace_minutes()" in reply_source
    assert "get_qr_login_grace_seconds()" in async_source
    assert "qr_login_grace_minutes = max(5" not in reply_source


def test_windows_guide_explains_hidden_terminal_and_short_grace_period():
    guide = WINDOWS_GUIDE.read_text(encoding="utf-8")

    assert "不显示终端窗口" in guide
    assert "3 分钟" in guide
