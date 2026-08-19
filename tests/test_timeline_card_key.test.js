// Regression test for the timeline per-card colour key.
//
// /timeline renders four parallel cards (Objects + Motion + Sounds + Continuous). Each card
// shows its own colour-key strip via renderCardKey(), which consumes
// partitionRecordingsForKeys() - the single source of truth for the
// per-kind chip partition. The interesting regression guard is the
// Dog Bark-on-object-source fix: a sound-class label (Dog Bark, Car
// Alarm, ...) routes to the Sounds card exclusively, never duplicated
// as an object chip on the Objects card. Real object labels (Person,
// Dog, ...) stay on the Objects card. A mixed clip (Person + Dog Bark)
// contributes one chip per card.
//
// timeline.js reaches for the DOM at load (initTimelineCards()), so we
// load it into a vm context behind lightweight stubs that resolve
// [data-timeline-card] and [data-timeline-card-key] into stub card
// stubs whose .innerHTML we can inspect after each render.
//
// utils.js exposes DETECTION_EYE_ICON + the sound emoji as
// `const`-declared top-level bindings, which are script-private and not
// surfaced on the context's global object - so the test reads those
// icon strings indirectly by calling partitionRecordingsForKeys() and
// inspecting the icon field on the chips it returns. This way the
// test automatically tracks any future rebase of the eye SVG path
// or the sound emoji without magic literals.
//
// Run with:
//   node --test tests/test_timeline_card_key.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');
const timelineSource = readFileSync(path.resolve(here, '../web/timeline.js'), 'utf8');

function makeKeyStub() {
  const stub = {
    innerHTML: '',
    attrs: {},
    setAttribute(k, v) { stub.attrs[k] = v; },
    removeAttribute(k) { delete stub.attrs[k]; },
    getAttribute(k) { return (k in stub.attrs) ? stub.attrs[k] : null; },
  };
  return stub;
}

function makeCardStub() {
  return {
    querySelector(selector) {
      if (typeof selector === 'string' && selector.includes('data-timeline-card-key')) {
        return makeKeyStub();
      }
      return null;
    },
  };
}

const documentStub = {
  getElementById() {
    return {
      innerHTML: '', textContent: '', value: '', checked: false,
      dataset: {}, style: {}, classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
      addEventListener() {}, removeEventListener() {}, appendChild() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
    };
  },
  querySelector(selector) {
    if (typeof selector === 'string') {
      const m = /data-timeline-card="(\w+)"/.exec(selector);
      if (m) return makeCardStub();
    }
    return null;
  },
  querySelectorAll() { return []; },
  createElement() { return makeKeyStub(); },
  addEventListener() {}, removeEventListener() {},
};

const sandbox = {
  window: {
    addEventListener() {}, removeEventListener() {},
    location: { search: '' },
    daygleAuthReady: Promise.resolve(),
  },
  document: documentStub,
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  BroadcastChannel: undefined,
  URLSearchParams,
  console,
};
sandbox.window.daygleUi = null;
vm.createContext(sandbox);

vm.runInContext(utilsSource, sandbox);
vm.runInContext(timelineSource, sandbox);

function partitionFor(recordings) {
  sandbox.__recordings = recordings;
  vm.runInContext('__result = partitionRecordingsForKeys(__recordings);', sandbox);
  return sandbox.__result;
}

function cardKeyHtmlFor(kind, recordings) {
  sandbox.__kind = kind;
  sandbox.__recordings = recordings;
  // Wrap the body in an IIFE so the inner `const card` lives in its
  // own function scope per call. Without this, Node's vm reuses the
  // same script lexical scoping across calls with identical source and
  // the second `const card = ...` throws
  // "Identifier 'card' has already been declared".
  vm.runInContext(`
    (() => {
      const card = TIMELINE_CARDS.find((c) => c.kind === __kind);
      if (!card) { __html = ''; __hidden = ''; }
      else {
        card.key = card.root.querySelector('[data-timeline-card-key]');
        renderCardKey(card, __recordings);
        __html = card.key.innerHTML;
        __hidden = card.key.getAttribute('aria-hidden') || '';
      }
    })();
  `, sandbox);
  return { html: sandbox.__html, ariaHidden: sandbox.__hidden };
}

