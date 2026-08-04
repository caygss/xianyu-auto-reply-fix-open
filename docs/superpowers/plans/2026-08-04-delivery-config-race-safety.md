# Delivery Config Race Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 防止快速切换商品或异步写入时发生 A/B 商品配置、库存、状态和按钮所有权串线。

**Architecture:** 新增一个无 DOM、无网络依赖的交付配置会话协调器，统一管理读取代次、AbortController、冻结商品上下文和写操作令牌。现有 `app.js` 只负责 API 与 DOM 接线，所有异步结果在落到界面前校验所有权；写操作始终使用开始时捕获的上下文。

**Tech Stack:** Plain JavaScript、UMD/CommonJS、浏览器 Fetch/AbortController、Node `node:test`、Python pytest 包装测试、现有 Bootstrap/FastAPI 静态页面。

---

## File Structure

- Create `static/js/delivery-config-session.js`: 纯会话协调器；管理读取、取消、冻结上下文和写令牌，不访问 DOM 或 API。
- Modify `static/index.html`: 在 `app.js` 前加载协调器；增加当前账号/商品身份区域。
- Modify `static/js/app.js:629-724,12854-13511`: 接入协调器，改造读取与全部写操作，拆分读取和写入忙碌状态，并在离开商品管理页时取消未完成读取。
- Modify `static/css/delivery-config.css`: 当前商品身份、加载/禁用状态和小屏布局样式，遵循 `DESIGN.md`。
- Create `tests/js/delivery-config-session.test.js`: 纯协调器 Node 测试。
- Create `tests/js/app-delivery-config-race.test.js`: 使用 `vm`、最小 DOM 和可控 fetch 测试真实 `app.js` 交付配置函数。
- Create `tests/test_delivery_config_frontend.py`: pytest 包装 Node 测试，确保全量 pytest 自动执行前端回归。

## Task 1: Pure Delivery Config Session Coordinator

**Files:**
- Create: `static/js/delivery-config-session.js`
- Create: `tests/js/delivery-config-session.test.js`
- Create: `tests/test_delivery_config_frontend.py`

- [ ] **Step 1: Write the failing coordinator tests**

Create `tests/js/delivery-config-session.test.js` with Node built-ins only:

```js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  createDeliveryConfigSessionCoordinator
} = require('../../static/js/delivery-config-session.js');

const selection = (itemId) => ({
  accountId: 'account-1',
  itemId,
  itemTitle: `商品 ${itemId}`
});

test('new load aborts and supersedes the previous load', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const first = session.beginLoad(selection('A'));
  const second = session.beginLoad(selection('B'));
  assert.equal(first.accepted, true);
  assert.equal(second.accepted, true);
  assert.equal(first.operation.signal.aborted, true);
  assert.equal(first.operation.isCurrent(), false);
  assert.equal(second.operation.isCurrent(), true);
});

test('only current load can commit an immutable context', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const first = session.beginLoad(selection('A')).operation;
  const second = session.beginLoad(selection('B')).operation;
  assert.equal(session.commitLoad(first, 'card-A'), null);
  const context = session.commitLoad(second, 'card-B');
  assert.deepEqual(context, {
    accountId: 'account-1', itemId: 'B', itemTitle: '商品 B', cardId: 'card-B'
  });
  assert.equal(Object.isFrozen(context), true);
  assert.throws(() => { context.itemId = 'A'; }, TypeError);
  assert.equal(context.itemId, 'B');
});

test('stale load and write owners cannot release current state', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const first = session.beginLoad(selection('A')).operation;
  const second = session.beginLoad(selection('B')).operation;
  assert.equal(session.finishLoad(first), false);
  session.commitLoad(second, 'card-B');
  assert.equal(session.finishLoad(second), true);
  const write = session.beginWrite('save');
  assert.equal(session.endWrite({ id: write.id }), false);
  assert.equal(session.isWriteActive(), true);
  assert.equal(session.isWriteCurrent(write), true);
  assert.equal(session.endWrite(write), true);
  assert.equal(session.isWriteCurrent(write), false);
});

test('write captures context and blocks a new load', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const load = session.beginLoad(selection('A')).operation;
  session.commitLoad(load, 'card-A');
  session.finishLoad(load);
  const write = session.beginWrite('save');
  assert.equal(Object.isFrozen(write.context), true);
  const blocked = session.beginLoad(selection('B'));
  assert.equal(blocked.accepted, false);
  assert.equal(blocked.reason, 'write_in_progress');
  assert.equal(blocked.context, write.context);
  assert.equal(write.context.itemId, 'A');
});

test('invalid replacement selection does not cancel the current load', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const current = session.beginLoad(selection('A')).operation;
  assert.throws(() => session.beginLoad({ accountId: '', itemId: '' }), TypeError);
  assert.equal(current.signal.aborted, false);
  assert.equal(current.isCurrent(), true);
});

test('failed load clears only its own committed context', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const load = session.beginLoad(selection('A')).operation;
  session.commitLoad(load, 'card-A');
  assert.equal(session.failLoad(load), true);
  assert.equal(session.getActiveContext(), null);
  const replacement = session.beginLoad(selection('B')).operation;
  assert.equal(session.failLoad(load), false);
  assert.equal(replacement.isCurrent(), true);
});

test('cancel aborts the current load and clears its uncommitted context', () => {
  const session = createDeliveryConfigSessionCoordinator();
  const load = session.beginLoad(selection('A')).operation;
  session.commitLoad(load, 'card-A');
  assert.equal(session.cancelLoad(), true);
  assert.equal(load.signal.aborted, true);
  assert.equal(load.isCurrent(), false);
  assert.equal(session.getActiveContext(), null);
});
```

