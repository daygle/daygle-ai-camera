// snapshots.js - Snapshots page (the captured-frame library under Clips).
// Loaded by snapshots.html only. Lists every event that saved a frame
// (event.has_snapshot) from /api/snapshots as a visual gallery. Each row
// opens the annotated snapshot image (/api/events/{id}/snapshot) and lets an
// admin delete the stored image - deleting a snapshot never touches the
// event or its recording.
//
// Unlike /api/recordings, /api/snapshots has no server-side filter params
// (only limit + since), so the recordings-style filter card (camera, label,
// from/to date+time, sort) filters the already-fetched snapshot list
// client-side. The endpoint returns full event objects - detections,
// metadata and created_at are all present - so every filter below is exact.
//
// escapeHtml, api, showToast, timeAgo, formatDate, cameraLabel, detectionPill,
// motionPill, isSoundLabel, GENERIC_TRIGGER_LABELS, titleCase,
// formatUserDate, renderTimeSelect and timeSelectValue are all provided by
// web/utils.js.

const els = {
  gallery: document.getElementById('snapshotGallery'),
  listStatus: document.getElementById('listStatus'),
  cameraFilter: document.getElementById('snapshotCameraFilter'),
  labelFilter: document.getElementById('snapshotLabelFilter'),
  faceFilter: document.getElementById('snapshotFaceFilter'),
  faceField: document.getElementById('snapshotFaceField'),
  dateFrom: document.getElementById('snapshotDateFrom'),
  timeFrom: null, // populated by renderFilterTimeSelects() below
  dateTo: document.getElementById('snapshotDateTo'),
  timeTo: null,   // populated by renderFilterTimeSelects() below
  sort: document.getElementById('snapshotSort'),
  filterForm: document.getElementById('snapshotFilterForm'),
  clearBtn: document.getElementById('snapshotClearBtn'),
  statTotal: document.getElementById('statTotalSnapshots'),
  statCameras: document.getElementById('statCameraCount'),
  statAlerted: document.getElementById('statAlertedCount'),
  statFilterStatus: document.getElementById('statFilterStatus'),
  statFilterHint: document.getElementById('statFilterHint'),
};

let allSnapshots = [];

// ── Filter time pickers (shared with /recordings) ───────────────────────
// Mount spans in the filter form render through the shared `renderTimeSelect`
// helper (web/utils.js) so the From / To time pickers follow the user's
// Profile > Time Format choice (12h with AM/PM vs. 24h). Re-rendered on
// init, on Reset Filters, and whenever the cross-tab prefs hook fires so a
// profile change instantly swaps the picker style without a refresh.
const FILTER_TIME_FROM_DEFAULT = '00:00';
const FILTER_TIME_TO_DEFAULT = '23:55';

function renderFilterTimeSelect(mountId, defaultValue) {
  const mount = document.getElementById(mountId);
  if (!mount) return null;
  const role = mount.dataset.timeRole || '';
  mount.innerHTML = renderTimeSelect(defaultValue, 'data-filter-time-role', role);
  return mount.querySelector('.time-select-wrap');
}

function renderFilterTimeSelects() {
  els.timeFrom = renderFilterTimeSelect('snapshotTimeFromMount', FILTER_TIME_FROM_DEFAULT);
  els.timeTo = renderFilterTimeSelect('snapshotTimeToMount', FILTER_TIME_TO_DEFAULT);
}

renderFilterTimeSelects();

// ── Snapshot classification ─────────────────────────────────────────────
// Mirrors the events page so the pill labels read identically: sound events
// come from the sound detector; motion-only frames carry no concrete object
// label; everything else is an object frame.
function concreteLabels(event) {
  return (event.detections || [])
    .map((d) => String(d && d.label || '').trim().toLowerCase())
    .filter((label) => label && !GENERIC_TRIGGER_LABELS.has(label));
}

