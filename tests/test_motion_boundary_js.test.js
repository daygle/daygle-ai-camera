// Regression test suite for the motion-vs-object boundary.
//
// The boundary is implemented once in web/utils.js (shared by the
// recordings list, recordings playback modal, timeline page and the
// dashboard activity feed). To make sure a future refactor can't silently
// widen or narrow the "motion-only" classification - which would change
// what surfaces as Motion vs Object across the app - this suite loads
// utils.js into a sandboxed vm context and runs the helpers against a
// battery of canonical payload shapes.
//
// Run with:
//   node --test tests/test_motion_boundary_js.test.js
// Or, from the repo root:
//   node --test tests/

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');

// utils.js touches a tiny bit of the browser surface at load time:
// - `window.addEventListener('storage', ...)` inside subscribeDaygleDatePrefs.
// - `window.daygleUi = { ... }` exposing the public surface.
// BroadcastChannel is referenced via `typeof === 'function'` so its
// absence in Node is fine. Stubbing `window` with no-op DOM listeners
// keeps the load side-effect-free.
const sandbox = {
  window: {
    addEventListener() {},
    removeEventListener() {},
  },
  // typeof BroadcastChannel === 'function' must return false so the
  // subscribe path skips the channel constructor.
  BroadcastChannel: undefined,
};
sandbox.window.daygleUi = null; // utils.js overwrites this on load
vm.createContext(sandbox);
vm.runInContext(utilsSource, sandbox);

const ui = sandbox.window.daygleUi;
assert.ok(ui && ui.isMotionOnlyRecording, 'utils.js should expose isMotionOnlyRecording on window.daygleUi');

const {
  isMotionOnlyRecording,
  isMotionOnlyEvent,
  isMotionOnlyEventItem,
  motionConfidenceFor,
  GENERIC_TRIGGER_LABELS,
  isSoundRecording,
  recordingDetectionSummary,
  recordingZoneNames,
  recordingEventPills,
} = ui;

// ─── Shared recording readers (recordings list + timeline) ─────────────────
// recordingDetectionSummary() and friends are hoisted into utils.js so the
// recordings list and the timeline page share one copy and cannot drift
// (the Dog Bark legend bug was a drift between the two surfaces).
//
// The helpers build their return values inside the vm realm, whose Array /
// Object prototypes differ from the test realm - so deepStrictEqual's
// reference-equal prototype check fails on otherwise-identical structures.
// Round-trip through JSON to re-home the values in the test realm first.
const plain = (value) => JSON.parse(JSON.stringify(value));

test('isSoundRecording keys off event.metadata.source', () => {
  assert.equal(isSoundRecording({ event: { metadata: { source: 'sound-detection' } } }), true);
  assert.equal(isSoundRecording({ event: { metadata: { source: 'live-stream' } } }), false);
  assert.equal(isSoundRecording({}), false);
  assert.equal(isSoundRecording(null), false);
});

test('recordingDetectionSummary: sound recording collapses to its class label', () => {
  const summary = recordingDetectionSummary({
    event: { metadata: { source: 'sound-detection', class_label: 'Dog Bark', confidence: 0.82 } },
  });
  assert.deepEqual(plain(summary), [{ label: 'dog bark', confidence: 0.82 }]);
});

test('recordingDetectionSummary: objects dedupe per label, keeping best confidence', () => {
  // Two people + two dogs -> two pills (Person, Dog), not four; each carries
  // the highest-seen confidence for that class, sorted descending.
  const summary = recordingDetectionSummary({
    labels: ['person', 'dog'],
    detections: [
      { label: 'person', confidence: 0.71 },
      { label: 'person', confidence: 0.93 },
      { label: 'dog', confidence: 0.66 },
      { label: 'dog', confidence: 0.80 },
    ],
  });
  assert.deepEqual(plain(summary), [
    { label: 'person', confidence: 0.93 },
    { label: 'dog', confidence: 0.80 },
  ]);
});

