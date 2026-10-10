// Regression tests for the Cameras page profile controls.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/cameras.js'), 'utf8');

test('camera profiles are chosen per mode, not edited on the camera page', () => {
  assert.doesNotMatch(source, /const CAT_PROFILE_SUGGESTIONS = \{/);
  assert.doesNotMatch(source, /cat-profile-suggest-btn|Suggest Cat Profiles/);
  // One select per mode, linked to a shared profile by id...
  assert.match(source, /<select name="profile_' \+ mode \+ '_preset" data-profile-select="' \+ mode \+ '">'/);
  assert.match(source, /day_preset_id: getName\('profile_day_preset'\) \|\| null/);
  assert.match(source, /night_preset_id: getName\('profile_night_preset'\) \|\| null/);
  assert.match(source, /globalDefaultProfileId\(mode\)/);
  assert.match(source, /Custom \(This Camera Only\)/);
  // ...managed on the Settings page, with no per-camera editor or preset buttons.
  assert.match(source, /href="\/settings#profiles"/);
  for (const gone of ['profileSectionHtml', 'readProfileFromForm', 'applyPendingProfile', 'profile-apply-day-btn',
    'profile-save-day-preset-btn', 'profile-update-day-preset-btn', 'profile-delete-day-preset-btn',
    "api('/api/camera-profile-presets'"]) {
    assert.ok(!source.includes(gone), gone);
  }
  // Only the profile ids are sent: no per-mode values, summaries or legacy
  // motion override notes on the camera page.
  for (const gone of ['legacyOverrides', 'data-legacy-motion-override', 'Use the global values', 'profileSelectSummary']) {
    assert.ok(!source.includes(gone), gone);
  }
});

test('PTZ Motion Detection switch is editable and collected into detection', () => {
  // Tri-state select with Automatic/On/Off...
  assert.match(source, /name="ptz_motion_detection"/);
  assert.match(source, /Automatic \(Follow PTZ\)/);
  // ...bound to the camera-level detection block (not a profile field)...
  assert.match(source, /camera\.detection\?\.ptz_motion_detection/);
  // ...and written into data.detection so the save merge preserves zones.
  assert.match(source, /detection:\s*\{\s*ptz_motion_detection:\s*getName\('ptz_motion_detection'\)/);
});

test('Detection Mode remains an object-level setting', () => {
  // Camera profiles must not expose or collect the per-object moving/still mode.
  assert.doesNotMatch(source, /name="profile_object_detection_motion_mode"/);
  assert.doesNotMatch(source, /profileValue\('object_detection_motion_mode'/);
});

test('camera table exposes day and night profiles and editor can collapse', () => {
  assert.match(source, /camera-profile-pill/);
  assert.match(source, /Solar \(Daily Sunrise\/Sunset\)/);
  assert.match(source, /ONVIF IR State \(Fallback Schedule\)/);
  assert.match(source, /'Global Default'/);
  assert.doesNotMatch(source, /Global default/);
  assert.match(source, /Solar sunrise and sunset times update daily/);
  assert.match(source, /runtimeSource === 'onvif'/);
  assert.match(source, /profiles\.source === 'solar' \? 'Solar'/);
  assert.match(source, /cameraProfilePresets\.find/);
  assert.match(source, /activeProfile\.charAt\(0\)\.toUpperCase\(\)/);
  assert.doesNotMatch(source, /Check IR State Now|ir-check-btn|\/ir-state/);
  assert.match(source, /renderCameraSortHeader\('Status', 'status'\) \+\s*'<th scope="col">Profiles<\/th>'/);
  assert.match(source, /cam-edit-collapse-btn/);
  assert.match(source, /ICONS\.chevronUp/);
  assert.doesNotMatch(source, /title="Collapse camera settings">Collapse<\/button>/);
  assert.match(source, /closeAllEditForms/);
  assert.match(source, /realIndex === openCameraEditIndex/);
  assert.doesNotMatch(source, /insertAdjacentHTML\('afterend', safeHtml\(\[formHtml\]\)\)/);
});

