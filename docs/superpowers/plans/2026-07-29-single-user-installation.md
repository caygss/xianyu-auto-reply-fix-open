# Single-User Windows Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each distributed Windows package operate as one local installation for one buyer on one PC: the package opens directly with the existing local admin account, does not require SMTP or public registration, and keeps all runtime data inside that installation directory.

**Architecture:** Keep the existing local authentication, database schema, `user_id` fields, email-login compatibility, and registration routes. Change the distribution default and startup migration so `registration_enabled` is always `false`; enforce that value at the backend boundary; hide registration in the login UI and make the admin setting visibly read-only. Do not introduce a cloud account service, cross-device synchronization, licensing server, or new data store.

**Tech Stack:** Python 3.12, SQLite through `DBManager`, FastAPI route handlers in `reply_server.py`, static HTML/JavaScript, pytest source-contract tests, PyInstaller/ZIP distribution checks.

---

### Task 1: Add failing regression tests for single-user mode

**Files:**
- Create: `tests/test_single_user_mode_contract.py`
- Modify: `.gitignore` only if needed to force-track this test file

- [ ] **Step 1: Write the database-default test**

Instantiate `DBManager` against a `tmp_path` database and assert that a brand-new installation returns `'false'` for `registration_enabled`. Use the existing manager cleanup pattern so the test does not touch the repository database.

- [ ] **Step 2: Write the existing-database migration test**

Create a temporary database through `DBManager`, explicitly set `registration_enabled` to `'true'`, close it, reopen it, and assert the startup migration changes only that setting to `'false'`. Also assert that the pre-existing admin user and a representative item/order record remain present; the test must prove that closing registration is not a data reset.

- [ ] **Step 3: Write backend contract tests**

Exercise the source-level or isolated route behavior already used by this repository’s tests and cover these cases: `/registration-status` reports disabled when the setting is absent or a database read fails; `PUT /registration-settings` cannot enable registration; `GET /register.html` and `POST /register` return the existing closed-registration response without invoking SMTP/email-code verification. Keep the tests independent from an external email service.

- [ ] **Step 4: Write static UI contract tests**

Read `static/login.html`, `static/index.html`, and `static/js/app.js` as text and assert that login registration is hidden by default, a failed status request does not reveal it, and the administrator page labels the registration control as permanently disabled/read-only rather than offering a working enable switch.

- [ ] **Step 5: Run the new tests and confirm the intentional red state**

Run `pytest tests/test_single_user_mode_contract.py -q`. The tests should fail against the current `true` defaults, permissive admin toggle, and login fallback. Record the failure categories in the implementation handoff before changing production code.

### Task 2: Enforce registration-off at database and API boundaries

**Files:**
- Modify: `db_manager.py`
- Modify: `reply_server.py`
- Test: `tests/test_single_user_mode_contract.py`

- [ ] **Step 1: Change the new-database default**

Change the `system_settings` seed value for `registration_enabled` from `'true'` to `'false'`. Keep the existing setting key and description so old databases remain compatible.

- [ ] **Step 2: Add an idempotent startup migration**

Inside the existing transactional `_migrate_database` flow, execute an update that sets `registration_enabled` to `'false'` for every startup. Do not drop tables, delete rows, alter the admin password, or remove existing `user_id` relationships. The update must be safe on repeated launches.

- [ ] **Step 3: Make public status fail closed**

Change `get_registration_status()` so a missing setting and an exception both resolve to `enabled: false` with a closed-registration message. This prevents a temporary database/API problem from exposing the registration link.

- [ ] **Step 4: Make the admin update endpoint non-enablable**

Retain `PUT /registration-settings` for frontend/API compatibility, but normalize every requested value to `'false'`, persist the closed state, and return `enabled: false`. Log that the single-user distribution mode rejected an enable request. Existing authorized administrators can still confirm the state, but cannot reopen public registration by accident or by direct API call.

- [ ] **Step 5: Keep registration routes closed before SMTP work**

Preserve the existing early registration checks in `register_page()` and `register()`. Ensure the closed branch is reached before email-code verification or any SMTP-dependent operation and uses a stable user-facing message.

- [ ] **Step 6: Run focused tests and relevant regressions**

Run `pytest tests/test_single_user_mode_contract.py tests/test_db_manager_logging.py tests/test_republish_*.py tests/test_delivery_republish_hook.py -q`. Fix only failures caused by the single-user change; do not relax the registration-off assertions to accommodate old behavior.

### Task 3: Make the UI match the one-user workflow

