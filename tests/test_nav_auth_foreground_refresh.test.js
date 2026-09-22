// Regression tests: returning to a backgrounded/frozen/bfcache-restored tab
// must re-verify auth when the account renders as the "Sign in" skeleton, so a
// still-valid session repaints the nav instead of staying stuck until a click.
//
// nav.js runs a DOM-coupled IIFE at import (fetch, document, window listeners),
// so it is not evaluated in a vm sandbox here; these assertions pin the guard
// in the source, matching tests/test_playback_overlay_logic.test.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const navSource = readFileSync(path.resolve(here, '../web/nav.js'), 'utf8');

function onReturnBody() {
  const start = navSource.indexOf('function onReturnToForeground()');
  assert.ok(start !== -1, 'onReturnToForeground should exist');
  const end = navSource.indexOf('\n  }', start);
  assert.ok(end !== -1, 'onReturnToForeground body should be delimited');
  return navSource.slice(start, end);
}

test('a null-user return re-verifies via /api/auth/me instead of bailing out', () => {
  const body = onReturnBody();
  // The old early-return `if (!window.daygleAuth?.user) return;` left the nav
  // stuck; the null branch must now call refreshDaygleAuth.
  assert.match(body, /if \(!window\.daygleAuth\?\.user\)\s*\{/);
  assert.match(body, /window\.refreshDaygleAuth\(\)/);
  assert.doesNotMatch(body, /if \(!window\.daygleAuth\?\.user\) return;/);
});

test('the null-user re-verify is skipped on public auth pages', () => {
  const body = onReturnBody();
  assert.match(body, /PUBLIC_AUTH_PATHS\.has\(window\.location\?\.pathname\)\) return;/);
  assert.match(navSource, /const PUBLIC_AUTH_PATHS = new Set\(\['\/login', '\/setup', '\/logout'\]\);/);
});

test('an in-flight session-loss redirect is not raced', () => {
  const body = onReturnBody();
  assert.match(body, /if \(window\.daygleAuth\?\.redirecting\) return;/);
});

test('a persisted pageshow (bfcache restore) triggers the re-verify', () => {
  const start = navSource.indexOf("addEventListener('pageshow'");
  assert.ok(start !== -1, 'a pageshow listener should be registered');
  const body = navSource.slice(start, start + 120);
  assert.match(body, /event\.persisted/);
  assert.match(body, /onReturnToForeground\(\)/);
});
