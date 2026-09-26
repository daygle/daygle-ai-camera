// Tests for the incremental list renderer and media lifecycle helpers added to
// web/utils.js (Item 15).
//
// The behaviours that matter:
//   1. The first screenful is painted SYNCHRONOUSLY, so a long list still
//      looks instant - that is the whole reason for progressive painting.
//   2. The container is emptied exactly once, not per batch: emptying it per
//      batch would destroy rows already painted.
//   3. A newer render cancels its predecessor, so two passes cannot interleave
//      and duplicate rows into the same container.
//   4. Offscreen video pauses and resumes only if it was playing; offscreen
//      images release their decoded bitmap and get it back with no refetch.
//   5. Rows land DIRECTLY in the container - no wrapper element. The old
//      wrapper defaulted to <tbody>, which nested a <tbody> inside the table
//      pages' existing <tbody id="...-rows">: invalid markup the browser
//      repairs with anonymous table boxes (deformed columns) and re-lays out
//      on every batch (slow pages).
//
// Rendered against a minimal DOM stub in a vm sandbox (the same pattern as
// tests/test_request_coalescing.test.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web', 'utils.js'), 'utf8');

// A DOM stub just large enough for these helpers: elements with children,
// innerHTML that clears on assignment, and a document that can create elements
// and fragments. Frames are queued rather than run so a test can step them.
function loadUtils({ withObserver = true } = {}) {
  const frames = [];
  const created = [];

  function makeElement(tag) {
    const isFragment = String(tag).startsWith('#fragment');
    const el = {
      tagName: String(tag).toUpperCase(),
      isFragment,
      children: [],
      className: '',
      dataset: {},
      attributes: {},
      _innerHTML: '',
      parentNode: null,
      get firstChild() { return this.children[0] || null; },
      // A <template>'s content is the fragment rows are parsed into; aliasing
      // it to the element itself is enough for the drain loop's semantics.
      get content() { return this; },
      get innerHTML() { return this._innerHTML; },
      set innerHTML(value) {
        // Assigning innerHTML replaces every child. This stub does not parse
        // HTML, so it models the result as a single node carrying the markup -
        // enough to give firstChild something real for the move-out loop.
        this._innerHTML = value;
        this.children = [];
        if (value !== '' && value != null) {
          this.children.push({ _html: value, isFragment: false, parentNode: this });
        }
      },
      appendChild(child) {
        if (!child) return child;
        // A DocumentFragment is spliced in, not appended as a node - that is
        // the whole reason renderIncrementally batches through one.
        if (child.isFragment) {
          const moved = child.children.slice();
          moved.forEach((node) => { node.parentNode = this; });
          this.children.push(...moved);
          child.children = [];
          return child;
        }
        // Appending MOVES a node: it is detached from its previous parent.
        // Without this, a `while (host.firstChild)` drain loop never advances.
        if (child.parentNode && child.parentNode.children) {
          const at = child.parentNode.children.indexOf(child);
          if (at >= 0) child.parentNode.children.splice(at, 1);
        }
        child.parentNode = this;
        this.children.push(child);
        return child;
      },
      removeAttribute(name) { delete this.attributes[name]; },
      querySelectorAll() { return []; },
    };
    created.push(el);
    return el;
  }

  const observerCallbacks = [];
  const observed = [];
  class FakeIntersectionObserver {
    constructor(callback) { observerCallbacks.push(callback); }
    observe(target) { observed.push(target); }
    unobserve() {}
    disconnect() {}
  }

  const sandbox = {
    window: { addEventListener() {}, removeEventListener() {} },
    document: {
      hidden: false,
      addEventListener() {},
      createElement: makeElement,
      createDocumentFragment: () => makeElement('#fragment'),
    },
    setInterval() { return 1; },
    clearInterval() {},
    setTimeout,
    clearTimeout,
    requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
    cancelAnimationFrame() {},
    Symbol,
    Promise,
    Math,
    Number,
    JSON,
    Object,
    Array,
    Error,
    TypeError,
    String,
    console,
    IntersectionObserver: withObserver ? FakeIntersectionObserver : undefined,
  };
  sandbox.window.daygleUi = null;
  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);

  return {
    sandbox,
    frames,
    observed,
    observerCallbacks,
    makeElement,
    // Run every queued frame until none remain (bounded, so a runaway
    // reschedule fails loudly instead of hanging the suite).
    flushFrames(limit = 500) {
      let ran = 0;
      while (frames.length && ran < limit) {
        const fn = frames.shift();
        fn();
        ran += 1;
      }
      return ran;
    },
  };
}

const rowHtml = (item) => `<tr data-id="${item}"><td>${item}</td></tr>`;

function containerWith(sandbox) {
  return sandbox.document.createElement('div');
}

// ─── renderIncrementally ──────────────────────────────────────────────────

