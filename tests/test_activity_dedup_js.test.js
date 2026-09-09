// Regression tests for the dashboard activity-feed "hide duplicate motion row"
// rule (web/utils.js: markSupersededMotionRows).
//
// A single continuous clip can emit both a typed (object/sound) event and a
// bare motion-only event on the same recording. The dashboard keeps them as
// separate rows so each stays filterable under its Type tab, but the combined
// "All" view must not show one clip as two near-identical rows. This suite
// pins that rule by loading utils.js into a sandboxed vm context (the same
// pattern as test_motion_boundary_js.test.js) and exercising the helper.
//
// Run with:
//   node --test tests/test_activity_dedup_js.test.js
// Or, from the repo root:
//   npm test          (runs all *.test.js suites; see package.json)

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');

const sandbox = {
  window: {
    addEventListener() {},
    removeEventListener() {},
  },
  BroadcastChannel: undefined,
};
sandbox.window.daygleUi = null;
vm.createContext(sandbox);
vm.runInContext(utilsSource, sandbox);

const ui = sandbox.window.daygleUi;
const { markSupersededMotionRows } = ui;

test('markSupersededMotionRows is exported', () => {
  assert.equal(typeof markSupersededMotionRows, 'function');
});

test('flags a motion-only row whose recording also has an object row', () => {
  const objectRow = { recordingId: 4869 };
  const motionRow = { recordingId: 4869, isMotionOnly: true };
  markSupersededMotionRows([objectRow, motionRow]);
  assert.equal(motionRow.supersededByTypedRow, true);
  // The typed row itself is never flagged.
  assert.equal(objectRow.supersededByTypedRow, undefined);
});

test('flags a motion-only row superseded by a sound row', () => {
  const soundRow = { recordingId: 12, isSound: true };
  const motionRow = { recordingId: 12, isMotionOnly: true };
  markSupersededMotionRows([soundRow, motionRow]);
  assert.equal(motionRow.supersededByTypedRow, true);
});

test('leaves a standalone motion-only recording untouched', () => {
  const motionRow = { recordingId: 77, isMotionOnly: true };
  markSupersededMotionRows([motionRow]);
  assert.equal(motionRow.supersededByTypedRow, undefined);
});

test('does not cross recordings: a typed row on a different clip does not flag', () => {
  const objectRow = { recordingId: 1 };
  const motionRow = { recordingId: 2, isMotionOnly: true };
  markSupersededMotionRows([objectRow, motionRow]);
  assert.equal(motionRow.supersededByTypedRow, undefined);
});

test('ignores rows without a recordingId (distinct system events)', () => {
  const objectRow = { recordingId: 5 };
  const motionRow = { recordingId: null, isMotionOnly: true };
  markSupersededMotionRows([objectRow, motionRow]);
  assert.equal(motionRow.supersededByTypedRow, undefined);
});

test('returns the same array reference and tolerates non-arrays', () => {
  const items = [];
  assert.equal(markSupersededMotionRows(items), items);
  assert.equal(markSupersededMotionRows(null), null);
  assert.equal(markSupersededMotionRows(undefined), undefined);
});