function snapshotIsSound(event) {
  if (!event) return false;
  if (String(event.source || '').toLowerCase() === 'sound') return true;
  if (event.metadata && event.metadata.source === 'sound-detection') return true;
  if ((event.detections || []).some((d) => isSoundLabel(d && d.label))) return true;
  return isSoundLabel(event.metadata && event.metadata.label);
}

function snapshotKind(event) {
  if (snapshotIsSound(event)) return 'sound';
  const detections = event.detections || [];
  if (detections.length && concreteLabels(event).length === 0
      && detections.some((d) => String(d && d.label || '').trim().toLowerCase() === 'motion')) {
    return 'motion';
  }
  return 'object';
}

function snapshotPills(event) {
  const kind = snapshotKind(event);
  if (kind === 'sound') {
    const meta = event.metadata || {};
    const soundDetections = (event.detections || []).filter((d) => isSoundLabel(d && d.label));
    if (soundDetections.length) {
      return soundDetections.map((d) => detectionPill(d.label, d.confidence, true)).join('');
    }
    const label = meta.class_label || meta.label;
    return label ? detectionPill(label, meta.confidence, true) : '';
  }
  if (kind === 'motion') {
    const strongest = (event.detections || [])
      .filter((d) => String(d && d.label || '').toLowerCase() === 'motion')
      .reduce((best, d) => (d && d.confidence > (best ? best.confidence : -1) ? d : best), null);
    return motionPill(strongest ? strongest.confidence : null);
  }
  const detections = event.detections || [];
  const objectDetections = detections
    .filter((d) => d && d.label && !GENERIC_TRIGGER_LABELS.has(String(d.label).trim().toLowerCase()));
  const motionDetections = detections
    .filter((d) => String(d && d.label || '').trim().toLowerCase() === 'motion');
  const strongestMotion = motionDetections.reduce(
    (best, d) => (d && Number(d.confidence) > (best ? Number(best.confidence) : -1) ? d : best),
    null,
  );
  const motionBadge = motionDetections.length ? motionPill(strongestMotion?.confidence ?? null) : '';
  return `${motionBadge}${objectDetections.map((d) => detectionPill(d.label, d.confidence)).join('')}`
    || '<span class="muted">No detections</span>';
}

function snapshotCameraLabel(event) {
  const meta = event.metadata || {};
  return cameraLabel(meta.camera_name, meta.camera_id) || event.source || 'unknown';
}

// ── Filter helpers ──────────────────────────────────────────────────────
// Snapshot events arrive from /api/snapshots with metadata.camera_id /
// metadata.camera_name. The camera filter select is populated from
// /api/cameras (id -> name), so an event matches when either its stored id
// equals the selected id or its stored name equals the selected camera name.
function snapshotMatchesCamera(event, cameraId) {
  if (!cameraId) return true;
  const meta = event.metadata || {};
  if (meta.camera_id && String(meta.camera_id) === String(cameraId)) return true;
  const option = Array.from(els.cameraFilter?.options || []).find((o) => o.value === String(cameraId));
  const cameraName = option?.textContent;
  if (cameraName && meta.camera_name && String(meta.camera_name).toLowerCase() === String(cameraName).toLowerCase()) {
    return true;
  }
  // Fall back to the display label the row uses so snapshots whose metadata
  // lacks a structured camera_id still filter correctly.
  return snapshotCameraLabel(event).toLowerCase() === String(cameraName || cameraId).toLowerCase();
}

// A snapshot matches a label when any of its detections carries that label
// (object labels, sound labels and the generic 'motion' trigger all flow
// through the same detection array), falling back to the metadata label for
// sound events that stored it there instead.
function snapshotHasLabel(event, label) {
  const needle = String(label || '').trim().toLowerCase();
  if (!needle) return true;
  const detections = event.detections || [];
  if (detections.some((d) => String(d && d.label || '').trim().toLowerCase() === needle)) return true;
  const meta = event.metadata || {};
  return String(meta.label || meta.class_label || '').trim().toLowerCase() === needle;
}

