const els = {
  cameraSelect: document.getElementById('timelineCameraSelect'),
  timelineDate: document.getElementById('timelineDate'),
  filterSelect: document.getElementById('timelineFilterSelect'),
  timelineLoadBtn: document.getElementById('timelineLoadBtn'),
  timelineStatus: document.getElementById('timelineStatus'),
  timelineSummary: document.getElementById('timelineSummary'),
  timelineLegend: document.getElementById('timelineLegend'),
  timelineHours: document.getElementById('timelineHours'),
  timelineGrid: document.getElementById('timelineGrid'),
  timelineRows: document.getElementById('timelineRows'),
  timelineRecordings: document.getElementById('timelineRecordings'),
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
};

const state = {
  auth: { user: null, csrfToken: null },
  payload: null,
  activeRecordingId: null,
};

const TIMELINE_SECONDS = 24 * 60 * 60;
const TIMELINE_ROW_HEIGHT = 42;
const SEGMENT_COLORS = [
  '#47d6ff',
  '#49e6a3',
  '#fbbf24',
  '#fb7185',
  '#38bdf8',
  '#f97316',
  '#a78bfa',
  '#22c55e',
  '#f43f5e',
  '#14b8a6',
];

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.auth.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = state.auth.csrfToken;
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

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function titleCase(value) {
  return String(value || '')
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(' ');
}

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString() : 'Unknown';
}

function formatClock(seconds) {
  const date = new Date(Date.UTC(2000, 0, 1, 0, 0, 0));
  date.setUTCSeconds(Math.max(0, Math.floor(seconds)));
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' });
}

