// ─── DOM handles ────────────────────────────────────────────────────────────
const els = {
  totalEvents: document.getElementById('totalEvents'),
  soundEvents: document.getElementById('soundEvents'),
  motionEvents: document.getElementById('motionEvents'),
  activityFeed: document.getElementById('activityFeed'),
  listStatus: document.getElementById('listStatus'),
  dismissAllEventsBtn: document.getElementById('dismissAllEventsBtn'),
  cpuValue: document.getElementById('cpuValue'),
  cpuSub: document.getElementById('cpuSub'),
  loadValue: document.getElementById('loadValue'),
  loadSub: document.getElementById('loadSub'),
  ramValue: document.getElementById('ramValue'),
  ramSub: document.getElementById('ramSub'),
  // Scope to [data-filter] so the category group and the range group stay
  // independent: both share the .activity-filter-pill class, so selecting by
  // class swept the range buttons into the category handler, which reset the
  // category filter to undefined on every range click (Object + 7d wouldn't
  // hold). Category buttons carry data-filter; range buttons carry data-range.
  filterPills: document.querySelectorAll('[data-filter]'),
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

let activeRange = 'today';

// ─── Inline clip player state ─────────────────────────────────────────────
let activeRecording = null;
let overlayEnabled = true;
let overlayRafId = null;
let overlayVfcHandle = null;
let overlayResizeObserver = null;
let _frameDuration = 1 / 30; // default 30fps, updated on each VFC frame

// daygleSinceParamForRange() is provided by web/utils.js: it converts the
// active UI range preset ('today' / '7d' / '30d' / 'all') into a `since`
// ISO bound that is the START OF THE LOCAL DAY expressed in UTC. The backend
// compares stored UTC timestamps lexically (created_at >= ?), so a bound
// based on the UTC date string would silently drop events/alerts fired
// between local midnight and UTC midnight for timezones ahead of UTC (the
// same "Today shows 1 of 6" bug that hit the alerts page).
function getSinceParam() {
  return daygleSinceParamForRange(activeRange);
}

// cameraLabel() is provided by web/utils.js (loaded before this script).

function eventSourceLabel(event) {
  const metadata = event?.metadata || {};
  const fromMetadata = cameraLabel(metadata.camera_name, metadata.camera_id);
  if (fromMetadata) return fromMetadata;
  const fromRecording = cameraLabel('', event?.recordings?.[0]?.camera_id);
  if (fromRecording) return fromRecording;
  return String(event?.source || 'unknown');
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
    .map(([label, conf]) => String(label).toLowerCase() === 'motion'
      ? motionPill(conf)
      : detectionPill(label, conf, isSound))
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
  // Collapse the multiple detection events that belong to one continuous clip
  // into a single row. Each new detection during the post-event window extends
  // the same recording (via extend_active_rtsp_recording) rather than starting a
  // new clip, so those events all carry the same recordingId. Titling each one
  // "Recording #N" made one clip look like several duplicate rows.
  //
  // Key the dedup on recordingId AND detection type (sound / motion / object).
  // A single clip can legitimately carry events of different types - e.g. an
  // object trigger whose recording a later sound detection extends - and the
  // Type filter reads item.isSound / item.isMotionOnly, so merging across types
  // would drop one type from its tab. Same-type events merge (their detections
  // combine onto the newest row); different types stay separate rows so each is
  // still filterable. This generalises the previous sound-only dedup. Events
  // with no recordingId are distinct system events ("Event #id") and never merge.
  const itemType = (item) => (item.isSound ? 'sound' : item.isMotionOnly ? 'motion' : 'object');
  const seenRecording = new Map();
  return eventItems.filter((item) => {
    const recId = item.recordingId;
    if (!recId) return true;
    const groupKey = `${recId}|${itemType(item)}`;
    const prev = seenRecording.get(groupKey);
    if (prev) {
      for (const d of item.detections) prev.detections.push(d);
      return false;
    }
    seenRecording.set(groupKey, item);
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

function recordingLink(recordingId, label) {
  if (!recordingId) return '';
  const playIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  return `<button class="secondary activity-item-action activity-item-action-play" data-play-recording="${encodeURIComponent(recordingId)}" type="button" aria-label="Play recording #${escapeHtml(String(recordingId))}">${playIcon}<span class="activity-action-label">${escapeHtml(label)}</span></button>`;
}

function renderActivityItem(item) {
  const isSound = Boolean(item.isSound);
  const isMotionOnly = Boolean(item.isMotionOnly);
  const typeClass = isSound ? 'activity-item-sound' : isMotionOnly ? 'activity-item-motion' : 'activity-item-event';
  const typeLabel = isSound ? 'Sound Detection' : isMotionOnly ? 'Motion Detection' : 'Object Detection';
  const title = item.recordingId ? `Recording #${item.recordingId}` : `Event #${item.id}`;
  const camera = item.camera ? escapeHtml(item.camera) : 'unknown';
  const zone = !isSound && item.zoneNames?.length
    ? item.zoneNames.map(escapeHtml).join(', ')
    : '-';
  const actions = [];
  if (item.recordingId) actions.push(recordingLink(item.recordingId, 'Play'));
  if (window.daygleAuth?.user?.role === 'admin') {
    const dismissIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    actions.push(`<button class="secondary delete-btn activity-item-action" data-dismiss-event="${escapeHtml(String(item.id))}" type="button" aria-label="Dismiss ${escapeHtml(title)}">${dismissIcon}<span class="activity-action-label">Dismiss</span></button>`);
  }
  return `
    <tr class="activity-table-row ${typeClass}" data-activity-id="${escapeHtml(String(item.id))}">
      <td class="activity-cell-type"><span class="activity-item-type">${typeLabel}</span><span class="activity-cell-ref">${escapeHtml(title)}</span></td>
      <td class="activity-cell-camera">${camera}</td>
      <td class="activity-cell-detections"><div class="activity-item-badges">${isMotionOnly ? motionPill() : detectionBadges(item.detections, { isSound })}</div></td>
      <td class="activity-cell-zone">${zone}</td>
      <td class="activity-cell-when">
        <div class="activity-item-when">
          <div class="activity-item-when-relative">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span>${escapeHtml(timeAgo(item.createdAt))}</span>
          </div>
          <span class="activity-item-when-absolute">${escapeHtml(formatDate(item.createdAt))}</span>
        </div>
      </td>
      <td class="activity-cell-actions"><div class="cell-actions">${actions.join('')}</div></td>
    </tr>
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

// ── Click-to-sort activity headers ────────────────────────────────────────
// Sorting is client-side, matching the recordings table. `null` preserves the
// existing newest-first API order; clicking a column cycles asc → desc → back
// to that default. The active sort survives filter and time-range changes.
let activitySortState = null;

function activitySortValue(item, key) {
  switch (key) {
    case 'type': return item.isSound ? 2 : item.isMotionOnly ? 1 : 0;
    case 'camera': return String(item.camera || '').toLowerCase();
    case 'detections': return new Set((item.detections || []).map((d) => String(d.label || '').trim().toLowerCase()).filter(Boolean)).size;
    case 'zone': return String(item.zoneNames?.[0] || '').toLowerCase();
    case 'when': {
      const timestamp = Date.parse(item.createdAt);
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
  document.querySelectorAll('#activityFeed [data-sort-key]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sortKey;
      if (activitySortState && activitySortState.key === key) {
        activitySortState = activitySortState.dir === 'asc'
          ? { key, dir: 'desc' }
          : null;
      } else {
        activitySortState = { key, dir: key === 'when' ? 'desc' : 'asc' };
      }
      renderActivityFeed();
    });
  });
}

function renderActivityFeed() {
  const items = applyFilter(buildEventItems());
  if (!items.length) {
    els.activityFeed.innerHTML = renderEmptyState();
    updateListStatus(0);
    updateDismissButtons();
    return;
  }
  const ordered = activitySortState ? items.slice().sort(compareActivityItems) : items;
  els.activityFeed.innerHTML =
    '<div class="cameras-table-wrap"><table class="rule-table activity-table">' +
      '<thead><tr>' +
        renderActivitySortHeader('Type', 'type') +
        renderActivitySortHeader('Camera', 'camera') +
        renderActivitySortHeader('Detections', 'detections') +
        renderActivitySortHeader('Zone', 'zone') +
        renderActivitySortHeader('When', 'when') +
        '<th class="cell-center" scope="col">Actions</th>' +
      '</tr></thead>' +
      '<tbody>' + ordered.map(renderActivityItem).join('') + '</tbody>' +
    '</table></div>';
  updateListStatus(items.length);
  bindActivityActions();
  bindActivitySortHeaders();
  updateDismissButtons();
}

function updateListStatus(count) {
  if (!els.listStatus) return;
  const labels = { all: 'items', 'object-detections': 'Object', 'motion-detections': 'Motion', 'sound-detections': 'Sound' };
  const label = labels[activeFilter] || 'items';
  if (count === 0) {
    els.listStatus.textContent = '';
  } else {
    els.listStatus.textContent = `${count} ${label}`;
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
  // Inline play buttons: open the clip player above the feed card.
  els.activityFeed.querySelectorAll('[data-play-recording]').forEach((button) => {
    button.addEventListener('click', (event) => {
      event.preventDefault();
      const id = button.dataset.playRecording;
      if (id) playRecording(id);
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
    const since = getSinceParam();
    const url = since ? `/api/stats?since=${since}` : '/api/stats';
    const stats = await api(url);
    els.totalEvents.textContent = stats.matched_object_events ?? stats.total_events ?? 0;
    if (els.soundEvents) els.soundEvents.textContent = stats.sound_detection_events ?? 0;
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  }
}

// ─── System resource cards (CPU / Load / RAM) ───────────────────────────────
// RAM is conventionally reported in binary units (1 GB = 1024^3 bytes),
// matching how operating systems and users talk about installed memory, so
// the "GB" label here is binary by design - name and unit label now agree.
function formatGB(bytes) {
  const gb = Number(bytes) / (1024 ** 3);
  if (!Number.isFinite(gb)) return '-';
  return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
}

function renderSystemResources(res) {
  const cpu = res?.cpu_percent;
  if (els.cpuValue) els.cpuValue.textContent = Number.isFinite(cpu) ? `${cpu}%` : ' - ';
  if (els.cpuSub) {
    const cores = res?.cpu_count;
    els.cpuSub.textContent = Number.isFinite(cores) ? `${cores} core${cores === 1 ? '' : 's'}` : 'Processor usage';
  }

  const load = res?.load_average;
  if (els.loadValue) els.loadValue.textContent = Array.isArray(load) && load.length ? load[0].toFixed(2) : ' - ';
  if (els.loadSub) {
    els.loadSub.textContent = Array.isArray(load) && load.length === 3
      ? `${load[0].toFixed(2)} / ${load[1].toFixed(2)} / ${load[2].toFixed(2)} · 1/5/15 min`
      : '1 / 5 / 15 min average';
  }

  const mem = res?.memory;
  const pct = mem?.percent;
  if (els.ramValue) els.ramValue.textContent = Number.isFinite(pct) ? `${pct}%` : ' - ';
  if (els.ramSub) {
    els.ramSub.textContent = mem && Number.isFinite(mem.used) && Number.isFinite(mem.total)
      ? `${formatGB(mem.used)} / ${formatGB(mem.total)} used`
      : 'Memory usage';
  }
}

async function loadSystemResources() {
  try {
    const res = await api('/api/system/resources');
    renderSystemResources(res);
  } catch (error) {
    // Non-admins get 403 on this admin-gated endpoint; leave the placeholder
    // dashes in place and stay quiet rather than flashing an error toast.
    if (window.daygleAuth?.redirecting) return;
  }
}

async function loadEvents() {
  try {
    const since = getSinceParam();
    const url = since ? `/api/events?with_recording=true&since=${since}` : '/api/events?with_recording=true';
events = await api(url);
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

// ─── Inline clip player ────────────────────────────────────────────────────

function filterByConfiguredLabels(detections) {
  if (!configuredLabels) return detections;
  return detections.filter((d) => {
    const label = String(d.label || '').trim().toLowerCase();
    return configuredLabels.has(label) || (configuredLabels.has('motion') && label === 'motion');
  });
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
  // Static event boxes describe the trigger moment, which sits after the
  // clip's pre-roll; never paint them over footage from before the event.
  if (!shouldRenderOverlayForTime(activeRecording, playerTime)) return;
  const allEventDetections = Array.isArray(activeRecording?.detections) ? activeRecording.detections : [];
  if (!allEventDetections.length) return;
  const hasSpecificEvent = allEventDetections.some((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()));
  const eventDetections = filterByConfiguredLabels(
    hasSpecificEvent
      ? allEventDetections.filter((d) => !GENERIC_TRIGGER_LABELS.has(String(d.label || '').toLowerCase()))
      : allEventDetections
  );
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
  // A motion clip can contain only one localized sample when movement starts
  // just before the next capture/extension cycle. Keep that real trigger visible
  // instead of collapsing the green event segment to zero width.
  const minimumEventSpan = Math.min(1, duration);
  const last = Math.min(Math.max(bounds.last, first + minimumEventSpan), duration);
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
    const camera = recording.camera_name || recording.metadata?.camera_name || '';
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

// ─── Time-range selector (segmented buttons) ───────────────────────────────
els.rangeBtns.forEach((btn) => {
  btn.addEventListener('click', () => {
    activeRange = btn.dataset.range;
    els.rangeBtns.forEach((other) => {
      const active = other === btn;
      other.classList.toggle('active', active);
      other.setAttribute('aria-selected', String(active));
    });
    loadStats().then(() => loadEvents().then(renderActivityFeed)).catch(() => {});
  });
});

// ─── Refresh orchestration ──────────────────────────────────────────────────
async function refreshAll() {
  await Promise.all([loadStats(), loadEvents(), loadSystemResources()]);
  renderActivityFeed();
}

// Re-render when the user's date_format / time_format changes in another tab
// (driven by utils.js daygleDatePrefsChanged hook). 5s stats / 30s events
// polls keep things fresh in the meantime.
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  renderActivityFeed();
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
    await loadConfiguredLabels();
    await refreshAll();
  })
  .catch((error) => {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(error.message, true);
  });

setInterval(() => { loadStats().catch(() => {}); }, 10000);
setInterval(() => { loadSystemResources().catch(() => {}); }, 5000);
setInterval(() => {
  loadEvents().then(renderActivityFeed).catch(() => {});
}, 30000);