function parseTimeParts(timeString) {
  const match = String(timeString || '').match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  return {
    hour: Math.min(23, Math.max(0, Number.parseInt(match[1], 10) || 0)),
    minute: Math.min(59, Math.max(0, Number.parseInt(match[2], 10) || 0)),
  };
}

// Build a local-time Date for a YYYY-MM-DD + HH:MM filter bound, so the
// From/To semantics feel intuitive (the browser returns dates without a
// timezone). Returns null when the date string is unusable.
function localBoundary(dateString, timeString, endOfDay) {
  const [year, month, day] = String(dateString || '').split('-').map((part) => Number.parseInt(part, 10));
  if (!year || !month || !day) return null;
  const fallback = endOfDay ? { hour: 23, minute: 59 } : { hour: 0, minute: 0 };
  const parts = parseTimeParts(timeString) || fallback;
  return new Date(year, month - 1, day, parts.hour, parts.minute, endOfDay ? 59 : 0, endOfDay ? 999 : 0);
}

function snapshotInRange(event, filters) {
  const created = Date.parse(event.created_at || '');
  if (!Number.isFinite(created)) return true;
  const fromBoundary = localBoundary(filters.dateFrom, filters.timeFrom, false);
  if (fromBoundary && created < fromBoundary.getTime()) return false;
  const toBoundary = localBoundary(filters.dateTo, filters.timeTo, true);
  if (toBoundary && created > toBoundary.getTime()) return false;
  return true;
}

function currentFilterValues() {
  return {
    label: els.labelFilter?.value || '',
    face: els.faceFilter?.value || '',
    cameraId: els.cameraFilter?.value || '',
    dateFrom: els.dateFrom?.value || '',
    // Read from the custom hour/minute (/AM/PM) selects so the filter value
    // always matches what the user sees in the picker.
    timeFrom: timeSelectValue(els.timeFrom) || FILTER_TIME_FROM_DEFAULT,
    dateTo: els.dateTo?.value || '',
    timeTo: timeSelectValue(els.timeTo) || FILTER_TIME_TO_DEFAULT,
    sort: els.sort?.value || 'newest',
  };
}

function describeFilters(filters) {
  const parts = [];
  if (filters.label) {
    const option = els.labelFilter?.querySelector(`option[value="${escapeHtml(filters.label)}"]`);
    parts.push(`label “${option?.textContent || filters.label}”`);
  }
  if (filters.face) {
    const faceOption = els.faceFilter?.querySelector(`option[value="${escapeHtml(filters.face)}"]`);
    parts.push(`face “${faceOption?.textContent || filters.face}”`);
  }
  if (filters.cameraId) {
    const cameraOption = Array.from(els.cameraFilter?.options || []).find((o) => o.value === filters.cameraId);
    parts.push(`camera “${cameraOption?.textContent || filters.cameraId}”`);
  }
  if (filters.dateFrom) parts.push(`from ${formatUserDate(filters.dateFrom)} ${filters.timeFrom || FILTER_TIME_FROM_DEFAULT}`);
  if (filters.dateTo) parts.push(`through ${formatUserDate(filters.dateTo)} ${filters.timeTo || FILTER_TIME_TO_DEFAULT}`);
  return parts;
}

function updateFilterStat(filters) {
  if (!els.statFilterStatus || !els.statFilterHint) return;
  const active = describeFilters(filters);
  if (active.length) {
    els.statFilterStatus.textContent = 'Filtered';
    els.statFilterHint.textContent = `Showing snapshots matching ${active.join(' and ')}.`;
  } else {
    els.statFilterStatus.textContent = 'All';
    els.statFilterHint.textContent = 'Showing every snapshot';
  }
}

