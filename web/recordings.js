const els = {
  recordings: document.getElementById('recordings'),
  cameraFilter: document.getElementById('cameraFilter'),
  recordingFilter: document.getElementById('recordingFilter'),
  recordingSearchBtn: document.getElementById('recordingSearchBtn'),
  recordingClearBtn: document.getElementById('recordingClearBtn'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
  deleteAllRecordingsBtn: document.getElementById('deleteAllRecordingsBtn'),
  clipOverlay: document.getElementById('clipOverlay'),
  clipOverlayToggle: document.getElementById('clipOverlayToggle'),
  clipOverlayTrackToggle: document.getElementById('clipOverlayTrackToggle'),
  videoModal: document.getElementById('videoModal'),
  videoModalClose: document.getElementById('videoModalClose'),
  videoModalDownload: document.getElementById('videoModalDownload'),
  listStatus: document.getElementById('listStatus'),
};

let authState = { user: null, csrfToken: null };
let recordingRefreshTimer = null;
let activeRecording = null;
let overlayResizeObserver = null;
const OVERLAY_TOGGLE_KEY = 'daygle.recordings.overlay.enabled';
const OVERLAY_TRACK_KEY = 'daygle.recordings.overlay.track.enabled';
let overlayEnabled = true;
let overlayTrackEnabled = true;
const GENERIC_TRIGGER_LABELS = new Set(['motion', 'alert', 'human', 'object', 'none', 'off', 'continuous']);

function filterByConfiguredLabels(detections) {
  if (!configuredLabels) return detections;
  return detections.filter((d) => {
    const label = String(d.label || '').trim().toLowerCase();
    return configuredLabels.has(label) || configuredLabels.has('motion') && label === 'motion';
  });
}
let overlayTrackIntervalMs = 300;
const OVERLAY_TRACK_MAX_WIDTH = 640;
const OVERLAY_TRACK_MAX_HEIGHT = 360;
const overlayTrackCanvas = document.createElement('canvas');
let overlayTrackLastRunMs = 0;
let overlayTrackInFlight = false;
let overlayTrackDetections = null;
let overlayTrackPrevDetections = null;
// Video currentTime (seconds) at which each sample's frame was captured, so
// the overlay can be projected onto the frame currently on screen regardless
// of how long detection took.
let overlayTrackCaptureTime = 0;
let overlayTrackPrevCaptureTime = 0;
let overlayRafId = null;
let configuredLabels = null; // null = no filter loaded yet

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authState.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = authState.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) { window.location.href = '/login'; throw new Error('Authentication required'); }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  const normalized = detections
    .map((detection) => ({
      label: String(detection.label || '').trim().toLowerCase(),
      confidence: Number(detection.confidence || 0),
    }))
    .filter((detection) => detection.label && (!configuredLabels || (configuredLabels.has(detection.label) && detection.confidence >= (configuredLabels.get(detection.label) ?? 0))));
  if (!normalized.length) return '<span class="muted">No detections</span>';
  return normalized.map((detection) => `<span class="detection">${escapeHtml(detection.label)} · ${Math.round(detection.confidence * 100)}%</span>`).join('');
}

function cameraLabel(recording) {
  const metadata = recording?.event?.metadata || {};
  return metadata.camera_name || recording.camera_id || recording.source || 'unknown';
}

function recordingTriggerType(recording) {
  return String(recording.trigger_type || 'motion').trim().toLowerCase() || 'motion';
}

function recordingTriggerLabel(recording) {
  return String(recording.trigger_label || '').trim().toLowerCase() || null;
}

function recordingDetectionLabels(recording) {
  const all = Array.from(new Set((recording.detections || [])
    .filter((d) => {
      const label = String(d.label || '').trim().toLowerCase();
      if (!label) return false;
      if (!configuredLabels) return true;
      return configuredLabels.has(label) && Number(d.confidence || 0) >= (configuredLabels.get(label) ?? 0);
    })
    .map((d) => String(d.label || '').trim().toLowerCase())));
  const specific = all.filter((label) => !GENERIC_TRIGGER_LABELS.has(label));
  return specific.length ? specific : all;
}

