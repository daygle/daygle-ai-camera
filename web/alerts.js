// alerts.js - Dedicated alerts page.
// Loaded by alerts.html only. Shows notifications fired by zone and sound rules,
// with filtering by type (object / motion / sound) and per-alert dismissal.

// ─── DOM handles ────────────────────────────────────────────────────────────
const els = {
  alertFeed: document.getElementById('alertFeed'),
  listStatus: document.getElementById('listStatus'),
  dismissAllBtn: document.getElementById('dismissAllAlertsBtn'),
  filterPills: document.querySelectorAll('[data-filter]'),
  statObjectAlerts: document.getElementById('statObjectAlerts'),
  statMotionAlerts: document.getElementById('statMotionAlerts'),
  statSoundAlerts: document.getElementById('statSoundAlerts'),
  rangeBtns: document.querySelectorAll('[data-range]'),
  // Inline clip player elements.
  clipPlayer: document.getElementById('clipPlayer'),
  clipPlayerStatus: document.getElementById('clipPlayerStatus'),
  recordingDetails: document.getElementById('recordingDetails'),
  clipOverlay: document.getElementById('clipOverlay'),
  clipOverlayToggle: document.getElementById('clipOverlayToggle'),
  clipPlayerCard: document.getElementById('clipPlayerCard'),
  clipPlayerTitle: document.getElementById('clipPlayerTitle'),
  clipPlayerClose: document.getElementById('clipPlayerClose'),
  clipTimeline: document.getElementById('clipTimeline'),
  clipTimelineBar: document.getElementById('clipTimelineBar'),
  clipTimelineLegend: document.getElementById('clipTimelineLegend'),
  videoModalDownload: document.getElementById('videoModalDownload'),
  videoModalSubtitle: document.getElementById('videoModalSubtitle'),
};

// SOUND_CLASS_IDS, isSoundLabel, isMotionOnlyAlertGroup, isMotionOnlyAlertItem,
// GENERIC_TRIGGER_LABELS, detectionPill, motionPill, formatDate, timeAgo,
// escapeHtml, titleCase, safeHtml, cameraLabel are provided by web/utils.js.
// cameraLabel(recording) reads the camera NAME from recording.event.metadata
// (the recording detail API only exposes camera_id at the top level).

let alertGroups = [];
let activeFilter = 'all';
let activeRange = 'today';

// ─── Inline clip player state ─────────────────────────────────────────────
let activeRecording = null;
let overlayEnabled = true;
let overlayRafId = null;
let overlayVfcHandle = null;
let overlayResizeObserver = null;
let _frameDuration = 1 / 30;
let configuredLabels = null;

// daygleSinceParamForRange() is provided by web/utils.js: it converts the
// active UI range preset ('today' / '7d' / '30d' / 'all') into a `since`
// ISO bound that is the START OF THE LOCAL DAY expressed in UTC. The backend
// compares stored UTC timestamps lexically (created_at >= ?), so a bound
// based on the UTC date string would silently drop alerts fired between
// local midnight and UTC midnight for timezones ahead of UTC (the "Today
// shows 1 alert but 7d shows all" bug).
function getSinceParam() {
  return daygleSinceParamForRange(activeRange);
}

// api() is provided by web/utils.js - shared CSRF, 401 redirect, JSON.

// ─── Alert grouping (consolidates alerts belonging to the same clip) ───────
// A single continuous clip accrues several detection events (each new object or
// sound extends the same recording via extend_active_rtsp_recording), and every
// alert fired against them carries the same recording_id. Grouping object/motion
// alerts by recording collapses those into one row so a clip is not listed as
// several duplicate "Recording #N" alerts.
//
// Sound alerts are deliberately NOT folded into the recording group: a clip can
// carry both a sound alert and an object alert (a sound extending an
// object-triggered recording), and the renderer/filter treat any group holding a
// sound label as a Sound Alert. Merging them would hide the object alert from the
// Object Alerts tab. Sound alerts therefore keep per-event grouping so each type
// stays its own filterable, separately dismissible row. Alerts with no recording
// fall back to per-event grouping, and alerts with neither stay individual.
function alertIsSound(alert) {
  return SOUND_CLASS_IDS.has(String(alert.label || '').trim().toLowerCase());
}

