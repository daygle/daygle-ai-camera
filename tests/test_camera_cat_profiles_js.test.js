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
  assert.match(source, /applyPendingProfile\(mode, preset/);
});

test('cat profile shortcut is removed while reusable presets remain', () => {
  assert.doesNotMatch(source, /cat-profile-suggest-btn/);
  assert.doesNotMatch(source, /Suggest Cat Profiles/);
  assert.match(source, /profile-apply-day-btn/);
  assert.match(source, /profile-save-preset-btn/);
});

test('PTZ Motion Detection switch is editable and collected into detection', () => {
  // Tri-state select with Auto/On/Off...
  assert.match(source, /name="ptz_motion_detection"/);
  assert.match(source, /Auto \(Follow PTZ\)/);
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
  assert.match(source, /Diff \(Legacy\)/);
  assert.match(source, /Global Default/);
  assert.match(source, /Choose a Preset…/);
  assert.doesNotMatch(source, /Choose a preset…/);
  assert.doesNotMatch(source, /Global default/);
  assert.match(source, /Solar sunrise and sunset times update daily/);
  assert.match(source, /runtimeSource === 'onvif'/);
  assert.match(source, /profiles\.source === 'solar' \? 'Solar'/);
  assert.match(source, /cameraProfilePresets\.find/);
  assert.match(source, /camera-profile-preset/);
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

test('day and night profiles are edited in separate, always-visible sections', () => {
  // Both sections render from their OWN stored values...
  assert.match(source, /profileSectionHtml\(camera, 'day'\)/);
  assert.match(source, /profileSectionHtml\(camera, 'night'\)/);
  assert.match(source, /<h4 class="cam-edit-section-title">' \+ label \+ ' Profile<\/h4>/);
  // ...and both are collected independently on save.
  assert.match(source, /readProfileFromForm\(form, 'day'\)/);
  assert.match(source, /readProfileFromForm\(form, 'night'\)/);
  // Switching the Active Profile select must NOT reload stored values into
  // the form: the old change handler silently discarded unsaved edits, which
  // is what made profile updates look like they reverted.
  assert.doesNotMatch(source, /profileSelect\.addEventListener\('change'/);
  // Field names are namespaced per mode so the two editors cannot collide.
  assert.match(source, /mode \+ '_' \+ profileFieldName\(key\)/);
});

test('day and night select and apply presets independently', () => {
  assert.match(source, /name="profile_day_preset"/);
  assert.match(source, /name="profile_night_preset"/);
  assert.match(source, /profile-apply-day-btn/);
  assert.match(source, /profile-apply-night-btn/);
  assert.match(source, /requestApplyPreset\('day'\)/);
  assert.match(source, /requestApplyPreset\('night'\)/);
  assert.match(source, /day_preset_id: dayPresetId/);
  assert.match(source, /night_preset_id: nightPresetId/);
  assert.doesNotMatch(source, /Apply to Both/);
});

test('camera editor exposes reusable preset lifecycle actions per profile', () => {
  assert.match(source, /profile-save-preset-btn/);
  assert.match(source, /profile-update-day-preset-btn/);
  assert.match(source, /profile-update-night-preset-btn/);
  assert.match(source, /profile-delete-day-preset-btn/);
  assert.match(source, /profile-delete-night-preset-btn/);
  assert.match(source, /api\('\/api\/camera-profile-presets'/);
  assert.match(source, /api\('\/api\/camera-profile-presets\/'/);
});
