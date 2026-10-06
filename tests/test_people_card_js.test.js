// The People card on the Faces page (/detection/faces#people).
//
// The card's markup outlived the script that fills it: a cleanup commit
// deleted web/people.js as "obsolete", leaving an empty People card with an
// Add Person form that did nothing. These checks keep the page, its script
// and the People API wired together.
//
// Run with:
//   node --test tests/test_people_card_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.resolve(here, '../web/face-recognition.html'), 'utf8');
const people = readFileSync(path.resolve(here, '../web/people.js'), 'utf8');
const review = readFileSync(path.resolve(here, '../web/unknown-faces.js'), 'utf8');

test('the Faces page loads the People script after utils.js', () => {
  const utils = html.indexOf('<script src="/static/utils.js"></script>');
  const script = html.indexOf('<script src="/static/people.js"></script>');
  assert.notEqual(script, -1, 'face-recognition.html must load people.js');
  assert.ok(utils !== -1 && utils < script, 'people.js uses api() / escapeHtml from utils.js');
});

test('every element the People script looks up exists on the page', () => {
  const ids = [...people.matchAll(/getElementById\('([^']+)'\)/g)].map((match) => match[1]);
  assert.deepEqual([...new Set(ids)].sort(), ['addPersonBtn', 'addPersonForm', 'peopleEmpty', 'peopleList', 'peopleMessage']);
  for (const id of ids) assert.match(html, new RegExp(`id="${id}"`), `#${id} missing from the Faces page`);
});

test('the People script lists, adds, renames, deletes and enrols through the People API', () => {
  assert.match(people, /api\('\/api\/persons'\)/);
  assert.match(people, /api\('\/api\/persons', \{\s*method: 'POST'/);
  assert.match(people, /method: 'PATCH'/);
  assert.match(people, /\/faces`, \{\s*method: 'POST'/);
  assert.match(people, /\/thumbnail`/);
  assert.match(people, /method: 'DELETE'/);
});

test('user-supplied names and notes are escaped', () => {
  assert.match(people, /\$\{escapeHtml\(person\.name\)\}/);
  assert.match(people, /\$\{escapeHtml\(person\.notes\)\}/);
});

test('the People and Review cards refresh each other', () => {
  assert.match(people, /new CustomEvent\('daygle:people-changed', \{ detail: \{ source: 'people' \} \}\)/);
  assert.match(people, /addEventListener\('daygle:people-changed'/);
  assert.match(review, /new CustomEvent\('daygle:people-changed', \{ detail: \{ source: 'review' \} \}\)/);
  assert.match(review, /addEventListener\('daygle:people-changed'/);
});
