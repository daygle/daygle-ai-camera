// Regression guard for the Snapshots library's default scope.
//
// /api/snapshots is a cursor walk over every snapshot row, and the page used
// to follow that cursor to the end before first paint - the whole table, page
// after page. The page now sends a `since` bound and loads TODAY's snapshots
// by default (snapshotsFloorMs / snapshotsRequestSinceMs in web/snapshots.js).
//
// These tests pin the three things that make the fast path safe:
//   1. the default lower bound is local midnight today (not the UTC date
//      string, which drops rows for operators east of UTC),
//   2. the request bound is always <= the display floor, so widening the date
//      card fetches the rows the client is about to render (a To Date in the
//      past still loads that day),
//   3. loadSnapshots actually sends `since=` and applyFiltersOrReload only
//      re-requests when the filter state reaches further back than the widest
//      window already in memory.
//
// snapshots.js reaches for the DOM at load (document.getElementById for the
// filter card, document.addEventListener for DOMContentLoaded), so it runs in
// a vm context behind a lightweight element stub - the same pattern as
// test_since_range_helpers.test.js, which loads utils.js first so the shared
// helpers (daygleSinceParamForRange, timeSelectValue, ...) exist.
//
// Run with:
//   node --test tests/test_snapshots_default_scope.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');
const snapshotsSource = readFileSync(path.resolve(here, '../web/snapshots.js'), 'utf8');

// A fresh stub per getElementById call: the page caches elements in `els`, and
// a shared object would make dateFrom/dateTo the same field. querySelector
// hands back a {value:''} select so timeSelectValue() falls back to the
// default 00:00 / 23:55 picks.
function stubElement() {
  return {
    dataset: {},
    style: {},
    hidden: false,
    innerHTML: '',
    value: '',
    textContent: '',
    options: [],
    querySelector() { return stubElement(); },
    querySelectorAll() { return []; },
    addEventListener() {},
    appendChild() {},
    setAttribute() {},
    remove() {},
  };
}

function createSandbox() {
  const sandbox = {
    window: {
      addEventListener() {},
      removeEventListener() {},
    },
    document: {
      getElementById: () => stubElement(),
      addEventListener() {},
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => stubElement(),
      body: { appendChild() {} },
    },
    BroadcastChannel: undefined,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    console,
  };
  sandbox.window.daygleUi = null; // utils.js overwrites this on load
  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);
  vm.runInContext(snapshotsSource, sandbox);
  return sandbox;
}

