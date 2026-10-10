// Settings > Camera Profiles (camera-profiles.js) and the shared profile
// field definitions (profile-fields.js).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (file) => readFileSync(path.resolve(here, '../web', file), 'utf8');

function loadFields() {
  const sandbox = {
    titleCase: (value) => String(value).replace(/\b\w/g, (c) => c.toUpperCase()),
  };
  vm.createContext(sandbox);
  vm.runInContext(`${read('profile-fields.js')}\nthis.api = { PROFILE_FIELD_GROUPS, parseProfileField, profileSummary };`, sandbox);
  return sandbox.api;
}

test('profile fields cover the profile keys and never the legacy motion keys', () => {
  const { PROFILE_FIELD_GROUPS } = loadFields();
  const keys = PROFILE_FIELD_GROUPS.flatMap((group) => group.fields.map((field) => field.key));
  assert.deepEqual(JSON.parse(JSON.stringify(PROFILE_FIELD_GROUPS.map((group) => group.title))), ['Detection Timing', 'Confirmation', 'Object Detection', 'Motion']);
  assert.equal(new Set(keys).size, keys.length);
  for (const key of ['detection_interval_seconds', 'object_detection_second_look', 'object_detection_low_light',
    'adaptive_detection_enabled', 'motion_shadow_suppression', 'periodic_scan_interval_seconds']) {
    assert.ok(keys.includes(key), key);
  }
  for (const legacy of ['motion_gate_fraction', 'motion_scale_fraction', 'motion_background_alpha', 'motion_algorithm']) {
    assert.ok(!keys.includes(legacy), legacy);
  }
});

test('field parsing and the one-line summary', () => {
  const { parseProfileField, profileSummary } = loadFields();
  assert.equal(parseProfileField({ kind: 'number' }, '0.3'), 0.3);
  assert.equal(parseProfileField({ kind: 'number' }, ''), null);
  assert.equal(parseProfileField({ kind: 'select' }, 'true'), true);
  assert.equal(parseProfileField({ kind: 'select' }, '2x2'), '2x2');
  assert.equal(profileSummary({}), '');
  assert.equal(
    profileSummary({ detection_interval_seconds: 0.3, ingest_frame_fps: 8, object_detection_tiling: '2x2', object_detection_second_look: true, motion_frame_width: 640 }),
    'Interval 0.3 s · 8 fps · Tiling 2 × 2 · Second Look On · +1 more',
  );
});

test('the library lists, edits, duplicates and deletes through the presets API', () => {
  const source = read('camera-profiles.js');
  assert.match(source, /api\('\/api\/camera-profile-presets'\)/);
  assert.match(source, /method: 'POST', body/);
  assert.match(source, /method: 'PUT', body/);
  assert.match(source, /method: 'DELETE'/);
  // Built-ins are read-only (View + Duplicate); in-use profiles cannot be deleted.
  assert.match(source, /profile\.builtin\s*\?\s*`<button type="button" class="secondary" data-profile-view=/);
  assert.match(source, /data-profile-delete="\$\{id\}"\$\{usedBy\.length \? ' disabled/);
  const html = read('settings.html');
  assert.match(html, /data-tab="profiles"/);
  assert.match(html, /<script src="\/static\/profile-fields\.js"><\/script>\s*<script src="\/static\/camera-profiles\.js"><\/script>/);
  assert.match(read('cameras.html'), /<script src="\/static\/profile-fields\.js"><\/script>\s*<script src="\/static\/cameras\.js"><\/script>/);
});

test('Live Performance is split into headed groups', () => {
  const html = read('settings.html');
  const card = html.slice(html.indexOf('<form id="liveSettingsForm">'), html.indexOf('<details class="settings-advanced">'));
  const titles = [...card.matchAll(/<h3 class="settings-group-title">([^<]+)<\/h3>/g)].map((m) => m[1]);
  assert.deepEqual(titles, ['Detection Timing', 'Confirmation', 'Object Detection', 'Events', 'Live View &amp; Display']);
});