function snapshotRow(event) {
  const created = event.created_at || '';
  const camera = snapshotCameraLabel(event);
  const kind = snapshotKind(event);
  const typeClass = kind === 'sound' ? 'activity-item-sound'
    : kind === 'motion' ? 'activity-item-motion'
    : 'activity-item-event';
  const typeLabel = kind === 'sound' ? 'Sound Event'
    : kind === 'motion' ? 'Motion Event'
    : 'Object Event';
  const alerted = Boolean(event.alert);
  const alertBadge = alerted
    ? '<span class="detection detection-alert" title="An alert notification was fired for this event">🔔 Alert</span>'
    : '';
  const snapshotUrl = `/api/events/${encodeURIComponent(event.id)}/snapshot`;
  const actions = [
    `<a class="secondary activity-item-action activity-item-action-snapshot" href="${snapshotUrl}" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg><span class="activity-action-label">Open</span></a>`,
  ];
  // Deleting a snapshot is an admin action (the backend requires admin).
  if (window.daygleAuth?.user?.role === 'admin') {
    actions.push(`<button class="secondary delete-btn activity-item-action" data-delete-snapshot="${escapeHtml(String(event.id))}" type="button" aria-label="Delete snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg><span class="activity-action-label">Delete</span></button>`);
  }
  return `
    <article class="snapshot-row ${typeClass}" data-snapshot-row="${escapeHtml(String(event.id))}">
      <a class="snapshot-row-thumb" href="${snapshotUrl}" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}">
        <img src="${snapshotUrl}" alt="Snapshot for event ${escapeHtml(String(event.id))} on ${escapeHtml(camera)}" loading="lazy" onerror="this.remove()" />
      </a>
      <div class="snapshot-row-body">
        <div class="snapshot-row-head">
          <span class="snapshot-ref">Event #${escapeHtml(String(event.id))}</span>
          <span class="activity-item-type">${escapeHtml(typeLabel)}</span>
        </div>
        <div class="snapshot-row-meta">
          <span class="snapshot-camera">${escapeHtml(camera)}</span>
          <span class="snapshot-when" title="${escapeHtml(formatDate(created))}">
            <span class="activity-item-when">
              <span class="activity-item-when-relative">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span>${escapeHtml(timeAgo(created))}</span>
              </span>
              <span class="activity-item-when-absolute">${escapeHtml(formatDate(created))}</span>
            </span>
          </span>
        </div>
        <div class="activity-item-badges snapshot-row-badges">${snapshotPills(event)}${faceIdentityPills(eventFaceIdentities(event))}${alertBadge}</div>
      </div>
      <div class="snapshot-row-actions">${actions.join('')}</div>
    </article>
  `;
}

function renderStats(snapshots) {
  const cameras = new Set(snapshots.map((event) => snapshotCameraLabel(event)).filter(Boolean));
  let alerted = 0;
  for (const event of snapshots) {
    if (event.alert) alerted += 1;
  }
  if (els.statTotal) els.statTotal.textContent = String(snapshots.length);
  if (els.statCameras) els.statCameras.textContent = String(cameras.size);
  if (els.statAlerted) els.statAlerted.textContent = String(alerted);
}

function renderGallery(snapshots) {
  if (els.listStatus) {
    els.listStatus.textContent = snapshots.length
      ? `${snapshots.length} snapshot${snapshots.length === 1 ? '' : 's'}`
      : '';
  }
  if (!els.gallery) return;
  if (!snapshots.length) {
    els.gallery.innerHTML = `
      <div class="activity-empty-state snapshots-empty-state">
        <div class="activity-empty-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>
        </div>
        <h2>No snapshots match the current filters</h2>
        <p class="muted">Try a wider time range, clearing a filter, or waiting for a new detection to be captured.</p>
      </div>`;
    return;
  }
  els.gallery.innerHTML = snapshots.map(snapshotRow).join('');
  bindDeleteButtons();
}

