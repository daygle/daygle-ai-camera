// Unit tests for the Celsius/Fahrenheit display preference in web/utils.js.
//
// The per-account choice (users.temp_unit) only changes presentation: the
// backend, nvidia-smi and the GPU Health thresholds all stay in Celsius, so
// formatTemperature() must convert for display and leave the stored value
// alone. nav.js seeds the preference from /api/auth/me on every page; the
// System page renders the GPU card through the helper.
//
// utils.js is a classic browser script that reaches for `window` at load, so
// we load it into a vm context behind a lightweight window stub - the same
// pattern as test_tripwire_geometry_js.test.js.
//
// Run with:
//   node --test tests/test_temperature_unit_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web', 'utils.js'), 'utf8');
const navSource = readFileSync(path.resolve(here, '../web', 'nav.js'), 'utf8');
const systemSource = readFileSync(path.resolve(here, '../web', 'system.js'), 'utf8');
const settingsSource = readFileSync(path.resolve(here, '../web', 'settings.js'), 'utf8');
const settingsHtml = readFileSync(path.resolve(here, '../web', 'settings.html'), 'utf8');

const sandbox = {
  window: { addEventListener() {}, removeEventListener() {} },
  BroadcastChannel: undefined,
};
sandbox.window.daygleUi = null;
vm.createContext(sandbox);
vm.runInContext(utilsSource, sandbox);

const ui = sandbox.window.daygleUi;

test('utils.js exposes the temperature unit helpers on window.daygleUi', () => {
  for (const name of [
    'formatTemperature', 'daygleTemperatureSymbol', 'daygleTemperatureInUnit',
    'daygleTemperatureFromUnit', 'setDaygleTempUnit', 'getDaygleTempUnit',
  ]) {
    assert.equal(typeof ui[name], 'function', `window.daygleUi.${name} should be a function`);
  }
});

test('temperatures render in Celsius by default and convert for Fahrenheit', () => {
  ui.setDaygleTempUnit('c');
  assert.equal(ui.getDaygleTempUnit(), 'c');
  assert.equal(ui.formatTemperature(85), '85°C');

  ui.setDaygleTempUnit('f');
  assert.equal(ui.getDaygleTempUnit(), 'f');
  // The GPU card's own readings: 85 C / 90 C warn and critical thresholds.
  assert.equal(ui.formatTemperature(85), '185°F');
  assert.equal(ui.formatTemperature(90), '194°F');
});

test('an unknown or missing unit falls back to Celsius', () => {
  ui.setDaygleTempUnit('kelvin');
  assert.equal(ui.getDaygleTempUnit(), 'c');
  assert.equal(ui.formatTemperature(21.5, { unit: 'F' }), '71°F');
});

test('a missing or non-numeric reading renders the placeholder, not NaN', () => {
  assert.equal(ui.formatTemperature(null), '-');
  assert.equal(ui.formatTemperature(undefined), '-');
  assert.equal(ui.formatTemperature('hot'), '-');
});

test('nav.js seeds the preference and the System page renders through it', () => {
  assert.match(navSource, /temp_unit: user\?\.temp_unit \|\| 'c'/);
  assert.match(systemSource, /formatTemperature\(temp\)/);
  assert.match(systemSource, /formatTemperature\(criticalTemp\)/);
});

test('values typed in the chosen unit convert back to Celsius', () => {
  // Settings > Maintenance edits the Celsius thresholds in the user's unit.
  assert.equal(ui.daygleTemperatureFromUnit(185, 'f'), 85);
  assert.equal(ui.daygleTemperatureFromUnit(194, 'f'), 90);
  assert.equal(ui.daygleTemperatureFromUnit(85, 'c'), 85);
  // The form's own bounds: 30-120 C (warn) and 40-150 C (critical).
  assert.equal(Math.round(ui.daygleTemperatureFromUnit(86, 'f')), 30);
  assert.equal(Math.round(ui.daygleTemperatureFromUnit(248, 'f')), 120);
  assert.equal(Math.round(ui.daygleTemperatureFromUnit(104, 'f')), 40);
  assert.equal(Math.round(ui.daygleTemperatureFromUnit(302, 'f')), 150);
  assert.equal(ui.daygleTemperatureFromUnit('', 'f'), null);
  assert.equal(ui.daygleTemperatureFromUnit(null, 'f'), null);
  assert.equal(ui.daygleTemperatureFromUnit('hot', 'c'), null);
});

test('the GPU Health form renders and saves thresholds in the chosen unit', () => {
  // The inputs are named *_c and the API only ever receives Celsius.
  assert.match(settingsHtml, /<span id="gpuWarnLabel">Warn Temperature \(°C\)<\/span>/);
  assert.match(settingsHtml, /<span id="gpuCriticalLabel">Critical Temperature \(°C\)<\/span>/);
  assert.match(settingsHtml, /id="gpuHealthHelp"/);
  assert.match(settingsSource, /function renderGpuHealth/);
  assert.match(settingsSource, /function gpuPayload/);
  assert.match(settingsSource, /input\.min = gpuUnitText\(field\.minC, unit\)/);
  assert.match(settingsSource, /input\.max = gpuUnitText\(field\.maxC, unit\)/);
  assert.match(settingsSource, /daygleTemperatureFromUnit\(value, unit\)/);
  assert.match(settingsSource, /renderGpuHealth\(settings\.system\)/);
  assert.match(settingsSource, /bindForm\('gpu', 'GPU health', 'gpu', gpuPayload, renderGpuHealth\)/);
});
