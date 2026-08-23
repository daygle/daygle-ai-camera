function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
}

// Tagged template literal helper for safe HTML construction. Same shape as
// ``escapeHtml`` but applied automatically to every interpolation, so call
// sites can mix literal HTML markup with interpolated user data without
// having to remember to call ``escapeHtml`` on each value individually. The
// literal parts of the template (the HTML markup between ``${...}``) pass
// through verbatim; only the interpolated values get escaped.
//
//   const row = safeHtml`<div><span>${label}</span><strong>${value}</strong></div>`;
//
// All interpolations are coerced via ``escapeHtml``, which escapes ``&``, ``<``,
// ``>``, ``'``, and ``"`` - enough to defang every XSS payload that doesn't
// require complex character-level tricks. Single source of truth for the
// rule that backs the H2RegressionGuardTests regression guard in
// ``tests/test_xss_static_guards.py``: every ``.innerHTML`` write that
// interpolates server data must route through this helper (or compose with
// it), so a future raw ``.innerHTML = `...${...}...``` write fails the test.
function safeHtml(strings, ...values) {
  let result = strings[0];
  for (let i = 0; i < values.length; i += 1) {
    result += escapeHtml(values[i]);
    result += strings[i + 1];
  }
  return result;
}

// ─── Required-element guard (used by API-shaped admin pages) ─────────────
// Audit / camera-log / settings / sounds run as conventional scripts that
// capture a handful of <div>/<button>/<form> ids at the top of the file and
// then drive the page off those references. Each script is paired with a
// specific HTML file, so today every getElementById() succeeds - but if a
// future HTML refactor renames or removes an id without updating the JS,
// the page would crash with a cryptic TypeError on the first innerHTML
// write a few lines later. requireElements() is a single throw point per
// page that fails loud (console.error + Error) with the offending ids
// spelled out, so future drift surfaces immediately instead of hiding
// behind a stack trace. Pages that legitimately reference elements
// dynamically (live.js, zones.js, recordings.js, etc.) skip this - those
// scripts tolerate missing elements per page (see users.js header
// comment for the rationale).
function requireElements(ids) {
  if (!Array.isArray(ids) || !ids.length) return;
  const missing = ids.filter((id) => !document.getElementById(id));
  if (!missing.length) return;
  const pageTitle = String(document.title || document.URL || 'current page').trim() || 'current page';
  // pageTitle is derived from document.title (potentially page-controlled), so
  // pass it as an argument rather than interpolating it into the format string.
  console.error('[%s] missing required element ids:', pageTitle, missing);
  throw new Error('This page is missing required DOM elements; check the HTML for matching ids.');
}

// ─── Tabbed section navigation (settings + onnx pages) ─────────────────────
// Groups a page's cards into `.settings-panel` blocks switched by a
// `.settings-tab` bar. Implements the ARIA tabs pattern (roving tabindex +
// arrow keys) and mirrors the active tab into the URL hash so a section can
// be deep-linked and survives a refresh. No-ops on pages without a tab bar,
// so it is safe to call unconditionally. Tabs are matched to panels by their
// shared `data-tab` / `data-panel` value.
function initDaygleTabs() {
  const tabs = Array.from(document.querySelectorAll('.settings-tab'));
  if (!tabs.length) return;
  const panels = new Map(
    Array.from(document.querySelectorAll('.settings-panel')).map((panel) => [panel.dataset.panel, panel]),
  );

  function activate(name, { focus = false, updateHash = true } = {}) {
    if (!panels.has(name)) name = tabs[0].dataset.tab;
    tabs.forEach((tab) => {
      const selected = tab.dataset.tab === name;
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;
      if (selected && focus) tab.focus();
    });
    panels.forEach((panel, key) => { panel.hidden = key !== name; });
    if (updateHash) {
      try { history.replaceState(null, '', `#${name}`); } catch { window.location.hash = name; }
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab.dataset.tab));
    tab.addEventListener('keydown', (event) => {
      const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      activate(tabs[(index + step + tabs.length) % tabs.length].dataset.tab, { focus: true });
    });
  });

  const initial = (window.location.hash || '').replace('#', '');
  activate(panels.has(initial) ? initial : tabs[0].dataset.tab, { updateHash: false });
}

// ─── Toast notification (shared by every page that fires user feedback) ────
// Moved here from nav.js so pages can fire toasts on their own. The toast
// container is lazily created and the toast self-removes after a short delay.
function showToast(message, isError) {
  if (!message) return;
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = 'toast' + (isError ? ' error' : '');
  toast.textContent = String(message);
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3500);
}
window.showToast = showToast;

// ─── Shared API client + auth holder ──────────────────────────────────────
// The same `api(path, options)` helper used to live in every page bundle
// (app.js, recordings.js, timeline.js, live.js). Centralising it here keeps
// auth-header wiring (CSRF) and error semantics consistent, and it gives a
// single home for the `Content-Type` rule used by JSON-bodied POSTs.
//
// Each page still owns its own `loadAuth()` flow that hits /api/auth/me and
// pipes the result through `setApiAuth()` so subsequent api() calls find
// the csrf token in `window.daygleAuth.csrfToken`. 401s trigger a redirect
// to /login - pages that need a different policy can call `api(...)`
// directly with custom handlers.

// CSRF auth state lives on `window.daygleAuth` so any page can read it
// without re-fetching /api/auth/me. Populated by `setApiAuth()`.
window.daygleAuth = window.daygleAuth || { user: null, csrfToken: null };

// Backwards-compatible: third arg is the ISO `expires_at` from
// /api/auth/me, kept on window.daygleAuth so the scheduled refresh can
// defer "re-fetch in (expiresAt - now - 60s)" without an extra round-trip.
function setApiAuth(user, csrfToken, expiresAt = null) {
  window.daygleAuth.user = user || null;
  window.daygleAuth.csrfToken = csrfToken || null;
  window.daygleAuth.expiresAt = expiresAt || '';
  // Single dispatch point for auth-state-change notifications. Every writer
  // (daygleAuthReady IIFE, refreshDaygleAuth, subscribeDaygleAuthCrossTabs,
  // handleSessionLoss via its internal setApiAuth(null, null, null)) routes
  // through here, so listeners get one event per real change regardless of
  // how it was triggered - login, refresh, cross-tab CSRF rotation, or
  // session-loss redirect. The event is a no-op in browsers without
  // CustomEvent (which is essentially no one) and a no-op for any consumer
  // that hasn't registered a listener.
  try { window.dispatchEvent(new CustomEvent('daygle:auth-state-changed')); } catch (_err) { /* ignore */ }
}

function getApiAuth() {
  return window.daygleAuth;
}

// Where to send the user when their session has died. Public pages
// (/login, /setup, /logout) would navigate to themselves in an infinite
// loop if used as returnTo, so they normalise to '/'.
function defaultReturnTo() {
  const path = window.location?.pathname || '/';
  if (path === '/login' || path === '/setup' || path === '/logout') return '/';
  return path + (window.location?.search || '');
}

