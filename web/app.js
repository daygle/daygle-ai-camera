// ─── DOM handles ────────────────────────────────────────────────────────────
const els = {
  totalEvents: document.getElementById('totalEvents'),
  soundEvents: document.getElementById('soundEvents'),
  motionEvents: document.getElementById('motionEvents'),
  activityFeed: document.getElementById('activityFeed'),
  listStatus: document.getElementById('listStatus'),
  dismissAllEventsBtn: document.getElementById('dismissAllEventsBtn'),
  filterPills: document.querySelectorAll('.activity-filter-pill'),
};

// ─── State ──────────────────────────────────────────────────────────────────
// CSRF token and current user live on window.daygleAuth (set in loadAuth()
// via setApiAuth(...) - provided by web/utils.js). Per-page flashes should
// read auth state from there rather than a local copy.
let configuredLabels = null;

// SOUND_CLASS_IDS, isSoundLabel, GENERIC_TRIGGER_LABELS, DETECTION_EYE_ICON,
// DETECTION_MOTION_ICON, MOTION_RUNNING_ROW_ICON, detectionPill() and
// motionPill() are provided by web/utils.js (loaded before this script).

let events = [];
let activeFilter = 'all';

// api() is provided by web/utils.js (loaded before this script) - it reads
// the CSRF token from window.daygleAuth.csrfToken and handles 401 redirects
// so every page shares identical auth and error semantics.

// ─── Small utilities (kept local to avoid touching utils.js) ────────────────
function cameraLabel(cameraName, cameraId) {
  const name = String(cameraName || '').trim();
  const id = String(cameraId || '').trim();
  if (name && id) return `${name} (${id})`;
  return name || id || '';
}

function eventSourceLabel(event) {
  const metadata = event?.metadata || {};
  const fromMetadata = cameraLabel(metadata.camera_name, metadata.camera_id);
  if (fromMetadata) return fromMetadata;
  const fromRecording = cameraLabel('', event?.recordings?.[0]?.camera_id);
  if (fromRecording) return fromRecording;
  return String(event?.source || 'unknown');
}


function timeAgo(isoString) {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  const minutes = Math.floor(diff / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(isoString);
}

// Deduplicate detections by label (keeping the best confidence per label) and
// render one pill each, sorted by confidence descending. No config filtering,
// so historical data always shows everything that was actually detected.
// `isSound` swaps the empty-state copy and forces the speaker-icon pill (object
// pills still upgrade to the speaker icon on their own via isSoundLabel).
function detectionBadges(detections = [], { isSound = false } = {}) {
  const emptyText = isSound ? 'No sound detections' : 'No detections';
  if (!detections.length) return `<span class="muted">${emptyText}</span>`;
  const best = new Map();
  for (const d of detections) {
    const label = String(d.label || '').trim().toLowerCase();
    if (!label) continue;
    const conf = Number(d.confidence);
    if (!Number.isFinite(conf)) {
      if (!best.has(label)) best.set(label, null);
      continue;
    }
    if (!best.has(label) || best.get(label) === null || conf > best.get(label)) best.set(label, conf);
  }
  if (!best.size) return `<span class="muted">${emptyText}</span>`;
  return Array.from(best.entries())
    .sort((a, b) => (b[1] ?? -1) - (a[1] ?? -1))
    .map(([label, conf]) => detectionPill(label, conf, isSound))
    .join('');
}

// ─── Detection feed rendering ─────────────────────────────────────────────
// Each item represents one event (detection). Items are sorted newest-first,
// filtered by `activeFilter`, and rendered as `.activity-item` rows.
//
// isMotionOnlyEvent / isMotionOnlyEventItem live in web/utils.js (loaded
// before this script) and are also exposed on window.daygleUi for callers
// that prefer the explicit namespace.

function updateMotionStats() {
  if (els.motionEvents) els.motionEvents.textContent = events.filter(isMotionOnlyEvent).length;
}

function buildEventItems() {
  const eventItems = events.map((event) => {
    const isSound = event.source === 'sound';
    let detections = event.detections || [];
    if (isSound && !detections.length && event.metadata) {
      const label = event.metadata.label || event.metadata.class_label || 'sound';
      const confidence = Number(event.metadata.confidence);
      detections = [{ label, confidence: Number.isFinite(confidence) ? confidence : null }];
    }
    const zoneNames = isSound ? [] : [...new Set(detections.map((d) => d.zone_name).filter(Boolean))];
    const item = {
      type: 'event',
      id: event.id,
      createdAt: event.created_at,
      camera: eventSourceLabel(event),
      detections,
      recordingId: event.recordings?.[0]?.id ?? null,
      isSound,
      soundMeta: isSound ? event.metadata : null,
      zoneNames,
    };
    if (isMotionOnlyEventItem(item)) item.isMotionOnly = true;
    return item;
  });
  // Deduplicate sound events by recordingId: multiple sound detections during
  // the same recording share a recordingId (via extend_active_rtsp_recording),
  // so collapse them into one entry. Merge detections from all grouped events
  // so every detected sound class shows as a badge on the single entry.
  const seenSoundRecording = new Map();
  return eventItems.filter((item) => {
    if (!item.isSound) return true;
    const recId = item.recordingId;
    if (!recId) return true;
    const prev = seenSoundRecording.get(recId);
    if (prev) {
      for (const d of item.detections) prev.detections.push(d);
      return false;
    }
    seenSoundRecording.set(recId, item);
    return true;
  }).filter((item) => item.createdAt)
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
}

function applyFilter(items) {
  if (activeFilter === 'object-detections') return items.filter((i) => !i.isSound && !i.isMotionOnly);
  if (activeFilter === 'motion-detections') return items.filter((i) => i.isMotionOnly);
  if (activeFilter === 'sound-detections') return items.filter((i) => i.isSound);
  return items;
}

function eventIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>';
}

function motionActivityIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="m4 19.5 4-4.5 1.5 4 5.5-3-2-7 4-3"/></svg>';
}

function soundIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';
}

function recordingLink(recordingId, label) {
  if (!recordingId) return '';
  const playIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  return `<a class="secondary activity-item-action" href="/recordings?recording_id=${encodeURIComponent(recordingId)}">${playIcon} ${escapeHtml(label)}</a>`;
}

function renderActivityItem(item) {
  const isSound = Boolean(item.isSound);
  const isMotionOnly = Boolean(item.isMotionOnly);
  const icon = isSound ? soundIcon() : isMotionOnly ? motionActivityIcon() : eventIcon();
  const typeClass = isSound ? 'activity-item-sound' : isMotionOnly ? 'activity-item-motion' : 'activity-item-event';
  const typeLabel = isSound ? 'Sound Detection' : isMotionOnly ? 'Motion Detection' : 'Object Detection';
  const title = item.recordingId ? `Recording #${item.recordingId}` : `Event #${item.id}`;
  const cameraLine = item.camera ? `Camera: ${escapeHtml(item.camera)}` : 'Camera: unknown';
  const zonePart = !isSound && item.zoneNames?.length
    ? ` · Zone: ${item.zoneNames.map(escapeHtml).join(', ')}`
    : '';
  const metaLine = `${cameraLine}${zonePart}`;
  const actions = [];
  if (item.recordingId) actions.push(recordingLink(item.recordingId, 'Recording'));
  if (window.daygleAuth?.user?.role === 'admin') {
    const dismissIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    actions.push(`<button class="secondary delete-btn activity-item-action" data-dismiss-event="${escapeHtml(String(item.id))}" type="button">${dismissIcon} Dismiss</button>`);
  }
  return `
    <article class="item activity-item ${typeClass}" data-activity-id="${escapeHtml(String(item.id))}">
      <div class="activity-item-icon">${icon}</div>
      <div class="activity-item-main">
        <div class="activity-item-header">
          <div class="activity-item-title">
            <span class="activity-item-type">${typeLabel}</span>
            <span class="activity-item-name">${title}</span>
          </div>
          <div class="activity-item-when">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span title="${escapeHtml(formatDate(item.createdAt))}">${escapeHtml(timeAgo(item.createdAt))}</span>
          </div>
        </div>
        <p class="muted activity-item-meta">${metaLine}</p>
        <div class="activity-item-badges">${isMotionOnly ? motionPill() : detectionBadges(item.detections, { isSound })}</div>
      </div>
      ${actions.length ? `<div class="activity-item-actions">${actions.join('')}</div>` : ''}
    </article>
  `;
}

function renderEmptyState() {
  const messages = {
    all: { title: 'No detections yet', subtitle: 'Detections from your cameras will appear here once the AI starts seeing events.' },
    'object-detections': { title: 'No object detections yet', subtitle: 'Detected objects will show up here once the AI starts seeing events.' },
    'motion-detections': { title: 'No motion detections yet', subtitle: 'Motion-only events will appear here once a camera reports frame motion without a recognised object.' },
    'sound-detections': { title: 'No sound detections yet', subtitle: 'Detected sounds will show up here once the AI starts hearing events.' },
  };
  const { title, subtitle } = messages[activeFilter] || messages.all;
  return `
    <div class="activity-empty-state">
      <div class="activity-empty-icon" aria-hidden="true">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>
      </div>
      <h2>${title}</h2>
      <p class="muted">${subtitle}</p>
    </div>
  `;
}

