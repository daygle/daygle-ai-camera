// Regression tests for Settings -> Notifications -> Undelivered Alerts.
//
// The card lists the alert deliveries the mail relay or ntfy server never
// accepted (app.alert_outbox) and offers a per-row retry plus a retry-all. Both
// endpoints are admin-gated server-side; these tests pin the DOM ids and the
// endpoint wiring the same static-analysis way as the other *_js.test.js
// guards, since settings.js touches the DOM at load.
//
// Run with:
//   node --test tests/test_alert_outbox_js.test.js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const settingsHtml = readFileSync(path.resolve(here, '../web/settings.html'), 'utf8');
const settingsJs = readFileSync(path.resolve(here, '../web/settings.js'), 'utf8');
const styles = readFileSync(path.resolve(here, '../web/styles.css'), 'utf8');

test('the notifications panel carries the undelivered alerts card', () => {
  const panelStart = settingsHtml.indexOf('id="panel-notifications"');
  assert.notEqual(panelStart, -1, 'settings.html must have the notifications panel');
  const panelEnd = settingsHtml.indexOf('/panel-notifications', panelStart);
  const panel = settingsHtml.slice(panelStart, panelEnd);
  assert.ok(panel.includes('Undelivered Alerts'), 'the outbox card must be on the notifications panel');
  assert.ok(panel.includes('id="alertOutboxList"'), 'the outbox list container is missing');
  assert.ok(panel.includes('id="alertOutboxRetryAllBtn"'), 'the retry-all button is missing');
  assert.ok(panel.includes('id="alertOutboxSection"'), 'the outbox section id used by the renderer is missing');
});

test('settings.js loads the queue and wires both retry endpoints', () => {
  assert.match(settingsJs, /api\('\/api\/alerts\/outbox'\)/, 'the list must be fetched in loadSettings');
  assert.match(
    settingsJs,
    /\/api\/alerts\/outbox\/\$\{Number\(button\.dataset\.outboxRetry\)\}\/retry/,
    'the per-row Retry button must post to its row endpoint',
  );
  assert.match(
    settingsJs,
    /api\('\/api\/alerts\/outbox\/retry', \{ method: 'POST' \}\)/,
    'Retry All must post to the retry-all endpoint',
  );
  assert.match(settingsJs, /data-outbox-retry=/, 'the row renderer must emit the retry attribute');
  assert.match(settingsJs, /escapeHtml\(entry\.rule_name/, 'row values must be escaped before innerHTML');
});

test('the outbox rows have styles', () => {
  assert.ok(styles.includes('.alert-outbox-list'), 'the list needs a style rule');
  assert.ok(styles.includes('.alert-outbox-row'), 'the row needs a style rule');
});

test('the badge polls on a timer so a stuck server shows on an open page', () => {
  assert.match(settingsJs, /const ALERT_OUTBOX_REFRESH_MS = \d+;/, 'the poll period must be a named constant');
  assert.match(
    settingsJs,
    /startPageInterval\(pollAlertOutbox, ALERT_OUTBOX_REFRESH_MS\)/,
    'the poll must go through startPageInterval so hidden tabs stay quiet',
  );
  // The poller is started from the successful render only (a viewer's first
  // read 403s and must not leave a timer re-failing every tick).
  const renderBody = settingsJs.slice(
    settingsJs.indexOf('function renderAlertOutbox('),
    settingsJs.indexOf('const ALERT_OUTBOX_REFRESH_MS'),
  );
  assert.ok(renderBody.includes('startPageInterval(pollAlertOutbox'), 'the poller must start after a successful read');
  const deniedBranch = renderBody.slice(
    renderBody.indexOf('if (payload === null) {'),
    renderBody.indexOf('section.hidden = false;'),
  );
  assert.ok(!deniedBranch.includes('startPageInterval'), 'the 403 path must not start polling');
  assert.match(settingsJs, /Admin access required/, 'a demoted admin must stop the poller');
});

test('the Notifications tab carries a queued-count badge', () => {
  const tab = settingsHtml.slice(
    settingsHtml.indexOf('id="tab-notifications"'),
    settingsHtml.indexOf('</button>', settingsHtml.indexOf('id="tab-notifications"')),
  );
  assert.ok(tab.includes('id="notificationsOutboxBadge"'), 'the badge must live in the Notifications tab button');
  assert.ok(settingsJs.includes("getElementById('notificationsOutboxBadge')"), 'settings.js must drive the badge');
  assert.ok(styles.includes('.settings-tab-badge'), 'the badge needs a style rule');
});
