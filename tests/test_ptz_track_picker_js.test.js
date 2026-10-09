// Camera > PTZ "Follow These Objects": a chip picker instead of free text.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/cameras.js'), 'utf8');

function extractFunction(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} not found`);
  let depth = 0;
  for (let i = source.indexOf('{', start); i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

test('the follow field is a picker backed by a hidden comma-separated input', () => {
  assert.match(source, /<input name="ptz_auto_track_labels" type="hidden"/);
  assert.doesNotMatch(source, /name="ptz_auto_track_labels" type="text"/);
  assert.match(source, /data-ptz-track-picker/);
  assert.match(source, /bindPtzTrackPicker\(form\)/);
  // Groups and model classes both come from the label-groups API.
  assert.match(source, /api\('\/api\/settings\/label_groups'\)/);
  // The save path still reads the same field name.
  assert.match(source, /labels: getName\('ptz_auto_track_labels'\)/);
});

test('saved labels open as de-duplicated lowercase chips', () => {
  const parse = new Function(`${extractFunction('parsePtzTrackLabels')}; return parsePtzTrackLabels;`)();
  assert.deepEqual(parse('Cat, dog,, cat , Pet'), ['cat', 'dog', 'pet']);
  assert.deepEqual(parse(''), []);
  assert.deepEqual(parse(null), []);
});
