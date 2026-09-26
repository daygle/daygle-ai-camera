// Unit tests for normalizeTime in web/utils.js - the client mirror of
// app/zone_schema.py::normalize_zone_time used by the Zones save path. It must
// keep the rule's delivery settings (edited on the Alerts page) intact through
// a Zones save and match what the backend stores.
//
// Same vm-in-a-window-stub pattern as test_loiter_normalize_js.test.js.
//
// Run with:
//   node --test tests/test_time_normalize_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');

const sandbox = {
  window: { addEventListener() {}, removeEventListener() {} },
  BroadcastChannel: undefined,
};
sandbox.window.daygleUi = null;
vm.createContext(sandbox);
vm.runInContext(utilsSource, sandbox);

const ui = sandbox.window.daygleUi;
assert.ok(ui && typeof ui.normalizeTime === 'function',
  'utils.js should expose normalizeTime on window.daygleUi');
const { normalizeTime } = ui;
const rehome = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));

test('defaults are filled in for a bare rule', () => {
  const rule = normalizeTime({});
  assert.equal(rule.enabled, true);
  assert.equal(rule.name, 'Unusual time');
  assert.deepEqual(rehome(rule.labels), []);
  assert.equal(rule.threshold, 0.15);
  assert.equal(rule.cooldown_seconds, 1800);
  assert.equal(rule.record_on_detect, true);
  assert.equal(rule.email_enabled, false);
  assert.equal(rule.push_enabled, false);
  assert.deepEqual(rehome(rule.email_recipients), []);
  assert.equal(rule.notify_start, null);
});

test('threshold + cooldown clamp and coerce like the backend', () => {
  assert.equal(normalizeTime({ threshold: 5 }).threshold, 1);        // capped at 1
  assert.equal(normalizeTime({ threshold: -0.2 }).threshold, 0);     // floored at 0
  assert.equal(normalizeTime({ threshold: 0.0836 }).threshold, 0.0836);
  assert.equal(normalizeTime({ threshold: 'nan' }).threshold, 0.15); // NaN -> default
  assert.equal(normalizeTime({ cooldown_seconds: -5 }).cooldown_seconds, 0);
  assert.equal(normalizeTime({ cooldown_seconds: 'x' }).cooldown_seconds, 1800);
});

test('labels lower-case + de-dupe; recipients + notify preserved', () => {
  const rule = normalizeTime({
    labels: ['Person', 'person', 'car'],
    email_enabled: true, email_recipients: 'a@example.com, , b@example.com',
    notify_start: '9:00', notify_end: '22:15',
  });
  assert.deepEqual(rehome(rule.labels), ['person', 'car']);
  assert.equal(rule.email_enabled, true);
  assert.deepEqual(rehome(rule.email_recipients), ['a@example.com', 'b@example.com']);
  assert.equal(rule.notify_start, '09:00');
  assert.equal(rule.notify_end, '22:15');
});

test('a non-object drops to null; a disabled rule is kept', () => {
  assert.equal(normalizeTime(null), null);
  assert.equal(normalizeTime('yes'), null);
  assert.equal(normalizeTime({ enabled: false }).enabled, false);
});
