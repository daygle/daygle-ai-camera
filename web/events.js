// events.js - Dedicated Events page (the single activity feed).
// Loaded by events.html only. Shows GRANULAR detection events (one row per
// occurrence) from /api/events, filterable by type (object / motion / sound)
// and time range. An alert is just a property of an event (whether a
// notification fired), surfaced as an indicator on the row - there is no
// separate alerts page. Each row links to the recording it belongs to, and a
// recording spans many events (event.recording_id).
//
// isSoundLabel, GENERIC_TRIGGER_LABELS, detectionPill, motionPill, formatDate,
// timeAgo, escapeHtml, titleCase, cameraLabel, daygleSinceParamForRange and
// api() are all provided by web/utils.js.

const els = {
  eventFeed: document.getElementById('eventFeed'),
  listStatus: document.getElementById('listStatus'),
  filterPills: document.querySelectorAll('[data-filter]'),
  rangeBtns: document.querySelectorAll('[data-range]'),
  statTotalEvents: document.getElementById('statTotalEvents'),
  statObjectEvents: document.getElementById('statObjectEvents'),
  statSoundEvents: document.getElementById('statSoundEvents'),
};

let allEvents = [];
let activeFilter = 'all';
let activeRange = 'today';

function getSinceParam() {
  return daygleSinceParamForRange(activeRange);
}

// ─── Event classification ───────────────────────────────────────────────────
// Mirrors the recordings page: sound events come from the sound
// detector; motion-only events carry no concrete object label; everything else
// is an object event.
function concreteLabels(event) {
  return (event.detections || [])
    .map((d) => String(d && d.label || '').trim().toLowerCase())
    .filter((label) => label && !GENERIC_TRIGGER_LABELS.has(label));
}

function eventIsSound(event) {
  if (!event) return false;
  if (String(event.source || '').toLowerCase() === 'sound') return true;
  if (event.metadata && event.metadata.source === 'sound-detection') return true;
  if ((event.detections || []).some((d) => isSoundLabel(d && d.label))) return true;
  return isSoundLabel(event.metadata && event.metadata.label);
}

function eventIsMotionOnly(event) {
  if (!event || eventIsSound(event)) return false;
  const detections = event.detections || [];
  if (!detections.length) return false;
  return concreteLabels(event).length === 0
    && detections.some((d) => String(d && d.label || '').trim().toLowerCase() === 'motion');
}

function eventKind(event) {
  if (eventIsSound(event)) return 'sound';
  if (eventIsMotionOnly(event)) return 'motion';
  return 'object';
}

// ─── Row rendering ──────────────────────────────────────────────────────────
function eventTitle(event) {
  const kind = eventKind(event);
  if (kind === 'sound') {
    const meta = event.metadata || {};
    const raw = meta.class_label || meta.label || (event.detections[0] && event.detections[0].label) || 'Sound';
    return titleCase(String(raw).replace(/_/g, ' '));
  }
  if (kind === 'motion') return 'Motion';
  const labels = concreteLabels(event);
  if (!labels.length) return 'Detection';
  return labels.map((label) => titleCase(label)).join(' · ');
}

function eventPills(event) {
  const kind = eventKind(event);
  if (kind === 'sound') {
    const meta = event.metadata || {};
    const soundDetections = (event.detections || []).filter((d) => isSoundLabel(d && d.label));
    if (soundDetections.length) {
      return soundDetections.map((d) => detectionPill(d.label, d.confidence, true)).join('');
    }
    const label = meta.class_label || meta.label;
    const conf = typeof meta.confidence === 'number' ? meta.confidence : null;
    return label ? detectionPill(label, conf, true) : '';
  }
  if (kind === 'motion') {
    const strongest = (event.detections || [])
      .filter((d) => String(d && d.label || '').toLowerCase() === 'motion')
      .reduce((best, d) => (d && d.confidence > (best ? best.confidence : -1) ? d : best), null);
    return motionPill(strongest ? strongest.confidence : null);
  }
  const objectDetections = (event.detections || [])
    .filter((d) => d && d.label && !GENERIC_TRIGGER_LABELS.has(String(d.label).trim().toLowerCase()));
  return objectDetections.map((d) => detectionPill(d.label, d.confidence)).join('') || '<span class="muted">No detections</span>';
}

function eventCameraLabel(event) {
  const meta = event.metadata || {};
  return cameraLabel(meta.camera_name, meta.camera_id) || event.source || 'unknown';
}

