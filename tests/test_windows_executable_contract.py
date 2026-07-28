from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PROJECT_ROOT / "tools" / "build_windows_executable.ps1"
SPEC_FILE = PROJECT_ROOT / "tools" / "xianyu_auto_delivery.spec"


def test_compiled_build_files_exist():
    assert BUILD_SCRIPT.is_file()
    assert SPEC_FILE.is_file()


def test_compiled_build_uses_frozen_entrypoint_and_bundled_assets():
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    spec = SPEC_FILE.read_text(encoding="utf-8")

    for phrase in ("Start.py", "PyInstaller", "onedir", "static", "global_config.yml", "announcement.json"):
        assert phrase.lower() in (script + spec).lower()

    assert "playwright" in (script + spec).lower()
    assert "data" in script.lower()
    assert "browser_data" in script.lower()
    assert "logs" in script.lower()
    assert "venv" in script.lower()


def test_compiled_build_rejects_source_and_runtime_files_from_final_package():
    script = BUILD_SCRIPT.read_text(encoding="utf-8").lower()

    for phrase in ("*.py", "*.db", "*.log", "browser_data", "venv", "cookie", "token"):
        assert phrase in script

    assert "sha256" in script or "get-filehash" in script