function groupAlertsByEvent(alerts) {
  const order = [];
  const groups = new Map();
  for (const alert of alerts) {
    const key = !alertIsSound(alert) && alert.recording_id !== null && alert.recording_id !== undefined
      ? `recording-${alert.recording_id}`
      : alert.event_id !== null && alert.event_id !== undefined
        ? `event-${alert.event_id}`
        : `alert-${alert.id}`;
    if (!groups.has(key)) {
      order.push(key);
      groups.set(key, {
        key,
        eventId: alert.event_id ?? null,
        camera: cameraLabel(alert.camera_name, alert.camera_id),
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
    const confidence = Number(alert.confidence);
    group.detections.push({
      label: label || String(alert.label || ''),
      confidence: Number.isFinite(confidence) ? confidence : null,
    });
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
    return { ...group, labels: Array.from(group.labels), zones: Array.from(group.zones), camera: group.camera || 'unknown' };
  });
}

// ─── Rendering ──────────────────────────────────────────────────────────────

function recordingLink(recordingId, label) {
  if (!recordingId) return '';
  const playIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  return `<button class="secondary activity-item-action" data-play-recording="${encodeURIComponent(recordingId)}" type="button" aria-label="Play ${escapeHtml(label)}">${playIcon}<span class="activity-action-label">${escapeHtml(label)}</span></button>`;
}

function renderAlertItem(group) {
  const isSound = group.labels.some((l) => SOUND_CLASS_IDS.has(l)) || group.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()));
  const isMotionOnly = isMotionOnlyAlertItem({ ...group, detections: group.detections, labels: group.labels });
  const typeClass = isSound ? 'activity-item-sound' : isMotionOnly ? 'activity-item-motion' : 'activity-item-alert';
  const typeLabel = isSound ? 'Sound Alert' : isMotionOnly ? 'Motion Alert' : 'Object Alert';
  const title = group.recordingId ? `Recording #${group.recordingId}` : 'Alert';
  const camera = group.camera ? escapeHtml(group.camera) : 'unknown';
  const zone = !isSound && group.zones?.length ? group.zones.map(escapeHtml).join(', ') : '-';
  const actions = [];
  if (group.recordingId) actions.push(recordingLink(group.recordingId, 'Footage'));

  if (window.daygleAuth?.user?.role === 'admin') {
    const dismissIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    actions.push(`<button class="secondary delete-btn activity-item-action" data-dismiss-alert="${escapeHtml(String(group.key))}" type="button" aria-label="Dismiss ${escapeHtml(title)}">${dismissIcon}<span class="activity-action-label">Dismiss</span></button>`);
  }

  return `
    <tr class="activity-table-row ${typeClass}" data-activity-id="${escapeHtml(String(group.key))}" data-activity-type="alert">
      <td class="activity-cell-type"><span class="activity-item-type">${typeLabel}</span><span class="activity-cell-ref">${escapeHtml(title)}</span></td>
      <td class="activity-cell-camera">${camera}</td>
      <td class="activity-cell-detections"><div class="activity-item-badges">${isMotionOnly ? motionPill() : detectionBadges(group.detections, { isSound })}</div></td>
      <td class="activity-cell-zone">${zone}</td>
      <td class="activity-cell-when">
        <div class="activity-item-when">
          <div class="activity-item-when-relative">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span>${escapeHtml(timeAgo(group.latestAt))}</span>
          </div>
          <span class="activity-item-when-absolute">${escapeHtml(formatDate(group.latestAt))}</span>
        </div>
      </td>
      <td class="activity-cell-actions"><div class="cell-actions">${actions.join('')}</div></td>
    </tr>
  `;
}

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

