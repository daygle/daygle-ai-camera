// AI alert verification in the UI: the Events verdict badge (web/events.js)
// and the Alerts page warning when AI Verify is on but the model is failing
// (web/alerts.js).
//
// Run with:
//   node --test tests/test_ai_verification_ui.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');

// Only the badge helpers are evaluated: they are pure functions of the event.
function badgeContext() {
  const source = read('events.js');
  const start = source.indexOf('function aiVerdictText');
  const end = source.indexOf('// An alert row is written when a rule matches');
  const context = vm.createContext({
    escapeHtml: (value) => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'),
  });
  vm.runInContext(source.slice(start, end), context);
  return context;
}

const badge = (context, record) => vm.runInContext(
  `aiVerificationBadge(${JSON.stringify({ metadata: { ai_verification: record } })})`, context);

test('verified, filtered and unverified verdicts each get a badge with a tappable reason', () => {
  const context = badgeContext();
  const verified = badge(context, { status: 'confirmed', labels: { person: { present: true, reason: 'man at door' } } });
  assert.match(verified, /detection-ai-verified">🤖 Verified/);
  assert.match(verified, /class="info-tip ai-verdict-tip"[^>]*tabindex="0"/);
  assert.match(verified, /person: confirmed - man at door/);

  const filtered = badge(context, { status: 'filtered', delivered: false,
    labels: { person: { present: false, reason: 'a shadow', instead: 'owl' } } });
  assert.match(filtered, /🤖 Filtered/);
  assert.match(filtered, /Notification not sent/);
  assert.match(filtered, /looks like an owl/);

  for (const record of [
    { status: 'unsure', labels: { person: { present: null, unsure: true, reason: 'too dark' } } },
    { status: 'error', labels: { person: { present: null, error: 'model did not answer within 20s' } } },
    { status: 'skipped', reason: 'Verification queue backlog; delivered unverified.' },
  ]) {
    const html = badge(context, record);
    assert.match(html, /detection-ai-unverified">🤖 Unverified/, record.status);
  }
  assert.match(badge(context, { status: 'unsure', labels: { person: { unsure: true, reason: 'too dark' } } }), /person: unsure - too dark/);
  assert.equal(badge(context, undefined), '');
  assert.equal(badge(context, { status: 'something-new' }), '');
});

test('the unverified badge has a style in both themes', () => {
  const css = read('styles.css');
  assert.match(css, /:root\.light \.detection\.detection-ai-unverified/);
  assert.match(css, /\n\.detection\.detection-ai-unverified/);
  // The light verified style is scoped to the light theme so it is not
  // overridden by (or overriding) the dark one.
  assert.match(css, /:root\.light \.detection\.detection-ai-verified/);
});

test('Alerts warns when AI Verify is on but the model is failing or not set up', () => {
  const source = read('alerts.js');
  const start = source.indexOf('function aiVerifyWarningText');
  const end = source.indexOf('function renderAiVerifyWarning');
  const context = vm.createContext({});
  vm.runInContext(source.slice(start, end), context);
  const warn = (rules, status) => vm.runInContext(`aiVerifyWarningText(${JSON.stringify(rules)}, ${JSON.stringify(status)})`, context);
  const on = [{ label: 'person', ai_verify: true }];
  assert.match(warn(on, { configured: true, state: 'error', last_error: 'model server unreachable' }), /failing \(model server unreachable\)/);
  assert.match(warn(on, { configured: false, state: 'unknown' }), /no AI model is set up/);
  assert.equal(warn(on, { configured: true, state: 'ok' }), '');
  assert.equal(warn(on, { configured: true, state: 'unknown' }), '');
  assert.equal(warn([{ label: 'person', ai_verify: false }], { configured: true, state: 'error' }), '');
  assert.equal(warn([{ label: 'motion', ai_verify: true }], { configured: true, state: 'error' }), '');
  assert.equal(warn(on, null), '');
});
