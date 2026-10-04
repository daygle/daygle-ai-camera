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
  assert.equal(hairline.title, 'Single detection at 12s');
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
