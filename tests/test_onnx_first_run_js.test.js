// Regression tests for the ONNX page's first-run state (/onnx).
//
// Nothing is downloaded on a clean install any more, so the page owns the job
// of telling the operator that detection is off and of pointing them at a
// model. These tests pin that contract in the markup and the page script.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/onnx.html'), 'utf8');
const source = readFileSync(path.resolve(here, '../web/onnx.js'), 'utf8');

test('the Status tab carries a first-run call to action', () => {
  assert.match(html, /id="firstRunNotice"/);
  // Hidden by default: a healthy install must not show it.
  assert.match(html, /<div id="firstRunNotice" class="first-run-notice" hidden>/);
  assert.match(html, /id="firstRunChooseModelBtn"/);
  assert.match(html, /Object detection is off/);
  assert.match(html, /Nothing is downloaded automatically/);
});

test('the first-run notice is styled page-locally with theme tokens', () => {
  assert.match(html, /\.first-run-notice \{/);
  assert.match(html, /\.first-run-notice\[hidden\] \{ display: none; \}/);
  // Theme-safe tokens only; the shared sheet has no color-mix either.
  assert.match(html, /border-left: 3px solid var\(--warning\)/);
  assert.match(html, /background: var\(--muted-bg\)/);
});

test('the notice appears only while the model is missing and links to the catalog', () => {
  // Toggled from the detector status, not from a one-shot page load.
  assert.match(source, /const noModel = aiEnabled && \(/);
  assert.match(source, /status\.model_exists === false/);
  assert.match(source, /'model missing'/);
  assert.match(source, /firstRunNotice\.hidden = !noModel;/);
  // Deliberately-disabled AI is not a missing model, so the notice stays away.
  assert.match(source, /const aiEnabled = status\.enabled === undefined/);
  // The button reuses the shared ARIA tab wiring by clicking the tab.
  assert.match(source, /firstRunChooseModelBtn\.addEventListener\('click'/);
  assert.match(source, /document\.getElementById\('tab-object-models'\)\?\.click\(\)/);
});

test('the object catalog explains that nothing is installed for you', () => {
  assert.match(html, /id="objectModelsFirstRunHint"/);
  assert.match(html, /press <strong>Download<\/strong> to install a model/);
  assert.match(html, /id="recommendedModelName"/);
});

test('the recommended model name comes from the server, escaped', () => {
  // textContent, never innerHTML: the label is server data.
  assert.match(source, /const recommended = objectModels\.find\(\(m\) => m\.recommended\)/);
  assert.match(source, /recommendedModelName\.textContent = recommended\.label/);
  assert.doesNotMatch(source, /recommendedModelName\.innerHTML/);
});

test('model cards badge the server-recommended model', () => {
  assert.match(source, /const recommendedBadge = m\.recommended/);
  assert.match(source, /model-status-recommended/);
  assert.match(source, /Recommended/);
  // The badge sits in the card meta row next to the install state.
  assert.match(source, /\$\{statusHtml\}\$\{recommendedBadge\}/);
});

test('the page never claims a model is fetched for the operator', () => {
  const combined = html + source;
  assert.doesNotMatch(combined, /auto.?download/i);
  assert.doesNotMatch(combined, /automatically (?:downloads?|installs?|fetches?)/i);
});
