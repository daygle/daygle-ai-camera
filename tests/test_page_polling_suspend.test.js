// Regression tests: every polling page must stop polling while its tab is
// hidden, and must refresh the moment the tab is looked at again.
//
// The Live page already had that behaviour inline (see
// tests/test_live_hidden_tab_poll.test.js). The other polling pages - the
// dashboard (stats every 10s, system resources every 5s, activity every 30s),
// the camera list (health + resolutions every 10s), the alert policy statuses
// (30s), the settings tunnel status (15s) and the application-log live tail -
// used to keep hitting the backend after the operator switched tabs, all night
// if the NVR was left open. They now share startPageInterval() from
// web/utils.js.
//
// The helper is exercised behaviourally in a vm sandbox (utils.js loads fine
// behind a window stub); the page wiring is pinned with source assertions
// because those scripts run DOM-coupled code at import.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const readWeb = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');
const utilsSource = readWeb('utils.js');

// ─── startPageInterval (web/utils.js) ─────────────────────────────────────
function loadUtils() {
  const docListeners = new Map();
  const timers = [];
  const sandbox = {
    window: {
      addEventListener() {},
      removeEventListener() {},
    },
    document: {
      hidden: false,
      addEventListener(type, handler) {
        if (!docListeners.has(type)) docListeners.set(type, []);
        docListeners.get(type).push(handler);
      },
    },
    setInterval(fn, ms) {
      timers.push({ fn, ms });
      return timers.length;
    },
    clearInterval() {},
    BroadcastChannel: undefined,
  };
  sandbox.window.daygleUi = null; // utils.js overwrites this on load
  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);
  return {
    sandbox,
    timers,
    fireVisibility(visible) {
      sandbox.document.hidden = !visible;
      for (const handler of docListeners.get('visibilitychange') || []) handler();
    },
  };
}

test('startPageInterval is a shared global for the polling page scripts', () => {
  const { sandbox } = loadUtils();
  assert.equal(typeof sandbox.startPageInterval, 'function');
});

test('a poller ticks on its interval while the tab is visible', () => {
  const { sandbox, timers } = loadUtils();
  let calls = 0;
  const handle = sandbox.startPageInterval(() => { calls += 1; }, 10000);

  assert.ok(handle, 'returns the timer handle so a page can clear it');
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 10000);
  timers[0].fn();
  assert.equal(calls, 1);
});

test('a poller skips its tick while the tab is hidden', () => {
  const { sandbox, timers } = loadUtils();
  let calls = 0;
  sandbox.startPageInterval(() => { calls += 1; }, 5000);

  sandbox.document.hidden = true;
  timers[0].fn();
  assert.equal(calls, 0, 'a backgrounded page must not hit the API');
});

test('every poller refreshes once when the tab becomes visible again', () => {
  const { sandbox, fireVisibility } = loadUtils();
  const calls = { stats: 0, health: 0 };
  sandbox.startPageInterval(() => { calls.stats += 1; }, 10000);
  sandbox.startPageInterval(() => { calls.health += 1; }, 10000);

  fireVisibility(false);
  fireVisibility(true);
  assert.deepEqual(calls, { stats: 1, health: 1 });

  // Only one visibilitychange listener is registered for the whole page.
  fireVisibility(true);
  assert.deepEqual(calls, { stats: 2, health: 2 });
});

test('a rejected poll promise does not surface as an unhandled rejection', () => {
  const { sandbox, timers } = loadUtils();
  sandbox.startPageInterval(() => Promise.reject(new Error('offline')), 5000);
  assert.doesNotThrow(() => timers[0].fn());
});

test('a bad poller registration is ignored instead of throwing', () => {
  const { sandbox, timers } = loadUtils();
  assert.equal(sandbox.startPageInterval(null, 1000), null);
  assert.equal(sandbox.startPageInterval(() => {}, 0), null);
  assert.equal(timers.length, 0);
});

// ─── Page wiring ──────────────────────────────────────────────────────────
test('the dashboard suspends stats, system resources and the activity feed', () => {
  const source = readWeb('app.js');
  assert.doesNotMatch(source, /^setInterval\(/m, 'no raw setInterval on the dashboard');
  for (const call of ['loadStats()', 'loadSystemResources()', 'loadEvents()']) {
    assert.ok(
      new RegExp(`startPageInterval\\([\\s\\S]{0,120}?${call.replace(/[()]/g, '\\$&')}`).test(source),
      `${call} should run through startPageInterval`,
    );
  }
});

test('the camera list suspends health and resolution polling', () => {
  const source = readWeb('cameras.js');
  assert.doesNotMatch(source, /^setInterval\(/m, 'no raw setInterval on the camera list');
  assert.ok(source.includes('startPageInterval(updateHealthStats, 10000);'));
  assert.ok(source.includes('startPageInterval(function() { fetchCameraResolutions()'));
});

test('the alerts page suspends policy status polling', () => {
  const source = readWeb('alerts.js');
  assert.ok(source.includes('startPageInterval(refreshPolicyStatuses, 30_000);'));
  assert.doesNotMatch(source, /^setInterval\(/m, 'no raw setInterval on the alerts page');
});

test('the settings page delegates its tunnel-status hidden check to the helper', () => {
  const source = readWeb('settings.js');
  assert.ok(source.includes('startPageInterval(() => { refreshCloudflareTunnelSafely(); }, 15000);'));
  // The helper owns the document.hidden check now; a leftover inline one would
  // mean the interval ran (and bailed) instead of being suspended.
  assert.doesNotMatch(source, /setInterval\(\(\) => \{\s*if \(!document\.hidden\)/);
});

test('the application log drops the live tail while hidden and catches up on return', () => {
  const source = readWeb('application-log.js');
  assert.ok(source.includes('function suspendStream()'), 'a suspend path must exist');
  assert.ok(source.includes("document.addEventListener('visibilitychange'"), 'must react to tab switches');
  // Live mode is tracked separately from the connection so suspending is not
  // mistaken for the operator pausing the tail.
  assert.ok(source.includes('let liveWanted = false;'));
  assert.ok(source.includes('if (liveWanted) {'), 'the Live button toggles liveWanted, not the connection');
  assert.ok(
    source.includes('loadEntries()') && source.includes('if (liveWanted && !eventSource) connectStream();'),
    'returning to the tab reloads the tail before reopening the stream',
  );
});
