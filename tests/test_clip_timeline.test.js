// Behavioural tests for the shared clip segment timeline (web/clip_timeline.js),
// rendered by the recordings and timeline playback pages.
//
// The script reads the page's `els` handles and `activeRecording`, so it runs
// in a vm context with minimal DOM stubs and the legend/bar contents are
// inspected after each render.
//
// Run with:
//   node --test tests/test_clip_timeline.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/clip_timeline.js'), 'utf8');

function makeElement() {
  const element = {
    children: [],
    style: {},
    attrs: {},
    className: '',
    title: '',
    id: '',
    text: '',
    hidden: true,
    appendChild(child) { element.children.push(child); return child; },
    setAttribute(k, v) { element.attrs[k] = v; },
  };
  Object.defineProperty(element, 'innerHTML', {
    get: () => '',
    set: () => { element.children = []; },
  });
  return element;
}

function render(recording, { duration = 60 } = {}) {
  const els = {
    clipTimeline: makeElement(),
    clipTimelineBar: makeElement(),
    clipTimelineLegend: makeElement(),
    clipPlayer: { duration, currentTime: 0 },
  };
  const sandbox = {
    els,
    activeRecording: recording,
    // utils.js provides titleCase on the real pages.
    titleCase: (value) => String(value || '').replace(/\b\w/g, (c) => c.toUpperCase()),
    document: {
      createElement: () => makeElement(),
      createTextNode: (text) => ({ text }),
      getElementById: () => null,
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  vm.runInContext('renderClipTimeline();', sandbox);
  const legend = els.clipTimelineLegend.children.map((item) => ({
    text: item.children.map((child) => child.text || '').join(''),
    title: item.title,
  }));
  return { els, legend, bar: els.clipTimelineBar.children };
}

const sample = (t) => ({ t, detections: [{ label: 'person', confidence: 0.9 }] });

test('a single detection is reported as such, never as an invented 1.0s span', () => {
  const { legend, bar } = render({ track: [sample(12)] });
  const texts = legend.map((item) => item.text);
  assert.ok(texts.includes('Event: single detection'), texts.join(' | '));
  assert.ok(!texts.some((text) => /^Event \d/.test(text)), texts.join(' | '));
  // The tail runs from the detection itself, not from an inflated span end.
  assert.ok(texts.includes('Tail 48s'), texts.join(' | '));
  // Still visible on the bar: a hairline at the detection.
  const hairline = bar.find((el) => el.className.includes('clip-seg-event-single'));
  assert.ok(hairline, 'single detection hairline is drawn');
  assert.equal(hairline.style.left, '20%');
  assert.equal(hairline.title, 'Single Detection at 12s');
});

test('a real span is reported with its measured duration', () => {
  const { legend } = render({ track: [sample(10), sample(10.4), sample(10.8)] });
  const texts = legend.map((item) => item.text);
  assert.deepEqual(texts.slice(0, 3), ['Pre-roll 10s', 'Event 0.8s', 'Tail 49s']);
});

test('legend items say what they measure and which setting governs it', () => {
  const { legend } = render({ track: [sample(10), sample(20)] });
  const titles = Object.fromEntries(legend.map((item) => [item.text.split(' ')[0], item.title]));
  assert.match(titles['Pre-roll'], /first detection/);
  assert.match(titles['Pre-roll'], /Pre-Event setting covers clip start to the trigger/);
  assert.match(titles.Event, /first to last detection/);
  assert.match(titles.Tail, /last detection to clip end/);
  assert.match(titles.Tail, /Post-event setting runs from the trigger/);
});

test('the trigger is marked where it actually fired, apart from the first detection', () => {
  // Trigger 10s into the footage; the first object detection 16s in (the
  // "Pre-roll reads 16s with Pre-Event 10s" case).
  const { legend, bar } = render({
    started_at: '2026-10-04T10:00:00+00:00',
    event: { created_at: '2026-10-04T10:00:10+00:00' },
    track: [sample(16), sample(30)],
  });
  const texts = legend.map((item) => item.text);
  assert.ok(texts.includes('Pre-roll 16s'), texts.join(' | '));
  assert.ok(texts.includes('Trigger 10s'), texts.join(' | '));
  const trigger = bar.find((el) => el.className === 'clip-event-trigger');
  assert.ok(trigger, 'trigger marker is drawn');
  assert.equal(trigger.style.left, `${(10 / 60) * 100}%`);
  const firstDetection = bar.find((el) => el.className === 'clip-trigger-marker');
  assert.equal(firstDetection.title, 'First detection at 16s');
});

test('no trigger marker when the trigger cannot be placed on the clip', () => {
  for (const recording of [
    { track: [sample(5)] },
    { started_at: '2026-10-04T10:00:00+00:00', event: { created_at: '2026-10-04T09:59:00+00:00' }, track: [sample(5)] },
    { started_at: 'not-a-date', event: { created_at: '2026-10-04T10:00:05+00:00' }, track: [sample(5)] },
  ]) {
    const { legend, bar } = render(recording);
    assert.ok(!legend.some((item) => item.text.startsWith('Trigger')));
    assert.ok(!bar.some((el) => el.className === 'clip-event-trigger'));
  }
});

test('clips without a localized detection get no timeline', () => {
  const { els } = render({ track: [{ t: 3, detections: [] }] });
  assert.equal(els.clipTimeline.hidden, true);
  assert.equal(els.clipTimelineLegend.children.length, 0);
});

// Extension markers: recording.extensions (app/recording_extension.py) holds
// runs of what kept the clip going and where it would originally have ended.
const iso = (base, seconds) => new Date(Date.parse(base) + seconds * 1000).toISOString();
const BASE = '2026-10-05T10:00:00.000Z';

function extendedRecording(runs, originalEnd) {
  return {
    started_at: BASE,
    track: [sample(10), sample(14)],
    extensions: {
      original_end: originalEnd === null ? null : iso(BASE, originalEnd),
      runs: runs.map(([start, end, reason, label]) => ({ start: iso(BASE, start), end: iso(BASE, end), reason, label })),
    },
  };
}

test('each extension run is marked on the bar and in the legend', () => {
  const { bar, legend } = render(extendedRecording([
    [11, 14, 'object', 'car'],
    [20, 28, 'still', 'car'],
    [30, 42, 'motion', 'motion'],
  ], 25));
  const strips = bar.filter((el) => el.className.startsWith('clip-extension'));
  assert.deepEqual(strips.map((el) => el.className), [
    'clip-extension clip-extension-object',
    'clip-extension clip-extension-still',
    'clip-extension clip-extension-motion',
  ]);
  assert.equal(strips[2].style.left, '50%');
  assert.equal(strips[2].style.width, '20%');
  assert.equal(strips[0].title, 'Car kept recording 11s–14s');
  assert.equal(strips[1].title, 'Car kept recording while still 20s–28s');
  assert.equal(strips[2].title, 'Motion kept recording 30s–42s');
  const original = bar.find((el) => el.className === 'clip-original-end');
  assert.equal(original.style.left, `${(25 / 60) * 100}%`);
  const texts = legend.map((item) => item.text);
  assert.ok(texts.includes('Extended ×3'), texts.join(' | '));
  assert.ok(texts.includes('Original end 25s'), texts.join(' | '));
});

test('no original-end line when the clip did not run past it', () => {
  const { bar, legend } = render(extendedRecording([[11, 14, 'object', 'car']], 60));
  assert.ok(!bar.some((el) => el.className === 'clip-original-end'));
  assert.ok(!legend.some((item) => item.text.startsWith('Original end')));
});

test('clips without extensions draw no markers', () => {
  const { bar, legend } = render({ started_at: BASE, track: [sample(10), sample(14)] });
  assert.ok(!bar.some((el) => el.className.startsWith('clip-extension') || el.className === 'clip-original-end'));
  assert.ok(!legend.some((item) => item.text.startsWith('Extended')));
});
