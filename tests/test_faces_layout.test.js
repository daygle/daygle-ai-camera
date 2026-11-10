// Regression tests for the Detection > Faces tab layout (/detection/faces).
//
// Settings are two titled sections on one fixed grid; "recognition is off"
// is explained once (the status banner) instead of three times; the People
// add form is one row. Field names are unchanged, so what Save posts is too.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');
const html = read('face-recognition.html');
const review = read('unknown-faces.js');

test('settings are split into Recognition and Face detection sections', () => {
  const recognition = html.slice(html.indexOf('id="frRecognitionTitle"'), html.indexOf('id="frDetectionTitle"'));
  for (const name of ['enabled', 'match_threshold', 'retention_days', 'auto_enrich_enabled']) {
    assert.match(recognition, new RegExp(`name="${name}"`), name);
  }
  const detection = html.slice(html.indexOf('id="frDetectionTitle"'), html.indexOf('id="frReloadBtn"'));
  for (const name of ['face_mode', 'face_confidence', 'min_face_pixels', 'face_require_person']) {
    assert.match(detection, new RegExp(`name="${name}"`), name);
  }
});

test('one fixed grid and one control height', () => {
  assert.match(html, /#frForm \.form-grid, #addPersonForm\.form-grid \{ grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/);
  assert.match(html, /#frForm \.form-grid select,\s*#addPersonForm input \{ height: 46px;/);
  // Short label so it no longer wraps and strands its info icon.
  assert.match(html, /<span>Require A Person <span class="info-tip"/);
});

test('the reload button says what it reloads', () => {
  assert.match(html, /id="frReloadBtn"[^>]*>Reload recognition model<\/button>/);
});

test('"recognition is off" is explained once, by the status banner', () => {
  assert.doesNotMatch(html, /Requires an embedding model and recognition enabled/);
  assert.match(review, /See the status at the top of this page\./);
  assert.doesNotMatch(review, /Turn it on in Settings above/);
});

test('the People add form is one row with the button beside the fields', () => {
  assert.match(html, /<form id="addPersonForm" class="form-grid">/);
  assert.match(html, /#addPersonForm\.form-grid \{ grid-template-columns: minmax\(0, 1fr\) minmax\(0, 2fr\) auto;/);
});
