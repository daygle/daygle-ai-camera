// Regression tests: a polling page must not flash an error toast while the
// browser is tearing it down.
//
// Symptom: clicking any nav link away from the dashboard produced a red error
// banner that flashed for a few hundred milliseconds - too short to read and
// impossible to act on, because the page was already gone. The cause was not a
// backend failure at all: the browser cancels every in-flight fetch() when the
// document unloads, the pending promise rejects, and the page's catch handler
// toasted that rejection on the way out.
//
// The rejection does NOT look like an AbortError. A navigation-cancelled
// fetch rejects with a network-level TypeError ("Failed to fetch" / "Load
// failed") carrying neither name === 'AbortError' nor an abort code, which is
// why the pre-existing isAbortError() guard did not catch it. isPageLeavingError()
// in web/utils.js latches on pagehide/beforeunload and treats any failure from
// then on as expected.
//
// The helper is exercised behaviourally in a vm sandbox; the dashboard wiring
// is pinned with source assertions because app.js runs DOM-coupled code at
// import.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const readWeb = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');
const utilsSource = readWeb('utils.js');
const appSource = readWeb('app.js');

function loadUtils() {
  const winListeners = new Map();
  const sandbox = {
    window: {
      addEventListener(type, handler) {
        if (!winListeners.has(type)) winListeners.set(type, []);
        winListeners.get(type).push(handler);
      },
      removeEventListener() {},
    },
    document: {
      hidden: false,
      addEventListener() {},
    },
    setInterval() { return 0; },
    clearInterval() {},
    BroadcastChannel: undefined,
  };
  sandbox.window.daygleUi = null; // utils.js overwrites this on load
  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);
  return {
    sandbox,
    fire(type, event = {}) {
      for (const handler of winListeners.get(type) || []) handler(event);
    },
  };
}

// ─── isPageLeavingError (web/utils.js) ─────────────────────────────────────
test('a real failure before navigating is still reported as a failure', () => {
  const { sandbox } = loadUtils();
  assert.equal(
    sandbox.isPageLeavingError(new TypeError('Failed to fetch')),
    false,
    'a genuine offline/network failure must NOT be suppressed before unload',
  );
});

test('a network-level rejection is recognised once the page starts leaving', () => {
  const { sandbox, fire } = loadUtils();
  fire('pagehide');
  // The exact shape a navigation-cancelled fetch produces: a TypeError, not an
  // AbortError, with no abort code for isAbortError() to match on.
  assert.equal(sandbox.isPageLeavingError(new TypeError('Failed to fetch')), true);
  assert.equal(sandbox.isPageLeavingError(new TypeError('Load failed')), true);
});

test('beforeunload latches the same way pagehide does', () => {
  const { sandbox, fire } = loadUtils();
  fire('beforeunload');
  assert.equal(sandbox.isPageLeavingError(new TypeError('Failed to fetch')), true);
});

test('the flag latches: a bfcache restore never un-suppresses errors', () => {
  const { sandbox, fire } = loadUtils();
  fire('pagehide');
  assert.equal(sandbox.isPageLeavingError(new Error('boom')), true);
});

test('an explicit AbortError is recognised without any unload having happened', () => {
  const { sandbox } = loadUtils();
  assert.equal(sandbox.isPageLeavingError({ name: 'AbortError' }), true);
  assert.equal(sandbox.isPageLeavingError({ code: 20 }), true);
  assert.equal(sandbox.isPageLeavingError({ errno: 'ABORT_ERR' }), true);
});

test('the helper is registered as a shared global for the page scripts', () => {
  const eslintConfig = readFileSync(path.resolve(here, '..', 'eslint.config.js'), 'utf8');
  assert.ok(
    eslintConfig.includes('isPageLeavingError'),
    'isPageLeavingError must be declared in WEB_SHARED_GLOBALS or app.js fails no-undef',
  );
});

// ─── Dashboard wiring (web/app.js) ─────────────────────────────────────────
// Every toast on the dashboard used to be reachable from a poll rejection, so
// each of these call sites needs the guard. The pattern is pinned rather than
// executed because app.js touches the DOM at import time.
test('every dashboard error toast is guarded against page-leaving rejections', () => {
  const unguarded = [];
  const lines = appSource.split('\n');
  lines.forEach((line, index) => {
    if (!line.includes('window.showToast?.(error.message, true)')) return;
    // Walk back over the preceding guard lines in the same catch block.
    const preceding = lines.slice(Math.max(0, index - 4), index).join('\n');
    if (!preceding.includes('isPageLeavingError(error)')) {
      unguarded.push(index + 1);
    }
  });
  assert.deepEqual(
    unguarded,
    [],
    `app.js shows an unguarded error toast at line(s) ${unguarded.join(', ')}; ` +
    'a poll rejected by page unload would flash an error banner on navigation',
  );
});

test('the dashboard loads the event feed through a cursor pager, not a full drain', () => {
  assert.ok(
    !/loadEvents[\s\S]{0,400}fetchAllCursorPages/.test(appSource),
    'loadEvents must not drain every page; the feed paints from the first page and streams the rest',
  );
  assert.ok(appSource.includes('eventsPager = createCursorPager(url, ACTIVITY_PAGE_SIZE)'),
    'loadEvents must build a cursor pager');
  assert.ok(appSource.includes('wireActivityLoadMore()'),
    'the streamed feed must wire its Load more control');
  assert.ok(appSource.includes("setLoadMoreSentinel('dashboard-activity'"),
    'the load-more sentinel needs a dashboard-specific key');
});
