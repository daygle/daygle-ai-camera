const els = {
  statusText: document.getElementById('statusText'),
  aiModeText: document.getElementById('aiModeText'),
  aiStatusDetail: document.getElementById('aiStatusDetail'),
  totalEvents: document.getElementById('totalEvents'),
  totalAlerts: document.getElementById('totalAlerts'),
  frameNumber: document.getElementById('frameNumber'),
  searchBtn: document.getElementById('searchBtn'),
  clearBtn: document.getElementById('clearBtn'),
  searchInput: document.getElementById('searchInput'),
  events: document.getElementById('events'),
  alerts: document.getElementById('alerts'),
  objectStats: document.getElementById('objectStats'),
  plateFilter: document.getElementById('plateFilter'),
  plateSearchBtn: document.getElementById('plateSearchBtn'),
  plateClearBtn: document.getElementById('plateClearBtn'),
  plates: document.getElementById('plates'),
  plateSightings: document.getElementById('plateSightings'),
  deleteAllObjectsBtn: document.getElementById('deleteAllObjectsBtn'),
  deleteAllEventsBtn: document.getElementById('deleteAllEventsBtn'),
  deleteAllAlertsBtn: document.getElementById('deleteAllAlertsBtn'),
  deleteAllPlatesBtn: document.getElementById('deleteAllPlatesBtn'),
};

let authState = { user: null, csrfToken: null };

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

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  })[char]);
}

function formatDate(value) {
  if (!value) return 'Unknown time';
  return new Date(value).toLocaleString();
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

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  return detections.map((d) => {
    const confidence = Math.round((d.confidence || 0) * 100);
    return `<span class="detection">${escapeHtml(d.label)} · ${confidence}%</span>`;
  }).join('');
}

function plateBadges(plateEvents = []) {
  if (!plateEvents.length) return '';
  return plateEvents.map((p) => `<span class="detection">${escapeHtml(p.plate_number)} ${Math.round((p.confidence || 0) * 100)}%</span>`).join('');
}

function recordingLink(recordings = []) {
  if (!recordings.length) return '<span class="muted">Recording: none</span>';
  return recordings.map((recording) => `<a class="link-button" href="/recordings?recording_id=${recording.id}">Recording #${recording.id}</a>`).join('');
}

function renderEvents(events) {
  if (!events.length) {
    els.events.innerHTML = '<div class="empty">No enabled alert events yet.</div>';
    return;
  }

  els.events.innerHTML = events.map((event) => `
    <div class="item event-row">
      <div class="item-title">
        <span>Event #${event.id}</span>
        <span>${formatDate(event.created_at)}</span>
      </div>
      <div class="event-row-badges">${detectionBadges(event.detections)}${plateBadges(event.plate_events)}</div>
      <p class="muted event-row-meta">Camera: ${escapeHtml(eventSourceLabel(event))} · ${escapeHtml(event.recording_status || 'none')}</p>
      <div class="event-row-footer">
        <div>${recordingLink(event.recordings)}</div>
        <button class="secondary delete-btn" data-delete-event="${event.id}">Delete</button>
      </div>
    </div>
  `).join('');
}

function renderAlerts(alerts) {
  if (!alerts.length) {
    els.alerts.innerHTML = '<div class="empty">No alerts triggered yet.</div>';
    return;
  }

  els.alerts.innerHTML = alerts.map((alert) => `
    <div class="item alert-row">
      <div class="item-title">
        <span>${escapeHtml(alert.rule_name)}</span>
        <span>${formatDate(alert.created_at)}</span>
      </div>
      <p class="muted alert-row-meta">${escapeHtml(alert.message)}</p>
      <div class="alert-row-badges"><span class="detection">${escapeHtml(alert.label)} · ${Math.round(alert.confidence * 100)}%</span></div>
    </div>
  `).join('');
}

function renderObjectStats(objects = []) {
  if (!objects.length) {
    els.objectStats.innerHTML = '<span class="muted">No objects indexed yet.</span>';
    return;
  }

  els.objectStats.innerHTML = objects.map((obj) => `
    <button class="chip" data-label="${escapeHtml(obj.label)}">${escapeHtml(obj.label)} · ${obj.count}</button>
  `).join('');

  document.querySelectorAll('[data-label]').forEach((button) => {
    button.addEventListener('click', () => {
      els.searchInput.value = button.dataset.label;
      loadEvents(button.dataset.label);
    });
  });
}

function renderPlates(plates = []) {
  if (!plates.length) {
    els.plates.innerHTML = '<div class="empty">No plates seen yet.</div>';
    return;
  }
  els.plates.innerHTML = plates.map((plate) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(plate.plate_number)}</span><span>${plate.sighting_count} sighting(s)</span></div>
      <p class="muted">First seen: ${formatDate(plate.first_seen)} · Last seen: ${formatDate(plate.last_seen)}</p>
      <p class="muted">${plate.is_blacklisted ? 'Blacklisted' : plate.is_whitelisted ? 'Whitelisted' : 'Unknown'} ${plate.notes ? `· ${escapeHtml(plate.notes)}` : ''}</p>
      <button class="secondary delete-btn" data-delete-plate="${plate.id}">Delete</button>
    </div>
  `).join('');
}

function renderPlateSightings(events = []) {
  if (!events.length) {
    els.plateSightings.innerHTML = '<div class="empty">No plate sightings match.</div>';
    return;
  }
  els.plateSightings.innerHTML = events.map((event) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(event.plate_number)}</span><span>${Math.round(event.confidence * 100)}%</span></div>
      <p class="muted">${formatDate(event.created_at)} · Event #${event.event_id}</p>
      <div>${recordingLink(event.event?.recordings || [])}</div>
    </div>
  `).join('');
}