function renderEventRow(event) {
  const created = event.created_at || '';
  const camera = eventCameraLabel(event);
  const kind = eventKind(event);
  const typeClass = kind === 'sound' ? 'activity-item-sound'
    : kind === 'motion' ? 'activity-item-motion'
    : 'activity-item-event';
  const typeLabel = kind === 'sound' ? 'Sound Event'
    : kind === 'motion' ? 'Motion Event'
    : 'Object Event';
  const alerted = Boolean(event.alert) || Boolean(event.alert_triggered);
  const alertBadge = alerted
    ? '<span class="detection detection-alert" title="An alert notification was fired for this event">🔔 Alert</span>'
    : '';
  // Two per-event actions: open the annotated snapshot (green detection
  // boxes, as in alert emails) and/or open the recording the event belongs to.
  const actions = [];
  if (event.has_snapshot) {
    actions.push(`<a class="secondary activity-item-action" href="/api/events/${encodeURIComponent(event.id)}/snapshot" target="_blank" rel="noopener" aria-label="Open snapshot for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg><span class="activity-action-label">Snapshot</span></a>`);
  }
  if (event.recording_id != null) {
    actions.push(`<a class="secondary activity-item-action" href="/recordings/${encodeURIComponent(event.recording_id)}" aria-label="Open recording for event ${escapeHtml(String(event.id))}"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg><span class="activity-action-label">Recording</span></a>`);
  }
  const recordingAction = actions.length ? actions.join('') : '<span class="muted">-</span>';
  return `
    <tr class="activity-table-row ${typeClass}" data-event-row="${escapeHtml(String(event.id))}">
      <td class="activity-cell-type"><span class="activity-item-type">${escapeHtml(typeLabel)}</span><span class="activity-cell-ref">${escapeHtml(eventTitle(event))}</span></td>
      <td class="activity-cell-camera">${escapeHtml(camera)}</td>
      <td class="activity-cell-detections"><div class="activity-item-badges">${eventPills(event)}${alertBadge}</div></td>
      <td class="activity-cell-when">
        <div class="activity-item-when">
          <div class="activity-item-when-relative">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span>${escapeHtml(timeAgo(created))}</span>
          </div>
          <span class="activity-item-when-absolute">${escapeHtml(formatDate(created))}</span>
        </div>
      </td>
      <td class="activity-cell-actions"><div class="cell-actions">${recordingAction}</div></td>
    </tr>
  `;
}

function visibleEvents() {
  if (activeFilter === 'all') return allEvents;
  return allEvents.filter((event) => eventKind(event) === activeFilter);
}

function renderStats() {
  let object = 0;
  let sound = 0;
  for (const event of allEvents) {
    const kind = eventKind(event);
    if (kind === 'sound') sound += 1;
    else if (kind === 'object') object += 1;
  }
  if (els.statTotalEvents) els.statTotalEvents.textContent = String(allEvents.length);
  if (els.statObjectEvents) els.statObjectEvents.textContent = String(object);
  if (els.statSoundEvents) els.statSoundEvents.textContent = String(sound);
}

function renderList() {
  const events = visibleEvents();
  if (els.listStatus) {
    els.listStatus.textContent = events.length
      ? `${events.length} event${events.length === 1 ? '' : 's'}`
      : '';
  }
  if (!els.eventFeed) return;
  if (!events.length) {
    els.eventFeed.innerHTML = `
      <div class="activity-empty-state">
        <div class="activity-empty-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
        </div>
        <h2>No events in this range</h2>
        <p class="muted">Try a wider time range, or wait for a new detection.</p>
      </div>`;
    return;
  }
  els.eventFeed.innerHTML =
    '<div class="cameras-table-wrap"><table class="rule-table activity-table">' +
    '<thead><tr>' +
      '<th scope="col">Type</th>' +
      '<th scope="col">Camera</th>' +
      '<th scope="col">Detections</th>' +
      '<th scope="col">When</th>' +
      '<th class="cell-center" scope="col">Actions</th>' +
    '</tr></thead>' +
    '<tbody>' + events.map(renderEventRow).join('') + '</tbody>' +
    '</table></div>';
}

async function loadEvents() {
  if (els.eventFeed) els.eventFeed.innerHTML = '<p class="muted">Loading events…</p>';
  const params = new URLSearchParams({ limit: '500' });
  const since = getSinceParam();
  if (since) params.set('since', since);
  try {
    const data = await api(`/api/events?${params.toString()}`);
    allEvents = Array.isArray(data) ? data : [];
  } catch (err) {
    allEvents = [];
    if (els.eventFeed) els.eventFeed.innerHTML = '<p class="muted empty-state">Could not load events.</p>';
    if (typeof showToast === 'function') showToast('Failed to load events.', true);
    return;
  }
  renderStats();
  renderList();
}

function wireControls() {
  els.filterPills.forEach((pill) => {
    pill.addEventListener('click', () => {
      activeFilter = pill.dataset.filter || 'all';
      els.filterPills.forEach((p) => {
        const selected = p === pill;
        p.classList.toggle('active', selected);
        p.setAttribute('aria-selected', selected ? 'true' : 'false');
      });
      renderList();
    });
  });
  els.rangeBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      activeRange = btn.dataset.range || 'today';
      els.rangeBtns.forEach((b) => {
        const selected = b === btn;
        b.classList.toggle('active', selected);
        b.setAttribute('aria-selected', selected ? 'true' : 'false');
      });
      loadEvents();
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  wireControls();
  loadEvents();
});