test('recordingDetectionSummary: includes labels added after the original event', () => {
  // The event may contain only the trigger object, while an extended clip's
  // recording_labels table contains later objects seen in the same footage.
  const summary = recordingDetectionSummary({
    labels: ['bird', 'cat'],
    detections: [{ label: 'bird', confidence: 0.69 }],
    label_confidences: { bird: 0.69, cat: 0.19 },
  });
  assert.deepEqual(plain(summary), [
    { label: 'bird', confidence: 0.69 },
    { label: 'cat', confidence: 0.19 },
  ]);
});

test('recordingDetectionSummary: generic trigger labels are filtered out', () => {
  const summary = recordingDetectionSummary({
    labels: ['motion', 'person'],
    detections: [{ label: 'person', confidence: 0.5 }],
  });
  assert.deepEqual(plain(summary), [{ label: 'person', confidence: 0.5 }]);
});

test('recordingZoneNames: empty for sounds, deduped for objects', () => {
  assert.deepEqual(plain(recordingZoneNames({ event: { metadata: { source: 'sound-detection' } } })), []);
  assert.deepEqual(
    plain(recordingZoneNames({ detections: [{ zone_name: 'Drive' }, { zone_name: 'Drive' }, { zone_name: 'Porch' }, {}] })),
    ['Drive', 'Porch'],
  );
});

test('recordingZoneNames: folds in zone names from the detection track, deduped', () => {
  // Objects detected after the trigger live in recording.track samples; their
  // zone names must surface too, without duplicating the event detections.
  const recording = {
    detections: [{ zone_name: 'Drive' }],
    track: [
      { detections: [{ zone_name: 'Drive' }, { zone_name: 'Porch' }, {}] },
      { detections: [{ zone_name: 'Porch' }] },
    ],
  };
  assert.deepEqual(plain(recordingZoneNames(recording)), ['Drive', 'Porch']);
});

// ─── GENERIC_TRIGGER_LABELS sanity ─────────────────────────────────────────

test('GENERIC_TRIGGER_LABELS contains the trigger placeholders and excludes concrete labels', () => {
  assert.ok(GENERIC_TRIGGER_LABELS.has('motion'));
  assert.ok(GENERIC_TRIGGER_LABELS.has('human'));
  assert.ok(GENERIC_TRIGGER_LABELS.has('alert'));
  assert.ok(GENERIC_TRIGGER_LABELS.has('object'));
  assert.ok(!GENERIC_TRIGGER_LABELS.has('person'));
  assert.ok(!GENERIC_TRIGGER_LABELS.has('doorbell'));
});

// ─── isMotionOnlyRecording (recording shape from /api/recordings) ────────

const SOUND_RECORDING = {
  event: { metadata: { source: 'sound-detection' } },
  trigger_type: 'motion',
  labels: [],
  detections: [],
};

function makeRecording(overrides = {}) {
  return {
    event: { metadata: {} },
    trigger_type: 'motion',
    labels: ['motion'],
    detections: [{ label: 'motion', confidence: 0.45 }],
    ...overrides,
  };
}

test('isMotionOnlyRecording: motion-only clip → true', () => {
  assert.equal(isMotionOnlyRecording(makeRecording()), true);
});

test('isMotionOnlyRecording: object clip (concrete label) → false', () => {
  assert.equal(isMotionOnlyRecording(makeRecording({
    labels: ['person'],
    detections: [{ label: 'person', confidence: 0.92 }],
  })), false);
});

test('isMotionOnlyRecording: multi-label clip mixes motion + object → false', () => {
  assert.equal(isMotionOnlyRecording(makeRecording({
    labels: ['motion', 'person'],
    detections: [{ label: 'person', confidence: 0.8 }],
  })), false);
});

