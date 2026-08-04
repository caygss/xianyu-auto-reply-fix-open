from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/guided-setup.css").read_text(encoding="utf-8") if (ROOT / "static/css/guided-setup.css").exists() else ""


REQUIRED_IDS = {
    "guidedSetupPanel",
    "guidedSetupStep",
    "guidedSetupTitle",
    "guidedSetupMessage",
    "guidedSetupPrimaryAction",
    "guidedSetupCountdown",
    "guidedSetupTechnicalDetails",
    "guidedSetupSteps",
    "guidedSetupAccount",
}


def test_guided_setup_dom_contract_is_present_in_account_page():
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in INDEX
    assert '/static/css/guided-setup.css' in INDEX
    assert 'aria-live="polite"' in INDEX
    assert 'aria-live="assertive"' in INDEX
    assert 'aria-label="首次登录向导"' in INDEX


def test_guided_setup_exposes_render_and_action_functions():
    for function_name in (
        "renderGuidedSetupStatus",
        "loadGuidedSetupStatus",
        "handleGuidedSetupAction",
        "isGuidedManualBrowserAvailable",
    ):
        assert re.search(rf"function\s+{function_name}\s*\(", APP)


def test_guided_setup_copy_is_chinese_and_task_focused():
    for phrase in (
        "准备登录",
        "扫码",
        "等待稳定",
        "人工验证",
        "连接成功",
        "验证可用",
        "自动验证没有完成",
        "不要重复扫码",
        "请保持浏览器窗口打开",
        "当前无需操作",
    ):
        assert phrase in INDEX or phrase in APP
    assert "599.3" not in INDEX
    assert "599.3" not in APP


def test_guided_setup_uses_server_deadline_and_refreshes_after_countdown():
    assert "retry_at" in APP
    assert "remaining_seconds" in APP
    assert re.search(r"setInterval\([^\n]*1000", APP)
    assert re.search(r"remainingSeconds\s*<=\s*0", APP)
    assert "loadGuidedSetupStatus" in APP


def test_manual_takeover_requires_an_active_browser_signal():
    assert "vnc_manual_action_available" in APP
    assert "manual_browser_session_status" in APP
    assert re.search(
        r"function\s+isGuidedManualBrowserAvailable[\s\S]{0,700}vnc_manual_action_available",
        APP,
    )
    assert re.search(
        r"function\s+isGuidedManualBrowserAvailable[\s\S]{0,700}manual_browser_session_status",
        APP,
    )


def test_guided_setup_styles_include_focus_and_mobile_layout():
    assert ":focus-visible" in CSS
    assert "@media" in CSS
    assert "guided-setup__steps" in CSS
    assert "guided-setup__actions" in CSS
