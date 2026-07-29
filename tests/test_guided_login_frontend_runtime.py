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
  { primary_action: 'refresh_status', step_index: 1, technical_status: 'password_login_backoff_wait' },
  { connection_state: 'connected', running: true, ws_ready: true, session_ready: true, has_current_token: true, message_stream_ready: true },
);
assert.equal(wait.step, 4);
assert.equal(wait.action, 'wait');

const qrWait = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 1, technical_status: 'qr_login_grace_wait' },
  { connection_state: 'connected' },
);
assert.equal(qrWait.step, 3);
assert.equal(qrWait.action, 'wait');

const reconnecting = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 1, technical_status: 'reconnecting' },
  { connection_state: 'connected' },
);
assert.equal(reconnecting.step, 5);
assert.equal(reconnecting.action, 'wait');

const manual = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'open_manual_verification', step_index: 1, technical_status: 'verification_pending_manual', manual_browser_available: false },
  { connection_state: 'disconnected', manual_browser_session_status: null, vnc_manual_action_available: false },
);
assert.equal(manual.step, 4);
assert.equal(manual.action, 'open_manual_verification');

const error = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'refresh_status', step_index: 1, technical_status: 'token_refresh_failed' },
  { connection_state: 'connected' },
);
assert.equal(error.step, 4);
assert.equal(error.action, 'refresh_status');

const ready = guided.getGuidedSetupStatusViewModel(
  { primary_action: 'finish', step_index: 3, technical_status: 'connected' },
  { connection_state: 'connected', running: true, ws_ready: true, session_ready: true, has_current_token: true, message_stream_ready: true },
);
assert.equal(ready.step, 6);
assert.equal(ready.action, 'finish_local');
assert.equal(ready.ready, true);

assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: true, manual_browser_session_status: 'processing' },
  { manual_browser_available: true },
), true);
assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: true, manual_browser_session_status: null },
  { manual_browser_available: true },
), false);
assert.equal(guided.isGuidedManualBrowserAvailable(
  { vnc_manual_action_available: true, manual_browser_session_status: 'active' },
), true);
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
