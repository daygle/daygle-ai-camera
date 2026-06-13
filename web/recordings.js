const els = {
  recordings: document.getElementById('recordings'),
  cameraFilter: document.getElementById('cameraFilter'),
  recordingDateFrom: document.getElementById('recordingDateFrom'),
  recordingDateTo: document.getElementById('recordingDateTo'),
  recordingSort: document.getElementById('recordingSort'),
  recordingSearchBtn: document.getElementById('recordingSearchBtn'),
  recordingClearBtn: document.getElementById('recordingClearBtn'),
  filterForm: document.getElementById('recordingsFilterForm'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
  deleteAllRecordingsBtn: document.getElementById('deleteAllRecordingsBtn'),
  clipOverlay: document.getElementById('clipOverlay'),
  clipOverlayToggle: document.getElementById('clipOverlayToggle'),
  videoModal: document.getElementById('videoModal'),
  videoModalClose: document.getElementById('videoModalClose'),
  videoModalDownload: document.getElementById('videoModalDownload'),
  videoModalSubtitle: document.getElementById('videoModalSubtitle'),
  listStatus: document.getElementById('listStatus'),
  statTotalClips: document.getElementById('statTotalClips'),
  statTotalDuration: document.getElementById('statTotalDuration'),
  statCameraCount: document.getElementById('statCameraCount'),
  statFilterStatus: document.getElementById('statFilterStatus'),
  statFilterHint: document.getElementById('statFilterHint'),
  // Multi-select label dropdown
  labelSelectTrigger: document.getElementById('labelSelectTrigger'),
  labelSelectText: document.getElementById('labelSelectText'),
  labelSelectDropdown: document.getElementById('labelSelectDropdown'),
  labelSelectSearch: document.getElementById('labelSelectSearch'),
  labelOptionsObjects: document.getElementById('labelOptionsObjects'),
  labelOptionsSounds: document.getElementById('labelOptionsSounds'),
  labelGroupObjects: document.getElementById('labelGroupObjects'),
  labelGroupSounds: document.getElementById('labelGroupSounds'),
};

let authState = { user: null, csrfToken: null };
// Date/time display preferences are global (utils.daygleDatePrefs) and are
// populated by nav.js from /api/auth/me — no page-local state to maintain.
let recordingRefreshTimer = null;
let activeRecording = null;
let overlayResizeObserver = null;
// Estimated frame duration (seconds) derived from the video element, used
// to project detection boxes one frame ahead of the VFC mediaTime.
let _frameDuration = 1 / 30; // default 30fps, updated on each VFC frame
const OVERLAY_TOGGLE_KEY = 'daygle.recordings.overlay.enabled';
// Off by default to save CPU; users opt in per-browser via the toggle.
let overlayEnabled = false;
const GENERIC_TRIGGER_LABELS = new Set(['motion', 'alert', 'human', 'object', 'none', 'off', 'continuous']);

function filterByConfiguredLabels(detections) {
  if (!configuredLabels) return detections;
  return detections.filter((d) => {
    const label = String(d.label || '').trim().toLowerCase();
    return configuredLabels.has(label) || configuredLabels.has('motion') && label === 'motion';
  });
}
let overlayRafId = null;
let overlayVfcHandle = null;
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

const DETECTION_EYE_ICON = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/></svg>';

function detectionPill(label, confidence, isSound) {
  const display = isSound
    ? titleCase(String(label).replace(/_/g, ' '))
    : titleCase(String(label));
  const pct = Math.round(Number(confidence) * 100);
  if (isSound) {
    return `<span class="detection detection-sound">🔊 ${escapeHtml(display)} · ${pct}%</span>`;
  }
  return `<span class="detection detection-object">${DETECTION_EYE_ICON} ${escapeHtml(display)} · ${pct}%</span>`;
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
  // Prefer the server-side `labels` array (one row per unique object detected
  // inside the recording, joined via recording_labels). Fall back to deriving
  // from the per-event detections when the join table is empty (e.g. very old
  // recordings that pre-date the multi-label upgrade).
  if (Array.isArray(recording.labels) && recording.labels.length) {
    return recording.labels
      .map((label) => String(label || '').trim().toLowerCase())
      .filter((label) => label && !GENERIC_TRIGGER_LABELS.has(label));
  }
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
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    const classLabel = meta.class_label || meta.label || recording.trigger_label || 'sound';
    return `🔊 ${titleCase(classLabel)}`;
  }

  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  const detectionLabels = recordingDetectionLabels(recording);
  const hasDetections = detectionLabels.length > 0;

  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human' || triggerType === 'object') {
    // Show ALL concrete object labels joined by · on the pill (e.g. "Person · Cat · Dog").
    if (detectionLabels.length) {
      return detectionLabels.map((label) => titleCase(label)).join(' · ');
    }
    // If detections exist and none are specific, trust the detection set and keep this as motion.
    if (!hasDetections && triggerLabel && !GENERIC_TRIGGER_LABELS.has(triggerLabel)) return `${triggerType} · ${triggerLabel}`;
    return triggerType;
  }

  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    return triggerType;
  }

  if (triggerLabel && triggerLabel !== triggerType) return `${triggerType} · ${triggerLabel}`;
  return triggerLabel || triggerType;
}