function bindDeleteButtons() {
  document.querySelectorAll('[data-delete-snapshot]').forEach((button) => {
    button.addEventListener('click', async () => {
      const id = button.dataset.deleteSnapshot;
      if (!confirm(`Delete snapshot for event #${id}? The event and its recording stay intact.`)) return;
      try {
        await api(`/api/snapshots/${id}`, { method: 'DELETE' });
        window.showToast?.(`Deleted snapshot for event #${id}.`);
        await loadSnapshots();
      } catch (error) {
        // Skip UI updates if api() triggered a 401 redirect.
        if (window.daygleAuth?.redirecting) return;
        window.showToast?.(`Failed to delete snapshot: ${error.message}`, true);
      }
    });
  });
}

// Apply the active filters to the full snapshot list, then render.
function applyFilters() {
  const filters = currentFilterValues();
  const filtered = allSnapshots.filter((event) => {
    if (!snapshotMatchesCamera(event, filters.cameraId)) return false;
    if (!snapshotHasLabel(event, filters.label)) return false;
    if (!matchesFaceFilter(eventFaceIdentities(event), filters.face)) return false;
    if (!snapshotInRange(event, filters)) return false;
    return true;
  });
  if (filters.sort === 'oldest') {
    filtered.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
  }
  updateFilterStat(filters);
  renderStats(filtered);
  renderGallery(filtered);
}

// ── Camera + label filter options ────────────────────────────────────────
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
    // Silent api() fallback (no UI mutation) - redirect guard skipped by design.
  }
}

function populateLabelOptions() {
  if (!els.labelFilter) return;
  const counts = {};
  allSnapshots.forEach((event) => {
    const labels = new Set();
    (event.detections || []).forEach((d) => {
      const label = String(d && d.label || '').trim().toLowerCase();
      if (label) labels.add(label);
    });
    const meta = event.metadata || {};
    if (meta.label || meta.class_label) labels.add(String(meta.label || meta.class_label).trim().toLowerCase());
    labels.forEach((label) => { counts[label] = (counts[label] || 0) + 1; });
  });

  const options = [{ value: '', label: `All Labels${allSnapshots.length ? ` (${allSnapshots.length})` : ''}` }];
  const seen = new Set(['']);
  const addOption = (value) => {
    const normalized = String(value || '').trim().toLowerCase();
    // Mirror /recordings: strip generic trigger words so the dropdown only
    // surfaces concrete object/sound labels plus a single Motion option.
    if (!normalized || seen.has(normalized) || GENERIC_TRIGGER_LABELS.has(normalized)) return;
    seen.add(normalized);
    const count = counts[normalized];
    options.push({ value: normalized, label: count ? `${titleCase(normalized)} (${count})` : titleCase(normalized) });
  };
  allSnapshots.forEach((event) => {
    (event.detections || []).forEach((d) => addOption(d && d.label));
    const meta = event.metadata || {};
    addOption(meta.label);
    addOption(meta.class_label);
  });
  // Motion-only frames carry no concrete object label - surface a single
  // Motion option (like /recordings) so those snapshots stay filterable.
  if (allSnapshots.some((event) => snapshotKind(event) === 'motion')) addOption('motion');
  const ordered = [options[0], ...options.slice(1).sort((left, right) => left.label.localeCompare(right.label))];
  els.labelFilter.innerHTML = ordered.map((option) => (
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
  )).join('');
}

