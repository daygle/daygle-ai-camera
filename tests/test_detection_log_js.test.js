// Behavioural tests for the recording Detection Log (web/detection_log.js).
//
// Run with:
//   node --test tests/test_detection_log_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/detection_log.js'), 'utf8');

function load() {
  const context = vm.createContext({
    escapeHtml: (value) => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    titleCase: (value) => String(value).replace(/\b\w/g, (c) => c.toUpperCase()),
  });
  vm.runInContext(source, context);
  return context;
}

// Recording 23591, abridged: a person kept until the bush, then only drops.
const TRACK = [
  { t: 1.9, detections: [] },
  { t: 3.9, detections: [] },
  { t: 10.568, detections: [{ label: 'person', confidence: 0.945, box: { x: 0.16, y: 0.32, width: 0.07, height: 0.26 }, motion_state: 'moving', motion_basis: 'mask', track_id: 125, track_age: 2 }] },
  { t: 12.45, detections: [], dropped: [{ label: 'person', confidence: 0.55, box: { x: 0.8, y: 0.3, width: 0.05, height: 0.2 }, reason: 'Below the person rule minimum (0.65)' }] },
  { t: 12.92, detections: [] },
  { t: 13.46, detections: [] },
  { t: 14.41, detections: [] },
];

test('rows show each check that saw something, and collapse runs of empty checks', () => {
  const log = load();
  const rows = log.detectionLogRows(TRACK);
  assert.deepEqual([...rows.map((row) => row.kind)], ['gap', 'sample', 'sample', 'gap']);
  assert.equal(rows[0].count, 2);
  assert.equal(rows[0].from, 1.9);
  assert.equal(rows[0].to, 3.9);
  assert.equal(rows[3].count, 3);
  assert.equal(rows[2].dropped[0].reason, 'Below the person rule minimum (0.65)');
});

test('summary counts checks, kept and dropped detections', () => {
  const summary = load().detectionLogSummary(TRACK);
  assert.deepEqual({ ...summary }, { checks: 7, kept: 1, dropped: 1 });
});

test('kept detections say how they were classified', () => {
  const log = load();
  assert.equal(log.describeKeptDetection(TRACK[2].detections[0]), 'moving (mask) · #125 · seen 2x');
  assert.equal(
    log.describeKeptDetection({ label: 'car', motion_state: 'still', motion_basis: 'displacement', track_displacement: 0.0042 }),
    'still (displacement 0.004)',
  );
  assert.equal(log.describeKeptDetection({ label: 'person', track_id: 3 }), 'camera moving · #3');
});

test('html lists kept and dropped entries with seek times, escaping server text', () => {
  const log = load();
  const html = log.detectionLogHtml([
    { t: 2, detections: [], dropped: [{ label: '<b>cat</b>', confidence: 0.4, reason: 'Below <threshold>' }] },
  ]);
  assert.match(html, /data-seek="2"/);
  assert.match(html, /0:02\.0/);
  assert.match(html, /Dropped/);
  assert.ok(!html.includes('<b>cat</b>'));
  assert.ok(html.includes('Below &lt;threshold&gt;'));
  assert.match(log.detectionLogHtml([]), /No detection checks/);
});

test('dropped boxes show only around the check that dropped them', () => {
  const log = load();
  assert.equal(log.droppedDetectionsAtTime(TRACK, 12.0).length, 0);
  assert.equal(log.droppedDetectionsAtTime(TRACK, 12.45).length, 1);
  assert.equal(log.droppedDetectionsAtTime(TRACK, 12.8).length, 1);
  assert.equal(log.droppedDetectionsAtTime(TRACK, 13.0).length, 0); // next check (12.92) dropped nothing
  assert.equal(log.droppedDetectionsAtTime([], 1).length, 0);
});

test('log times read as minutes:seconds', () => {
  const log = load();
  assert.equal(log.formatLogTime(0), '0:00.0');
  assert.equal(log.formatLogTime(75.25), '1:15.3');
});

test('dropped entries lead with their tracker id so a parked car reads as itself', () => {
  const log = load();
  assert.equal(
    log.describeDroppedDetection({ label: 'car', track_id: 176, track_age: 886, reason: 'Judged still (displacement); car is set to Moving Only' }),
    '#176 · seen 886x · Judged still (displacement); car is set to Moving Only',
  );
  assert.equal(log.describeDroppedDetection({ label: 'car', reason: 'Below the detection minimum (0.32 < 0.50)' }), 'Below the detection minimum (0.32 < 0.50)');
  assert.match(log.detectionLogHtml([{ t: 1, detections: [], dropped: [{ label: 'car', confidence: 0.88, track_id: 176, track_age: 2, reason: 'x' }] }]), /#176 · seen 2x · x/);
});
