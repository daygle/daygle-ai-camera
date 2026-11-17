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

test('the Events AI Check filter round-trips through the URL only where shown', () => {
  const sandbox = createSandbox();
  const shown = '{ range: true, camera: true, sort: true, aiCheck: true }';
  const search = run(sandbox, `librarySearchFromState({ ...libraryDefaultState(), aiCheck: 'filtered' }, '', ${shown})`);
  assert.equal(new URLSearchParams(search).get('ai_check'), 'filtered');
  assert.equal(run(sandbox, `libraryStateFromSearch(${JSON.stringify(search)}, ['all'], ${shown}).aiCheck`), 'filtered');
  assert.equal(run(sandbox, `libraryQueryForState({ ...libraryDefaultState(), aiCheck: 'unverified' }).ai_check`), 'unverified');
  // Recordings and Snapshots do not show it, so it is neither read nor written there.
  assert.equal(run(sandbox, `libraryStateFromSearch('?ai_check=filtered', ['all']).aiCheck`), '');
  assert.equal(run(sandbox, `libraryStateFromSearch('?ai_check=bogus', ['all'], ${shown}).aiCheck`), '');
  assert.match(read('events.js'), /showAiCheck: true/);
  assert.match(read('events.js'), /params\.set\('ai_check', query\.ai_check\)/);
});

test('the False Positives filter round-trips the URL only where a page shows it', () => {
  const sandbox = createSandbox();
  const shown = run(sandbox, 'libraryControls({ showFalsePositive: true })');
  const state = run(sandbox, `libraryStateFromSearch('?false_positive=hide', ['all'], ${JSON.stringify(shown)})`);
  assert.equal(state.falsePositive, 'hide');
  assert.equal(run(sandbox, `libraryQueryForState(${JSON.stringify(state)})`).false_positive, 'hide');
  assert.match(run(sandbox, `librarySearchFromState(${JSON.stringify(state)}, '', ${JSON.stringify(shown)})`), /false_positive=hide/);
  const hidden = run(sandbox, `libraryStateFromSearch('?false_positive=only', ['all'])`);
  assert.equal(hidden.falsePositive, '', 'a page without the control ignores the parameter');
  const bogus = run(sandbox, `libraryStateFromSearch('?false_positive=all', ['all'], ${JSON.stringify(shown)})`);
  assert.equal(bogus.falsePositive, '');
});

test('the three library pages offer the False Positives filter and send it to the API', () => {
  for (const script of ['events.js', 'snapshots.js', 'recordings.js']) {
    const source = read(script);
    assert.match(source, /showFalsePositive: true/, `${script} shows the filter`);
    assert.match(source, /params\.set\('false_positive', query\.false_positive\)/, `${script} sends it`);
    assert.match(source, /bindFalsePositiveButtons\(/, `${script} wires the mark button`);
  }
});

test('false-positive badge and button render for the right users and states', () => {
  const sandbox = createSandbox();
  assert.equal(run(sandbox, 'eventFalsePositive({ false_positive: 0 })'), null);
  assert.equal(run(sandbox, 'eventFalsePositive({ false_positive: null })'), null);
  const marked = run(sandbox, "eventFalsePositive({ false_positive: 1, false_positive_by: 'ai', false_positive_reason: 'person: a coat' })");
  assert.equal(marked.by, 'ai');
  const badge = run(sandbox, `falsePositiveBadge(${JSON.stringify(marked)})`);
  assert.match(badge, /False Positive · AI/);
  assert.match(badge, /person: a coat/);
  assert.equal(run(sandbox, 'falsePositiveBadge(null)'), '');
  // A mark by hand names who made it (escaped); older marks read "User".
  const byViewer = run(sandbox, "eventFalsePositive({ false_positive: 1, false_positive_by: 'user', false_positive_user: 'sam<b>' })");
  assert.match(run(sandbox, `falsePositiveBadge(${JSON.stringify(byViewer)})`), /False Positive · sam&lt;b&gt;/);
  assert.match(run(sandbox, "falsePositiveBadge({ by: 'user', reason: '' })"), /False Positive · User/);
  assert.match(run(sandbox, "falsePositiveBadge({ by: 'mixed', reason: '', users: ['alex'] })"), /False Positive · AI \+ alex/);
  run(sandbox, 'window.daygleAuth = {}');
  assert.equal(run(sandbox, "falsePositiveButton('event', 5, false)"), '', 'no button before sign-in');
  run(sandbox, "window.daygleAuth = { user: { role: 'viewer' } }");
  assert.match(run(sandbox, "falsePositiveButton('event', 5, false)"), /data-false-positive-value="true"/, 'viewers can mark');
  run(sandbox, "window.daygleAuth = { user: { role: 'admin' } }");
  const mark = run(sandbox, "falsePositiveButton('event', 5, false)");
  assert.match(mark, /data-false-positive-value="true"/);
  assert.match(mark, />False Positive</);
  const unmark = run(sandbox, "falsePositiveButton('recording', 9, true)");
  assert.match(unmark, /data-false-positive-kind="recording"/);
  assert.match(unmark, /data-false-positive-value="false"/);
  assert.match(unmark, />Not False Positive</);
});

test('each detection shows the AI verdict, and a clip shows its strongest one per label', () => {
  const sandbox = createSandbox();
  assert.equal(run(sandbox, 'aiDetectionMark({ label: "person" })'), '');
  assert.match(run(sandbox, 'aiDetectionMark({ ai_status: "confirmed", ai_reason: "a person" })'), /✓[\s\S]*|AI confirmed: a person/);
  assert.match(run(sandbox, 'aiDetectionMark({ ai_status: "rejected" })'), /AI says not there/);
  assert.match(run(sandbox, 'aiDetectionMark({ ai_status: "reclassified", original_label: "dog" })'), /detected as Dog/);
  const recording = { events: [
    { detections: [{ label: 'person', ai_status: 'rejected' }] },
    { detections: [{ label: 'person', ai_status: 'confirmed' }, { label: 'car' }] },
  ] };
  assert.equal(run(sandbox, `recordingLabelVerdict(${JSON.stringify(recording)}, 'person')`).ai_status, 'confirmed');
  assert.equal(run(sandbox, `recordingLabelVerdict(${JSON.stringify(recording)}, 'car')`), null);
  assert.match(run(sandbox, 'JSON.stringify(LIBRARY_AI_CHECKS)'), /object_confirmed/);
});
