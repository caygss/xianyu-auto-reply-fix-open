'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const {
  createDeliveryConfigSessionCoordinator,
} = require('../../static/js/delivery-config-session.js');

test('browser build exposes the frozen API on DeliveryConfigSession only', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../../static/js/delivery-config-session.js'),
    'utf8',
  );
  const sandbox = { AbortController };
  vm.runInNewContext(source, sandbox);

  assert.ok(sandbox.DeliveryConfigSession);
  assert.ok(Object.isFrozen(sandbox.DeliveryConfigSession));
  assert.equal(
    typeof sandbox.DeliveryConfigSession.createDeliveryConfigSessionCoordinator,
    'function',
  );
  assert.equal(sandbox.createDeliveryConfigSessionCoordinator, undefined);
});

test('browser fallback resolves AbortController from its root', () => {
  const source = fs.readFileSync(
    path.join(__dirname, '../../static/js/delivery-config-session.js'),
    'utf8',
  );
  const sandbox = { AbortController };
  vm.runInNewContext(`const globalThis = undefined;\n${source}`, sandbox);

  assert.ok(sandbox.DeliveryConfigSession);
  assert.doesNotThrow(() => {
    sandbox.DeliveryConfigSession.createDeliveryConfigSessionCoordinator();
  });
});

function selection(name) {
  return {
    accountId: ` account-${name} `,
    itemId: ` item-${name} `,
    itemTitle: ` ${name} `,
  };
}

function establishContext(coordinator, name) {
  const result = coordinator.beginLoad(selection(name));
  assert.equal(result.accepted, true);
  const context = coordinator.commitLoad(result.operation, ` card-${name} `);
  assert.equal(coordinator.finishLoad(result.operation), true);
  return context;
}

test('new load aborts and supersedes previous load', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const first = coordinator.beginLoad(selection('A')).operation;
  const second = coordinator.beginLoad(selection('B')).operation;

  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.signal.aborted, false);
  assert.equal(second.isCurrent(), true);
  assert.equal(second.generation, first.generation + 1);
  assert.strictEqual(coordinator.getActiveLoad(), second);
  assert.ok(Object.isFrozen(second));
  assert.ok(Object.isFrozen(second.selection));
});

test('only current load commits immutable normalized context', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const first = coordinator.beginLoad(selection('A')).operation;
  const second = coordinator.beginLoad(selection('B')).operation;

  assert.equal(coordinator.commitLoad(first, 'card-A'), null);

  const context = coordinator.commitLoad(second, ' card-B ');
  assert.deepEqual(context, {
    accountId: 'account-B',
    itemId: 'item-B',
    itemTitle: 'B',
    cardId: 'card-B',
  });
  assert.strictEqual(coordinator.getActiveContext(), context);
  assert.ok(Object.isFrozen(context));
  assert.throws(() => {
    context.itemTitle = 'changed';
  }, TypeError);
  assert.equal(context.itemTitle, 'B');
});

test('stale load cannot finish current load', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const first = coordinator.beginLoad(selection('A')).operation;
  const second = coordinator.beginLoad(selection('B')).operation;

  assert.equal(coordinator.finishLoad(first), false);
  assert.strictEqual(coordinator.getActiveLoad(), second);
  assert.equal(second.isCurrent(), true);
});

test('forged object with the same write id cannot end the real write', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  establishContext(coordinator, 'A');
  const write = coordinator.beginWrite(' save ');
  const forged = { ...write };

  assert.equal(coordinator.endWrite(forged), false);
  assert.equal(coordinator.isWriteCurrent(forged), false);
  assert.equal(coordinator.isWriteCurrent(write), true);
  assert.equal(coordinator.isWriteActive(), true);
});

test('isWriteCurrent changes from true to false after exact endWrite', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  establishContext(coordinator, 'A');
  const first = coordinator.beginWrite(' save ');

  assert.equal(first.kind, 'save');
  assert.ok(Object.isFrozen(first));
  assert.equal(coordinator.isWriteCurrent(first), true);
  assert.equal(coordinator.endWrite(first), true);
  assert.equal(coordinator.isWriteCurrent(first), false);
  assert.equal(coordinator.isWriteActive(), false);

  const second = coordinator.beginWrite('delete');
  assert.notEqual(first.id, second.id);
});

test('write captures frozen A context and rejects B load with that exact context', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const activeContext = establishContext(coordinator, 'A');
  const write = coordinator.beginWrite(' save ');
  const rejected = coordinator.beginLoad(selection('B'));

  assert.notStrictEqual(write.context, activeContext);
  assert.deepEqual(write.context, activeContext);
  assert.ok(Object.isFrozen(write.context));
  assert.ok(Object.isFrozen(rejected));
  assert.deepEqual(
    { accepted: rejected.accepted, reason: rejected.reason },
    { accepted: false, reason: 'write_in_progress' },
  );
  assert.strictEqual(rejected.context, write.context);
  assert.strictEqual(coordinator.getActiveContext(), activeContext);
  assert.equal(coordinator.getActiveLoad(), null);
});

test('invalid replacement selection does not abort current A load', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const first = coordinator.beginLoad(selection('A')).operation;

  assert.throws(() => {
    coordinator.beginLoad({ accountId: ' ', itemId: 'item-B', itemTitle: 'B' });
  }, TypeError);
  assert.equal(first.signal.aborted, false);
  assert.equal(first.isCurrent(), true);
  assert.strictEqual(coordinator.getActiveLoad(), first);
});

test('failed load clears only its own context and stale fail cannot affect B', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const first = coordinator.beginLoad(selection('A')).operation;
  coordinator.commitLoad(first, 'card-A');
  const second = coordinator.beginLoad(selection('B')).operation;
  const secondContext = coordinator.commitLoad(second, 'card-B');

  assert.equal(coordinator.failLoad(first), false);
  assert.strictEqual(coordinator.getActiveLoad(), second);
  assert.strictEqual(coordinator.getActiveContext(), secondContext);

  assert.equal(coordinator.failLoad(second), true);
  assert.equal(coordinator.getActiveLoad(), null);
  assert.equal(coordinator.getActiveContext(), null);
});

test('cancel aborts active load and clears its context', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const operation = coordinator.beginLoad(selection('A')).operation;
  coordinator.commitLoad(operation, 'card-A');

  assert.equal(coordinator.cancelLoad(), true);
  assert.equal(operation.signal.aborted, true);
  assert.equal(operation.isCurrent(), false);
  assert.equal(coordinator.getActiveLoad(), null);
  assert.equal(coordinator.getActiveContext(), null);
  assert.equal(coordinator.cancelLoad(), false);
});

test('current load requires a non-empty card id while stale load returns null', () => {
  const coordinator = createDeliveryConfigSessionCoordinator();
  const first = coordinator.beginLoad(selection('A')).operation;

  assert.throws(() => coordinator.commitLoad(first, '  '), TypeError);
  assert.equal(first.isCurrent(), true);

  coordinator.beginLoad(selection('B'));
  assert.equal(coordinator.commitLoad(first, '  '), null);
});

test('uses an injected AbortController', () => {
  class InjectedAbortController {
    constructor() {
      this.signal = { aborted: false, source: 'injected' };
    }

    abort() {
      this.signal.aborted = true;
    }
  }

  const coordinator = createDeliveryConfigSessionCoordinator({
    AbortController: InjectedAbortController,
  });
  const first = coordinator.beginLoad(selection('A')).operation;

  assert.equal(first.signal.source, 'injected');
  coordinator.beginLoad(selection('B'));
  assert.equal(first.signal.aborted, true);
});
