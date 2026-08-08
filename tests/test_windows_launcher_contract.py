from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PROJECT_ROOT / "\u9996\u6b21\u5b89\u88c5\u95f2\u9c7c\u81ea\u52a8\u53d1\u8d27.bat"
LAUNCHER = PROJECT_ROOT / "\u542f\u52a8\u95f2\u9c7c\u81ea\u52a8\u53d1\u8d27.bat"
SHORTCUT = PROJECT_ROOT / "tools" / "create_desktop_shortcut.ps1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_windows_launcher_files_exist():
    assert INSTALLER.is_file()
    assert LAUNCHER.is_file()
    assert SHORTCUT.is_file()


def test_batch_launchers_use_cmd_compatible_crlf_line_endings():
    for batch_file in (INSTALLER, LAUNCHER):
        raw = batch_file.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")


def test_batch_scripts_resolve_compiled_package_root_relative_to_their_location():
    installer = _read(INSTALLER)
    launcher = _read(LAUNCHER)

    assert "%~dp0" in installer
    assert "%~dp0" in launcher
    assert "C:\\Users\\" not in installer
    assert "C:\\Users\\" not in launcher
    assert "XianyuAutoDelivery.exe" in installer
    assert "XianyuAutoDelivery.exe" in launcher
    assert "requirements.txt" not in installer
    assert "Start.py" not in launcher
    assert "python" in installer.lower()
    assert "node.js" in installer.lower()
    assert "chromium" in installer.lower()
    assert "127.0.0.1:8090" in launcher


def test_installer_checks_compiled_payload_and_pauses_after_failures():
    installer = _read(INSTALLER).lower()

    assert "xianyuautodelivery.exe" in installer
    assert "writable" in installer
    assert "errorlevel" in installer or "if errorlevel" in installer
    assert "pause" in installer


def test_launcher_checks_compiled_executable_and_may_minimize_service_window():
    launcher = _read(LAUNCHER).lower()

    assert "if not exist" in launcher
    assert "xianyuautodelivery.exe" in launcher
    assert "start /min" in launcher or "start \"\" /min" in launcher
    assert "start \"\" http://127.0.0.1:8090" in launcher
    assert "pause" in launcher


def test_launcher_health_checks_existing_service_and_detects_port_conflicts():
    launcher = _read(LAUNCHER).lower()

    assert "invoke-webrequest" in launcher
    assert "127.0.0.1:8090/health" in launcher
    assert "already running" in launcher
    assert "port" in launcher and "conflict" in launcher
    assert "netstat" in launcher or "get-nettcpconnection" in launcher


def test_launcher_opens_browser_only_after_health_check_succeeds():
    launcher = _read(LAUNCHER).lower()
    lines = [line.strip() for line in launcher.splitlines() if line.strip()]

    ready_label = lines.index(":service_ready")
    browser_line = next(
        index
        for index, line in enumerate(lines)
        if index > ready_label and line == 'start "" http://127.0.0.1:8090'
    )
    assert any("wait_for_service" in line for line in lines[:browser_line])
    assert any(
        "health check failed" in line
        or "failed to start" in line
        or "did not become ready" in line
        for line in lines
    )
    assert "timeout" in launcher
    assert "pause" in launcher


def test_installer_creates_runtime_directories_without_deleting_user_data():
    installer = _read(INSTALLER).lower()

    assert "data" in installer
    assert "logs" in installer
    assert "browser_data" in installer
    assert "mkdir" in installer
    assert "delete" not in installer
    assert "rmdir" not in installer
    assert "del " not in installer


def test_shortcut_uses_direct_executable_and_writes_only_to_current_users_desktop():
    shortcut = _read(SHORTCUT)

    assert "$PSScriptRoot" in shortcut
    assert "XianyuAutoDelivery.exe" in shortcut
    assert "$link.TargetPath = $executablePath" in shortcut
    assert "*.bat" not in shortcut
    assert "Get-ChildItem" not in shortcut
    assert "CreateShortcut" in shortcut
    assert "Desktop" in shortcut
    assert "WorkingDirectory" in shortcut
    assert "C:\\Users\\" not in shortcut
    assert ".lnk" in shortcut