// Single redirect-on-session-loss path. Used by both 401 (auth gone) and a
// CSRF-mismatch 403 (auth was working, now the cached csrf_token is stale).
// Idempotent: a burst of in-flight requests collapse to a single redirect.
function handleSessionLoss(reason, returnTo) {
  if (!window.daygleAuth) return;
  if (window.daygleAuth.redirecting) return;
  if (typeof setApiAuth === 'function') setApiAuth(null, null, null);
  window.daygleAuth.redirecting = true;
  // Broadcast session loss to other open tabs so they don't stay stuck with
  // stale auth state. The empty csrf signals "session ended" to the cross-tab
  // listener in subscribeDaygleAuthCrossTabs. Safe to call with null user -
  // the function handles falsy input.
  if (typeof broadcastAuthStateToOtherTabs === 'function') {
    broadcastAuthStateToOtherTabs(null, '', '');
  }
  showToast(reason || 'Session expired - please sign in again', true);
  const target = '/login?returnTo=' + encodeURIComponent(returnTo || defaultReturnTo());
  try { window.location.href = target; } catch (_err) { /* ignore */ }
  clearTimeout(window.daygleAuth._redirectTimer);
  window.daygleAuth._redirectTimer = setTimeout(() => {
    if (!window.daygleAuth) return;
    window.daygleAuth.redirecting = false;
    window.daygleAuth._redirectTimer = null;
  }, 250);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  // Attach the CSRF token only for state-changing verbs; GETs don't need it.
  const method = (options.method || 'GET').toUpperCase();
  if (window.daygleAuth.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    headers['X-CSRF-Token'] = window.daygleAuth.csrfToken;
  }
  // Only auto-set Content-Type for non-FormData bodies. FormData requires the
  // browser to inject the multipart boundary itself; an explicit
  // 'application/json' header would strip that boundary and break file uploads
  // (e.g. the database-restore endpoint in settings.js).
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  let response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    handleSessionLoss('Authentication required', defaultReturnTo());
    throw new Error('Authentication required');
  }
  const payload = await response.json().catch(() => {});
  // 403 with CSRF-related detail text usually means a STALE cached token
  // (another tab or an overlapping refresh rotated the session's token) -
  // NOT a dead session, which is why GETs keep working. Self-heal: re-read
  // the session's current token via /api/auth/me and retry ONCE. Only if
  // the retry also fails do we declare the session lost.
  //
  // Admin-role denials surface as ``Admin access required`` and
  // intentionally do NOT match the regex below, so a non-admin hitting an
  // admin endpoint gets the original 403 toast, not a login redirect or a
  // pointless refresh+retry.
  //
  // The ``window.daygleAuth?.user`` guard was REMOVED because a
  // concurrent in-flight request may have already cleared auth state
  // (via a prior 401 → handleSessionLoss → setApiAuth(null, null, null))
  // before this 403 arrives. The server's error message alone is the
  // authoritative signal - if it says CSRF (after one recovery attempt),
  // the session is gone.
  if (response.status === 403) {
    const detail = String((payload && payload.detail) || '');
    if (/csrf|invalid.?token|missing.?cookie|invalid.?x-csr/i.test(detail)) {
      const retried = await retryAfterCsrfRefresh(path, options);
      if (retried && retried.status !== 403) {
        const retryPayload = await retried.json().catch(() => {});
        if (retried.ok) return retryPayload || {};
        throw new Error((retryPayload && retryPayload.detail) || `Request failed: ${retried.status}`);
      }
      // Recovery failed (refresh errored / no token / still mismatched):
      // fall through to session-loss handling below.
      handleSessionLoss('Session expired - please sign in again', defaultReturnTo());
      throw new Error('Session expired');
    }
  }
  if (!response.ok) {
    throw new Error((payload && payload.detail) || `Request failed: ${response.status}`);
  }
  return payload || {};
}
window.api = api;

// ─── CSRF self-heal ──────────────────────────────────────────────────────
// A CSRF-mismatch 403 on a mutating request usually means this tab's cached
// X-CSRF-Token went stale (another tab or an overlapping refresh rotated the
// session's token). That is NOT a dead session - the cookie is still valid,
// which is exactly why GETs keep working. Re-fetch /api/auth/me to pick up
// the session's current token and retry ONCE before declaring the session
// lost. Returns the retried Response, or null when recovery failed.
async function retryAfterCsrfRefresh(path, options) {
  try {
    await refreshDaygleAuth();
  } catch (_err) {
    return null;
  }
  const freshToken = window.daygleAuth?.csrfToken;
  if (!freshToken) return null;
  const headers = { ...(options.headers || {}) };
  headers['X-CSRF-Token'] = freshToken;
  if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  try {
    return await fetch(path, { ...options, headers });
  } catch (_err) {
    return null;
  }
}

// ─── Background auth refresh ──────────────────────────────────────────────
// Single source of truth used by nav.js's daygleAuthReady IIFE *and* by
// visibilitychange / focus-driven refresh. Re-runs the same /api/auth/me
// fetch and pipes the result through setApiAuth (now extended to carry
// expiresAt). After every successful refresh, schedule the next one ~1
// minute before the server-stated expiry so the CSRF token is rotated
// before it goes stale, and never after the user has been logged out.
//
// Transient network failures keep the last-known auth state intact so
// the user doesn't get bounced on a flaky connection; only a real 401
// triggers handleSessionLoss.
async function refreshDaygleAuth() {
  let response;
  try {
    response = await fetch('/api/auth/me', { credentials: 'same-origin' });
  } catch (_err) {
    return window.daygleAuth?.user ? { user: window.daygleAuth.user, csrfToken: window.daygleAuth.csrfToken, expiresAt: window.daygleAuth.expiresAt || '' } : null;
  }
  if (response.status === 401) {
    handleSessionLoss('Session expired - please sign in again', defaultReturnTo());
    return null;
  }
  if (!response.ok) {
    return window.daygleAuth?.user ? { user: window.daygleAuth.user, csrfToken: window.daygleAuth.csrfToken, expiresAt: window.daygleAuth.expiresAt || '' } : null;
  }
  let payload = null;
  try { payload = await response.json(); } catch (_err) { return null; }
  const user = payload?.user || null;
  const csrfToken = payload?.csrf_token || '';
  const expiresAt = payload?.expires_at || '';
  setApiAuth(user, csrfToken, expiresAt);
  scheduleNextAuthRefresh();
  if (user && csrfToken) {
    broadcastAuthStateToOtherTabs(user, csrfToken, expiresAt);
  }
  return { user, csrfToken, expiresAt };
}

function scheduleNextAuthRefresh() {
  if (!window.daygleAuth) return;
  clearTimeout(window.daygleAuth._refreshTimer);
  const exp = window.daygleAuth.expiresAt;
  if (!exp) return;
  const ms = Date.parse(exp) - Date.now() - 60_000;
  if (!Number.isFinite(ms)) return;
  if (ms <= 0) {
    // Already past the soft window - refresh now (network permitting).
    window.daygleAuth._refreshTimer = setTimeout(refreshDaygleAuth, 0);
  } else {
    window.daygleAuth._refreshTimer = setTimeout(refreshDaygleAuth, ms);
  }
}

// localStorage is the cross-tab transport. The ``storage`` event in every
// OTHER open tab fires when one tab writes; the writing tab itself does
// NOT receive the event (so this naturally avoids double-fetching within
// the same tab on every refresh).
const DAYGLE_AUTH_STORAGE_KEY = 'daygle.auth.v1';
function broadcastAuthStateToOtherTabs(user, csrfToken, expiresAt) {
  try {
    localStorage.setItem(
      DAYGLE_AUTH_STORAGE_KEY,
      JSON.stringify({ u: user?.username || '', csrf: csrfToken, exp: expiresAt || '', ts: Date.now() }),
    );
  } catch (_err) { /* storage disabled / quota - silently no-op */ }
}
function subscribeDaygleAuthCrossTabs() {
  window.addEventListener('storage', (event) => {
    if (event.key !== DAYGLE_AUTH_STORAGE_KEY || !event.newValue) return;
    let parsed;
    try { parsed = JSON.parse(event.newValue); } catch (_err) { return; }
    if (!parsed) return;
    // Don't clobber a logged-in user with a logout-shaped event when the
    // payload has no csrf; treat missing csrf as "session ended".
    if (!parsed.csrf) {
      if (typeof handleSessionLoss === 'function') {
        handleSessionLoss('Session expired - please sign in again', defaultReturnTo());
      }
      return;
    }
    // Keep the local CSRF token intact - it belongs to THIS tab's session
    // cookie. Overwriting with the remote tab's token causes 403 CSRF
    // mismatches when the two tabs hold different sessions (e.g. one tab
    // re-logged in after its session expired while the other tab still has
    // its original, still-valid session). Only update the user object and
    // expiry so the UI stays consistent; the CSRF token is refreshed on the
    // next local /api/auth/me fetch.
    const userObj = (window.daygleAuth?.user) || null;
    const existingCsrf = (window.daygleAuth && window.daygleAuth.csrfToken) || null;
    setApiAuth(userObj, existingCsrf, parsed.exp || '');
    if (typeof scheduleNextAuthRefresh === 'function') scheduleNextAuthRefresh();
  });
}
subscribeDaygleAuthCrossTabs();

// ─── Detection pill rendering (shared by dashboard, recordings, timeline) ─
// Sound class identifiers (mirror SOUND_CLASSES in app/sound_detector.py). A
// detection label that matches one of these is a sound, even when it appears
// on an object list, so its pill carries the speaker icon rather than the eye.
const SOUND_CLASS_IDS = new Set([
  'cat_meow', 'dog_bark', 'glass_breaking', 'smoke_alarm',
  'baby_crying', 'doorbell', 'car_alarm', 'loud_bang',
]);

function isSoundLabel(label) {
  return SOUND_CLASS_IDS.has(String(label || '').trim().toLowerCase().replace(/\s+/g, '_'));
}

