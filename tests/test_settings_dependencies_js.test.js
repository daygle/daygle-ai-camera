// Settings that another setting makes irrelevant are dimmed, not hidden, and
// legacy / duplicate choices are not offered.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (file) => readFileSync(path.resolve(here, '../web', file), 'utf8');

test('settings page dims Periodic Scan and the confirm fields when they cannot apply', () => {
  const source = read('settings.js');
  assert.match(source, /function syncLiveFieldDependencies\(\)/);
  assert.match(source, /setFieldInactive\(form\.elements\.periodic_scan_interval_seconds, alwaysRun/);
  assert.match(source, /\['detection_confirm_window', 'detection_confirm_iou'\]/);
  // Re-run after every programmatic fill, not only on user edits.
  assert.equal((source.match(/syncLiveFieldDependencies\(\);/g) || []).length, 2);
  // The legacy model is only listed while it is the saved value.
  assert.match(source, /legacy\.hidden = algorithm\.value !== 'diff'/);
});

test('camera profiles dim the same fields from their own or the global value', () => {
  const source = read('cameras.js');
  assert.match(source, /function bindProfileFieldDependencies\(form\)/);
  assert.match(source, /globalLiveSettings = settings\.live \|\| \{\}/);
  assert.match(source, /form\.__syncProfileDependencies\(\)/);  // after a preset is applied
});

test('inactive fields stay enabled so their values are still saved', () => {
  const source = read('utils.js');
  const helper = source.slice(source.indexOf('function setFieldInactive'), source.indexOf('function titleCase'));
  assert.doesNotMatch(helper, /\.disabled\s*=/);
  assert.match(helper, /classList\.toggle\('field-inactive'/);
});

test('face detection mode has a single Moving & Still choice', () => {
  const html = read('face-recognition.html');
  assert.doesNotMatch(html, /<option value="any">/);
  assert.match(html, /<option value="inherit">Moving &amp; Still \(Default\)<\/option>/);
  assert.match(read('face-recognition.js'), /override && override !== 'any' \? override : 'inherit'/);
});

test('PTZ Motion Detection lives on the PTZ tab with an explanation', () => {
  const source = read('cameras.js');
  const ptzPanel = source.slice(source.indexOf('data-panel="ptz"'), source.indexOf('ptzAutoTrackSectionHtml(camera, index) +'));
  assert.match(ptzPanel, /name="ptz_motion_detection"/);
  assert.match(ptzPanel, /PTZ Motion Detection <span class="info-tip"/);
});
