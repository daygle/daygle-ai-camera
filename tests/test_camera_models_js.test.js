// Regression tests for the Camera Models assignment page (/models/cameras).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/camera-models.html'), 'utf8');
const source = readFileSync(path.resolve(here, '../web/camera-models.js'), 'utf8');
const nav = readFileSync(path.resolve(here, '../web/nav.js'), 'utf8');

test('page exposes the default model and the assignment table in one card', () => {
  assert.match(html, /id="defaultModelInfo"/);
  assert.match(html, /id="cameraModelsTable"/);
  assert.match(html, /id="cameraModelsBody"/);
  assert.match(html, /href="\/models"/);
  assert.match(html, /camera-models\.js/);
});

test('one picker per camera saves on change: Default unassigns, a model assigns', () => {
  assert.match(source, /tableBody\.addEventListener\('change'/);
  assert.match(source, /method: 'PUT'/);
  assert.match(source, /method: 'DELETE'/);
  assert.match(source, /\/api\/camera-models/);
  // The first option is the default model by name; choosing it unassigns.
  assert.match(source, /`Default \(\$\{label\}\)`/);
  assert.match(source, /if \(modelPath\) \{[\s\S]*method: 'PUT'[\s\S]*\} else \{[\s\S]*method: 'DELETE'/);
  // No separate Assign / Change / Unassign buttons any more.
  assert.doesNotMatch(source, /data-action="(assign|unassign)"/);
  // A failed save re-renders from the server so the picker snaps back.
  const save = source.slice(source.indexOf('async function saveCameraModel'));
  assert.match(save, /await loadAssignments\(\);/);
});

test('an assignment whose file is missing stays selected, never shown as Default', () => {
  assert.match(source, /- file missing/);
  assert.match(source, /if \(assigned && !objectModels\.some\(\(model\) => model\.path === camera\.model_path\)\)/);
  assert.match(source, /is-missing/);
});

test('face-pass models are not offered as camera models', () => {
  assert.match(source, /filter\(\(model\) => model\.assignable\)/);
  assert.match(html, /href="\/models\/faces"/);
});

test('dynamic values are escaped before HTML interpolation', () => {
  // Every interpolated value must pass through escapeHtml(); raw camera or
  // model strings are never concatenated into markup.
  const interpolations = [...source.matchAll(/\$\{([^}]*)\}/g)].map((match) => match[1].trim());
  const allowed = interpolations.filter((expr) =>
    /^escapeHtml\(/.test(expr)
    // Helpers that return already-escaped markup, and fixed attribute flags.
    || /^(selectOptions|statusPill|cameraIdNote)\(camera\)$/.test(expr)
    || /^(missing|selected)$/.test(expr)
    || /^assigned \? '' : ' selected'$/.test(expr)
    // Plain strings that are escaped (or sent to a textContent toast) later.
    || /^(label|cameraName\(cameraId\))$/.test(expr)
    || /^camera\.model_name \|\| fileName\(camera\.model_path\)$/.test(expr)
    // Non-HTML sinks: the request URL builders.
    || /^encodeURIComponent\(/.test(expr));
  assert.ok(interpolations.length > 0);
  assert.equal(allowed.length, interpolations.length,
    `unescaped interpolations: ${interpolations.filter((expr) => !allowed.includes(expr)).join(' | ')}`);
});

test('model names keep their case: no uppercasing status-badge', () => {
  assert.doesNotMatch(source, /status-badge/);
  assert.match(source, /camera-model-status/);
});

test('Camera Models is a tab of Intelligence > Models', () => {
  assert.match(nav, /\{ href: '\/models', matches: \['\/models'\], label: 'Models'/);
  assert.match(nav, /\{ href: '\/models\/cameras', icon: '[^']+', label: 'Camera Models' \}/);
  assert.ok(nav.indexOf("href: '/models/cameras', icon") > nav.indexOf("href: '/models', icon"));
});
