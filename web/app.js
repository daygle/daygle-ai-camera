// ─── DOM handles ────────────────────────────────────────────────────────────
const els = {
  totalEvents: document.getElementById('totalEvents'),
  soundEvents: document.getElementById('soundEvents'),
  objectAlerts: document.getElementById('objectAlerts'),
  soundAlerts: document.getElementById('soundAlerts'),
  activityFeed: document.getElementById('activityFeed'),
  listStatus: document.getElementById('listStatus'),
  dismissAllEventsBtn: document.getElementById('dismissAllEventsBtn'),
  dismissAllAlertsBtn: document.getElementById('dismissAllAlertsBtn'),
  filterPills: document.querySelectorAll('.activity-filter-pill'),
};

// ─── State ──────────────────────────────────────────────────────────────────
let authState = { user: null, csrfToken: null };
let configuredLabels = null;

const SOUND_CLASS_IDS = new Set(['cat_meow', 'dog_bark', 'glass_breaking', 'smoke_alarm', 'baby_crying', 'doorbell', 'car_alarm', 'loud_bang']);
let events = [];
let alertGroups = [];
let activeFilter = 'all';

// ─── API helper (shared pattern with cameras.js / recordings.js) ───────────
async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authState.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = authState.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('Authentication required');
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

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

function soundDetectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No sound detections</span>';
  const best = new Map();
  for (const d of detections) {
    const label = String(d.label || '').trim().toLowerCase();
    if (!label) continue;
    const conf = Number(d.confidence || 0);
    if (!best.has(label) || conf > best.get(label)) best.set(label, conf);
  }
  if (!best.size) return '<span class="muted">No sound detections</span>';
  return Array.from(best.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([label, conf]) => {
      const display = titleCase(label.replace(/_/g, ' '));
      const confText = conf > 0 ? ` · ${Math.round(conf * 100)}%` : '';
      return `<span class="detection detection-sound">🔊 ${escapeHtml(display)}${escapeHtml(confText)}</span>`;
    })
    .join('');
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  // Deduplicate by label, keep best confidence per label — no config filtering
  // so historical data always shows everything that was actually detected.
  const best = new Map();
  for (const d of detections) {
    const label = String(d.label || '').trim().toLowerCase();
    if (!label) continue;
    const conf = Number(d.confidence || 0);
    if (!best.has(label) || conf > best.get(label)) best.set(label, conf);
  }
  if (!best.size) return '<span class="muted">No detections</span>';
  const eyeIcon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>';
  return Array.from(best.entries())
    .sort((a, b) => b[1] - a[1])
    .map(([label, conf]) => `<span class="detection detection-object">${eyeIcon} ${escapeHtml(titleCase(label))} · ${Math.round(conf * 100)}%</span>`)
    .join('');
}

// ─── Alert grouping (consolidates multiple alerts for the same event) ──────
function groupAlertsByEvent(alerts) {
  const order = [];
  const groups = new Map();
  for (const alert of alerts) {
    const key = alert.event_id !== null && alert.event_id !== undefined ? `event-${alert.event_id}` : `alert-${alert.id}`;
    if (!groups.has(key)) {
      order.push(key);
      groups.set(key, {
        key,
        eventId: alert.event_id ?? null,
        ruleNames: [],
        zones: new Set(),
        labels: new Set(),
        detections: [],
        latestAt: alert.created_at,
        earliestAt: alert.created_at,
        recordingId: alert.recording_id ?? null,
        message: alert.message,
      });
    }
    const group = groups.get(key);
    if (alert.rule_name && !group.ruleNames.includes(alert.rule_name)) {
      group.ruleNames.push(alert.rule_name);
      const parts = String(alert.rule_name).split(' / ');
      if (parts.length >= 3) group.zones.add(parts[1]);
    }
    const label = String(alert.label || '').trim().toLowerCase();
    if (label) group.labels.add(label);
    group.detections.push({ label: label || String(alert.label || ''), confidence: Number(alert.confidence || 0) });
    if (alert.created_at && (!group.latestAt || String(alert.created_at) > String(group.latestAt))) {
      group.latestAt = alert.created_at;
    }
    if (alert.created_at && (!group.earliestAt || String(alert.created_at) < String(group.earliestAt))) {
      group.earliestAt = alert.created_at;
    }
    if (alert.recording_id && !group.recordingId) group.recordingId = alert.recording_id;
  }
  return order.map((key) => {
    const group = groups.get(key);
    return { ...group, labels: Array.from(group.labels), zones: Array.from(group.zones) };
  });
}

// ─── Unified activity feed rendering ───────────────────────────────────────
//
// Each item is one of:
//   { type: 'event',  id, createdAt, camera, detections, recordingId, source }
//   { type: 'alert',  id, latestAt,  eventId, camera, ruleNames, labels, detections, recordingId, message }
//
// Items are merged, sorted newest-first, filtered by `activeFilter`, and
// rendered as `.activity-item` rows. The single template keeps the visual
// language consistent between detections and alerts (one icon + main + actions
// column) without duplicating structure across two renderers.

