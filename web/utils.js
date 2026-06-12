function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'\"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
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
  const isoDate = String(value).slice(0, 10);
  return `${formatUserDate(isoDate)} ${formatUserTime(date)}`;
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