const DETECTION_EYE_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>';

// Running-man icon (lucide-style "user-running") used for motion-only
// detections, recordings and dashboard items. Sized for inline pills (11px);
// callers that need a larger standalone icon can wrap it in their own svg.
const DETECTION_MOTION_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="m4 19.5 4-4.5 1.5 4 5.5-3-2-7 4-3"/></svg>';

// Small clock icon (lucide-style "timer") for the still-alert badge. Same
// 11px inline sizing as the eye / running-man pill icons.
const DETECTION_CLOCK_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>';

// Rotating-arrows glyph for the "Continuous" recording pill (always-on
// capture, no triggering detection).
const DETECTION_CONTINUOUS_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>';

// Same icon, scaled up for row-level list rendering (recordings row icon).
const MOTION_RUNNING_ROW_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="m4 19.5 4-4.5 1.5 4 5.5-3-2-7 4-3"/></svg>';

// Generic trigger labels are not concrete object classes - they describe
// the trigger condition that caused the recording/event/alert instead of
// naming a recognised object ('motion', 'alert', 'object', 'human', and
// the recording-mode placeholders 'none', 'off', 'continuous'). Used to
// distinguish "motion-only" frames from frames carrying a real object label,
// so the recordings list, the dashboard activity feed and the timeline
// page all agree on which clips qualify as pure motion.
const GENERIC_TRIGGER_LABELS = new Set(['motion', 'alert', 'human', 'object', 'none', 'off', 'continuous']);
function isGenericTriggerLabel(label) {
  return GENERIC_TRIGGER_LABELS.has(String(label || '').trim().toLowerCase());
}

// Render a single detection pill (eye icon for objects, speaker for sounds).
// Each label decides its own icon independently of `isSound` so a sound class
// that sneaks into an object list still renders with the speaker icon.
function detectionPill(label, confidence, isSound = false, count = 1) {
  const labelIsSound = isSound || isSoundLabel(label);
  const display = labelIsSound
    ? titleCase(String(label).replace(/_/g, ' '))
    : titleCase(String(label));
  const numericConfidence = confidence == null ? NaN : Number(confidence);
  const confidenceText = Number.isFinite(numericConfidence)
    ? ` · ${Math.round(numericConfidence * 100)}%`
    : '';
  // When the same label was detected across several events in one clip, a
  // single pill stands in for all of them (best confidence) plus a "×N"
  // multiplier -- so two Dog Bark events read as one "Dog Bark · 89% ×2"
  // pill instead of two near-identical pills stacked on the row.
  const numericCount = Math.round(Number(count));
  const countText = Number.isFinite(numericCount) && numericCount > 1
    ? ` <span class="detection-count">×${numericCount}</span>`
    : '';
  if (labelIsSound) {
    return `<span class="detection detection-sound">🔊 ${escapeHtml(display)}${confidenceText}${countText}</span>`;
  }
  return `<span class="detection detection-object">${DETECTION_EYE_ICON} ${escapeHtml(display)}${confidenceText}${countText}</span>`;
}

// Amber "Still N Min" badge for a detection that fired a still-dwell alert
// (the Objects page "still for N minutes" setting): the object had been
// detected continuously still for N minutes -- a package left in view, a pet
// that settled down. Rendered beside the object pill on the dashboard and
// events list; the badge is absent for ordinary detections.
function stillAlertBadge(minutes) {
  const parsed = Number.parseInt(minutes, 10);
  const n = Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  const label = n === null ? 'Still' : `Still ${n} Min`;
  const title = n === null ? 'Still alert' : `Still alert: detected continuously still for ${n} minutes`;
  return `<span class="detection detection-still-alert" title="${escapeHtml(title)}">${DETECTION_CLOCK_ICON} ${label}</span>`;
}

// Render the teal/green "Motion" pill (running-man icon + optional motion
// intensity confidence). Shared by the recordings list, dashboard activity
// feed and timeline so every surface shows the same chip styling.
function motionPill(confidence = null) {
  const numericConfidence = confidence == null ? NaN : Number(confidence);
  const confidenceText = Number.isFinite(numericConfidence)
    ? ` · ${Math.round(numericConfidence * 100)}%`
    : '';
  return `<span class="detection detection-motion">${DETECTION_MOTION_ICON} Motion${confidenceText}</span>`;
}

// Render the neutral "Continuous" pill for always-on recording chunks.
// Continuous recordings carry no triggering detection, so the recordings
// list shows this chip (instead of the bare "No detections" fallback that
// reads as broken) to say the clip is a scheduled continuous segment.
function continuousPill() {
  return `<span class="detection detection-continuous">${DETECTION_CONTINUOUS_ICON} Continuous</span>`;
}

// ── Face-identity pills + filters (Recordings + Snapshots) ───────────────
// Face recognition annotates person detections in the live loop and stores a
// compact summary on the event metadata as
//   metadata.face_identities = { people: [{person_id, name, ...}], unknown: N }
// (see app/face_identity.py::face_identity_metadata). Faces are NOT a new
// top-level detection type -- a face only exists because a person was
// detected -- so these helpers render identity as an inline pill on the
// existing person/object row and let the pages filter by "which person" or
// "Unknown". Events that ran no face through recognition carry no
// ``face_identities`` key, so every helper below is a no-op for them and
// non-face deployments see nothing new.
const DETECTION_FACE_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';

// Normalise a stored ``face_identities`` object into
//   { people: Map(key -> {key, personId, name, count}), unknown: N }
// collapsing duplicate people (same person on several tracks/events) and
// coercing any malformed shape to empty. ``key`` is the stable filter token
// shared by the dropdown option value and the match test below: ``id:<pid>``
// for an enrolled person (falling back to ``name:<lower>`` for a legacy row
// that stored a name but no id).
function normalizeFaceIdentities(faceIdentities) {
  const fi = faceIdentities && typeof faceIdentities === 'object' ? faceIdentities : {};
  const people = new Map();
  const rawPeople = Array.isArray(fi.people) ? fi.people : [];
  for (const person of rawPeople) {
    if (!person || typeof person !== 'object') continue;
    const name = String(person.name || '').trim();
    const key = person.person_id != null
      ? `id:${person.person_id}`
      : (name ? `name:${name.toLowerCase()}` : '');
    if (!key) continue;
    const existing = people.get(key);
    if (existing) {
      existing.count += 1;
    } else {
      people.set(key, {
        key,
        personId: person.person_id != null ? person.person_id : null,
        name: name || 'Unknown person',
        count: 1,
      });
    }
  }
  const unknown = Math.max(0, Math.round(Number(fi.unknown) || 0));
  return { people, unknown };
}

function eventFaceIdentities(event) {
  const meta = (event && event.metadata) || {};
  return normalizeFaceIdentities(meta.face_identities);
}

// Merge the identities across every event linked to a recording -- the
// triggering ``event`` plus the clip's ``events`` array -- deduped by event id
// so an event that is both the trigger and a member is not counted twice. A
// person's ``count`` becomes the number of distinct events they appeared in
// over the clip (a "sightings" tally), which is why the recordings row renders
// the Unknown pill without a numeric count (see ``faceIdentityPills``): summing
// per-frame unknown faces across events would overstate how many strangers
// there were, while "seen" is honest.
function collectRecordingFaceIdentities(recording) {
  const merged = { people: new Map(), unknown: 0 };
  const seen = new Set();
  const sources = [];
  const pushSource = (event) => {
    if (!event || typeof event !== 'object') return;
    const id = event.id;
    if (id != null && seen.has(id)) return;
    if (id != null) seen.add(id);
    sources.push(event);
  };
  if (recording) {
    pushSource(recording.event);
    if (Array.isArray(recording.events)) recording.events.forEach(pushSource);
  }
  for (const source of sources) {
    const { people, unknown } = eventFaceIdentities(source);
    if (unknown > 0) merged.unknown += 1;
    for (const [key, person] of people) {
      const existing = merged.people.get(key);
      if (existing) existing.count += 1;
      else merged.people.set(key, { ...person, count: 1 });
    }
  }
  return merged;
}

function asNormalizedFaceIdentities(identities) {
  return identities && identities.people instanceof Map
    ? identities
    : normalizeFaceIdentities(identities);
}

