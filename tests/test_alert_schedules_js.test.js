import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/alerts.js'), 'utf8');
const start = source.indexOf('function defaultAlertSchedule()');
const end = source.indexOf('function defaultRule(');
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

test('a rule without schedules gets one empty schedule; rule-level fields are ignored', () => {
  const rule = { email_enabled: true, email_recipients: 'day@example.com', active_start: '06:00' };

  const [schedule] = ensureAlertSchedules(rule);
  assert.match(schedule.id, /^schedule-/);
  assert.equal(schedule.email_enabled, false);
  assert.deepEqual(rehome(schedule.email_recipients), []);
  assert.equal('active_start' in schedule, false, 'the detect window is the rule\'s own');
  assert.equal(rule.active_start, '06:00');
  assert.equal(rule.email_enabled, false, 'summary is derived from the schedules');
  assert.equal(ensureAlertSchedules(rule).length, 1);
});

test('explicit schedules retain separate channels, windows, and recipients', () => {
  const rule = {
    email_enabled: false,
    push_enabled: false,
    alert_schedules: [
      { id: 'day', email_enabled: true, email_recipients: ['day@example.com'], notify_start: '06:00', notify_end: '18:00' },
      { id: 'night', push_enabled: true, email_recipients: ['night@example.com'], notify_start: '18:00', notify_end: '06:00' },
    ],
  };

  const schedules = ensureAlertSchedules(rule);
  assert.equal(schedules.length, 2);
  assert.deepEqual(rehome(schedules.map(({ id, email_enabled, push_enabled, email_recipients, notify_start, notify_end }) => ({
    id, email_enabled, push_enabled, email_recipients, notify_start, notify_end,
  }))), [
    { id: 'day', email_enabled: true, push_enabled: false, email_recipients: ['day@example.com'], notify_start: '06:00', notify_end: '18:00' },
    { id: 'night', email_enabled: false, push_enabled: true, email_recipients: ['night@example.com'], notify_start: '18:00', notify_end: '06:00' },
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
  assert.equal(statusSandbox.policyStatus({ enabled: true, active_start: '07:00', active_end: '08:00', alert_schedules: [{}] }, 'object', now).className, 'is-in-schedule');
  assert.equal(statusSandbox.policyStatus({ enabled: true, active_start: '20:00', active_end: '22:00', alert_schedules: [{}] }, 'object', now).className, 'is-out-of-schedule');
  assert.equal(statusSandbox.policyStatus({ enabled: true, active_start: '09:00', active_end: '17:00' }, 'sound', now).className, 'is-out-of-schedule');
  assert.equal(statusSandbox.policyStatus({ enabled: true, active_start: '08:00', active_end: '08:00' }, 'sound', now).className, 'is-in-schedule');
});

test('behaviour policy status reflects its notify window instead of always showing Enabled', () => {
  const now = new Date('2026-06-01T15:00:00Z'); // 08:00 in Los Angeles
  const status = (rule, type = 'co_presence') => statusSandbox.policyStatus({ enabled: true, ...rule }, type, now);
  // No channel on: detection stays enabled, but there is no alert schedule to show.
  assert.equal(status({}).className, 'is-enabled');
  // A channel on and the notify window covers now.
  assert.equal(status({ email_enabled: true, notify_start: '07:00', notify_end: '09:00' }).className, 'is-in-schedule-alert');
  assert.equal(status({ push_enabled: true, notify_start: '08:00', notify_end: '08:00' }).label, 'In Schedule (Alert)');
  // A channel on but the notify window excludes now (including overnight).
  assert.equal(status({ email_enabled: true, notify_start: '20:00', notify_end: '05:00' }).label, 'Outside Schedule');
  assert.equal(status({ push_enabled: true, notify_start: '09:00', notify_end: '17:00' }).label, 'Outside Schedule');
  // Every behaviour policy shares the window gate.
  ['tripwire', 'loiter', 'time', 'activity', 'ai_tags'].forEach((type) => {
    assert.equal(status({ email_enabled: true, notify_start: '20:00', notify_end: '05:00' }, type).label, 'Outside Schedule');
  });
  // People rules have no notify window: keep the plain enabled state.
  assert.equal(status({ email_enabled: true, notify_start: '20:00', notify_end: '05:00' }, 'people').className, 'is-enabled');
});

const labelStart = source.indexOf('function ruleLabel(');
const labelEnd = source.indexOf('function ruleOptions(', labelStart);
assert.ok(labelStart >= 0 && labelEnd > labelStart, 'alerts.js ruleLabel should be present');
const labelSandbox = {
  alertType: 'co_presence',
  currentZone: () => ({ name: 'Yard' }),
  titleCase: (value) => String(value),
  soundClasses: [],
};
vm.createContext(labelSandbox);
vm.runInContext(`${source.slice(labelStart, labelEnd)}; globalThis.ruleLabel = ruleLabel;`, labelSandbox);

test('co-presence policy name matches the alert-history rule name', () => {
  assert.equal(
    labelSandbox.ruleLabel({ name: 'Co-presence', all_of: ['cat', 'person'], any_of: [] }),
    'Yard · Co-presence',
  );
  assert.equal(labelSandbox.ruleLabel({ name: 'Garden watch' }), 'Yard · Garden watch');
  // Same fallbacks as app/behaviour_monitor.py: the zone id when unnamed, and
  // the default rule name when blank.
  labelSandbox.currentZone = () => ({ name: '', id: 'z1' });
  assert.equal(labelSandbox.ruleLabel({ name: '  ' }), 'z1 · Co-presence');
});

test('policy status shows In Schedule (Alert) only when a detecting rule will send alerts now', () => {
  const now = new Date('2026-06-01T15:00:00Z'); // 08:00 in Los Angeles
  const status = (schedules, window = {}) => statusSandbox.policyStatus({ enabled: true, ...window, alert_schedules: schedules }, 'object', now);
  // Detecting, channel on, alert window covers now.
  assert.equal(status([{ push_enabled: true, notify_start: '07:00', notify_end: '09:00' }]).className, 'is-in-schedule-alert');
  assert.equal(status([{ email_enabled: true }]).label, 'In Schedule (Alert)');
  // Detecting, but alert window is overnight and excludes now.
  assert.equal(status([{ email_enabled: true, push_enabled: true, notify_start: '20:00', notify_end: '05:00' }]).label, 'In Schedule');
  // Detecting, alert window covers now, but no channel enabled.
  assert.equal(status([{ notify_start: '07:00', notify_end: '09:00' }]).className, 'is-in-schedule');
  // The rule's detect window gates every schedule.
  assert.equal(status([{ push_enabled: true }], { active_start: '05:00', active_end: '20:00' }).className, 'is-in-schedule-alert');
  assert.equal(status([{ push_enabled: true }], { active_start: '20:00', active_end: '05:00' }).label, 'Outside Schedule');
});
