from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tools" / "xianyu_auto_delivery.spec"
START = ROOT / "Start.py"
SHORTCUT = ROOT / "tools" / "create_desktop_shortcut.ps1"
DIST_BUILDER = ROOT / "tools" / "build_windows_distribution.ps1"


def test_compiled_spec_includes_runtime_javascript_files():
    text = SPEC.read_text(encoding="utf-8")

    assert '"gen_tfstk.js"' in text
    assert '"et_f.js"' in text
    assert '"utils"' in text


def test_frozen_entrypoint_anchors_relative_runtime_data_to_package_root():
    text = START.read_text(encoding="utf-8")

    assert "os.chdir(Path(os.environ['XIANYU_APP_ROOT']).resolve())" in text
    assert "_ensure_runtime_directories()" in text
    for directory in ("data", "logs", "browser_data", "trajectory_history"):
        assert f"Path('{directory}')" in text


def test_compiled_exe_opens_dashboard_but_batch_launcher_avoids_duplicate_browser():
    start_text = START.read_text(encoding="utf-8")
    launcher_text = (ROOT / "启动闲鱼自动发货.bat").read_text(encoding="utf-8-sig")

    assert "_open_compiled_dashboard_when_ready" in start_text
    assert "XIANYU_AUTO_OPEN_BROWSER" in start_text
    assert "XIANYU_AUTO_OPEN_BROWSER=0" in launcher_text


def test_shortcut_helper_selects_only_the_startup_launcher():
    text = SHORTCUT.read_text(encoding="utf-8")

    assert "start" in text.lower()
    assert "/min" in text
    assert "APP_EXE" in text
    assert "Expected exactly one launcher" in text


def test_distribution_contains_shortcut_helper_for_buyers():
    text = DIST_BUILDER.read_text(encoding="utf-8")

    assert "create_desktop_shortcut.ps1" in text
    assert 'Join-Path $stagingRoot "tools"' in text
