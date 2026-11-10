// Regression tests for the Detection > Objects tab layout (/detection).
//
// Groups were two cards (list + behaviour) and the 80-class override table
// made the page ~7000px tall with Save only at the bottom. Now: one Groups
// table, a searchable override table defaulting to "Changed only", and a
// sticky save bar. Filtering HIDES rows - Save reads every row's input.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/objects.html'), 'utf8');
const source = readFileSync(path.resolve(here, '../web/objects.js'), 'utf8');

test('groups are one card: members, mode, colour and edit/remove in one table', () => {
  assert.equal((html.match(/<h2>Groups<\/h2>/g) || []).length, 1);
  assert.doesNotMatch(html, /<h2>Group Behavior<\/h2>/);
  assert.doesNotMatch(html, /id="groupsList"/);
  const card = html.slice(html.indexOf('id="groupsCard"'), html.indexOf('</section>', html.indexOf('id="groupsCard"')));
  for (const id of ['groupModesBody', 'groupForm', 'groupAddBtn']) assert.match(card, new RegExp(`id="${id}"`), id);
  const table = source.slice(source.indexOf('function renderGroupModes'), source.indexOf('function resetGroupForm'));
  assert.match(table, /data-group-edit=/);
  assert.match(table, /data-group-remove=/);
  // The two save models are spelled out on the card.
  assert.match(html, /Adding, editing or removing a group saves straight away\./);
});

test('removing a group asks first, since it saves immediately', () => {
  assert.match(source, /window\.confirm\(`Remove the \$\{titleCase\(name\)\} group\? This saves straight away\.`\)/);
});

test('the override table is searchable and defaults to changed-only', () => {
  assert.match(html, /<input type="search" id="objectsFilter"/);
  assert.match(html, /<input type="checkbox" id="objectsChangedOnly" checked \/>/);
  assert.match(source, /function applyObjectFilter\(\)/);
  // Hidden, not removed: saveObjects reads every row's still-alert input.
  assert.match(source, /row\.hidden = !matches;/);
  assert.match(source, /tableBody\.querySelectorAll\('input\[data-still-alert\]'\)\.forEach/);
  // Rows edited this session stay visible under "Changed only".
  assert.match(source, /touchedLabels\.add\(label\);/);
});

test('unsaved changes show a sticky save bar', () => {
  assert.match(html, /<div class="objects-save-bar" id="objectsSaveBar"[^>]*hidden>/);
  assert.match(html, /\.objects-save-bar \{\s*position: sticky; bottom: 12px;/);
  assert.match(source, /if \(saveBar\) saveBar\.hidden = false;/);
  assert.match(source, /if \(saveBar\) saveBar\.hidden = true;/);
});

test('defaults sit side by side with aligned labels', () => {
  assert.match(html, /<form id="objectsForm" class="objects-defaults">/);
  assert.match(html, /\.objects-defaults \{ display: grid; grid-template-columns: minmax\(0, 320px\) auto;[^}]*align-items: start;/);
});