function renderEmptyState() {
  const messages = {
    all: { title: 'No alerts yet', subtitle: 'Alerts from your zone and sound rules will appear here when they fire.' },
    'object-alerts': { title: 'No object alerts yet', subtitle: 'Object alerts from your zone rules will appear here when they fire.' },
    'motion-alerts': { title: 'No motion alerts yet', subtitle: 'Motion alerts from your zone rules will appear here when they fire.' },
    'sound-alerts': { title: 'No sound alerts yet', subtitle: 'Sound alerts from your sound rules will appear here when they fire.' },
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

function applyFilter(groups) {
  if (activeFilter === 'object-alerts') return groups.filter((g) => {
    if (isMotionOnlyAlertGroup(g)) return false;
    return !g.labels.some((l) => SOUND_CLASS_IDS.has(l)) && !g.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()));
  });
  if (activeFilter === 'motion-alerts') return groups.filter((g) => isMotionOnlyAlertGroup(g));
  if (activeFilter === 'sound-alerts') return groups.filter((g) =>
    g.labels.some((l) => SOUND_CLASS_IDS.has(l)) || g.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()))
  );
  return groups;
}

function updateStats() {
  const objectAlerts = alertGroups.filter((g) => {
    if (isMotionOnlyAlertGroup(g)) return false;
    return !g.labels.some((l) => SOUND_CLASS_IDS.has(l)) && !g.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()));
  }).length;
  const motionAlerts = alertGroups.filter((g) => isMotionOnlyAlertGroup(g)).length;
  const soundAlerts = alertGroups.filter((g) =>
    g.labels.some((l) => SOUND_CLASS_IDS.has(l)) || g.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()))
  ).length;
  if (els.statObjectAlerts) els.statObjectAlerts.textContent = String(objectAlerts);
  if (els.statMotionAlerts) els.statMotionAlerts.textContent = String(motionAlerts);
  if (els.statSoundAlerts) els.statSoundAlerts.textContent = String(soundAlerts);
}

// ── Click-to-sort activity headers ────────────────────────────────────────
// Sorting is client-side, matching the recordings table. `null` preserves the
// existing newest-first API order; clicking a column cycles asc → desc → back
// to that default. The active sort survives filter and time-range changes.
let activitySortState = null;

function alertGroupSortType(group) {
  const isSound = group.labels.some((label) => SOUND_CLASS_IDS.has(label))
    || group.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()));
  return isSound ? 2 : isMotionOnlyAlertGroup(group) ? 1 : 0;
}

function activitySortValue(group, key) {
  switch (key) {
    case 'type': return alertGroupSortType(group);
    case 'camera': return String(group.camera || '').toLowerCase();
    case 'detections': return new Set((group.detections || []).map((d) => String(d.label || '').trim().toLowerCase()).filter(Boolean)).size;
    case 'zone': return String(group.zones?.[0] || '').toLowerCase();
    case 'when': {
      const timestamp = Date.parse(group.latestAt);
      return Number.isFinite(timestamp) ? timestamp : null;
    }
    default: return 0;
  }
}

function compareActivityItems(left, right) {
  if (!activitySortState) return 0;
  const leftValue = activitySortValue(left, activitySortState.key);
  const rightValue = activitySortValue(right, activitySortState.key);
  let result;
  if (leftValue === null && rightValue !== null) return 1;
  if (leftValue !== null && rightValue === null) return -1;
  if (typeof leftValue === 'number' && typeof rightValue === 'number') {
    result = leftValue - rightValue;
  } else {
    result = String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: 'base' });
  }
  return activitySortState.dir === 'asc' ? result : -result;
}

