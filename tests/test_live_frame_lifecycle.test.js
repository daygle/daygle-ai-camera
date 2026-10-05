// Regression tests for the Live page's frame-blob lifecycle and the stale-
// response guards on the camera-scoped polls.
//
// live.js runs DOM-coupled code at import, so (like
// tests/test_live_hidden_tab_poll.test.js) these assertions pin the fixes in
// the source rather than loading the page into a sandbox.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');
const liveSource = read('live.js');

function listenerBody(source, eventName) {
  const start = source.indexOf(`liveEls.frame.addEventListener('${eventName}'`);
  assert.ok(start !== -1, `a frame ${eventName} listener should exist`);
  return source.slice(start, source.indexOf('\n});', start));
}

test('the previous frame blob is released on load AND on decode error', () => {
  // A frame that fails to decode never fires 'load'; releasing only there
  // leaked one JPEG blob per failed refresh.
  assert.match(listenerBody(liveSource, 'load'), /revokePreviousLiveFrameUrl\(\);/);
  assert.match(listenerBody(liveSource, 'error'), /revokePreviousLiveFrameUrl\(\);/);
});

test('refreshFrame drops a frame fetched for a camera that is no longer selected', () => {
  const start = liveSource.indexOf('async function refreshFrame()');
  const body = liveSource.slice(start, liveSource.indexOf('\n}\n', start));
  assert.match(body, /const requestedCamera = selectedCamera;/);
  // The guard runs before a new object URL is minted, so nothing leaks either.
  const guard = body.indexOf('selectedCamera?.id !== requestedCamera?.id');
  assert.ok(guard !== -1 && guard < body.indexOf('URL.createObjectURL'));
});

test('refreshDetectionStatus ignores a response for a previously selected camera', () => {
  const start = liveSource.indexOf('async function refreshDetectionStatus()');
  const body = liveSource.slice(start, liveSource.indexOf('\n}\n', start));
  assert.match(body, /const requestedCamera = selectedCamera;/);
  const guard = body.indexOf('selectedCamera?.id !== requestedCamera?.id');
  assert.ok(guard !== -1 && guard < body.indexOf('ingestServerTrackDetections(payload)'));
  // Runtime FPS is stored under the camera the request was made for.
  assert.match(body, /cameraRuntimeFps\[requestedCamera\.id\] = streamStatus\.fps;/);
});

test('timeline and snapshots loads discard responses superseded by a newer load', () => {
  const timeline = read('timeline.js');
  const tStart = timeline.indexOf('async function loadTimeline(');
  const tBody = timeline.slice(tStart, timeline.indexOf('\n}\n', tStart));
  assert.match(tBody, /const session = timelineLoadSession;/);
  assert.ok(tBody.indexOf('if (session !== timelineLoadSession) return;') < tBody.indexOf('state.payload = payload;'));

  const snapshots = read('snapshots.js');
  const sStart = snapshots.indexOf('async function loadSnapshots(');
  const sBody = snapshots.slice(sStart, snapshots.indexOf('\n}\n', sStart));
  assert.match(sBody, /const session = snapshotsLoadSession;/);
  assert.ok(sBody.indexOf('if (session !== snapshotsLoadSession) return;') < sBody.indexOf('allSnapshots = page.items;'));
});