function localDayIso(daysFromNow) {
  const d = new Date();
  d.setDate(d.getDate() + daysFromNow);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function localMidnightMs(daysFromNow) {
  const d = new Date();
  d.setDate(d.getDate() + daysFromNow);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

function floorFor(sandbox, filters) {
  return vm.runInContext(`snapshotsFloorMs(${JSON.stringify(filters)})`, sandbox);
}

function requestSinceFor(sandbox, filters) {
  return vm.runInContext(
    `new Date(snapshotsRequestSinceMs(${JSON.stringify(filters)})).toISOString()`,
    sandbox,
  );
}

const NO_DATE_FILTERS = { dateFrom: '', dateTo: '', timeFrom: '00:00', timeTo: '23:55' };

test('snapshots default floor is local midnight today', () => {
  const sandbox = createSandbox();
  assert.equal(floorFor(sandbox, NO_DATE_FILTERS), localMidnightMs(0),
    'with no date filters the list should start at local midnight today');
  assert.equal(requestSinceFor(sandbox, NO_DATE_FILTERS),
    sandbox.window.daygleUi.daygleSinceParamForRange('today'),
    'the default request bound must match the shared "today" helper');
});

test('an earlier From Date widens both the floor and the request bound', () => {
  const sandbox = createSandbox();
  const filters = { ...NO_DATE_FILTERS, dateFrom: localDayIso(-10) };
  const expected = localMidnightMs(-10);
  assert.equal(floorFor(sandbox, filters), expected,
    'a past From Date must lower the display floor to that day');
  assert.equal(Date.parse(requestSinceFor(sandbox, filters)), expected,
    'the request bound must follow the From Date so older rows are fetched');
});

test('a To Date in the past still loads that day', () => {
  const sandbox = createSandbox();
  const filters = { ...NO_DATE_FILTERS, dateTo: localDayIso(-3) };
  const expected = localMidnightMs(-3);
  assert.equal(floorFor(sandbox, filters), expected,
    'a past To Date without a From Date must floor the list at that day');
  assert.equal(Date.parse(requestSinceFor(sandbox, filters)), expected,
    'otherwise a past To Date would request nothing newer than today and show an empty list');
});

test('a future From Date never widens the request past today', () => {
  const sandbox = createSandbox();
  const filters = { ...NO_DATE_FILTERS, dateFrom: localDayIso(3) };
  assert.equal(floorFor(sandbox, filters), localMidnightMs(3),
    'the floor keeps the operator-chosen future date');
  assert.equal(Date.parse(requestSinceFor(sandbox, filters)), localMidnightMs(0),
    'the request stays clamped to today so the fetched set is a superset');
});

test('loadSnapshots sends a since bound for today', async () => {
  const sandbox = createSandbox();
  vm.runInContext(
    'var captured = []; fetchAllCursorPages = async (p) => { captured.push(p); return []; };',
    sandbox,
  );
  await vm.runInContext('loadSnapshots()', sandbox);
  const captured = vm.runInContext('captured', sandbox);
  assert.equal(captured.length, 1, 'the first load should issue exactly one request');
  const decoded = decodeURIComponent(captured[0]);
  assert.ok(decoded.startsWith('/api/snapshots?since='),
    `expected a since-bounded request, got ${captured[0]}`);
  assert.ok(decoded.includes(sandbox.window.daygleUi.daygleSinceParamForRange('today')),
    `expected the today bound, got ${decoded}`);
  assert.notEqual(vm.runInContext('loadedSinceMs', sandbox), null,
    'a successful load must record the bound it fetched');
});

test('applyFiltersOrReload re-requests only when the range widens', async () => {
  const sandbox = createSandbox();
  vm.runInContext(
    'var captured = []; fetchAllCursorPages = async (p) => { captured.push(p); return []; };',
    sandbox,
  );
  await vm.runInContext('loadSnapshots()', sandbox);
  assert.equal(vm.runInContext('captured.length', sandbox), 1);

  // Widen: an earlier From Date must trigger a fresh, wider request.
  vm.runInContext(`els.dateFrom.value = ${JSON.stringify(localDayIso(-10))};`, sandbox);
  await vm.runInContext('applyFiltersOrReload()', sandbox);
  const afterWiden = vm.runInContext('captured', sandbox);
  assert.equal(afterWiden.length, 2, 'widening the date range should re-request');
  assert.equal(
    Date.parse(decodeURIComponent(afterWiden[1]).split('since=')[1]),
    localMidnightMs(-10),
    'the second request must carry the widened bound',
  );

  // Narrow back to the default: today's rows are already in memory.
  vm.runInContext('els.dateFrom.value = "";', sandbox);
  await vm.runInContext('applyFiltersOrReload()', sandbox);
  assert.equal(vm.runInContext('captured.length', sandbox), 2,
    'narrowing back to today must reuse the loaded list instead of re-requesting');
});

test('a failed load drops the bound so the next attempt re-requests', async () => {
  const sandbox = createSandbox();
  vm.runInContext(
    'fetchAllCursorPages = async () => { throw new Error("boom"); }; showToast = () => {};',
    sandbox,
  );
  await vm.runInContext('loadSnapshots()', sandbox);
  assert.equal(vm.runInContext('loadedSinceMs', sandbox), null,
    'a failed request must not leave a stale bound behind');
});
