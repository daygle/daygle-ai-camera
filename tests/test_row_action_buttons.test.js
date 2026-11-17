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

test('camera row actions use the Recordings button style, with Edit last', () => {
  const js = read('cameras.js');
  const start = js.indexOf("rowHtml += '<div class=\"cell-actions\">'");
  assert.ok(start !== -1, 'camera actions are rendered');
  const actions = js.slice(start, js.indexOf("'</div>';", start));
  const power = actions.indexOf('activity-item-action-power cam-toggle-btn');
  const remove = actions.indexOf('activity-item-action-delete cam-remove-btn');
  const edit = actions.indexOf('activity-item-action-edit cam-edit-btn');
  assert.ok(power > -1 && remove > -1 && edit > -1, 'all three buttons present');
  assert.ok(power < remove && remove < edit, 'order: power, remove, edit');
  assert.equal((actions.match(/secondary activity-item-action activity-item-action-icon /g) || []).length, 3);
  const css = read('styles.css');
  assert.match(css, /:root\.light button\.activity-item-action\.activity-item-action-edit \{\s*background:/);
  assert.doesNotMatch(css, /\.cameras-table \.cell-actions button,\s*\.cameras-table \.cell-actions a \{/);
  assert.match(css, /\.cameras-table \.cell-actions \{\s*display: flex;\s*align-items: center;\s*justify-content: flex-end;\s*gap: 4px;/);
});
