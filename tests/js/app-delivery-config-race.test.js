'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
  createDeliveryConfigSessionCoordinator,
} = require('../../static/js/delivery-config-session.js');

const appSource = fs.readFileSync(path.join(__dirname, '../../static/js/app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const deliveryCssSource = fs.readFileSync(path.join(__dirname, '../../static/css/delivery-config.css'), 'utf8');

function extractFunction(name) {
  const start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist in app.js`);
  let brace = appSource.indexOf('{', start);
  let depth = 0;
  for (let index = brace; index < appSource.length; index += 1) {
    if (appSource[index] === '{') depth += 1;
    if (appSource[index] === '}' && --depth === 0) return appSource.slice(start, index + 1);
  }
  throw new Error(`could not extract ${name}`);
}

function deliverySource() {
  const start = appSource.indexOf("const DELIVERY_UI_STATE_KEY = 'deliveryConfigUiState';");
  const end = appSource.indexOf('function displayItems(items)', start);
  assert.notEqual(start, -1, 'delivery configuration source must exist');
  assert.notEqual(end, -1, 'delivery configuration source must have an end');
  return `${extractFunction('showSection')}\n${appSource.slice(start, end)}\n${extractFunction('getItemDetailText')}\n${extractFunction('displayCurrentPageItems')}\n`;
}

class Element {
  constructor(id = '') {
    this.id = id;
    this.attributes = new Map();
    this.dataset = {};
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      has: (name) => classes.has(name),
    };
    this.children = [];
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.firstChild = { textContent: '' };
  }

  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === 'title') this.title = '';
  }
  addEventListener() {}
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  querySelector() { return null; }
  closest() { return this; }
  scrollIntoView() { this.scrolled = true; }
}

class TableBodyElement extends Element {
  constructor(id, deliveryItemButtons) {
    super(id);
    this.deliveryItemButtons = deliveryItemButtons;
    this._innerHTML = '';
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.deliveryItemButtons.splice(0);
    const buttonPattern = /<button\b([^>]*data-delivery-item-index="([^"]+)"[^>]*)>/g;
    let match;
    while ((match = buttonPattern.exec(this._innerHTML))) {
      const button = new Element();
      button.dataset.deliveryItemIndex = match[2];
      button.disabled = /\sdisabled(?:\s|$)/.test(match[1]);
      const title = match[1].match(/\stitle="([^"]*)"/);
      button.title = title?.[1] || '';
      if (button.title) button.setAttribute('title', button.title);
      this.deliveryItemButtons.push(button);
    }
  }

  get innerHTML() { return this._innerHTML; }
  querySelectorAll(selector) {
    return selector === '[data-delivery-item-index]' ? this.deliveryItemButtons : [];
  }
}

function createHarness({ honorAbort = true } = {}) {
  const elements = new Map();
  const deliveryItemButtons = Array.from({ length: 2 }, () => new Element());
  const ensure = (id) => {
    if (!elements.has(id)) {
      elements.set(id, id === 'itemsTableBody'
        ? new TableBodyElement(id, deliveryItemButtons)
        : new Element(id));
    }
    return elements.get(id);
  };
  const inventoryDds = Array.from({ length: 6 }, () => new Element());
  [
    'deliveryConfigPanel', 'deliveryConfigPanelBody', 'deliveryConfigToggleButton',
    'deliveryConfigOpenButton', 'deliveryConfigStatus', 'deliveryMethodSelect',
    'fixedLinkInput', 'inventoryPreviewList', 'cardInventorySummary',
    'deliveryInventoryShortage', 'deliveryInventoryShortageText', 'cardGeneratorBatch',
    'items-section', 'dashboard-section',
    'deliveryConfigSaveButton', 'deliveryConfigDeleteButton', 'cardImportButton',
    'cardGenerateButton', 'cardReplenishButton', 'cardContinueButton',
    'deliveryCurrentItemIdentity', 'deliveryCurrentAccountIdentity',
    'deliveryCurrentTitleIdentity', 'deliveryCurrentIdIdentity',
    'itemsTableBody', 'selectAllItems',
  ].forEach(ensure);
  const document = {
    getElementById: ensure,
    createElement: () => new Element(),
    addEventListener() {},
    querySelector: (selector) => {
      if (selector === '.content-section.active') {
        return [...elements.values()].find((element) => element.classList.has('content-section') && element.classList.has('active')) || null;
      }
      return null;
    },
    querySelectorAll: (selector) => {
      if (selector === '#cardInventorySummary dd') return inventoryDds;
      if (selector === '[data-delivery-item-index]') return deliveryItemButtons;
      if (selector === '[data-delivery-method]') return [];
      if (selector === '#sidebar .sidebar-nav .nav-link') return [];
      return [];
    },
  };
  const requests = [];
  const fetch = (url, options = {}) => new Promise((resolve, reject) => {
    const request = { url, options, resolve, reject, settled: false };
    requests.push(request);
    if (honorAbort && options.signal) {
      options.signal.addEventListener('abort', () => {
        if (!request.settled) {
          request.settled = true;
          const error = new Error('aborted internally');
          error.name = 'AbortError';
          reject(error);
        }
      }, { once: true });
    }
  });
  const respond = (match, payload, status = 200) => {
    const request = requests.find((candidate) => !candidate.settled && candidate.url.includes(match));
    assert.ok(request, `pending request matching ${match}`);
    request.settled = true;
    request.resolve({ ok: status >= 200 && status < 300, status, json: async () => payload });
    return request;
  };
  const sandbox = {
    AbortController,
    DeliveryConfigSession: { createDeliveryConfigSessionCoordinator },
    document,
    fetch,
    localStorage: { getItem: () => null, setItem() {} },
    authToken: 'test-token',
    apiBase: '',
    console: { log() {}, error() {} },
    loadDashboard() {}, stopOrdersStream() {}, stopChatStream() {},
    dashboardRuntimeRetryTimer: null, aboutRuntimeRetryTimer: null,
    clearInterval() {}, clearTimeout() {},
    window: {},
    filteredItemsData: [], currentItemsPage: 1, itemsPerPage: 20,
    escapeHtml: (value) => String(value ?? ''),
    displayRepublishStatus: () => '', formatDateTime: () => '', resetItemsSelection() {},
  };
  vm.runInNewContext(`${deliverySource()}\nglobalThis.deliveryTestApi = {
    openDeliveryConfigForItem, showSection, setDeliveryConfigBusy, initDeliveryConfigUi,
    saveCurrentDeliveryConfig, deleteCurrentDeliveryConfig, importCardInventory,
    generateCardInventory, continueDeliveryProcessing,
    displayCurrentPageItems,
    beginDeliveryWrite: typeof beginDeliveryWrite === 'undefined' ? null : beginDeliveryWrite,
    finishDeliveryWrite: typeof finishDeliveryWrite === 'undefined' ? null : finishDeliveryWrite,
    setDeliveryWriteStatus: typeof setDeliveryWriteStatus === 'undefined' ? null : setDeliveryWriteStatus,
    deliveryItemLabel: typeof deliveryItemLabel === 'undefined' ? null : deliveryItemLabel,
    deliverySafeErrorMessage: typeof deliverySafeErrorMessage === 'undefined' ? null : deliverySafeErrorMessage,
    setItems: (items) => { filteredItemsData = items; currentItemsPage = 1; itemsPerPage = 20; },
    setContext: (context) => { deliveryConfigContext = context; },
    state: () => ({ context: deliveryConfigContext, ready: deliveryConfigReady, loadOwner: deliveryLoadUiOwner, writeOwner: typeof deliveryWriteUiOwner === 'undefined' ? null : deliveryWriteUiOwner })
  };`, sandbox);
  return { api: sandbox.deliveryTestApi, elements, inventoryDds, deliveryItemButtons, requests, respond };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const writeButtonIds = ['deliveryConfigSaveButton', 'deliveryConfigDeleteButton', 'cardImportButton', 'cardGenerateButton', 'cardReplenishButton', 'cardContinueButton'];

function identity(harness) {
  return ['deliveryCurrentAccountIdentity', 'deliveryCurrentTitleIdentity', 'deliveryCurrentIdIdentity']
    .map((id) => harness.elements.get(id).textContent);
}

async function settleSuccessfulB(harness, load) {
  harness.respond('/api/items/item-B/delivery-card', { card_id: 'card-B' });
  await tick();
  harness.respond('/delivery-config', { mode: 'generated_card' });
  harness.respond('/inventory/settings', { settings: { stock_ceiling: 99, generator_prefix: 'B-' } });
  harness.respond('/inventory?', { inventory: { available: 7, shortage: 0 } });
  harness.respond('/inventory/preview', { items: ['B***'] });
  await load;
}

async function loadDeliveryItem(harness, accountId = 'account-A', itemId = 'item-A', cardId = 'card-A') {
  const load = harness.api.openDeliveryConfigForItem(accountId, itemId, `商品 ${itemId.slice(-1).toUpperCase()}`);
  harness.respond(`/api/items/${itemId}/delivery-card`, { card_id: cardId });
  await tick();
  harness.respond('/delivery-config', { mode: 'generated_card' });
  harness.respond('/inventory/settings', { settings: { stock_ceiling: 10, generator_prefix: 'A-' } });
  harness.respond('/inventory?', { inventory: { available: 7, shortage: 0 } });
  harness.respond('/inventory/preview', { items: ['A***'] });
  await load;
}

function assertOnlyAUrls(harness) {
  const urls = harness.requests.map((request) => request.url);
  assert.ok(urls.some((url) => url.includes('card-A') && url.includes('account-A')));
  assert.ok(urls.every((url) => !url.includes('card-B') && !url.includes('account-B')));
}

test('setDeliveryConfigBusy(false) preserves a current B load owner', () => {
  const harness = createHarness({ honorAbort: false });
  void harness.api.openDeliveryConfigForItem('account-B', 'item-B', '商品 B');

  assert.ok(harness.api.state().loadOwner);
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'true');
  harness.api.setDeliveryConfigBusy(false);

  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'true');
  assert.ok(writeButtonIds.every((id) => harness.elements.get(id).disabled));
});

test('initDeliveryConfigUi disables writes without a ready context', () => {
  const harness = createHarness();

  harness.api.initDeliveryConfigUi();

  assert.equal(harness.api.state().ready, false);
  assert.ok(writeButtonIds.every((id) => harness.elements.get(id).disabled));
});

test('slow A cannot overwrite fast B or release B loading state', async () => {
  const harness = createHarness({ honorAbort: false });
  const a = harness.api.openDeliveryConfigForItem('account-A', 'item-A', '商品 A');
  const b = harness.api.openDeliveryConfigForItem('account-B', 'item-B', '商品 B');

  assert.deepEqual(identity(harness), ['账号：account-B', '商品：商品 B', '商品 ID：item-B']);
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'true');
  harness.respond('/api/items/item-A/delivery-card', { card_id: 'card-A' });
  await tick();
  assert.deepEqual(identity(harness), ['账号：account-B', '商品：商品 B', '商品 ID：item-B']);
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'true');

  await settleSuccessfulB(harness, b);
  assert.equal(harness.elements.get('deliveryCurrentItemIdentity').getAttribute('data-ready'), 'true');
  assert.equal(harness.elements.get('deliveryMethodSelect').value, 'generated_card');
  assert.equal(String(harness.inventoryDds[1].textContent), '7');
  assert.deepEqual(harness.elements.get('inventoryPreviewList').children.map((row) => row.textContent), ['B***']);
  assert.deepEqual(identity(harness), ['账号：account-B', '商品：商品 B', '商品 ID：item-B']);
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'false');
  assert.equal(harness.api.state().context.itemId, 'item-B');
});

test('aborted A is silent while real B failure is actionable', async () => {
  const harness = createHarness();
  harness.elements.get('fixedLinkInput').value = 'old-value';
  const a = harness.api.openDeliveryConfigForItem('account-A', 'item-A', '商品 A');
  const b = harness.api.openDeliveryConfigForItem('account-B', 'item-B', '商品 B');
  harness.respond('/api/items/item-B/delivery-card', { detail: 'internal token https://secret.example' }, 500);
  await b;

  const status = harness.elements.get('deliveryConfigStatus').textContent;
  assert.deepEqual(identity(harness), ['账号：account-B', '商品：商品 B', '商品 ID：item-B']);
  assert.equal(harness.elements.get('deliveryCurrentItemIdentity').getAttribute('data-ready'), 'false');
  assert.equal(harness.elements.get('fixedLinkInput').value, '');
  assert.equal(status, '“商品 B”加载失败，请重新点击商品列表中的“设置交付方式”。');
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'false');
  assert.doesNotMatch(status, /AbortError|generation|authorization|bearer|token|internal|secret|https?:\/\/|\/api\//i);
  assert.ok(writeButtonIds.every((id) => harness.elements.get(id).disabled));
});

test('leaving item management aborts an unfinished load without a user-facing error', async () => {
  const harness = createHarness({ honorAbort: false });
  const itemsSection = harness.elements.get('items-section');
  itemsSection.classList.add('content-section', 'active');
  harness.elements.get('dashboard-section').classList.add('content-section');
  const load = harness.api.openDeliveryConfigForItem('account-A', 'item-A', '商品 A');
  const request = harness.requests[0];
  const before = {
    identity: identity(harness),
    status: harness.elements.get('deliveryConfigStatus').textContent,
  };

  harness.api.showSection('dashboard');
  assert.equal(request.options.signal.aborted, true);
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'false');
  harness.respond('/api/items/item-A/delivery-card', { card_id: 'card-A' });
  assert.deepEqual(identity(harness), before.identity);
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, before.status);
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'false');
});

test('save config and inventory use the same captured A context', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  harness.elements.get('deliveryMethodSelect').value = 'generated_card';
  harness.elements.get('inventoryStockCeiling').value = '42';

  const save = harness.api.saveCurrentDeliveryConfig();
  const configRequest = harness.requests.at(-1);
  assert.match(configRequest.url, /card-A.*account-A/);
  assert.deepEqual(JSON.parse(configRequest.options.body), { mode: 'generated_card', config: { source: 'local-generated' } });
  assert.ok(writeButtonIds.every((id) => harness.elements.get(id).disabled));
  assert.ok(harness.deliveryItemButtons.every((button) => button.disabled));

  await harness.api.openDeliveryConfigForItem('account-B', 'item-B', '商品 B');
  assert.equal(harness.requests.filter((request) => request.url.includes('/api/items/item-B/delivery-card')).length, 0);
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '正在处理“商品 A”，完成后可以切换商品。');

  harness.respond('/delivery-config', {});
  await tick();
  const settingsRequest = harness.requests.at(-1);
  assert.match(settingsRequest.url, /card-A.*account-A/);
  assert.deepEqual(JSON.parse(settingsRequest.options.body), {
    stock_ceiling: 42, low_stock_threshold: 0, auto_replenish: false,
    generator_prefix: 'A-', generator_length: 0, generator_charset: '',
  });
  assert.ok(writeButtonIds.every((id) => harness.elements.get(id).disabled));
  assert.ok(harness.deliveryItemButtons.every((button) => button.disabled));
  harness.respond('/inventory/settings', {});
  await save;

  assertOnlyAUrls(harness);
  assert.ok(writeButtonIds.every((id) => !harness.elements.get(id).disabled));
  assert.ok(harness.deliveryItemButtons.every((button) => !button.disabled));
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的交付配置和库存设置已保存。');
});

test('fixed link save reports the exact config-only success and sends no inventory write', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  harness.elements.get('deliveryMethodSelect').value = 'fixed_link';
  harness.elements.get('fixedLinkInput').value = 'https://buyer.example/code';

  const save = harness.api.saveCurrentDeliveryConfig();
  harness.respond('/delivery-config', {});
  await save;

  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的交付配置已保存。');
  assert.equal(harness.requests.filter((request) => request.options.method === 'PUT' && request.url.includes('/inventory/settings')).length, 0);
});

test('config success then inventory failure reports exact partial success and recovers controls', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  harness.elements.get('deliveryMethodSelect').value = 'generated_card';

  const save = harness.api.saveCurrentDeliveryConfig();
  const progress = harness.elements.get('deliveryConfigStatus').textContent;
  harness.respond('/delivery-config', {});
  await tick();
  harness.respond('/inventory/settings', { detail: 'generation token https://internal.example/api/settings' }, 500);
  await save;

  const status = harness.elements.get('deliveryConfigStatus');
  assert.equal(status.textContent, '“商品 A”的交付方式已保存，但库存设置保存失败，请再次保存。');
  assert.equal(progress, '正在保存“商品 A”的交付配置…');
  assert.ok(['warning', 'error'].includes(status.dataset.status));
  assert.notEqual(status.dataset.status, 'success');
  assert.doesNotMatch(status.textContent, /generation|token|internal|https?:\/\/|\/api\//i);
  assert.ok(writeButtonIds.every((id) => !harness.elements.get(id).disabled));
  assert.ok(harness.deliveryItemButtons.every((button) => !button.disabled));
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'false');
});

test('config failure preserves inputs, skips inventory, and gives A a safe actionable reason', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  harness.elements.get('deliveryMethodSelect').value = 'generated_card';
  harness.elements.get('fixedLinkInput').value = 'https://buyer.example/code';
  harness.elements.get('providerEndpoint').value = 'https://provider.example/deliver';
  harness.elements.get('providerToken').value = 'keep-this-secret';

  const save = harness.api.saveCurrentDeliveryConfig();
  harness.respond('/delivery-config', { detail: '固定链接格式不正确' }, 400);
  await save;

  assert.equal(harness.elements.get('fixedLinkInput').value, 'https://buyer.example/code');
  assert.equal(harness.elements.get('providerEndpoint').value, 'https://provider.example/deliver');
  assert.equal(harness.elements.get('providerToken').value, 'keep-this-secret');
  assert.equal(harness.requests.filter((request) => request.options.method === 'PUT' && request.url.includes('/inventory/settings')).length, 0);
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的交付配置保存失败：固定链接格式不正确。请检查后再次保存。');
  assert.equal(harness.elements.get('deliveryConfigStatus').dataset.status, 'error');
  assert.ok(writeButtonIds.every((id) => !harness.elements.get(id).disabled));
  assert.equal(harness.elements.get('deliveryConfigPanel').getAttribute('aria-busy'), 'false');
});

test('delete uses its captured A context after the visible context mutates', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  const deletion = harness.api.deleteCurrentDeliveryConfig();
  const progress = harness.elements.get('deliveryConfigStatus').textContent;
  harness.api.setContext({ accountId: 'account-B', itemId: 'item-B', itemTitle: '商品 B', cardId: 'card-B' });
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  harness.respond('/delivery-config', {});
  await deletion;
  assertOnlyAUrls(harness);
  assert.equal(progress, '正在删除“商品 A”的交付配置，完成后可以重新选择交付方式。');
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的交付配置已删除，请重新选择交付方式后保存。');
});

test('import uses its captured A context for import and refresh after visible context mutates', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  harness.elements.get('cardImportInput').value = 'one\ntwo';
  const imported = harness.api.importCardInventory();
  const progress = harness.elements.get('deliveryConfigStatus').textContent;
  harness.api.setContext({ accountId: 'account-B', itemId: 'item-B', itemTitle: '商品 B', cardId: 'card-B' });
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  assert.deepEqual(JSON.parse(harness.requests.at(-1).options.body), { secrets: ['one', 'two'] });
  harness.respond('/inventory/import', {});
  await tick();
  assert.match(harness.requests.at(-2).url, /card-A.*account-A/);
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  harness.respond('/inventory?', { inventory: {} });
  harness.respond('/inventory/preview', { items: [] });
  await imported;
  assertOnlyAUrls(harness);
  assert.equal(progress, '正在为“商品 A”导入卡密，完成后将刷新库存预览。');
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的卡密已导入，脱敏库存预览已刷新，可以继续处理订单。');
});

test('generate uses its captured A context for settings, generation, and refresh', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  const generated = harness.api.generateCardInventory();
  const progress = harness.elements.get('deliveryConfigStatus').textContent;
  harness.api.setContext({ accountId: 'account-B', itemId: 'item-B', itemTitle: '商品 B', cardId: 'card-B' });
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  harness.respond('/inventory/settings', {});
  await tick();
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  harness.respond('/inventory/generate', {});
  await tick();
  assert.match(harness.requests.at(-2).url, /card-A.*account-A/);
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  harness.respond('/inventory?', { inventory: {} });
  harness.respond('/inventory/preview', { items: [] });
  await generated;
  assertOnlyAUrls(harness);
  assert.equal(progress, '正在为“商品 A”保存生成设置并生成卡密，完成后将刷新库存。');
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的卡密已生成，库存已刷新，可以继续处理订单。');
});

test('import failure preserves input and shows a safe A-specific next step', async () => {
  const importedHarness = createHarness();
  await loadDeliveryItem(importedHarness);
  importedHarness.elements.get('cardImportInput').value = 'secret-one\nsecret-two';
  const imported = importedHarness.api.importCardInventory();
  importedHarness.api.setContext({ accountId: 'account-B', itemId: 'item-B', itemTitle: '商品 B', cardId: 'card-B' });
  importedHarness.respond('/inventory/import', { detail: 'Authorization Bearer token at https://internal.example/api/import' }, 500);
  await imported;

  assert.equal(importedHarness.elements.get('cardImportInput').value, 'secret-one\nsecret-two');
  assert.match(importedHarness.elements.get('deliveryConfigStatus').textContent, /^“商品 A”的卡密导入失败：.+。请检查内容后再次导入。$/);
  assert.doesNotMatch(importedHarness.elements.get('deliveryConfigStatus').textContent, /authorization|bearer|token|internal|https?:\/\/|\/api\//i);
});

test('generate failure preserves settings and shows a safe A-specific next step', async () => {
  const generatedHarness = createHarness();
  await loadDeliveryItem(generatedHarness);
  generatedHarness.elements.get('inventoryStockCeiling').value = '81';
  generatedHarness.elements.get('cardGeneratorPrefix').value = 'KEEP-';
  generatedHarness.elements.get('inventoryAutoReplenish').checked = true;
  const generated = generatedHarness.api.generateCardInventory();
  generatedHarness.api.setContext({ accountId: 'account-B', itemId: 'item-B', itemTitle: '商品 B', cardId: 'card-B' });
  generatedHarness.respond('/inventory/settings', { detail: 'AbortError generation stack C:\\private\\worker.js' }, 500);
  await generated;

  assert.equal(generatedHarness.elements.get('inventoryStockCeiling').value, '81');
  assert.equal(generatedHarness.elements.get('cardGeneratorPrefix').value, 'KEEP-');
  assert.equal(generatedHarness.elements.get('inventoryAutoReplenish').checked, true);
  assert.match(generatedHarness.elements.get('deliveryConfigStatus').textContent, /^“商品 A”的卡密生成失败：.+。生成设置已保留，请检查后再次生成。$/);
  assert.doesNotMatch(generatedHarness.elements.get('deliveryConfigStatus').textContent, /aborterror|generation|stack|private|worker\.js/i);
});

test('continue gets A exclusively from the coordinator even if visible context was mutated first', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  harness.api.setContext({ accountId: 'account-B', itemId: 'item-B', itemTitle: '商品 B', cardId: 'card-B' });
  const continued = harness.api.continueDeliveryProcessing();
  const progress = harness.elements.get('deliveryConfigStatus').textContent;
  assert.match(harness.requests.at(-2).url, /card-A.*account-A/);
  assert.match(harness.requests.at(-1).url, /card-A.*account-A/);
  harness.respond('/inventory?', { inventory: {} });
  harness.respond('/inventory/preview', { items: [] });
  await continued;
  assertOnlyAUrls(harness);
  assert.equal(progress, '正在刷新“商品 A”的库存状态，完成后请确认是否仍然缺货。');
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, '“商品 A”的库存状态已刷新；如已补足，系统将继续处理。');
});

test('a stale write token cannot enable controls or replace the current status', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  assert.equal(typeof harness.api.beginDeliveryWrite, 'function');
  const stale = harness.api.beginDeliveryWrite('stale');
  assert.ok(stale);
  assert.equal(harness.api.finishDeliveryWrite(stale), true);
  const current = harness.api.beginDeliveryWrite('current');
  assert.ok(current);
  harness.elements.get('deliveryConfigStatus').textContent = 'current write status';

  assert.equal(harness.api.beginDeliveryWrite('blocked'), null);
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, 'current write status');

  assert.equal(harness.api.finishDeliveryWrite(stale), false);
  assert.equal(harness.api.setDeliveryWriteStatus(stale, 'stale write status'), false);
  assert.ok(writeButtonIds.every((id) => harness.elements.get(id).disabled));
  assert.ok(harness.deliveryItemButtons.every((button) => button.disabled));
  assert.equal(harness.elements.get('deliveryConfigStatus').textContent, 'current write status');
  assert.equal(harness.api.finishDeliveryWrite(current), true);
});

test('real item-list rerender keeps a new delivery switch disabled until the exact write finishes', async () => {
  const harness = createHarness();
  await loadDeliveryItem(harness);
  const operation = harness.api.beginDeliveryWrite('rerender');
  assert.ok(operation);
  harness.api.setItems([{
    cookie_id: 'account-B', item_id: 'item-B', item_title: '商品 B', item_detail: '',
    item_price: '10', is_multi_spec: false, multi_quantity_delivery: false, updated_at: '',
  }]);

  harness.api.displayCurrentPageItems();

  assert.equal(harness.deliveryItemButtons.length, 1);
  assert.equal(harness.deliveryItemButtons[0].disabled, true);
  assert.equal(harness.deliveryItemButtons[0].title, '正在处理当前商品，完成后可以切换');
  assert.equal(harness.api.finishDeliveryWrite(operation), true);
  assert.equal(harness.deliveryItemButtons[0].disabled, false);
  assert.equal(harness.deliveryItemButtons[0].title, '');
});

test('delivery labels, safe errors, live status, and disabled styles meet the UI contract', () => {
  const harness = createHarness();
  assert.equal(harness.api.deliveryItemLabel({ itemTitle: ' 商品 A ', itemId: 'item-A' }), '“商品 A”');
  assert.equal(harness.api.deliveryItemLabel({ itemTitle: '', itemId: ' item-A ' }), '“item-A”');
  assert.equal(harness.api.deliverySafeErrorMessage(new Error(`  ${'可见原因'.repeat(60)}  `), '安全提示').length, 160);
  assert.equal(harness.api.deliverySafeErrorMessage(new Error('Bearer token in /api/private generation stack'), '请稍后重试'), '请稍后重试');
  assert.equal(harness.api.deliverySafeErrorMessage(new Error('failed at src/private/worker.js'), '安全提示'), '安全提示');
  assert.equal(harness.api.deliverySafeErrorMessage(new Error('{"detail":"raw server data"}'), '安全提示'), '安全提示');

  const statusTag = indexSource.match(/<div id="deliveryConfigStatus"[^>]*>/)?.[0] || '';
  assert.match(statusTag, /aria-live="polite"/);
  assert.match(statusTag, /aria-atomic="true"/);

  const disabledRule = deliveryCssSource.match(/\.delivery-config-primary-button:disabled,[\s\S]*?\.delivery-config-secondary-button:disabled\s*\{([^}]*)\}/)?.[1] || '';
  assert.match(disabledRule, /color:\s*var\(--text-secondary\)/);
  assert.match(disabledRule, /background:\s*var\(--light-color\)/);
  assert.match(disabledRule, /border(?:-color)?:\s*[^;]*var\(--border-color\)/);
  assert.match(disabledRule, /cursor:\s*not-allowed/);
  assert.doesNotMatch(disabledRule, /opacity/);
  assert.doesNotMatch(deliveryCssSource, /\.delivery-config-status\[data-status[^}]*border-left/);
});