function renderActivitySortHeader(label, key) {
  const active = activitySortState && activitySortState.key === key;
  const ariaSort = active ? (activitySortState.dir === 'asc' ? 'ascending' : 'descending') : 'none';
  const glyph = active ? (activitySortState.dir === 'asc' ? '▲' : '▼') : '⇅';
  const cls = active ? 'table-sort-btn is-active' : 'table-sort-btn';
  return `<th scope="col" aria-sort="${ariaSort}"><button type="button" class="${cls}" data-sort-key="${key}" aria-label="Sort by ${label}">${label}<span class="table-sort-glyph" aria-hidden="true">${glyph}</span></button></th>`;
}

function bindActivitySortHeaders() {
  document.querySelectorAll('#alertFeed [data-sort-key]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sortKey;
      if (activitySortState && activitySortState.key === key) {
        activitySortState = activitySortState.dir === 'asc'
          ? { key, dir: 'desc' }
          : null;
      } else {
        activitySortState = { key, dir: key === 'when' ? 'desc' : 'asc' };
      }
      renderFeed();
    });
  });
}

function renderFeed() {
  const filtered = applyFilter(alertGroups);
  if (!filtered.length) {
    els.alertFeed.innerHTML = renderEmptyState();
    updateListStatus(0);
    return;
  }
  const ordered = activitySortState ? filtered.slice().sort(compareActivityItems) : filtered;
  els.alertFeed.innerHTML =
    '<div class="cameras-table-wrap"><table class="rule-table activity-table">' +
      '<thead><tr>' +
        renderActivitySortHeader('Type', 'type') +
        renderActivitySortHeader('Camera', 'camera') +
        renderActivitySortHeader('Detections', 'detections') +
        renderActivitySortHeader('Zone', 'zone') +
        renderActivitySortHeader('When', 'when') +
        '<th class="cell-center" scope="col">Actions</th>' +
      '</tr></thead>' +
      '<tbody>' + ordered.map(renderAlertItem).join('') + '</tbody>' +
    '</table></div>';
  bindActions();
  bindActivitySortHeaders();
  updateListStatus(filtered.length);
}

function updateListStatus(count) {
  if (!els.listStatus) return;
  const labels = { all: 'alerts', 'object-alerts': 'Object', 'motion-alerts': 'Motion', 'sound-alerts': 'Sound' };
  const label = labels[activeFilter] || 'alerts';
  els.listStatus.textContent = count > 0 ? `${count} ${label}` : '';
}

function bindActions() {
  els.alertFeed.querySelectorAll('[data-dismiss-alert]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.dismissAlert;
      btn.disabled = true;
      try {
        await api(`/api/alerts/${encodeURIComponent(key)}/dismiss`, { method: 'POST' });
        alertGroups = alertGroups.filter((g) => String(g.key) !== String(key));
        renderFeed();
        updateStats();
      } catch (error) {
        if (window.daygleAuth?.redirecting) return;
        window.showToast?.(error.message, true);
        btn.disabled = false;
      }
    });
  });
  // Inline play buttons: open the clip player above the feed card.
  els.alertFeed.querySelectorAll('[data-play-recording]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      const id = button.dataset.playRecording;
      if (id) playRecording(id);
    });
  });
}

// ─── Filter pills ───────────────────────────────────────────────────────────
els.filterPills.forEach((pill) => {
  pill.addEventListener('click', () => {
    activeFilter = pill.dataset.filter;
    els.filterPills.forEach((other) => {
      const active = other === pill;
      other.classList.toggle('active', active);
      other.setAttribute('aria-selected', String(active));
    });
    renderFeed();
  });
});

// ─── Time-range selector (segmented buttons) ───────────────────────────────
els.rangeBtns.forEach((btn) => {
  btn.addEventListener('click', () => {
    activeRange = btn.dataset.range;
    els.rangeBtns.forEach((other) => {
      const active = other === btn;
      other.classList.toggle('active', active);
      other.setAttribute('aria-selected', String(active));
    });
    loadAlerts().then(() => { renderFeed(); updateDismissBtn(); }).catch(() => {});
  });
});