function renderActivityFeed() {
  const items = applyFilter(buildEventItems());
  if (!items.length) {
    els.activityFeed.innerHTML = renderEmptyState();
    updateListStatus(0);
    updateDismissButtons();
    return;
  }
  els.activityFeed.innerHTML = items.map(renderActivityItem).join('');
  updateListStatus(items.length);
  bindActivityActions();
  updateDismissButtons();
}

function updateListStatus(count) {
  if (!els.listStatus) return;
  const labels = { all: 'detections', 'object-detections': 'object detections', 'motion-detections': 'motion detections', 'sound-detections': 'sound detections' };
  const label = labels[activeFilter] || 'detections';
  if (count === 0) {
    els.listStatus.textContent = '';
  } else {
    els.listStatus.textContent = `Showing ${count} ${label}`;
  }
}

function bindActivityActions() {
  els.activityFeed.querySelectorAll('[data-dismiss-event]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.dismissEvent;
      btn.disabled = true;
      try {
        await api(`/api/events/${id}/dismiss`, { method: 'POST' });
        events = events.filter((e) => String(e.id) !== String(id));
        renderActivityFeed();
      } catch (error) {
        // Skip UI updates if api() triggered a 401 redirect
        if (window.daygleAuth?.redirecting) return;
        window.showToast?.(error.message, true);
        btn.disabled = false;
      }
    });
  });
}

function updateDismissButtons() {
  const isAdmin = window.daygleAuth?.user?.role === 'admin';
  if (els.dismissAllEventsBtn) els.dismissAllEventsBtn.hidden = !isAdmin || events.length === 0;
}

// ─── Stats + activity data loaders ──────────────────────────────────────────
async function loadStats() {
  try {
    const stats = await api('/api/stats');
    els.totalEvents.textContent = stats.matched_object_events ?? stats.total_events ?? 0;
    if (els.soundEvents) els.soundEvents.textContent = stats.sound_detection_events ?? 0;
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  }
}

async function loadEvents() {
  try {
    events = await api('/api/events?with_recording=true');
    updateMotionStats();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    events = [];
    window.showToast?.(error.message, true);
  }
}

async function loadConfiguredLabels() {
  try {
    const settings = await api('/api/settings/system');
    const labels = new Map([['motion', 0.45]]);
    const setMin = (label, conf) => {
      if (!label) return;
      if (!labels.has(label) || conf < labels.get(label)) labels.set(label, conf);
    };
    for (const camera of (settings?.cameras || [])) {
      for (const zone of (camera?.detection?.zones || [])) {
        for (const rule of (zone?.object_rules || [])) {
          if (rule.enabled !== false && (rule.email_enabled === true || rule.push_enabled === true || rule.record_on_detect !== false)) {
            const label = String(rule.label || '').trim().toLowerCase();
            setMin(label, Number(rule.min_confidence ?? 0.5));
          }
        }
      }
    }
    configuredLabels = labels;
  } catch {
    // Show all labels if settings are unavailable.
  }
}

// ─── Auth ────────────────────────────────────────────────────────────────────
async function loadAuth() {
  // nav.js kicks off /api/auth/me at script load and exposes the result on
  // window.daygleAuth / window.daygleAuthReady. Awaiting here means we
  // never issue a duplicate /api/auth/me on bootstrap.
  await window.daygleAuthReady;
}

els.dismissAllEventsBtn?.addEventListener('click', async () => {
  els.dismissAllEventsBtn.disabled = true;
  try {
    await api('/api/events/dismiss-all', { method: 'POST' });
    events = [];
    renderActivityFeed();
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  } finally {
    els.dismissAllEventsBtn.disabled = false;
  }
});

// ─── Filter pills ───────────────────────────────────────────────────────────
els.filterPills.forEach((pill) => {
  pill.addEventListener('click', () => {
    activeFilter = pill.dataset.filter;
    els.filterPills.forEach((other) => {
      const active = other === pill;
      other.classList.toggle('active', active);
      other.setAttribute('aria-selected', String(active));
    });
    renderActivityFeed();
  });
});

// ─── Refresh orchestration ──────────────────────────────────────────────────
async function refreshAll() {
  await Promise.all([loadStats(), loadEvents()]);
  renderActivityFeed();
}

// Re-render when the user's date_format / time_format changes in another tab
// (driven by utils.js daygleDatePrefsChanged hook). 5s stats / 30s events
// polls keep things fresh in the meantime.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  renderActivityFeed();
};

loadAuth()
  .then(async () => {
    await loadConfiguredLabels();
    await refreshAll();
  })
  .catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  });

setInterval(() => { loadStats().catch(() => {}); }, 10000);
setInterval(() => {
  loadEvents().then(renderActivityFeed).catch(() => {});
}, 30000);