const soundRecording = (classLabel, confidence = 0.8) => ({
  event: { metadata: { source: 'sound-detection', class_label: classLabel, confidence } },
});
const objectRecordingWithLabel = (label, confidence = 0.6) => ({
  trigger_type: 'object',
  labels: [label],
  detections: [{ label, confidence }],
});

// Capture the canonical icon strings directly from partition output so the
// test tracks any future utils.js rebase without magic literals.
const personIcons = partitionFor([objectRecordingWithLabel('person')]);
const dogIcons = partitionFor([objectRecordingWithLabel('dog bark')]);
const OBJECT_CHIP_ICON = personIcons.objectChips[0]?.icon || '';
const SOUND_CHIP_ICON = dogIcons.soundChips[0]?.icon || '';
const MOTION_CHIP_ICON = partitionFor([{
  trigger_type: 'motion',
  labels: ['motion'],
  detections: [{ label: 'motion', confidence: 0.42 }],
}]).motionChips[0]?.icon || '';
assert.ok(OBJECT_CHIP_ICON.length > 0,
  'partition should produce a non-empty icon string for object chips');
assert.ok(SOUND_CHIP_ICON.length > 0,
  'partition should produce a non-empty icon string for sound chips');

test('card key: motion-only recordings route exclusively to the Motion card', () => {
  const motionRecording = {
    trigger_type: 'motion',
    labels: ['motion'],
    detections: [{ label: 'motion', confidence: 0.42 }],
  };
  const objectCard = cardKeyHtmlFor('object', [motionRecording]);
  const motionCard = cardKeyHtmlFor('motion', [motionRecording]);
  const soundCard = cardKeyHtmlFor('sound', [motionRecording]);
  assert.equal(objectCard.html, '', 'Objects card must not contain motion-only clips');
  assert.equal(soundCard.html, '', 'Sounds card must not contain motion-only clips');
  assert.equal(motionCard.html.split('Motion').length - 1, 1, 'Motion card shows one Motion chip');
  assert.ok(motionCard.html.includes(MOTION_CHIP_ICON), 'Motion card uses the motion icon');
});

test('card key: continuous recordings route exclusively to the Continuous card', () => {
  // Always-on capture chunks carry no concrete label. They must land on the
  // Continuous card only - never padding out the Objects card (which used to
  // catch every non-sound, non-motion clip).
  const continuousRecording = { trigger_type: 'continuous', labels: [], detections: [] };
  const objectCard = cardKeyHtmlFor('object', [continuousRecording]);
  const motionCard = cardKeyHtmlFor('motion', [continuousRecording]);
  const soundCard = cardKeyHtmlFor('sound', [continuousRecording]);
  const continuousCard = cardKeyHtmlFor('continuous', [continuousRecording]);
  assert.equal(objectCard.html, '', 'Objects card must not contain continuous clips');
  assert.equal(motionCard.html, '', 'Motion card must not contain continuous clips');
  assert.equal(soundCard.html, '', 'Sounds card must not contain continuous clips');
  assert.equal(continuousCard.html.split('Continuous').length - 1, 1, 'Continuous card shows one Continuous chip');
});

test('card key: a continuous chunk that caught an object stays on the Continuous card', () => {
  // An always-on chunk remains a continuous recording even when it
  // recognised an object during the hour; it must not be re-routed to the
  // Objects card.
  const rec = { trigger_type: 'continuous', labels: ['person'], detections: [{ label: 'person', confidence: 0.7 }] };
  const objectCard = cardKeyHtmlFor('object', [rec]);
  const continuousCard = cardKeyHtmlFor('continuous', [rec]);
  assert.equal(objectCard.html, '', 'object-carrying continuous chunk must not show on Objects card');
  assert.equal(continuousCard.html.split('Continuous').length - 1, 1, 'Continuous card shows one Continuous chip');
});

