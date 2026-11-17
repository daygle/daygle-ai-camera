// Row action buttons look the same on Events, Recordings and Snapshots:
// icon-only squares (name in title / aria-label) with one corner radius
// whether the action is a <button> or an <a> link.
//
// Run with:
//   node --test tests/test_row_action_buttons.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');

test('snapshot row actions are icon-only, like Recordings', () => {
  const js = read('snapshots.js');
  assert.doesNotMatch(js, /activity-action-label/);
  assert.match(js, /falsePositiveButton\('event', event\.id, Boolean\(falsePositive\), \{ iconOnly: true \}\)/);
  assert.match(js, /activity-item-action-icon activity-item-action-delete" data-delete-snapshot="[^"]*" type="button" title="Delete"/);
  assert.match(js, /activity-item-action-icon activity-item-action-snapshot" href="\$\{snapshotUrl\}" target="_blank" rel="noopener" title="Open"/);
});

test('icon actions share one corner radius for links and buttons', () => {
  const css = read('styles.css');
  const rule = css.match(/a\.activity-item-action\.activity-item-action-icon,\s*button\.activity-item-action\.activity-item-action-icon,[^{]*\{([^}]*)\}/);
  assert.ok(rule, 'element-qualified icon rule exists');
  assert.match(rule[1], /border-radius: 8px;/);
});
