// Behavioral regression tests for the shared playback overlay track sampler.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const overlaySource = readFileSync(path.resolve(here, '../web/overlay.js'), 'utf8');
const sandbox = { window: { devicePixelRatio: 1 } };
vm.createContext(sandbox);
vm.runInContext(overlaySource, sandbox);

const box = (x, y = 0.2) => ({ x, y, width: 0.2, height: 0.3 });
const detection = (x, confidence = 0.8) => ({ label: 'person', confidence, box: box(x) });
const track = [
  { t: 0, detections: [detection(0)] },
  { t: 1, detections: [] },
  { t: 2, detections: [detection(0.2)] },
];


test('overlay boxes and label backgrounds use readable transparency', () => {
  assert.match(overlaySource, /const OVERLAY_BOX_ALPHA = 0\.78;/);
  assert.match(overlaySource, /rgba\(7, 11, 19, 0\.58\)/);
  assert.match(overlaySource, /ctx\.globalAlpha = OVERLAY_BOX_ALPHA;/);
});


test('motion and object detections use distinct overlay colors', () => {
  assert.equal(sandbox.overlayColorForDetection({ label: 'motion', motion_event: true }), '#49e6a3');
  assert.equal(sandbox.overlayColorForDetection({ label: 'person', confidence: 0.9 }), '#47d6ff');
});


test('object priority removes overlapping playback motion but keeps unrelated motion', () => {
  const person = { label: 'person', confidence: 0.9, box: box(0.2) };
  const overlappingMotion = { label: 'motion', motion_event: true, box: box(0.2) };
  const unrelatedMotion = { label: 'motion', motion_event: true, box: box(0.8) };
  assert.deepEqual(
    JSON.parse(JSON.stringify(sandbox.filterObjectPriorityDetections([person, overlappingMotion, unrelatedMotion]))),
    [person, unrelatedMotion],
  );
});


test('overlayMotionStateTag maps moving/still to their tag colors', () => {
  // Round-trip through JSON: values built inside the vm realm carry the sandbox
  // realm's prototypes, which deepStrictEqual rejects on reference equality.
  const plain = (value) => JSON.parse(JSON.stringify(value));
  assert.deepEqual(plain(sandbox.overlayMotionStateTag({ motion_state: 'moving' })), { text: 'Moving', color: '#49e6a3' });
  assert.deepEqual(plain(sandbox.overlayMotionStateTag({ motion_state: 'still' })), { text: 'Still', color: '#fbbf24' });
});


test('overlayMotionStateTag returns null without a classification', () => {
  assert.equal(sandbox.overlayMotionStateTag({ label: 'person' }), null);
  assert.equal(sandbox.overlayMotionStateTag({ motion_state: 'any' }), null);
  assert.equal(sandbox.overlayMotionStateTag(null), null);
});


test('overlayTrackIdTag renders the tracker identity for positive integer ids', () => {
  const plain = (value) => JSON.parse(JSON.stringify(value));
  assert.deepEqual(plain(sandbox.overlayTrackIdTag({ track_id: 12 })), { text: '#12', color: '#49e6a3' });
  assert.deepEqual(plain(sandbox.overlayTrackIdTag({ track_id: 1 })), { text: '#1', color: '#49e6a3' });
});


test('overlayTrackIdTag returns null without a usable id', () => {
  // Legacy events / pre-tracker samples carry no id; junk values must not
  // render either (0 and negatives are not valid tracker ids).
  assert.equal(sandbox.overlayTrackIdTag({ label: 'person' }), null);
  assert.equal(sandbox.overlayTrackIdTag({ track_id: 0 }), null);
  assert.equal(sandbox.overlayTrackIdTag({ track_id: -3 }), null);
  assert.equal(sandbox.overlayTrackIdTag({ track_id: 'junk' }), null);
  assert.equal(sandbox.overlayTrackIdTag(null), null);
});


test('sampleTrackAtTime interpolates through a short missed sample', () => {
  const sampled = sandbox.sampleTrackAtTime(track, 1.5);
  assert.equal(sampled.length, 1);
  assert.ok(sampled[0].box.x > 0 && sampled[0].box.x < 0.2);
});

test('matchDetection keeps same-class objects attached to their stable track id', () => {
  const first = { label: 'person', track_id: 4, box: box(0.10) };
  const second = { label: 'person', track_id: 9, box: box(0.70) };
  const target = { label: 'person', track_id: 9, box: box(0.72) };
  assert.equal(sandbox.matchDetection([first, second], target).track_id, 9);
});


test('sampleTrackAtTime stops drawing after the track hold window', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.sampleTrackAtTime(track, 5.1))), []);
});


test('normalizeDetectionBox maps pixel boxes into normalized coordinates', () => {
  assert.deepEqual(
    JSON.parse(JSON.stringify(sandbox.normalizeDetectionBox({ x: 320, y: 180, width: 160, height: 90 }, 1280, 720))),
    { x: 0.25, y: 0.25, width: 0.125, height: 0.125 },
  );
});
