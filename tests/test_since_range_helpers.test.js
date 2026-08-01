// Regression test for the shared date-range "since" bound helpers
// (daygleSinceParamForRange / daygleLocalDayStartIso) in web/utils.js.
//
// The alerts page "Today" filter and the dashboard range presets send a
// `since` ISO bound to /api/alerts, /api/events and /api/stats. The backend
// compares stored UTC ISO timestamps lexically (`created_at >= ?`), so the
// bound MUST be the START OF THE LOCAL DAY expressed in UTC — NOT the UTC
// date string. The old code sent `new Date().toISOString().split('T')[0]`
// (the UTC date), which for operators in timezones AHEAD of UTC silently
// dropped every alert fired between local midnight and UTC midnight (those
// rows carry yesterday's UTC date): the alerts page showed 1 of 6 alerts for
// "Today" while "7d" showed all of them.
//
// utils.js reaches for the browser surface at load (window.addEventListener
// in the storage/theme/broadcast subscribers), so we load it into a vm
// context behind a lightweight window stub — the same pattern as
// test_motion_boundary_js.test.js.
//
// Run with:
//   node --test tests/test_since_range_helpers.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');

const sandbox = {
  window: {
    addEventListener() {},
    removeEventListener() {},
  },
  BroadcastChannel: undefined,
};
sandbox.window.daygleUi = null; // utils.js overwrites this on load
vm.createContext(sandbox);
vm.runInContext(utilsSource, sandbox);

const ui = sandbox.window.daygleUi;
assert.ok(ui && typeof ui.daygleSinceParamForRange === 'function',
  'utils.js should expose daygleSinceParamForRange on window.daygleUi');
assert.ok(ui && typeof ui.daygleLocalDayStartIso === 'function',
  'utils.js should expose daygleLocalDayStartIso on window.daygleUi');

// Local helper that mirrors the expected semantics: local midnight N days ago,
// as the browser (and therefore the vm sandbox) sees it.
function localMidnightUtcIso(daysAgo) {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
}

test('since range: "all" sends no since filter', () => {
  assert.equal(ui.daygleSinceParamForRange('all'), '');
  assert.equal(ui.daygleSinceParamForRange('unknown-range'), '');
});

test('since range: "today" is local midnight, not the UTC date string', () => {
  const since = ui.daygleSinceParamForRange('today');
  // The regression: the old bound was a bare 'YYYY-MM-DD' UTC date (e.g.
  // '2026-08-01'), which dropped alerts fired between local and UTC midnight
  // for timezones ahead of UTC. The fixed bound is a full ISO timestamp whose
  // instant is LOCAL midnight converted to UTC.
  assert.match(since, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/,
    'today since must be a full ISO timestamp (with a time-of-day), not a bare date');
  assert.equal(since, localMidnightUtcIso(0),
    'today since must equal local midnight today expressed in UTC');
  // Parse the bound back in the local timezone: it must land on 00:00:00 local.
  const parsed = new Date(since);
  assert.equal(parsed.getHours(), 0, 'bound must represent local midnight (00:00 local)');
  assert.equal(parsed.getMinutes(), 0);
  assert.equal(parsed.getSeconds(), 0);
});

test('since range: "7d" is local midnight 7 days ago', () => {
  const since = ui.daygleSinceParamForRange('7d');
  assert.match(since, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/, '7d since must be a full ISO timestamp');
  assert.equal(since, localMidnightUtcIso(7), '7d since must equal local midnight 7 days ago');
  const parsed = new Date(since);
  assert.equal(parsed.getHours(), 0);
});

test('since range: "30d" is local midnight 30 days ago', () => {
  const since = ui.daygleSinceParamForRange('30d');
  assert.match(since, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/, '30d since must be a full ISO timestamp');
  assert.equal(since, localMidnightUtcIso(30), '30d since must equal local midnight 30 days ago');
  const parsed = new Date(since);
  assert.equal(parsed.getHours(), 0);
});