**Files:**
- Modify: `static/login.html`
- Modify: `static/index.html`
- Modify: `static/js/app.js`
- Test: `tests/test_single_user_mode_contract.py`

- [ ] **Step 1: Hide registration by default in the login page**

Set `registerSection` to hidden in the initial markup. Keep the existing closed-registration route as a defensive fallback for old bookmarks and direct requests.

- [ ] **Step 2: Make registration status errors fail closed**

Update `checkRegistrationStatus()` so non-OK responses and exceptions explicitly hide `registerSection`; remove the current “error means show registration” behavior.

- [ ] **Step 3: Replace the admin registration switch with an explanatory read-only state**

Update the login/registration settings card in `static/index.html` to explain that the distributed package is single-user and public registration is closed. Disable or remove only the registration toggle; leave unrelated login-info and captcha settings working.

- [ ] **Step 4: Stop the frontend from attempting to enable registration**

Adjust `loadRegistrationSettings()` and `updateLoginInfoSettings()` so the displayed state is always off and saving unrelated settings does not send an enable request. Keep error handling for the remaining settings and preserve the existing admin authorization behavior.

- [ ] **Step 5: Verify frontend syntax and contracts**

Run `node --check static/js/app.js` and `pytest tests/test_single_user_mode_contract.py tests/test_republish_ui.py -q`. Confirm the UI still exposes the normal admin dashboard, item/SKU cloud-link configuration, publish/relist controls, and login-info settings.

### Task 4: Clarify data isolation and first-use documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/windows-distribution.md`
- Modify: `docs/windows-republish-runbook.md`
- Modify: `SOURCE-CODE.md` only if the workflow note belongs there
- Test: `tests/test_windows_config_contract.py`

- [ ] **Step 1: Document the direct first-use path**

State that the buyer extracts one package, double-clicks `XianyuAutoDelivery.exe`, signs in with the package’s local default admin account, changes the password, and then configures the Xianyu account and fixed cloud-drive links. Explicitly state that SMTP and registration are not required.

- [ ] **Step 2: Document the isolation boundary**

Explain that `data/`, `browser_data/`, `logs/`, and local configuration remain under the installation directory, so separate PCs or separate install directories do not share the SQLite database or Xianyu cookies. Warn users not to copy a populated runtime directory to another buyer or run two instances against the same directory.

- [ ] **Step 3: Preserve honest distribution/licensing language**

Keep the public source pointer, AGPL-3.0 notice, modification/redistribution permissions, and the statement that the package is not an official Xianyu product. Do not add seller-specific payment codes, private URLs, credentials, or a mandatory online registration service.

- [ ] **Step 4: Extend the Windows documentation contract**

Add assertions for the default-admin/no-SMTP path, registration-disabled mode, per-install data directories, and the existing automatic delivery/relist workflow. Run `pytest tests/test_windows_config_contract.py -q`.

### Task 5: Rebuild, inspect, and verify the distributable package

**Files:**
- Modify: `tools/build_windows_distribution.ps1` only if the new UI/docs or runtime contract exposes a packaging issue
- Modify: `tools/build_windows_executable.ps1` only if the frozen build needs an explicit resource update
- Test: `tests/test_distribution_contract.py`

- [ ] **Step 1: Run the complete source test suite**

Run `pytest -q` and confirm the existing auto-delivery, fixed-link/SKU, item-publish, and republish tests remain green alongside the new single-user tests.

- [ ] **Step 2: Build the compiled Windows executable and archive**

Use the existing PowerShell build flow to produce a fresh one-directory executable and ZIP package. Do not include the repository’s `data`, `browser_data`, `logs`, database files, cookies, tokens, keys, Python source, or development virtual environment.

- [ ] **Step 3: Run package safety checks**

Inspect the ZIP entry list and assert: `XianyuAutoDelivery.exe`, bundled Node/Playwright runtime, `SOURCE-CODE.md`, `LICENSE`, and shortcut tooling are present; forbidden runtime/source/secret files are absent; no old upstream URL, original-author payment QR text, donation text, or unresolved source placeholders remain; batch files retain CRLF endings.

- [ ] **Step 4: Run the direct-EXE smoke test**

Launch only the packaged `XianyuAutoDelivery.exe`, wait for `/health` to return HTTP 200, confirm the browser/dashboard opens without Python or Docker, then terminate only the smoke-test process. Confirm that an empty package creates runtime directories locally and that a second extracted package would use its own directory.

- [ ] **Step 5: Report the finished artifact**

Provide the package path, SHA-256, source tag/commit, direct double-click instructions, and a concise note that each buyer should use one package on one PC and should change the default admin password on first login.
