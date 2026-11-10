// Regression tests for the Detection > Sounds tab (/detection/sounds).
//
// The status table and the per-camera editor share ONE health verdict.
// "running" alone only means the listener thread is alive: with the TFLite
// runtime missing the thread runs but nothing is classified, which the page
// used to show as a green "Detector Running" box.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/sounds.html'), 'utf8');
const source = readFileSync(path.resolve(here, '../web/sounds.js'), 'utf8');

// Run detectorHealth against sample cameras/statuses.
function loadHealth() {
  const start = source.indexOf('function detectorSoundConfig(camera)');
  const end = source.indexOf('function detectorHealthPill');
  const titleCase = (value) => String(value).replace(/\b\w/g, (c) => c.toUpperCase());
  return new Function('titleCase', `${source.slice(start, end)}; return detectorHealth;`)(titleCase);
}

const listening = {
  stream_url: 'rtsp://cam/stream',
  detection: { sound: { enabled: true, rules: [{ class: 'dog_bark', enabled: true }] } },
};

test('a running thread with an unavailable backend is an error, not green', () => {
  const health = loadHealth()(listening, { running: true, backend: 'unavailable', backend_reason: 'TensorFlow Lite runtime is not installed.' });
  assert.equal(health.tone, 'error');
  assert.equal(health.label, 'Unavailable');
  assert.match(health.detail, /not installed/);
});

test('green only when running with a usable backend', () => {
  const detectorHealth = loadHealth();
  assert.equal(detectorHealth(listening, { running: true, backend: 'yamnet_tflite' }).tone, 'ok');
  assert.equal(detectorHealth(listening, { running: false, backend: 'loading' }).tone, 'warn');
  assert.equal(detectorHealth({ ...listening, detection: { sound: { enabled: false } } }, { running: false }).tone, 'off');
  assert.equal(detectorHealth({ ...listening, detection: { sound: { enabled: true, rules: [] } } }, {}).label, 'No sound classes');
  assert.equal(detectorHealth({ detection: listening.detection }, {}).label, 'No RTSP stream');
});

test('the editor banner and the table row use the same verdict', () => {
  const banner = source.slice(source.indexOf('function renderStatus()'), source.indexOf('function renderEditor()'));
  assert.match(banner, /detectorHealth\(camera, selectedStatus\)/);
  assert.doesNotMatch(banner, /selectedStatus\.running \? 'status-ok'/);
  const table = source.slice(source.indexOf('function renderDetectorStatuses'), source.indexOf('async function refreshDetectorStatuses'));
  assert.match(table, /detectorHealth\(camera, status\)/);
});

test('rows pick the camera; the camera dropdown is gone', () => {
  assert.doesNotMatch(html, /id="soundCameraSelect"/);
  assert.match(source, /closest\('tr\[data-camera-row\]'\)/);
  assert.match(source, /Discard unsaved sound settings for this camera\?/);
  // Refresh belongs to the table it refreshes.
  const tableCard = html.slice(html.indexOf('sound-detectors-card'), html.indexOf('id="soundEditorCard"'));
  assert.match(tableCard, /id="reloadSoundsBtn"/);
});

test('the table has five readable columns', () => {
  assert.match(source, /<th>Camera<\/th><th>Sound<\/th><th>Listening for<\/th><th>Status<\/th><th>Last heard<\/th>/);
});
