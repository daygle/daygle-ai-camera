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

const statusStart = source.indexOf('function isWithinDetectWindow(');
const statusEnd = source.indexOf('function defaultAlertSchedule()', statusStart);
assert.ok(statusStart >= 0 && statusEnd > statusStart, 'alerts.js status helpers should be present');
const statusSandbox = {
  window: { daygleAuth: { user: { timezone: 'America/Los_Angeles' } } },
  ensureAlertSchedules(rule) {
    return Array.isArray(rule.alert_schedules) && rule.alert_schedules.length ? rule.alert_schedules : [rule];
  },
};
vm.createContext(statusSandbox);
vm.runInContext(`${source.slice(statusStart, statusEnd)}; globalThis.isWithinDetectWindow = isWithinDetectWindow; globalThis.clockInTimezone = clockInTimezone; globalThis.policyStatus = policyStatus;`, statusSandbox);

test('detect window status matches admin timezone, including boundaries and overnight schedules', () => {
  const now = new Date('2026-06-01T15:00:00Z'); // 08:00 in Los Angeles
  assert.equal(statusSandbox.clockInTimezone(now, 'America/Los_Angeles'), '08:00');
  assert.equal(statusSandbox.isWithinDetectWindow('07:30', '08:00', '08:00'), true);
  assert.equal(statusSandbox.isWithinDetectWindow('22:00', '06:00', '04:30'), true);
  assert.equal(statusSandbox.isWithinDetectWindow('22:00', '06:00', '12:00'), false);
  assert.equal(statusSandbox.isWithinDetectWindow('09:00', null, '12:00'), true);
  assert.equal(statusSandbox.isWithinDetectWindow('08:00', '08:00', '12:00'), true);
});

test('policy status distinguishes disabled, active, and outside-window policies', () => {
  const now = new Date('2026-06-01T15:00:00Z'); // 08:00 in Los Angeles
  assert.equal(statusSandbox.policyStatus({ enabled: false }, 'object', now).className, 'is-disabled');
  assert.equal(statusSandbox.policyStatus({ enabled: true, alert_schedules: [
    { active_start: '07:00', active_end: '08:00' },
    { active_start: '20:00', active_end: '22:00' },
  ] }, 'object', now).className, 'is-in-schedule');
  assert.equal(statusSandbox.policyStatus({ enabled: true, active_start: '09:00', active_end: '17:00' }, 'sound', now).className, 'is-out-of-schedule');
  assert.equal(statusSandbox.policyStatus({ enabled: true, active_start: '08:00', active_end: '08:00' }, 'sound', now).className, 'is-in-schedule');
});