// Render identity pills from either a raw ``face_identities`` object or an
// already-normalised structure. ``countUnknown`` is false for recordings
// (where the merged unknown tally is a sightings count, not a headcount) so
// the Unknown pill shows no ``×N`` there; snapshots (a single event/frame)
// pass the default true so "Unknown ×2" reads exactly.
function faceIdentityPills(identities, options = {}) {
  const norm = asNormalizedFaceIdentities(identities);
  const countUnknown = options.countUnknown !== false;
  const pills = [];
  for (const person of norm.people.values()) {
    const countText = person.count > 1 ? ` <span class="detection-count">×${person.count}</span>` : '';
    pills.push(`<span class="detection detection-face" title="Recognised person">${DETECTION_FACE_ICON} ${escapeHtml(person.name)}${countText}</span>`);
  }
  if (norm.unknown > 0) {
    const countText = countUnknown && norm.unknown > 1 ? ` <span class="detection-count">×${norm.unknown}</span>` : '';
    pills.push(`<span class="detection detection-face-unknown" title="Face not matched to an enrolled person">${DETECTION_FACE_ICON} Unknown${countText}</span>`);
  }
  return pills.join('');
}

// True when ``identities`` satisfies a face-filter selection. Values:
//   ''         -> no face filter (always matches)
//   'any'      -> any recognised or unknown face present
//   'unknown'  -> at least one unrecognised face
//   'id:<pid>' / 'name:<lower>' -> that specific enrolled person present
function matchesFaceFilter(identities, value) {
  if (!value) return true;
  const norm = asNormalizedFaceIdentities(identities);
  if (value === 'any') return norm.people.size > 0 || norm.unknown > 0;
  if (value === 'unknown') return norm.unknown > 0;
  return norm.people.has(value);
}

// A recording is "motion-only" when:
//  * it isn't a sound recording,
//  * no concrete object label is attached to it (the join-table labels
//    array and the per-event detections are both empty once the generic
//    trigger words are filtered out), and
//  * it isn't an always-on chunk (see ``isContinuousOnlyRecording``) -
//    a label-less event clip is motion-only even when continuous mode
//    stamped its trigger_type as 'continuous'.
//
// Used by every surface that renders a recording (recordings list, the
// playback modal on both pages, and the timeline) so the boundary lives in
// exactly one place.
function isMotionOnlyRecording(recording) {
  if (!recording) return false;
  if (recording?.event?.metadata?.source === 'sound-detection') return false;
  const labelCandidates = [];
  if (Array.isArray(recording.labels)) labelCandidates.push(...recording.labels);
  if (Array.isArray(recording.detections)) {
    for (const d of recording.detections) labelCandidates.push(d?.label);
  }
  const hasConcrete = labelCandidates.some((label) => {
    const normalized = String(label || '').trim().toLowerCase();
    return normalized && !GENERIC_TRIGGER_LABELS.has(normalized);
  });
  if (hasConcrete) return false;
  // Always-on chunks (no triggering event, no trigger label) are classified
  // as continuous, not motion; a label-less event clip is motion-only even
  // when continuous mode stamped its trigger_type as 'continuous'.
  if (isContinuousOnlyRecording(recording)) return false;
  return true;
}

// A recording is "continuous-only" when it is an always-on capture chunk: a
// trigger_type 'continuous' / 'none' / 'off' recording with no linked
// triggering event and no trigger label. This is the partner of
// ``isMotionOnlyRecording``: together they split the recording space into
// event-triggered clips (motion/object/sound) and always-on segments.
// Always-on chunks are identified structurally (no event link), NOT by the
// absence of object labels - a 1-hour chunk that happened to catch an object
// is still a continuous recording and must stay on the Continuous card.
function isContinuousOnlyRecording(recording) {
  if (!recording) return false;
  if (recording?.event?.metadata?.source === 'sound-detection') return false;
  // Event clips recorded while continuous mode is enabled are also stamped
  // trigger_type='continuous' (with the event's trigger label), so the event
  // link + label are what separate them from real always-on chunks.
  if (recording.event_id !== null && recording.event_id !== undefined) return false;
  if (recording.trigger_label) return false;
  const triggerType = String(recording.trigger_type || 'motion').trim().toLowerCase();
  return ['continuous', 'none', 'off'].includes(triggerType);
}

// Motion intensity lives as a 'motion'-labelled entry on either the event
// detections or any track frame. Surface the strongest one so the
// recordings list / modal / timeline can render "Motion · NN%" alongside
// the concrete-object pills.
function motionConfidenceFor(recording) {
  let best = null;
  const consider = (det) => {
    if (!det) return;
    if (String(det.label || '').trim().toLowerCase() !== 'motion') return;
    const c = Number(det.confidence);
    if (!Number.isFinite(c)) return;
    if (best === null || c > best) best = c;
  };
  for (const d of (recording?.detections || [])) consider(d);
  for (const sample of (recording?.track || [])) {
    for (const d of (sample?.detections || [])) consider(d);
  }
  return best;
}

// Mixed recordings keep their object classification, but still need a
// separate Motion pill anywhere their saved event/track contains frame motion.
// Keeping this reader beside motionConfidenceFor prevents Events, Recordings,
// Snapshots and the timeline from drifting on multi-event clips.
function recordingHasMotion(recording) {
  return motionConfidenceFor(recording) !== null
    || (recording?.detections || []).some((d) => String(d?.label || '').trim().toLowerCase() === 'motion')
    || (recording?.track || []).some((sample) => (sample?.detections || []).some((d) => String(d?.label || '').trim().toLowerCase() === 'motion'));
}

// ---------------------------------------------------------------------------
// Motion-vs-object boundary helpers - shared by every page that renders a
// motion / object / sound split (recordings, recordings modal, timeline,
// dashboard activity feed, stat cards). Each helper takes the exact payload
// shape the corresponding API or item builder produces, so the tests can
// pin behaviour at every layer without re-implementing the logic.
//
// "Motion-only" means:
//   * it isn't a sound recording / event (sound source or sound class
//     label), AND
//   * every detection / label sits inside GENERIC_TRIGGER_LABELS (motion,
//     alert, human, object, none, off, continuous) - i.e. no concrete
//     object or sound class labels.
//
// Edge cases: events / recordings with zero detections / labels are NOT
// classified as motion-only - they're just under-recorded samples and
// shouldn't pull the counts.
// ---------------------------------------------------------------------------
function _hasOnlyGenericLabels(labels) {
  const normalized = (Array.isArray(labels) ? labels : []).map((entry) => {
    if (typeof entry === 'string') return String(entry || '').trim().toLowerCase();
    return String(entry?.label || '').trim().toLowerCase();
  }).filter(Boolean);
  if (!normalized.length) return false;
  return normalized.every(isGenericTriggerLabel);
}

function isMotionOnlyEvent(event) {
  if (!event) return false;
  if (event.source === 'sound') return false;
  return _hasOnlyGenericLabels(event.detections);
}

function isMotionOnlyEventItem(item) {
  if (!item || item.isSound) return false;
  return _hasOnlyGenericLabels(item.detections);
}

// ─── Shared recording helpers (recordings list + timeline) ────────────────
// The /api/recordings and /api/recordings/timeline endpoints return the same
// recording shape, so the recordings list and the timeline page share these
// readers here instead of keeping parallel copies that can silently drift
// (the Dog Bark legend bug was exactly such a drift). Page-specific bits that
// depend on per-page state - configuredLabels filtering, timeline colours -
// deliberately stay in their own files.
function isSoundRecording(recording) {
  return recording?.event?.metadata?.source === 'sound-detection';
}

function recordingTriggerType(recording) {
  return String(recording.trigger_type || 'motion').trim().toLowerCase() || 'motion';
}

function recordingTriggerLabel(recording) {
  return String(recording.trigger_label || '').trim().toLowerCase() || null;
}

function recordingZoneNames(recording) {
  if (isSoundRecording(recording)) return [];
  const names = new Set();
  const remember = (detection) => {
    const zoneName = String(detection?.zone_name || '').trim();
    if (zoneName) names.add(zoneName);
  };
  for (const d of (recording.detections || [])) remember(d);
  // The live detection track (recording.track, loaded for the playback modal
  // and timeline) can carry zone names for objects detected after the trigger
  // event. Fold those in so the playback-card Zone row is complete even when
  // the event's detections table row lacks a zone name.
  for (const sample of (recording.track || [])) {
    for (const d of (sample?.detections || [])) remember(d);
  }
  return [...names];
}

