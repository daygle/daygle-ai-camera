// Unit tests for normalizeActivity in web/utils.js — the client mirror of
// app/zone_schema.py::normalize_zone_activity used by the Zones save path.
//
// Same vm-in-a-window-stub pattern as the other normalize tests.
//
// Run with:
//   node --test tests/test_activity_normalize_js.test.js

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
assert.ok(ui && typeof ui.normalizeActivity === 'function',
  'utils.js should expose normalizeActivity on window.daygleUi');
const { normalizeActivity } = ui;
const rehome = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));

test('defaults are filled in for a bare rule', () => {
  const rule = normalizeActivity({});
  assert.equal(rule.enabled, true);
  assert.equal(rule.name, 'Activity Spike');
  assert.deepEqual(rehome(rule.labels), []);
  assert.equal(rule.min_count, 5);
  assert.equal(rule.sensitivity, 3);
  assert.equal(rule.cooldown_seconds, 900);
  assert.equal(rule.record_on_detect, true);
  assert.equal(rule.email_enabled, false);
  assert.equal(rule.notify_start, null);
});

test('numeric fields clamp and coerce like the backend', () => {
  assert.equal(normalizeActivity({ min_count: 0 }).min_count, 1);
  assert.equal(normalizeActivity({ min_count: 'x' }).min_count, 5);
  assert.equal(normalizeActivity({ sensitivity: 50 }).sensitivity, 10);
  assert.equal(normalizeActivity({ sensitivity: -2 }).sensitivity, 0);
  assert.equal(normalizeActivity({ sensitivity: 'nan' }).sensitivity, 3);
  assert.equal(normalizeActivity({ cooldown_seconds: -5 }).cooldown_seconds, 0);
  assert.equal(normalizeActivity({ cooldown_seconds: 'y' }).cooldown_seconds, 900);
});

test('labels lower-case + de-dupe; recipients + notify preserved', () => {
  const rule = normalizeActivity({
    labels: ['Car', 'car', 'Person'],
    email_enabled: true, email_recipients: 'a@example.com, , b@example.com',
    notify_start: '9:00', notify_end: '22:15',
  });
  assert.deepEqual(rehome(rule.labels), ['car', 'person']);
  assert.deepEqual(rehome(rule.email_recipients), ['a@example.com', 'b@example.com']);
  assert.equal(rule.notify_start, '09:00');
  assert.equal(rule.notify_end, '22:15');
});

test('a non-object drops to null; a disabled rule is kept', () => {
  assert.equal(normalizeActivity(null), null);
  assert.equal(normalizeActivity('yes'), null);
  assert.equal(normalizeActivity({ enabled: false }).enabled, false);
});
