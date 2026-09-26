// Regression tests for the Camera Models assignment page (/camera-models).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/camera-models.html'), 'utf8');
const source = readFileSync(path.resolve(here, '../web/camera-models.js'), 'utf8');
const nav = readFileSync(path.resolve(here, '../web/nav.js'), 'utf8');

test('page exposes the default-model panel and the assignment table', () => {
  assert.match(html, /id="defaultModelInfo"/);
  assert.match(html, /id="cameraModelsTable"/);
  assert.match(html, /id="cameraModelsBody"/);
  assert.match(html, /href="\/onnx"/);
  assert.match(html, /camera-models\.js/);
});

test('assignment actions cover assign, switch, and unassign', () => {
  assert.match(source, /data-action="assign"/);
  assert.match(source, /data-action="unassign"/);
  assert.match(source, /method: 'PUT'/);
  assert.match(source, /method: 'DELETE'/);
  assert.match(source, /\/api\/camera-models/);
  // A camera with an assignment offers Change + Unassign; without one, Assign.
  assert.match(source, /Use the default model/);
  assert.match(source, /Pick a model to assign first\./);
});

test('face-pass models stay unassignable on the page', () => {
  assert.match(source, /model\.assignable/);
  assert.match(source, /\(face pass\)/);
  assert.match(source, /disabled/);
});

test('dynamic values are escaped before HTML interpolation', () => {
  // Every interpolated value must pass through escapeHtml(); raw camera or
  // model strings are never concatenated into markup.
  const interpolations = [...source.matchAll(/\$\{([^}]*)\}/g)].map((match) => match[1].trim());
  const allowed = interpolations.filter((expr) =>
    /^escapeHtml\(/.test(expr)
    || /^(selectOptions|currentBadge|missingNote|actions)\b/.test(expr)
    || /^selected$/.test(expr)
    // Non-HTML sinks: the DOM querySelector and the request URL builders.
    || /^(CSS\.escape|encodeURIComponent)\(/.test(expr)
    || /^cameraId$/.test(expr));
  assert.ok(interpolations.length > 0);
  assert.equal(allowed.length, interpolations.length,
    `unescaped interpolations: ${interpolations.filter((expr) => !allowed.includes(expr)).join(' | ')}`);
});

test('nav links the Camera Models page under Intelligence', () => {
  assert.match(nav, /\{ href: '\/camera-models', match: '\/camera-models', label: 'Camera Models' \}/);
  assert.ok(nav.indexOf('/camera-models') > nav.indexOf('/onnx'));
});
