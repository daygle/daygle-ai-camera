// The AI Log page (web/ai-log.js + web/ai-log.html).
//
// Structural mirror of tests/test_camera_log_page.test.js: every event type
// in the filter dropdown has a friendly label, the Event column falls back
// to the raw code with it as the tooltip, and the page copy names what this
// stream covers (verification, descriptions/tag checks, relabels, review).
//
// Run with:
//   node --test tests/test_ai_log_page.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = readFileSync(path.resolve(here, '../web/ai-log.js'), 'utf8');
const html = readFileSync(path.resolve(here, '../web/ai-log.html'), 'utf8');

test('every filterable AI event type reads as words, with the raw code as tooltip', () => {
  const eventTypeSelect = html.match(/<select id="filterEventType"[\s\S]*?<\/select>/);
  assert.ok(eventTypeSelect, 'the event-type filter select exists');
  const optionValues = [...eventTypeSelect[0].matchAll(/<option value="([^"]*)">/g)]
    .map((match) => match[1])
    .filter((value) => value);
  assert.ok(optionValues.length > 0, 'the page has no event-type filter options');
  for (const value of optionValues) {
    assert.ok(
      new RegExp(`^  ${value}: '`, 'm').test(js),
      `${value} has no friendly label in EVENT_TYPE_LABELS`,
    );
  }
  assert.ok(/EVENT_TYPE_LABELS\[key\] \|\| key \|\| '-';/.test(js));
  assert.ok(/title: entry\.event_type \|\| undefined/.test(js));
});

test('severity info is neutral so warning and error rows stand out', () => {
  assert.match(js, /severity==='error'\)return'status-failed';|severity === 'error'\) return 'status-failed';/);
  assert.match(js, /severity==='warning'\)return'status-pending';|severity === 'warning'\) return 'status-pending';/);
});

test('engineering events show their step as a badge, not duplicated in Details', () => {
  // Model Install / Face Recognition / YAMNet TFLite records carry a `step`
  // (download, integrity, export, registry, delete, matcher_refresh,
  // scaling_sync, reload, rollback, update_check...). The Event cell must
  // render it as a human-readable badge and the Details cell must not
  // repeat it.
  assert.match(js, /function stepLabel\(/);
  assert.match(js, /k !== 'step'/);
  assert.match(js, /\.replace\(\/_\/g, ' '\)/);
  assert.match(js, /stepBadge\.className = 'step-badge'/);
});

const styles = readFileSync(path.resolve(here, '../web/styles.css'), 'utf8');
test('the step badge has styles in the shared stylesheet', () => {
  assert.match(styles, /\.step-badge \{/);
  assert.match(styles, /:root\.light \.step-badge \{/);
});

test('the page copy names the AI activity the log covers', () => {
  assert.match(html, /verification verdicts|verification/);
  assert.match(html, /AI Log/);
});

test('the page queries the dedicated AI log API', () => {
  assert.match(js, /\/api\/ai-log/);
  assert.doesNotMatch(js, /\/api\/camera-log/);
});

test('free-text search reaches the API server-side', () => {
  // Operators search records by model file name or sha256; the AI Log is a
  // paged API, so the search box is a `q` query param (not client-side
  // filtering like the streaming Application Log page).
  assert.match(html, /id="filterSearch" type="search"/);
  assert.match(html, /aria-label="Search messages and details"/);
  assert.match(js, /q: document\.getElementById\('filterSearch'\)\.value\.trim\(\)/);
  assert.match(js, /params\.set\('q', f\.q\)/);
  assert.match(js, /filterSearch'\)\.value = ''/);
  assert.match(js, /filterSearch[\s\S]{0,200}Enter/);
});

test('the page is reachable from the System section tabs', () => {
  const nav = readFileSync(path.resolve(here, '../web/nav.js'), 'utf8');
  assert.ok(nav.includes("href: '/ai-log'"), '/ai-log is not reachable from the navigation');
  assert.ok(nav.includes("'/ai-log',"), '/ai-log is not admin-menu covered');
});
