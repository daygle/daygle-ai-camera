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
  console.error(`[${pageTitle}] missing required element ids:`, missing);
  throw new Error('This page is missing required DOM elements; check the HTML for matching ids.');
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
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    handleSessionLoss('Authentication required', defaultReturnTo());
    throw new Error('Authentication required');
  }
  const payload = await response.json().catch(() => {});
  // 403 with CSRF-related detail text = a stale CSRF cookie/token.
  // The server-side ``csrf_protect`` returns ``CSRF token mismatch``
  // (or similar ``Invalid token`` / ``Missing cookie``) when the
  // cached token no longer matches the session row. Admin-role
  // denials surface as ``Admin access required`` and intentionally
  // do NOT match the regex below, so a non-admin hitting an admin
  // endpoint gets the original 403 toast, not a login redirect.
  //
  // The ``window.daygleAuth?.user`` guard was REMOVED because a
  // concurrent in-flight request may have already cleared auth state
  // (via a prior 401 → handleSessionLoss → setApiAuth(null, null, null))
  // before this 403 arrives. The server's error message alone is the
  // authoritative signal - if it says CSRF, the session is gone.
  if (response.status === 403) {
    const detail = String((payload && payload.detail) || '');
    if (/csrf|invalid.?token|missing.?cookie|invalid.?x-csr/i.test(detail)) {
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
function detectionPill(label, confidence, isSound = false) {
  const labelIsSound = isSound || isSoundLabel(label);
  const display = labelIsSound
    ? titleCase(String(label).replace(/_/g, ' '))
    : titleCase(String(label));
  const numericConfidence = confidence == null ? NaN : Number(confidence);
  const confidenceText = Number.isFinite(numericConfidence)
    ? ` · ${Math.round(numericConfidence * 100)}%`
    : '';
  if (labelIsSound) {
    return `<span class="detection detection-sound">🔊 ${escapeHtml(display)}${confidenceText}</span>`;
  }
  return `<span class="detection detection-object">${DETECTION_EYE_ICON} ${escapeHtml(display)}${confidenceText}</span>`;
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

// A recording is "motion-only" when:
//  * it isn't a sound recording,
//  * no concrete object label is attached to it (the join-table labels
//    array and the per-event detections are both empty once the generic
//    trigger words are filtered out), and
//  * its trigger type isn't one of the always-on / disabled placeholders
//    ('continuous', 'none', 'off') so we don't accidentally label
//    always-on clips as motion.
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
  const triggerType = String(recording.trigger_type || 'motion').trim().toLowerCase();
  if (['continuous', 'none', 'off'].includes(triggerType)) return false;
  return true;
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

// ---------------------------------------------------------------------------
// Motion-vs-object boundary helpers - shared by every page that renders a
// motion / object / sound split (recordings, recordings modal, timeline,
// dashboard activity feed, stat cards). Each helper takes the exact payload
// shape the corresponding API or item builder produces, so the tests can
// pin behaviour at every layer without re-implementing the logic.
//
// "Motion-only" means:
//   * it isn't a sound recording / event / alert (sound source or sound
//     class label), AND
//   * every detection / label sits inside GENERIC_TRIGGER_LABELS (motion,
//     alert, human, object, none, off, continuous) - i.e. no concrete
//     object or sound class labels.
//
// Edge cases: events / alerts / recordings with zero detections / labels
// are NOT classified as motion-only - they're just under-recorded samples
// and shouldn't pull the counts.
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

function isMotionOnlyAlertGroup(group) {
  if (!group) return false;
  // Alert groups carry a labels Set (or array) - include the sound-class
  // check so a single mixed alert (motion + doorbell) is treated as sound.
  const raw = Array.isArray(group.labels) ? group.labels : Array.from(group.labels || []);
  if (raw.some(isSoundLabel)) return false;
  return _hasOnlyGenericLabels(raw);
}

function isMotionOnlyAlertItem(item) {
  if (!item || item.isSound) return false;
  return _hasOnlyGenericLabels(item.labels);
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
  return [...new Set((recording.detections || []).map((d) => d.zone_name).filter(Boolean))];
}

// Returns an array of { label, confidence } sorted by confidence descending.
// Sound recordings collapse to their single class label; object recordings
// surface one entry per unique detected label (deduped via recording.labels
// and the live track), each carrying its best-seen confidence.
function recordingDetectionSummary(recording) {
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    const label = (meta.class_label || meta.label || recording.trigger_label || 'sound').toLowerCase();
    const confidence = Number(meta.confidence || 0);
    return [{ label, confidence }];
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
  // Use recording.labels as the authoritative label list when available.
  const authLabels = Array.isArray(recording.labels) && recording.labels.length
    ? recording.labels.map((l) => String(l || '').trim().toLowerCase()).filter((l) => l && !GENERIC_TRIGGER_LABELS.has(l))
    : Array.from(best.keys()).filter((l) => !GENERIC_TRIGGER_LABELS.has(l));
  return authLabels
    .map((label) => ({ label, confidence: best.has(label) ? best.get(label) : null }))
    .sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1));
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

// Seconds-of-day → wall clock (e.g. 37800 → "10:30" or "10:30 am"). Honours
// the user's timeFormat preference so timeline ticks match the rest of the
// app instead of being hardcoded to 24h.
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
function getDaygleDatePrefs() {
  return window.daygleDatePrefs;
}

window.daygleUi = {
  // API + auth
  api, setApiAuth, getApiAuth, refreshDaygleAuth, scheduleNextAuthRefresh,
  handleSessionLoss, defaultReturnTo,
  // UI helpers
  showToast, escapeHtml, safeHtml, titleCase, requireElements,
  detectionPill, motionPill, isSoundLabel, SOUND_CLASS_IDS, DETECTION_EYE_ICON, DETECTION_MOTION_ICON, MOTION_RUNNING_ROW_ICON,
  isGenericTriggerLabel, GENERIC_TRIGGER_LABELS,
  isMotionOnlyRecording, motionConfidenceFor,
  isMotionOnlyEvent, isMotionOnlyEventItem, isMotionOnlyAlertGroup, isMotionOnlyAlertItem,
  // Shared recording readers (recordings list + timeline). cameraLabel is
  // deliberately NOT shared here: app.js and yamnet-tflite.js define their own
  // cameraLabel() with different signatures in the same global realm.
  isSoundRecording, recordingTriggerType, recordingTriggerLabel, recordingZoneNames, recordingDetectionSummary,
  renderTimeSelect, timeSelectValue, setTimeSelectValue,
  // Logs (audit + camera-log share these)
  formatLogTime, LOG_PAGE_SIZE,
  // User-facing date/time renderers (honour daygleDatePrefs)
  formatUserDate, formatUserTime, formatDate, formatDateTime, formatUserClock,
  setDaygleDatePrefs, getDaygleDatePrefs,
  // Cross-tab preferences broadcast
  broadcastDaygleDatePrefs, subscribeDaygleDatePrefs,
  // Page-preference storage keys. Page scripts reference the bare
  // identifiers below; top-level const declarations in this file resolve by
  // name in later scripts loaded into the same realm. (window.X is NOT a
  // property - that's how const differs from var - so reach for the bare
  // name or window.daygleUi.X, never window.X.)
  RECORDINGS_OVERLAY_TOGGLE_KEY, TIMELINE_OVERLAY_TOGGLE_KEY, LIVE_AI_TRACK_KEY, DAYGLE_PREFS_STORAGE_KEY,
};