// Returns an array of { label, confidence } sorted by confidence descending.
// Sound recordings collapse to their single class label; object recordings
// surface one entry per unique detected label (deduped via recording.labels
// and the live track), each carrying its best-seen confidence.
function recordingDetectionSummary(recording) {
  if (isSoundRecording(recording)) {
    return soundDetectionSummary(recording);
  }
  // Build best-confidence map from the saved event detections and, when
  // present, the clip's live detection track. Multi-object recordings can pick
  // up additional labels while the clip is extended; those labels are persisted
  // in recording.labels, but their confidence may only exist in the track.
  const best = new Map();
  const rememberBest = (detection) => {
    const label = String(detection?.label || '').trim().toLowerCase();
    if (!label) return;
    const rawConfidence = Number(detection?.confidence);
    if (!Number.isFinite(rawConfidence)) return;
    if (!best.has(label) || rawConfidence > best.get(label)) best.set(label, rawConfidence);
  };
  for (const d of (recording.detections || [])) rememberBest(d);
  for (const sample of (recording.track || [])) {
    for (const d of (sample?.detections || [])) rememberBest(d);
  }
  // Persisted per-label confidence (recording_labels.confidence) covers secondary
  // objects that only appeared after the trigger, whose confidence is otherwise
  // absent from the event detections the list endpoints load.
  for (const [label, confidence] of Object.entries(recording.label_confidences || {})) {
    rememberBest({ label, confidence });
  }
  // Count how many distinct events contributed each concrete label so the
  // recordings list can show "Person ×2" when the same object triggered
  // several events inside one clip, rather than repeating the pill. Counting
  // per-event (not per raw detection) keeps the multiplier meaningful -- a
  // frame-by-frame track would otherwise inflate it into the hundreds.
  const eventCounts = labelEventCounts(recording);
  // Use recording.labels as the authoritative label list when available.
  const authLabels = Array.isArray(recording.labels) && recording.labels.length
    ? recording.labels.map((l) => String(l || '').trim().toLowerCase()).filter((l) => l && !GENERIC_TRIGGER_LABELS.has(l))
    : Array.from(best.keys()).filter((l) => !GENERIC_TRIGGER_LABELS.has(l));
  return authLabels
    .map((label) => ({
      label,
      confidence: best.has(label) ? best.get(label) : null,
      count: eventCounts.get(label) || 1,
    }))
    .sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1));
}

// Number of distinct events in a clip that carried each concrete object label.
// Each event contributes at most one increment per label (a Set per event) so
// a single event that localised the same object across many frames counts once.
function labelEventCounts(recording) {
  const counts = new Map();
  for (const event of (Array.isArray(recording?.events) ? recording.events : [])) {
    const detections = Array.isArray(event?.detections) ? event.detections : [];
    const labelsInEvent = new Set(
      detections
        .map((d) => String(d?.label || '').trim().toLowerCase())
        .filter((label) => label && !GENERIC_TRIGGER_LABELS.has(label)),
    );
    for (const label of labelsInEvent) counts.set(label, (counts.get(label) || 0) + 1);
  }
  return counts;
}

// Aggregate a sound recording's linked events into one entry per sound class,
// carrying the strongest confidence seen and a count of how many events fired
// that class. A clip that logged two dog barks reads as one "Dog Bark ×2" pill
// instead of two separate near-identical pills. Falls back to the recording's
// primary event metadata when no per-event detections are attached.
function soundDetectionSummary(recording) {
  const agg = new Map();
  const add = (rawLabel, confidence) => {
    const label = String(rawLabel || '').trim().toLowerCase();
    if (!label) return;
    const numeric = Number(confidence);
    const conf = Number.isFinite(numeric) ? numeric : null;
    const current = agg.get(label);
    if (!current) {
      agg.set(label, { confidence: conf, count: 1 });
      return;
    }
    current.count += 1;
    if (conf != null && (current.confidence == null || conf > current.confidence)) {
      current.confidence = conf;
    }
  };
  for (const event of (Array.isArray(recording?.events) ? recording.events : [])) {
    const detections = Array.isArray(event?.detections) ? event.detections : [];
    const soundDetections = detections.filter((d) => isSoundLabel(d && d.label));
    if (soundDetections.length) {
      for (const d of soundDetections) add(d.label, d.confidence);
      continue;
    }
    const meta = event?.metadata || {};
    const label = meta.class_label || meta.label || event?.trigger_label;
    if (label) add(label, typeof meta.confidence === 'number' ? meta.confidence : null);
  }
  if (!agg.size) {
    const meta = recording.event?.metadata || {};
    const label = (meta.class_label || meta.label || recording.trigger_label || 'sound').toLowerCase();
    return [{ label, confidence: Number(meta.confidence || 0), count: 1 }];
  }
  return Array.from(agg.entries())
    .map(([label, value]) => ({ label, confidence: value.confidence, count: value.count }))
    .sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1));
}

// Render one type pill for a single event inside a recording (object /
// motion / sound), mirroring the Events page's boundary: a sound source or
// sound-class detection renders the speaker pill, an event whose detections
// are all generic markers (motion) renders the Motion pill, and anything
// else renders the strongest concrete object label. The Recordings list uses
// this per-event to show what a multi-event clip actually contains -- one
// pill per linked event -- instead of collapsing them into a bare "N events"
// count. The payload shape is the event dict that /api/recordings attaches to
// ``recording.events`` (each with its own ``detections`` + ``metadata``).
function recordingEventPills(event) {
  if (!event) return '';
  const detections = Array.isArray(event.detections) ? event.detections : [];
  if (String(event.source || '').toLowerCase() === 'sound'
      || event.metadata?.source === 'sound-detection'
      || detections.some((d) => isSoundLabel(d && d.label))) {
    const soundDetections = detections.filter((d) => isSoundLabel(d && d.label));
    if (soundDetections.length) {
      return soundDetections
        .map((d) => detectionPill(d.label, d.confidence, true))
        .join('');
    }
    const meta = event.metadata || {};
    const label = meta.class_label || meta.label || event.trigger_label;
    const confidence = typeof meta.confidence === 'number' ? meta.confidence : null;
    return label ? detectionPill(label, confidence, true) : '';
  }
  // Motion-only event: every detection is a generic marker (motion), so the
  // single Motion pill with the strongest intensity stands in for the event.
  const concrete = detections.filter((d) => {
    const label = String(d && d.label || '').trim().toLowerCase();
    return label && !GENERIC_TRIGGER_LABELS.has(label);
  });
  if (!concrete.length) {
    const strongest = detections
      .filter((d) => String(d && d.label || '').trim().toLowerCase() === 'motion')
      .reduce((best, d) => (d && Number(d.confidence) > (best ? Number(best.confidence) : -1) ? d : best), null);
    return strongest ? motionPill(strongest.confidence) : '';
  }
  // Object event: the strongest concrete detection of THIS event (plus its
  // still-alert badge when the event fired a dwell alert). The clip-level
  // Motion pill in the row already covers frame motion, so no duplicate here.
  const strongestObject = concrete
    .slice()
    .sort((a, b) => Number(b.confidence || 0) - Number(a.confidence || 0))[0];
  return detectionPill(strongestObject.label, strongestObject.confidence)
    + (strongestObject.still_alert ? stillAlertBadge(strongestObject.still_alert_minutes) : '');
}

// ─── Shared log table formatting ──────────────────────────────────────────
// Audit + camera-log entries use the same locale-aware "Nov 4, 2025, 12:30:45"
// format. Centralised here so a future tweak (e.g. honouring the user's
// time-format preference on these lists) lands in one place rather than two.
// Log entries come from the audit_log + camera_log tables which store the
// wall-clock ISO timestamp the operator should see. ``formatDate`` honours
// the operator's preferred date/time format (via
// ``formatUserDate`` + ``formatUserTime``) and timezone
// (``window.daygleDatePrefs.timezone``). The previous implementation called
// ``.toLocaleString()`` with a fixed English-locale option object that
// ignored the operator preference - the camera-log and audit pages were
// out of step with the rest of the dashboard. Delegating here keeps both
// pages consistent with live / recordings / timeline in one place.
function formatLogTime(iso) {
  if (!iso) return '-';
  try {
    return formatDate(iso);
  } catch {
    return String(iso || '-');
  }
}

// ─── Shared log pagination size ───────────────────────────────────────────
// Audit + camera-log pages paginate identically (50 rows / page). Kept here
// so the limits stay in sync; if one ever needs to change the other follows.
const LOG_PAGE_SIZE = 50;

