// The Recordings and Snapshots libraries hide their filter forms behind a
// Filters button, and Delete All Recordings sits to the left of Filters.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');

test('Snapshots starts with its filters collapsed behind a Filters button', () => {
  const html = read('snapshots.html');
  const js = read('snapshots.js');
  assert.match(html, /<form id="snapshotFilterForm"[^>]* hidden>/);
  assert.match(html, /id="snapshotsFilterToggle"[^>]*aria-expanded="false" aria-controls="snapshotFilterForm"/);
  assert.match(html, /id="snapshotsFilterBadge"/);
  assert.match(js, /const SNAPSHOTS_FILTER_PANEL_KEY = 'daygle\.snapshots\.filters\.open'/);
  assert.match(js, /els\.filterToggle\?\.addEventListener\('click'/);
  // Default dates (today) do not count as active filters.
  assert.match(js, /filters\.dateFrom && filters\.dateFrom !== today/);
});

test('Delete All Recordings sits to the left of Filters', () => {
  const html = read('recordings.html');
  assert.ok(html.indexOf('id="deleteAllRecordingsBtn"') < html.indexOf('id="recordingsFilterToggle"'));
});
