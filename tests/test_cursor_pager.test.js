// Tests for the streaming list primitives added to web/utils.js: the cursor
// pager the events/recordings lists stream through, the append mode of the
// incremental renderer, and the load-more sentinel.
//
// The behaviours that matter:
//   1. A page is exactly one request carrying limit/cursor, and the pager
//      reports `done` only when the server stops handing out cursors.
//   2. Concurrent loadPage() calls coalesce into ONE request returning ONE
//      page - the scroll sentinel and the load-more button can otherwise
//      double-fetch and double-append the same rows.
//   3. A repeated cursor is a server bug and must throw, not loop forever.
//   4. Appending a streamed page must NOT wipe rows already painted; a full
//      render still replaces them.
//   5. The sentinel fires loadMore when visible, re-kicks on re-registration
//      (an appended page that leaves the sentinel on-screen keeps
//      streaming), and is released by unobserve.
//
// Rendered against a stub fetch and a minimal DOM in a vm sandbox (the same
// pattern as tests/test_request_coalescing.test.js and
// tests/test_incremental_render.test.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const utilsSource = readFileSync(path.resolve(here, '../web', 'utils.js'), 'utf8');

function jsonResponse(payload, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

// The DOM stub from tests/test_incremental_render.test.js, trimmed to what
// renderIncrementally needs: children, innerHTML that clears on assignment,
// and fragments whose children MOVE out on appendChild.
function makeElement(created) {
  return function create(tag) {
    const el = {
      tagName: String(tag).toUpperCase(),
      isFragment: String(tag).startsWith('#fragment'),
      children: [],
      className: '',
      dataset: {},
      attributes: {},
      _innerHTML: '',
      parentNode: null,
      get firstChild() { return this.children[0] || null; },
      get content() { return this; },
      get innerHTML() { return this._innerHTML; },
      set innerHTML(value) {
        this._innerHTML = value;
        this.children = [];
        if (value !== '' && value != null) {
          this.children.push({ _html: value, isFragment: false, parentNode: this });
        }
      },
      appendChild(child) {
        if (!child) return child;
        if (child.isFragment) {
          const moved = child.children.slice();
          moved.forEach((node) => { node.parentNode = this; });
          this.children.push(...moved);
          child.children = [];
          return child;
        }
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
  };
}

// An IntersectionObserver stub whose callbacks the test drives directly; it
// also records observe/unobserve so the sentinel's lifecycle is observable.
class FakeIntersectionObserver {
  static instances = [];
  constructor(callback, options) {
    this.callback = callback;
    this.options = options;
    this.observed = new Set();
    this.unobserved = [];
    FakeIntersectionObserver.instances.push(this);
  }
  observe(target) { this.observed.add(target); }
  unobserve(target) { this.observed.delete(target); this.unobserved.push(target); }
  disconnect() { this.observed.clear(); }
  fire(target, isIntersecting) {
    this.callback([{ target, isIntersecting }]);
  }
}

function loadUtils({ respond } = {}) {
  const calls = [];
  const frames = [];
  const created = [];
  FakeIntersectionObserver.instances = [];
  const sandbox = {
    window: { addEventListener() {}, removeEventListener() {}, daygleAuth: {} },
    document: {
      hidden: false,
      addEventListener() {},
      createElement: makeElement(created),
      createDocumentFragment: () => makeElement(created)('#fragment'),
    },
    setInterval() { return 1; },
    clearInterval() {},
    setTimeout,
    clearTimeout,
    requestAnimationFrame(fn) { frames.push(fn); return frames.length; },
    cancelAnimationFrame() {},
    AbortController,
    FormData,
    IntersectionObserver: FakeIntersectionObserver,
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
    fetch: async (url, options = {}) => {
      const record = { url, options };
      calls.push(record);
      return respond(record, calls.length - 1);
    },
  };
  sandbox.window.daygleUi = null;
  vm.createContext(sandbox);
  vm.runInContext(utilsSource, sandbox);
  return {
    sandbox,
    calls,
    frames,
    created,
    evaluate(expression) {
      return vm.runInContext(expression, sandbox);
    },
    flushFrames(limit = 500) {
      let ran = 0;
      while (frames.length && ran < limit) {
        frames.shift()();
        ran += 1;
      }
      return ran;
    },
  };
}

const rowHtml = (item) => `<tr data-id="${item}"><td>${item}</td></tr>`;

// ─── createCursorPager ─────────────────────────────────────────────────────

test('the first page is one request with a page limit and reports more', async () => {
  const { sandbox, calls } = loadUtils({
    respond: () => jsonResponse({ items: [1, 2], next_cursor: 'c1' }),
  });
  const pager = sandbox.createCursorPager('/api/events?since=x', 200);

  const page = await pager.loadPage();

  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /since=x/, 'the caller query must survive');
  assert.match(calls[0].url, /limit=200/, 'the pager must set its own limit');
  assert.deepEqual(page.items, [1, 2]);
  assert.equal(pager.done, false, 'a next_cursor means more pages');
  assert.equal(pager.loading, false);
});

test('later pages follow the cursor and finish when it runs out', async () => {
  const replies = [
    { items: [1], next_cursor: 'c1' },
    { items: [2], next_cursor: '' },
  ];
  const { sandbox, calls } = loadUtils({
    respond: () => jsonResponse(replies.shift()),
  });
  const pager = sandbox.createCursorPager('/api/events', 200);

  await pager.loadPage();
  const second = await pager.loadPage();

  assert.equal(calls.length, 2);
  assert.match(calls[1].url, /cursor=c1/, 'the second request must carry the cursor');
  assert.deepEqual(second.items, [2]);
  assert.equal(pager.done, true, 'an empty next_cursor ends the list');
});

test('concurrent loadPage calls share one request and one page', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { sandbox, calls } = loadUtils({
    respond: async () => {
      await gate;
      return jsonResponse({ items: [1], next_cursor: '' });
    },
  });
  const pager = sandbox.createCursorPager('/api/events', 200);

  const first = pager.loadPage();
  const second = pager.loadPage();
  release();
  const [a, b] = await Promise.all([first, second]);

  assert.equal(calls.length, 1, 'a scroll hit plus a button hit must not double-fetch');
  assert.deepEqual(a.items, [1]);
  assert.equal(a, b, 'both callers must see the same page object');
});

test('a repeated cursor is rejected instead of looping', async () => {
  const { sandbox } = loadUtils({
    respond: () => jsonResponse({ items: [1], next_cursor: 'c1' }),
  });
  const pager = sandbox.createCursorPager('/api/events', 200);

  await pager.loadPage();
  await assert.rejects(() => pager.loadPage(), /repeated pagination cursor/);
});

test('legacy array payloads load as a single complete page', async () => {
  const { sandbox, calls } = loadUtils({
    respond: () => jsonResponse([1, 2, 3]),
  });
  const pager = sandbox.createCursorPager('/api/events', 200);

  const page = await pager.loadPage();

  assert.deepEqual(page.items, [1, 2, 3]);
  assert.equal(pager.done, true, 'an array carries no cursor: it is the whole set');
  const after = await pager.loadPage();
  assert.equal(after.items.length, 0, 'nothing follows a complete set');
  assert.equal(calls.length, 1, 'a finished pager must not hit the network');
});

// ─── fetchAllCursorPages (kept honest after the pager refactor) ────────────

test('fetchAllCursorPages still drains every page in order', async () => {
  const replies = [
    { items: [1, 2], next_cursor: 'c1' },
    { items: [3], next_cursor: '' },
  ];
  const { sandbox, calls } = loadUtils({
    respond: () => jsonResponse(replies.shift()),
  });

  const items = await sandbox.fetchAllCursorPages('/api/events', 500);

  // Spread into a host-realm array: the accumulator is a vm-realm Array and
  // deepStrictEqual is prototype-sensitive across realms.
  assert.deepEqual([...items], [1, 2, 3]);
  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /limit=500/);
});

