const els = {
  statusText: document.getElementById('statusText'),
  cameraDetail: document.getElementById('cameraDetail'),
  aiModeText: document.getElementById('aiModeText'),
  aiStatusDetail: document.getElementById('aiStatusDetail'),
  totalEvents: document.getElementById('totalEvents'),
  totalAlerts: document.getElementById('totalAlerts'),
  uptimeText: document.getElementById('uptimeText'),
  events: document.getElementById('events'),
  alerts: document.getElementById('alerts'),
  deleteAllEventsBtn: document.getElementById('deleteAllEventsBtn'),
  deleteAllAlertsBtn: document.getElementById('deleteAllAlertsBtn'),
};

let authState = { user: null, csrfToken: null };
let configuredLabels = null;

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
  if (!response.ok) {
    let detail = `Request failed: ${response.status}`;
    try {
      const errorBody = await response.json();
      detail = errorBody.detail || detail;
    } catch {
      // Keep the generic status message when the response body is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

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

function formatUptime(seconds) {
  if (!seconds && seconds !== 0) return '-';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  const normalized = detections
    .map((d) => ({ label: String(d.label || '').trim().toLowerCase(), confidence: Number(d.confidence || 0) }))
    .filter((d) => d.label && (!configuredLabels || (configuredLabels.has(d.label) && d.confidence >= (configuredLabels.get(d.label) ?? 0))));
  if (!normalized.length) return '<span class="muted">No detections</span>';
  return normalized.map((d) => `<span class="detection">${escapeHtml(d.label)} · ${Math.round(d.confidence * 100)}%</span>`).join('');
}

function recordingLink(recordings = []) {
  if (!recordings.length) return '<span class="muted">Recording: none</span>';
  return recordings.map((recording) => `<a class="link-button" href="/recordings?recording_id=${recording.id}">Recording #${recording.id}</a>`).join('');
}

function renderEvents(events) {
  if (!events.length) {
    els.events.innerHTML = '<div class="empty">No recorded events yet.</div>';
    return;
  }

  els.events.innerHTML = events.map((event) => `
    <div class="item event-row">
      <div class="item-title">
        <span>Event #${event.id}</span>
        <span>${formatDate(event.created_at)}</span>
      </div>
      <div class="event-row-badges">${detectionBadges(event.detections)}</div>
      <p class="muted event-row-meta">Camera: ${escapeHtml(eventSourceLabel(event))} · ${escapeHtml(event.recording_status || 'none')}</p>
      <div class="event-row-footer">
        <div>${recordingLink(event.recordings)}</div>
        <button class="secondary delete-btn" data-delete-event="${event.id}">Delete</button>
      </div>
    </div>
  `).join('');
}

function groupAlertsByEvent(alerts) {
  // Collapse multiple alert rows that fired for the same event into a single
  // card, so reviewers see every label that triggered (e.g. "Cat, Person")
  // instead of one row per matching rule. Rows without an event_id stay
  // standalone so legacy / orphaned alerts are still visible.
  const order: number[] = [];
  const groups = new Map();
  for (const alert of alerts) {
    const key = alert.event_id !== null && alert.event_id !== undefined ? `event-${alert.event_id}` : `alert-${alert.id}`;
    if (!groups.has(key)) {
      order.push(key);
      groups.set(key, {
        key,
        eventId: alert.event_id ?? null,
        firstAlertId: alert.id,
        ruleNames: [],
        labels: new Set(),
        detections: [],   // [{label, confidence}]
        maxConfidence: 0,
        latestAt: alert.created_at,
        earliestAt: alert.created_at,
        recordingId: alert.recording_id ?? null,
        message: alert.message,
      });
    }
    const group = groups.get(key);
    if (alert.rule_name && !group.ruleNames.includes(alert.rule_name)) {
      group.ruleNames.push(alert.rule_name);
    }
    const label = String(alert.label || '').trim().toLowerCase();
    if (label) group.labels.add(label);
    group.detections.push({ label: label || String(alert.label || ''), confidence: Number(alert.confidence || 0) });
    if (Number(alert.confidence || 0) > group.maxConfidence) group.maxConfidence = Number(alert.confidence || 0);
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
    return { ...group, labels: Array.from(group.labels) };
  });
}

function renderAlerts(alerts) {
  if (!alerts.length) {
    els.alerts.innerHTML = '<div class="empty">No alerts triggered yet.</div>';
    return;
  }

  const groups = groupAlertsByEvent(alerts);
  els.alerts.innerHTML = groups.map((group) => {
    const titleSuffix = group.ruleNames.length > 1 ? ` (${group.ruleNames.length} rules)` : '';
    const ruleLabel = group.ruleNames.join(', ') || 'Alert';
    const labelChips = group.labels.length
      ? `<div class="alert-label-chips" aria-label="All labels that triggered for this event">${
          group.labels.map((label) => `<span class="alert-label-chip">${escapeHtml(label)}</span>`).join('')
        }</div>`
      : '';
    const eventTag = group.eventId !== null
      ? `<span class="muted">Event #${escapeHtml(String(group.eventId))}</span>`
      : '';
    return `
      <div class="item alert-row" data-alert-event-id="${escapeHtml(String(group.eventId ?? ''))}">
        <div class="item-title">
          <span>${escapeHtml(ruleLabel)}${titleSuffix}</span>
          <span>${formatDate(group.latestAt)}</span>
        </div>
        ${eventTag}
        <p class="muted alert-row-meta">${escapeHtml(group.message || 'Alert triggered.')}</p>
        ${labelChips}
        <div class="alert-row-badges">${detectionBadges(group.detections)}</div>
        ${group.recordingId ? `<a class="button-link secondary-link" href="/recordings?recording_id=${encodeURIComponent(group.recordingId)}">View Footage</a>` : ''}
      </div>
    `;
  }).join('');
}

function bindDeleteButtons() {
  document.querySelectorAll('[data-delete-event]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete event #${button.dataset.deleteEvent}? This cannot be undone.`)) return;
      try {
        await api(`/api/events/${button.dataset.deleteEvent}`, { method: 'DELETE' });
        await Promise.all([loadStats(), loadEvents()]);
        bindDeleteButtons();
      } catch (error) {
        alert(`Failed to delete event: ${error.message}`);
      }
    });
  });
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
    // Show all labels if settings unavailable.
  }
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  if (authInfo.user.role !== 'admin') {
    if (els.deleteAllEventsBtn) els.deleteAllEventsBtn.hidden = true;
    if (els.deleteAllAlertsBtn) els.deleteAllAlertsBtn.hidden = true;
  } else {
    if (els.deleteAllEventsBtn) {
      els.deleteAllEventsBtn.hidden = false;
      els.deleteAllEventsBtn.addEventListener('click', async () => {
        if (!confirm('Delete ALL events? This cannot be undone.')) return;
        try {
          await api('/api/events', { method: 'DELETE' });
          await Promise.all([loadStats(), loadEvents()]);
          bindDeleteButtons();
        } catch (error) {
          alert(`Failed to delete events: ${error.message}`);
        }
      });
    }
    if (els.deleteAllAlertsBtn) {
      els.deleteAllAlertsBtn.hidden = false;
      els.deleteAllAlertsBtn.addEventListener('click', async () => {
        if (!confirm('Delete ALL alert history? This cannot be undone.')) return;
        try {
          await api('/api/alerts', { method: 'DELETE' });
          await Promise.all([loadAlerts(), loadStats()]);
        } catch (error) {
          alert(`Failed to delete alert history: ${error.message}`);
        }
      });
    }
  }
}