function buildActivityItems() {
  const eventItems = events.map((event) => {
    const isSound = event.source === 'sound';
    let detections = event.detections || [];
    if (isSound && !detections.length && event.metadata) {
      const label = event.metadata.label || event.metadata.class_label || 'sound';
      const confidence = Number(event.metadata.confidence || 0);
      detections = [{ label, confidence }];
    }
    const zoneNames = isSound ? [] : [...new Set(detections.map((d) => d.zone_name).filter(Boolean))];
    return {
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
  });
  const alertItems = alertGroups.map((group) => {
    const isSound = group.labels.some((l) => SOUND_CLASS_IDS.has(l)) || group.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()));
    return {
      type: 'alert',
      id: group.key,
      createdAt: group.latestAt,
      eventId: group.eventId,
      camera: group.camera, // populated below
      ruleNames: group.ruleNames,
      labels: group.labels,
      detections: group.detections,
      recordingId: group.recordingId,
      message: group.message,
      isSound,
      zoneNames: isSound ? [] : (group.zones || []),
    };
  });
  // Alerts don't carry a camera name in the grouping step; try to surface it
  // from the event's `metadata.camera_name` if we can match by event id.
  const eventsById = new Map(events.map((e) => [e.id, e]));
  for (const item of alertItems) {
    if (item.camera) continue;
    const ev = item.eventId !== null ? eventsById.get(item.eventId) : null;
    item.camera = ev ? eventSourceLabel(ev) : '';
  }
  // Deduplicate sound events by recordingId: multiple sound detections during
  // the same recording share a recordingId (via extend_active_rtsp_recording),
  // so collapse them into one entry — matching how object detections appear
  // once per recording.  Merge detections from all grouped events so every
  // detected sound class shows as a badge on the single entry.
  const seenSoundRecording = new Map();
  const dedupedEventItems = eventItems.filter((item) => {
    if (!item.isSound) return true;
    const recId = item.recordingId;
    if (!recId) return true; // no recording — keep as-is
    const prev = seenSoundRecording.get(recId);
    if (prev) {
      // Merge detections into the first (most recent) entry for this recording.
      for (const d of item.detections) {
        prev.detections.push(d);
      }
      return false;
    }
    seenSoundRecording.set(recId, item);
    return true;
  });

  return [...dedupedEventItems, ...alertItems]
    .filter((item) => item.createdAt)
    .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
}

function applyFilter(items) {
  if (activeFilter === 'detections') return items.filter((i) => i.type === 'event');
  if (activeFilter === 'alerts') return items.filter((i) => i.type === 'alert');
  return items;
}

function eventIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>';
}

function alertIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>';
}

function soundIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';
}

function recordingLink(recordingId, label) {
  if (!recordingId) return '';
  const playIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  return `<a class="secondary" href="/recordings?recording_id=${encodeURIComponent(recordingId)}">${playIcon} ${escapeHtml(label)}</a>`;
}

function renderActivityItem(item) {
  const isEvent = item.type === 'event';
  const isSound = Boolean(item.isSound);
  const icon = isSound ? soundIcon() : isEvent ? eventIcon() : alertIcon();
  const typeClass = isSound ? 'activity-item-sound' : isEvent ? 'activity-item-event' : 'activity-item-alert';
  const typeLabel = isSound ? (isEvent ? 'Sound Detection' : 'Sound Alert') : isEvent ? 'Object Detection' : 'Object Alert';
  const title = item.recordingId
    ? `Recording #${item.recordingId}`
    : isEvent
      ? `Event #${item.id}`
      : 'Alert';
  const titleSuffix = '';
  const cameraLine = item.camera ? `Camera: ${escapeHtml(item.camera)}` : 'Camera: unknown';
  const zonePart = !isSound && item.zoneNames?.length
    ? ` · Zone: ${item.zoneNames.map(escapeHtml).join(', ')}`
    : '';
  const metaLine = `${cameraLine}${zonePart}`;
  const actions = [];
  if (isEvent && item.recordingId) {
    actions.push(recordingLink(item.recordingId, 'Recording'));
  } else if (!isEvent && item.recordingId) {
    actions.push(recordingLink(item.recordingId, 'Footage'));
  }
  const dismissAttr = isEvent
    ? `data-dismiss-event="${escapeHtml(String(item.id))}"`
    : `data-dismiss-alert="${escapeHtml(String(item.id))}"`;
  const dismissIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  actions.push(`<button class="secondary" ${dismissAttr} type="button">${dismissIcon} Dismiss</button>`);
  return `
    <article class="item activity-item ${typeClass}" data-activity-id="${escapeHtml(String(item.id))}" data-activity-type="${item.type}">
      <div class="activity-item-icon">${icon}</div>
      <div class="activity-item-main">
        <div class="activity-item-header">
          <div class="activity-item-title">
            <span class="activity-item-type">${typeLabel}</span>
            <span class="activity-item-name">${title}${titleSuffix}</span>
          </div>
          <div class="activity-item-when">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span title="${escapeHtml(formatDate(item.createdAt))}">${escapeHtml(timeAgo(item.createdAt))}</span>
          </div>
        </div>
        <p class="muted activity-item-meta">${metaLine}</p>
        <div class="activity-item-badges">${isSound ? soundDetectionBadges(item.detections) : detectionBadges(item.detections)}</div>
      </div>
      ${actions.length ? `<div class="activity-item-actions">${actions.join('')}</div>` : ''}
    </article>
  `;
}

