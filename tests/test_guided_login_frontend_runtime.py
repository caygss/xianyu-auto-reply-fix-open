from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_guided_status_view_model_and_deadline_use_real_server_values():
    script = r"""
const assert = require('node:assert/strict');
global.localStorage = { getItem: () => '', setItem: () => {}, removeItem: () => {} };
global.location = { origin: 'http://test', hostname: 'test' };
global.window = { location: global.location, open: () => null, addEventListener: () => {} };
global.document = {
  addEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, appendChild: () => {}, setAttribute: () => {} }),
  getElementsByTagName: () => [],
  head: { appendChild: () => {} },
  body: { appendChild: () => {} },
};
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });

const guided = require('./static/js/app.js');
const now = 1700000000000;
assert.equal(guided.getGuidedDeadlineSeconds(1700000010, now), 10);
assert.equal(guided.getGuidedDeadlineSeconds(1700000010, now + 10000), 0);

const wait = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 4, technical_status: 'password_login_backoff_wait', needs_user_action: false },
  { connection_state: 'connected', running: true, ws_ready: true, session_ready: true, has_current_token: true, message_stream_ready: true },
);
assert.equal(wait.step, 4);
assert.equal(wait.action, 'refresh_status');
assert.equal(wait.showPrimaryAction, false);

const primaryActionWins = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'open_manual_verification', step_index: 4, technical_status: 'password_login_backoff_wait', needs_user_action: true },
  { connection_state: 'connected', running: true, ws_ready: true, session_ready: true, has_current_token: true, message_stream_ready: true },
);
assert.equal(primaryActionWins.action, 'open_manual_verification');

const refreshActionWins = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 4, technical_status: 'verification_pending_manual', needs_user_action: true },
  { connection_state: 'disconnected' },
);
assert.equal(refreshActionWins.action, 'refresh_status');

const stepIndexWins = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 6, technical_status: 'token_refresh_failed' },
  { connection_state: 'connected', message_stream_ready: true },
);
assert.equal(stepIndexWins.step, 6);
assert.equal(stepIndexWins.action, 'refresh_status');
assert.equal(stepIndexWins.ready, false);

const qrWait = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 3, technical_status: 'qr_login_grace_wait', needs_user_action: false },
  { connection_state: 'connected' },
);
assert.equal(qrWait.step, 3);
assert.equal(qrWait.action, 'refresh_status');

const reconnecting = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 5, technical_status: 'reconnecting', needs_user_action: false },
  { connection_state: 'connected' },
);
assert.equal(reconnecting.step, 5);
assert.equal(reconnecting.action, 'refresh_status');
assert.equal(reconnecting.mode, 'reconnect_wait');
assert.equal(reconnecting.showPrimaryAction, false);

const disconnected = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 5, technical_status: 'disconnected', needs_user_action: false },
  { connection_state: 'disconnected' },
);
assert.equal(disconnected.step, 5);
assert.equal(disconnected.mode, 'reconnect_wait');
assert.equal(disconnected.showPrimaryAction, false);

const connectionUnready = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 5, technical_status: 'connection_unready', needs_user_action: false },
  { connection_state: 'connection_unready' },
);
assert.equal(connectionUnready.step, 5);
assert.equal(connectionUnready.mode, 'reconnect_wait');
assert.equal(connectionUnready.showPrimaryAction, false);

const runtimeMessageStreamUnready = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
  { connection_state: 'connected', message_stream_status: 'healthy', message_stream_ready: false },
);
assert.equal(runtimeMessageStreamUnready.step, 5);
assert.equal(runtimeMessageStreamUnready.mode, 'reconnect_wait');
assert.equal(runtimeMessageStreamUnready.showPrimaryAction, false);
assert.equal(runtimeMessageStreamUnready.ready, false);

const runtimeStatusUnready = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
  { connection_state: 'connected', message_stream_status: 'connection_unready', message_stream_ready: true },
);
assert.equal(runtimeStatusUnready.step, 5);
assert.equal(runtimeStatusUnready.mode, 'reconnect_wait');
assert.equal(runtimeStatusUnready.showPrimaryAction, false);

for (const messageStreamStatus of ['Recovering', 'SUSPECTED-STALE']) {
  const compatibilityState = guided.getGuidedSetupStatusViewModel(
    { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
    { connection_state: 'connected', message_stream_status: messageStreamStatus, message_stream_ready: 'true' },
  );
  assert.equal(compatibilityState.step, 5);
  assert.equal(compatibilityState.mode, 'reconnect_wait');
  assert.equal(compatibilityState.showPrimaryAction, false);
}

const stringMessageStreamReady = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
  { connection_state: 'connected', message_stream_status: 'healthy', message_stream_ready: 'FALSE' },
);
assert.equal(stringMessageStreamReady.step, 5);
assert.equal(stringMessageStreamReady.mode, 'reconnect_wait');
assert.equal(stringMessageStreamReady.showPrimaryAction, false);

const missingMessageStreamReady = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
  { connection_state: 'connected', message_stream_status: 'healthy' },
);
assert.equal(missingMessageStreamReady.step, 5);
assert.equal(missingMessageStreamReady.mode, 'reconnect_wait');
assert.equal(missingMessageStreamReady.showPrimaryAction, false);

const missingMessageStreamReadyFromStatus = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
  { status: 'CONNECTED', message_stream_status: 'healthy' },
);
assert.equal(missingMessageStreamReadyFromStatus.step, 5);
assert.equal(missingMessageStreamReadyFromStatus.mode, 'reconnect_wait');
assert.equal(missingMessageStreamReadyFromStatus.showPrimaryAction, false);

const manual = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'open_manual_verification', step_index: 4, technical_status: 'verification_pending_manual', manual_browser_available: false, needs_user_action: true },
  { connection_state: 'connected', manual_browser_session_status: null, vnc_manual_action_available: false },
);
assert.equal(manual.step, 4);
assert.equal(manual.action, 'open_manual_verification');

const error = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 4, technical_status: 'token_refresh_failed', needs_user_action: true },
  { connection_state: 'connected' },
);
assert.equal(error.step, 4);
assert.equal(error.action, 'refresh_status');

const ready = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, technical_status: 'connected' },
  { connection_state: 'connected', running: true, ws_ready: true, session_ready: true, has_current_token: true, message_stream_ready: true },
);
assert.equal(ready.step, 6);
assert.equal(ready.action, 'finish');
assert.equal(ready.ready, true);

const noItemsCopy = guided.getGuidedSetupCopy(
  5,
  { guidedStatus: { step_id: 'no_items', title: '先准备商品', message: '请先同步商品。' } },
  { action: 'go_to_item_management' },
);
assert.equal(noItemsCopy.title, '先准备商品');
assert.match(noItemsCopy.message, /同步商品/);

const republishCopy = guided.getGuidedSetupCopy(
  5,
  { guidedStatus: { step_id: 'republish_config', title: '开启自动重新上架', message: '请开启自动重新上架。' } },
  { action: 'go_to_republish_config' },
);
assert.equal(republishCopy.title, '开启自动重新上架');
assert.match(republishCopy.message, /自动重新上架/);

const setupSnapshotReady = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, runtime_ready: true, technical_status: 'connected' },
  null,
);
assert.equal(setupSnapshotReady.step, 6);
assert.equal(setupSnapshotReady.showPrimaryAction, true);
assert.equal(setupSnapshotReady.ready, true);

const setupSnapshotUnavailable = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 6, runtime_ready: false, technical_status: 'connected' },
  null,
);
assert.equal(setupSnapshotUnavailable.step, 5);
assert.equal(setupSnapshotUnavailable.showPrimaryAction, false);
assert.equal(setupSnapshotUnavailable.ready, false);

for (const missingRuntime of [null, {}, { connection_state: 'connected' }]) {
  const unavailable = guided.getGuidedSetupStatusViewModel(
    { primary_action: 'finish', step_index: 6, technical_status: 'connected', needs_user_action: false },
    missingRuntime,
  );
  assert.equal(unavailable.step, 5);
  assert.equal(unavailable.mode, 'reconnect_wait');
  assert.equal(unavailable.showPrimaryAction, false);
  assert.equal(unavailable.ready, false);
}

const accounts = [
  {
    id: 'stale-finish',
    guidedStatus: { primary_action: 'finish', step_index: 6 },
    runtimeStatus: null,
  },
  {
    id: 'needs-setup',
    guidedStatus: { primary_action: 'refresh_status', step_index: 5 },
    runtimeStatus: { connection_state: 'reconnecting' },
  },
];
assert.equal(guided.selectGuidedSetupAccount(accounts, '').id, 'needs-setup');
assert.equal(guided.isGuidedRuntimeReady(null), false);
assert.equal(guided.isGuidedRuntimeReady({ connection_state: 'connected' }), false);

assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: true, manual_browser_session_status: 'processing' },
  { manual_browser_available: true },
), true);
assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: true, manual_browser_session_status: null },
  { manual_browser_available: true },
), true);
assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: false, manual_browser_session_status: 'active' },
  { manual_browser_available: false },
), true);
assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: false, manual_browser_session_status: 'false' },
  { manual_browser_available: true },
), true);
assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: true, manual_browser_session_status: 'active' },
), true);

assert.equal(guided.getGuidedManualPrimaryAction(
  'open_manual_verification',
  { vnc_manual_action_available: true, manual_browser_session_status: 'active' },
), 'takeover_browser');
assert.equal(guided.getGuidedManualPrimaryAction(
  'complete_manual_verification',
  { vnc_manual_action_available: true, manual_browser_session_status: 'active' },
), 'complete_manual_verification');
assert.equal(guided.getGuidedManualPrimaryAction(
  'open_manual_verification',
  { vnc_manual_action_available: false, manual_browser_session_status: null },
), 'open_manual_verification');
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_guided_navigation_requires_success_action_and_current_server_transition():
    script = r"""
