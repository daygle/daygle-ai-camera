const els = {
  statusText: document.getElementById('statusText'),
  aiModeText: document.getElementById('aiModeText'),
  aiStatusDetail: document.getElementById('aiStatusDetail'),
  totalEvents: document.getElementById('totalEvents'),
  totalAlerts: document.getElementById('totalAlerts'),
  frameNumber: document.getElementById('frameNumber'),
  userMenuBtn: document.getElementById('userMenuBtn'),
  usersLink: document.getElementById('usersLink'),
  settingsLink: document.getElementById('settingsLink'),
  alertSettingsLink: document.getElementById('alertSettingsLink'),
  systemSettingsLink: document.getElementById('systemSettingsLink'),
  searchBtn: document.getElementById('searchBtn'),
  clearBtn: document.getElementById('clearBtn'),
  searchInput: document.getElementById('searchInput'),
  events: document.getElementById('events'),
  alerts: document.getElementById('alerts'),
  objectStats: document.getElementById('objectStats'),
  recordings: document.getElementById('recordings'),
  recordingFilter: document.getElementById('recordingFilter'),
  recordingSearchBtn: document.getElementById('recordingSearchBtn'),
  recordingClearBtn: document.getElementById('recordingClearBtn'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  plateFilter: document.getElementById('plateFilter'),
  plateSearchBtn: document.getElementById('plateSearchBtn'),
  plateClearBtn: document.getElementById('plateClearBtn'),
  plates: document.getElementById('plates'),
  plateSightings: document.getElementById('plateSightings'),
  deleteAllEventsBtn: document.getElementById('deleteAllEventsBtn'),
  deleteAllAlertsBtn: document.getElementById('deleteAllAlertsBtn'),
  deleteAllRecordingsBtn: document.getElementById('deleteAllRecordingsBtn'),
  deleteAllPlatesBtn: document.getElementById('deleteAllPlatesBtn'),
};

let authState = { user: null, csrfToken: null };
let recordingRefreshTimer = null;
['userMenuBtn', 'usersLink', 'settingsLink', 'alertSettingsLink', 'systemSettingsLink'].forEach((key) => {
  if (!els[key]) els[key] = { hidden: false, textContent: '' };
});

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

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  return detections.map((d) => {
    const confidence = Math.round((d.confidence || 0) * 100);
    return `<span class="detection">${escapeHtml(d.label)} · ${confidence}%</span>`;
  }).join('');
}

function renderEvents(events) {
  if (!events.length) {
    els.events.innerHTML = '<div class="empty">No enabled alert events yet.</div>';
    return;
  }

  els.events.innerHTML = events.map((event) => `
    <div class="item">
      <div class="item-title">
        <span>Event #${event.id}</span>
        <span>${formatDate(event.created_at)}</span>
      </div>
      <div>${detectionBadges(event.detections)}</div>
      <div>${plateBadges(event.plate_events)}</div>
      <p class="muted">Source: ${escapeHtml(event.source)} · ${escapeHtml(event.recording_status || 'none')}</p>
      <div>${recordingLink(event.recordings)}</div>
      <button class="secondary delete-btn" data-delete-event="${event.id}">Delete</button>
    </div>
  `).join('');
}

function recordingLink(recordings = []) {
  if (!recordings.length) return '<span class="muted">Recording: none</span>';
  return recordings.map((recording) => `<button class="link-button" data-play-recording="${recording.id}">Recording #${recording.id}</button>`).join('');
}

