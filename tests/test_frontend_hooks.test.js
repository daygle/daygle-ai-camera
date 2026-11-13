// Static integrity of the frontend's DOM contract: every element id a script
// looks up must exist somewhere, and every id/class hook CSS styles by id must
// belong to an element that still exists.
//
// These are the checks the "HTML/CSS audit" round did by hand. They caught five
// dead lookups in web/live.js (the zones page's old stat cards) and four stale
// CSS id selectors (#aiStatusPanel, #soundRulesWrap, #activityFeed, #alertFeed)
// left behind by renames and page merges. Nothing at runtime fails when a hook
// disappears - the lookup just yields null and the CSS rule just never matches -
// so the drift is invisible until someone wonders why a panel is unstyled.
//
// Ids can legitimately come from three places: a template in web/, markup a
// script builds itself (id="event-feed-rows", id="navAvatar"), or markup a
// Python module renders (the sign-in page). All three count as "defined".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const webDir = path.join(root, 'web');

function listFiles(dir, suffix) {
  return readdirSync(dir).filter((name) => name.endsWith(suffix));
}

function walk(dir, suffix, out = []) {
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) walk(full, suffix, out);
    else if (name.endsWith(suffix)) out.push(full);
  }
  return out;
}

const htmlFiles = listFiles(webDir, '.html');
const jsFiles = listFiles(webDir, '.js');
const cssFiles = listFiles(webDir, '.css');
const pyFiles = walk(path.join(root, 'app'), '.py');

const htmlSource = htmlFiles.map((f) => readFileSync(path.join(webDir, f), 'utf8')).join('\n');
const pySource = pyFiles.map((f) => readFileSync(f, 'utf8')).join('\n');
const jsSource = jsFiles.map((f) => readFileSync(path.join(webDir, f), 'utf8')).join('\n');
const cssSource = cssFiles.map((f) => readFileSync(path.join(webDir, f), 'utf8')).join('\n');

const definedIds = new Set();
for (const source of [htmlSource, jsSource, pySource]) {
  for (const m of source.matchAll(/\bid\s*=\s*["']([A-Za-z][-\w]*)["']/g)) definedIds.add(m[1]);
}

test('every getElementById target exists in a template, a script, or a Python page', () => {
  const missing = [];
  for (const file of jsFiles) {
    const source = readFileSync(path.join(webDir, file), 'utf8');
    for (const m of source.matchAll(/getElementById\(\s*'([^']+)'\s*\)/g)) {
      const id = m[1];
      if (id.includes('${')) continue; // templated id, resolved at runtime
      if (!definedIds.has(id)) missing.push(`${file}: #${id}`);
    }
  }
  assert.deepEqual(missing, [], `element ids looked up but never rendered:\n  ${missing.join('\n  ')}`);
});

test('every CSS id selector matches an element id that still exists', () => {
  // Skip colour literals: `#fff`, `#f8fafc` and friends are not selectors.
  const isColour = (value) => /^[0-9a-fA-F]{3,8}$/.test(value);
  const missing = new Set();
  for (const m of cssSource.matchAll(/#([A-Za-z][-\w]*)/g)) {
    const id = m[1];
    if (isColour(id) || definedIds.has(id)) continue;
    missing.add(id);
  }
  assert.deepEqual([...missing].sort(), [], 'stale CSS id selectors');
});

test('no template declares the same id twice', () => {
  const duplicates = [];
  for (const file of htmlFiles) {
    const source = readFileSync(path.join(webDir, file), 'utf8');
    const seen = new Set();
    for (const m of source.matchAll(/\sid="([^"]+)"/g)) {
      if (seen.has(m[1])) duplicates.push(`${file}: #${m[1]}`);
      seen.add(m[1]);
    }
  }
  assert.deepEqual(duplicates, [], 'duplicate ids in a template');
});
