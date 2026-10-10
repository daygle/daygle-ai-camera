// Behavioural tests for the Events page Play action while a clip is still
// being written (web/events.js).
//
// /api/events describes the clip each row links to in event.recordings, whose
// media_ready flag is the same one the recordings page disables its Play button
// on. The row must therefore show the recordings page's disabled
// "Preparing..." action instead of a link that lands on an unplayable clip, and
// the feed re-checks on a quiet 3s timer so the action enables itself.
//
// The page script is evaluated in a vm context with the utils.js helpers it
// calls stubbed out (as tests/test_clip_timeline.test.js does), so the branch
// logic under test is events.js's own.
//
// Run with:
//   node --test tests/test_events_recording_ready.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/events.js'), 'utf8');

function loadPage() {
  // A minimal element whose innerHTML writes are recorded, so a test can see
  // exactly what the feed painted (and in what order).
  const feedWrites = [];
  const feed = {};
  Object.defineProperty(feed, 'innerHTML', {
    get: () => feedWrites[feedWrites.length - 1] ?? '',
    set: (value) => { feedWrites.push(value); },
  });

  const state = { page: { items: [] }, pagerUrls: [], renderedRows: [] };
  const timers = [];

  const sandbox = {
    state,
    feed,
    feedWrites,
    timers,
    // utils.js / library_filters.js helpers used by the rows and the loader.
    GENERIC_TRIGGER_LABELS: new Set(['motion', 'alert', 'human', 'object', 'none', 'off', 'continuous']),
    isSoundLabel: () => false,
    escapeHtml: (value) => String(value),
    cameraLabel: (name, id) => name || id || '',
    timeAgo: () => 'just now',
    formatDate: () => '2026-10-10 10:00',
    detectionPill: (label) => `<span class="detection">${label}</span>`,
    motionPill: () => '<span class="detection detection-motion">Motion</span>',
    stillAlertBadge: () => '',
    motionFractionOf: () => null,
    aiTagPills: () => '',
    aiDescriptionTip: () => '',
    libraryDefaultQuery: () => ({}),
    createCursorPager: (url) => {
      state.pagerUrls.push(url);
      return { done: false, loadPage: async () => state.page };
    },
    setLoadMoreSentinel: () => {},
    renderIncrementally: (container, items, renderItem, options) => {
      state.renderedRows = items.map((item) => renderItem(item));
      options?.onComplete?.();
    },
    observeMediaLifecycle: () => {},
    showToast: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    URLSearchParams,
    document: {
      getElementById: (id) => (id === 'eventFeed' ? feed : (id === 'event-feed-rows' ? {} : null)),
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    window: {},
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return sandbox;
}

function renderRow(event, sandbox = loadPage()) {
  sandbox.event = event;
  return vm.runInContext('renderEventRow(event);', sandbox);
}

function setLoadedEvents(sandbox, events) {
  sandbox.events = events;
  vm.runInContext('allEvents = events;', sandbox);
}

const event = (recordingId, mediaReady) => ({
  id: 575,
  recording_id: recordingId,
  created_at: '2026-10-10T10:00:00+00:00',
  source: 'camera',
  metadata: { camera_name: 'Front Door' },
  detections: [{ label: 'person', confidence: 0.91 }],
  has_snapshot: true,
  recordings: recordingId == null ? [] : [{ id: recordingId, media_ready: mediaReady }],
});

test('an event whose clip is still being written offers no playable link', () => {
  const html = renderRow(event(21425, false));
  assert.match(html, /disabled aria-label="Preparing recording"/);
  assert.match(html, />Preparing\.\.\.</);
  assert.doesNotMatch(html, /href="\/recordings\/21425"/);
  // The snapshot action is independent of the clip and stays available.
  assert.match(html, /href="\/api\/events\/575\/snapshot"/);
});

test('a ready clip keeps the ordinary Play link', () => {
  const html = renderRow(event(21425, true));
  assert.match(html, /activity-item-action-play/);
  assert.match(html, /href="\/recordings\/21425"/);
  assert.doesNotMatch(html, /Preparing/);
});

test('a clip missing from the payload is not disabled on a guess', () => {
  // Older payloads / a purged recording row give no readiness signal; the
  // playable-clip default must win over hiding the action.
  const noList = { ...event(21425, true), recordings: [] };
  assert.match(renderRow(noList), /href="\/recordings\/21425"/);
  const noRecordingsKey = { ...event(21425, true) };
  delete noRecordingsKey.recordings;
  assert.match(renderRow(noRecordingsKey), /href="\/recordings\/21425"/);
});

test('an event with no recording gets neither action', () => {
  const html = renderRow({ ...event(null, false), has_snapshot: false, recordings: [] });
  assert.match(html, /<span class="muted">-<\/span>/);
  assert.doesNotMatch(html, /Preparing|activity-item-action-play/);
});

test('the feed re-checks on a quiet 3s timer only while a clip is preparing', async () => {
  const sandbox = loadPage();
  sandbox.state.page = { items: [event(21425, false)] };
  setLoadedEvents(sandbox, [event(21425, false)]);
  vm.runInContext('scheduleEventRefresh();', sandbox);
  assert.equal(sandbox.timers.length, 1, 'a preparing clip arms the timer');
  assert.equal(sandbox.timers[0].ms, 3000);
  assert.equal(vm.runInContext('eventsRefreshTimer !== null', sandbox), true);

  // The timer's callback is the quiet re-check: it refreshes the rows without
  // blanking them first.
  await sandbox.timers[0].fn();
  assert.ok(sandbox.state.pagerUrls.length, 'the re-check re-queries /api/events');
  assert.ok(
    !sandbox.feedWrites.some((html) => String(html).includes('Loading events')),
    'the timer must re-check quietly',
  );

  // Once every loaded clip is ready the timer disarms. (The quiet reload above
  // re-armed one, because the page it fetched was still preparing.)
  const armed = sandbox.timers.length;
  setLoadedEvents(sandbox, [event(21425, true)]);
  vm.runInContext('scheduleEventRefresh();', sandbox);
  assert.equal(sandbox.timers.length, armed, 'a ready-only list schedules nothing');
  assert.equal(vm.runInContext('eventsRefreshTimer', sandbox), null);
});

test('a quiet reload repaints the rows without the loading placeholder', async () => {
  const sandbox = loadPage();
  sandbox.state.page = { items: [event(21425, false)] };
  await vm.runInContext('loadEvents({ quiet: true });', sandbox);
  const quietWrites = sandbox.feedWrites;
  assert.ok(quietWrites.length, 'the quiet reload still painted');
  assert.ok(
    !quietWrites.some((html) => String(html).includes('Loading events')),
    'a background re-check must not blank the list',
  );
  assert.match(String(quietWrites[quietWrites.length - 1]), /<table class="rule-table activity-table"/);
  assert.ok(
    sandbox.state.renderedRows.some((html) => html.includes('Preparing...')),
    'the re-check painted the (still preparing) row',
  );

  // The user-driven load still shows its placeholder.
  const sandbox2 = loadPage();
  sandbox2.state.page = { items: [event(21425, true)] };
  await vm.runInContext('loadEvents();', sandbox2);
  assert.match(String(sandbox2.feedWrites[0]), /Loading events/);
  assert.ok(sandbox2.state.renderedRows.some((html) => html.includes('href="/recordings/21425"')));
});

test('the events script reads readiness from the payload instead of refetching a clip', () => {
  assert.match(source, /recording\.media_ready === false/);
  assert.match(source, /EVENTS_PREPARING_REFRESH_MS = 3000/);
});
