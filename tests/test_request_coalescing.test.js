// Tests for the request coalescing in web/utils.js (Item 14 of the
// performance roadmap). The two guarantees under test, each of which was a
// real defect:
//
//   1. Concurrent GETs for the same path share ONE network request. The
//      dashboard's several widgets routinely ask for the same endpoint at the
//      same moment.
//
//   2. A poller whose tick outlasts its own period must not stack requests.
//      startPageInterval now skips a tick whose predecessor is still in
//      flight, and must always release that lock, even on rejection.
//
// The helpers are exercised in a vm sandbox against a stub fetch (utils.js
// loads fine behind a window stub; see tests/test_page_polling_suspend.test.js
// for the same pattern).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web', 'utils.js'), 'utf8');

// A promise plus its resolve/reject handles, so a test can settle a stubbed
// fetch at a chosen moment and observe ordering.
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function jsonResponse(payload, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

// Loads utils.js with a controllable fetch. `respond` maps a request to
// either a Response or a deferred promise, letting a test hold a request open
// across a supersede.
function loadUtils({ respond } = {}) {
  const calls = [];
  const docListeners = new Map();
  const sandbox = {
    window: { addEventListener() {}, removeEventListener() {} },
    document: {
      hidden: false,
      addEventListener(type, handler) {
        if (!docListeners.has(type)) docListeners.set(type, []);
        docListeners.get(type).push(handler);
      },
    },
    setInterval() { return 1; },
    clearInterval() {},
    AbortController,
    FormData,
    Symbol,
    Promise,
    setTimeout,
    clearTimeout,
    Math,
    Number,
    JSON,
    Object,
    Array,
    Error,
    TypeError,
    String,
    console,
    fetch: async (url, options = {}) => {
      const record = { url, options, signal: options.signal };
      calls.push(record);
      return respond(record, calls.length - 1);
    },
  };
  sandbox.window.daygleUi = null;
  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);
  return {
    sandbox,
    calls,
    fireVisibility(visible) {
      sandbox.document.hidden = !visible;
      for (const handler of docListeners.get('visibilitychange') || []) handler();
    },
  };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ─── api(): in-flight GET sharing ─────────────────────────────────────────

test('concurrent GETs for one path share a single network request', async () => {
  const gate = deferred();
  const { sandbox, calls } = loadUtils({ respond: () => gate.promise });

  const a = sandbox.api('/api/stats');
  const b = sandbox.api('/api/stats');
  assert.equal(calls.length, 1, 'duplicate GET should not hit the network twice');

  gate.resolve(jsonResponse({ cameras: 2 }));
  const [first, second] = await Promise.all([a, b]);
  assert.deepEqual(first, { cameras: 2 });
  assert.deepEqual(second, { cameras: 2 });
});

test('a later GET is not served from a settled earlier request', async () => {
  const { sandbox, calls } = loadUtils({ respond: () => jsonResponse({ n: calls.length }) });

  assert.deepEqual(await sandbox.api('/api/stats'), { n: 1 });
  assert.deepEqual(await sandbox.api('/api/stats'), { n: 2 });
  assert.equal(calls.length, 2, 'the in-flight entry must be released on settle');
});

test('mutating verbs are never shared', async () => {
  const { sandbox, calls } = loadUtils({ respond: () => jsonResponse({ ok: true }) });

  const first = sandbox.api('/api/rules', { method: 'POST', body: '{}' });
  const second = sandbox.api('/api/rules', { method: 'POST', body: '{}' });
  await Promise.all([first, second]);
  assert.equal(calls.length, 2, 'POSTs must each reach the network');
});

test('a caller-supplied signal opts out of sharing', async () => {
  const gate = deferred();
  const { sandbox, calls } = loadUtils({ respond: () => gate.promise });

  const controlled = sandbox.api('/api/live/frame', { signal: new AbortController().signal });
  const shared = sandbox.api('/api/live/frame');
  assert.equal(calls.length, 2, 'a caller-owned request must not be shared away');

  gate.resolve(jsonResponse({ ok: true }));
  await Promise.all([controlled, shared]);
});

test('a rejected shared GET releases the slot for the next attempt', async () => {
  let attempt = 0;
  const { sandbox } = loadUtils({
    respond: () => {
      attempt += 1;
      if (attempt === 1) return Promise.reject(new Error('network down'));
      return jsonResponse({ recovered: true });
    },
  });

  await assert.rejects(sandbox.api('/api/stats'), /network down/);
  assert.deepEqual(await sandbox.api('/api/stats'), { recovered: true });
});

// ─── startPageInterval: no overlapping ticks ──────────────────────────────

test('a slow poller does not stack overlapping requests', async () => {
  const { sandbox } = loadUtils();
  const gates = [];

  // Each tick returns a promise that stays pending for the rest of the test,
  // simulating a poll slower than its own period.
  const slowPoll = () => {
    const gate = deferred();
    gates.push(gate);
    return gate.promise;
  };
  sandbox.startPageInterval(slowPoll, 1000);
  sandbox._runDayglePagePoller(slowPoll);
  sandbox._runDayglePagePoller(slowPoll);
  sandbox._runDayglePagePoller(slowPoll);

  assert.equal(gates.length, 1, 'only the first tick should start while one is in flight');
  gates[0].resolve();
  await tick();
});

test('a failed poll releases its lock instead of wedging the poller', async () => {
  const { sandbox } = loadUtils();
  let attempts = 0;

  const failingPoll = () => { attempts += 1; return Promise.reject(new Error('boom')); };
  sandbox.startPageInterval(failingPoll, 1000);
  sandbox._runDayglePagePoller(failingPoll);
  await tick();
  sandbox._runDayglePagePoller(failingPoll);
  await tick();

  assert.equal(attempts, 2, 'a rejected poll must not block the next tick');
});

test('a poller returning a non-promise is not treated as in flight', async () => {
  const { sandbox } = loadUtils();
  let calls = 0;
  const syncPoll = () => { calls += 1; };

  sandbox.startPageInterval(syncPoll, 1000);
  sandbox._runDayglePagePoller(syncPoll);
  sandbox._runDayglePagePoller(syncPoll);

  assert.equal(calls, 2);
});

test('a hidden tab still skips polling, and resuming waits for the in-flight tick', async () => {
  const { sandbox, fireVisibility } = loadUtils();
  let calls = 0;
  const gates = [];
  const poll = () => {
    calls += 1;
    const gate = deferred();
    gates.push(gate);
    return gate.promise;
  };
  sandbox.startPageInterval(poll, 1000);

  sandbox._runDayglePagePoller(poll);
  assert.equal(calls, 1);

  fireVisibility(false);
  assert.equal(calls, 1, 'a hidden tab must not poll');

  // Resuming while the first tick is still open must not fire a second one.
  fireVisibility(true);
  assert.equal(calls, 1, 'resume must respect the in-flight guard');

  gates[0].resolve();
  await tick();
});
