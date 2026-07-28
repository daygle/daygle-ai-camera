// Regression test for the Timeline legend's object-vs-sound classification.
//
// The legend builds one chip per distinct label. A sound-class label (Dog
// Bark, Car Alarm, ...) must always render as a *sound* chip - speaker icon,
// reserved purple swatch - even when it rides on an object-sourced recording
// rather than a sound-detection one. Keying the chip off isSoundRecording()
// alone (the recording's source) let a sound label leak in a second time as
// an "object" chip with the eye icon, so the legend listed e.g. "Dog Bark"
// twice: once as a sound and once as an object. This pins the fix (each
// label is classified via isSoundLabel(), matching detectionPill()).
//
// timeline.js is a classic <script> that reaches for the DOM, localStorage
// and window at load, so we load it into a vm context alongside utils.js
// (which supplies the shared helpers) behind lightweight stubs, then drive
// renderLegend() directly and inspect the HTML it writes into the legend
// element.
//
// Run with:
//   node --test tests/test_timeline_legend_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');
const timelineSource = readFileSync(path.resolve(here, '../web/timeline.js'), 'utf8');

// getElementById memoises one stub per id so the timeline's `els` map holds
// stable references; the test reads the legend element back out of the store
// after renderLegend() writes to its innerHTML.
const elStore = {};
function makeEl() {
  return {
    innerHTML: '', textContent: '', value: '', checked: false,
    dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, removeEventListener() {}, appendChild() {},
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return makeEl(); }, querySelectorAll() { return []; },
  };
}
function getElementById(id) {
  if (!elStore[id]) elStore[id] = makeEl();
  return elStore[id];
}

const documentStub = {
  getElementById,
  createElement() { return makeEl(); },
  querySelector() { return makeEl(); },
  querySelectorAll() { return []; },
  addEventListener() {}, removeEventListener() {},
};
const localStorageStub = { getItem() { return null; }, setItem() {}, removeItem() {} };

const sandbox = {
  window: {
    addEventListener() {}, removeEventListener() {},
    location: { search: '' },
    daygleAuthReady: Promise.resolve(),
  },
  document: documentStub,
  localStorage: localStorageStub,
  BroadcastChannel: undefined,
  URLSearchParams,
  console,
};
sandbox.window.daygleUi = null;
vm.createContext(sandbox);

// utils.js first (defines isSoundLabel, isSoundRecording, titleCase, ...),
// then timeline.js in the same context so its functions see those globals -
// exactly the <script> ordering in timeline.html.
vm.runInContext(utilsSource, sandbox);
// timeline.js kicks off an async bootstrap (loadAuth -> loadTimeline) at the
// end of load; those reject cleanly against the fetch-less sandbox and are
// swallowed by the page's own .catch, so a synchronous load is enough here.
vm.runInContext(timelineSource, sandbox);

// Drive renderLegend() from inside the context so it resolves colorForKey,
// isSoundLabel, recordingDetectionSummary et al. through the shared scope,
// then read the HTML it wrote into the (stubbed) legend element.
function legendHtmlFor(recordings) {
  sandbox.__recordings = recordings;
  vm.runInContext('renderLegend(__recordings);', sandbox);
  return elStore.timelineLegend.innerHTML;
}

const SPEAKER = '🔊';
const EYE = 'M2 12s3-7 10-7'; // fragment unique to DETECTION_EYE_ICON

const soundRecording = (classLabel, confidence = 0.8) => ({
  event: { metadata: { source: 'sound-detection', class_label: classLabel, confidence } },
});
// An object-sourced recording (not sound-detection) that nonetheless carries a
// sound-class label - the exact shape that produced the duplicate legend chip.
const objectRecordingWithLabel = (label, confidence = 0.6) => ({
  trigger_type: 'object',
  labels: [label],
  detections: [{ label, confidence }],
});

test('legend: a sound-class label on an object recording renders once, as a sound', () => {
  const html = legendHtmlFor([objectRecordingWithLabel('dog bark')]);
  const chipCount = html.split('Dog Bark').length - 1;
  assert.equal(chipCount, 1, 'Dog Bark should appear exactly once in the legend');
  assert.ok(html.includes(SPEAKER), 'the Dog Bark chip should carry the speaker icon');
  assert.ok(!html.includes(EYE), 'the Dog Bark chip must not carry the object (eye) icon');
});

test('legend: sound recording and object recording of the same sound class collapse to one chip', () => {
  const html = legendHtmlFor([
    soundRecording('Dog Bark'),
    objectRecordingWithLabel('dog bark'),
  ]);
  const chipCount = html.split('Dog Bark').length - 1;
  assert.equal(chipCount, 1, 'the two Dog Bark sources should dedupe to a single sound chip');
  assert.ok(!html.includes(EYE), 'no eye icon should appear for a pure sound-class legend');
});

test('legend: real object labels still render as object chips with the eye icon', () => {
  const html = legendHtmlFor([objectRecordingWithLabel('person')]);
  assert.ok(html.includes('Person'), 'Person chip should be present');
  assert.ok(html.includes(EYE), 'a genuine object label keeps the eye icon');
  assert.ok(!html.includes(SPEAKER), 'a genuine object label is not a sound');
});

test('legend: mixed clip contributes an object chip and a sound chip', () => {
  // Person + Dog Bark seen on one object recording -> Person (eye) and
  // Dog Bark (speaker), never Dog Bark twice.
  const html = legendHtmlFor([{
    trigger_type: 'object',
    labels: ['person', 'dog bark'],
    detections: [
      { label: 'person', confidence: 0.9 },
      { label: 'dog bark', confidence: 0.5 },
    ],
  }]);
  assert.equal(html.split('Dog Bark').length - 1, 1, 'Dog Bark once');
  assert.equal(html.split('Person').length - 1, 1, 'Person once');
  assert.ok(html.includes(SPEAKER), 'Dog Bark keeps the speaker icon');
  assert.ok(html.includes(EYE), 'Person keeps the eye icon');
});
