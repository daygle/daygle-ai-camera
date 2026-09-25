// Tests for the request cancellation / coalescing / backoff layer added to
// web/utils.js (Item 14 of the performance roadmap).
//
// The three guarantees under test, each of which was a real defect:
//
//   1. A poll that is superseded before it settles must not write stale data
//      over the newer response. Guaranteed twice over - by aborting the
//      previous request AND by a sequence ticket - because either mechanism
//      alone has a hole.
//
//   2. Concurrent GETs for the same path share ONE network request. The
//      dashboard's several widgets routinely ask for the same endpoint at the
//      same moment.
//
//   3. A poller whose tick outlasts its own period must not stack requests.
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

// A DOMException-shaped abort error, matching what a real fetch() raises.
function abortError() {
  const error = new Error('The operation was aborted.');
  error.name = 'AbortError';
  return error;
}

// A stand-in for an in-flight fetch: resolves/rejects when the test says so,
// and rejects with an AbortError the moment the signal fires - exactly the
// contract the coalescer's abort half relies on. Without that last part a
// superseded request would hang forever instead of settling, and the stale
// promise would never resolve.
function fetchLike(signal) {
  const gate = deferred();
  if (signal) {
    if (signal.aborted) gate.reject(abortError());
    else signal.addEventListener('abort', () => gate.reject(abortError()));
  }
  return gate;
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
    // Top-level `const` bindings in a vm script are lexical, not properties of
    // the global object, so REQUEST_SUPERSEDED is invisible on `sandbox`.
    // Other page scripts see it as a bare global identifier, so evaluate in
    // the context to observe it exactly the way a consumer would.
    evaluate(expression) {
      return vm.runInContext(expression, sandbox);
    },
    fireVisibility(visible) {
      sandbox.document.hidden = !visible;
      for (const handler of docListeners.get('visibilitychange') || []) handler();
    },
  };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ─── createRequestCoalescer: latest wins ──────────────────────────────────

test('a superseded coalesced call resolves with REQUEST_SUPERSEDED, not stale data', async () => {
  const { sandbox, evaluate } = loadUtils();
  const SUPERSEDED = evaluate('REQUEST_SUPERSEDED');
  const run = sandbox.createRequestCoalescer();

  const first = run((signal) => fetchLike(signal).promise);
  const second = run(async () => 'newest');

  // The stale caller must be told it lost, and must NOT receive the winner's
  // value - that is the bug this whole layer exists to prevent.
  assert.equal(await first, SUPERSEDED);
  assert.equal(await second, 'newest');
});

test('superseding aborts the previous request signal', async () => {
  const { sandbox, evaluate } = loadUtils();
  const SUPERSEDED = evaluate('REQUEST_SUPERSEDED');
  const run = sandbox.createRequestCoalescer();

  const seen = [];
  const first = run((signal) => { seen.push(signal); return fetchLike(signal).promise; });
  const second = run((signal) => { seen.push(signal); return 'ok'; });

  assert.equal(seen[0].aborted, true, 'previous signal should be aborted');
  assert.equal(seen[1].aborted, false, 'current signal must stay live');
  assert.equal(await second, 'ok');
  assert.equal(await first, SUPERSEDED);
});

test('the newest call wins even when the stale call ignores its abort signal', async () => {
  const { sandbox, evaluate } = loadUtils();
  const SUPERSEDED = evaluate('REQUEST_SUPERSEDED');
  const run = sandbox.createRequestCoalescer();

  // This task ignores `signal` completely - the sequence ticket alone has to
  // carry the guarantee, because nothing will abort it for us.
  const slow = deferred();
  const stale = run(() => slow.promise);
  const winner = run(async () => ({ value: 'fresh' }));

  assert.equal((await winner).value, 'fresh');
  // The stale response arrives LAST and resolves successfully; the ticket is
  // the only thing stopping it from being handed back to the caller.
  slow.resolve({ value: 'old' });
  assert.equal(await stale, SUPERSEDED);
});

test('a real error still rejects; only cancellation is absorbed', async () => {
  const { sandbox } = loadUtils();
  const run = sandbox.createRequestCoalescer();

  await assert.rejects(
    run(async () => { throw new Error('backend down'); }),
    /backend down/,
  );
});

test('a synchronous throw from the task does not wedge the coalescer', async () => {
  const { sandbox } = loadUtils();
  const run = sandbox.createRequestCoalescer();

  await assert.rejects(run(() => { throw new Error('sync boom'); }), /sync boom/);
  // The next call must still work and must not be treated as superseded.
  assert.equal(await run(async () => 'recovered'), 'recovered');
});

test('isAbortError recognises browser and Node abort spellings', () => {
  const { sandbox } = loadUtils();
  const isAbortError = sandbox.isAbortError;

  assert.equal(isAbortError({ name: 'AbortError' }), true);
  assert.equal(isAbortError({ code: 20 }), true);
  assert.equal(isAbortError({ code: 'ABORT_ERR' }), true);
  assert.equal(isAbortError(new Error('nope')), false);
  assert.equal(isAbortError(null), false);
  assert.equal(isAbortError('AbortError'), false);
});

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

// ─── backoffDelayMs ───────────────────────────────────────────────────────

test('backoff grows exponentially and clamps at maxMs', () => {
  const { sandbox } = loadUtils();
  const delay = (attempt) => sandbox.backoffDelayMs(attempt, { jitter: 0, baseMs: 1000, maxMs: 30000 });

  assert.equal(delay(0), 1000);
  assert.equal(delay(1), 2000);
  assert.equal(delay(2), 4000);
  assert.equal(delay(5), 30000, 'must clamp at maxMs');
  assert.equal(delay(50), 30000, 'a long outage must not schedule hours out');
});

test('backoff jitter stays within bounds and never goes negative', () => {
  const { sandbox } = loadUtils();
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const nominal = sandbox.backoffDelayMs(attempt, { jitter: 0, baseMs: 1000, maxMs: 30000 });
    for (let i = 0; i < 50; i += 1) {
      const value = sandbox.backoffDelayMs(attempt, { jitter: 0.2, baseMs: 1000, maxMs: 30000 });
      assert.ok(value >= nominal * 0.8 - 1, `attempt ${attempt}: ${value} below jitter floor`);
      assert.ok(value <= nominal * 1.2 + 1, `attempt ${attempt}: ${value} above jitter ceiling`);
      assert.ok(value >= 0, 'delay must never be negative');
    }
  }
});

test('backoff normalises a nonsense attempt into the base delay', () => {
  const { sandbox } = loadUtils();
  assert.equal(sandbox.backoffDelayMs(-5, { jitter: 0, baseMs: 1000 }), 1000);
  assert.equal(sandbox.backoffDelayMs(undefined, { jitter: 0, baseMs: 1000 }), 1000);
  assert.equal(sandbox.backoffDelayMs('nope', { jitter: 0, baseMs: 1000 }), 1000);
});
