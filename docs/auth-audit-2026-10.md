# Authentication lifecycle audit — October 2, 2026

## Scope

Reviewed login/setup/logout routes, AuthService password checking and session
storage, session renewal/absolute expiry, cookie creation/deletion, auth
middleware, proxy trust, redirect handling, CSRF recovery, background auth
refresh, countdowns and logout UI. Earlier codebase and AI audit changes remain
intact. No secrets, deployment settings, commits or running services changed.

Focused baseline: 105 existing backend tests passed. Added 14 backend cases in
`tests/test_auth_lifecycle_audit.py` and three browser-runtime sandbox tests in
`tests/test_auth_frontend_edge_cases.test.js`.

## Confirmed issues fixed

- **Lost login destination:** frontend session-loss redirects used `returnTo`,
  but GET /login only accepted `return_to`. Both spellings now work; failed
  CSRF/password attempts and rate-limited forms retain the validated destination.
- **Redirect/template errors:** reject control characters before stripping input;
  replace only the literal CSRF placeholder instead of formatting the whole HTML
  template. A return path containing braces cannot cause a formatting exception.
- **Blocking authentication work:** bcrypt/login work and middleware session/user
  database checks run in the threadpool rather than blocking the async event loop.
- **Expiry reporting:** login and session reads/renewals cap the returned deadline
  at the absolute session expiry. The hard cap itself never moves. Null legacy
  caps derive from the session creation time; an old session cannot become an
  immortal sliding session through a null cap.
- **Cookie deadline mismatch:** browser Max-Age previously reset to the complete
  timeout on every response, overriding the shorter Expires deadline. It now
  matches the remaining server deadline (including the absolute cap).
- **Session write amplification:** recent reads no longer update last_seen_at on
  every camera/status/countdown poll. Activity writes use the five-minute cadence.
- **Proxy cookie security:** Secure is set for HTTPS seen directly or indicated by
  a configured trusted proxy. Untrusted forwarded headers cannot set that flag.
- **Logout:** retain stale/missing CSRF-token resilience, but reject explicitly
  cross-origin browser requests. Token comparison is constant-time. The UI no
  longer reports logout/broadcasts session loss when the revocation request fails;
  it shows a retryable error instead. GET /logout remains non-mutating.
- **Auth caching:** login/setup/logout and auth-state API responses carry no-store.
- **Refresh concurrency:** concurrent refresh calls share one authoritative fetch;
  a late response does not restore local auth during session-loss redirection.
- **Timer edge cases:** refresh timers have a five-second minimum near absolute
  expiry and a signed-32-bit-safe maximum for long configured timeouts. Failed
  timer refreshes can schedule a later attempt rather than permanently stopping.
  Session loss cancels the pending refresh timer.

## Timeout semantics (preserved)

- session_timeout_hours is a **sliding authenticated-request timeout**, not a
  keyboard/mouse inactivity timeout. Live camera polling, countdown requests,
  and scheduled auth refreshes count as activity and can keep an open tab signed
  in until the absolute cap. Changing this would be a product behavior change.
- Unexpired sessions renew lazily, approximately every five minutes; expired
  sessions are removed before renewal and cannot be resurrected.
- absolute_session_lifetime_seconds defaults to 14 days from login. It is not
  extended by requests. The browser cookie, auth API and countdown now expose
  the effective deadline rather than an impossible deadline beyond that cap.
- Password changes revoke other sessions; admin password/role/deactivation or
  username changes revoke sessions per existing policy. Ordinary auth/me polls
  do not rotate CSRF tokens, avoiding cross-tab token invalidation races.
- Logout revokes the current server session and deletes configured cookies.
  Already-in-flight requests admitted before revocation can still complete;
  cookies for a revoked token cannot authenticate later requests.

## Verification

- Full backend: **2,091 passed, 7 skipped**, **80.59% coverage**, required 60%
  coverage floor passed (`pytest -q -n 6 --dist loadfile`).
- Frontend: **276 passed**, no failures (`npm test`).
- Ruff for app and the new test file, ESLint, Python compilation and diff
  whitespace checks passed.
- Coverage includes real HTTP login destination handling, cross-origin logout,
  revoked/expired session lookup, null caps, SQLite activity timestamps, cookie
  flags/deadlines, template safety, and frontend coalescing/timer/late-response
  behavior. Existing stale-CSRF logout, auth role, renewal and proxy suites pass.

## Remaining considerations

- No visual browser/proxy deployment or multi-day soak test was performed. Tests
  use local HTTP servers and sandboxed JS; this is not a claim of error-free
  production authentication.
- First-admin setup and last-active-admin guards still need transactionally
  serialized check/write operations for concurrent administrator changes. This
  audit did not rewrite user management transactions.
- Session checks do not cancel already-admitted requests. Strong revocation
  boundaries for long requests or concurrent role/password changes require
  transactional endpoint-specific checks.
- Background polling intentionally counts as session activity. If the desired
  policy is sign-out after *human inactivity*, introduce separate explicit user
  activity tracking and passive session reads, with UI acceptance tests.
- Cross-tab localStorage broadcasts retain their existing transport/expiry
  semantics; a different-account broadcast does not authoritatively refresh user
  identity until the next server auth refresh. Multi-account browser support
  warrants a dedicated redesign (same-origin tabs share browser cookies).
- Pre-auth forms still read unbounded request bodies and rely on double-submit
  CSRF tokens. Apply proxy request-size/time limits; stronger application-wide
  caps and pre-auth origin policy should be evaluated separately.
- A very delayed response from a request admitted before logout may reset a
  browser cookie for an already-revoked token. Server revocation still prevents
  authentication, but eliminating all stale response cookie races would require
  additional response-finalization/session-version coordination.