- [ ] **Step 2: Add the pytest wrapper and verify RED**

Create `tests/test_delivery_config_frontend.py`:

```python
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_delivery_config_node_suite():
    result = subprocess.run(
        [
            "node",
            "--test",
            "tests/js/delivery-config-session.test.js",
            "tests/js/app-delivery-config-race.test.js",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Temporarily create `tests/js/app-delivery-config-race.test.js` with one passing smoke test so this task fails only because the coordinator is missing:

```js
const test = require('node:test');
test('app delivery race harness file loads', () => {});
```

Run:

```powershell
venv\Scripts\python.exe -m pytest -q tests\test_delivery_config_frontend.py
```

Expected: FAIL because `static/js/delivery-config-session.js` cannot be required.

- [ ] **Step 3: Implement the minimal coordinator**

Create `static/js/delivery-config-session.js`:

```js
(function exposeDeliveryConfigSession(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.DeliveryConfigSession = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function buildApi() {
  function normalizeSelection(selection = {}) {
    const normalized = Object.freeze({
      accountId: String(selection.accountId || '').trim(),
      itemId: String(selection.itemId || '').trim(),
      itemTitle: String(selection.itemTitle || '').trim()
    });
    if (!normalized.accountId || !normalized.itemId) {
      throw new TypeError('delivery selection requires accountId and itemId');
    }
    return normalized;
  }

  function createDeliveryConfigSessionCoordinator(options = {}) {
    const AbortControllerCtor = options.AbortController || globalThis.AbortController;
    let generation = 0;
    let writeSequence = 0;
    let activeLoad = null;
    let activeContext = null;
    let activeWrite = null;

    function ownsLoad(operation) {
      return Boolean(
        operation && activeLoad &&
        activeLoad.generation === operation.generation &&
        !operation.signal.aborted
      );
    }

    function ownsWrite(operation) {
      return Boolean(operation && activeWrite && operation === activeWrite);
    }

    function beginLoad(selection) {
      const normalizedSelection = normalizeSelection(selection);
      if (activeWrite) {
        return Object.freeze({
          accepted: false,
          reason: 'write_in_progress',
          context: activeWrite.context
        });
      }
      if (activeLoad) activeLoad.controller.abort();
      activeContext = null;
      const controller = new AbortControllerCtor();
      const currentGeneration = ++generation;
      const operation = Object.freeze({
        generation: currentGeneration,
        selection: normalizedSelection,
        signal: controller.signal,
        isCurrent: () => ownsLoad(operation)
      });
      activeLoad = { generation: currentGeneration, controller, operation };
      return Object.freeze({ accepted: true, operation });
    }

    function commitLoad(operation, cardId) {
      if (!ownsLoad(operation)) return null;
      const normalizedCardId = String(cardId || '').trim();
      if (!normalizedCardId) throw new TypeError('delivery context requires cardId');
      activeContext = Object.freeze({ ...operation.selection, cardId: normalizedCardId });
      return activeContext;
    }

    function finishLoad(operation) {
      if (!ownsLoad(operation)) return false;
      activeLoad = null;
      return true;
    }

    function failLoad(operation) {
      if (!ownsLoad(operation)) return false;
      activeContext = null;
      activeLoad = null;
      return true;
    }

    function cancelLoad() {
      if (!activeLoad) return false;
      activeLoad.controller.abort();
      activeLoad = null;
      activeContext = null;
      return true;
    }

    function beginWrite(kind) {
      if (activeLoad || activeWrite || !activeContext) return null;
      activeWrite = Object.freeze({
        id: `delivery-write-${++writeSequence}`,
        kind: String(kind || 'write'),
        context: Object.freeze({ ...activeContext })
      });
      return activeWrite;
    }

    function endWrite(operation) {
      if (!ownsWrite(operation)) return false;
      activeWrite = null;
      return true;
    }

    return Object.freeze({
      beginLoad, commitLoad, finishLoad, failLoad, cancelLoad,
      beginWrite, endWrite,
      isWriteActive: () => Boolean(activeWrite),
      isWriteCurrent: ownsWrite,
      getActiveContext: () => activeContext,
      getActiveLoad: () => activeLoad?.operation || null
    });
  }

  return Object.freeze({ createDeliveryConfigSessionCoordinator });
});
```

- [ ] **Step 4: Verify GREEN**

Run:

```powershell
venv\Scripts\python.exe -m pytest -q tests\test_delivery_config_frontend.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add -f static/js/delivery-config-session.js tests/js/delivery-config-session.test.js tests/js/app-delivery-config-race.test.js tests/test_delivery_config_frontend.py
git commit -m "test: add delivery config session coordinator"
```

## Task 2: Race-Safe Item Loading and Visible Identity

**Files:**
- Modify: `static/index.html:1126-1134,5337-5338`
- Modify: `static/js/app.js:12854-13115,13333-13358`
- Modify: `static/css/delivery-config.css`
- Modify: `tests/js/app-delivery-config-race.test.js`
- Modify: `tests/test_delivery_config_frontend.py`

- [ ] **Step 1: Replace the smoke test with a minimal app.js VM harness**

The harness must:

```js
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const { createDeliveryConfigSessionCoordinator } = require('../../static/js/delivery-config-session.js');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function createElement(id) {
  const attributes = new Map();
  const classes = new Set();
  const element = {
    id, value: '', textContent: '', hidden: false, disabled: false,
    dataset: {}, style: {}, checked: false, children: [],
    classList: {
      add(...names) { names.forEach(name => classes.add(name)); },
      remove(...names) { names.forEach(name => classes.delete(name)); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : Boolean(force);
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      }
    },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.has(name) ? attributes.get(name) : null; },
    removeAttribute(name) { attributes.delete(name); },
    addEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...children) { this.children = children; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    scrollIntoView() {}, focus() {}
  };
  return element;
}

