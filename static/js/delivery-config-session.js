'use strict';

(function deliveryConfigSessionModule(root, factory) {
  const api = factory(root);

  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.DeliveryConfigSession = api;
  }
}(typeof globalThis !== 'undefined' ? globalThis : this, function createApi(root) {
  function trimRequired(value, name) {
    if (typeof value !== 'string' || value.trim() === '') {
      throw new TypeError(`${name} is required`);
    }
    return value.trim();
  }

  function normalizeSelection(selection) {
    if (!selection || typeof selection !== 'object') {
      throw new TypeError('selection is required');
    }

    return Object.freeze({
      accountId: trimRequired(selection.accountId, 'accountId'),
      itemId: trimRequired(selection.itemId, 'itemId'),
      itemTitle: typeof selection.itemTitle === 'string' ? selection.itemTitle.trim() : '',
    });
  }

  function createDeliveryConfigSessionCoordinator(options = {}) {
    const AbortControllerConstructor = options.AbortController || root.AbortController;
    if (typeof AbortControllerConstructor !== 'function') {
      throw new TypeError('AbortController is required');
    }

    let generation = 0;
    let writeId = 0;
    let activeLoad = null;
    let activeContext = null;
    let activeWrite = null;
    const loadControllers = new WeakMap();

    function isCurrentLoad(operation) {
      return operation !== null && activeLoad === operation && !operation.signal.aborted;
    }

    function beginLoad(selection) {
      const normalizedSelection = normalizeSelection(selection);

      if (activeWrite) {
        return Object.freeze({
          accepted: false,
          reason: 'write_in_progress',
          context: activeWrite.context,
        });
      }

      if (activeLoad) {
        loadControllers.get(activeLoad).abort();
      }
      activeLoad = null;
      activeContext = null;

      const controller = new AbortControllerConstructor();
      const operation = {};
      Object.assign(operation, {
        generation: ++generation,
        selection: normalizedSelection,
        signal: controller.signal,
        isCurrent: () => isCurrentLoad(operation),
      });
      Object.freeze(operation);
      loadControllers.set(operation, controller);
      activeLoad = operation;

      return Object.freeze({ accepted: true, operation });
    }

    function commitLoad(operation, cardId) {
      if (!isCurrentLoad(operation)) {
        return null;
      }

      const context = Object.freeze({
        accountId: operation.selection.accountId,
        itemId: operation.selection.itemId,
        itemTitle: operation.selection.itemTitle,
        cardId: trimRequired(cardId, 'cardId'),
      });
      activeContext = context;
      return context;
    }

    function finishLoad(operation) {
      if (!isCurrentLoad(operation)) {
        return false;
      }
      activeLoad = null;
      return true;
    }

    function failLoad(operation) {
      if (!isCurrentLoad(operation)) {
        return false;
      }
      activeLoad = null;
      activeContext = null;
      return true;
    }

    function cancelLoad() {
      if (!activeLoad) {
        return false;
      }
      loadControllers.get(activeLoad).abort();
      activeLoad = null;
      activeContext = null;
      return true;
    }

    function beginWrite(kind) {
      if (activeLoad || activeWrite || !activeContext) {
        return null;
      }

      const write = Object.freeze({
        id: ++writeId,
        kind: typeof kind === 'string' ? kind.trim() : '',
        context: Object.freeze({ ...activeContext }),
      });
      activeWrite = write;
      return write;
    }

    function endWrite(write) {
      if (activeWrite !== write) {
        return false;
      }
      activeWrite = null;
      return true;
    }

    return Object.freeze({
      beginLoad,
      commitLoad,
      finishLoad,
      failLoad,
      cancelLoad,
      beginWrite,
      endWrite,
      isWriteCurrent: (write) => activeWrite === write,
      isWriteActive: () => activeWrite !== null,
      getActiveContext: () => activeContext,
      getActiveLoad: () => activeLoad,
    });
  }

  return Object.freeze({ createDeliveryConfigSessionCoordinator });
}));
