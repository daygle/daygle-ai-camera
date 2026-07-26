// alerts.js - Dedicated alerts page.
// Loaded by alerts.html only. Shows notifications fired by zone and sound rules,
// with filtering by type (object / motion / sound) and per-alert dismissal.

// ─── DOM handles ────────────────────────────────────────────────────────────
const els = {
  alertFeed: document.getElementById('alertFeed'),
  listStatus: document.getElementById('listStatus'),
  dismissAllBtn: document.getElementById('dismissAllAlertsBtn'),
  filterPills: document.querySelectorAll('.activity-filter-pill'),
  statObjectAlerts: document.getElementById('statObjectAlerts'),
  statMotionAlerts: document.getElementById('statMotionAlerts'),
  statSoundAlerts: document.getElementById('statSoundAlerts'),
  rangeBtns: document.querySelectorAll('[data-range]'),
};

// SOUND_CLASS_IDS, isSoundLabel, isMotionOnlyAlertGroup, isMotionOnlyAlertItem,
// GENERIC_TRIGGER_LABELS, detectionPill, motionPill, formatDate, timeAgo,
// escapeHtml, titleCase, safeHtml are provided by web/utils.js.

let alertGroups = [];
let activeFilter = 'all';
let activeRange = 'today';

function dateDaysAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().split('T')[0];
}

function getSinceParam() {
  if (activeRange === 'today') return new Date().toISOString().split('T')[0];
  if (activeRange === '7d') return dateDaysAgo(7);
  if (activeRange === '30d') return dateDaysAgo(30);
  return ''; // 'all' - no since filter
}

// api() is provided by web/utils.js - shared CSRF, 401 redirect, JSON.

// ─── Alert grouping (consolidates multiple alerts for the same event) ──────
function groupAlertsByEvent(alerts) {
  const order = [];
  const groups = new Map();
  for (const alert of alerts) {
    const key = alert.event_id !== null && alert.event_id !== undefined ? `event-${alert.event_id}` : `alert-${alert.id}`;
    if (!groups.has(key)) {
      order.push(key);
      groups.set(key, {
        key,
        eventId: alert.event_id ?? null,
        camera: [alert.camera_name, alert.camera_id].filter(Boolean).join(' ('),
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
    const cam = group.camera.replace(/\(\s*$/, '').trim();
    return { ...group, labels: Array.from(group.labels), zones: Array.from(group.zones), camera: cam || 'unknown' };
  });
}

// ─── Rendering ──────────────────────────────────────────────────────────────

function alertIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>';
}

function motionActivityIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13" cy="4" r="2"/><path d="m4 19.5 4-4.5 1.5 4 5.5-3-2-7 4-3"/></svg>';
}

function soundIcon() {
  return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>';
}

function recordingLink(recordingId, label) {
  if (!recordingId) return '';
  const playIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
  return `<a class="secondary activity-item-action" href="/recordings?recording_id=${encodeURIComponent(recordingId)}">${playIcon} ${escapeHtml(label)}</a>`;
}

function cameraLabel(cameraName, cameraId) {
  const name = String(cameraName || '').trim();
  const id = String(cameraId || '').trim();
  if (name && id) return `${name} (${id})`;
  return name || id || '';
}

function renderAlertItem(group) {
  const isSound = group.labels.some((l) => SOUND_CLASS_IDS.has(l)) || group.detections.some((d) => SOUND_CLASS_IDS.has(String(d.label || '').toLowerCase()));
  const isMotionOnly = isMotionOnlyAlertItem({ ...group, detections: group.detections, labels: group.labels });
  const icon = isSound ? soundIcon() : isMotionOnly ? motionActivityIcon() : alertIcon();
  const typeClass = isSound ? 'activity-item-sound' : isMotionOnly ? 'activity-item-motion' : 'activity-item-alert';
  const typeLabel = isSound ? 'Sound Alert' : isMotionOnly ? 'Motion Alert' : 'Object Alert';
  const title = group.recordingId ? `Recording #${group.recordingId}` : 'Alert';
  const cameraLine = group.camera ? `Camera: ${escapeHtml(group.camera)}` : 'Camera: unknown';
  const zonePart = !isSound && group.zones?.length ? ` · Zone: ${group.zones.map(escapeHtml).join(', ')}` : '';
  const metaLine = `${cameraLine}${zonePart}`;
  const rulePart = group.ruleNames?.length ? ` · Rule: ${group.ruleNames.map(escapeHtml).join(', ')}` : '';
  const actions = [];
  if (group.recordingId) actions.push(recordingLink(group.recordingId, 'Footage'));

  if (window.daygleAuth?.user?.role === 'admin') {
    const dismissIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
    actions.push(`<button class="secondary delete-btn activity-item-action" data-dismiss-alert="${escapeHtml(String(group.key))}" type="button">${dismissIcon} Dismiss</button>`);
  }

  return `
    <article class="item activity-item ${typeClass}" data-activity-id="${escapeHtml(String(group.key))}" data-activity-type="alert">
      <div class="activity-item-icon">${icon}</div>
      <div class="activity-item-main">
        <div class="activity-item-header">
          <div class="activity-item-title">
            <span class="activity-item-type">${typeLabel}</span>
            <span class="activity-item-name">${title}</span>
          </div>
          <div class="activity-item-when">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            <span title="${escapeHtml(formatDate(group.latestAt))}">${escapeHtml(timeAgo(group.latestAt))}</span>
          </div>
        </div>
        <p class="muted activity-item-meta">${metaLine}${rulePart}</p>
        <div class="activity-item-badges">${isMotionOnly ? motionPill() : detectionBadges(group.detections, { isSound })}</div>
      </div>
      ${actions.length ? `<div class="activity-item-actions">${actions.join('')}</div>` : ''}
    </article>
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

function renderFeed() {
  const filtered = applyFilter(alertGroups);
  if (!filtered.length) {
    els.alertFeed.innerHTML = renderEmptyState();
    return;
  }
  els.alertFeed.innerHTML = filtered.map(renderAlertItem).join('');
  bindActions();
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
