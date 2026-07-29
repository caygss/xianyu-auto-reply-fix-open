from __future__ import annotations

import re
from pathlib import Path

import yaml

from Start import load_republish_config


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
WINDOWS_GUIDE = ROOT / "docs" / "windows-distribution.md"
RUNBOOK = ROOT / "docs" / "windows-republish-runbook.md"


def _runbook_text() -> str:
    assert RUNBOOK.exists(), "Windows runbook must exist"
    return RUNBOOK.read_text(encoding="utf-8")


def _windows_documentation() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (README, WINDOWS_GUIDE, RUNBOOK)
    )


def test_windows_docs_explain_single_user_first_use_and_isolation() -> None:
    text = _windows_documentation()
    required_phrases = (
        "XianyuAutoDelivery.exe",
        "default administrator account",
        "admin123",
        "first login",
        "SMTP",
        "ordinary user",
        "registration is permanently disabled",
        "one computer",
        "one installation directory",
        "data/",
        "browser_data/",
        "logs/",
        "Do not copy",
        "two instances",
        "automatic delivery",
        "publish",
        "republish",
    )
    for phrase in required_phrases:
        assert phrase in text, f"Windows docs missing required guidance: {phrase}"


def test_runbook_covers_safe_windows_setup_and_operations() -> None:
    text = _runbook_text()
    required_phrases = (
        "PowerShell",
        "python -m venv",
        "Activate.ps1",
        "pip install",
        "playwright install chromium",
        "python Start.py",
        "\u626b\u7801",
        "\u540c\u6b65\u5546\u54c1",
        "\u9ed8\u8ba4\u56fa\u5b9a\u7f51\u76d8\u94fe\u63a5",
        "SKU",
        "enabled: false",
        "dry_run: true",
        "\u4f4e\u4ef7",
        "\u53ea\u5165\u961f",
        "\u6f14\u7ec3",
        "REPUBLISH.enabled=true",
        "dry_run=false",
        "\u6682\u505c",
        "\u6062\u590d",
        "\u7acb\u5373\u68c0\u67e5",
        "\u4eba\u5de5\u5904\u7406",
        "data/xianyu_data.db",
        "SQLite",
        "ItemPublisher",
        "\u65b0\u53d1\u5e03",
        "\u65b0 ID",
        "Cookie",
        "Token",
        "\u9a8c\u8bc1\u7801",
        "\u56de\u6eda",
    )
    for phrase in required_phrases:
        assert phrase in text, f"runbook missing required guidance: {phrase}"


def test_runbook_does_not_offer_unsafe_docker_or_shell_download_steps() -> None:
    text = _runbook_text()
    assert "\u4e0d\u4f7f\u7528 Docker" in text or "\u4e0d\u7528 Docker" in text

    fenced_blocks = re.findall(r"```(?:powershell|pwsh|shell|bash)?\s*\n(.*?)```", text, re.I | re.S)
    commands = "\n".join(fenced_blocks).lower()
    for unsafe in ("docker ", "docker-compose", "curl ", "wget ", "invoke-webrequest", " iwr ", "bash ", "sh "):
        assert unsafe not in commands, f"unsafe command found in runbook: {unsafe.strip()}"


def test_global_config_keeps_republish_opt_in_defaults() -> None:
    config = yaml.safe_load((ROOT / "global_config.yml").read_text(encoding="utf-8"))
    republish = config["REPUBLISH"]
    assert republish["enabled"] is False
    assert republish["dry_run"] is True
    assert load_republish_config({}) == {
        "enabled": False,
        "dry_run": True,
        "check_interval_seconds": 30.0,
        "delay_seconds": 300.0,
        "max_retries": 3,
        "retry_backoff_seconds": (300.0, 900.0, 1800.0),
        "account_id": "",
    }


def test_start_reads_republish_block_and_requires_single_account() -> None:
    source = (ROOT / "Start.py").read_text(encoding="utf-8")
    assert "global_config.get('REPUBLISH', {})" in source
    assert "if republish_settings['enabled']:" in source
    assert "len(enabled_cookies) != 1" in source


def test_runbook_explains_enabled_dry_run_transition_and_disabled_behavior() -> None:
    text = _runbook_text()
    required_phrases = (
        "演练前先备份",
        "enabled: true",
        "dry_run: true",
        "重启 Start.py",
        "只记录计划",
        "不真实发货/发布",
        "低价验收通过后",
        "dry_run: false",
        "恢复 enabled: false",
        "enabled: false 不会启动补发运行时",
    )
    for phrase in required_phrases:
        assert phrase in text, f"runbook missing transition guidance: {phrase}"