function renderAlerts(alerts) {
  if (!alerts.length) {
    els.alerts.innerHTML = '<div class="empty">No alerts triggered yet.</div>';
    return;
  }

  els.alerts.innerHTML = alerts.map((alert) => `
    <div class="item">
      <div class="item-title">
        <span>${escapeHtml(alert.rule_name)}</span>
        <span>${formatDate(alert.created_at)}</span>
      </div>
      <p>${escapeHtml(alert.message)}</p>
      <span class="detection">${escapeHtml(alert.label)} · ${Math.round(alert.confidence * 100)}%</span>
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
      loadRecordings(button.dataset.label);
    });
  });
}


function renderRecordings(recordings) {
  if (!recordings.length) {
    els.recordings.innerHTML = '<div class="empty">No enabled alert recordings yet.</div>';
    return;
  }

  els.recordings.innerHTML = recordings.map((recording) => {
    const eventLabel = recording.event_id ? `Event #${recording.event_id}` : 'No linked event';
    const fileName = (recording.file_path || '').split('/').pop();
    const mediaReady = recording.media_ready !== false;
    return `
      <div class="item">
        <div class="item-title">
          <span>Recording #${recording.id} · ${escapeHtml(eventLabel)}</span>
          <span>${formatDate(recording.started_at)}</span>
        </div>
        <div>${detectionBadges(recording.detections)}</div>
        <div>${plateBadges(recording.plate_events)}</div>
        <p class="muted">Duration: ${Number(recording.duration_seconds || 0).toFixed(1)}s · Source: ${escapeHtml(recording.source)} · Trigger: ${escapeHtml(recording.trigger_type || 'motion')} ${escapeHtml(recording.trigger_label || '')} · ${escapeHtml(fileName)}</p>
        <button class="secondary" data-play-recording="${recording.id}" ${mediaReady ? '' : 'disabled'}>${mediaReady ? 'Play clip' : 'Preparing clip...'}</button>
        <button class="secondary delete-btn" data-delete-recording="${recording.id}">Delete</button>
      </div>
    `;
  }).join('');
  if (recordings.some((recording) => recording.media_ready === false)) {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = setTimeout(() => loadRecordings(els.recordingFilter.value.trim()), 3000);
  } else {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = null;
  }
}

function bindPlaybackButtons() {
  document.querySelectorAll('[data-play-recording]').forEach((button) => {
    button.addEventListener('click', () => {
      playRecording(button.dataset.playRecording);
    });
  });
}

