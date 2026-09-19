// Regression tests for the Live Performance "Reset Defaults" button on the
// Settings page (Settings → Detection & Live).
//
// The button refills #liveSettingsForm from FORM_DEFAULTS.live in
// web/settings.js — the same map loadSettings() falls back to when the API
// omits a key — and nothing is persisted until the user presses Save.
//
// The load-bearing invariant: FORM_DEFAULTS.live and the field names inside
// <form id="liveSettingsForm"> must stay in exact sync. If someone adds a
// field to the HTML without a default (the reset silently skips it) or adds
// a default with no field (a dead entry), these tests fail. A second test
// pins the click-handler wiring so the button can't regress to a no-op.
//
// settings.js touches the DOM at load (requireElements, enhanceFormFieldLabels),
// so these tests read the sources as text — the same static-analysis approach
// the Python suite uses in tests/test_audit_and_api_warnings.py
// (StaticRound9FixesTests).
//
// Run with:
//   node --test tests/test_settings_reset_defaults_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const settingsHtml = readFileSync(path.resolve(here, '../web/settings.html'), 'utf8');
const settingsJs = readFileSync(path.resolve(here, '../web/settings.js'), 'utf8');
const navJs = readFileSync(path.resolve(here, '../web/nav.js'), 'utf8');

function extractLiveDefaults(source) {
  // FORM_DEFAULTS opens with "const FORM_DEFAULTS = {" and its first entry is
  // "live: { ... },". Slice from that opening to the matching close so a
  // reformat (reordered keys, trailing commas) never breaks extraction.
  const start = source.indexOf('const FORM_DEFAULTS = {');
  assert.notEqual(start, -1, 'settings.js must declare FORM_DEFAULTS');
  const liveStart = source.indexOf('live: {', start);
  assert.notEqual(liveStart, -1, 'FORM_DEFAULTS must start with a live entry');
  const bodyStart = source.indexOf('{', liveStart) + 1;
  const bodyEnd = source.indexOf('\n  },', bodyStart);
  assert.notEqual(bodyEnd, -1, 'FORM_DEFAULTS.live block must close');
  const keys = [];
  for (const match of source.slice(bodyStart, bodyEnd).matchAll(/([a-z_][a-z_0-9]*):/g)) {
    keys.push(match[1]);
  }
  return keys;
}

function extractLiveFormFieldNames(html) {
  const formStart = html.indexOf('<form id="liveSettingsForm">');
  assert.notEqual(formStart, -1, 'settings.html must contain the live form');
  const formEnd = html.indexOf('</form>', formStart);
  assert.notEqual(formEnd, -1, 'the live form must close');
  const names = [];
  for (const match of html.slice(formStart, formEnd).matchAll(/name="([a-z_][a-z_0-9]*)"/g)) {
    names.push(match[1]);
  }
  return names;
}

test('FORM_DEFAULTS.live matches the Live Performance form fields exactly', () => {
  const defaultKeys = extractLiveDefaults(settingsJs);
  const fieldNames = extractLiveFormFieldNames(settingsHtml);
  assert.ok(defaultKeys.length > 0, 'FORM_DEFAULTS.live must not be empty');
  assert.deepEqual(
    [...new Set(fieldNames)].sort(),
    [...new Set(defaultKeys)].sort(),
    'every live form field needs a default and every default needs a field',
  );
});

test('Reset Defaults buttons exist on Live, Recording, and Retention cards', () => {
  for (const id of ['resetLiveDefaultsBtn', 'resetRecordingDefaultsBtn', 'resetRetentionDefaultsBtn']) {
    assert.match(settingsHtml, new RegExp(`id="${id}"`));
  }
  // All are plain secondary buttons, never implicit form submissions.
  assert.match(settingsHtml, /<button[^>]*id="resetLiveDefaultsBtn"[^>]*class="secondary"[^>]*>Reset Defaults<\/button>/);
  assert.match(settingsHtml, /<button[^>]*id="resetRecordingDefaultsBtn"[^>]*class="secondary"[^>]*>Reset Defaults<\/button>/);
  assert.match(settingsHtml, /<button[^>]*id="resetRetentionDefaultsBtn"[^>]*class="secondary"[^>]*>Reset Defaults<\/button>/);
});

test('Reset Defaults handler refills from FORM_DEFAULTS.live behind a confirm', () => {
  const start = settingsJs.indexOf("getElementById('resetLiveDefaultsBtn')");
  assert.notEqual(start, -1, 'settings.js must wire the resetLiveDefaultsBtn click');
  const handler = settingsJs.slice(start, settingsJs.indexOf('}));', start));
  // Confirm guard so a stray click cannot wipe a carefully tuned form.
  assert.match(handler, /window\.confirm\(/);
  // The refill must use the same defaults map loadSettings() falls back to.
  assert.match(handler, /fillForm\(forms\.live,\s*\{\},\s*FORM_DEFAULTS\.live\)/);
  // Client-side only: no accidental PUT, and the saved-message wording tells
  // the user the change is pending until Save.
  assert.ok(!handler.includes('api('), 'reset must not persist anything itself');
  assert.match(handler, /Save to apply/);
});

test('Recording and Retention reset handlers reuse recording defaults', () => {
  assert.match(settingsJs, /bindDefaultsReset\('resetRecordingDefaultsBtn', 'recording', 'Recording Clips'\)/);
  assert.match(settingsJs, /bindDefaultsReset\('resetRetentionDefaultsBtn', 'retention', 'Retention'\)/);
  const helperStart = settingsJs.indexOf('function bindDefaultsReset');
  const helper = settingsJs.slice(helperStart, settingsJs.indexOf("document.getElementById('resetLiveDefaultsBtn')", helperStart));
  assert.match(helper, /FORM_DEFAULTS\.recording/);
  assert.match(helper, /Nothing changes until you save/);
  assert.match(helper, /fillForm\(forms\[formName\]/);
});

test('nav.js decorates the button with the reset icon', () => {
  assert.match(navJs, /\['reset defaults',\s*DAYGLE_BUTTON_ICONS\.reset\]/);
});
