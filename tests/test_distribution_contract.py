import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "tools" / "build_windows_distribution.ps1"
GUIDE = REPO_ROOT / "docs" / "windows-distribution.md"
SOURCE_POINTER = REPO_ROOT / "SOURCE-CODE.md"
OPEN_SOURCE_GUIDE = REPO_ROOT / "docs" / "open-source-distribution.md"
LISTING_COPY = REPO_ROOT / "docs" / "xianyu-listing-copy.md"


EXCLUDED_ENTRIES = (
    ".git", "venv", ".venv", "data", "browser_data", "logs",
    "trajectory_history", ".deepeval", "__pycache__", ".pytest_cache",
    ".cache", "cache", ".playwright-browsers", "*.db", "*.sqlite*",
    "*.db-wal", "*.db-shm", "*.db-journal", "*.log", "*.key",
    "realtime.log", "dist",
)


def test_distribution_builder_uses_repo_relative_timestamped_staging_and_zip():
    text = BUILDER.read_text(encoding="utf-8")

    for phrase in (
        "$PSScriptRoot", "dist", "xianyu-auto-reply-fix-windows-", "timestamp",
        "Compress-Archive", "fileCount", "zipPath", "create_desktop_shortcut.ps1",
    ):
        assert phrase in text

    assert "C:\\Users\\" not in text
    assert "git push" not in text.lower()


def test_distribution_builder_contains_complete_runtime_and_sensitive_exclusions():
    text = BUILDER.read_text(encoding="utf-8")

    for entry in EXCLUDED_ENTRIES:
        assert entry in text, f"missing exclusion: {entry}"
    for phrase in (
        "staging", "Copy-Item", "sensitive", "clean", "Stop", "[guid]::NewGuid",
        "Resolve-Path", "GetFullPath", "StartsWith", "createdStaging", "PSIsContainer",
    ):
        assert phrase.lower() in text.lower()


def test_distribution_builder_validates_unique_owned_staging_before_cleanup():
    text = BUILDER.read_text(encoding="utf-8")

    assert "New-Item -ItemType Directory" in text
    assert "-Force" in text
    assert "owner" in text.lower()
    assert "Test-Path -LiteralPath $stagingRoot" in text
    assert "Remove-Item -LiteralPath $stagingRoot -Recurse -Force" in text
    assert "Test-Path -LiteralPath $ownershipMarker" in text


def test_distribution_builder_uses_compatible_file_creation_parameter():
    text = BUILDER.read_text(encoding="utf-8")

    assert not re.search(
        r"New-Item\b(?:(?!\r?\n).)*?-ItemType\s+File\b(?:(?!\r?\n).)*?-LiteralPath\b",
        text, flags=re.IGNORECASE,
    )
    assert not re.search(
        r"New-Item\b(?:(?!\r?\n).)*?-LiteralPath\b(?:(?!\r?\n).)*?-ItemType\s+File\b",
        text, flags=re.IGNORECASE,
    )


def test_distribution_guide_documents_distribution_build_command_and_output():
    text = GUIDE.read_text(encoding="utf-8")

    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\tools\\build_windows_distribution.ps1" in text
    assert "dist" in text


def test_distribution_guide_documents_recipient_and_github_workflows():
    text = GUIDE.read_text(encoding="utf-8")

    for phrase in (
        "首次安装闲鱼自动发货.bat", "启动闲鱼自动发货.bat", "扫码登录", "GitHub",
        "源码仓库", "提交到 Git", "自己的闲鱼账号", "本地数据", "enabled: false",
        "dry_run: true", "低价验收", "Docker", "Cookie", "Token", "密码", "验证码",
        "完整敏感网盘链接",
    ):
        assert phrase in text, f"missing guide phrase: {phrase}"

    assert "C:\\Users\\" not in text


def test_open_source_distribution_documents_allow_paid_distribution_and_redistribution():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (SOURCE_POINTER, OPEN_SOURCE_GUIDE, LISTING_COPY, GUIDE)
    )

    for phrase in ("AGPL-3.0", "修改", "再次分发", "源码", "编译", "Cookie", "授权码"):
        assert phrase in combined
    assert "不禁止买家" in combined
    assert "不禁止" in combined or "自由" in combined
    assert "仅限商业" not in combined


def test_source_pointer_declares_public_repository_and_release_fields():
    text = SOURCE_POINTER.read_text(encoding="utf-8")

    for phrase in (
        "https://github.com/caygss/xianyu-auto-reply-fix-open", "源码版本",
        "源码提交", "AGPL-3.0",
    ):
        assert phrase in text


def test_distribution_builder_targets_compiled_payload_and_source_pointer():
    text = BUILDER.read_text(encoding="utf-8").lower()

    for phrase in ("compiled", "source-code.md", "*.py", "executable", "sha256"):
        assert phrase in text