// ─── Data loading ──────────────────────────────────────────────────────────
async function loadAlerts() {
  try {
    const since = getSinceParam();
    const url = since ? `/api/alerts?since=${since}` : '/api/alerts';
    const alerts = await api(url);
    alertGroups = groupAlertsByEvent(alerts);
    updateStats();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    alertGroups = [];
    window.showToast?.(error.message, true);
  }
}

async function refreshAll() {
  await loadAlerts();
  renderFeed();
  updateDismissBtn();
}

// ─── Dismiss all ────────────────────────────────────────────────────────────
els.dismissAllBtn?.addEventListener('click', async () => {
  els.dismissAllBtn.disabled = true;
  try {
    await api('/api/alerts/dismiss-all', { method: 'POST' });
    alertGroups = [];
    renderFeed();
    updateStats();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  } finally {
    els.dismissAllBtn.disabled = false;
  }
});

// ─── Inline clip player ────────────────────────────────────────────────────

function filterByConfiguredLabels(detections) {
  if (!configuredLabels) return detections;
  return detections.filter((d) => {
    const label = String(d.label || '').trim().toLowerCase();
    return configuredLabels.has(label) || (configuredLabels.has('motion') && label === 'motion');
  });
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
  const useVfc = typeof video.requestVideoFrameCallback === 'function';
  let prevVfcTime = 0;
  function onVfcFrame(now, metadata) {
    if (!els.clipPlayer || els.clipPlayer.paused || !overlayShouldAnimate()) {
      overlayRafId = null;
      overlayVfcHandle = null;
      return;
    }
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
    if (overlayVfcHandle !== null) return;
    overlayVfcHandle = video.requestVideoFrameCallback(onVfcFrame);
  } else {
    if (overlayRafId !== null) return;
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
  let playerTime;
  if (typeof vfcMediaTime === 'number' && Number.isFinite(vfcMediaTime)) {
    playerTime = vfcMediaTime + _frameDuration;
  } else {
    playerTime = Number(els.clipPlayer.currentTime || 0) + _frameDuration;
  }
  const track = recordingTrack();
  if (track) {
    const tracked = filterByConfiguredLabels(sampleTrackAtTime(track, playerTime));
    if (tracked.length) drawDetectionBoxesOnCanvas(els.clipOverlay, tracked, els.clipPlayer);
    return;
  }
  if (activeRecording && !activeRecording.detections?.length) return;
  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  if (!allEventDetections.length) return;
  const eventDetections = filterByConfiguredLabels(allEventDetections);
  if (!eventDetections.length) return;
  drawDetectionBoxesOnCanvas(els.clipOverlay, eventDetections, els.clipPlayer);
}

function renderRecordingDetails(recording) {
  // Use the recording-level summary rather than only the linked event's
  // detections. Extended clips can accumulate additional labels in
  // recording_labels / label_confidences after the original event is saved.
  const detections = recordingDetectionSummary(recording);
  const isSound = isSoundRecording(recording);
  const isMotionOnly = isMotionOnlyRecording(recording);
  let detectionBadges;
  let detectionLabel;
  if (isMotionOnly) {
    detectionLabel = 'Motion';
    detectionBadges = motionPill(motionConfidenceFor(recording));
  } else if (isSound) {
    detectionLabel = 'Sound';
    const soundDetections = recordingDetectionSummary(recording);
    detectionBadges = soundDetections.length
      ? soundDetections.map((d) => detectionPill(d.label, d.confidence, true)).join(' ')
      : 'none';
  } else {
    detectionLabel = 'Detections';
    detectionBadges = detections.length
      ? detections.map((d) => detectionPill(d.label, d.confidence)).join(' ')
      : 'none';
  }
  const zones = recordingZoneNames(recording);
  const zoneRow = zones.length
    ? safeHtml`<div><span>Zone</span><strong>${zones.join(', ')}</strong></div>`
    : '';
  const detailRows = [
    safeHtml`<div><span>Recording</span><strong>#${recording.id}</strong></div>`,
    safeHtml`<div><span>Event</span><strong>${recording.event_id || 'none'}</strong></div>`,
    safeHtml`<div><span>Camera</span><strong>${cameraLabel(recording)}</strong></div>`,
    zoneRow,
    safeHtml`<div><span>Trigger</span><strong>${recordingDisplayTrigger(recording)}</strong></div>`,
    safeHtml`<div><span>Started</span><strong>${formatDateTime(recording.started_at)}</strong></div>`,
    safeHtml`<div><span>Duration</span><strong>${Number(recording.duration_seconds || 0).toFixed(1)}s</strong></div>`,
  ].filter(Boolean);
  els.recordingDetails.innerHTML = detailRows.join('');
  els.recordingDetails.insertAdjacentHTML(
    'beforeend',
    `<div class="wide"><span>${escapeHtml(detectionLabel)}</span><strong class="recording-detail-detections">${detectionBadges}</strong></div>`,
  );
}


function recordingDisplayTrigger(recording) {
  if (isSoundRecording(recording)) {
    const meta = recording.event?.metadata || {};
    const classLabel = meta.class_label || meta.label || recording.trigger_label || 'sound';
    return titleCase(classLabel);
  }
  const triggerLabel = recordingTriggerLabel(recording);
  return titleCase(triggerLabel || 'motion');
}

// ── Clip segment timeline ───────────────────────────────────────────────────

function clipAuthoritativeDuration() {
  const videoDuration = Number(els.clipPlayer?.duration);
  if (Number.isFinite(videoDuration) && videoDuration > 0) return videoDuration;
  const metaDuration = Number(activeRecording?.duration_seconds);
  return Number.isFinite(metaDuration) && metaDuration > 0 ? metaDuration : 0;
}

function clipEventBounds(track) {
  if (!Array.isArray(track) || !track.length) return null;
  let first = null;
  let last = null;
  for (const sample of track) {
    if (!sample || !Array.isArray(sample.detections) || !sample.detections.length) continue;
    const t = Number(sample.t);
    if (!Number.isFinite(t) || t < 0) continue;
    if (first === null) first = t;
    last = t;
  }
  return first === null ? null : { first, last };
}

function fmtClipSeconds(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
}

function resetClipTimeline() {
  if (!els.clipTimeline) return;
  els.clipTimeline.hidden = true;
  if (els.clipTimelineBar) els.clipTimelineBar.innerHTML = '';
  if (els.clipTimelineLegend) els.clipTimelineLegend.innerHTML = '';
}

function renderClipTimeline() {
  if (!els.clipTimeline || !els.clipTimelineBar) return;
  const duration = clipAuthoritativeDuration();
  const track = Array.isArray(activeRecording?.track) ? activeRecording.track : null;
  const bounds = clipEventBounds(track);
  if (!duration || !bounds) {
    resetClipTimeline();
    return;
  }
  const first = Math.max(0, Math.min(bounds.first, duration));
  const last = Math.min(Math.max(bounds.last, first), duration);
  const pct = (value) => `${Math.max(0, Math.min(100, (value / duration) * 100))}%`;
  const segments = [
    { cls: 'pre', label: 'Pre-roll', start: 0, end: first },
    { cls: 'event', label: 'Event', start: first, end: last },
    { cls: 'tail', label: 'Tail', start: last, end: duration },
  ];
  els.clipTimelineBar.innerHTML = '';
  for (const seg of segments) {
    const span = seg.end - seg.start;
    if (span <= 0.05) continue;
    const div = document.createElement('div');
    div.className = `clip-seg clip-seg-${seg.cls}`;
    div.style.left = pct(seg.start);
    div.style.width = pct(span);
    div.title = `${seg.label}: ${fmtClipSeconds(span)}`;
    els.clipTimelineBar.appendChild(div);
  }
  const marker = document.createElement('div');
  marker.className = 'clip-trigger-marker';
  marker.style.left = pct(first);
  marker.title = `Event trigger at ${fmtClipSeconds(first)}`;
  els.clipTimelineBar.appendChild(marker);
  const playhead = document.createElement('div');
  playhead.className = 'clip-playhead';
  playhead.id = 'clipPlayhead';
  els.clipTimelineBar.appendChild(playhead);
  const legendItems = [
    { cls: 'pre', label: 'Pre-roll', secs: first },
    { cls: 'event', label: 'Event', secs: last - first },
    { cls: 'tail', label: 'Tail', secs: duration - last },
  ];
  els.clipTimelineLegend.innerHTML = '';
  for (const item of legendItems) {
    const wrap = document.createElement('span');
    wrap.className = 'clip-legend-item';
    const swatch = document.createElement('i');
    swatch.className = `clip-legend-swatch clip-seg-${item.cls}`;
    wrap.appendChild(swatch);
    wrap.appendChild(document.createTextNode(`${item.label} ${fmtClipSeconds(Math.max(0, item.secs))}`));
    els.clipTimelineLegend.appendChild(wrap);
  }
  els.clipTimeline.hidden = false;
  els.clipTimelineBar.setAttribute('aria-valuemax', duration.toFixed(1));
  updateClipTimelinePlayhead();
}

function updateClipTimelinePlayhead() {
  if (!els.clipTimeline || els.clipTimeline.hidden) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const playhead = document.getElementById('clipPlayhead');
  if (!playhead) return;
  const current = Number(els.clipPlayer?.currentTime) || 0;
  playhead.style.left = `${Math.max(0, Math.min(100, (current / duration) * 100))}%`;
  els.clipTimelineBar?.setAttribute('aria-valuenow', current.toFixed(1));
}

function seekClipFromClientX(clientX) {
  if (!els.clipTimelineBar || !els.clipPlayer) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const rect = els.clipTimelineBar.getBoundingClientRect();
  if (rect.width <= 0) return;
  const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  try {
    els.clipPlayer.currentTime = fraction * duration;
  } catch (_error) {
    /* not seekable yet */
  }
  updateClipTimelinePlayhead();
}

function showInlinePlayer() {
  if (els.clipPlayerCard) {
    els.clipPlayerCard.hidden = false;
    els.clipPlayerCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function hideInlinePlayer() {
  if (els.clipPlayerCard) els.clipPlayerCard.hidden = true;
  if (els.clipPlayer) {
    els.clipPlayer.pause();
    stopOverlayRaf();
    els.clipPlayer.removeAttribute('src');
    els.clipPlayer.load();
  }
  clearClipOverlay();
  resetClipTimeline();
  activeRecording = null;
  if (els.clipPlayerStatus) els.clipPlayerStatus.textContent = '';
  if (els.recordingDetails) els.recordingDetails.innerHTML = '';
  if (els.clipPlayerTitle) els.clipPlayerTitle.textContent = 'Recording';
  if (els.videoModalSubtitle) els.videoModalSubtitle.textContent = 'Watch a recording and review its detection details.';
}

async function playRecording(id) {
  const recording = await api(`/api/recordings/${id}`);
  activeRecording = recording;
  renderRecordingDetails(recording);
  if (els.clipPlayerTitle) els.clipPlayerTitle.textContent = `Recording #${recording.id}`;
  if (els.videoModalSubtitle) {
    const started = formatDateTime(recording.started_at);
    const camera = cameraLabel(recording);
    els.videoModalSubtitle.textContent = started
      ? `Recording from ${camera} captured ${started}.`
      : `Recording from ${camera}.`;
  }
  resetClipTimeline();
  showInlinePlayer();
  if (recording.media_ready === false) {
    clearClipOverlay();
    els.clipPlayerStatus.textContent = `Recording #${id} is still being prepared.`;
    return;
  }
  if (els.videoModalDownload) {
    els.videoModalDownload.href = `/api/recordings/${id}/download`;
    els.videoModalDownload.hidden = false;
  }
  els.clipPlayer.pause();
  els.clipPlayer.removeAttribute('src');
  els.clipPlayer.load();
  els.clipPlayer.src = `/api/recordings/${id}/stream?t=${Date.now()}`;
  drawClipOverlay();
  els.clipPlayerStatus.textContent = `Loading recording #${id}...`;
  try {
    els.clipPlayer.load();
    await els.clipPlayer.play();
    // Successful playback is self-evident from the native video controls;
    // reserve this line for preparation, loading, and error feedback.
    els.clipPlayerStatus.textContent = '';
  } catch (error) {
    if (['AbortError', 'NotAllowedError'].includes(error?.name)) {
      els.clipPlayerStatus.textContent = `Recording #${id} loaded.`;
      return;
    }
    els.clipPlayerStatus.textContent = `Unable to play recording #${id}: ${error?.message || 'media playback failed'}.`;
  }
}

// ─── Auth ────────────────────────────────────────────────────────────────────
async function loadAuth() {
  await window.daygleAuthReady;
}

function updateDismissBtn() {
  const isAdmin = window.daygleAuth?.user?.role === 'admin';
  if (els.dismissAllBtn) els.dismissAllBtn.hidden = !isAdmin || alertGroups.length === 0;
}

// ─── Refresh orchestration ──────────────────────────────────────────────────
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  renderFeed();
};

// ─── Clip player event listeners ───────────────────────────────────────────
els.clipPlayerClose?.addEventListener('click', () => hideInlinePlayer());

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !els.clipPlayerCard?.hidden) hideInlinePlayer();
});

