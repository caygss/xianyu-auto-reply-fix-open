'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const vm = require('node:vm');

const appPath = path.join(__dirname, '../../static/js/app.js');
const appSource = fs.readFileSync(appPath, 'utf8');

function extractFunction(name) {
  const match = new RegExp(`(?:async\\s+)?function ${name}\\(`).exec(appSource);
  assert.ok(match, `${name} must exist in app.js`);
  const start = match.index;
  const brace = appSource.indexOf('{', start);
  let depth = 0;
  for (let index = brace; index < appSource.length; index += 1) {
    if (appSource[index] === '{') depth += 1;
    if (appSource[index] === '}' && --depth === 0) return appSource.slice(start, index + 1);
  }
  throw new Error(`could not extract ${name}`);
}

function createHarness() {
  const events = [];
  const elements = new Map([
    ['itemCookieFilter', { id: 'itemCookieFilter', value: '' }],
    ['deliveryConfigPanel', { id: 'deliveryConfigPanel' }],
    ['republishConfigModal', { id: 'republishConfigModal' }],
    ['republishAutoRepublish', { id: 'republishAutoRepublish' }],
  ]);
  const rows = [];
  const toTargetId = (item) => `${item.cookie_id}:${item.item_id}`;
  const sandbox = {
    document: {
      getElementById: (id) => elements.get(id) || null,
      querySelectorAll: (selector) => {
        if (selector === '#itemsTableBody tr[data-guided-item-id]') return rows;
        return [];
      },
    },
    __rows: rows,
    __events: events,
    __showSection: () => undefined,
    __loadItems: async () => events.push('loadItems'),
    __loadItemsByCookie: async () => events.push('loadItemsByCookie'),
    __openDeliveryConfigForItem: async (...args) => {
      events.push(`openDelivery:${args.join(':')}`);
    },
    __openRepublishConfig: async (...args) => {
      events.push(`openRepublish:${args.join(':')}`);
    },
    __showToast: (message, type) => events.push({ kind: 'toast', message, type }),
    __goToItemsPage: (page) => events.push(`goToItemsPage:${page}`),
  };

  vm.runInNewContext(`
    const GUIDED_SETUP_NAVIGATION_ACTIONS = new Set([
      'go_to_item_management',
      'go_to_delivery_config',
      'go_to_republish_config',
    ]);
    const rows = globalThis.__rows;
    let allItemsData = [];
    let filteredItemsData = [];
    let itemsPerPage = 20;
    const showSection = (...args) => globalThis.__showSection(...args);
    const loadItems = (...args) => globalThis.__loadItems(...args);
    const loadItemsByCookie = (...args) => globalThis.__loadItemsByCookie(...args);
    const openDeliveryConfigForItem = (...args) => globalThis.__openDeliveryConfigForItem(...args);
    const openRepublishConfig = (...args) => globalThis.__openRepublishConfig(...args);
    const showToast = (...args) => globalThis.__showToast(...args);
    const goToItemsPage = (...args) => globalThis.__goToItemsPage(...args);
    const highlightGuidedSetupTarget = (element) => {
      if (!element) return false;
      globalThis.__events.push('highlight:' + element.id);
      return true;
    };
    ${extractFunction('guidedSetupTargetItem')}
    ${extractFunction('findGuidedSetupTargetRow')}
    ${extractFunction('shouldNavigateGuidedSetupAction')}
    ${extractFunction('navigateGuidedSetupAction')}
    globalThis.guidedNavigationApi = {
      findGuidedSetupTargetRow,
      navigateGuidedSetupAction,
      shouldNavigateGuidedSetupAction,
      setItems: (items) => {
        allItemsData = items;
        filteredItemsData = items;
      },
      addRow: (cookieId, itemId) => rows.push({
        id: cookieId + ':' + itemId,
        dataset: { guidedItemCookieId: cookieId, guidedItemId: itemId },
      }),
    };
  `, sandbox);

  return {
    api: sandbox.guidedNavigationApi,
    elements,
    events,
    rows,
    toTargetId,
    setHook(name, value) { sandbox[`__${name}`] = value; },
  };
}

