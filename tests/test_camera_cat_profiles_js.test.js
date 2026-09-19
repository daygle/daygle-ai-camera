// Regression tests for the Cameras → Advanced → Suggest Cat Profiles action.
// The action must prepare both profiles, preserve the user's automatic-selection
// choice, and only persist when the camera form is saved.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/cameras.js'), 'utf8');

test('cat suggestions define distinct day and night profiles', () => {
  assert.doesNotMatch(source, /const CAT_PROFILE_SUGGESTIONS = \{/);
  assert.match(source, /day: \{/);
  assert.match(source, /night: \{/);
  // The values now come from the backend-owned preset, rather than a second
  // CAT_PROFILE_SUGGESTIONS constant in camera code.
  assert.match(source, /id === 'cat-small-animal'/);
  assert.match(source, /applyPendingProfiles\(catPreset/);
});

test('cat suggestion action is reviewable and non-destructive before Save', () => {
  assert.match(source, /cat-profile-suggest-btn/);
  assert.match(source, /Nothing is saved until you save the camera/);
  assert.match(source, /form\.__suggestedProfiles = \{/);
  assert.match(source, /id === 'cat-small-animal'/);
  assert.match(source, /applyPendingProfiles\(catPreset/);
  assert.match(source, /profile_day_start.*suggestion\.day_start/);
  assert.match(source, /profile_night_start.*suggestion\.night_start/);
  assert.match(source, /profile-schedule-suggestion/);
  assert.match(source, /\.\.\.\(form\.__suggestedProfiles\?\.day \|\| \{\}\)/);
  assert.match(source, /\.\.\.\(form\.__suggestedProfiles\?\.night \|\| \{\}\)/);
});

test('switching profiles displays pending cat suggestions', () => {
  assert.match(source, /form\.__suggestedProfiles\?\.\[mode\]/);
  assert.match(source, /suggested && Object\.prototype\.hasOwnProperty\.call\(suggested, key\)/);
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