function recordingDisplayTrigger(recording) {
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);
  const firstSpecificDetection = detectionLabels.find((label) => !GENERIC_TRIGGER_LABELS.has(label));
  const hasDetections = detectionLabels.length > 0;

  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human') {
    if (firstSpecificDetection) return `motion · ${firstSpecificDetection}`;
    // If detections exist and none are specific, trust the detection set and keep this as motion.
    if (!hasDetections && triggerLabel && !GENERIC_TRIGGER_LABELS.has(triggerLabel)) return `motion · ${triggerLabel}`;
    return 'motion';
  }

  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    return triggerType;
  }

  if (triggerLabel && triggerLabel !== triggerType) return `${triggerType} · ${triggerLabel}`;
  return triggerLabel || triggerType;
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
        <p class="muted recording-row-meta">${escapeHtml(recordingDisplayTrigger(recording))} · ${escapeHtml(fileName)}</p>
        <div class="button-row">
          <button class="secondary" data-play-recording="${recording.id}" ${mediaReady ? '' : 'disabled'}>${mediaReady ? 'Play' : 'Preparing...'}</button>
          <button class="secondary delete-btn" data-delete-recording="${recording.id}">Delete</button>
        </div>
      </div>
    `;
  }).join('');
  if (recordings.some((recording) => recording.media_ready === false)) {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = setTimeout(() => loadRecordings(els.recordingFilter.value.trim(), els.cameraFilter?.value || ''), 3000);
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
    <div><span>Trigger</span><strong>${escapeHtml(recordingDisplayTrigger(recording))}</strong></div>
    <div><span>Started</span><strong>${formatDate(recording.started_at)}</strong></div>
    <div><span>Duration</span><strong>${Number(recording.duration_seconds || 0).toFixed(1)}s</strong></div>
    <div class="wide"><span>Detections</span><strong>${(recording.detections || []).filter((d) => { const label = String(d.label || '').trim().toLowerCase(); return label && (!configuredLabels || (configuredLabels.has(label) && Number(d.confidence || 0) >= (configuredLabels.get(label) ?? 0))); }).map((d) => escapeHtml(`${String(d.label || '').trim().toLowerCase()} (${Math.round(Number(d.confidence || 0) * 100)}%)`)).join(', ') || 'none'}</strong></div>
  `;
}

function detectionAnchorSeconds(recording) {
  const startedAt = Date.parse(recording?.started_at || '');
  const eventAt = Date.parse(recording?.event?.created_at || '');
  if (!Number.isFinite(startedAt) || !Number.isFinite(eventAt)) return null;
  const seconds = (eventAt - startedAt) / 1000;
  return Number.isFinite(seconds) ? Math.max(0, seconds) : null;
}

function shouldRenderOverlayForTime(recording, playerTimeSeconds) {
  const anchorSeconds = detectionAnchorSeconds(recording);
  if (anchorSeconds === null) return true;
  return playerTimeSeconds >= anchorSeconds;
}


function clearClipOverlay() {
  if (!els.clipOverlay) return;
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);
}

function clearOverlayTrackDetections() {
  overlayTrackDetections = null;
  overlayTrackPrevDetections = null;
  overlayTrackCaptureTime = 0;
  overlayTrackPrevCaptureTime = 0;
  overlayTrackLastRunMs = 0;
}

function startOverlayRaf() {
  if (overlayRafId !== null) return;
  function loop() {
    if (!overlayTrackEnabled || !els.clipPlayer || els.clipPlayer.paused) {
      overlayRafId = null;
      return;
    }
    detectOverlayFrameDetections();
    drawClipOverlay();
    overlayRafId = requestAnimationFrame(loop);
  }
  overlayRafId = requestAnimationFrame(loop);
}

function stopOverlayRaf() {
  if (overlayRafId !== null) {
    cancelAnimationFrame(overlayRafId);
    overlayRafId = null;
  }
}

