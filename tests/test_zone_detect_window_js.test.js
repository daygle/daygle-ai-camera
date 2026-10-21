// Zone rule detect window on the Zones page (web/zones.js).
//
// The detect window (Detect From / Detect Until) belongs to the rule, not to
// an alert schedule: it gates recording and alerts. Normalizing a rule's alert
// schedules must never overwrite it from the first schedule.
//
// Run with:
//   node --test tests/test_zone_detect_window_js.test.js

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/zones.js'), 'utf8');

function slice(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  const end = source.indexOf(endMarker, start);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  return source.slice(start, end);
}

const sandbox = {
  normalizeEmailList: (value) => (Array.isArray(value) ? value : String(value || '').split(',').map((item) => item.trim()).filter(Boolean)),
  escapeHtml: (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`),
};
vm.createContext(sandbox);
vm.runInContext(`${slice('function normalizeAlertSchedules(', '// Optional object size / shape limits')}
${slice('function detectWindowSelect(', 'function ruleToggleCell(')}
globalThis.normalizeAlertSchedules = normalizeAlertSchedules;
globalThis.detectWindowFields = detectWindowFields;`, sandbox);

test('alert schedules do not carry or overwrite the rule detect window', () => {
  const rule = {
    active_start: '05:00', active_end: '20:00',
    alert_schedules: [{ id: 'a', push_enabled: true, active_start: '09:00', active_end: '10:00', notify_start: '06:00', notify_end: '19:00' }],
  };
  const [schedule] = sandbox.normalizeAlertSchedules(rule);
  assert.equal(rule.active_start, '05:00');
  assert.equal(rule.active_end, '20:00');
  assert.equal(rule.notify_start, '06:00');
  assert.equal(schedule.notify_end, '19:00');
});

test('detect window fields select the rule window, defaulting to any time', () => {
  const html = sandbox.detectWindowFields({ active_start: '05:00', active_end: null }, '0:1', 'cat');
  assert.match(html, /Detect From/);
  assert.match(html, /Detect Until/);
  assert.match(html, /data-zone-detect-window="0:1" data-window-field="active_start"/);
  assert.match(html, /<option value="05:00" selected>05:00<\/option>/);
  const untilSelect = html.slice(html.indexOf('data-window-field="active_end"'));
  assert.doesNotMatch(untilSelect, / selected>/);
});
