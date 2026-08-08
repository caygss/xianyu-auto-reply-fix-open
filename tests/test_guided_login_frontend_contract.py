from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/guided-setup.css").read_text(encoding="utf-8")


def test_first_login_wizard_has_six_chinese_steps_and_accessible_status_region():
    expected_steps = ["准备登录", "扫码", "等待稳定", "人工验证或等待冷却", "连接成功", "验证可用"]

    assert 'id="guidedSetupPanel"' in INDEX
    assert 'id="guidedSetupSteps"' in INDEX
    assert 'id="guidedSetupMessage"' in INDEX
    assert 'aria-live="polite"' in INDEX
    assert 'aria-atomic="true"' in INDEX
    assert all(step in INDEX for step in expected_steps)


def test_wizard_reads_guided_status_and_uses_server_deadline_for_countdown():
    assert "/setup/status" in APP
    assert "/setup/action" in APP
    assert "guided_status" in APP
    assert "retry_at" in APP
    assert "Math.ceil" in APP
    assert "599.3" not in APP


def test_manual_takeover_is_gated_by_active_browser_signal():
    assert "manual_browser_session_status" in APP
    assert "vnc_manual_action_available" in APP
    assert "接管验证" in APP
    assert "保持浏览器窗口打开" in APP
    assert "不要重复扫码" in APP
    assert "冷却期间请等待" in APP


def test_wizard_has_clear_actions_and_responsive_styles():
    assert "guidedSetupPrimaryAction" in INDEX
    assert "guidedSetupSecondaryAction" in INDEX
    assert "type=\"button\"" in INDEX
    assert "@media (max-width: 768px)" in CSS
    assert "grid-template-columns: 1fr" in CSS


def test_guided_status_contract_exports_named_render_load_and_browser_helpers():
    assert "function renderGuidedSetupStatus" in APP
    assert "function loadGuidedSetupStatus" in APP
    assert "function isGuidedManualBrowserAvailable" in APP
    assert "function getGuidedManualPrimaryAction" in APP
    assert "module.exports" in APP


def test_manual_verification_has_only_one_visible_primary_action():
    assert "secondary.hidden = false" not in APP
    assert "getGuidedManualPrimaryAction(viewModel.contractAction, runtimeStatus, guidedStatus)" in APP


def test_waiting_states_use_server_actionability_and_keep_waiting_without_recheck_button():
    assert "showPrimaryAction" in APP
    assert "primary.hidden = !viewModel.showPrimaryAction" in APP
    assert "stepIndex + 3" not in APP
    assert "disconnected: 'reconnect_wait'" in APP
    assert "connection_unready: 'reconnect_wait'" in APP


def test_finish_action_waits_for_server_acceptance_before_hiding_wizard():
    assert "response?.success === true" in APP
    handler_start = APP.index("async function handleGuidedSetupAction")
    request_start = APP.index("beginGuidedSetupRequest()", handler_start)
    preflight = APP[handler_start:request_start]
    assert "toggleGuidedSetup(false)" not in preflight


def test_guided_status_load_uses_one_setup_status_snapshot_for_readiness():
    load_start = APP.index("async function loadGuidedSetupStatus")
    load_end = APP.index("async function handleGuidedSetupAction", load_start)
    load_block = APP[load_start:load_end]
    assert "GUIDED_SETUP_STATUS_ENDPOINT" in load_block
    assert "/cookies/details" not in load_block
    assert "runtime_ready" in APP
    assert "正在恢复连接" in APP
    assert "请等待" in APP


def test_ready_state_keeps_the_panel_visible_for_step_six_confirmation():
    assert "elements.panel.hidden = true" not in APP
    assert "验证可用" in APP
    assert "完成首次登录" in APP