function bindDeleteButtons() {
  document.querySelectorAll('[data-delete-event]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete event #${button.dataset.deleteEvent}? This cannot be undone.`)) return;
      try {
        await api(`/api/events/${button.dataset.deleteEvent}`, { method: 'DELETE' });
        await Promise.all([loadStats(), loadEvents(els.searchInput.value.trim())]);
        bindDeleteButtons();
      } catch (error) {
        alert(`Failed to delete event: ${error.message}`);
      }
    });
  });
  document.querySelectorAll('[data-delete-plate]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete plate #${button.dataset.deletePlate}? This cannot be undone.`)) return;
      try {
        await api(`/api/plates/${button.dataset.deletePlate}`, { method: 'DELETE' });
        await Promise.all([loadPlates(), searchPlateSightings(els.plateFilter.value.trim())]);
        bindDeleteButtons();
      } catch (error) {
        alert(`Failed to delete plate: ${error.message}`);
      }
    });
  });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  if (authInfo.user.role !== 'admin') {
    if (els.deleteAllObjectsBtn) els.deleteAllObjectsBtn.hidden = true;
    if (els.deleteAllEventsBtn) els.deleteAllEventsBtn.hidden = true;
    if (els.deleteAllAlertsBtn) els.deleteAllAlertsBtn.hidden = true;
    if (els.deleteAllPlatesBtn) els.deleteAllPlatesBtn.hidden = true;
  } else {
    if (els.deleteAllObjectsBtn) {
      els.deleteAllObjectsBtn.hidden = false;
      els.deleteAllObjectsBtn.addEventListener('click', async () => {
        if (!confirm('Delete ALL object detections used by Object Search? This cannot be undone.')) return;
        try {
          await api('/api/objects', { method: 'DELETE' });
          await Promise.all([loadStats(), loadEvents(els.searchInput.value.trim())]);
          bindDeleteButtons();
        } catch (error) {
          alert(`Failed to delete object detections: ${error.message}`);
        }
      });
    }
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
    if (els.deleteAllPlatesBtn) {
      els.deleteAllPlatesBtn.hidden = false;
      els.deleteAllPlatesBtn.addEventListener('click', async () => {
        if (!confirm('Delete ALL plates and sightings? This cannot be undone.')) return;
        try {
          await api('/api/plates', { method: 'DELETE' });
          await Promise.all([loadPlates(), searchPlateSightings(els.plateFilter.value.trim())]);
          bindDeleteButtons();
        } catch (error) {
          alert(`Failed to delete plates: ${error.message}`);
        }
      });
    }
  }
}

async function loadStatus() {
  try {
    const [status, aiStatus] = await Promise.all([api('/api/status'), api('/api/status/ai')]);
    els.statusText.textContent = `${status.status} · ${status.mode}`;
    els.frameNumber.textContent = status.frame_number;
    els.aiModeText.textContent = aiStatus.mode;
    els.aiModeText.className = `ai-mode ${aiStatus.mode.toLowerCase().replace(/\s+/g, '-')}`;
    const errorText = aiStatus.error ? ` · ${aiStatus.error}` : '';
    els.aiStatusDetail.textContent = `${aiStatus.active_backend} · model loaded: ${aiStatus.model_loaded}${errorText}`;
  } catch (error) {
    els.statusText.textContent = 'offline';
    els.aiModeText.textContent = 'MODEL FAILED';
    els.aiModeText.className = 'ai-mode model-failed';
    els.aiStatusDetail.textContent = error.message;
  }
}

async function loadStats() {
  const stats = await api('/api/stats');
  els.totalEvents.textContent = stats.total_events;
  els.totalAlerts.textContent = stats.total_alerts;
  renderObjectStats(stats.objects);
  if (els.deleteAllObjectsBtn && authState.user?.role === 'admin') {
    els.deleteAllObjectsBtn.hidden = !(stats.objects || []).length;
  }
}

async function loadEvents(label = '') {
  const params = new URLSearchParams({ alerted_only: 'true' });
  if (label) params.set('label', label);
  renderEvents(await api(`/api/events?${params.toString()}`));
}

async function loadAlerts() {
  const alerts = await api('/api/alerts');
  renderAlerts(alerts);
  if (els.deleteAllAlertsBtn && authState.user?.role === 'admin') {
    els.deleteAllAlertsBtn.hidden = alerts.length === 0;
  }
}

async function loadPlates() {
  const plates = await api('/api/plates');
  renderPlates(plates);
  if (els.deleteAllPlatesBtn && authState.user?.role === 'admin') {
    els.deleteAllPlatesBtn.hidden = plates.length === 0;
  }
}

async function searchPlateSightings(query = '') {
  const q = query ? encodeURIComponent(query) : '';
  renderPlateSightings(await api(`/api/plates/search?q=${q}`));
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadStats(), loadEvents(), loadAlerts(), loadPlates(), searchPlateSightings()]);
  bindDeleteButtons();
}

els.searchBtn.addEventListener('click', () => {
  const label = els.searchInput.value.trim();
  loadEvents(label).then(() => bindDeleteButtons());
});
els.clearBtn.addEventListener('click', () => {
  els.searchInput.value = '';
  loadEvents().then(() => bindDeleteButtons());
});
els.plateSearchBtn.addEventListener('click', () => searchPlateSightings(els.plateFilter.value.trim()));
els.plateClearBtn.addEventListener('click', () => {
  els.plateFilter.value = '';
  loadPlates();
  searchPlateSightings();
});
els.searchInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadEvents(els.searchInput.value.trim());
});

loadAuth().then(refreshAll);
setInterval(loadStatus, 3000);
setInterval(loadStats, 10000);
