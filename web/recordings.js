const els = {
  recordings: document.getElementById('recordings'),
  recordingFilter: document.getElementById('recordingFilter'),
  recordingSearchBtn: document.getElementById('recordingSearchBtn'),
  recordingClearBtn: document.getElementById('recordingClearBtn'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
  deleteAllRecordingsBtn: document.getElementById('deleteAllRecordingsBtn'),
};

let authState = { user: null, csrfToken: null };
let recordingRefreshTimer = null;

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authState.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = authState.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : 'Unknown time';
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  return detections.map((d) => `<span class="detection">${escapeHtml(d.label)} · ${Math.round((d.confidence || 0) * 100)}%</span>`).join('');
}

function cameraLabel(recording) {
  const metadata = recording?.event?.metadata || {};
  return metadata.camera_name || recording.camera_id || recording.source || 'unknown';
}

function renderRecordings(recordings) {
  if (!recordings.length) {
    els.recordings.innerHTML = '<div class="empty">No recordings yet.</div>';
    return;
  }
  els.recordings.innerHTML = recordings.map((recording) => {
    const fileName = (recording.file_path || '').split(/[\\/]/).pop();
    const mediaReady = recording.media_ready !== false;
    return `
      <div class="item recording-row" data-recording-row="${recording.id}">
        <div class="item-title">
          <span>Recording #${recording.id}</span>
          <span>${formatDate(recording.started_at)}</span>
        </div>
        <div class="recording-row-badges">${detectionBadges(recording.detections)}</div>
        <p class="muted recording-row-meta">Event #${recording.event_id || 'none'} · ${Number(recording.duration_seconds || 0).toFixed(1)}s · ${escapeHtml(cameraLabel(recording))}</p>
        <p class="muted recording-row-meta">${escapeHtml(recording.trigger_type || 'motion')} ${escapeHtml(recording.trigger_label || '')} · ${escapeHtml(fileName)}</p>
        <div class="button-row">
          <button class="secondary" data-play-recording="${recording.id}" ${mediaReady ? '' : 'disabled'}>${mediaReady ? 'Play' : 'Preparing...'}</button>
          <button class="secondary delete-btn" data-delete-recording="${recording.id}">Delete</button>
        </div>
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
  bindRecordingButtons();
}

function renderRecordingDetails(recording) {
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong>#${recording.id}</strong></div>
    <div><span>Event</span><strong>${recording.event_id || 'none'}</strong></div>
    <div><span>Camera</span><strong>${escapeHtml(cameraLabel(recording))}</strong></div>
    <div><span>Trigger</span><strong>${escapeHtml(recording.trigger_type || 'motion')} ${escapeHtml(recording.trigger_label || '')}</strong></div>
    <div><span>Started</span><strong>${formatDate(recording.started_at)}</strong></div>
    <div><span>Duration</span><strong>${Number(recording.duration_seconds || 0).toFixed(1)}s</strong></div>
    <div class="wide"><span>Detections</span><strong>${(recording.detections || []).map((d) => d.label).join(', ') || 'none'}</strong></div>
  `;
}

async function playRecording(id) {
  const recording = await api(`/api/recordings/${id}`);
  renderRecordingDetails(recording);
  if (recording.media_ready === false) {
    els.clipPlayerStatus.textContent = `Recording #${id} is still being prepared.`;
    return;
  }
  els.clipPlayer.pause();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.clipPlayer.src = `/api/recordings/${id}/stream?t=${Date.now()}`;
  els.clipPlayerStatus.textContent = `Loading recording #${id}...`;
  try {
    els.clipPlayer.load();
    await els.clipPlayer.play();
    els.clipPlayerStatus.textContent = `Playing recording #${id}.`;
  } catch (error) {
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${id} loaded. Press play to start.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${id}: ${error?.message || 'media playback failed'}.`;
  }
}

function bindRecordingButtons() {
  document.querySelectorAll('[data-play-recording]').forEach((button) => {
    button.addEventListener('click', () => playRecording(button.dataset.playRecording));
  });
  document.querySelectorAll('[data-delete-recording]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!confirm(`Delete recording #${button.dataset.deleteRecording}? This cannot be undone.`)) return;
      try {
        await api(`/api/recordings/${button.dataset.deleteRecording}`, { method: 'DELETE' });
        await loadRecordings(els.recordingFilter.value.trim());
      } catch (error) {
        alert(`Failed to delete recording: ${error.message}`);
      }
    });
  });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  if (authInfo.user.role === 'admin') {
    els.deleteAllRecordingsBtn.hidden = false;
    els.deleteAllRecordingsBtn.addEventListener('click', async () => {
      if (!confirm('Delete ALL recordings? This will remove the media files too.')) return;
      await api('/api/recordings', { method: 'DELETE' });
      await loadRecordings();
    });
  }
}

async function loadRecordings(label = '') {
  const params = new URLSearchParams();
  if (label) params.set('label', label);
  renderRecordings(await api(`/api/recordings?${params.toString()}`));
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

els.recordingSearchBtn.addEventListener('click', () => loadRecordings(els.recordingFilter.value.trim()));
els.recordingClearBtn.addEventListener('click', () => {
  els.recordingFilter.value = '';
  loadRecordings();
});
els.recordingFilter.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadRecordings(els.recordingFilter.value.trim());
});

loadAuth().then(async () => {
  await loadRecordings();
  const selected = new URLSearchParams(window.location.search).get('recording_id');
  if (selected) playRecording(selected).catch((error) => { els.clipPlayerStatus.textContent = error.message; });
}).catch((error) => { els.clipPlayerStatus.textContent = error.message; });