async function loadStatus() {
  try {
    const [status, aiStatus] = await Promise.all([api('/api/status'), api('/api/status/ai')]);
    els.statusText.textContent = status.status;
    const cameraName = status.camera_name ? ` · ${status.camera_name}` : '';
    els.cameraDetail.textContent = `${status.mode}${cameraName}`;
    els.uptimeText.textContent = formatUptime(status.uptime_seconds);
    const modelLabel = aiStatus.model_name || aiStatus.active_backend;
    els.aiModeText.textContent = modelLabel;
    els.aiModeText.className = `ai-mode ${aiStatus.mode.toLowerCase().replace(/\s+/g, '-')}`;
    const loadedText = aiStatus.model_loaded ? 'loaded' : 'not loaded';
    const errorText = aiStatus.error ? ` · ${aiStatus.error}` : '';
    els.aiStatusDetail.textContent = `${aiStatus.mode} · ${loadedText}${errorText}`;
  } catch (error) {
    els.statusText.textContent = 'offline';
    els.cameraDetail.textContent = '';
    els.uptimeText.textContent = '-';
    els.aiModeText.textContent = 'unknown';
    els.aiModeText.className = 'ai-mode model-failed';
    els.aiStatusDetail.textContent = error.message;
  }
}

async function loadStats() {
  try {
    const stats = await api('/api/stats');
    els.totalEvents.textContent = stats.matched_object_events ?? stats.total_events;
    els.totalAlerts.textContent = stats.total_alerts;
  } catch {
    // Keep last values on failure.
  }
}

async function loadEvents() {
  try {
    renderEvents(await api('/api/events?with_recording=true'));
  } catch {
    els.events.innerHTML = '<div class="empty">Could not load events.</div>';
  }
}

async function loadAlerts() {
  try {
    const alerts = await api('/api/alerts');
    renderAlerts(alerts);
    if (els.deleteAllAlertsBtn && authState.user?.role === 'admin') {
      els.deleteAllAlertsBtn.hidden = alerts.length === 0;
    }
  } catch {
    els.alerts.innerHTML = '<div class="empty">Could not load alerts.</div>';
  }
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadStats(), loadEvents(), loadAlerts()]);
  bindDeleteButtons();
}

loadAuth().then(async () => { await loadConfiguredLabels(); await refreshAll(); }).catch(() => {});
setInterval(loadStatus, 3000);
setInterval(() => loadStats().catch(() => {}), 10000);

// Re-render the dashboard's status / stats / events / alerts when the
// user's date_format / time_format changes in another tab. The 3s/10s
// polling timers will keep these fresh on their own; this hook just makes
// the change feel instant instead of waiting for the next tick.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  if (typeof refreshAll === 'function') refreshAll().catch(() => {});
};