function renderTimeSelect(value, dataAttr, dataAttrValue) {
  const [hStr, mStr] = (value || '').split(':');
  const selH = hStr !== undefined && hStr !== '' ? parseInt(hStr, 10) : -1;
  const selM = mStr !== undefined && mStr !== '' ? parseInt(mStr, 10) : -1;
  const use12h = (window.daygleDatePrefs || {}).timeFormat === '12h';
  const minutes = Array.from({ length: 12 }, (_, i) => i * 5)
    .map((m) => `<option value="${String(m).padStart(2, '0')}"${selM === m ? ' selected' : ''}>${String(m).padStart(2, '0')}</option>`)
    .join('');

  let hourOpts;
  let ampmSel = '';
  if (use12h) {
    const isPm = selH >= 12;
    const h12 = selH < 0 ? -1 : selH % 12 === 0 ? 12 : selH % 12;
    hourOpts = Array.from({ length: 12 }, (_, i) => i + 1)
      .map((h) => `<option value="${h}"${h12 === h ? ' selected' : ''}>${h}</option>`)
      .join('');
    ampmSel = `<select class="time-select-ampm"><option value="am"${!isPm && selH >= 0 ? ' selected' : ''}>AM</option><option value="pm"${isPm ? ' selected' : ''}>PM</option></select>`;
  } else {
    hourOpts = Array.from({ length: 24 }, (_, i) => `<option value="${String(i).padStart(2, '0')}"${selH === i ? ' selected' : ''}>${String(i).padStart(2, '0')}</option>`).join('');
  }

  return `<span class="time-select-wrap" ${dataAttr}="${escapeHtml(dataAttrValue)}"><select class="time-select-hour"><option value="">--</option>${hourOpts}</select><span class="time-select-colon">:</span><select class="time-select-minute"><option value="">--</option>${minutes}</select>${ampmSel}</span>`;
}

function timeSelectValue(wrap) {
  const hRaw = wrap.querySelector('.time-select-hour').value;
  const m = wrap.querySelector('.time-select-minute').value;
  if (!hRaw || !m) return null;
  const ampmEl = wrap.querySelector('.time-select-ampm');
  if (ampmEl) {
    let h = parseInt(hRaw, 10) % 12;
    if (ampmEl.value === 'pm') h += 12;
    return `${String(h).padStart(2, '0')}:${m}`;
  }
  return `${hRaw}:${m}`;
}

// Writes an HH:MM value into a renderTimeSelect picker. Snaps minutes to
// the nearest 5-minute step and clamps the result to 23:55 so deep-link
// / clock values that can't be represented (e.g. "14:23" -> "14:25",
// "23:59" -> "23:55") still land on a choice the picker can display.
// Mirrors the 12h↔24h conventions used by timeSelectValue and
// renderTimeSelect: hour 0 (midnight) and hour 12 (noon) both surface
// as "12" with the matching AM/PM, and the 24h writer pads to two
// digits ("00"-"23"). Programmatic writes do NOT fire a 'change' event
// on <select>, so callers that need the surrounding UI to react
// (loadRecordings, loadTimeline) invoke their own handler.
function setTimeSelectValue(wrap, hhmm) {
  if (!wrap) return;
  const [hStr, mStr] = String(hhmm || '').split(':');
  const h = parseInt(hStr, 10);
  if (!Number.isFinite(h)) return;
  const totalMinutes = h * 60 + (parseInt(mStr, 10) || 0);
  const clampedTotal = Math.max(0, Math.min(1439, totalMinutes));
  const snappedMinutes = Math.min(1435, Math.round(clampedTotal / 5) * 5);
  const snappedHour = Math.floor(snappedMinutes / 60);
  const snappedMinute = snappedMinutes % 60;
  const minuteEl = wrap.querySelector('.time-select-minute');
  const hourEl = wrap.querySelector('.time-select-hour');
  const ampmEl = wrap.querySelector('.time-select-ampm');
  if (minuteEl) minuteEl.value = String(snappedMinute).padStart(2, '0');
  if (ampmEl) {
    const h12 = snappedHour % 12 || 12;
    if (hourEl) hourEl.value = String(h12);
    ampmEl.value = snappedHour >= 12 ? 'pm' : 'am';
  } else if (hourEl) {
    hourEl.value = String(snappedHour).padStart(2, '0');
  }
}

function titleCase(value) {
  return String(value || '')
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(' ');
}

// Normalise the persisted shape of an email-recipients field so the
// /sounds and /zones rule editors agree on what the backend needs to see.
// Inputs arrive in two shapes: (1) a comma-separated string from the rule
// row's <input type="email"> field, (2) a list of strings from the JSON
// round-trip. Both collapse to the canonical ``Array<string> of trimmed,
// non-empty addresses`` that the API expects.
function normalizeEmailList(value) {
  const source = Array.isArray(value) ? value : String(value || '').split(',');
  return source.map((recipient) => String(recipient).trim()).filter(Boolean);
}

// ─── User display preferences (date_format / time_format) ──────────────────
// Populated by nav.js after /api/auth/me resolves, but exposed as early as
// possible so every page (dashboard, events, alerts, recordings, etc.) renders
// timestamps the way the user configured on the Profile page. Defaults match
// the previous toLocaleString() behaviour so any caller that runs before the
// auth fetch settles still gets a sensible value.
window.daygleDatePrefs = window.daygleDatePrefs || { dateFormat: 'locale', timeFormat: '24h' };

function setDaygleDatePrefs(prefs) {
  if (!prefs) return;
  if (prefs.date_format) window.daygleDatePrefs.dateFormat = prefs.date_format;
  else if (prefs.dateFormat) window.daygleDatePrefs.dateFormat = prefs.dateFormat;
  if (prefs.time_format) window.daygleDatePrefs.timeFormat = prefs.time_format;
  else if (prefs.timeFormat) window.daygleDatePrefs.timeFormat = prefs.timeFormat;
  // Pages that already render can opt-in to a refresh hook (e.g. to redraw
  // timestamps after the user changes their profile). The hook is no-op by
  // default; pages override it on demand.
  if (typeof window.daygleDatePrefsChanged === 'function') {
    try { window.daygleDatePrefsChanged(window.daygleDatePrefs); } catch (_err) { /* ignore */ }
  }
}

// ─── Cross-tab broadcast (Profile page → every other open Daygle tab) ──────
// Used after the user saves a new date_format / time_format on the Profile
// page. BroadcastChannel gives instant in-tab delivery on modern browsers;
// localStorage fires `storage` events on every other tab as a fallback so
// the change still propagates on browsers without BroadcastChannel support
// (and survives a quick page reload because the value is persisted).
const DAYGLE_PREFS_CHANNEL = 'daygle-prefs';
const DAYGLE_PREFS_STORAGE_KEY = 'daygle.datePrefs';
const DAYGLE_PREFS_MESSAGE_TYPE = 'daygle-date-prefs';

// ─── Page-preference storage keys ──────────────────────────────────────────
// The recordings, timeline and live pages used to declare their own per-page
// localStorage key for the detection-tracking overlay toggle. Moving them
// here keeps the constants discoverable via window.daygleUi, stops future
// page scripts from re-declaring them locally, and gives a single place to
// bump namespacing. The recordings + timeline keys are deliberately kept
// INDEPENDENT so toggling the overlay on /recordings doesn't quietly flip
// the same preference on /timeline - unify them only if a global "always
// show detection tracking" preference is desired.
const RECORDINGS_OVERLAY_TOGGLE_KEY = 'daygle.recordings.overlay.enabled';
const TIMELINE_OVERLAY_TOGGLE_KEY = 'daygle.timeline.overlay.enabled';
const LIVE_AI_TRACK_KEY = 'daygle.live.overlay.track.enabled';

function broadcastDaygleDatePrefs(prefs) {
  if (!prefs) return;
  const payload = JSON.stringify({
    type: DAYGLE_PREFS_MESSAGE_TYPE,
    dateFormat: prefs.dateFormat || prefs.date_format || window.daygleDatePrefs.dateFormat,
    timeFormat: prefs.timeFormat || prefs.time_format || window.daygleDatePrefs.timeFormat,
  });
  if (typeof BroadcastChannel === 'function') {
    try {
      const channel = new BroadcastChannel(DAYGLE_PREFS_CHANNEL);
      channel.postMessage(payload);
      channel.close();
    } catch (_err) { /* ignore */ }
  }
  try { localStorage.setItem(DAYGLE_PREFS_STORAGE_KEY, payload); } catch (_err) { /* ignore */ }
}