function formatDurationShort(totalSeconds) {
  const seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remSeconds}s`;
  const hours = Math.floor(minutes / 60);
  const remMinutes = minutes % 60;
  return remMinutes ? `${hours}h ${remMinutes}m` : `${hours}h`;
}

function updateFilterStat(label, hint) {
  if (!els.statFilterStatus || !els.statFilterHint) return;
  els.statFilterStatus.textContent = label;
  els.statFilterHint.textContent = hint;
}

function formatIsoDateForFilter(dateString, endOfDay = false) {
  if (!dateString) return '';
  // The browser returns YYYY-MM-DD without a timezone. Anchor from/to bounds
  // to the start/end of the day in local time so the filter feels intuitive
  // (a chosen "To" date should include recordings from that day).
  const [year, month, day] = dateString.split('-').map((part) => Number.parseInt(part, 10));
  if (!year || !month || !day) return '';
  const date = new Date(year, month - 1, day, endOfDay ? 23 : 0, endOfDay ? 59 : 0, endOfDay ? 59 : 0, 0);
  return date.toISOString();
}

// ── Multi-select label state ────────────────────────────────────────────
let selectedLabels = new Set();
let allLabelOptions = []; // { value, label, group: 'objects'|'sounds' }

function currentFilterValues() {
  return {
    sourceType: '',
    label: [...selectedLabels].join(','),
    cameraId: els.cameraFilter?.value || '',
    dateFrom: els.recordingDateFrom?.value || '',
    dateTo: els.recordingDateTo?.value || '',
    sort: els.recordingSort?.value || 'newest',
  };
}

function describeFilters(filters) {
  const parts = [];
  if (filters.sourceType) parts.push(`type “${filters.sourceType}”`);
  if (filters.label) {
    const labelList = filters.label.split(',').filter(Boolean);
    if (labelList.length === 1) {
      const opt = allLabelOptions.find((o) => o.value === labelList[0]);
      parts.push(`label “${opt ? opt.label : labelList[0]}”`);
    } else if (labelList.length > 1) {
      parts.push(`labels (${labelList.length})`);
    }
  }
  if (filters.cameraId) {
    const cameraOption = Array.from(els.cameraFilter?.options || []).find((o) => o.value === filters.cameraId);
    parts.push(`camera “${cameraOption?.textContent || filters.cameraId}”`);
  }
  if (filters.dateFrom) parts.push(`from ${formatUserDate(filters.dateFrom)}`);
  if (filters.dateTo) parts.push(`through ${formatUserDate(filters.dateTo)}`);
  return parts;
}

function renderStats(recordings) {
  if (els.statTotalClips) els.statTotalClips.textContent = String(recordings.length);
  if (els.statTotalDuration) {
    const totalSeconds = recordings.reduce((sum, rec) => sum + (Number(rec.duration_seconds) || 0), 0);
    els.statTotalDuration.textContent = formatDurationShort(totalSeconds);
  }
  if (els.statCameraCount) {
    const cameras = new Set(recordings.map((rec) => cameraLabel(rec)).filter(Boolean));
    els.statCameraCount.textContent = String(cameras.size);
  }
}

function renderRecordings(recordings) {
  renderStats(recordings);
  if (!recordings.length) {
    els.recordings.innerHTML = `
      <div class="recordings-empty-state">
        <div class="recordings-empty-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
        </div>
        <h2>No recordings match the current filters</h2>
        <p class="muted">Try resetting the filters, or wait for a new event to be captured.</p>
      </div>`;
    return;
  }
  els.recordings.innerHTML = recordings.map((recording) => {
    const mediaReady = recording.media_ready !== false;
    const isSound = isSoundRecording(recording);
    const typeClass = isSound ? 'activity-item-sound' : 'activity-item-event';
    const typeLabel = isSound ? 'Sound Recording' : 'Object Recording';
    const icon = isSound
      ? '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>'
      : '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>';
    const zones = recordingZoneNames(recording);
    const metaParts = [`Camera: ${escapeHtml(cameraLabel(recording))}`];
    if (zones.length) metaParts.push(`Zone: ${zones.map(escapeHtml).join(', ')}`);
    metaParts.push(`Duration: ${Number(recording.duration_seconds || 0).toFixed(1)}s`);
    if (!mediaReady) metaParts.push('Preparing...');
    const badges = recordingDetectionSummary(recording).map((d) => detectionPill(d.label, d.confidence, isSound)).join('') || '<span class="muted">No detections</span>';
    return `
      <div class="item activity-item ${typeClass}" data-recording-row="${recording.id}">
        <div class="activity-item-icon">${icon}</div>
        <div class="activity-item-main">
          <div class="activity-item-header">
            <div class="activity-item-title">
              <span class="activity-item-type">${typeLabel}</span>
              <span class="activity-item-name">Recording #${recording.id}</span>
            </div>
            <div class="activity-item-when">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              <span>${escapeHtml(formatDateTime(recording.started_at))}</span>
            </div>
          </div>
          <p class="muted activity-item-meta">${metaParts.join(' · ')}</p>
          <div class="activity-item-badges">${badges}</div>
        </div>
        <div class="recording-row-actions">
          <button class="secondary" data-play-recording="${recording.id}" ${mediaReady ? '' : 'disabled'}>
            ${mediaReady
              ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg> Play'
              : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg> Preparing...'}
          </button>
          <button class="secondary delete-btn" data-delete-recording="${recording.id}" aria-label="Delete recording #${recording.id}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
            Delete
          </button>
        </div>
      </div>
    `;
  }).join('');
  if (recordings.some((recording) => recording.media_ready === false)) {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = setTimeout(() => loadRecordings(), 3000);
  } else {
    clearTimeout(recordingRefreshTimer);
    recordingRefreshTimer = null;
  }
  bindRecordingButtons();
}

function isSoundRecording(recording) {
  return recording?.event?.metadata?.source === 'sound-detection';
}

function recordingZoneNames(recording) {
  if (isSoundRecording(recording)) return [];
  return [...new Set((recording.detections || []).map((d) => d.zone_name).filter(Boolean))];
}

function triggerBadgeClass(trigger, recording) {
  if (recording && isSoundRecording(recording)) return 'chip-sound';
  const t = String(trigger || '').toLowerCase();
  if (t.startsWith('alert') || t.startsWith('human')) return 'chip-warn';
  if (t.startsWith('motion')) return 'chip-info';
  if (t === 'continuous' || t === 'none' || t === 'off') return 'chip-dim';
  return 'chip-info';
}

function recordingDetectionSummary(recording) {
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    const label = (meta.class_label || meta.label || recording.trigger_label || 'sound').toLowerCase();
    const confidence = Number(meta.confidence || 0);
    return [{ label, confidence }];
  }

  // Build best-confidence map from all detections regardless of current config —
  // this is historical data so we show everything that was actually recorded.
  const best = new Map();
  for (const d of (recording.detections || [])) {
    const label = String(d.label || '').trim().toLowerCase();
    if (!label) continue;
    const conf = Number(d.confidence || 0);
    if (!best.has(label) || conf > best.get(label)) best.set(label, conf);
  }
  // Use recording.labels as the authoritative label list when available.
  const authLabels = Array.isArray(recording.labels) && recording.labels.length
    ? recording.labels.map((l) => String(l || '').trim().toLowerCase()).filter((l) => l && !GENERIC_TRIGGER_LABELS.has(l))
    : Array.from(best.keys()).filter((l) => !GENERIC_TRIGGER_LABELS.has(l));
  return authLabels
    .map((label) => ({ label, confidence: best.get(label) ?? 0 }))
    .sort((a, b) => b.confidence - a.confidence);
}

function renderRecordingDetails(recording) {
  const detections = recordingDetectionSummary(recording);
  const isSound = isSoundRecording(recording);
  const detectionBadges = detections.length
    ? detections.map((d) => detectionPill(d.label, d.confidence, isSound)).join(' ')
    : 'none';
  const detectionLabel = isSound ? 'Sound' : 'Detections';
  const zones = recordingZoneNames(recording);
  const zoneRow = zones.length ? `<div><span>Zone</span><strong>${zones.map(escapeHtml).join(', ')}</strong></div>` : '';
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong>#${recording.id}</strong></div>
    <div><span>Event</span><strong>${recording.event_id || 'none'}</strong></div>
    <div><span>Camera</span><strong>${escapeHtml(cameraLabel(recording))}</strong></div>
    ${zoneRow}
    <div><span>Trigger</span><strong>${escapeHtml(recordingDisplayTrigger(recording))}</strong></div>
    <div><span>Started</span><strong>${escapeHtml(formatDateTime(recording.started_at))}</strong></div>
    <div><span>Duration</span><strong>${Number(recording.duration_seconds || 0).toFixed(1)}s</strong></div>
    <div class="wide"><span>${detectionLabel}</span><strong>${detectionBadges}</strong></div>
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

function recordingTrack() {
  return Array.isArray(activeRecording?.track) && activeRecording.track.length ? activeRecording.track : null;
}

function overlayShouldAnimate() {
  return overlayEnabled;
}

function startOverlayRaf() {
  const video = els.clipPlayer;
  if (!video) return;
  // Uses requestVideoFrameCallback for frame-accurate sync with the video
  // decoder. The callback provides `mediaTime` — the exact PTS of the frame
  // being displayed. We project one frame ahead (mediaTime + frameDuration)
  // so the overlay paints boxes where the object will be when the next frame
  // hits the screen, compensating for the 1-frame VFC-to-composite delay.
  // Falls back to rAF + currentTime when VFC is unavailable (older browsers).
  const useVfc = typeof video.requestVideoFrameCallback === 'function';

  let prevVfcTime = 0;
  function onVfcFrame(now, metadata) {
    if (!els.clipPlayer || els.clipPlayer.paused || !overlayShouldAnimate()) {
      overlayRafId = null;
      overlayVfcHandle = null;
      return;
    }
    // Estimate frame duration from the delta between consecutive VFC frames
    // (clamped to a reasonable 10-200ms range to filter outliers).
    const mediaTime = metadata && typeof metadata.mediaTime === 'number' ? metadata.mediaTime : null;
    if (mediaTime !== null && prevVfcTime > 0) {
      const dt = mediaTime - prevVfcTime;
      if (dt >= 0.01 && dt <= 0.2) _frameDuration = dt;
    }
    if (mediaTime !== null) prevVfcTime = mediaTime;
    drawClipOverlay(mediaTime);
    overlayVfcHandle = video.requestVideoFrameCallback(onVfcFrame);
  }

  function onRafFrame() {
    if (!els.clipPlayer || els.clipPlayer.paused || !overlayShouldAnimate()) {
      overlayRafId = null;
      return;
    }
    drawClipOverlay();
    overlayRafId = requestAnimationFrame(onRafFrame);
  }

  if (useVfc) {
    if (overlayVfcHandle !== null) return; // already running
    overlayVfcHandle = video.requestVideoFrameCallback(onVfcFrame);
  } else {
    if (overlayRafId !== null) return; // already running
    overlayRafId = requestAnimationFrame(onRafFrame);
  }
}

function stopOverlayRaf() {
  if (overlayVfcHandle !== null && els.clipPlayer && typeof els.clipPlayer.cancelVideoFrameCallback === 'function') {
    els.clipPlayer.cancelVideoFrameCallback(overlayVfcHandle);
    overlayVfcHandle = null;
  }
  if (overlayRafId !== null) {
    cancelAnimationFrame(overlayRafId);
    overlayRafId = null;
  }
}

function drawClipOverlay(vfcMediaTime) {
  if (!els.clipOverlay || !els.clipPlayer) return;
  if (!overlayEnabled) {
    clearClipOverlay();
    return;
  }
  resizeOverlayCanvas(els.clipOverlay, els.clipPlayer);
  const context = els.clipOverlay.getContext('2d');
  if (!context) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, els.clipOverlay.width, els.clipOverlay.height);

  // Use the VFC-provided mediaTime (exact frame PTS) and project one frame
  // ahead. This compensates for the inherent 1-frame delay between VFC
  // firing (after the frame was sent to compositor) and the overlay paint
  // being displayed (on the next frame). Falls back to currentTime (with
  // forward projection) for the rAF path or when VFC isn't available.
  let playerTime;
  if (typeof vfcMediaTime === 'number' && Number.isFinite(vfcMediaTime)) {
    playerTime = vfcMediaTime + _frameDuration;
  } else {
    playerTime = Number(els.clipPlayer.currentTime || 0) + _frameDuration;
  }

  // The saved detection track replays the boxes the live monitor computed
  // while the clip recorded, so playback never runs inference. Clips without
  // a track fall back to the event's static boxes.
  const track = recordingTrack();
  if (track) {
    const tracked = filterByConfiguredLabels(sampleTrackAtTime(track, playerTime));
    if (tracked.length) drawDetectionBoxesOnCanvas(els.clipOverlay, tracked, els.clipPlayer);
    return;
  }

  // Static event boxes describe the trigger moment, which sits after the
  // clip's pre-roll; drawing them from time 0 puts a frozen box over footage
  // recorded before the detection existed.
  if (!shouldRenderOverlayForTime(activeRecording, playerTime)) return;
  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  );
  if (!eventDetections.length) return;
  drawDetectionBoxesOnCanvas(els.clipOverlay, eventDetections, els.clipPlayer);
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
  activeRecording = null;
  els.clipPlayerStatus.textContent = '';
  els.recordingDetails.innerHTML = '';
  if (els.videoModalSubtitle) {
    els.videoModalSubtitle.textContent = 'Watch a recording and review its detection details.';
  }
}

async function playRecording(id) {
  const recording = await api(`/api/recordings/${id}`);
  activeRecording = recording;
  renderRecordingDetails(recording);
  if (els.videoModalSubtitle) {
    const started = formatDateTime(recording.started_at);
    const camera = cameraLabel(recording);
    els.videoModalSubtitle.textContent = started
      ? `Recording from ${camera} captured ${started}.`
      : `Recording from ${camera}.`;
  }
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
      const id = button.dataset.deleteRecording;
      if (!confirm(`Delete recording #${id}? This cannot be undone.`)) return;
      try {
        await api(`/api/recordings/${id}`, { method: 'DELETE' });
        window.showToast?.(`Deleted recording #${id}.`);
        await loadRecordings();
      } catch (error) {
        window.showToast?.(`Failed to delete recording: ${error.message}`, true);
      }
    });
  });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  // Date/time display preferences are now set globally by nav.js from the
  // same /api/auth/me response; nothing page-local to do here.
  if (authInfo.user.role === 'admin') {
    els.deleteAllRecordingsBtn.hidden = false;
    els.deleteAllRecordingsBtn.addEventListener('click', async () => {
      if (!confirm('Delete ALL recordings and media files? Settings, users, and rules will not be changed.')) return;
      try {
        const result = await api('/api/recordings', { method: 'DELETE' });
        await loadRecordings();
        const deletedCount = Number(result?.deleted || 0);
        window.showToast?.(`Deleted ${deletedCount} recording${deletedCount === 1 ? '' : 's'}. Settings were not changed.`);
      } catch (error) {
        window.showToast?.(`Failed to delete recordings: ${error.message}`, true);
      }
    });
  }
}

