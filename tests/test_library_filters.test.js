// Regression guards for the filter bar shared by the Events, Recordings and
// Snapshots pages (web/library_filters.js).
//
// The pure helpers (time window, URL <-> state) run in a vm context after
// utils.js, the same pattern as test_since_range_helpers.test.js. The page
// scripts are checked for the wiring that keeps the three pages unified: each
// loads the shared bar, mounts it, and maps its state onto the list API.
//
// Run with:
//   node --test tests/test_library_filters.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');

function stubElement() {
  return {
    dataset: {},
    style: {},
    hidden: false,
    innerHTML: '',
    value: '',
    textContent: '',
    options: [],
    classList: { add() {}, toggle() {}, remove() {} },
    querySelector() { return stubElement(); },
    querySelectorAll() { return []; },
    addEventListener() {},
    insertAdjacentHTML() {},
    appendChild() {},
    setAttribute() {},
    remove() {},
  };
}

function createSandbox(pageScript) {
  const sandbox = {
    window: { addEventListener() {}, removeEventListener() {}, location: { search: '' } },
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
  vm.runInContext(read('utils.js'), sandbox);
  vm.runInContext(read('library_filters.js'), sandbox);
  if (pageScript) vm.runInContext(read(pageScript), sandbox);
  return sandbox;
}

const run = (sandbox, code) => vm.runInContext(code, sandbox);

function localMidnightMs(daysFromNow) {
  const d = new Date();
  d.setDate(d.getDate() + daysFromNow);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

test('every library page loads the shared bar before its own script and mounts it', () => {
  for (const [page, script, mount] of [
    ['events.html', 'events.js', 'eventFilters'],
    ['recordings.html', 'recordings.js', 'recordingFilters'],
    ['snapshots.html', 'snapshots.js', 'snapshotFilters'],
  ]) {
    const html = read(page);
    const shared = html.indexOf('/static/library_filters.js');
    assert.ok(shared > html.indexOf('/static/utils.js'), `${page} must load library_filters.js after utils.js`);
    assert.ok(shared < html.indexOf(`/static/${script}`), `${page} must load library_filters.js before ${script}`);
    assert.match(html, new RegExp(`<div id="${mount}"></div>`));
    assert.match(read(script), /createLibraryFilters\(\{/);
    // The old per-page forms (and their Apply buttons) are gone.
    assert.doesNotMatch(html, /Apply Filters/);
  }
});

test('Delete All Recordings stays in the library header', () => {
  const html = read('recordings.html');
  const header = html.slice(html.indexOf('<h2>Recordings Library</h2>'), html.indexOf('id="recordingFilters"'));
  assert.match(header, /id="deleteAllRecordingsBtn"/);
});

test('the default window starts at local midnight today', () => {
  const sandbox = createSandbox();
  const query = run(sandbox, 'libraryDefaultQuery()');
  assert.equal(Date.parse(query.since), localMidnightMs(0));
  assert.equal(query.until, '');
  assert.equal(query.sort, 'newest');
  assert.equal(query.type, 'all');
});

test('preset ranges resolve to the expected since bound', () => {
  const sandbox = createSandbox();
  const windowFor = (range) => run(sandbox, `libraryFilterWindow({ ...libraryDefaultState(), range: ${JSON.stringify(range)} })`);
  assert.equal(Date.parse(windowFor('7d').since), localMidnightMs(-7));
  assert.equal(Date.parse(windowFor('30d').since), localMidnightMs(-30));
  assert.equal(windowFor('all').since, '', 'All time sends no lower bound');
  const dayAgo = Date.parse(windowFor('24h').since);
  assert.ok(Math.abs(Date.now() - 24 * 3600 * 1000 - dayAgo) < 5000, '24h is a rolling window');
});

test('a custom range covers both chosen minutes in local time', () => {
  const sandbox = createSandbox();
  const result = run(sandbox, `libraryFilterWindow({ ...libraryDefaultState(), range: 'custom',
    dateFrom: '2026-03-04', timeFrom: '08:15', dateTo: '2026-03-05', timeTo: '17:30' })`);
  assert.equal(Date.parse(result.since), new Date(2026, 2, 4, 8, 15, 0, 0).getTime());
  assert.equal(Date.parse(result.until), new Date(2026, 2, 5, 17, 30, 59, 999).getTime());
  const open = run(sandbox, `libraryFilterWindow({ ...libraryDefaultState(), range: 'custom', dateFrom: '', dateTo: '2026-03-05' })`);
  assert.equal(open.since, '', 'an open-ended From sends no lower bound');
});

test('historical deep links (?label=, ?camera_id=, ?face=) still filter', () => {
  const sandbox = createSandbox();
  const state = run(sandbox, `libraryStateFromSearch('?label=Person&camera_id=driveway&face=id:7', ['all', 'object'])`);
  assert.equal(state.label, 'person');
  assert.equal(state.camera, 'driveway');
  assert.equal(state.face, 'id:7');
  assert.equal(state.range, 'today');
});

test('the URL round-trips the state and keeps unrelated parameters', () => {
  const sandbox = createSandbox();
  const search = run(sandbox, `librarySearchFromState({ ...libraryDefaultState(), q: 'red car', range: 'custom',
    dateFrom: '2026-03-04', timeFrom: '08:15', dateTo: '', type: 'motion', alerted: true, sort: 'oldest' },
    '?recording_id=42&label=dog')`);
  const params = new URLSearchParams(search);
  assert.equal(params.get('recording_id'), '42', 'unrelated parameters survive');
  assert.equal(params.get('label'), null, 'a cleared filter leaves the URL');
  assert.equal(params.get('q'), 'red car');
  assert.equal(params.get('from'), '2026-03-04T08:15');
  const state = run(sandbox, `libraryStateFromSearch(${JSON.stringify(search)}, ['all', 'object', 'motion'])`);
  assert.equal(state.range, 'custom');
  assert.equal(state.dateFrom, '2026-03-04');
  assert.equal(state.timeFrom, '08:15');
  assert.equal(state.type, 'motion');
  assert.equal(state.alerted, true);
  assert.equal(state.sort, 'oldest');
  assert.equal(run(sandbox, 'librarySearchFromState(libraryDefaultState(), "")'), '', 'defaults keep the URL clean');
});

test('snapshots open on today without a filter bar and stream one page', async () => {
  const sandbox = createSandbox('snapshots.js');
  run(sandbox, `var captured = [];
    createCursorPager = (p) => { captured.push(p); return { done: true, loadPage: async () => ({ items: [], done: true }) }; };`);
  await run(sandbox, 'loadSnapshots()');
  const captured = run(sandbox, 'captured');
  assert.equal(captured.length, 1);
  const since = new URLSearchParams(captured[0].split('?')[1]).get('since');
  assert.equal(Date.parse(since), localMidnightMs(0), 'the first load must stay bounded to today');
});

test('events and snapshots send every bar filter to the server', () => {
  const query = `({ since: 'S', until: 'U', q: 'red car', camera_id: 'cam1', label: 'person',
    face: 'unknown', alerted_only: true, sort: 'oldest', type: 'motion' })`;
  for (const [script, fn] of [['events.js', 'eventsQueryString'], ['snapshots.js', 'snapshotsQueryString']]) {
    const sandbox = createSandbox(script);
    const params = new URLSearchParams(run(sandbox, `${fn}(${query})`));
    assert.deepEqual(Object.fromEntries(params), {
      since: 'S', until: 'U', q: 'red car', camera_id: 'cam1', label: 'person',
      face: 'unknown', alerted_only: 'true', sort: 'oldest',
    }, `${script} must forward the server-side filters (type stays client-side)`);
  }
});

test('recordings map the window to started_after/before and type to source_type', () => {
  const html = read('recordings.js');
  const start = html.indexOf('function recordingsQueryParams(query) {');
  const body = html.slice(start, html.indexOf('\n}\n', start));
  assert.match(body, /params\.set\('started_after', query\.since\)/);
  assert.match(body, /params\.set\('started_before', query\.until\)/);
  assert.match(body, /query\.label && query\.label !== 'motion'/, 'Motion filters client-side; the server strips generic labels');
  assert.match(body, /params\.set\('source_type', 'sound'\)/);
});

test('the collapsed panel counts its active filters and the caret flips', () => {
  const js = read('library_filters.js');
  assert.match(js, /els\.badge\.hidden = !count;/);
  assert.match(js, /els\.more\.classList\.toggle\('is-filtered', count > 0\)/);
  assert.match(js, /try \{ localStorage\.setItem\(LIBRARY_FILTER_PANEL_KEY/);
  const css = readFileSync(path.resolve(here, '../web/styles.css'), 'utf8');
  assert.match(css, /\.library-more-toggle\[aria-expanded="true"\] \.library-filter-caret \{ transform: rotate\(180deg\); \}/);
});