async function detectOverlayFrameDetections() {
  if (!overlayTrackEnabled || !activeRecording || !els.clipPlayer) return;
  if (els.clipPlayer.readyState < 2 || els.clipPlayer.videoWidth <= 0 || els.clipPlayer.videoHeight <= 0) return;

  const now = performance.now();
  if (overlayTrackInFlight || now - overlayTrackLastRunMs < overlayTrackIntervalMs) return;
  overlayTrackLastRunMs = now;
  overlayTrackInFlight = true;

  try {
    const sourceWidth = Number(els.clipPlayer.videoWidth || 0);
    const sourceHeight = Number(els.clipPlayer.videoHeight || 0);
    if (sourceWidth <= 0 || sourceHeight <= 0) return;
    const scale = Math.min(1, OVERLAY_TRACK_MAX_WIDTH / sourceWidth, OVERLAY_TRACK_MAX_HEIGHT / sourceHeight);
    const frameWidth = Math.max(1, Math.round(sourceWidth * scale));
    const frameHeight = Math.max(1, Math.round(sourceHeight * scale));

    overlayTrackCanvas.width = frameWidth;
    overlayTrackCanvas.height = frameHeight;
    const context = overlayTrackCanvas.getContext('2d');
    if (!context) return;
    // Capture the frame's position on the playback timeline before sending it
    // off for inference; the result is anchored to this moment, not to when
    // the (latency-delayed) response comes back.
    const captureTime = Number(els.clipPlayer.currentTime || 0);
    context.drawImage(els.clipPlayer, 0, 0, frameWidth, frameHeight);

    const blob = await new Promise((resolve) => {
      overlayTrackCanvas.toBlob((value) => resolve(value), 'image/jpeg', 0.8);
    });
    if (!blob) return;

    const payload = await api('/api/detect/frame', {
      method: 'POST',
      headers: { 'Content-Type': 'image/jpeg' },
      body: blob,
    });
    const detections = Array.isArray(payload?.detections) ? payload.detections : [];
    const newDetections = detections.map((detection) => {
      const normalizedBox = normalizeDetectionBox(detection?.box || {}, frameWidth, frameHeight);
      if (!normalizedBox) return null;
      return {
        ...detection,
        box: normalizedBox,
      };
    }).filter(Boolean);
    overlayTrackPrevDetections = overlayTrackDetections;
    overlayTrackPrevCaptureTime = overlayTrackCaptureTime;
    overlayTrackCaptureTime = captureTime;
    overlayTrackDetections = newDetections;
  } catch (_error) {
    // Keep the last successful overlay detections if transient frame inference fails.
  } finally {
    overlayTrackInFlight = false;
    if (!overlayRafId) drawClipOverlay();
  }
}

function drawClipOverlay() {
  if (!els.clipOverlay || !els.clipPlayer) return;
  resizeOverlayCanvas(els.clipOverlay, els.clipPlayer);
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);
  if (!overlayEnabled) return;

  if (overlayTrackEnabled && !overlayRafId) {
    detectOverlayFrameDetections();
  }

  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  );
  const playerTime = Number(els.clipPlayer.currentTime || 0);
  let rawTrackDetections = overlayTrackEnabled && Array.isArray(overlayTrackDetections) && overlayTrackDetections.length
    ? overlayTrackDetections : null;
  if (rawTrackDetections && overlayTrackPrevDetections) {
    const interval = overlayTrackCaptureTime - overlayTrackPrevCaptureTime;
    const maxLead = Math.max(0.5, interval * 3);
    rawTrackDetections = projectDetections(
      overlayTrackPrevDetections,
      rawTrackDetections,
      overlayTrackPrevCaptureTime,
      overlayTrackCaptureTime,
      playerTime,
      maxLead,
    );
  }
  const detections = rawTrackDetections ? filterByConfiguredLabels(rawTrackDetections) : eventDetections;
  if (!detections.length) return;
  if (!overlayTrackEnabled && !shouldRenderOverlayForTime(activeRecording, playerTime)) return;

  drawDetectionBoxesOnCanvas(els.clipOverlay, detections, els.clipPlayer);
}

function openVideoModal() {
  els.videoModal.hidden = false;
  document.body.style.overflow = 'hidden';
  els.videoModalClose.focus();
}

function closeVideoModal() {
  els.videoModal.hidden = true;
  document.body.style.overflow = '';
  els.clipPlayer.pause();
  stopOverlayRaf();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.videoModalDownload.hidden = true;
  els.videoModalDownload.removeAttribute('href');
  clearClipOverlay();
  clearOverlayTrackDetections();
  activeRecording = null;
  els.clipPlayerStatus.textContent = '';
  els.recordingDetails.innerHTML = '';
}

async function playRecording(id) {
  const recording = await api(`/api/recordings/${id}`);
  activeRecording = recording;
  clearOverlayTrackDetections();
  renderRecordingDetails(recording);
  openVideoModal();
  if (recording.media_ready === false) {
    clearClipOverlay();
    els.clipPlayerStatus.textContent = `Recording #${id} is still being prepared.`;
    return;
  }
  els.videoModalDownload.href = `/api/recordings/${id}/download`;
  els.videoModalDownload.hidden = false;
  els.clipPlayer.pause();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.clipPlayer.src = `/api/recordings/${id}/stream?t=${Date.now()}`;
  drawClipOverlay();
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
      if (!confirm('Delete ALL recordings and media files? Settings, users, and rules will not be changed.')) return;
      const result = await api('/api/recordings', { method: 'DELETE' });
      await loadRecordings();
      const deletedCount = Number(result?.deleted || 0);
      els.listStatus.textContent = `Deleted ${deletedCount} recording${deletedCount === 1 ? '' : 's'}. Settings were not changed.`;
    });
  }
}