test('card key: sound-class label on an object recording routes exclusively to the Sounds card', () => {
  // The Dog-Bark-on-object-source regression guard: a recording whose
  // only label is a sound-class name should never produce an Objects
  // card chip.
  const objectCard = cardKeyHtmlFor('object', [objectRecordingWithLabel('dog bark')]);
  assert.equal(objectCard.html, '', 'Objects card must stay empty for sound-class labels on object recordings');
  assert.equal(objectCard.ariaHidden, 'true', 'Objects card key is aria-hidden when empty');

  const soundCard = cardKeyHtmlFor('sound', [objectRecordingWithLabel('dog bark')]);
  assert.equal(soundCard.html.split('Dog Bark').length - 1, 1, 'Sounds card shows one Dog Bark chip');
  assert.ok(soundCard.html.includes(SOUND_CHIP_ICON), 'Sounds card chip carries the sound icon');
  assert.ok(!soundCard.html.includes(OBJECT_CHIP_ICON), 'no eye-iconed chip on Sounds card for a sound class');
});

test('card key: sound + object sources of the same sound class collapse to one Sounds-card chip', () => {
  const soundCard = cardKeyHtmlFor('sound', [
    soundRecording('Dog Bark'),
    objectRecordingWithLabel('dog bark'),
  ]);
  assert.equal(soundCard.html.split('Dog Bark').length - 1, 1, 'one chip dedupes the two sources');
  assert.ok(soundCard.html.includes(SOUND_CHIP_ICON), 'sound icon preserved on the surviving chip');

  const objectCard = cardKeyHtmlFor('object', [
    soundRecording('Dog Bark'),
    objectRecordingWithLabel('dog bark'),
  ]);
  assert.equal(objectCard.html, '', 'Objects card still empty - Dog Bark lives on Sounds card only');
});

test('card key: real object labels render exclusively on the Objects card with the eye icon', () => {
  const objectCard = cardKeyHtmlFor('object', [objectRecordingWithLabel('person')]);
  assert.ok(objectCard.html.includes('Person'), 'Person chip present');
  assert.ok(objectCard.html.includes(OBJECT_CHIP_ICON), 'object chip carries the eye icon');
  assert.ok(!objectCard.html.includes(SOUND_CHIP_ICON), 'no sound-styled chip for a real object label');

  const soundCard = cardKeyHtmlFor('sound', [objectRecordingWithLabel('person')]);
  assert.equal(soundCard.html, '', 'Sounds card stays empty for object-only labels');
});

test('card key: mixed clip splits - Person on Objects, Dog Bark on Sounds', () => {
  // One object recording carrying both Person and a sound-class label:
  // the chips fan out per kind so each card only shows its own.
  const recording = {
    trigger_type: 'object',
    labels: ['person', 'dog bark'],
    detections: [
      { label: 'person', confidence: 0.9 },
      { label: 'dog bark', confidence: 0.5 },
    ],
  };

  const objectCard = cardKeyHtmlFor('object', [recording]);
  assert.equal(objectCard.html.split('Person').length - 1, 1, 'Objects card: Person once');
  assert.equal(objectCard.html.split('Dog Bark').length - 1, 0, 'Objects card: no Dog Bark chip');
  assert.ok(objectCard.html.includes(OBJECT_CHIP_ICON), 'Objects card: Person keeps the eye icon');

  const soundCard = cardKeyHtmlFor('sound', [recording]);
  assert.equal(soundCard.html.split('Dog Bark').length - 1, 1, 'Sounds card: Dog Bark once');
  assert.equal(soundCard.html.split('Person').length - 1, 0, 'Sounds card: no Person chip');
  assert.ok(soundCard.html.includes(SOUND_CHIP_ICON), 'Sounds card: Dog Bark keeps the sound icon');
  assert.ok(!soundCard.html.includes(OBJECT_CHIP_ICON), 'Sounds card: no eye-icon chip leaked across');
});

