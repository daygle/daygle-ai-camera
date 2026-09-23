import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/alerts.js'), 'utf8');
const start = source.indexOf('function defaultAlertSchedule()');
const end = source.indexOf('function policyHasChannel(');
assert.ok(start >= 0 && end > start, 'alerts.js schedule helpers should be present');

const sandbox = {
  normalizeEmailList(value) {
    const values = Array.isArray(value) ? value : String(value || '').split(',');
    return [...new Set(values.map((item) => String(item).trim()).filter(Boolean))];
  },
};
vm.createContext(sandbox);
vm.runInContext(`${source.slice(start, end)}; globalThis.ensureAlertSchedules = ensureAlertSchedules;`, sandbox);
const ensureAlertSchedules = sandbox.ensureAlertSchedules;
const rehome = (value) => JSON.parse(JSON.stringify(value));

test('legacy singular alert fields migrate into one schedule without data loss', () => {
  const rule = {
    email_enabled: true,
    push_enabled: false,
    email_recipients: 'day@example.com, backup@example.com',
    active_start: '06:00',
    active_end: '18:00',
    notify_start: '07:00',
    notify_end: '17:00',
  };

  const [schedule] = ensureAlertSchedules(rule);
  assert.deepEqual(rehome(schedule), {
    id: 'schedule-1',
    email_enabled: true,
    push_enabled: false,
    email_recipients: ['day@example.com', 'backup@example.com'],
    active_start: '06:00',
    active_end: '18:00',
    notify_start: '07:00',
    notify_end: '17:00',
  });
  assert.deepEqual(rehome(ensureAlertSchedules(rule)[0]), rehome(schedule));
});

test('explicit schedules retain separate channels, windows, and recipients', () => {
  const rule = {
    email_enabled: false,
    push_enabled: false,
    alert_schedules: [
      { id: 'day', email_enabled: true, email_recipients: ['day@example.com'], active_start: '06:00', active_end: '18:00' },
      { id: 'night', push_enabled: true, email_recipients: ['night@example.com'], active_start: '18:00', active_end: '06:00' },
    ],
  };

  const schedules = ensureAlertSchedules(rule);
  assert.equal(schedules.length, 2);
  assert.deepEqual(rehome(schedules.map(({ id, email_enabled, push_enabled, email_recipients, active_start, active_end }) => ({
    id, email_enabled, push_enabled, email_recipients, active_start, active_end,
  }))), [
    { id: 'day', email_enabled: true, push_enabled: false, email_recipients: ['day@example.com'], active_start: '06:00', active_end: '18:00' },
    { id: 'night', email_enabled: false, push_enabled: true, email_recipients: ['night@example.com'], active_start: '18:00', active_end: '06:00' },
  ]);
  assert.equal(rule.email_enabled, true);
  assert.equal(rule.push_enabled, true);
  assert.deepEqual(rehome(rule.email_recipients), ['day@example.com', 'night@example.com']);
});
