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