async function loadLiveSettings() {
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

async function loadRecordings(filters = {}) {
  const resolved = typeof filters === 'string' || filters instanceof String
    ? { label: String(filters), cameraId: '' }
    : { ...currentFilterValues(), ...filters };
  const params = new URLSearchParams();
  if (resolved.sourceType) params.set('source_type', resolved.sourceType);
  if (resolved.label) params.set('label', resolved.label);
  if (resolved.cameraId) params.set('camera_id', resolved.cameraId);
  const startedAfter = formatIsoDateForFilter(resolved.dateFrom);
  if (startedAfter) params.set('started_after', startedAfter);
  const startedBefore = formatIsoDateForFilter(resolved.dateTo, { endOfDay: true });
  if (startedBefore) params.set('started_before', startedBefore);
  if (resolved.sort) params.set('sort', resolved.sort);
  const queryString = params.toString();
  const recordings = await api(`/api/recordings${queryString ? `?${queryString}` : ''}`);
  const activeFilters = describeFilters(resolved);
  if (activeFilters.length) {
    updateFilterStat('Filtered', `Showing clips matching ${activeFilters.join(' and ')}.`);
  } else {
    updateFilterStat('All', 'Showing every clip');
  }
  renderRecordings(recordings);
  return recordings;
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

// timeupdate is intentionally omitted — the requestVideoFrameCallback/rAF loop
// already draws the overlay on every frame during playback, making it redundant.
['loadedmetadata', 'loadeddata', 'pause', 'seeked'].forEach((eventName) => {
  els.clipPlayer.addEventListener(eventName, () => {
    drawClipOverlay();
  });
});

els.clipPlayer.addEventListener('play', () => {
  if (overlayShouldAnimate()) startOverlayRaf();
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
  overlayEnabled = savedValue === '1';
  els.clipOverlayToggle.checked = overlayEnabled;
  els.clipOverlayToggle.addEventListener('change', () => {
    overlayEnabled = Boolean(els.clipOverlayToggle.checked);
    localStorage.setItem(OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
    if (els.clipPlayer && !els.clipPlayer.paused && overlayShouldAnimate()) {
      startOverlayRaf();
    } else if (!overlayEnabled) {
      stopOverlayRaf();
    }
    drawClipOverlay();
  });
}

els.cameraFilter?.addEventListener('change', () => loadRecordings());
els.filterForm?.addEventListener('submit', (event) => {
  event.preventDefault();
  loadRecordings();
});
els.recordingClearBtn.addEventListener('click', () => {
  clearLabelSelection();
  if (els.cameraFilter) els.cameraFilter.value = '';
  if (els.recordingDateFrom) els.recordingDateFrom.value = '';
  if (els.recordingDateTo) els.recordingDateTo.value = '';
  if (els.recordingSort) els.recordingSort.value = 'newest';
  loadRecordings();
});

// ── Multi-select dropdown ────────────────────────────────────────────────

function clearLabelSelection() {
  selectedLabels.clear();
  updateLabelSelectDisplay();
  const checkboxes = document.querySelectorAll('#labelOptionsObjects input[type="checkbox"], #labelOptionsSounds input[type="checkbox"]');
  checkboxes.forEach((cb) => { cb.checked = false; });
}

function updateLabelSelectDisplay() {
  if (!els.labelSelectText) return;
  if (selectedLabels.size === 0) {
    els.labelSelectText.textContent = 'All Labels';
  } else if (selectedLabels.size === 1) {
    const label = [...selectedLabels][0];
    const opt = allLabelOptions.find((o) => o.value === label);
    els.labelSelectText.textContent = opt ? opt.label : label;
  } else {
    els.labelSelectText.textContent = `${selectedLabels.size} labels selected`;
  }
}

function renderLabelCheckboxes(searchText) {
  if (!els.labelOptionsObjects || !els.labelOptionsSounds) return;
  const query = (searchText || '').trim().toLowerCase();

  let objectHtml = '';
  let soundHtml = '';
  let hasObjectMatch = false;
  let hasSoundMatch = false;

  for (const opt of allLabelOptions) {
    if (query && !opt.value.includes(query) && !opt.label.toLowerCase().includes(query)) continue;
    const checked = selectedLabels.has(opt.value) ? ' checked' : '';
    const escaped = escapeHtml(opt.label);
    const item = `<label class="multi-select-option"><input type="checkbox" value="${escapeHtml(opt.value)}"${checked} /><span>${escaped}</span></label>`;
    if (opt.group === 'objects') {
      objectHtml += item;
      hasObjectMatch = true;
    } else {
      soundHtml += item;
      hasSoundMatch = true;
    }
  }

  els.labelOptionsObjects.innerHTML = objectHtml || '<span class="multi-select-empty">No matching objects</span>';
  els.labelOptionsSounds.innerHTML = soundHtml || '<span class="multi-select-empty">No matching sounds</span>';
  els.labelGroupObjects.hidden = !hasObjectMatch;
  els.labelGroupSounds.hidden = !hasSoundMatch;

  // Bind checkbox events
  els.labelOptionsObjects.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.checked) selectedLabels.add(cb.value);
      else selectedLabels.delete(cb.value);
      updateLabelSelectDisplay();
    });
  });
  els.labelOptionsSounds.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.checked) selectedLabels.add(cb.value);
      else selectedLabels.delete(cb.value);
      updateLabelSelectDisplay();
    });
  });
}

