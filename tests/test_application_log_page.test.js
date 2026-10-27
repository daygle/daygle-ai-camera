// The Application Log page (web/application-log.js + web/application-log.html).
//
// Structural checks for the free-text search that pre-filters the live
// stream: unlike the Camera Log and AI Log pages (paged APIs, server-side
// `q`), the Application Log is a journalctl SSE tail, so its search box is
// applied client-side — while typing (the `input` listener) and to every
// entry that streams in afterwards (`isRowVisible` at `appendEntry` time).
// These guards keep all three log pages' search behaviour pinned.
//
// Run with:
//   node --test tests/test_application_log_page.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = readFileSync(path.resolve(here, '../web/application-log.js'), 'utf8');
const html = readFileSync(path.resolve(here, '../web/application-log.html'), 'utf8');

test('the search box exists in the filter row and pre-filters while typing', () => {
  assert.match(html, /id="filterSearch" type="search"/);
  assert.match(html, /aria-label="Search messages"/);
  // Live pre-filter: every keystroke reapplies the row filter to the
  // buffered stream — not only on Apply.
  assert.match(js, /filterSearch'\)\.addEventListener\('input'/);
  assert.match(js, /applyClientFilters\(\)/);
});

test('searched text filters streamed entries too, not just the initial batch', () => {
  // New SSE rows are hidden at append time through the same isRowVisible
  // rule, and that rule matches the (lowercased) message against the
  // active search — case-insensitive for the operator.
  assert.match(js, /if \(activeSearch && !\(tr\.dataset\.msg \|\| ''\)\.includes\(activeSearch\)\) return false;/);
  assert.match(js, /tr\.dataset\.msg = \(entry\.message \|\| ''\)\.toLowerCase\(\);/);
  assert.match(js, /activeSearch = document\.getElementById\('filterSearch'\)\.value\.trim\(\)\.toLowerCase\(\);/);
});

test('apply, enter and reset all honour the search box', () => {
  // Apply Filters re-reads the box (and re-fetches), Enter applties it
  // explicitly, and Reset Filters clears it and the active filter state.
  assert.match(js, /applyFiltersBtn'\)\.addEventListener\(\s*'click'[\s\S]*?activeSearch =/);
  assert.match(js, /filterSearch'\)\.addEventListener\('keydown'[\s\S]*?applyClientFilters\(\)/);
  assert.match(js, /clearFiltersBtn[\s\S]*?filterSearch'\)\.value = ''/);
  assert.match(js, /clearFiltersBtn[\s\S]*?activeSearch = ''/);
});
