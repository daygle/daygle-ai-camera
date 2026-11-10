// Regression tests for the Models > Settings tab layout (/models/settings).
//
// The form is split into Object detection / Face detection / Advanced tuning
// sections on one fixed grid. What it SAVES must not change: the model and
// labels paths left the page as visible inputs but still post as hidden ones.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/models-settings.html'), 'utf8');
const source = readFileSync(path.resolve(here, '../web/models.js'), 'utf8');

test('the form is split into titled sections', () => {
  assert.match(html, /id="objectSettingsTitle">Object detection</);
  assert.match(html, /id="faceSettingsTitle">Face detection/);
  assert.match(html, /<summary>Advanced tuning<\/summary>/);
  assert.equal((html.match(/class="models-settings-section"/g) || []).length, 3);
});

test('every section shares one fixed grid and one control height', () => {
  assert.match(html, /\.models-settings-form \.form-grid \{\s*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);/);
  assert.match(html, /\.models-settings-form \.form-grid select \{ height: 46px;/);
});

test('model and labels paths still post with the form, as hidden inputs', () => {
  for (const name of ['model_path', 'face_model_path_display', 'labels_path']) {
    assert.match(html, new RegExp(`<input type="hidden" name="${name}" />`), name);
  }
  assert.doesNotMatch(html, /name="model_path" readonly/);
  assert.match(html, /id="modelFilesInUse"/);
  assert.match(source, /function renderFilesInUse\(\)/);
  assert.match(source, /\$\{escapeHtml\(value \|\| 'Not configured'\)\}/);
});

test('the master toggle is named for what it controls', () => {
  assert.match(html, /<span>Object Detection <span class="info-tip"/);
  assert.doesNotMatch(html, /<span>AI Enabled/);
});

test('the face model is dimmed, not disabled, while face detection is off', () => {
  assert.match(html, /<label id="faceModelField">/);
  assert.match(source, /field\.classList\.toggle\('is-inactive', toggle\.value !== 'true'\)/);
  assert.match(source, /elements\['face_enabled'\]\?\.addEventListener\('change', syncFaceModelField\)/);
  // A disabled select would drop face_model_path out of the saved FormData.
  assert.doesNotMatch(source, /faceModelSelect[^\n]*\.disabled = /);
});

test('the face model option does not repeat its size', () => {
  const block = source.slice(source.indexOf('function populateFaceModelSelect'), source.indexOf('async function loadModels'));
  assert.doesNotMatch(block, /\(\$\{model\.exported_imgsz \|\| '\?'\}\)/);
  assert.match(block, /!String\(model\.label \|\| ''\)\.includes\('×'\)/);
});