function loadAppHarness(fetchImpl) {
  const elements = new Map();
  const document = {
    body: createElement('body'),
    addEventListener() {},
    createElement(tag) { return createElement(tag); },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, createElement(id));
      return elements.get(id);
    },
    querySelectorAll() { return []; },
    querySelector() { return null; }
  };
  const sandbox = {
    console, document, fetch: fetchImpl,
    window: null, globalThis: null,
    location: { origin: 'http://test.local' },
    addEventListener() {}, removeEventListener() {},
    localStorage: {
      getItem(key) { return key === 'auth_token' ? 'test-token' : null; },
      setItem() {}
    },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    AbortController, URL, URLSearchParams,
    setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() {},
    DeliveryConfigSession: { createDeliveryConfigSessionCoordinator }
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../../static/js/app.js'), 'utf8'), sandbox);
  return { sandbox, elements };
}
```

Add real assertions for controlled A/B responses rather than testing mock call counts alone.

- [ ] **Step 2: Write RED tests for A slow / B fast and stale finally**

Required test names and assertions:

```js
test('slow A cannot overwrite fast B or release B loading state', async () => {
  // Start A, then B. Resolve B completely before A.
  // Assert B identity/config/inventory are rendered.
  // Resolve or reject A and assert all B values and aria-busy remain unchanged.
});