test('isMotionOnlyRecording: sound clip → false even when trigger = motion', () => {
  assert.equal(isMotionOnlyRecording(SOUND_RECORDING), false);
});

test('isMotionOnlyRecording: continuous clip → false', () => {
  assert.equal(isMotionOnlyRecording(makeRecording({ trigger_type: 'continuous', labels: [], detections: [] })), false);
});

test('isMotionOnlyRecording: trigger_type "none" → false', () => {
  assert.equal(isMotionOnlyRecording(makeRecording({ trigger_type: 'none', labels: [], detections: [] })), false);
});

test('isMotionOnlyRecording: trigger_type "off" → false', () => {
  assert.equal(isMotionOnlyRecording(makeRecording({ trigger_type: 'off', labels: [], detections: [] })), false);
});

test('isMotionOnlyRecording: null → false', () => {
  assert.equal(isMotionOnlyRecording(null), false);
  assert.equal(isMotionOnlyRecording(undefined), false);
});

test('isMotionOnlyRecording: concrete label present only in detections (not in joined labels) → false', () => {
  // recordings_labels join may be empty for freshly-extended clips but the
  // event detections should still surface a concrete label and disqualify
  // motion-only.
  assert.equal(isMotionOnlyRecording(makeRecording({
    labels: [],
    detections: [{ label: 'cat', confidence: 0.7 }],
  })), false);
});

test('isMotionOnlyRecording: human trigger + empty labels/detections → true', () => {
  // Human trigger without concrete findings still classifies as motion-only
  // under the current definition - locks behaviour so future refactors don't
  // accidentally widen the bucket.
  assert.equal(isMotionOnlyRecording(makeRecording({
    trigger_type: 'human',
    labels: [],
    detections: [],
  })), true);
});

// ─── motionConfidenceFor ──────────────────────────────────────────────────

test('motionConfidenceFor: returns the strongest motion confidence across detections + track', () => {
  const rec = {
    detections: [
      { label: 'motion', confidence: 0.3 },
      { label: 'motion', confidence: 0.85 },
      { label: 'person', confidence: 0.95 },
    ],
    track: [
      { detections: [{ label: 'motion', confidence: 0.6 }] },
      { detections: [{ label: 'motion', confidence: 0.5 }, { label: 'cat', confidence: 0.6 }] },
    ],
  };
  assert.equal(motionConfidenceFor(rec), 0.85);
});

test('motionConfidenceFor: no motion confidence → null', () => {
  assert.equal(motionConfidenceFor({ detections: [], track: [] }), null);
  assert.equal(motionConfidenceFor({}), null);
  assert.equal(motionConfidenceFor(null), null);
});

test('motionConfidenceFor: motion present but NaN/invalid conf → null', () => {
  assert.equal(motionConfidenceFor({ detections: [{ label: 'motion', confidence: NaN }] }), null);
  assert.equal(motionConfidenceFor({ detections: [{ label: 'motion' }] }), null);
});

// ─── isMotionOnlyEventItem (merged dashboard activity item, type=event) ───

test('isMotionOnlyEventItem: motion-only merged event item → true', () => {
  assert.equal(isMotionOnlyEventItem({
    type: 'event',
    isSound: false,
    detections: [{ label: 'motion', confidence: 0.5 }],
  }), true);
});

test('isMotionOnlyEventItem: object detection item → false', () => {
  assert.equal(isMotionOnlyEventItem({
    type: 'event',
    isSound: false,
    detections: [{ label: 'person', confidence: 0.9 }],
  }), false);
});

test('isMotionOnlyEventItem: sound item → false', () => {
  assert.equal(isMotionOnlyEventItem({
    type: 'event',
    isSound: true,
    detections: [{ label: 'doorbell', confidence: 0.7 }],
  }), false);
});

test('isMotionOnlyEventItem: empty detections → false (under-recorded sample)', () => {
  assert.equal(isMotionOnlyEventItem({ type: 'event', isSound: false, detections: [] }), false);
});

