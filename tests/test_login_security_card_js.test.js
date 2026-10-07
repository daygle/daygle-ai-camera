// Regression tests for the Login Security card on the Settings page
// (Settings → Network & Access → Login Security).
//
// The card is <form id="authSettingsForm"> in web/settings.html: Session
// Timeout (hours), Max Login Attempts, and Lockout Minutes, saved via the
// shared bindForm('auth') PUT to /api/settings/system/auth and loaded back
// from GET /api/settings/system (settings.auth).
//
// The load-bearing invariants, the same static-analysis style the Python
// suite uses for HTML/script contracts:
//   1. FORM_DEFAULTS.auth and the form's field names must stay in exact sync
//      (fillForm() silently skips a field with no default).
//   2. Every field must be declared in FIELD_TYPES with the right coercion —
//      session_timeout_hours as a fractional number, the other two as
//      integers — and blanks must not be sent as 0 (coercePayload skips
//      empty strings, so clearing a field means "leave unchanged" server-side
//      rather than a bogus minimum value).
//   3. The submit button lives outside the form and relies on form="…", so
//      dropping that attribute would turn Save into a no-op.
//   4. HTML min/max bounds mirror the backend validator in
//      app/payload_validators.py (0.25–720 hours, 1–100 attempts,
//      1–1440 minutes) so the browser rejects out-of-range input first.
//
// Run with:
//   node --test tests/test_login_security_card_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const settingsHtml = readFileSync(path.resolve(here, '../web/settings.html'), 'utf8');
const settingsJs = readFileSync(path.resolve(here, '../web/settings.js'), 'utf8');

const AUTH_FIELDS = [
  { name: 'session_timeout_hours', min: '0.25', max: '720', step: '0.25', default: 12 },
  { name: 'max_login_attempts', min: '1', max: '100', step: null, default: 5 },
  { name: 'lockout_minutes', min: '1', max: '1440', step: null, default: 15 },
];

function authFormHtml() {
  const formStart = settingsHtml.indexOf('<form id="authSettingsForm"');
  assert.notEqual(formStart, -1, 'settings.html must contain authSettingsForm');
  const formEnd = settingsHtml.indexOf('</form>', formStart);
  assert.notEqual(formEnd, -1, 'the auth settings form must close');
  return settingsHtml.slice(formStart, formEnd);
}

test('the Login Security form carries exactly the three card fields with backend-matching bounds', () => {
  const html = authFormHtml();
  const names = [...html.matchAll(/name="([a-z_][a-z_0-9]*)"/g)].map((match) => match[1]);
  assert.deepEqual(names.sort(), AUTH_FIELDS.map((field) => field.name).sort());
  for (const field of AUTH_FIELDS) {
    const input = html.match(new RegExp(`<input name="${field.name}"[^>]*>`));
    assert.ok(input, `field ${field.name} must be an input`);
    assert.match(input[0], /type="number"/, `${field.name} must be a number input`);
    assert.match(input[0], new RegExp(`min="${field.min.replace('.', '\\.')}"`), `${field.name} min must match the backend validator`);
    assert.match(input[0], new RegExp(`max="${field.max}"`), `${field.name} max must match the backend validator`);
    if (field.step) assert.match(input[0], new RegExp(`step="${field.step.replace('.', '\\.')}"`), `${field.name} step`);
  }
});

test('FORM_DEFAULTS.auth matches the form fields exactly with the documented defaults', () => {
  const start = settingsJs.indexOf('auth: {', settingsJs.indexOf('const FORM_DEFAULTS = {'));
  assert.notEqual(start, -1, 'FORM_DEFAULTS must declare an auth entry');
  const bodyStart = settingsJs.indexOf('{', start) + 1;
  const bodyEnd = settingsJs.indexOf('\n  },', bodyStart);
  assert.notEqual(bodyEnd, -1, 'FORM_DEFAULTS.auth block must close');
  const defaults = {};
  for (const match of settingsJs.slice(bodyStart, bodyEnd).matchAll(/([a-z_][a-z_0-9]*):\s*([0-9.]+)/g)) {
    defaults[match[1]] = Number(match[2]);
  }
  assert.deepEqual(defaults, Object.fromEntries(AUTH_FIELDS.map((field) => [field.name, field.default])));
});

test('payload coercion types the fields the API expects', () => {
  const integerBlock = settingsJs.match(/integer: new Set\(\[([\s\S]*?)\]\)/);
  assert.ok(integerBlock, 'FIELD_TYPES must declare the integer set');
  assert.match(integerBlock[1], /\bmax_login_attempts\b/);
  assert.match(integerBlock[1], /\blockout_minutes\b/);
  const numberBlock = settingsJs.match(/number: new Set\(\[([\s\S]*?)\]\)/);
  assert.ok(numberBlock, 'FIELD_TYPES must declare the number set');
  assert.match(numberBlock[1], /\bsession_timeout_hours\b/);
  // coercePayload must skip blank strings so a cleared field is omitted from
  // the payload instead of being sent as 0 (below every backend minimum).
  assert.match(settingsJs, /else if \(value === ''\) continue;/);
});

test('load and save wiring is intact', () => {
  // The form element must be registered.
  assert.match(settingsJs, /auth: document\.getElementById\('authSettingsForm'\),/);
  // Loaded from the system settings payload with the same defaults fallback.
  assert.match(settingsJs, /fillForm\(forms\.auth, settings\.auth, FORM_DEFAULTS\.auth\);/);
  // Saved through the shared PUT binding to /api/settings/system/auth.
  assert.match(settingsJs, /bindForm\('auth', 'Login security'\);/);
  assert.match(settingsJs, /\/api\/settings\/system\/\$\{endpointName\}`, \{ method: 'PUT'/);
});

test('the Save button outside the form still submits via form="authSettingsForm"', () => {
  const button = settingsHtml.match(/<button[^>]*type="submit"[^>]*form="authSettingsForm"[^>]*>/)
    ?? settingsHtml.match(/<button[^>]*form="authSettingsForm"[^>]*type="submit"[^>]*>/);
  assert.ok(button, 'Save Login Security must reference the form id');
});