function openLabelDropdown() {
  if (!els.labelSelectDropdown) return;
  els.labelSelectDropdown.hidden = false;
  els.labelSelectTrigger.classList.add('open');
  if (els.labelSelectSearch) {
    els.labelSelectSearch.value = '';
    els.labelSelectSearch.focus();
  }
  renderLabelCheckboxes('');
}

function closeLabelDropdown() {
  if (!els.labelSelectDropdown) return;
  els.labelSelectDropdown.hidden = true;
  els.labelSelectTrigger.classList.remove('open');
}

function toggleLabelDropdown() {
  if (!els.labelSelectDropdown) return;
  if (els.labelSelectDropdown.hidden) {
    openLabelDropdown();
  } else {
    closeLabelDropdown();
  }
}

async function loadLabelOptions() {
  try {
    const data = await api('/api/labels');
    allLabelOptions = [];
    if (Array.isArray(data.objects)) {
      for (const label of data.objects) {
        allLabelOptions.push({ value: label.toLowerCase(), label: titleCase(label), group: 'objects' });
      }
    }
    if (Array.isArray(data.sounds)) {
      for (const sound of data.sounds) {
        allLabelOptions.push({ value: sound.id.toLowerCase(), label: sound.label, group: 'sounds' });
      }
    }
    // Sort alphabetically within groups
    allLabelOptions.sort((a, b) => {
      if (a.group !== b.group) return a.group === 'objects' ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
    renderLabelCheckboxes('');
  } catch (_error) {
    // Labels will remain empty; dropdown shows empty state.
  }
}

// Bind multi-select events
if (els.labelSelectTrigger) {
  els.labelSelectTrigger.addEventListener('click', (e) => {
    e.preventDefault();
    toggleLabelDropdown();
  });
}

if (els.labelSelectSearch) {
  els.labelSelectSearch.addEventListener('input', () => {
    renderLabelCheckboxes(els.labelSelectSearch.value);
  });
  els.labelSelectSearch.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeLabelDropdown();
  });
}

