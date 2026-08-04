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
  return `${extractFunction('showSection')}\n${appSource.slice(start, end)}\n`;
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
  addEventListener() {}
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  querySelector() { return null; }
  closest() { return this; }
  scrollIntoView() { this.scrolled = true; }
}

function createHarness({ honorAbort = true } = {}) {
  const elements = new Map();
  const ensure = (id) => {
    if (!elements.has(id)) elements.set(id, new Element(id));
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
  };
  vm.runInNewContext(`${deliverySource()}\nglobalThis.deliveryTestApi = { openDeliveryConfigForItem, showSection, state: () => ({ context: deliveryConfigContext, ready: deliveryConfigReady, owner: deliveryLoadUiOwner }) };`, sandbox);
  return { api: sandbox.deliveryTestApi, elements, inventoryDds, requests, respond };
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
  assert.match(status, /“商品 B”加载失败，请重新点击商品列表中的“设置交付方式”。/);
  assert.doesNotMatch(status, /AbortError|internal|secret|https?:\/\//i);
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