if (els.clipPlayer) {
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

  ['loadedmetadata', 'loadeddata', 'pause', 'seeked'].forEach((eventName) => {
    els.clipPlayer.addEventListener(eventName, () => {
      drawClipOverlay();
    });
  });

  els.clipPlayer.addEventListener('loadedmetadata', renderClipTimeline);
  ['timeupdate', 'seeked', 'play'].forEach((eventName) => {
    els.clipPlayer.addEventListener(eventName, updateClipTimelinePlayhead);
  });

  if (els.clipTimelineBar) {
    els.clipTimelineBar.addEventListener('click', (event) => seekClipFromClientX(event.clientX));
  }

  els.clipPlayer.addEventListener('play', () => {
    if (overlayShouldAnimate()) startOverlayRaf();
    drawClipOverlay();
  });

  els.clipPlayer.addEventListener('pause', () => {
    stopOverlayRaf();
    drawClipOverlay();
  });

  window.addEventListener('resize', drawClipOverlay);

  if ('ResizeObserver' in window) {
    overlayResizeObserver = new ResizeObserver(drawClipOverlay);
    overlayResizeObserver.observe(els.clipPlayer);
  }

  if (els.clipOverlayToggle) {
    const savedValue = localStorage.getItem(RECORDINGS_OVERLAY_TOGGLE_KEY);
    overlayEnabled = savedValue !== '0';
    els.clipOverlayToggle.checked = overlayEnabled;
    els.clipOverlayToggle.addEventListener('change', () => {
      overlayEnabled = Boolean(els.clipOverlayToggle.checked);
      localStorage.setItem(RECORDINGS_OVERLAY_TOGGLE_KEY, overlayEnabled ? '1' : '0');
      if (els.clipPlayer && !els.clipPlayer.paused && overlayShouldAnimate()) {
        startOverlayRaf();
      } else if (!overlayEnabled) {
        stopOverlayRaf();
      }
      drawClipOverlay();
    });
  }
}

loadAuth()
  .then(async () => {
    await refreshAll();
  })
  .catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  });

setInterval(() => {
  loadAlerts().then(() => { renderFeed(); updateDismissBtn(); }).catch(() => {});
}, 15000);
