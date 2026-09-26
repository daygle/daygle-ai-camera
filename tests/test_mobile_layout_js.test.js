// Mobile-layout regression tests for the four pages that were not usable on a
// small screen: cameras, alerts, camera-models and users.
//
// Two failure modes are pinned here:
//
//  1. Page-level horizontal overflow. A wide table inside a scroll box still
//     stretches the WHOLE page when its card is a grid item, because a grid
//     item's automatic minimum size is its min-content. Every card that holds
//     a wide table therefore needs an explicit `min-width: 0` reset.
//  2. Unusable columns. Where a table is the page's main content it stacks into
//     cards on phones, driven by `data-label` on the cells.
//
// These are source-level assertions (the repo's convention for page scripts);
// there is no DOM in the node test runner.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');
const shared = read('styles.css');

const pages = {
  cameras: { html: read('cameras.html'), js: read('cameras.js') },
  alerts: { html: read('alerts.html'), js: read('alerts.js') },
  'camera-models': { html: read('camera-models.html'), js: read('camera-models.js') },
  users: { html: read('users.html'), js: read('users.js') },
};

// ── 1. No page-level horizontal overflow ─────────────────────────────────────

test('every page that holds a wide table resets its grid-item minimum size', () => {
  // A scrollable wrapper alone is not enough: the card around it is a
  // page-stack grid item and would otherwise pass the table's min-content up,
  // scrolling the header, stats and filters sideways too.
  const resets = [
    ['users.html', /\.users-list-card \{[^}]*min-width: 0/],
    ['users.html', /\.users-table-wrap \{ min-width: 0; \}/],
    ['alerts.html', /\.alerts-editor-card \{[^}]*min-width: 0/],
    ['alerts.html', /#alertsList \.cameras-table-wrap,\s*#alertsList \.alerts-policy-table-wrap \{ min-width: 0; \}/],
    ['camera-models.html', /\.camera-models-card \{[^}]*min-width: 0/],
    ['camera-models.html', /\.camera-models-card \.audit-table-wrap \{ min-width: 0; \}/],
    // The cameras card already had this in the shared sheet.
  ];
  for (const [file, pattern] of resets) {
    assert.match(pages[file.replace('.html', '')].html, pattern, `${file}: ${pattern}`);
  }
  assert.match(shared, /\.cameras-list-card \{ min-width: 0; grid-template-columns: minmax\(0, 1fr\); \}/);
  assert.match(pages.cameras.html, /<section class="card cameras-list-card">/);
});

test('the camera-models assignment table is marked up for the card grid', () => {
  // The second card on the page is the one holding the table; the first (the
  // default-model summary) must not be the one that gets the class.
  assert.match(pages['camera-models'].html, /<section class="card camera-models-card">/);
  assert.match(pages['camera-models'].html, /<div class="audit-table-wrap">/);
});

// ── 2. Stacked tables ────────────────────────────────────────────────────────

const stacked = [
  { page: 'users', table: '.users-table', labels: ['Account', 'Role', 'Status', 'Last login', 'Actions'] },
  { page: 'camera-models', table: '#cameraModelsTable', labels: ['Camera', 'Current model', 'Assign model', 'Actions'] },
  { page: 'alerts', table: '.alerts-policy-table', labels: ['Policy', 'Scope', 'Enabled', 'Email', 'Push', 'Actions'] },
];

for (const { page, table, labels } of stacked) {
  test(`${page}: every data cell carries the column name for the stacked layout`, () => {
    const { js } = pages[page];
    const found = [...js.matchAll(/data-label="([^"]+)"/g)].map((m) => m[1]);
    for (const label of labels) {
      assert.ok(found.includes(label), `${page}: no data-label="${label}" (found ${found.join(' | ')})`);
    }
  });

  test(`${page}: the table stacks into cards on a phone`, () => {
    const { html } = pages[page];
    const block = new RegExp(`@media \\(max-width: 700px\\)[\\s\\S]*${table.replace('.', '\\.')}`);
    assert.match(html, block, `${page}: no phone breakpoint for ${table}`);
    // The header row is dropped and the labels are rendered from data-label.
    assert.match(html, /thead \{ display: none; \}/);
    assert.match(html, /::before \{\s*content: attr\(data-label\)/);
    assert.match(html, /tbody \{ display: grid;/);
    // Full-width cells (empty state, inline editors, expanded panels) have no
    // column name, so they opt out of the label/value grid.
    assert.match(html, /td\[colspan\] \{\s*display: block/);
  });
}

test('users: the account cell heads its card and the actions get full-width targets', () => {
  const { html } = pages.users;
  assert.match(html, /td\.user-account-cell \{ grid-column: 1 \/ -1; min-width: 0;/);
  assert.match(html, /td\.user-account-cell::before \{ display: none; \}/);
  assert.match(html, /td\.user-actions-cell \{/);
  assert.match(html, /\.user-actions button \{ flex: 1 1 0; min-height: 40px;/);
  // The last-login cell's desktop min-width/nowrap would fight the stack.
  assert.match(html, /td\.user-last-login \{ min-width: 0; white-space: normal; \}/);
});

test('camera-models: the assign select and its buttons fill the stacked card', () => {
  const { html } = pages['camera-models'];
  assert.match(html, /\.camera-model-select \{ max-width: none; \}/);
  assert.match(html, /\.camera-model-actions \{ display: grid; grid-template-columns: 1fr; \}/);
  assert.match(html, /\.camera-model-actions button \{ min-height: 40px; \}/);
});

test('alerts: the toolbar becomes one column and the toggles keep a label', () => {
  const { html } = pages.alerts;
  assert.match(html, /\.alerts-toolbar \{ display: grid; grid-template-columns: 1fr;/);
  assert.match(html, /\.alerts-toolbar button \{ width: 100%; \}/);
  assert.match(html, /\.alerts-table-actions button \{ width: 40px; height: 40px; \}/);
  assert.match(html, /#alertsList \.alerts-policy-grid \{ grid-template-columns: 1fr; \}/);
});

// ── 3. The trap: an author `display` rule beats the UA `[hidden]` rule ───────

test('alerts: the collapsible settings row stays collapsed on a phone', () => {
  // The stacked layout sets `display` on rows, which outranks the user-agent
  // `[hidden] { display: none }` rule. Without an explicit guard the expanded
  // policy editor would be permanently open.
  const { html, js } = pages.alerts;
  assert.match(html, /tbody tr\[hidden\] \{ display: none; \}/);
  // The card chrome is scoped to visible rows for the same reason.
  assert.match(html, /tbody tr:not\(\[hidden\]\) \{/);
  assert.match(html, /tr\.alerts-policy-details-row:not\(\[hidden\]\)/);
  // And the shared sheet's own guard is still there for wider screens.
  assert.match(shared, /\.alerts-policy-table \.alerts-policy-details-row\[hidden\] \{ display: none; \}/);
  assert.match(js, /class="alerts-policy-details-row" id="alert-policy-settings-\$\{index\}"[^>]*hidden/);
});

test('the cameras scroll hint is only shown when the table really overflows', () => {
  const { html, js } = pages.cameras;
  assert.match(html, /<p class="cameras-scroll-hint" hidden>/);
  assert.match(html, /\.cameras-scroll-hint\[hidden\] \{ display: none; \}/);
  assert.match(js, /hint\.hidden = !wrap \|\| wrap\.scrollWidth <= wrap\.clientWidth \+ 1;/);
  // Re-measured on re-render (ResizeObserver) and on rotate / breakpoint
  // changes, so it never promises a scroll that is not there.
  assert.match(js, /new ResizeObserver\(function\(\) \{ updateScrollHint\(\); \}\)\.observe\(gridEl\)/);
  assert.match(js, /window\.addEventListener\('resize'/);
});

// ── 4. The cameras page keeps its columns (sorting) but sheds the fat ───────

test('cameras: the widest secondary column is dropped on phones, the sort keys stay', () => {
  const { html, js } = pages.cameras;
  // Profiles is the widest cell and is fully visible in the camera's own edit
  // panel; Camera / Connection / Video / Status / PTZ are all sort controls and
  // must survive, otherwise sorting is lost on the smallest screens.
  assert.match(html, /#cameraGrid \.cameras-table th:nth-child\(5\),\s*#cameraGrid \.cameras-table td\.cell-profiles \{ display: none; \}/);
  const sortKeys = [...js.matchAll(/renderCameraSortHeader\('[^']+', '([^']+)'\)/g)].map((m) => m[1]);
  assert.deepEqual(sortKeys, ['camera', 'connection', 'video', 'status', 'ptz']);
});

test('cameras: row actions unstick from the name gutter and become phone-sized', () => {
  const { html } = pages.cameras;
  // The actions were absolutely positioned in the name cell's 88px right
  // gutter, which is what forced the column to be so wide.
  assert.match(html, /\.cell-camera \{ min-width: 160px; padding-right: 10px; \}/);
  assert.match(html, /\.cell-actions \{\s*position: static;\s*transform: none;\s*margin-top: 8px;\s*flex-wrap: wrap;/);
  assert.match(html, /\.cell-actions button \{ min-width: 40px; min-height: 40px; padding: 8px; \}/);
  assert.match(html, /#cameraGrid \.cameras-table \{ min-width: 520px; \}/);
});

test('cameras: the filter row stays one column and Reset fills it', () => {
  const { html } = pages.cameras;
  assert.match(html, /\.cameras-filter-actions \.button-row \{ width: 100%; \}/);
  assert.match(html, /\.cameras-filter-actions \.button-row button \{ width: 100%; justify-content: center; \}/);
});