test('CommonJS app export exposes guided setup navigation behavior', () => {
  const script = `
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
    const app = require(${JSON.stringify(appPath)});
    assert.equal(typeof app.navigateGuidedSetupAction, 'function');
  `;
  const result = spawnSync(process.execPath, ['-e', script], {
    cwd: path.dirname(appPath),
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
});

test('waits for showSection before loading the cookie and opening delivery config', async () => {
  const harness = createHarness();
  const item = { cookie_id: 'account-A', item_id: 'item-1', item_title: '商品 1' };
  harness.api.setItems([item]);
  harness.api.addRow(item.cookie_id, item.item_id);
  let resolveNavigation;
  const navigation = new Promise((resolve) => { resolveNavigation = resolve; });
  harness.setHook('showSection', (section) => {
    harness.events.push(`showSection:${section}`);
    return navigation;
  });

  const pending = harness.api.navigateGuidedSetupAction(
    'go_to_delivery_config',
    item.cookie_id,
    { target_item_id: item.item_id },
  );
  await Promise.resolve();
  assert.deepEqual(harness.events, ['showSection:items']);

  resolveNavigation();
  await pending;

  assert.ok(harness.events.indexOf('loadItemsByCookie') > harness.events.indexOf('showSection:items'));
  assert.ok(harness.events.indexOf(`openDelivery:${item.cookie_id}:${item.item_id}:商品 1`) > harness.events.indexOf('loadItemsByCookie'));
});

test('waits for republish config before highlighting its modal controls', async () => {
  const harness = createHarness();
  const item = { cookie_id: 'account-A', item_id: 'item-2', item_title: '商品 2' };
  harness.api.setItems([item]);
  harness.api.addRow(item.cookie_id, item.item_id);
  let resolveRepublish;
  const republish = new Promise((resolve) => { resolveRepublish = resolve; });
  harness.setHook('openRepublishConfig', (...args) => {
    harness.events.push(`openRepublish:start:${args.join(':')}`);
    return republish.then(() => harness.events.push('openRepublish:resolved'));
  });

  const pending = harness.api.navigateGuidedSetupAction(
    'go_to_republish_config',
    item.cookie_id,
    { target_item_id: item.item_id },
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(harness.events.includes('openRepublish:start:account-A:item-2'));
  assert.ok(!harness.events.includes('highlight:republishConfigModal'));
  assert.ok(!harness.events.includes('highlight:republishAutoRepublish'));

  resolveRepublish();
  await pending;

  const resolvedIndex = harness.events.indexOf('openRepublish:resolved');
  assert.ok(harness.events.indexOf('highlight:republishConfigModal') > resolvedIndex);
  assert.ok(harness.events.indexOf('highlight:republishAutoRepublish') > resolvedIndex);
});

test('rejects unsuccessful or mismatched guided setup responses', () => {
  const harness = createHarness();
  assert.equal(
    harness.api.shouldNavigateGuidedSetupAction(
      { success: false, action: 'go_to_delivery_config', guided_status: { primary_action: 'go_to_delivery_config' } },
      'go_to_delivery_config',
    ),
    false,
  );
  assert.equal(
    harness.api.shouldNavigateGuidedSetupAction(
      { success: true, action: 'go_to_item_management', guided_status: { primary_action: 'go_to_delivery_config' } },
      'go_to_delivery_config',
    ),
    false,
  );
});

test('finds a guided setup row by both account and item id', () => {
  const harness = createHarness();
  harness.api.addRow('account-A', 'same-item');
  harness.api.addRow('account-B', 'same-item');

  assert.equal(
    harness.api.findGuidedSetupTargetRow({ cookie_id: 'account-A', item_id: 'same-item' }).id,
    'account-A:same-item',
  );
  assert.equal(
    harness.api.findGuidedSetupTargetRow({ cookie_id: 'account-B', item_id: 'same-item' }).id,
    'account-B:same-item',
  );
});

test('shows a Chinese warning and does not call write functions when no target exists', async () => {
  const harness = createHarness();
  harness.api.setItems([{ cookie_id: 'account-B', item_id: 'same-item', item_title: '商品 B' }]);
  const writes = [];
  harness.setHook('openDeliveryConfigForItem', () => writes.push('delivery'));
  harness.setHook('openRepublishConfig', () => writes.push('republish'));

  await harness.api.navigateGuidedSetupAction(
    'go_to_delivery_config',
    'account-A',
    { target_item_id: 'same-item' },
  );

  assert.equal(writes.length, 0);
  const toast = harness.events.find((event) => event.kind === 'toast');
  assert.ok(toast);
  assert.match(toast.message, /[\u3400-\u9fff]/);
});