const assert = require('node:assert/strict');
global.localStorage = { getItem: () => '', setItem: () => {}, removeItem: () => {} };
global.location = { origin: 'http://test', hostname: 'test' };
global.window = { location: global.location, open: () => null, addEventListener: () => {}, setTimeout: () => 0 };
global.document = {
  addEventListener: () => {}, getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], createElement: () => ({ style: {}, appendChild: () => {}, setAttribute: () => {} }),
  getElementsByTagName: () => [], head: { appendChild: () => {} }, body: { appendChild: () => {} },
};
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
const guided = require('./static/js/app.js');
const action = 'go_to_delivery_config';
const valid = { success: true, action, guided_status: { primary_action: action } };
assert.equal(guided.shouldNavigateGuidedSetupAction(valid, action), true);
assert.equal(guided.shouldNavigateGuidedSetupAction({ ...valid, success: false }, action), false);
assert.equal(guided.shouldNavigateGuidedSetupAction({ ...valid, action: 'go_to_item_management' }, action), false);
assert.equal(guided.shouldNavigateGuidedSetupAction({ ...valid, guided_status: { primary_action: 'refresh_status' } }, action), false);
assert.equal(guided.shouldNavigateGuidedSetupAction(valid, 'finish'), false);
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_guided_target_row_matches_account_and_item_together():
    script = r"""
const assert = require('node:assert/strict');
const rows = [
  { dataset: { guidedItemCookieId: 'account-a', guidedItemId: 'same-item' } },
  { dataset: { guidedItemCookieId: 'account-b', guidedItemId: 'same-item' } },
];
global.localStorage = { getItem: () => '', setItem: () => {}, removeItem: () => {} };
global.location = { origin: 'http://test', hostname: 'test' };
global.window = { location: global.location, open: () => null, addEventListener: () => {} };
global.document = {
  addEventListener: () => {}, getElementById: () => null, querySelector: () => null,
  querySelectorAll: selector => selector.includes('data-guided-item-id') ? rows : [],
  createElement: () => ({ style: {}, appendChild: () => {}, setAttribute: () => {} }),
  getElementsByTagName: () => [], head: { appendChild: () => {} }, body: { appendChild: () => {} },
};
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
const guided = require('./static/js/app.js');
assert.equal(
  guided.findGuidedSetupTargetRow({ cookie_id: 'account-b', item_id: 'same-item' }),
  rows[1],
);
assert.equal(guided.findGuidedSetupTargetRow({ cookie_id: 'account-c', item_id: 'same-item' }), null);
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
