// Regression guard for the snapshots gallery timestamp treatment.
// Snapshots should match the events/recordings pages by showing a relative
// value (for example, "1h ago") and the full user-formatted date/time below it.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const snapshotsSource = readFileSync(path.resolve(here, '../web/snapshots.js'), 'utf8');

function snapshotRowBody() {
  const start = snapshotsSource.indexOf('function snapshotRow(event)');
  const end = snapshotsSource.indexOf('\nfunction renderStats', start);
  assert.ok(start >= 0, 'snapshotRow should be present');
  assert.ok(end > start, 'snapshotRow should end before renderStats');
  return snapshotsSource.slice(start, end);
}

test('snapshots show relative and absolute event timestamps', () => {
  const body = snapshotRowBody();
  const whenStart = body.indexOf('<span class="snapshot-when"');
  const whenEnd = body.indexOf('\n        <div class="activity-item-badges snapshot-row-badges">', whenStart);
  assert.ok(whenStart >= 0, 'snapshot row should include a timestamp container');
  assert.ok(whenEnd > whenStart, 'timestamp container should be closed');
  const whenMarkup = body.slice(whenStart, whenEnd);
  assert.match(whenMarkup, /activity-item-when-relative/);
  assert.match(whenMarkup, /timeAgo\(created\)/);
  assert.match(whenMarkup, /activity-item-when-absolute/);
  assert.match(whenMarkup, /formatDate\(created\)/);
});