test('card key: empty state marks the landmark aria-hidden so SRs skip it', () => {
  const { html, ariaHidden } = cardKeyHtmlFor('object', []);
  assert.equal(html, '', 'empty card key writes no innerHTML');
  assert.equal(ariaHidden, 'true', 'empty card key flips aria-hidden=true');
});

test('card key: repainted populated state removes the aria-hidden attribute', () => {
  cardKeyHtmlFor('object', []);
  const { ariaHidden } = cardKeyHtmlFor('object', [objectRecordingWithLabel('person')]);
  assert.equal(ariaHidden, '', 'repainted with chips - aria-hidden is removed');
});


test('partition: distinct sound classes produce per-class swatch colours (no flat reserved purple)', () => {
  // Sound segments now derive hue per class instead of every clip
  // flattening onto '#a855f7'. Pins both that no reserved token leaks
  // through for a labelled sound *and* that distinct classes don't all
  // collapse to the same colour.
  const classes = ['Dog Bark', 'Car Alarm', 'Siren'];
  const colors = classes.map((cls) =>
    partitionFor([soundRecording(cls)]).soundChips[0]?.color || ''
  );
  for (let i = 0; i < classes.length; i += 1) {
    assert.ok(colors[i] && /^#[0-9a-f]{3,8}$/i.test(colors[i]),
      `${classes[i]} chip must expose a hash-derived hex colour, got "${colors[i]}"`);
    assert.notEqual(colors[i], '#a855f7',
      `${classes[i]} must not fall back to the reserved purple token once it has a class label`);
  }
  // At least one pair ticks a distinct hue - catches the regression
  // where every sound class collapses back to one flat colour.
  const distinct = new Set(colors);
  assert.ok(distinct.size > 1,
    `expected at least two of three sound classes to hash to different hues - got [${colors.join(', ')}]`);
});

test('partition: sound + object labels sharing a root word still hash to different colours', () => {
  // Regression for the snd: salt: a 'dog' object detection and a 'dog bark'
  // sound-class label share the 'dog' root word but must land on distinct
  // SEGMENT_COLORS index so the chips don't visually coincide.
  const objectDog = partitionFor([objectRecordingWithLabel('dog')]).objectChips[0]?.color;
  const soundDogBark = partitionFor([soundRecording('Dog Bark')]).soundChips[0]?.color;
  assert.ok(objectDog, 'object Dog chip has a colour');
  assert.ok(soundDogBark, 'sound Dog Bark chip has a colour');
  assert.notEqual(objectDog, soundDogBark,
    `'dog' object and 'dog bark' sound must not hash to the same colour`);
});


test('partition: six sound classes hash to mostly distinct hues', () => {
  // The collision guard for the user's request: across 6 distinct sound
  // classes, at least 5 must hash to different SEGMENT_COLORS indexes.
  // A regression that pushed sound colours back to one flat hue would
  // fail this instantly. The 10-colour palette still has some room
  // for accidental collisions, so 5/6 leaves slack for one unlucky
  // hash pair without losing the assertion's bite.
  const classes = ['Dog Bark', 'Car Alarm', 'Siren', 'Speech', 'Glass Break', 'Door Bell'];
  const colors = classes.map((cls) =>
    partitionFor([soundRecording(cls)]).soundChips[0]?.color || ''
  );
  for (let i = 0; i < classes.length; i += 1) {
    assert.ok(colors[i] && /^#[0-9a-f]{3,8}$/i.test(colors[i]),
      `${classes[i]} chip must expose a hash-derived hex colour, got "${colors[i]}"`);
    assert.notEqual(colors[i], '#a855f7',
      `${classes[i]} must not fall back to the reserved purple token once it has a class label`);
  }
  const distinct = new Set(colors);
  assert.ok(distinct.size >= 5,
    `expected at least 5 of 6 sound classes to hash to distinct hues - got ${distinct.size} (${[...distinct].join(', ')})`);
});