// Close dropdown on outside click
document.addEventListener('click', (e) => {
  if (!els.labelSelectDropdown || els.labelSelectDropdown.hidden) return;
  const trigger = els.labelSelectTrigger;
  const dropdown = els.labelSelectDropdown;
  if (!trigger.contains(e.target) && !dropdown.contains(e.target)) {
    closeLabelDropdown();
  }
});

// Close on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && els.labelSelectDropdown && !els.labelSelectDropdown.hidden) {
    closeLabelDropdown();
    if (els.labelSelectTrigger) els.labelSelectTrigger.focus();
  }
});

els.videoModalClose.addEventListener('click', () => closeVideoModal());

els.videoModal.addEventListener('click', (event) => {
  if (event.target === els.videoModal || event.target.classList.contains('video-modal-backdrop')) closeVideoModal();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.videoModal.hidden) closeVideoModal();
});

// Re-render the recordings list (and any open modal's "Started" line)
// when the user's date_format / time_format changes in another tab. Uses
// loadRecordings() so the active filter inputs (camera, label, dates,
// sort) are preserved — only the displayed formatting changes.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  if (typeof loadRecordings !== 'function' || !els || !els.listStatus) return;
  loadRecordings().catch((error) => { els.listStatus.textContent = error.message; });
};

loadAuth().then(async () => {
  await Promise.all([loadCameras(), loadLiveSettings(), loadLabelOptions()]);
  await loadRecordings();
  const selected = new URLSearchParams(window.location.search).get('recording_id');
  if (selected) playRecording(selected).catch((error) => { els.listStatus.textContent = error.message; });
}).catch((error) => {
  if (els.listStatus) els.listStatus.textContent = error.message;
  window.showToast?.(error.message, true);
});
