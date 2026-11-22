// Unit tests for normalizeCoPresence in web/utils.js - the client mirror of
// app/zone_schema.py::normalize_zone_co_presence used by the Zones save path.
// It must keep the co-presence rule's delivery settings (edited on the Alerts
// page) intact through a Zones save and match what the backend stores.
//
// utils.js is a classic browser script that reaches for `window` at load, so we
// load it into a vm context behind a lightweight window stub - same pattern as
// test_loiter_normalize_js.test.js.
//
// Run with:
//   node --test tests/test_co_presence_normalize_js.test.js

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
assert.ok(ui && typeof ui.normalizeCoPresence === 'function',
  'utils.js should expose normalizeCoPresence on window.daygleUi');
const { normalizeCoPresence } = ui;
const rehome = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));

test('defaults are filled in for a bare co-presence rule', () => {
  const rule = normalizeCoPresence({});
  assert.equal(rule.enabled, true);
  assert.equal(rule.name, 'Co-presence');
  assert.deepEqual(rehome(rule.all_of), []);
  assert.deepEqual(rehome(rule.any_of), []);
  assert.equal(rule.window_seconds, 15);
  assert.equal(rule.cooldown_seconds, 60);
  assert.equal(rule.record_on_detect, true);
  assert.equal(rule.email_enabled, false);
  assert.equal(rule.push_enabled, false);
  assert.deepEqual(rehome(rule.email_recipients), []);
  assert.deepEqual(rehome(rule.push_recipients), []);
  assert.equal(rule.notify_start, null);
  assert.equal(rule.notify_end, null);
});

test('absent rule is dropped', () => {
  assert.equal(normalizeCoPresence(null), null);
  assert.equal(normalizeCoPresence(undefined), null);
  assert.equal(normalizeCoPresence('nope'), null);
});

test('labels are lowercased, deduped, and never appear in both lists', () => {
  const rule = normalizeCoPresence({
    all_of: ['Cat', ' person ', 'cat'],
    any_of: ['person', 'Dog', 'dog', 'motion', 'face'],
  });
  assert.deepEqual(rehome(rule.all_of), ['cat', 'person']);
  assert.deepEqual(rehome(rule.any_of), ['dog']);
});

test('the recent-sighting window and cooldown are clamped', () => {
  assert.equal(normalizeCoPresence({ window_seconds: 0 }).window_seconds, 1);
  assert.equal(normalizeCoPresence({ window_seconds: 9999 }).window_seconds, 300);
  assert.equal(normalizeCoPresence({ window_seconds: 'x' }).window_seconds, 15);
  assert.equal(normalizeCoPresence({ cooldown_seconds: -5 }).cooldown_seconds, 0);
});

test('delivery settings made on the Alerts page survive a Zones save', () => {
  const rule = normalizeCoPresence({
    enabled: false,
    name: 'Cat and visitor',
    window_seconds: 30,
    cooldown_seconds: 120,
    record_on_detect: false,
    email_enabled: true,
    email_recipients: ['user:1', 'user:2'],
    push_enabled: true,
    push_recipients: ['user:2'],
    notify_start: '21:00',
    notify_end: '06:00',
  });
  assert.equal(rule.enabled, false);
  assert.equal(rule.name, 'Cat and visitor');
  assert.equal(rule.window_seconds, 30);
  assert.equal(rule.cooldown_seconds, 120);
  assert.equal(rule.record_on_detect, false);
  assert.equal(rule.email_enabled, true);
  assert.deepEqual(rehome(rule.email_recipients), ['user:1', 'user:2']);
  assert.equal(rule.push_enabled, true);
  assert.deepEqual(rehome(rule.push_recipients), ['user:2']);
  assert.equal(rule.notify_start, '21:00');
  assert.equal(rule.notify_end, '06:00');
});