function bindDeleteButtons() {
  document.querySelectorAll('[data-delete-event]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete event #${button.dataset.deleteEvent}? This cannot be undone.`)) return;
      try {
        await api(`/api/events/${button.dataset.deleteEvent}`, { method: 'DELETE' });
        await Promise.all([loadStats(), loadEvents(els.searchInput.value.trim()), loadRecordings(els.recordingFilter.value.trim())]);
        bindDeleteButtons();
        bindPlaybackButtons();
      } catch (error) {
        alert(`Failed to delete event: ${error.message}`);
      }
    });
  });
  document.querySelectorAll('[data-delete-recording]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete recording #${button.dataset.deleteRecording}? This cannot be undone.`)) return;
      try {
        await api(`/api/recordings/${button.dataset.deleteRecording}`, { method: 'DELETE' });
        await loadRecordings(els.recordingFilter.value.trim());
        bindDeleteButtons();
        bindPlaybackButtons();
      } catch (error) {
        alert(`Failed to delete recording: ${error.message}`);
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

async function playRecording(id) {
  if (!id) return;
  const recording = await api(`/api/recordings/${id}`);
  if (recording.media_ready === false) {
    els.clipPlayerStatus.textContent = `Recording #${id} is still being prepared. Try again in a few seconds.`;
    return;
  }
  const clipUrl = `/api/recordings/${id}/stream?t=${Date.now()}`;
  els.clipPlayerStatus.textContent = `Loading recording #${id}...`;
  els.clipPlayer.pause();
  els.clipPlayer.src = clipUrl;
  els.clipPlayer.load();
  try {
    await els.clipPlayer.play();
    els.clipPlayerStatus.textContent = `Playing recording #${id}.`;
  } catch (error) {
    if (error?.name === 'AbortError') {
      els.clipPlayerStatus.textContent = `Recording #${id} loaded. Press play to start.`;
      return;
    }
    if (error?.name === 'NotAllowedError') {
      els.clipPlayerStatus.textContent = `Recording #${id} loaded. Press play to start.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${id}: ${error?.message || 'media playback failed'}.`;
  }
}

els.clipPlayer.addEventListener('error', () => {
  const error = els.clipPlayer.error;
  const messages = {
    1: 'Playback was aborted.',
    2: 'The recording could not be downloaded.',
    3: 'The recording could not be decoded by this browser.',
    4: 'The recording format is not supported by this browser.',
  };
  els.clipPlayerStatus.textContent = messages[error?.code] || 'Unable to play this recording.';
});

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  els.userMenuBtn.textContent = `${authInfo.user.username} ▼`;
  if (authInfo.user.role !== 'admin') {
    els.usersLink.hidden = true;
    els.settingsLink.hidden = true;
    els.alertSettingsLink.hidden = true;
    els.systemSettingsLink.hidden = true;
    if (els.deleteAllEventsBtn) els.deleteAllEventsBtn.hidden = true;
    if (els.deleteAllAlertsBtn) els.deleteAllAlertsBtn.hidden = true;
    if (els.deleteAllRecordingsBtn) els.deleteAllRecordingsBtn.hidden = true;
    if (els.deleteAllPlatesBtn) els.deleteAllPlatesBtn.hidden = true;
  } else {
    if (els.deleteAllEventsBtn) {
      els.deleteAllEventsBtn.hidden = false;
      els.deleteAllEventsBtn.addEventListener('click', async () => {
        if (!confirm('Delete ALL events? This cannot be undone.')) return;
        try {
          const result = await api('/api/events', { method: 'DELETE' });
          await Promise.all([loadStats(), loadEvents(), loadRecordings()]);
          bindPlaybackButtons();
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
    if (els.deleteAllRecordingsBtn) {
      els.deleteAllRecordingsBtn.hidden = false;
      els.deleteAllRecordingsBtn.addEventListener('click', async () => {
        if (!confirm('Delete ALL recordings? This will remove the media files too. Cannot be undone.')) return;
        try {
          const result = await api('/api/recordings', { method: 'DELETE' });
          await loadRecordings();
          bindPlaybackButtons();
          bindDeleteButtons();
        } catch (error) {
          alert(`Failed to delete recordings: ${error.message}`);
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

function plateBadges(plateEvents = []) {
  if (!plateEvents.length) return '<span class="muted">No plates</span>';
  return plateEvents.map((p) => `<span class="detection">${escapeHtml(p.plate_number)} ${Math.round((p.confidence || 0) * 100)}%</span>`).join('');
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
}

async function loadEvents(label = '') {
  const params = new URLSearchParams({ alerted_only: 'true' });
  if (label) params.set('label', label);
  const path = `/api/events?${params.toString()}`;
  renderEvents(await api(path));
}

async function loadAlerts() {
  const alerts = await api('/api/alerts');
  renderAlerts(alerts);
  if (els.deleteAllAlertsBtn && authState.user?.role === 'admin') {
    els.deleteAllAlertsBtn.hidden = alerts.length === 0;
  }
}

async function loadRecordings(label = '') {
  const params = new URLSearchParams({ alerted_only: 'true' });
  if (label) params.set('label', label);
  const path = `/api/recordings?${params.toString()}`;
  renderRecordings(await api(path));
  bindPlaybackButtons();
}

async function loadPlates() {
  const plates = await api('/api/plates');
  renderPlates(plates);
  if (els.deleteAllPlatesBtn && authState.user?.role === 'admin') {
    els.deleteAllPlatesBtn.hidden = plates.length === 0;
  }
}

async function searchPlateSightings(query = '') {
  const path = query ? `/api/plates/search?q=${encodeURIComponent(query)}` : '/api/plates/search?q=';
  renderPlateSightings(await api(path));
  bindPlaybackButtons();
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadStats(), loadEvents(), loadAlerts(), loadRecordings(), loadPlates(), searchPlateSightings()]);
  bindPlaybackButtons();
  bindDeleteButtons();
}

els.searchBtn.addEventListener('click', () => {
  const label = els.searchInput.value.trim();
  loadEvents(label).then(() => { bindDeleteButtons(); bindPlaybackButtons(); });
  loadRecordings(label).then(() => { bindPlaybackButtons(); bindDeleteButtons(); });
});
els.clearBtn.addEventListener('click', () => {
  els.searchInput.value = '';
  loadEvents().then(() => { bindDeleteButtons(); bindPlaybackButtons(); });
  loadRecordings().then(() => { bindPlaybackButtons(); bindDeleteButtons(); });
});
els.recordingSearchBtn.addEventListener('click', () => loadRecordings(els.recordingFilter.value.trim()).then(() => { bindPlaybackButtons(); bindDeleteButtons(); }));
els.recordingClearBtn.addEventListener('click', () => {
  els.recordingFilter.value = '';
  loadRecordings().then(() => { bindPlaybackButtons(); bindDeleteButtons(); });
});
els.plateSearchBtn.addEventListener('click', () => searchPlateSightings(els.plateFilter.value.trim()));
els.plateClearBtn.addEventListener('click', () => {
  els.plateFilter.value = '';
  loadPlates();
  searchPlateSightings();
});
els.searchInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    const label = els.searchInput.value.trim();
    loadEvents(label);
    loadRecordings(label);
  }
});
els.recordingFilter.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadRecordings(els.recordingFilter.value.trim());
});

loadAuth().then(refreshAll);
setInterval(loadStatus, 3000);
setInterval(loadStats, 10000);
