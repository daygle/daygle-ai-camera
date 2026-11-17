// The live overlay and its Vision chips hide objects the selected camera does
// not watch. The browser used to decide that from the raw zone rule labels, so
// a ``pet``/``animal`` group rule never drew the cat it alerted on, and an
// alert-only rule (Record off, delivery on an alert schedule) never drew its
// object at all. The server now sends ``watched_labels`` resolved with the
// alert engine's own matcher; the rule-derived list is only the fallback.
//
// live.js runs DOM-coupled code at import, so the two helpers are lifted out of
// the source and evaluated on their own.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const liveSource = readFileSync(path.resolve(here, '../web/live.js'), 'utf8');

function functionSource(name) {
  const start = liveSource.indexOf(`function ${name}(`);
  assert.ok(start !== -1, `${name} should exist`);
  // Top-level functions in live.js end at the first line that is exactly "}".
  const end = liveSource.indexOf('\n}\n', start);
  return liveSource.slice(start, end + 2);
}

function harness(camera) {
  const factory = new Function(`
    let selectedCamera = null;
    let configuredLabels = null;
    let serverWatchedLabels = null;
    ${functionSource('rebuildConfiguredLabels')}
    ${functionSource('liveLabelWatched')}
    return {
      select(camera) { selectedCamera = camera; rebuildConfiguredLabels(); },
      setServer(labels) { serverWatchedLabels = labels ? new Set(labels) : null; },
      watched: liveLabelWatched,
    };
  `);
  const live = factory();
  live.select(camera);
  return live;
}

const scheduleOnlyCamera = {
  detection: {
    zones: [{
      object_rules: [
        { label: 'cat', enabled: true, record_on_detect: false, alert_schedules: [{ push_enabled: true }] },
        { label: 'car', enabled: true, record_on_detect: false, alert_schedules: [{ push_enabled: false }] },
      ],
    }],
  },
};

test('server watched labels decide what the live view shows', () => {
  const live = harness({ detection: { zones: [{ object_rules: [{ label: 'pet', enabled: true }] }] } });
  // Without the server list the raw rule label cannot match the detection.
  assert.equal(live.watched('cat'), false);
  live.setServer(['cat']);
  assert.equal(live.watched('cat'), true);
  assert.equal(live.watched('Cat '), true);
  assert.equal(live.watched('potted plant'), false);
  assert.equal(live.watched('motion'), true);
});

test('the fallback list counts delivery on alert schedules', () => {
  const live = harness(scheduleOnlyCamera);
  assert.equal(live.watched('cat'), true, 'an alert-only rule delivering on a schedule is watched');
  assert.equal(live.watched('car'), false, 'a rule that neither records nor delivers is not');
});

test('the status refresh stores the server list before drawing', () => {
  const start = liveSource.indexOf('async function refreshDetectionStatus(');
  const body = liveSource.slice(start, liveSource.indexOf('\n}\n', start));
  const stored = body.indexOf('serverWatchedLabels = ');
  assert.ok(stored !== -1, 'the refresh should store payload.watched_labels');
  assert.ok(stored < body.indexOf('ingestServerTrackDetections(payload);'));
  assert.ok(stored < body.indexOf('summarizeDetectionStatus(payload'));
  assert.match(functionSource('drawLiveOverlay'), /liveLabelWatched\(d\.label\)/);
});

test('live boxes are placed at the displayed frame, not the wall clock', () => {
  // The picture is a still JPEG refreshed every snapshot interval. Advancing
  // the overlay clock with performance.now() while that frame stays on screen
  // slid moving boxes ahead of the frozen picture.
  const body = functionSource('drawLiveOverlay');
  assert.match(body, /const sourceNowMs = liveAiTrackUsesFrameTimestamp && liveAiTrackFrameTimestamp > 0\s*\? liveAiTrackFrameTimestamp \* 1000/);
  assert.doesNotMatch(liveSource, /liveAiTrackFramePerformanceMs/);
});

test('the events page says a held-back candidate alert was not sent', () => {
  const eventsSource = readFileSync(path.resolve(here, '../web/events.js'), 'utf8');
  const start = eventsSource.indexOf('function aiVerificationInfo(');
  const body = eventsSource.slice(start, eventsSource.indexOf('\n}\n', start) + 2);
  const aiVerificationInfo = new Function(`${body}; return aiVerificationInfo;`)();
  const held = aiVerificationInfo({ status: 'error', delivered: false, labels: {} });
  assert.match(held.summary, /^Notification not sent/);
  const sent = aiVerificationInfo({ status: 'error', delivered: true, labels: {} });
  assert.match(sent.summary, /sent unverified/);
});
