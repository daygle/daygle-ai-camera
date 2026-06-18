function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
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
// to /login — pages that need a different policy can call `api(...)`
// directly with custom handlers.

// CSRF auth state lives on `window.daygleAuth` so any page can read it
// without re-fetching /api/auth/me. Populated by `setApiAuth()`.
window.daygleAuth = window.daygleAuth || { user: null, csrfToken: null };

function setApiAuth(user, csrfToken) {
  window.daygleAuth.user = user || null;
  window.daygleAuth.csrfToken = csrfToken || null;
}

function getApiAuth() {
  return window.daygleAuth;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  // Attach the CSRF token only for state-changing verbs; GETs don't need it.
  const method = (options.method || 'GET').toUpperCase();
  if (window.daygleAuth.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    headers['X-CSRF-Token'] = window.daygleAuth.csrfToken;
  }
  // Mirror live.js's prior behaviour: when the caller supplies a body but
  // doesn't set Content-Type, assume JSON. Pages that send other shapes
  // (FormData, etc.) set the header themselves, so this stays a safe default.
  if (options.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    // Flag the redirect on daygleAuth (alongside user/csrfToken) so
    // page-side catches short-circuit the 'Authentication required'
    // panel flash on a page about to navigate. Cache the timer's id
    // so a burst of 401s resets the 250 ms bound (exceeds typical
    // redirect round-trip; short enough that a genuine non-401 error
    // on the same still-alive page surfaces). clearTimeout(undefined)
    // is a safe no-op.
    window.daygleAuth.redirecting = true;
    window.location.href = '/login';
    clearTimeout(window.daygleAuth._redirectTimer);
    window.daygleAuth._redirectTimer = setTimeout(() => {
      window.daygleAuth.redirecting = false;
      window.daygleAuth._redirectTimer = null;
    }, 250);
    throw new Error('Authentication required');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return payload;
}
window.api = api;

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

// ─── Shared log table formatting ──────────────────────────────────────────
// Audit + camera-log entries use the same locale-aware "Nov 4, 2025, 12:30:45"
// format. Centralised here so a future tweak (e.g. honouring the user's
// time-format preference on these lists) lands in one place rather than two.
function formatLogTime(iso) {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch {
    return iso;
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

// ─── Explicit public surface (window.daygleUi) ───────────────────────────–
// All helpers above attach to window implicitly via top-level function / const
// declarations. That's historically been good enough for these pages, but it
// makes shadowing cheap (any local `let api = ...` would override the global)
// and creates a footgun the first time someone wraps a helper inside a future
// ES module. Re-expose every helper on one explicit `daygleUi` object so the
// contract is discoverable, future-module-friendly, and identical to what an
// ambient type would declare: `window.daygleUi.api / showToast / ...`. The
// bare-name call sites keep working as free aliases — they're now backed by
// the same functions you see here.
function getDaygleDatePrefs() {
  return window.daygleDatePrefs;
}

window.daygleUi = {
  // API + auth
  api, setApiAuth, getApiAuth,
  // UI helpers
  showToast, escapeHtml, titleCase,
  detectionPill, isSoundLabel, SOUND_CLASS_IDS, DETECTION_EYE_ICON,
  renderTimeSelect, timeSelectValue,
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