test('aborted A is silent while real B failure is actionable', async () => {
  // Assert no AbortError text is rendered.
  // Assert B failure preserves B identity, clears old values, disables writes,
  // and tells the user to click 设置交付方式 again.
});

test('leaving item management aborts the unfinished load without user-facing error', async () => {
  // Start A, call showSection('dashboard'), then settle A.
  // Assert A's signal is aborted and no form, status, identity, or busy state changes afterward.
});
```

Run:

```powershell
node --test tests/js/app-delivery-config-race.test.js
```

Expected: FAIL because `openDeliveryConfigForItem` still owns a single mutable global context and does not pass signals.

- [ ] **Step 3: Load the coordinator before app.js and add identity markup**

In `static/index.html`, add before `app.js`:

```html
<script src="/static/js/delivery-config-session.js?v=1"></script>
<script src="/static/js/app.js?v=1.2.10"></script>
```

Under “当前商品专属配置”, add a persistent identity region that always includes account ID,
item title, and item ID:

```html
<div id="deliveryCurrentItemIdentity" class="delivery-config-current-item" aria-live="polite" data-ready="false">
  <span id="deliveryCurrentAccountIdentity">账号：尚未选择</span>
  <span id="deliveryCurrentTitleIdentity">商品：尚未选择</span>
  <span id="deliveryCurrentIdIdentity">商品 ID：尚未选择</span>
</div>
```

- [ ] **Step 4: Integrate the coordinator into the load path**

At the delivery config globals in `app.js`:

```js
const deliveryConfigSession = DeliveryConfigSession.createDeliveryConfigSessionCoordinator();
let deliveryConfigContext = null;
let deliveryLoadUiOwner = null;
let deliveryWriteUiOwner = null;

function isDeliveryAbort(error) {
  return error?.name === 'AbortError';
}

function syncDeliveryConfigContext() {
  deliveryConfigContext = deliveryConfigSession.getActiveContext();
  return deliveryConfigContext;
}
```

`setDeliveryLoadBusy(operation, busy)` must keep an independent `deliveryLoadUiOwner`.
Starting busy is accepted only when `operation.isCurrent()` is true; releasing busy is accepted
only when `deliveryLoadUiOwner === operation`. This lets the coordinator finish/fail the load before
the UI release while still preventing stale `finally` blocks from touching B.

Change `deliveryConfigFetch` so caller options, including `signal`, survive:

```js
const response = await fetch(`${apiBase}${path}`, { ...options, headers });
```

Change `deliveryConfigFetchOptional(path, options = {})` to pass `options` through.

Refactor `openDeliveryConfigForItem` to this ownership sequence:

```js
const started = deliveryConfigSession.beginLoad({
  accountId: normalizedAccountId,
  itemId: normalizedItemId,
  itemTitle: String(itemTitle || '')
});
if (!started.accepted) {
  setDeliveryConfigStatus(
    `正在处理“${started.context.itemTitle || started.context.itemId}”，完成后可以切换商品。`,
    'info'
  );
  return;
}
const operation = started.operation;
let loadSucceeded = false;
deliveryConfigContext = null;
clearDeliveryConfigForSelection(operation.selection);
setDeliveryLoadBusy(operation, true);