test('the first batch is painted synchronously', () => {
  const { sandbox } = loadUtils();
  const container = containerWith(sandbox);
  const items = Array.from({ length: 500 }, (_, i) => i);

  sandbox.renderIncrementally(container, items, rowHtml);

  // No frame has run yet, but rows are already in the DOM - this is what keeps
  // a long list feeling instant.
  assert.equal(container.children.length, 40, 'first screenful should be synchronous');
  assert.ok(container.children.length < items.length, 'the rest must be deferred');
});

test('the container is emptied once, not per batch', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = containerWith(sandbox);
  const items = Array.from({ length: 500 }, (_, i) => i);

  // Count innerHTML writes on the container. Emptying it per batch would
  // destroy the rows already painted, so it must happen exactly once.
  let innerHTMLWrites = 0;
  let value = '';
  Object.defineProperty(container, 'innerHTML', {
    get() { return value; },
    set(next) {
      innerHTMLWrites += 1;
      value = next;
      this.children = [];
    },
    configurable: true,
  });

  sandbox.renderIncrementally(container, items, rowHtml);
  flushFrames();

  assert.equal(innerHTMLWrites, 1, `container written ${innerHTMLWrites} times; must be once`);
  assert.ok(container.children.length > 0, 'rows should still be present');
});

test('every row is painted exactly once, in order, across frames', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = containerWith(sandbox);
  const items = Array.from({ length: 250 }, (_, i) => i);
  const rendered = [];

  sandbox.renderIncrementally(container, items, (item) => {
    rendered.push(item);
    return rowHtml(item);
  });
  flushFrames();

  assert.deepEqual(rendered, items, 'rows must render once each, in order');
  assert.equal(container.children.length, items.length, 'every row lands directly in the container');
});

test('a newer render cancels its predecessor instead of interleaving', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = containerWith(sandbox);
  const first = [];
  const second = [];

  sandbox.renderIncrementally(container, [1, 2, 3, 4, 5, 6, 7, 8], (i) => {
    first.push(i);
    return rowHtml(i);
  });
  // Supersede before the deferred batches have run.
  sandbox.renderIncrementally(container, [100, 200], (i) => {
    second.push(i);
    return rowHtml(i);
  });
  flushFrames();

  assert.equal(first.length, 8, 'first render painted only its synchronous first batch');
  assert.deepEqual(second, [100, 200], 'the surviving render paints exactly its own rows');
  // The cancelled render must not have appended anything after being superseded.
  assert.equal(container.children.length, 2, `container has ${container.children.length} rows, expected 2`);
});

test('an empty list clears the container and completes immediately', () => {
  const { sandbox } = loadUtils();
  const container = containerWith(sandbox);
  let completed = 0;

  sandbox.renderIncrementally(container, [], rowHtml, { onComplete: () => { completed += 1; } });

  assert.equal(container.children.length, 0, 'container should be empty');
  assert.equal(completed, 1, 'onComplete should fire for an empty list');
});

test('a non-array list is treated as empty rather than throwing', () => {
  const { sandbox } = loadUtils();
  const container = containerWith(sandbox);
  assert.doesNotThrow(() => sandbox.renderIncrementally(container, null, rowHtml));
  assert.doesNotThrow(() => sandbox.renderIncrementally(container, undefined, rowHtml));
});

test('onComplete fires once, after the final batch', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = containerWith(sandbox);
  const items = Array.from({ length: 130 }, (_, i) => i);
  let completed = 0;

  sandbox.renderIncrementally(container, items, rowHtml, { onComplete: () => { completed += 1; } });
  assert.equal(completed, 0, 'must not complete before the last batch');
  flushFrames();
  assert.equal(completed, 1, 'must complete exactly once');
});

test('the cancel function stops further batches', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = containerWith(sandbox);
  const items = Array.from({ length: 500 }, (_, i) => i);
  const rendered = [];

  const cancel = sandbox.renderIncrementally(container, items, (i) => {
    rendered.push(i);
    return rowHtml(i);
  });
  const afterFirstBatch = rendered.length;
  cancel();
  flushFrames();

  assert.equal(rendered.length, afterFirstBatch, 'cancelled render must not paint more rows');
});