function subscribeDaygleDatePrefs() {
  function handleMessage(raw) {
    let data = raw;
    if (typeof raw === 'string') {
      try { data = JSON.parse(raw); } catch (_err) { return; }
    }
    if (!data || data.type !== DAYGLE_PREFS_MESSAGE_TYPE) return;
    setDaygleDatePrefs({
      date_format: data.dateFormat,
      time_format: data.timeFormat,
    });
  }
  if (typeof BroadcastChannel === 'function') {
    try {
      const channel = new BroadcastChannel(DAYGLE_PREFS_CHANNEL);
      channel.addEventListener('message', (event) => handleMessage(event.data));
    } catch (_err) { /* ignore */ }
  }
  // The `storage` event only fires on OTHER tabs of the same origin, so it
  // complements the BroadcastChannel above without double-firing locally.
  window.addEventListener('storage', (event) => {
    if (event.key === DAYGLE_PREFS_STORAGE_KEY) handleMessage(event.newValue);
  });
}

subscribeDaygleDatePrefs();

function formatUserDate(isoDateString) {
  if (!isoDateString) return '';
  const [year, month, day] = String(isoDateString).slice(0, 10).split('-');
  if (!year || !month || !day) return String(isoDateString);
  switch (window.daygleDatePrefs.dateFormat) {
    case 'iso': return `${year}-${month}-${day}`;
    case 'us': return `${month}/${day}/${year}`;
    case 'au': return `${day}/${month}/${year}`;
    default:
      // Browser locale: anchor at midday to avoid TZ rolling the date
      // back/forward across the day boundary.
      return new Date(`${year}-${month}-${day}T12:00:00`).toLocaleDateString();
  }
}

