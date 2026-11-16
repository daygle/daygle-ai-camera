// The Camera Log page (web/camera-log.js + web/camera-log.html).
//
// Structural checks: the Event column reads as words (matching the filter
// dropdown, with the raw machine code kept as a tooltip), severity info is
// rendered neutrally so warning/error rows stand out on the small-screen
// stacked cards, and the page copy mentions the PTZ auto-track events that
// now land on this page.
//
// Run with:
//   node --test tests/test_camera_log_page.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = readFileSync(path.resolve(here, '../web/camera-log.js'), 'utf8');
const html = readFileSync(path.resolve(here, '../web/camera-log.html'), 'utf8');

test('every filterable event type reads as words, with the raw code as tooltip', () => {
  // Scope the extraction to the event-type select - the severity filter's
  // <option value="info">-style values are severities, not event types.
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
  // Unknown or future event types fall back to the code in Title Case, not '-'. And the
  // tooltip carries the raw machine code (including empty → undefined).
  assert.ok(/EVENT_TYPE_LABELS\[key\] \|\| logLabel\(key\) \|\| '-';/.test(js));
  assert.ok(/title: entry\.event_type \|\| undefined/.test(js));
});

test('info severity renders neutral so warning/error rows stand out', () => {
  assert.ok(/if \(severity === 'error'\) return 'status-failed';/.test(js));
  assert.ok(/if \(severity === 'warning'\) return 'status-pending';/.test(js));
  // The success (green) palette must not be used for the diagnostic stream;
  // info rows are activity, not achievements.
  assert.ok(!/return 'status-success';/.test(js));
});

test('the page copy names the PTZ auto-track events that land here', () => {
  // The hero paragraph is the page's promise of what it covers; it gains
  // auto-track alongside the capture/detection diagnostics.
  assert.ok(/PTZ auto-track following/.test(html));
  // The five PTZ event types must exist in the dropdown so they are filterable.
  for (const value of ['ptz_track_acquired', 'ptz_track_switched', 'ptz_track_lost', 'ptz_move_failed', 'ptz_no_home_preset']) {
    assert.ok(new RegExp(`<option value="${value}">`).test(html), `${value} missing from the filter`);
  }
});

test('free-text search reaches the API server-side', () => {
  // Operators search records by PTZ target ids, preset names or timestamps;
  // the Camera Log is a paged API, so the search box is a `q` query param
  // (not client-side filtering like the streaming Application Log page).
  assert.match(html, /id="filterSearch" type="search"/);
  assert.match(html, /aria-label="Search messages and details"/);
  assert.match(js, /q: document\.getElementById\('filterSearch'\)\.value\.trim\(\)/);
  assert.match(js, /params\.set\('q', f\.q\)/);
  assert.match(js, /filterSearch'\)\.value = ''/);
  assert.match(js, /filterSearch[\s\S]{0,200}Enter/);
});

const utils = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');

function loadLogLabel() {
  const start = utils.indexOf('const LOG_LABEL_ACRONYMS');
  const end = utils.indexOf('\n}\n', utils.indexOf('function logLabel(')) + 3;
  return new Function(`${utils.slice(start, end)}; return logLabel;`)();
}

test('log labels are Title Case with acronyms kept', () => {
  const logLabel = loadLogLabel();
  assert.equal(logLabel('stderr_tail'), 'Stderr Tail');
  assert.equal(logLabel('gpu_fallback'), 'GPU Fallback');
  assert.equal(logLabel('camera_id'), 'Camera ID');
  assert.equal(logLabel('sha256'), 'SHA-256');
  assert.equal(logLabel('update_check'), 'Update Check');
  assert.equal(logLabel(''), '');
});

for (const page of ['camera-log', 'ai-log']) {
  const pageJs = readFileSync(path.resolve(here, `../web/${page}.js`), 'utf8');
  const pageHtml = readFileSync(path.resolve(here, `../web/${page}.html`), 'utf8');

  test(`${page}: cameras are picked by name from a dropdown`, () => {
    assert.match(pageHtml, /<select id="filterCamera" aria-label="Camera">\s*<option value="">All cameras<\/option>/);
    assert.doesNotMatch(pageHtml, /Filter by camera id/);
    assert.match(pageJs, /api\('\/api\/cameras'\)/);
    assert.match(pageJs, /option\.value = camera\.id;/);
    assert.match(pageJs, /option\.textContent = camera\.name;/);
    assert.match(pageJs, /filterCamera'\)\.addEventListener\('change'/);
  });

  test(`${page}: labels are Title Case full words`, () => {
    // Filter buttons match the Application Log, away from the destructive Clear Log.
    assert.match(pageHtml, />Apply Filters<\/button>/);
    assert.match(pageHtml, />Reset Filters<\/button>/);
    // Details keys are labelled, values kept as-is.
    assert.match(pageJs, /\$\{logLabel\(k\)\}: /);
    const labels = pageJs.match(/const EVENT_TYPE_LABELS = \{([\s\S]*?)\n\};/)[1];
    for (const [, label] of labels.matchAll(/: '([^']+)'/g)) {
      for (const word of label.split(/[\s-]+/)) {
        assert.match(word, /^[A-Z0-9]/, `"${label}" is not Title Case`);
      }
      assert.doesNotMatch(label, /&|\bVerify\b/, `"${label}" uses an abbreviation`);
    }
    const select = pageHtml.match(/<select id="filterEventType"[\s\S]*?<\/select>/)[0];
    assert.doesNotMatch(select, /&/);
  });
}