// ─── renderIncrementally append mode ───────────────────────────────────────

test('an appended page lands after the painted rows without a wipe', () => {
  const { sandbox, flushFrames, created } = loadUtils();
  const container = sandbox.document.createElement('div');

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

  sandbox.renderIncrementally(container, Array.from({ length: 100 }, (_, i) => i), rowHtml);
  flushFrames();
  const firstRow = container.children[0];
  sandbox.renderIncrementally(container, [100, 101], rowHtml, { append: true });
  flushFrames();

  assert.equal(innerHTMLWrites, 1, 'the append must not write (and clear) innerHTML');
  assert.equal(container.children.length, 102);
  assert.equal(container.children[0], firstRow, 'rows already painted must survive');
  assert.ok(created.length >= 102, 'every row is created through the template host');
});

test('an empty appended page keeps the painted rows', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = sandbox.document.createElement('div');

  sandbox.renderIncrementally(container, [1, 2, 3], rowHtml);
  flushFrames();
  sandbox.renderIncrementally(container, [], rowHtml, { append: true });
  flushFrames();

  assert.equal(container.children.length, 3, 'a filtered page with no matches must not clear the list');
});

test('a full render still replaces appended rows', () => {
  const { sandbox, flushFrames } = loadUtils();
  const container = sandbox.document.createElement('div');

  sandbox.renderIncrementally(container, [1, 2, 3], rowHtml);
  flushFrames();
  sandbox.renderIncrementally(container, [4, 5], rowHtml, { append: true });
  flushFrames();
  sandbox.renderIncrementally(container, [9], rowHtml);
  flushFrames();

  assert.equal(container.children.length, 1, 'the full render owns the container again');
  assert.equal(container.children[0]._html, rowHtml(9));
});