try {
  const resolved = await deliveryConfigFetch(resolvePath, {
    method: 'POST', signal: operation.signal
  });
  if (!operation.isCurrent()) return;
  const context = deliveryConfigSession.commitLoad(operation, resolved.card_id);
  if (!context) return;
  deliveryConfigContext = context;
  const paths = deliveryCardPaths(context.cardId, context.accountId);
  const [config, settingsPayload, inventoryPayload, previewPayload] = await Promise.all([
    deliveryConfigFetchOptional(paths.config, { signal: operation.signal }),
    deliveryConfigFetch(paths.settings, { signal: operation.signal }),
    deliveryConfigFetch(paths.inventory, { signal: operation.signal }),
    deliveryConfigFetch(paths.preview, { signal: operation.signal })
  ]);
  if (!operation.isCurrent()) return;
  renderLoadedDeliveryContext(context, config, settingsPayload, inventoryPayload, previewPayload);
  loadSucceeded = true;
} catch (error) {
  if (isDeliveryAbort(error) || !operation.isCurrent()) return;
  renderDeliveryLoadFailure(operation.selection, error);
} finally {
  if (operation.isCurrent()) {
    const ended = loadSucceeded
      ? deliveryConfigSession.finishLoad(operation)
      : deliveryConfigSession.failLoad(operation);
    if (ended) {
      syncDeliveryConfigContext();
      setDeliveryLoadBusy(operation, false);
    }
  }
}
```

`clearDeliveryConfigForSelection` must clear the old form, inventory, preview, ready state, and
writeability before starting the request, while preserving and displaying the newly selected
account/title/item identity. `renderLoadedDeliveryContext` changes `data-ready` to `true` only after
all four dependent reads succeed. `renderDeliveryLoadFailure` keeps the selected identity, leaves
`data-ready="false"`, and keeps every write action disabled.

In `showSection`, when `sectionName !== 'items'`, call a small lifecycle helper that invokes
`deliveryConfigSession.cancelLoad()`. A canceled stale request remains silent. Do not cancel or retry
a non-idempotent write operation. Call the same helper at the start of `initDeliveryConfigUi` so a
reinitialization also cancels an unfinished read.

Extend `tests/test_delivery_config_frontend.py` with a static-page contract that asserts the
coordinator script appears before `app.js`, all four identity IDs exist, and the updated `app.js`
cache version is present. This prevents a correct coordinator from being omitted in packaged HTML.

- [ ] **Step 5: Add identity and state styles**

Add to `delivery-config.css`:

```css
.delivery-config-current-item {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin: 6px 0 0;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.delivery-config-current-item[data-ready="true"] {
  color: var(--text-color);
  font-weight: 600;
}

.delivery-config-shell [aria-busy="true"] {
  cursor: progress;
}
```

Do not add decorative animation. Keep the existing reduced-motion rule.

- [ ] **Step 6: Verify GREEN and commit**

Run:

```powershell
venv\Scripts\python.exe -m pytest -q tests\test_delivery_config_frontend.py
```

Expected: PASS for coordinator and A/B load tests.

Commit:

```powershell
git add -f static/index.html static/js/app.js static/css/delivery-config.css tests/js/app-delivery-config-race.test.js
git commit -m "fix: isolate delivery config item loads"
```

## Task 3: Immutable Write Context Across Every Action

**Files:**
- Modify: `static/js/app.js:13156-13331`
- Modify: `tests/js/app-delivery-config-race.test.js`

- [ ] **Step 1: Write RED tests for immutable writes**

Add tests with controlled fetch URLs and payloads:

```js
test('save config and inventory use the same captured A context', async () => {
  // Load A completely, call saveCurrentDeliveryConfig, and attempt to select B.
  // Assert B load is rejected while write is active.
  // Assert config and settings URLs both contain A card/account only.
});

test('delete import generate and refresh never reread mutable global context', async () => {
  // For each action, capture A, mutate the test-visible global context toward B,
  // then assert every request remains scoped to the frozen A operation context.
});

test('stale write token cannot enable controls or replace status', async () => {
  // End a stale operation after a new owner exists and assert controls stay disabled.
});

test('forged write token with a copied id cannot end the real write', async () => {
  // Begin A, pass a different object with the same visible id, and assert A remains the owner.
});
```

Run:

```powershell
node --test tests/js/app-delivery-config-race.test.js
```

Expected: FAIL because helpers currently call `requireDeliveryConfigContext()` after awaits and use a shared boolean busy state.

- [ ] **Step 2: Introduce explicit write ownership helpers**

Add to `app.js`:

```js
function beginDeliveryWrite(kind) {
  const operation = deliveryConfigSession.beginWrite(kind);
  if (!operation) {
    setDeliveryConfigStatus('当前商品还没有加载完成，请稍候', 'error');
    return null;
  }
  setDeliveryWriteBusy(operation, true);
  return operation;
}

function finishDeliveryWrite(operation) {
  if (!deliveryConfigSession.endWrite(operation)) return false;
  setDeliveryWriteBusy(operation, false);
  return true;
}

function setDeliveryWriteStatus(operation, message, type = 'info') {
  if (!deliveryConfigSession.isWriteCurrent(operation)) return false;
  setDeliveryConfigStatus(message, type);
  return true;
}
```

`setDeliveryWriteBusy` uses the same independent-owner pattern as loading: setting busy requires
`deliveryConfigSession.isWriteCurrent(operation)`, and releasing requires
`deliveryWriteUiOwner === operation`. Therefore `finishDeliveryWrite` may end the coordinator token
first and then release only that token's UI state; a stale operation cannot enable buttons.

`setDeliveryWriteBusy` must disable:

- `deliveryConfigSaveButton`
- `deliveryConfigDeleteButton`
- `cardImportButton`
- `cardGenerateButton`
- `cardReplenishButton`
- `cardContinueButton`
- every `[data-delivery-item-index]` button, including buttons created by a later table rerender

`displayCurrentPageItems` must initialize delivery buttons with `disabled = deliveryConfigSession.isWriteActive()`.

- [ ] **Step 3: Make internal helpers require explicit context**

Use these signatures and remove internal global re-reads:

```js
async function saveInventorySettings(context, payload = buildInventorySettingsPayload())
async function refreshDeliveryInventory(context, owner)
async function saveCurrentDeliveryConfig()
async function deleteCurrentDeliveryConfig()
async function importCardInventory()
async function generateCardInventory()
async function continueDeliveryProcessing()
```

Each public action first performs only synchronous form validation, then captures exactly one write
operation before its first request:

```js
const operation = beginDeliveryWrite('save');
if (!operation) return;
const { context } = operation;
```

Every URL must come from `deliveryCardPaths(context.cardId, context.accountId)`. Status and render calls after awaits must verify that the operation still owns the write before mutating the DOM.

Use these ownership rules in every action:

```js
const operation = beginDeliveryWrite('save');
if (!operation) return;
const { context } = operation;
try {
  // Build every path from context and never call requireDeliveryConfigContext() below this point.
  // After each await, return immediately unless deliveryConfigSession.isWriteCurrent(operation).
} finally {
  finishDeliveryWrite(operation);
}
```

For save, capture `const inventoryPayload = buildInventorySettingsPayload()` before the first
request. If the mode uses inventory, call `saveInventorySettings(context, inventoryPayload)` so the
config request and inventory request are guaranteed to use one context and one snapshot of form
values. `refreshDeliveryInventory(context, operation)` may render only while the same operation is
current. Delete, import, and generate clear user inputs only after their request succeeds and their
operation still owns the write.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```powershell
venv\Scripts\python.exe -m pytest -q tests\test_delivery_config_frontend.py
```

Expected: PASS.

Commit:

```powershell
git add -f static/js/app.js tests/js/app-delivery-config-race.test.js
git commit -m "fix: bind delivery writes to immutable item context"
```

## Task 4: Partial Success, Actionable Errors, and Accessibility

**Files:**
- Modify: `static/js/app.js:12935-13358`
- Modify: `static/index.html:1126-1134`
- Modify: `static/css/delivery-config.css`
- Modify: `tests/js/app-delivery-config-race.test.js`

- [ ] **Step 1: Write RED tests for partial success and user-facing states**

```js
test('config success and inventory failure reports partial success for A', async () => {
  // First PUT resolves, second PUT rejects.
  // Assert status contains 商品 A, 交付方式已保存, 库存设置保存失败, 再次保存.
  // Assert status does not contain 全部保存 or a success-only state.
});

test('write failure preserves user input and names the target item', async () => {
  // Fill fixedLinkInput, reject PUT, and assert the value remains unchanged.
});

test('load failure disables writes and never leaks internal abort text', async () => {
  // Assert aria-busy false, all write buttons disabled, B identity retained,
  // and no AbortError, generation, token, or internal URL in visible status.
});

test('rerendered item switch buttons remain disabled during write', async () => {
  // Start write, invoke displayItems/displayCurrentPageItems, inspect new buttons.
});
```

Run:

```powershell
node --test tests/js/app-delivery-config-race.test.js
```

Expected: FAIL on current generic success/error messages and button state.

- [ ] **Step 2: Implement exact Chinese state messages**

Use target-specific messages:

```js
function deliveryItemLabel(context) {
  return `“${context.itemTitle || context.itemId}”`;
}
```

Add one sanitizing boundary for backend/network errors:

```js
function deliverySafeErrorMessage(error, fallback = '请求失败，请稍后重试') {
  const raw = String(error?.message || '').trim();
  if (!raw) return fallback;
  if (/AbortError|https?:\/\/|\/api\/|authorization|bearer|token|generation|stack/i.test(raw)) {
    return fallback;
  }
  return raw.slice(0, 160);
}
```

Required message semantics:

- Load: `正在加载“商品 A”的交付配置…`
- Write blocked switch: `正在处理“商品 A”，完成后可以切换商品。`
- Full save success: `“商品 A”的交付配置和库存设置已保存。`
- Partial save: `“商品 A”的交付方式已保存，但库存设置保存失败，请再次保存。`
- Load failure: `“商品 B”加载失败，请重新点击商品列表中的“设置交付方式”。`
- Action failure: action + item label + concise backend message + retry instruction.
- Successful load: name the item and tell the user either to edit then click `保存当前商品配置`,
  or to choose a delivery method when no configuration exists.

Do not show `AbortError`, request generations, tokens, stack traces, internal paths, or raw response objects.

- [ ] **Step 3: Preserve form data and complete accessibility states**

- Do not call `applyDeliveryConfig(null)` on write failure.
- Use `aria-busy` only while the current owner is busy.
- Keep `deliveryConfigStatus` as `aria-live="polite"` and `aria-atomic="true"`.
- Set `deliveryCurrentItemIdentity.dataset.ready` only after complete successful load.
- On load failure keep identity text but mark `data-ready="false"` and disable writes.
- Disabled buttons retain readable labels and `title="正在处理当前商品，完成后可以切换"` for item switch buttons.
- Long account IDs, titles, emoji, and CJK text wrap without pushing controls outside the panel;
  test at 200% zoom and 520px width.

- [ ] **Step 4: Verify GREEN and commit**

Run:

```powershell
venv\Scripts\python.exe -m pytest -q tests\test_delivery_config_frontend.py
```

Expected: PASS.

Commit:

```powershell
git add -f static/js/app.js static/index.html static/css/delivery-config.css tests/js/app-delivery-config-race.test.js
git commit -m "fix: clarify delivery config operation states"
```

## Task 5: Regression, Browser Acceptance, and Final Review

**Files:**
- Modify only if tests expose a defect in the files above.
- Test: `tests/test_delivery_config_frontend.py`

- [ ] **Step 1: Run the focused frontend suite**

```powershell
node --test tests/js/delivery-config-session.test.js tests/js/app-delivery-config-race.test.js
venv\Scripts\python.exe -m pytest -q tests\test_delivery_config_frontend.py
```

Expected: all Node and pytest wrapper tests pass.

- [ ] **Step 2: Run related backend/UI regressions**

```powershell
venv\Scripts\python.exe -m pytest -q tests\test_bound_item_delivery_runtime.py tests\test_bound_delivery_finalization_runtime.py tests\test_delivery_orchestration_migration.py
```

Expected: PASS with only existing FastAPI/Starlette deprecation warnings.

- [ ] **Step 3: Run the full suite and static checks**

```powershell
venv\Scripts\python.exe -m pytest -q
venv\Scripts\python.exe -m py_compile reply_server.py XianyuAutoAsync.py delivery_orchestration_service.py db_manager.py
git diff --check
```

Expected: at least 692 pytest tests (691 existing tests plus the new frontend wrapper) pass;
compile and diff checks exit 0.

- [ ] **Step 4: Browser acceptance**

Start the existing app and use the browser with mocked or development API latency:

1. Open 商品管理 and click A, B, C rapidly; only C may remain visible.
2. Delay A responses until after B completes; A must not flash into B.
3. Save A and verify all 商品设置交付方式 buttons are disabled until completion.
4. Force inventory settings failure after config success; verify the partial-success Chinese message.
5. Check light/dark themes at 520px and 900px widths.
6. Complete select, save, and error recovery with keyboard only.

Expected: no cross-item render/write, no stale busy release, no internal error text, no inaccessible focus state.

- [ ] **Step 5: Request two-stage review**

Spec reviewer compares implementation against:

```text
docs/superpowers/specs/2026-08-04-delivery-config-race-safety-design.md
```

After spec approval, code-quality reviewer checks race ownership, abort semantics, immutable context, test realism, DOM accessibility and absence of secret leakage. Fix every Critical/Important and re-run review.

- [ ] **Step 6: Commit any review fixes**

```powershell
git add -f static/index.html static/js/app.js static/js/delivery-config-session.js static/css/delivery-config.css tests/js tests/test_delivery_config_frontend.py
git commit -m "fix: harden delivery config race handling"
```

Skip this commit if review requires no changes.