async function loadLiveSettings() {
  try {
    const settings = await api('/api/settings/system');

    const intervalMs = Number(settings?.live?.overlay_track_interval_ms);
    if (Number.isFinite(intervalMs) && intervalMs >= 100) overlayTrackIntervalMs = intervalMs;

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
  } catch (_error) {
    // Keep default if settings unavailable.
  }
}

async function loadCameras() {
  try {
    const data = await api('/api/cameras');
    const cameras = data?.cameras || [];
    if (!cameras.length || !els.cameraFilter) return;
    for (const camera of cameras) {
      const option = document.createElement('option');
      option.value = camera.id;
      option.textContent = camera.name || camera.id;
      els.cameraFilter.appendChild(option);
    }
  } catch (_error) {
    // Keep "All Cameras" only if cameras unavailable
  }
}

async function loadRecordings(label = '', cameraId = '') {
  const params = new URLSearchParams();
  if (label) params.set('label', label);
  if (cameraId) params.set('camera_id', cameraId);
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
  clearClipOverlay();
  els.clipPlayerStatus.textContent = messages[error?.code] || 'Unable to play this recording.';
});

['loadedmetadata', 'loadeddata', 'pause', 'seeked', 'timeupdate'].forEach((eventName) => {
  els.clipPlayer.addEventListener(eventName, drawClipOverlay);
});

els.clipPlayer.addEventListener('play', () => {
  if (overlayTrackEnabled) startOverlayRaf();
  drawClipOverlay();
});

els.clipPlayer.addEventListener('pause', () => {
  stopOverlayRaf();
  drawClipOverlay();
});

window.addEventListener('resize', drawClipOverlay);

if ('ResizeObserver' in window && els.clipPlayer) {
  overlayResizeObserver = new ResizeObserver(drawClipOverlay);
  overlayResizeObserver.observe(els.clipPlayer);
}

if (els.clipOverlayToggle) {
  const savedValue = localStorage.getItem(OVERLAY_TOGGLE_KEY);
  overlayEnabled = savedValue !== '0';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
    drawClipOverlay();
  });
}

if (els.clipOverlayTrackToggle) {
  const savedTrackValue = localStorage.getItem(OVERLAY_TRACK_KEY);
  overlayTrackEnabled = savedTrackValue !== '0';
  els.clipOverlayTrackToggle.checked = overlayTrackEnabled;
  els.clipOverlayTrackToggle.addEventListener('change', () => {
    overlayTrackEnabled = Boolean(els.clipOverlayTrackToggle.checked);
    localStorage.setItem(OVERLAY_TRACK_KEY, overlayTrackEnabled ? '1' : '0');
    clearOverlayTrackDetections();
    if (overlayTrackEnabled && els.clipPlayer && !els.clipPlayer.paused) {
      startOverlayRaf();
    } else {
      stopOverlayRaf();
    }
    drawClipOverlay();
  });
}


els.cameraFilter?.addEventListener('change', () => loadRecordings(els.recordingFilter.value.trim(), els.cameraFilter.value));
els.recordingSearchBtn.addEventListener('click', () => loadRecordings(els.recordingFilter.value.trim(), els.cameraFilter?.value || ''));
els.recordingClearBtn.addEventListener('click', () => {
  els.recordingFilter.value = '';
  if (els.cameraFilter) els.cameraFilter.value = '';
  loadRecordings();
});
els.recordingFilter.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadRecordings(els.recordingFilter.value.trim(), els.cameraFilter?.value || '');
});

els.videoModalClose.addEventListener('click', () => closeVideoModal());

els.videoModal.addEventListener('click', (event) => {
  if (event.target === els.videoModal || event.target.classList.contains('video-modal-backdrop')) closeVideoModal();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.videoModal.hidden) closeVideoModal();
});

loadAuth().then(async () => {
  await Promise.all([loadCameras(), loadLiveSettings()]);
  await loadRecordings();
  const selected = new URLSearchParams(window.location.search).get('recording_id');
  if (selected) playRecording(selected).catch((error) => { els.listStatus.textContent = error.message; });
}).catch((error) => { els.listStatus.textContent = error.message; });
