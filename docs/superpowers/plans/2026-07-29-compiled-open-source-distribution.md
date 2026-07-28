# Compiled Open-Source Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a Windows distribution archive containing a compiled Xianyu management application, its bundled browser/assets, launchers, and a fixed public source pointer while preserving AGPL-3.0 rights.

**Architecture:** Keep the existing Python source as the canonical public source. Add a PyInstaller-based Windows build contract that compiles `Start.py` in one-directory mode, bundles `static`, configuration templates, and the Playwright browser runtime, then copies only compiled artifacts and public documentation into the install archive. The archive's source pointer is generated only after the public source commit and tag are known.

**Tech Stack:** Python 3.12 development environment, PyInstaller, Playwright 1.59.0, PowerShell 5.1-compatible build scripts, ZIP distribution, pytest contract tests.

---

### Task 1: Lock the legal and artifact contract

**Files:**
- Modify: `README.md`
- Modify: `docs/windows-distribution.md`
- Create: `docs/open-source-distribution.md`
- Create: `SOURCE-CODE.md`
- Test: `tests/test_distribution_contract.py`

- [ ] **Step 1: Add the public distribution policy**

Document that the project is a modified derivative of `GuDong2003/xianyu-auto-reply-fix`, remains under AGPL-3.0, permits fee-based packaging/support, and does not prohibit modification or redistribution. State that the Windows archive contains compiled artifacts and that the exact corresponding source is linked by `SOURCE-CODE.md`.

- [ ] **Step 2: Add a source-pointer template**

Create `SOURCE-CODE.md` with explicit fields for repository URL, release tag, source commit, modification date, and the AGPL-3.0 source-availability notice. The build script will replace the four marked values before packaging.

- [ ] **Step 3: Add contract tests before implementation**

Add tests that assert the policy documents preserve `AGPL-3.0`, mention modification and redistribution, and contain no seller-specific credentials or runtime data. Add a test that the source pointer has no unresolved release placeholders after the release-preparation step.

- [ ] **Step 4: Run the contract tests and record the expected initial failure**

Run `pytest tests/test_distribution_contract.py -q`. The new placeholder test must fail until the release metadata is supplied; document the failure as the intended red state before the build changes.

### Task 2: Add a reproducible compiled build

**Files:**
- Create: `tools/build_windows_executable.ps1`
- Create: `tools/xianyu_auto_delivery.spec`
- Modify: `Start.py` only if the frozen-path smoke test identifies a path issue
- Test: `tests/test_windows_executable_contract.py`

- [ ] **Step 1: Define the PyInstaller build inputs**

Use `Start.py` as the entry point, one-directory mode, and include `static`, `global_config.yml`, `announcement.json`, and the local Playwright browser directory as data. Collect Playwright and DrissionPage hidden imports. Write build output only beneath `build/windows-executable` and refuse to overwrite an existing output without an explicit clean marker.

- [ ] **Step 2: Add the build script safety checks**

Require the repository root, verify that `venv\Scripts\python.exe` and PyInstaller exist, reject missing browser assets, and reject runtime files (`*.db`, `*.log`, `browser_data`, `data`, `venv`) from the final artifact. Print the executable path and SHA-256 checksum.

- [ ] **Step 3: Add executable contract tests**

Test that the build script references `Start.py`, uses one-directory mode, includes static assets and Playwright data, writes under `dist`/`build`, and has no command that copies `data`, `browser_data`, `logs`, or `venv` into the release.

- [ ] **Step 4: Build and smoke-test the executable**

Run the build script, invoke the compiled executable with the existing health-check-compatible startup path, wait for `http://127.0.0.1:8090/health` to return HTTP 200, then terminate only the process started by the smoke test. Confirm that the UI opens without a Python installation.

### Task 3: Build the source-free Windows archive

**Files:**
- Modify: `tools/build_windows_distribution.ps1`
- Modify: `启动闲鱼自动发货.bat`
- Modify: `首次安装闲鱼自动发货.bat`
- Modify: `docs/windows-distribution.md`
- Test: `tests/test_windows_launcher_contract.py`
- Test: `tests/test_distribution_contract.py`

- [ ] **Step 1: Change the distribution input from source tree to compiled artifact**

Copy the compiled application directory, launchers, `LICENSE`, `SOURCE-CODE.md`, and end-user documentation into staging. Do not copy `*.py`, `venv`, `data`, `browser_data`, `logs`, databases, keys, or the source repository's development-only files.

- [ ] **Step 2: Make the launchers target the compiled executable**

Keep CRLF endings and PowerShell 5.1-compatible syntax. The installer must verify the compiled executable and browser runtime, create local runtime directories, and pause with a readable error if files are missing. The normal launcher must start the compiled executable and open the UI only after the health endpoint is ready.

- [ ] **Step 3: Add archive safety assertions**

Extend archive checks to reject Python source files in application payload directories, runtime state, secrets, and unresolved `SOURCE-CODE.md` placeholders. Keep the existing ZIP path traversal and ownership-marker protections.

- [ ] **Step 4: Build and inspect the archive**

Run the archive builder, list entries, verify there are no `.py` files outside documentation examples, verify `SOURCE-CODE.md` contains the fixed source metadata, verify all `.bat` files are CRLF, and compute the archive SHA-256.

### Task 4: Publish the corresponding source repository

**Files:**
- Git remote: new public GitHub repository under `caygss`
- Modify: `SOURCE-CODE.md` with the final URL/tag/commit
- Modify: `README.md` with the final public source URL

- [ ] **Step 1: Create the public repository without an auto-generated README**

Create a public repository named `xianyu-auto-reply-fix-open` under `caygss`, keeping the local source history and preserving the upstream remote as a separate reference.

- [ ] **Step 2: Commit the complete corresponding source**

Commit the modified source, tests, build scripts, documentation, launchers, and license. Confirm `git status --ignored` shows runtime data excluded and scan tracked files for credentials, database files, cookies, and logs.

- [ ] **Step 3: Push and tag the source**

Push the source to `main`, create tag `windows-installer-v1.0.0`, push the tag, and record the resulting commit hash.

- [ ] **Step 4: Fill the source pointer and rebuild**

Replace the source-pointer metadata with `https://github.com/caygss/xianyu-auto-reply-fix-open`, tag `windows-installer-v1.0.0`, and the exact commit hash. Rebuild the compiled artifact and archive so the sold package points to the published source.

### Task 5: Final verification and handoff

**Files:**
- Create: `docs/xianyu-listing-copy.md`
- Test: full relevant pytest suite

- [ ] **Step 1: Add honest listing copy**

State that the listing sells a compiled Windows installation package and optional installation/support service; it is not an official Xianyu product, gives no exclusive rights, and permits downstream modification/redistribution under AGPL-3.0.

- [ ] **Step 2: Run verification**

Run `pytest tests/test_republish_*.py tests/test_delivery_republish_hook.py tests/test_windows_config_contract.py tests/test_windows_launcher_contract.py tests/test_distribution_contract.py -q`, `node --check static/js/app.js`, and the archive safety scan. Run the compiled health smoke test and record the result.

- [ ] **Step 3: Report artifacts**

Provide the public GitHub URL, source tag/commit, package path, SHA-256 checksums, and the known unrelated upstream test failure if it remains.
