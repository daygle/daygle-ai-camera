// Regression tests for the Cameras page profile controls.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/cameras.js'), 'utf8');

test('camera profiles remain distinct day and night values', () => {
  assert.doesNotMatch(source, /const CAT_PROFILE_SUGGESTIONS = \{/);
  assert.match(source, /day: \{/);
  assert.match(source, /night: \{/);
  assert.match(source, /applyPendingProfiles\(preset/);
});

test('cat profile shortcut is removed while reusable presets remain', () => {
  assert.doesNotMatch(source, /cat-profile-suggest-btn/);
  assert.doesNotMatch(source, /Suggest Cat Profiles/);
  assert.match(source, /profile-apply-preset-btn/);
  assert.match(source, /profile-save-preset-btn/);
});

test('camera table exposes day and night profiles and editor can collapse', () => {
  assert.match(source, /camera-profile-pill/);
  assert.match(source, /renderCameraSortHeader\('Status', 'status'\) \+\s*'<th scope="col">Profiles<\/th>'/);
  assert.match(source, /cam-edit-collapse-btn/);
  assert.match(source, /closeAllEditForms/);
});

test('camera editor exposes reusable preset lifecycle actions', () => {
  assert.match(source, /profile_preset/);
  assert.match(source, /profile-apply-preset-btn/);
  assert.match(source, /profile-save-preset-btn/);
  assert.match(source, /profile-update-preset-btn/);
  assert.match(source, /profile-delete-preset-btn/);
  assert.match(source, /api\('\/api\/camera-profile-presets'/);
  assert.match(source, /api\('\/api\/camera-profile-presets\/'/);
});