test('rows parse through a template host and land directly in the container', () => {
  // Regression: two DOM mistakes made the table pages unreadable.
  //   1. Rows were parsed through a throwaway <div>, whose innerHTML silently
  //      DROPS <tr>/<td> (body-mode fragment parsing discards them). Every
  //      table page therefore rendered its rows as loose text inside the
  //      <tbody>, and the browser repaired that with thousands of anonymous
  //      table boxes - deformed columns AND a slow, janky layout.
  //   2. Batches were wrapped in a <tbody> by default, nesting a second
  //      <tbody> inside the pages' existing <tbody id="...-rows">.
  // A <template> parses the row markup into real TR/TD nodes whatever the
  // container is; rows then go straight into the container.
  const { sandbox, flushFrames } = loadUtils();
  const tbody = sandbox.document.createElement('tbody');
  // Spy AFTER building the container so the tag log only covers the render.
  const tags = [];
  const createElement = sandbox.document.createElement;
  sandbox.document.createElement = (tag) => {
    tags.push(String(tag).toUpperCase());
    return createElement(tag);
  };
  const items = Array.from({ length: 250 }, (_, i) => i);

  sandbox.renderIncrementally(tbody, items, rowHtml);
  flushFrames();

  assert.ok(tags.filter((tag) => tag === 'TEMPLATE').length >= items.length,
    'each row must be parsed through a <template>, not a <div>');
  assert.ok(!tags.includes('TBODY'), 'no wrapper tbody may be created');
  assert.equal(tbody.children.length, items.length, 'every row is a direct child of the tbody');
  for (const child of tbody.children) {
    assert.ok(String(child._html || '').includes('<tr'), 'children are the rendered rows');
  }
});

// ─── media lifecycle ──────────────────────────────────────────────────────

function makeMedia(tag, extra = {}) {
  // src reflects the attribute, as it does in a real DOM: removeAttribute('src')
  // is what makes img.src read back as ''. The accessor must be defined AFTER
  // the spread, or a plain `src` in extra would shadow it.
  const media = {
    tagName: tag.toUpperCase(),
    dataset: {},
    attributes: {},
    paused: true,
    playCount: 0,
    play() { this.playCount += 1; this.paused = false; return Promise.resolve(); },
    pause() { this.paused = true; },
    removeAttribute(name) { delete this.attributes[name]; },
    ...extra,
  };
  Object.defineProperty(media, 'src', {
    get() { return this.attributes.src || ''; },
    set(value) { this.attributes.src = value; },
    configurable: true,
  });
  return media;
}

test('an offscreen video pauses and an onscreen one resumes', () => {
  const { sandbox, observerCallbacks } = loadUtils();
  const observer = sandbox.observeMediaLifecycle({ querySelectorAll: () => [] });
  assert.ok(observer, 'observer should be created');

  const video = makeMedia('video', { paused: false });
  const fire = observerCallbacks[0];
  fire([{ target: video, isIntersecting: false }]);
  assert.equal(video.paused, true, 'offscreen video must pause');

  fire([{ target: video, isIntersecting: true }]);
  assert.equal(video.playCount, 1, 'returning video must resume if it was playing');
});

test('a deliberately paused video is not resumed on return', () => {
  const { sandbox, observerCallbacks } = loadUtils();
  sandbox.observeMediaLifecycle({ querySelectorAll: () => [] });
  const video = makeMedia('video', { paused: true });
  const fire = observerCallbacks[0];

  fire([{ target: video, isIntersecting: false }]);
  fire([{ target: video, isIntersecting: true }]);

  assert.equal(video.playCount, 0, 'a paused clip must stay paused when scrolled back into view');
});

test('an offscreen image releases its src and gets it back without a refetch', () => {
  const { sandbox, observerCallbacks } = loadUtils();
  sandbox.observeMediaLifecycle({ querySelectorAll: () => [] });
  const img = makeMedia('img');
  // Assign through the accessor so it lands in attributes.src, which is what
  // removeAttribute('src') clears - the same reflection a real <img> has.
  img.src = '/api/events/1/snapshot';  const fire = observerCallbacks[0];

  fire([{ target: img, isIntersecting: false }]);
  assert.equal(img.src, '', 'offscreen image should release its decoded bitmap');
  assert.equal(img.dataset.daygleSrc, '/api/events/1/snapshot', 'src should be stashed, not discarded');

  fire([{ target: img, isIntersecting: true }]);
  assert.equal(img.src, '/api/events/1/snapshot', 'src must be restored from the stash');
  assert.equal(img.dataset.daygleSrc, undefined, 'the stash should be cleared once restored');
});

test('observeMediaLifecycle is a no-op without IntersectionObserver', () => {
  const { sandbox } = loadUtils({ withObserver: false });
  assert.equal(sandbox.observeMediaLifecycle({ querySelectorAll: () => [] }), null);
  assert.doesNotThrow(() => sandbox.observeMediaLifecycle(null));
});

test('a single observer instance is shared across calls', () => {
  const { sandbox, observed, observerCallbacks } = loadUtils();
  const root = { querySelectorAll: () => [makeMedia('video')] };
  sandbox.observeMediaLifecycle(root);
  sandbox.observeMediaLifecycle(root);
  assert.equal(observerCallbacks.length, 1, 'the observer must be created once and reused');
  assert.equal(observed.length, 2, 'each call still observes its own media');
});
