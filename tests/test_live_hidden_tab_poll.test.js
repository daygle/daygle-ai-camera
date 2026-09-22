// Regression tests: the Live page must not keep polling while its tab is
// hidden, and must resume immediately when the tab is refocused.
//
// live.js runs DOM-coupled code at import (element lookups, listeners, init()),
// so it is not loaded into a vm sandbox here. These assertions pin the guard in
// the source instead, matching the source-assertion style used by
// tests/test_playback_overlay_logic.test.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const liveSource = readFileSync(path.resolve(here, '../web/live.js'), 'utf8');

test('refreshDetectionStatus bails out while the tab is hidden', () => {
  // Isolate the function body so the guard cannot be satisfied by an unrelated
  // document.hidden check elsewhere in the file (e.g. refreshFrame).
  const start = liveSource.indexOf('async function refreshDetectionStatus()');
  assert.ok(start !== -1, 'refreshDetectionStatus should exist');
  // Window spans the guard block (an explanatory comment precedes the return)
  // but stops before isAllCameraMode(), so an unrelated hidden check elsewhere
  // cannot satisfy it.
  const body = liveSource.slice(start, liveSource.indexOf('if (isAllCameraMode())', start));
  assert.match(body, /if \(document\.hidden\) return;/);
});

test('refreshFrame bails out while the tab is hidden (unchanged parity)', () => {
  const start = liveSource.indexOf('function refreshFrame()');
  assert.ok(start !== -1, 'refreshFrame should exist');
  const body = liveSource.slice(start, start + 200);
  assert.match(body, /if \(!selectedCamera \|\| document\.hidden\) return;/);
});

test('a visibilitychange listener resumes both polls on refocus', () => {
  const start = liveSource.indexOf("addEventListener('visibilitychange'");
  assert.ok(start !== -1, 'a visibilitychange listener should be registered');
  const body = liveSource.slice(start, start + 200);
  // Guards against re-running while still hidden, then refreshes frame + status.
  assert.match(body, /if \(document\.hidden\) return;/);
  assert.match(body, /refreshFrame\(\);/);
  assert.match(body, /refreshDetectionStatus\(\);/);
});