function renderEmptyState() {
  const messages = {
    all: { title: 'No activity yet', subtitle: 'Detections and alerts will appear here as your cameras report them.' },
    detections: { title: 'No detections yet', subtitle: 'Detected objects will show up here once the AI starts seeing events.' },
    alerts: { title: 'No alerts yet', subtitle: 'Alerts from your zone rules will appear here when they fire.' },
  };
  const { title, subtitle } = messages[activeFilter] || messages.all;
  return `
    <div class="activity-empty-state">
      <div class="activity-empty-icon" aria-hidden="true">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
      </div>
      <h2>${title}</h2>
      <p class="muted">${subtitle}</p>
    </div>
  `;
}

function renderActivityFeed() {
  const items = applyFilter(buildActivityItems());
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
  const labels = { all: 'activity items', detections: 'detections', alerts: 'alerts' };
  const label = labels[activeFilter] || 'items';
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
        window.showToast?.(error.message, true);
        btn.disabled = false;
      }
    });
  });
  els.activityFeed.querySelectorAll('[data-dismiss-alert]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.dismissAlert;
      btn.disabled = true;
      try {
        await api(`/api/alerts/${encodeURIComponent(key)}/dismiss`, { method: 'POST' });
        alertGroups = alertGroups.filter((g) => String(g.key) !== String(key));
        renderActivityFeed();
      } catch (error) {
        window.showToast?.(error.message, true);
        btn.disabled = false;
      }
    });
  });
}

function updateDismissButtons() {
  if (els.dismissAllEventsBtn) els.dismissAllEventsBtn.hidden = events.length === 0;
  if (els.dismissAllAlertsBtn) els.dismissAllAlertsBtn.hidden = alertGroups.length === 0;
}

// ─── Stats + activity data loaders ──────────────────────────────────────────
async function loadStats() {
  try {
    const stats = await api('/api/stats');
    els.totalEvents.textContent = stats.matched_object_events ?? stats.total_events ?? 0;
    if (els.soundEvents) els.soundEvents.textContent = stats.sound_detection_events ?? 0;
    if (els.objectAlerts) els.objectAlerts.textContent = stats.object_alerts ?? stats.total_alerts ?? 0;
    if (els.soundAlerts) els.soundAlerts.textContent = stats.sound_alerts ?? 0;
  } catch (error) {
    window.showToast?.(error.message, true);
  }
}

async function loadEvents() {
  try {
    events = await api('/api/events?with_recording=true');
  } catch (error) {
    events = [];
    window.showToast?.(error.message, true);
  }
}

async function loadAlerts() {
  try {
    const alerts = await api('/api/alerts');
    alertGroups = groupAlertsByEvent(alerts);
  } catch (error) {
    alertGroups = [];
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
          if (rule.enabled !== false && (rule.alert_on_detect !== false || rule.record_on_detect !== false)) {
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
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
}

els.dismissAllEventsBtn?.addEventListener('click', async () => {
  els.dismissAllEventsBtn.disabled = true;
  try {
    await api('/api/events/dismiss-all', { method: 'POST' });
    events = [];
    renderActivityFeed();
  } catch (error) {
    window.showToast?.(error.message, true);
  } finally {
    els.dismissAllEventsBtn.disabled = false;
  }
});

els.dismissAllAlertsBtn?.addEventListener('click', async () => {
  els.dismissAllAlertsBtn.disabled = true;
  try {
    await api('/api/alerts/dismiss-all', { method: 'POST' });
    alertGroups = [];
    renderActivityFeed();
  } catch (error) {
    window.showToast?.(error.message, true);
  } finally {
    els.dismissAllAlertsBtn.disabled = false;
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
  await Promise.all([loadStats(), loadEvents(), loadAlerts()]);
  renderActivityFeed();
}

// Re-render when the user's date_format / time_format changes in another tab
// (driven by utils.js daygleDatePrefsChanged hook). 5s status / 30s activity
// polls keep things fresh in the meantime.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  renderActivityFeed();
};

loadAuth()
  .then(async () => {
    await loadConfiguredLabels();
    await refreshAll();
  })
  .catch((error) => window.showToast?.(error.message, true));

setInterval(() => { loadStats().catch(() => {}); }, 10000);
setInterval(() => {
  Promise.all([loadEvents(), loadAlerts()]).then(renderActivityFeed).catch(() => {});
}, 30000);