test('isMotionOnlyEventItem: null/undefined → false', () => {
  assert.equal(isMotionOnlyEventItem(null), false);
  assert.equal(isMotionOnlyEventItem(undefined), false);
});

// ─── isMotionOnlyEvent (raw /api/events payload, used by stat cards) ─────

test('isMotionOnlyEvent: rtsp event with motion-only detections → true', () => {
  assert.equal(isMotionOnlyEvent({
    id: 1,
    source: 'rtsp',
    detections: [{ label: 'motion', confidence: 0.4 }],
  }), true);
});

test('isMotionOnlyEvent: sound event → false', () => {
  assert.equal(isMotionOnlyEvent({
    source: 'sound',
    detections: [{ label: 'doorbell', confidence: 0.8 }],
  }), false);
});

test('isMotionOnlyEvent: object event → false', () => {
  assert.equal(isMotionOnlyEvent({
    source: 'rtsp',
    detections: [{ label: 'person', confidence: 0.92 }],
  }), false);
});

test('isMotionOnlyEvent: event with no detections → false', () => {
  assert.equal(isMotionOnlyEvent({ id: 2, source: 'rtsp', detections: [] }), false);
});

test('isMotionOnlyEvent: null → false', () => {
  assert.equal(isMotionOnlyEvent(null), false);
  assert.equal(isMotionOnlyEvent(undefined), false);
});

// ─── recordingEventPills (per-event type pills on the Recordings list) ────

test('recordingEventPills: object event renders the strongest concrete label', () => {
  const html = recordingEventPills({
    source: 'rtsp',
    detections: [
      { label: 'person', confidence: 0.71 },
      { label: 'person', confidence: 0.93 },
      { label: 'motion', confidence: 0.4 },
    ],
  });
  assert.ok(html.includes('Person'), 'object pill names the label');
  assert.ok(html.includes('93%'), 'object pill carries the strongest confidence');
  assert.ok(!html.includes('Motion'), 'clip-level Motion pill is not duplicated per event');
});

test('recordingEventPills: motion-only event renders the Motion pill', () => {
  const html = recordingEventPills({
    source: 'rtsp',
    detections: [{ label: 'motion', confidence: 0.6 }, { label: 'motion', confidence: 0.4 }],
  });
  assert.ok(html.includes('Motion'), 'motion pill rendered');
  assert.ok(html.includes('60%'), 'strongest motion intensity used');
});

test('recordingEventPills: sound event renders the speaker pill with its class', () => {
  const html = recordingEventPills({
    source: 'sound',
    metadata: { source: 'sound-detection', class_label: 'Dog Bark', confidence: 0.82 },
    detections: [{ label: 'dog_bark', confidence: 0.82 }],
  });
  assert.ok(html.includes('Dog Bark'), 'sound class named');
  assert.ok(html.includes('82%'), 'sound confidence shown');
});

test('recordingEventPills: sound event falls back to metadata when detections are empty', () => {
  const html = recordingEventPills({
    source: 'sound',
    metadata: { source: 'sound-detection', class_label: 'Car Alarm', confidence: 0.9 },
    detections: [],
  });
  assert.ok(html.includes('Car Alarm'));
  assert.ok(html.includes('90%'));
});

test('recordingEventPills: still-alert object event keeps its dwell badge', () => {
  const html = recordingEventPills({
    source: 'rtsp',
    detections: [{ label: 'package', confidence: 0.88, still_alert: true, still_alert_minutes: 12 }],
  });
  assert.ok(html.includes('Package'));
  assert.ok(html.includes('Still 12 Min'));
});

test('recordingEventPills: empty/unusable events render nothing', () => {
  assert.equal(recordingEventPills(null), '');
  assert.equal(recordingEventPills(undefined), '');
  assert.equal(recordingEventPills({ source: 'rtsp', detections: [] }), '');
});
