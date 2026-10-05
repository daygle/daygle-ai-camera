// The Timeline's filters: the shared filter bar (web/library_filters.js) with
// its time range, camera and sort switched off, applied to the loaded day by
// matchesTimelineFilters() in web/timeline.js - keyword, type, label, face and
// alerted-only - plus the Label/Face options built from the day's clips.
//
// timeline.js touches the DOM at load, so it runs in a vm context behind the
// same lightweight stubs as test_timeline_card_key.test.js.
//
// Run with:
//   node --test tests/test_timeline_filters.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');

function element() {
  return {
    innerHTML: '', textContent: '', value: '', checked: false,
    dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {} },
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
}

function createSandbox(search = '') {
  const sandbox = {
    window: {
      addEventListener() {}, removeEventListener() {},
      location: { search, pathname: '/timeline' },
      // Never resolves: the page bootstrap (and its API calls) stays parked.
      daygleAuthReady: new Promise(() => {}),
      history: null,
    },
    document: {
      getElementById: () => element(),
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: () => element(),
      addEventListener() {}, removeEventListener() {},
    },
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    BroadcastChannel: undefined,
    URLSearchParams,
    console,
  };
  sandbox.window.history = {
    replaceState(_state, _title, url) { sandbox.window.location.search = url.slice(url.indexOf('?') >= 0 ? url.indexOf('?') : url.length); },
  };
  sandbox.window.daygleUi = null;
  vm.createContext(sandbox);
  vm.runInContext(read('utils.js'), sandbox);
  vm.runInContext(read('library_filters.js'), sandbox);
  vm.runInContext(read('clip_timeline.js'), sandbox);
  vm.runInContext(read('timeline.js'), sandbox);
  return sandbox;
}

const person = {
  id: 1, camera_id: 'front', trigger_type: 'object', trigger_label: 'person', labels: ['person'], ai_labels: ['parcel'],
  alerted: true,
  event: { id: 10, metadata: { camera_name: 'Front Door', ai_description: { text: 'A courier in a red jacket.' }, face_identities: { people: [{ person_id: 7, name: 'Alice' }], unknown: 0 } } },
  detections: [{ label: 'person', confidence: 0.9, zone_name: 'Driveway' }],
};
const motion = {
  id: 2, camera_id: 'front', trigger_type: 'motion', labels: [], alerted: false,
  event: { id: 11, metadata: { face_identities: { people: [], unknown: 1 } } },
  detections: [{ label: 'motion', confidence: 0.5 }],
};
const sound = {
  id: 3, camera_id: 'front', trigger_type: 'object', alerted: false,
  event: { id: 12, metadata: { source: 'sound-detection', class_label: 'dog_bark' } },
};

function matching(sandbox, query) {
  sandbox.__query = { ...vm.runInContext('libraryDefaultQuery()', sandbox), ...query };
  sandbox.__recordings = [person, motion, sound];
  return vm.runInContext('__recordings.filter((r) => matchesTimelineFilters(r, __query)).map((r) => r.id)', sandbox);
}

test('the Timeline mounts the shared bar without its range, camera or sort', () => {
  const html = read('timeline.html');
  assert.ok(html.indexOf('/static/library_filters.js') > html.indexOf('/static/utils.js'));
  assert.ok(html.indexOf('/static/library_filters.js') < html.indexOf('/static/timeline.js'));
  assert.match(html, /<div id="timelineFilters"><\/div>/);
  assert.doesNotMatch(html, /timelineFilterSelect/);
  const js = read('timeline.js');
  assert.match(js, /showRange: false,\n\s+showCamera: false,\n\s+showSort: false,\n\s+facets: 'manual',/);
});

test('type, label, face, alerted-only and keyword filters narrow the day', () => {
  const sandbox = createSandbox();
  assert.deepEqual(matching(sandbox, {}), [1, 2, 3]);
  assert.deepEqual(matching(sandbox, { type: 'motion' }), [2]);
  assert.deepEqual(matching(sandbox, { type: 'sound' }), [3]);
  assert.deepEqual(matching(sandbox, { type: 'object' }), [1]);
  assert.deepEqual(matching(sandbox, { label: 'person' }), [1]);
  assert.deepEqual(matching(sandbox, { label: 'parcel' }), [1], 'AI tags are labels too');
  assert.deepEqual(matching(sandbox, { label: 'motion' }), [2]);
  assert.deepEqual(matching(sandbox, { face: 'id:7' }), [1]);
  assert.deepEqual(matching(sandbox, { face: 'unknown' }), [2]);
  assert.deepEqual(matching(sandbox, { alerted_only: true }), [1]);
  assert.deepEqual(matching(sandbox, { q: 'red JACKET' }), [1], 'AI description, case-insensitive');
  assert.deepEqual(matching(sandbox, { q: 'driveway alice' }), [1], 'zone + face name, every word');
  assert.deepEqual(matching(sandbox, { q: 'driveway dog' }), []);
});

test('Label and Face options come from the loaded day', () => {
  const sandbox = createSandbox();
  sandbox.__recordings = [person, motion, sound];
  const facets = JSON.parse(vm.runInContext('JSON.stringify(timelineFacets(__recordings))', sandbox));
  const labels = Object.fromEntries(facets.labels.map((item) => [item.value, item]));
  assert.equal(labels.person.count, 1);
  assert.equal(labels.parcel.ai, true);
  assert.equal(labels.motion, undefined, 'Motion is offered once, as an extra label');
  assert.deepEqual(facets.faces.people, [{ value: 'id:7', name: 'Alice', count: 1 }]);
  assert.equal(facets.faces.unknown, 1);
});

test('old ?filter= deep links map onto type and label', () => {
  for (const [legacy, key, value] of [['__sound__', 'type', 'sound'], ['motion', 'type', 'motion'], ['person', 'label', 'person']]) {
    const sandbox = createSandbox(`?camera_id=front&filter=${legacy}`);
    vm.runInContext('migrateLegacyFilterParam()', sandbox);
    const params = new URLSearchParams(sandbox.window.location.search);
    assert.equal(params.get('filter'), null);
    assert.equal(params.get(key), value);
    assert.equal(params.get('camera_id'), 'front', 'the Timeline keeps its own camera parameter');
  }
});

test('the bar leaves the Timeline\'s camera and time parameters alone', () => {
  const sandbox = createSandbox();
  const controls = '({ range: false, camera: false, sort: false })';
  const state = vm.runInContext(`libraryStateFromSearch('?camera_id=front&label=person&sort=oldest', ['all'], ${controls})`, sandbox);
  assert.equal(state.camera, '', 'camera_id belongs to the Timeline picker, not the bar');
  assert.equal(state.sort, 'newest');
  assert.equal(state.label, 'person');
  const search = vm.runInContext(`librarySearchFromState({ ...libraryDefaultState(), alerted: true }, '?camera_id=front&day=2026-06-01&from_time=08:00', ${controls})`, sandbox);
  const params = new URLSearchParams(search);
  assert.equal(params.get('camera_id'), 'front');
  assert.equal(params.get('day'), '2026-06-01');
  assert.equal(params.get('from_time'), '08:00');
  assert.equal(params.get('alerted'), '1');
  assert.equal(params.get('range'), null);
});