function formatUserTime(date) {
  if (!date) return '';
  if (window.daygleDatePrefs.timeFormat === '12h') {
    return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
}

function timeAgo(isoString) {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diff < 5) return 'Just now';
  if (diff < 60) return `${diff}s ago`;
  const minutes = Math.floor(diff / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(isoString);
}

function formatDate(value) {
  if (!value) return 'Unknown time';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown time';
  // Derive the date from the SAME local Date object that formatUserTime uses.
  // Slicing the raw ISO string takes the UTC date, which disagrees with the
  // locally-converted time whenever the viewer's timezone crosses midnight
  // relative to UTC (e.g. a UTC+10 morning event is stored as the previous
  // UTC day) - producing a "Started" date that is a day off from the wall clock.
  const localIso = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  return `${formatUserDate(localIso)} ${formatUserTime(date)}`;
}

function formatDateTime(value) {
  return formatDate(value);
}

// ─── Date-range "since" bound (alerts page + dashboard activity feed) ──────
// Both the alerts page and the dashboard filter by a UI range preset
// ('today' / '7d' / '30d' / 'all') and send a `since` ISO bound to
// /api/alerts, /api/events and /api/stats. The backend compares stored UTC
// ISO timestamps lexically (`created_at >= ?`), so the bound MUST be the
// START OF THE LOCAL DAY expressed in UTC -- NOT the UTC date string. The
// old code sent `new Date().toISOString().split('T')[0]` (the UTC date),
// which for operators in timezones AHEAD of UTC silently dropped every
// alert fired between local midnight and UTC midnight (those rows carry
// yesterday's UTC date): the alerts page "Today" tab showed 1 of 6 alerts
// while "7d" showed them all.
function daygleLocalDayStartIso(daysAgo) {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  d.setHours(0, 0, 0, 0); // local midnight of that local day
  return d.toISOString();
}

function daygleSinceParamForRange(range) {
  if (range === 'today') return daygleLocalDayStartIso(0);
  if (range === '7d') return daygleLocalDayStartIso(7);
  if (range === '30d') return daygleLocalDayStartIso(30);
  return ''; // 'all' - no since filter
}

// Seconds-of-day → wall clock (e.g. 37800 → "10:30" or "10:30 am"). Honours
// the user's timeFormat preference so timeline ticks match the rest of the
// app instead of being hardcoded to 24h.
// ─── Shared rule expand-row template (zones.js + sounds.js) ──────────────
// Both zone object rules and sound rules have an expandable row that
// contains an email-recipients field plus four time-picker fields
// (active_start / active_end / notify_start / notify_end). The data-
// attribute prefix differs between the two pages ('zone-rule' vs 'rule')
// but the HTML structure is byte-identical. Consolidating here keeps
// the two page scripts in sync so future tweaks to the time-picker
// layout land in one place.
//
// renderRuleExpandFields is the shared field set (email recipients + four
// time pickers). renderRuleExpandRow wraps it in a table row for the
// object/sound rule tables; the zones page Motion card embeds the same
// fields directly in a div so Motion's advanced settings match the object
// rules without living in the table.
function renderRuleExpandFields(prefix, key, rule) {
  return `
    <label class="sound-rule-field sound-rule-email-field">
      <span>Email recipients</span>
      <input type="email" data-${prefix}-email-recipients="${escapeHtml(key)}" value="${escapeHtml(normalizeEmailList(rule.email_recipients).join(', '))}" placeholder="alerts@example.com" multiple autocomplete="off" data-lpignore="true" data-1p-ignore data-bwignore />
    </label>
    <label class="sound-rule-field" title="Detection window: this rule only detects, records and raises alerts between these times. Leave blank to run all day. Wraps past midnight, e.g. 22:00 to 05:00.">
      <span>Active from</span>
      ${renderTimeSelect(rule.active_start, `data-${prefix}-active-start`, key)}
    </label>
    <label class="sound-rule-field" title="Detection window: this rule only detects, records and raises alerts between these times. Leave blank to run all day. Wraps past midnight, e.g. 22:00 to 05:00.">
      <span>Active to</span>
      ${renderTimeSelect(rule.active_end, `data-${prefix}-active-end`, key)}
    </label>
    <label class="sound-rule-field" title="Email/Push window: only send email and push notifications between these times. Outside it you still get on-site alerts and recordings. Leave blank to notify whenever the rule is active. Wraps past midnight, e.g. 22:00 to 05:00.">
      <span>Email/Push from</span>
      ${renderTimeSelect(rule.notify_start, `data-${prefix}-notify-start`, key)}
    </label>
    <label class="sound-rule-field" title="Email/Push window: only send email and push notifications between these times. Outside it you still get on-site alerts and recordings. Leave blank to notify whenever the rule is active. Wraps past midnight, e.g. 22:00 to 05:00.">
      <span>Email/Push to</span>
      ${renderTimeSelect(rule.notify_end, `data-${prefix}-notify-end`, key)}
    </label>`;
}

function renderRuleExpandRow(prefix, key, rule, expanded) {
  return `
    <tr class="rule-expand-row" ${expanded ? '' : 'hidden'}>
      <td colspan="9">
        <div class="rule-expand-body">
          ${renderRuleExpandFields(prefix, key, rule)}
        </div>
      </td>
    </tr>`;
}

function formatUserClock(seconds) {
  if (!Number.isFinite(Number(seconds))) return '';
  const safeSeconds = Math.max(0, Number(seconds));
  const totalMinutes = Math.floor(safeSeconds / 60);
  const h = Math.floor(totalMinutes / 60) % 24;
  const m = totalMinutes % 60;
  if (window.daygleDatePrefs.timeFormat === '12h') {
    const period = h < 12 ? 'am' : 'pm';
    const h12 = h % 12 || 12;
    return `${h12}:${String(m).padStart(2, '0')} ${period}`;
  }
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

// ─── Unified camera label helper ─────────────────────────────────────────
// Shared by the dashboard (app.js), recordings list (recordings.js), and
// timeline (timeline.js). Handles both calling conventions:
//   cameraLabel(recording)              - extract name from recording/event object
//   cameraLabel(cameraName, cameraId)   - two-argument string form
function cameraLabel(cameraNameOrRecording, cameraId) {
  // Object-style: cameraLabel(recording) or cameraLabel(cameraConfig)
  if (cameraNameOrRecording && typeof cameraNameOrRecording === 'object') {
    const metadata = cameraNameOrRecording?.event?.metadata || {};
    return metadata.camera_name || cameraNameOrRecording.camera_id || cameraNameOrRecording.source || 'unknown';
  }
  // String-style: cameraLabel(cameraName, cameraId)
  const name = String(cameraNameOrRecording || '').trim();
  const id = String(cameraId || '').trim();
  return name || id || '';
}

// ─── Explicit public surface (window.daygleUi) ───────────────────────────-
// All helpers above attach to window implicitly via top-level function / const
// declarations. That's historically been good enough for these pages, but it
// makes shadowing cheap (any local `let api = ...` would override the global)
// and creates a footgun the first time someone wraps a helper inside a future
// ES module. Re-expose every helper on one explicit `daygleUi` object so the
// contract is discoverable, future-module-friendly, and identical to what an
// ambient type would declare: `window.daygleUi.api / showToast / ...`. The
// bare-name call sites keep working as free aliases - they're now backed by
// the same functions you see here.
// ─── Theme management (light / dark / system) ─────────────────────────────
// Manages the ``light`` class on ``<html>`` based on the user's saved
// preference from the Profile page. Three modes:
//   'light'  → always ``class="light"``
//   'dark'   → no class (default dark mode)
//   'system' → watches ``matchMedia('(prefers-color-scheme: light)')``
//              and adds/removes ``light`` class reactively.
// The resolved class is cached in localStorage["daygle.theme"] so the
// inline script in each HTML <head> can apply it before paint.

function applyDaygleTheme(themePref) {
  // themePref is one of 'system', 'light', 'dark'. Resolve to a boolean:
  // true = add "light" class, false = remove it.
  let isLight = false;
  if (themePref === 'light') {
    isLight = true;
  } else if (themePref === 'system') {
    isLight = window.matchMedia('(prefers-color-scheme: light)').matches;
  }
  // else 'dark' → isLight stays false

  document.documentElement.classList.toggle('light', isLight);

  // Cache the resolved theme for the inline flash-prevention script. We store
  // 'dark' explicitly (rather than an empty string) so the pre-paint script can
  // distinguish "user chose dark" from "no choice yet" - the latter now
  // defaults to light.
  const cache = isLight ? 'light' : 'dark';
  try { localStorage.setItem('daygle.theme', cache); } catch (_err) { /* ignore */ }
}

// Listen for OS-level theme changes when in 'system' mode. Cleaned up
// when the user picks an explicit theme.
let _daygleThemeMediaListener = null;

function watchDaygleSystemTheme() {
  unwatchDaygleSystemTheme();
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  _daygleThemeMediaListener = () => {
    // Only re-apply if the effective preference is 'system'
    const pref = window.daygleThemePref || 'system';
    if (pref === 'system') {
      document.documentElement.classList.toggle('light', mq.matches);
      const cache = mq.matches ? 'light' : 'dark';
      try { localStorage.setItem('daygle.theme', cache); } catch (_err) { /* ignore */ }
    }
  };
  mq.addEventListener('change', _daygleThemeMediaListener);
}

function unwatchDaygleSystemTheme() {
  if (_daygleThemeMediaListener) {
    try {
      window.matchMedia('(prefers-color-scheme: light)').removeEventListener('change', _daygleThemeMediaListener);
    } catch (_err) { /* ignore */ }
    _daygleThemeMediaListener = null;
  }
}

// Apply the user's saved theme preference. Called on page load after auth
// resolves, and on profile save when the preference changes.
window.daygleThemePref = 'light'; // default until auth resolves

function setDaygleThemePref(theme) {
  window.daygleThemePref = theme || 'light';
  applyDaygleTheme(window.daygleThemePref);
  if (window.daygleThemePref === 'system') {
    watchDaygleSystemTheme();
  } else {
    unwatchDaygleSystemTheme();
  }
  // Broadcast to other tabs so they pick up the change instantly.
  broadcastDaygleThemePref(theme);
}

function getDaygleThemePref() {
  return window.daygleThemePref;
}

// ─── Cross-tab theme broadcast ───────────────────────────────────────────
const DAYGLE_THEME_STORAGE_KEY = 'daygle.themePref';
const DAYGLE_THEME_CHANNEL = 'daygle-theme';
const DAYGLE_THEME_MESSAGE_TYPE = 'daygle-theme-prefs';

function broadcastDaygleThemePref(theme) {
  const payload = JSON.stringify({
    type: DAYGLE_THEME_MESSAGE_TYPE,
    theme: theme || 'light',
  });
  if (typeof BroadcastChannel === 'function') {
    try {
      const channel = new BroadcastChannel(DAYGLE_THEME_CHANNEL);
      channel.postMessage(payload);
      channel.close();
    } catch (_err) { /* ignore */ }
  }
  try { localStorage.setItem(DAYGLE_THEME_STORAGE_KEY, payload); } catch (_err) { /* ignore */ }
}

function subscribeDaygleThemePref() {
  function handleMessage(raw) {
    let data = raw;
    if (typeof raw === 'string') {
      try { data = JSON.parse(raw); } catch (_err) { return; }
    }
    if (!data || data.type !== DAYGLE_THEME_MESSAGE_TYPE) return;
    // Skip if the theme hasn't actually changed. BroadcastChannel delivers
    // messages to the SAME tab that posted them, which would otherwise
    // re-enter setDaygleThemePref → broadcastDaygleThemePref and create an
    // infinite loop causing the site to flash between themes every second.
    if (data.theme === window.daygleThemePref) return;
    setDaygleThemePref(data.theme);
  }
  if (typeof BroadcastChannel === 'function') {
    try {
      const channel = new BroadcastChannel(DAYGLE_THEME_CHANNEL);
      channel.addEventListener('message', (event) => handleMessage(event.data));
    } catch (_err) { /* ignore */ }
  }
  window.addEventListener('storage', (event) => {
    if (event.key === DAYGLE_THEME_STORAGE_KEY) handleMessage(event.newValue);
  });
}

subscribeDaygleThemePref();

function getDaygleDatePrefs() {
  return window.daygleDatePrefs;
}

window.daygleUi = {
  // Preserve methods registered by nav.js, which loads before this shared
  // utility bundle. The registry is intentionally additive so loading utils
  // cannot silently discard nav-specific helpers such as the auth countdown.
  ...(window.daygleUi || {}),
  // API + auth
  api, setApiAuth, getApiAuth, refreshDaygleAuth, scheduleNextAuthRefresh,
  handleSessionLoss, defaultReturnTo,
  // UI helpers
  showToast, escapeHtml, safeHtml, titleCase, normalizeEmailList, requireElements, initDaygleTabs,
  detectionPill, motionPill, continuousPill, stillAlertBadge, isSoundLabel, SOUND_CLASS_IDS, DETECTION_EYE_ICON, DETECTION_MOTION_ICON, DETECTION_CLOCK_ICON, DETECTION_CONTINUOUS_ICON, MOTION_RUNNING_ROW_ICON,
  // Face-identity pills + filters (recordings + snapshots)
  DETECTION_FACE_ICON, normalizeFaceIdentities, eventFaceIdentities, collectRecordingFaceIdentities, faceIdentityPills, matchesFaceFilter,
  isGenericTriggerLabel, GENERIC_TRIGGER_LABELS,
  isMotionOnlyRecording, isContinuousOnlyRecording, motionConfidenceFor, recordingHasMotion,
  isMotionOnlyEvent, isMotionOnlyEventItem,
  // Shared recording readers (recordings list + timeline).
  isSoundRecording, recordingTriggerType, recordingTriggerLabel, recordingZoneNames, recordingDetectionSummary, recordingEventPills, cameraLabel,
  renderTimeSelect, timeSelectValue, setTimeSelectValue,
  // Logs (audit + camera-log share these)
  formatLogTime, LOG_PAGE_SIZE,
  // User-facing date/time renderers (honour daygleDatePrefs)
  formatUserDate, formatUserTime, formatDate, formatDateTime, formatUserClock,
  setDaygleDatePrefs, getDaygleDatePrefs,
  // Date-range "since" bounds (alerts page + dashboard share these)
  daygleSinceParamForRange, daygleLocalDayStartIso,
  // Theme management
  setDaygleThemePref, getDaygleThemePref, applyDaygleTheme,
  watchDaygleSystemTheme, unwatchDaygleSystemTheme,
  // Cross-tab preferences broadcast
  broadcastDaygleDatePrefs, subscribeDaygleDatePrefs,
  // Page-preference storage keys. Page scripts reference the bare
  // identifiers below; top-level const declarations in this file resolve by
  // name in later scripts loaded into the same realm. (window.X is NOT a
  // property - that's how const differs from var - so reach for the bare
  // name or window.daygleUi.X, never window.X.)
  RECORDINGS_OVERLAY_TOGGLE_KEY, TIMELINE_OVERLAY_TOGGLE_KEY, LIVE_AI_TRACK_KEY, DAYGLE_PREFS_STORAGE_KEY,
};
