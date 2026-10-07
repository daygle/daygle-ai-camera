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
  //
  // The guard now reads `!authUser || !authUser.username` rather than
  // `!window.daygleAuth?.user`. That is a deliberate strengthening, not a
  // cosmetic rename: the regression test below pins the reason. An empty
  // object is truthy, so `!window.daygleAuth?.user` was FALSE for a
  // truthy-but-empty user - the branch was skipped and the nav stayed wedged.
  assert.match(body, /const authUser = window\.daygleAuth\?\.user;/);
  assert.match(body, /if \(!authUser \|\| !authUser\.username\)\s*\{/);
  assert.match(body, /window\.refreshDaygleAuth\(\)/);
  assert.doesNotMatch(body, /if \(!window\.daygleAuth\?\.user\) return;/);
});

test('a truthy-but-empty cached user cannot wedge the nav at "Sign in"', () => {
  // Regression: daygleAuthReady used to normalise a payload with no `user`
  // to `{}`. `{}` is truthy, so (a) renderNavAccount - which keys off
  // user.username - painted the "Sign in" skeleton, while (b) the foreground
  // refresh treated it as signed in and skipped re-verifying. The session was
  // never lost, so only an unrelated click appeared to "log you back in".
  assert.match(
    navSource,
    /const user = payload\?\.user \|\| null;/,
    'daygleAuthReady must normalise a missing user to null, never {}',
  );
  assert.doesNotMatch(
    navSource,
    /const user = payload\.user \|\| \{\};/,
    'payload.user || {} is truthy and re-introduces the wedged nav',
  );
  // The two writers of window.daygleAuth.user must agree, or one of them
  // keeps re-creating the state the other one has to defend against.
  const utilsSource = readFileSync(path.resolve(here, '../web/utils.js'), 'utf8');
  assert.match(utilsSource, /const user = payload\?\.user \|\| null;/);
});

test('an unknown session expiry forces a re-verify rather than assuming freshness', () => {
  // isFreshForRefresh() returned true when expiresAt was empty. Combined with
  // `!wasIdle && isFreshForRefresh()` that skipped the re-verify on a quick
  // window switch - the precise repro for "switch windows, come back, still
  // signed out". An absent expiry is an absence of evidence, not evidence.
  const start = navSource.indexOf('function isFreshForRefresh()');
  assert.ok(start !== -1, 'isFreshForRefresh should exist');
  const body = navSource.slice(start, navSource.indexOf('\n  }', start));
  assert.match(body, /if \(!exp\) return false;/);
  assert.doesNotMatch(body, /if \(!exp\) return true;/);
});

test('an empty cached user is never fed to setDaygleDatePrefs unguarded', () => {
  // `user` is null after the normalisation above, so the direct property
  // reads must be optional-chained or the initial paint throws (which is
  // swallowed by the surrounding catch, leaving the nav unpainted).
  assert.doesNotMatch(navSource, /date_format: user\.date_format/);
  assert.match(navSource, /date_format: user\?\.date_format/);
  assert.doesNotMatch(navSource, /time_format: user\.time_format/);
  assert.match(navSource, /time_format: user\?\.time_format/);
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

test('refreshed auth state restores admin nav groups for admins', () => {
  assert.match(navSource, /nav\.querySelectorAll\('\[data-admin="true"\]'\)\.forEach\(\(el\) => \{\s*el\.hidden = user\.role !== 'admin';/);
});

test('a failed page-load auth check keeps retrying instead of leaving "Sign in"', () => {
  // Regression: one 502 (app restarting behind a proxy) or a dropped
  // connection on the page-load /api/auth/me left the nav at "Sign in" with
  // every admin group hidden, and nothing retried until the tab regained
  // focus - which a phone that stays on the page never does.
  assert.match(navSource, /let daygleInitialAuthFailed = false;/);
  assert.match(navSource, /if \(response\.status !== 401\) daygleInitialAuthFailed = true;/);
  assert.match(navSource, /catch \{\s*daygleInitialAuthFailed = true;\s*return null;/);
  assert.match(navSource, /if \(daygleInitialAuthFailed && !window\.daygleAuth\?\.user\?\.username\) retryFailedAuthCheck\(\);/);
  const start = navSource.indexOf('function retryFailedAuthCheck(');
  assert.ok(start !== -1, 'retryFailedAuthCheck should exist');
  const body = navSource.slice(start, navSource.indexOf('\n  }\n', start));
  // Stops once signed in or when a real session loss is redirecting.
  assert.match(body, /if \(window\.daygleAuth\?\.user\?\.username \|\| window\.daygleAuth\?\.redirecting\) return;/);
  // Re-verifies through the shared path (repaint on 200, /login on a 401).
  assert.match(body, /await window\.refreshDaygleAuth\(\)/);
  assert.match(body, /retryFailedAuthCheck\(attempt \+ 1\)/);
  assert.match(navSource, /const AUTH_RETRY_DELAYS_MS = \[1000, 2000, 4000, 8000, 15000\];/);
});

test('regaining a connection re-verifies auth', () => {
  assert.match(navSource, /window\.addEventListener\('online', onReturnToForeground\);/);
});

test('a restored session clears the signed-out "go to Sign in" click', () => {
  const start = navSource.indexOf('function renderNavAccount(user)');
  const body = navSource.slice(start);
  const signedIn = body.slice(body.indexOf('if (navUser) navUser.textContent = user.username;'));
  assert.match(signedIn, /if \(accountTrigger\) accountTrigger\.onclick = null;/);
});
