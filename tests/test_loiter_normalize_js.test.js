// Unit tests for normalizeLoiter in web/utils.js — the client mirror of
// app/zone_schema.py::normalize_zone_loiter used by the Zones save path. It
// must keep the loiter rule's delivery settings (edited on the Alerts page)
// intact through a Zones save and match what the backend stores.
//
// utils.js is a classic browser script that reaches for `window` at load, so we
// load it into a vm context behind a lightweight window stub — same pattern as
// test_tripwire_geometry_js.test.js.
//
// Run with:
//   node --test tests/test_loiter_normalize_js.test.js

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
assert.ok(ui && typeof ui.normalizeLoiter === 'function',
  'utils.js should expose normalizeLoiter on window.daygleUi');
const { normalizeLoiter } = ui;
const rehome = (value) => (value == null ? value : JSON.parse(JSON.stringify(value)));

test('defaults are filled in for a bare loiter rule', () => {
  const rule = normalizeLoiter({});
  assert.equal(rule.enabled, true);
  assert.equal(rule.name, 'Loitering');
  assert.deepEqual(rehome(rule.labels), []);
  assert.equal(rule.min_dwell_seconds, 30);
  assert.equal(rule.sensitivity, 3);
  assert.equal(rule.cooldown_seconds, 120);
  assert.equal(rule.record_on_detect, true);
  assert.equal(rule.email_enabled, false);
  assert.equal(rule.push_enabled, false);
  assert.deepEqual(rehome(rule.email_recipients), []);
  assert.equal(rule.notify_start, null);
  assert.equal(rule.notify_end, null);
});

test('numeric fields clamp and coerce like the backend', () => {
  assert.equal(normalizeLoiter({ min_dwell_seconds: 0 }).min_dwell_seconds, 1);      // floored to >= 1
  assert.equal(normalizeLoiter({ min_dwell_seconds: 'x' }).min_dwell_seconds, 30);   // bad -> default
  assert.equal(normalizeLoiter({ sensitivity: 50 }).sensitivity, 10);               // capped at 10
  assert.equal(normalizeLoiter({ sensitivity: -2 }).sensitivity, 0);                // floored at 0
  assert.equal(normalizeLoiter({ sensitivity: 'nan' }).sensitivity, 3);             // NaN -> default
  assert.equal(normalizeLoiter({ cooldown_seconds: -5 }).cooldown_seconds, 0);      // floored at 0
});

test('labels lower-case + de-dupe; recipients + notify preserved', () => {
  const rule = normalizeLoiter({
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

test('a non-object drops to null (field removed)', () => {
  assert.equal(normalizeLoiter(null), null);
  assert.equal(normalizeLoiter('yes'), null);
  assert.equal(normalizeLoiter(undefined), null);
});

test('a disabled rule is kept (enabled:false), not dropped', () => {
  const rule = normalizeLoiter({ enabled: false });
  assert.ok(rule);
  assert.equal(rule.enabled, false);
});