function formatDuration(seconds) {
  const totalSeconds = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainder = totalSeconds % 60;
  if (hours) return `${hours}h ${minutes}m ${remainder}s`;
  if (minutes) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function recordingTriggerType(recording) {
  return String(recording.trigger_type || 'motion').trim().toLowerCase() || 'motion';
}

function recordingTriggerLabel(recording) {
  return String(recording.trigger_label || '').trim().toLowerCase() || null;
}

function recordingDetectionLabels(recording) {
  return Array.from(new Set((recording.detections || [])
    .map((detection) => String(detection.label || '').trim().toLowerCase())
    .filter(Boolean)));
}

function recordingTypeLabel(recording) {
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  if (triggerType === 'motion' || triggerType === 'alert' || triggerType === 'human') {
    // In many deployments recording policy stores trigger_type as "alert" even for motion-style captures.
    return 'motion';
  }
  if (triggerType === 'continuous' || triggerType === 'none' || triggerType === 'off') {
    return triggerType;
  }
  return triggerLabel || triggerType;
}

function recordingColorKey(recording) {
  return recordingTypeLabel(recording).toLowerCase();
}

function recordingTriggerSummary(recording) {
  const triggerType = recordingTriggerType(recording);
  const triggerLabel = recordingTriggerLabel(recording);
  if (!triggerLabel || triggerLabel === triggerType || (triggerType === 'human' && triggerLabel === 'person')) {
    return recordingTypeLabel(recording);
  }
  if (triggerType === 'human' || triggerType === 'alert') {
    return `motion · detected ${triggerLabel}`;
  }
  return `${recordingTypeLabel(recording)} · detected ${triggerLabel}`;
}

function recordingFilterTokens(recording) {
  const tokens = new Set([recordingTypeLabel(recording).toLowerCase()]);
  const triggerType = recordingTriggerType(recording);
  if (triggerType) tokens.add(triggerType);
  const triggerLabel = recordingTriggerLabel(recording);
  if (triggerLabel) tokens.add(triggerLabel);
  recordingDetectionLabels(recording).forEach((label) => tokens.add(label));
  return tokens;
}

function matchesRecordingFilter(recording, filterValue) {
  const normalized = String(filterValue || '').trim().toLowerCase();
  if (!normalized) return true;
  if (normalized === 'motion') {
    const triggerType = recordingTriggerType(recording);
    return !['continuous', 'off', 'none'].includes(triggerType);
  }
  return recordingFilterTokens(recording).has(normalized);
}

function cameraLabel(recording) {
  const metadata = recording?.event?.metadata || {};
  return metadata.camera_name || recording.camera_id || recording.source || 'unknown';
}

function colorForKey(key) {
  const normalized = String(key || 'motion').trim().toLowerCase() || 'motion';
  let hash = 0;
  for (let index = 0; index < normalized.length; index += 1) {
    hash = ((hash << 5) - hash) + normalized.charCodeAt(index);
    hash |= 0;
  }
  return SEGMENT_COLORS[Math.abs(hash) % SEGMENT_COLORS.length];
}

function timelineParams(overrides = {}) {
  const cameraId = overrides.cameraId || els.cameraSelect.value || '';
  const day = overrides.day || els.timelineDate.value || new Date().toISOString().slice(0, 10);
  return { cameraId, day };
}

function replaceUrl(recordingId = state.activeRecordingId) {
  const params = new URLSearchParams();
  const { cameraId, day } = timelineParams();
  const filter = els.filterSelect.value || '';
  if (cameraId) params.set('camera_id', cameraId);
  if (day) params.set('day', day);
  if (filter) params.set('filter', filter);
  if (recordingId) params.set('recording_id', String(recordingId));
  const query = params.toString();
  window.history.replaceState({}, '', `${window.location.pathname}${query ? `?${query}` : ''}`);
}

function populateControls(payload) {
  const selectedCameraId = payload.camera?.id || '';
  const selectedDay = payload.day || new Date().toISOString().slice(0, 10);
  els.cameraSelect.innerHTML = payload.cameras.map((camera) => (
    `<option value="${escapeHtml(camera.id)}" ${camera.id === selectedCameraId ? 'selected' : ''}>${escapeHtml(camera.name)}</option>`
  )).join('');
  els.timelineDate.value = selectedDay;
}

function populateFilterOptions(recordings) {
  const currentFilter = els.filterSelect.value || new URLSearchParams(window.location.search).get('filter') || '';
  const options = [{ value: '', label: 'All recordings' }];
  const seen = new Set(['']);
  const addOption = (value, label) => {
    const normalized = String(value || '').trim().toLowerCase();
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    options.push({ value: normalized, label });
  };

  recordings.forEach((recording) => {
    const displayLabel = recordingTypeLabel(recording).toLowerCase();
    addOption(displayLabel, titleCase(displayLabel));
    recordingDetectionLabels(recording).forEach((label) => addOption(label, titleCase(label)));
  });
  if (recordings.length) {
    addOption('motion', 'Motion');
  }

  const ordered = [options[0], ...options.slice(1).sort((left, right) => {
    if (left.value === 'motion') return -1;
    if (right.value === 'motion') return 1;
    return left.label.localeCompare(right.label);
  })];
  els.filterSelect.innerHTML = ordered.map((option) => (
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
  )).join('');

  const availableValues = new Set(ordered.map((option) => option.value));
  els.filterSelect.value = availableValues.has(currentFilter) ? currentFilter : '';
}

function filteredRecordings() {
  const recordings = state.payload?.recordings || [];
  return recordings.filter((recording) => matchesRecordingFilter(recording, els.filterSelect.value));
}

function renderSummary(payload, totalRecordingCount) {
  const recordings = payload.recordings || [];
  const totalSeconds = recordings.reduce((sum, recording) => sum + Number(recording.timeline_duration_seconds || recording.duration_seconds || 0), 0);
  const uniqueTriggers = new Set(recordings.map((recording) => recordingTypeLabel(recording).toLowerCase()));
  const clipLabel = totalRecordingCount > recordings.length ? `${recordings.length} of ${totalRecordingCount}` : `${recordings.length}`;
  els.timelineSummary.innerHTML = `
    <div><span>Camera</span><strong>${escapeHtml(payload.camera?.name || payload.camera?.id || 'Unknown')}</strong></div>
    <div><span>Day</span><strong>${escapeHtml(payload.day || '')}</strong></div>
    <div><span>Clips</span><strong>${escapeHtml(clipLabel)}</strong></div>
    <div><span>Coverage</span><strong>${escapeHtml(formatDuration(totalSeconds))}</strong></div>
    <div class="wide"><span>Triggers</span><strong>${recordings.length ? escapeHtml(Array.from(uniqueTriggers).join(', ')) : 'none'}</strong></div>
  `;
}

function renderLegend(recordings) {
  const unique = [];
  const seen = new Set();
  recordings.forEach((recording) => {
    const key = recordingColorKey(recording);
    if (seen.has(key)) return;
    seen.add(key);
    unique.push({ key, label: recordingTypeLabel(recording), color: colorForKey(key) });
  });
  if (!unique.length) {
    els.timelineLegend.innerHTML = '<p class="muted">No recordings match this filter for the selected day.</p>';
    return;
  }
  els.timelineLegend.innerHTML = unique.map((item) => `
    <span class="timeline-legend-chip">
      <span class="timeline-legend-swatch" style="background:${item.color}"></span>
      <span>${escapeHtml(item.label)}</span>
    </span>
  `).join('');
}

function buildTimelineLayout(recordings) {
  const rowEnds = [];
  return recordings.map((recording) => {
    const start = Number(recording.timeline_start_seconds || 0);
    const end = Number(recording.timeline_end_seconds || start + 1);
    let rowIndex = rowEnds.findIndex((rowEnd) => rowEnd <= start);
    if (rowIndex === -1) {
      rowIndex = rowEnds.length;
      rowEnds.push(end);
    } else {
      rowEnds[rowIndex] = end;
    }
    return { ...recording, rowIndex };
  });
}

function renderTimeline(payload) {
  const recordings = buildTimelineLayout(payload.recordings || []);
  const rowCount = Math.max(1, recordings.reduce((max, recording) => Math.max(max, recording.rowIndex + 1), 0));
  const majorHours = [0, 3, 6, 9, 12, 15, 18, 21, 24];
  els.timelineHours.innerHTML = majorHours.map((hour) => (
    `<span class="timeline-hour major" style="left:${(hour / 24) * 100}%">${String(hour).padStart(2, '0')}:00</span>`
  )).join('');
  els.timelineGrid.innerHTML = Array.from({ length: 25 }, (_, hour) => `
    <span class="timeline-grid-line" style="left:${(hour / 24) * 100}%"></span>
  `).join('');
  els.timelineRows.style.height = `${Math.max(96, rowCount * TIMELINE_ROW_HEIGHT)}px`;

  if (!recordings.length) {
    els.timelineRows.innerHTML = '<div class="empty timeline-empty">No recordings match the selected filter for this camera and day.</div>';
    return;
  }

  els.timelineRows.innerHTML = recordings.map((recording) => {
    const start = Number(recording.timeline_start_seconds || 0);
    const duration = Math.max(1, Number(recording.timeline_duration_seconds || 1));
    const left = (start / TIMELINE_SECONDS) * 100;
    const width = Math.max((duration / TIMELINE_SECONDS) * 100, 0.06);
    const color = colorForKey(recordingColorKey(recording));
    const activeClass = Number(recording.id) === Number(state.activeRecordingId) ? ' active' : '';
    const compactClass = width < 0.7 ? ' compact' : '';
    const tinyClass = width < 0.2 ? ' tiny' : '';
    return `
      <button
        class="timeline-segment${activeClass}${compactClass}${tinyClass}"
        type="button"
        data-recording-id="${recording.id}"
        title="${escapeHtml(`${recordingTriggerSummary(recording)} · ${formatClock(start)} · ${formatDuration(recording.duration_seconds)}`)}"
        style="left:${left}%;width:${width}%;top:${recording.rowIndex * TIMELINE_ROW_HEIGHT + 8}px;--segment-color:${color};"
      >
        <span class="timeline-segment-label">${escapeHtml(recordingTypeLabel(recording))}</span>
        <span class="timeline-segment-time">${escapeHtml(formatClock(start))}</span>
      </button>
    `;
  }).join('');
}

function renderRecordingList(recordings) {
  if (!recordings.length) {
    els.timelineRecordings.innerHTML = '';
    return;
  }
  els.timelineRecordings.innerHTML = recordings.map((recording) => {
    const activeClass = Number(recording.id) === Number(state.activeRecordingId) ? ' active' : '';
    const color = colorForKey(recordingColorKey(recording));
    return `
      <button class="timeline-recording-item${activeClass}" type="button" data-recording-id="${recording.id}">
        <span class="timeline-recording-color" style="background:${color}"></span>
        <span class="timeline-recording-main">
          <strong>${escapeHtml(recordingTypeLabel(recording))}</strong>
          <span>${escapeHtml(formatClock(recording.timeline_start_seconds || 0))} to ${escapeHtml(formatClock(recording.timeline_end_seconds || 0))}</span>
        </span>
        <span class="timeline-recording-meta">${escapeHtml(formatDuration(recording.duration_seconds))} · ${escapeHtml(recordingTriggerSummary(recording))}</span>
        <span class="timeline-recording-meta muted">Recording #${recording.id}</span>
      </button>
    `;
  }).join('');
}

function renderRecordingDetails(recording) {
  els.recordingDetails.innerHTML = `
    <div><span>Recording</span><strong>#${recording.id}</strong></div>
    <div><span>Camera</span><strong>${escapeHtml(cameraLabel(recording))}</strong></div>
    <div><span>Trigger</span><strong>${escapeHtml(recordingTriggerSummary(recording))}</strong></div>
    <div><span>Started</span><strong>${escapeHtml(formatDateTime(recording.started_at))}</strong></div>
    <div><span>Duration</span><strong>${escapeHtml(formatDuration(recording.duration_seconds))}</strong></div>
    <div class="wide"><span>Detections</span><strong>${escapeHtml(recordingDetectionLabels(recording).join(', ') || 'none')}</strong></div>
  `;
}

function highlightActiveRecording() {
  document.querySelectorAll('[data-recording-id]').forEach((node) => {
    const isActive = Number(node.dataset.recordingId) === Number(state.activeRecordingId);
    node.classList.toggle('active', isActive);
  });
}

async function playRecording(recordingId, updateHistory = true) {
  const recording = await api(`/api/recordings/${recordingId}`);
  state.activeRecordingId = Number(recording.id);
  renderRecordingDetails(recording);
  highlightActiveRecording();
  if (updateHistory) replaceUrl(state.activeRecordingId);

  if (recording.media_ready === false) {
    els.clipPlayer.pause();
    els.clipPlayer.removeAttribute('src');
    els.clipPlayer.load();
    els.clipPlayerStatus.textContent = `Recording #${recording.id} is still being prepared.`;
    return;
  }

  els.clipPlayer.pause();
  els.clipPlayer.src = `/api/recordings/${recording.id}/stream?t=${Date.now()}`;
  els.clipPlayerStatus.textContent = `Loading recording #${recording.id}...`;
  try {
    els.clipPlayer.load();
    await els.clipPlayer.play();
    els.clipPlayerStatus.textContent = `Playing recording #${recording.id}.`;
  } catch (error) {
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${recording.id} loaded. Press play to start.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${recording.id}: ${error?.message || 'media playback failed'}.`;
  }
}

function clearPlayback(updateHistory = true) {
  state.activeRecordingId = null;
  els.clipPlayer.pause();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.clipPlayerStatus.textContent = 'Select a timeline segment to play a recording.';
  els.recordingDetails.innerHTML = '';
  highlightActiveRecording();
  if (updateHistory) replaceUrl(null);
}

async function renderFilteredTimeline({ preserveSelection = true } = {}) {
  const allRecordings = state.payload?.recordings || [];
  const recordings = filteredRecordings();
  const viewPayload = { ...(state.payload || {}), recordings };
  renderSummary(viewPayload, allRecordings.length);
  renderLegend(recordings);
  renderTimeline(viewPayload);
  renderRecordingList(recordings);

  if (!recordings.length) {
    els.timelineStatus.textContent = allRecordings.length
      ? `No recordings match the selected filter for ${state.payload.camera.name} on ${state.payload.day}.`
      : `No recordings found for ${state.payload.camera.name} on ${state.payload.day}.`;
    clearPlayback(false);
    replaceUrl(null);
    return;
  }

  const filterLabel = els.filterSelect.value ? ` matching ${titleCase(els.filterSelect.value)}` : '';
  els.timelineStatus.textContent = `${recordings.length} clip${recordings.length === 1 ? '' : 's'}${filterLabel} for ${state.payload.camera.name} on ${state.payload.day}.`;

  const querySelection = Number(new URLSearchParams(window.location.search).get('recording_id')) || null;
  const requestedSelection = preserveSelection ? (state.activeRecordingId || querySelection) : null;
  const selectedRecording = recordings.find((recording) => Number(recording.id) === Number(requestedSelection));
  if (selectedRecording) {
    await playRecording(selectedRecording.id, false);
  } else {
    clearPlayback(false);
  }
  replaceUrl(state.activeRecordingId);
}

async function loadTimeline({ preserveSelection = true } = {}) {
  const { cameraId, day } = timelineParams();
  els.timelineStatus.textContent = 'Loading timeline…';
  const payload = await api(`/api/recordings/timeline?camera_id=${encodeURIComponent(cameraId)}&day=${encodeURIComponent(day)}`);
  state.payload = payload;
  populateControls(payload);
  populateFilterOptions(payload.recordings || []);
  await renderFilteredTimeline({ preserveSelection });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  state.auth = { user: authInfo.user, csrfToken: authInfo.csrf_token };
}

els.timelineLoadBtn.addEventListener('click', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.cameraSelect.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.timelineDate.addEventListener('change', () => {
  loadTimeline({ preserveSelection: false }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.filterSelect.addEventListener('change', () => {
  renderFilteredTimeline({ preserveSelection: true }).catch((error) => {
    els.timelineStatus.textContent = error.message;
  });
});

els.timelineRows.addEventListener('click', (event) => {
  const button = event.target.closest('[data-recording-id]');
  if (!button) return;
  playRecording(button.dataset.recordingId).catch((error) => {
    els.clipPlayerStatus.textContent = error.message;
  });
});

els.timelineRecordings.addEventListener('click', (event) => {
  const button = event.target.closest('[data-recording-id]');
  if (!button) return;
  playRecording(button.dataset.recordingId).catch((error) => {
    els.clipPlayerStatus.textContent = error.message;
  });
});

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

loadAuth().then(async () => {
  const params = new URLSearchParams(window.location.search);
  const queryDay = params.get('day');
  const queryCameraId = params.get('camera_id');
  const queryFilter = params.get('filter');
  if (queryDay) els.timelineDate.value = queryDay;
  if (queryCameraId) els.cameraSelect.innerHTML = `<option value="${escapeHtml(queryCameraId)}" selected>${escapeHtml(queryCameraId)}</option>`;
  if (queryFilter) els.filterSelect.innerHTML = `<option value="${escapeHtml(queryFilter)}" selected>${escapeHtml(titleCase(queryFilter))}</option>`;
  await loadTimeline({ preserveSelection: true });
}).catch((error) => {
  els.timelineStatus.textContent = error.message;
  els.clipPlayerStatus.textContent = error.message;
});