// Build the Face filter from the identities actually present in the loaded
// snapshots: one option per recognised person, plus "Any Face" / "Unknown"
// when applicable. The whole field stays hidden on deployments that never run
// face recognition (no face_identities in any snapshot), so it adds no clutter
// there. Mirrors populateLabelOptions' preserve-selection behaviour.
function populateFaceOptions() {
  if (!els.faceFilter) return;
  const previous = els.faceFilter.value || '';
  const people = new Map(); // key -> {name, count}
  let anyUnknown = 0;
  let anyFace = 0;
  for (const event of allSnapshots) {
    const { people: eventPeople, unknown } = eventFaceIdentities(event);
    if (eventPeople.size || unknown > 0) anyFace += 1;
    if (unknown > 0) anyUnknown += 1;
    for (const [key, person] of eventPeople) {
      const existing = people.get(key);
      if (existing) existing.count += 1;
      else people.set(key, { name: person.name, count: 1 });
    }
  }
  const hasFaces = people.size > 0 || anyUnknown > 0;
  if (els.faceField) els.faceField.hidden = !hasFaces;
  if (!hasFaces) {
    els.faceFilter.innerHTML = '<option value="">All Faces</option>';
    els.faceFilter.value = '';
    return;
  }
  const options = [{ value: '', label: `All Faces${anyFace ? ` (${anyFace})` : ''}` }, { value: 'any', label: 'Any Face' }];
  const peopleOptions = Array.from(people.entries())
    .map(([key, person]) => ({ value: key, label: `${person.name} (${person.count})` }))
    .sort((left, right) => left.label.localeCompare(right.label));
  options.push(...peopleOptions);
  if (anyUnknown > 0) options.push({ value: 'unknown', label: `Unknown (${anyUnknown})` });
  const values = new Set(options.map((option) => option.value));
  els.faceFilter.innerHTML = options.map((option) => (
    `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
  )).join('');
  els.faceFilter.value = values.has(previous) ? previous : '';
}

async function loadSnapshots() {
  if (els.gallery) els.gallery.innerHTML = '<p class="muted">Loading snapshots…</p>';
  try {
    const data = await api('/api/snapshots?limit=500');
    allSnapshots = Array.isArray(data) ? data : [];
  } catch (err) {
    allSnapshots = [];
    if (els.gallery) els.gallery.innerHTML = '<p class="muted empty-state">Could not load snapshots.</p>';
    if (typeof showToast === 'function') showToast('Failed to load snapshots.', true);
    return;
  }
  populateLabelOptions();
  populateFaceOptions();
  applyFilters();
}

function wireControls() {
  els.filterForm?.addEventListener('submit', (event) => {
    event.preventDefault();
    applyFilters();
  });
  // Camera and label are instant-pick filters (like /recordings); the date
  // range and sort apply on the Apply Filters button.
  els.cameraFilter?.addEventListener('change', () => applyFilters());
  els.labelFilter?.addEventListener('change', () => applyFilters());
  els.faceFilter?.addEventListener('change', () => applyFilters());
  els.clearBtn?.addEventListener('click', () => {
    if (els.labelFilter) els.labelFilter.value = '';
    if (els.faceFilter) els.faceFilter.value = '';
    if (els.cameraFilter) els.cameraFilter.value = '';
    if (els.dateFrom) els.dateFrom.value = '';
    if (els.dateTo) els.dateTo.value = '';
    if (els.sort) els.sort.value = 'newest';
    // Re-render the From/To time pickers back to their defaults. Going through
    // renderFilterTimeSelects (rather than poking child selects directly) means
    // Reset Filters also handles the 12h vs 24h AM/PM swap correctly.
    renderFilterTimeSelects();
    applyFilters();
  });
}

// Re-render the time pickers when the user's date_format / time_format
// changes in another tab (mirrors /recordings).
window.daygleDatePrefsChanged = function daygleDatePrefsChanged() {
  const preservedFrom = els.timeFrom ? timeSelectValue(els.timeFrom) : FILTER_TIME_FROM_DEFAULT;
  const preservedTo = els.timeTo ? timeSelectValue(els.timeTo) : FILTER_TIME_TO_DEFAULT;
  els.timeFrom = renderFilterTimeSelect('snapshotTimeFromMount', preservedFrom || FILTER_TIME_FROM_DEFAULT);
  els.timeTo = renderFilterTimeSelect('snapshotTimeToMount', preservedTo || FILTER_TIME_TO_DEFAULT);
};

document.addEventListener('DOMContentLoaded', async () => {
  wireControls();
  // Await the shared /api/auth/me so the delete button only renders for
  // admins (the backend enforces this either way).
  await window.daygleAuthReady;
  await loadCameras();
  loadSnapshots();
});
