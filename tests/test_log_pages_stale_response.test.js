// The paged log pages (Audit, AI Log, Camera Log) must render the NEWEST
// request's rows. Paging or re-filtering quickly used to let a slow earlier
// response land last and overwrite the table, and the page offset moved
// before the request succeeded, so a failed request left the pager pointing
// at a page that was never shown.
//
// Each page script runs in a vm sandbox behind a minimal DOM stub with a
// controllable api().
//
// Run with:
//   node --test tests/test_log_pages_stale_response.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));

function fakeElement() {
  const el = {
    value: '',
    hidden: false,
    disabled: false,
    textContent: '',
    className: '',
    title: '',
    style: {},
    dataset: {},
    children: [],
    set innerHTML(_value) { el.children = []; },
    get innerHTML() { return ''; },
    appendChild(child) { el.children.push(child); return child; },
    addEventListener() {},
  };
  return el;
}

function loadPage(file, bodyId) {
  const elements = new Map();
  const requests = [];
  const sandbox = {
    LOG_PAGE_SIZE: 50,
    formatLogTime: (value) => String(value || ''),
    requireElements() {},
    api(url) {
      return new Promise((resolve, reject) => requests.push({ url, resolve, reject }));
    },
    URLSearchParams,
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, fakeElement());
        return elements.get(id);
      },
      createElement: () => fakeElement(),
    },
    window: { daygleAuthReady: Promise.resolve(), showToast() {}, confirm: () => false },
  };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(path.resolve(here, '../web', file), 'utf8'), sandbox);
  return {
    requests,
    tbody: () => sandbox.document.getElementById(bodyId),
    run: (code) => vm.runInContext(code, sandbox),
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const rows = (n) => ({ total: 500, entries: Array.from({ length: n }, (_, i) => ({ id: i, created_at: '' })) });

for (const [file, bodyId] of [['audit.js', 'auditBody'], ['ai-log.js', 'logBody'], ['camera-log.js', 'logBody']]) {
  test(`${file}: a slow earlier response cannot overwrite a newer page`, async () => {
    const page = loadPage(file, bodyId);
    await settle();
    // The initial load on script start.
    assert.equal(page.requests.length, 1);
    page.requests[0].resolve(rows(1));
    await settle();

    page.run('loadEntries(50)');
    page.run('loadEntries(100)');
    assert.equal(page.requests.length, 3);
    page.requests[2].resolve(rows(3));
    await settle();
    page.requests[1].resolve(rows(7));
    await settle();

    assert.equal(page.tbody().children.length, 3, 'the newest request owns the table');
    assert.equal(page.run('currentOffset'), 100);
  });

  test(`${file}: a failed page request leaves the offset on the rows shown`, async () => {
    const page = loadPage(file, bodyId);
    await settle();
    page.requests[0].resolve(rows(2));
    await settle();

    page.run('loadEntries(50)');
    page.requests[1].reject(new Error('offline'));
    await settle();

    assert.equal(page.run('currentOffset'), 0);
    assert.equal(page.tbody().children.length, 2);
  });
}