// ─── setLoadMoreSentinel ───────────────────────────────────────────────────

test('the sentinel fires loadMore when visible and ignores when hidden', () => {
  const { sandbox } = loadUtils();
  const sentinel = sandbox.document.createElement('div');
  let kicks = 0;

  sandbox.setLoadMoreSentinel('events', sentinel, () => { kicks += 1; });
  const observer = FakeIntersectionObserver.instances[0];

  observer.fire(sentinel, true);
  assert.equal(kicks, 1, 'scrolling to the sentinel must trigger the next page');
  observer.fire(sentinel, false);
  assert.equal(kicks, 1, 'scrolling away must not trigger');
});

test('re-setting the sentinel re-kicks it and releasing unobserves it', () => {
  const { sandbox } = loadUtils();
  const sentinel = sandbox.document.createElement('div');
  let kicks = 0;
  const loadMore = () => { kicks += 1; };

  sandbox.setLoadMoreSentinel('events', sentinel, loadMore);
  const observer = FakeIntersectionObserver.instances[0];
  assert.equal(observer.observed.size, 1);

  // The same element registered again: an observer's initial callback fires
  // on observe(), so this re-kick is what keeps an on-screen sentinel
  // streaming after an appended page.
  sandbox.setLoadMoreSentinel('events', sentinel, loadMore);
  assert.equal(FakeIntersectionObserver.instances.length, 1, 'one observer per key');
  assert.ok(observer.observed.has(sentinel));

  // A replacement element drops the old registration instead of leaking it.
  // (The re-set above already unobserved the sentinel once - unobserve then
  // observe is exactly what re-fires the initial callback - so the element
  // shows up in the unobserve log twice by now.)
  const next = sandbox.document.createElement('div');
  sandbox.setLoadMoreSentinel('events', next, loadMore);
  assert.equal(observer.unobserved.length, 2, 'the old sentinel must be unobserved again');
  assert.ok(observer.unobserved.every((el) => el === sentinel));
  assert.ok(observer.observed.has(next));

  observer.fire(next, true);
  assert.equal(kicks, 1, 'the replacement sentinel must carry the loadMore hook');

  sandbox.setLoadMoreSentinel('events', null, loadMore);
  assert.ok(!observer.observed.has(next), 'releasing must unobserve the current sentinel');
  assert.equal(observer.unobserved[2], next);
});
