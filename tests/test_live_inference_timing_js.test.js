// The central inference scheduler records queue wait separately from cycle
// run time (app/inference_scheduler.py) and publishes both on the live
// detection status payload as inference_wait_ms / inference_ms. The Live page
// surfaces them in the stream details card so an oversubscribed detector (long
// wait) is distinguishable from a slow model (long run).
//
// live.js runs DOM-coupled code at import, so these are source assertions, the
// same style as tests/test_live_hidden_tab_poll.test.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const liveSource = readFileSync(path.resolve(here, '../web/live.js'), 'utf8');
const liveHtml = readFileSync(path.resolve(here, '../web/live.html'), 'utf8');

test('the stream details card has a slot for the detection queue timing', () => {
  assert.match(liveHtml, /id="streamDetailQueue"/);
  assert.match(liveSource, /streamDetailQueue: document\.getElementById\('streamDetailQueue'\)/);
});

test('renderInferenceTiming reads both numbers off the status payload', () => {
  const start = liveSource.indexOf('function renderInferenceTiming(');
  assert.ok(start !== -1, 'renderInferenceTiming should exist');
  const body = liveSource.slice(start, liveSource.indexOf('function formatMotionPixelPercent', start));
  // Wait and run must be read as two separate fields: averaging them into one
  // number is exactly the diagnostic this feature exists to avoid.
  assert.match(body, /inference_wait_ms/);
  assert.match(body, /inference_ms/);
  assert.match(body, /wait \$\{format\(wait\)\} \/ run \$\{format\(run\)\}/);
});

test('the status refresh updates the timing readout', () => {
  const start = liveSource.indexOf('ingestServerTrackDetections(payload);');
  assert.ok(start !== -1, 'the detection status refresh should be found');
  const body = liveSource.slice(start, start + 200);
  assert.match(body, /renderInferenceTiming\(payload\);/);
});
