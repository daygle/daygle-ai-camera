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
  // Unknown or future event types fall back to the raw code, not '-'. And the
  // tooltip carries the raw machine code (including empty → undefined).
  assert.ok(/EVENT_TYPE_LABELS\[key\] \|\| key \|\| '-';/.test(js));
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
