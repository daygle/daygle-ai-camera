// The Storage card on the System page (web/system.js + web/system.html).
//
// Runs system.js in a sandbox (no DOM: elements are stubs, the page's init
// waits forever on auth) and checks the byte formatting, the meter levels,
// the summary sentence and the rendered card.
//
// Run with:
//   node --test tests/test_system_storage_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const js = readFileSync(path.resolve(here, '../web/system.js'), 'utf8');
const html = readFileSync(path.resolve(here, '../web/system.html'), 'utf8');

function loadPage() {
  const elements = {};
  const context = {
    document: {
      getElementById: (id) => {
        elements[id] ??= { id, textContent: '', innerHTML: '', hidden: false, classList: { toggle() {} } };
        return elements[id];
      },
    },
    window: { daygleAuthReady: new Promise(() => {}) },
    escapeHtml: (value) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
    api: async () => ({}),
    startPageInterval: () => null,
  };
  vm.createContext(context);
  vm.runInContext(js, context);
  return { context, elements };
}

const GIB = 1024 ** 3;

test('bytes read in binary units like the RAM card', () => {
  const { context } = loadPage();
  assert.equal(context.formatBytes(512), '512 B');
  assert.equal(context.formatBytes(1.5 * 1024 ** 2), '1.5 MB');
  assert.equal(context.formatBytes(250 * GIB), '250 GB');
  assert.equal(context.formatBytes(2 * 1024 ** 4), '2.0 TB');
  assert.equal(context.formatBytes(undefined), '-');
});

test('a disk meter warns at 85% and turns red at 95%', () => {
  const { context } = loadPage();
  assert.equal(context.storageLevel(50), '');
  assert.equal(context.storageLevel(85), 'is-warn');
  assert.equal(context.storageLevel(95), 'is-danger');
  assert.equal(context.storageLevel(NaN), '');
});

test('the summary compares recordings with the retention cap', () => {
  const { context } = loadPage();
  assert.equal(
    context.storageSummaryText({ usage: { recordings_bytes: 12 * GIB }, retention: { max_storage_bytes: 20 * GIB, retention_days: 14, auto_purge_enabled: true } }),
    'Recordings use 12 GB of the 20 GB limit, kept for 14 days.',
  );
  assert.equal(
    context.storageSummaryText({ usage: { recordings_bytes: 1024 }, retention: { max_storage_bytes: null, retention_days: 1, auto_purge_enabled: false } }),
    'Recordings use 1.0 KB, kept for 1 day, auto-purge is off.',
  );
});

test('the card lists each disk once with what it holds, then per-kind usage', () => {
  const { context, elements } = loadPage();
  context.renderStorage({
    volumes: [{ total_bytes: 100 * GIB, used_bytes: 90 * GIB, free_bytes: 10 * GIB, percent: 90, holds: ['recordings', 'database'] }],
    usage: { recordings_bytes: 80 * GIB, snapshots_bytes: 2 * GIB, events_bytes: 0, database_bytes: 50 * 1024 ** 2 },
    retention: { max_storage_bytes: 100 * GIB, retention_days: 30, auto_purge_enabled: true },
  });
  const volumes = elements.storageVolumes.innerHTML;
  assert.match(volumes, /Recordings · Database/);
  assert.match(volumes, /90 GB of 100 GB used · 10 GB free/);
  assert.match(volumes, /storage-meter-fill is-warn" style="width: 90%"/);
  const usage = elements.storageUsage.innerHTML;
  for (const label of ['Recordings', 'Snapshots', 'Events', 'Database']) assert.match(usage, new RegExp(`<span>${label}</span>`));
  assert.match(usage, /<strong>50 MB<\/strong>/);
});

test('the page has a Storage card and polls storage once a minute', () => {
  assert.match(html, /<section class="card" id="storageCard">/);
  assert.match(html, /id="storageVolumes"/);
  assert.match(js, /api\('\/api\/system\/storage'\)/);
  assert.match(js, /startPageInterval\(\(\) => \{ loadSystemStorage\(\)\.catch\(\(\) => \{\}\); \}, 60000\);/);
});
