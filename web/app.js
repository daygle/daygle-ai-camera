const els = {
  statusText: document.getElementById('statusText'),
  totalEvents: document.getElementById('totalEvents'),
  totalAlerts: document.getElementById('totalAlerts'),
  frameNumber: document.getElementById('frameNumber'),
  generateBtn: document.getElementById('generateBtn'),
  searchBtn: document.getElementById('searchBtn'),
  clearBtn: document.getElementById('clearBtn'),
  searchInput: document.getElementById('searchInput'),
  events: document.getElementById('events'),
  alerts: document.getElementById('alerts'),
  objectStats: document.getElementById('objectStats'),
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

function formatDate(value) {
  if (!value) return 'Unknown time';
  return new Date(value).toLocaleString();
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  return detections.map((d) => {
    const confidence = Math.round((d.confidence || 0) * 100);
    return `<span class="detection">${d.label} · ${confidence}%</span>`;
  }).join('');
}

function renderEvents(events) {
  if (!events.length) {
    els.events.innerHTML = '<div class="empty">No events yet. Generate a mock detection to start.</div>';
    return;
  }

  els.events.innerHTML = events.map((event) => `
    <div class="item">
      <div class="item-title">
        <span>Event #${event.id}</span>
        <span>${formatDate(event.created_at)}</span>
      </div>
      <div>${detectionBadges(event.detections)}</div>
      <p class="muted">Source: ${event.source}</p>
    </div>
  `).join('');
}

function renderAlerts(alerts) {
  if (!alerts.length) {
    els.alerts.innerHTML = '<div class="empty">No alerts triggered yet.</div>';
    return;
  }

  els.alerts.innerHTML = alerts.map((alert) => `
    <div class="item">
      <div class="item-title">
        <span>${alert.rule_name}</span>
        <span>${formatDate(alert.created_at)}</span>
      </div>
      <p>${alert.message}</p>
      <span class="detection">${alert.label} · ${Math.round(alert.confidence * 100)}%</span>
    </div>
  `).join('');
}

function renderObjectStats(objects = []) {
  if (!objects.length) {
    els.objectStats.innerHTML = '<span class="muted">No objects indexed yet.</span>';
    return;
  }

  els.objectStats.innerHTML = objects.map((obj) => `
    <button class="chip" data-label="${obj.label}">${obj.label} · ${obj.count}</button>
  `).join('');

  document.querySelectorAll('[data-label]').forEach((button) => {
    button.addEventListener('click', () => {
      els.searchInput.value = button.dataset.label;
      loadEvents(button.dataset.label);
    });
  });
}

async function loadStatus() {
  try {
    const status = await api('/api/status');
    els.statusText.textContent = status.status;
    els.frameNumber.textContent = status.frame_number;
  } catch (error) {
    els.statusText.textContent = 'offline';
  }
}

async function loadStats() {
  const stats = await api('/api/stats');
  els.totalEvents.textContent = stats.total_events;
  els.totalAlerts.textContent = stats.total_alerts;
  renderObjectStats(stats.objects);
}

async function loadEvents(label = '') {
  const path = label ? `/api/events?label=${encodeURIComponent(label)}` : '/api/events';
  renderEvents(await api(path));
}

async function loadAlerts() {
  renderAlerts(await api('/api/alerts'));
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadStats(), loadEvents(), loadAlerts()]);
}

els.generateBtn.addEventListener('click', async () => {
  els.generateBtn.disabled = true;
  els.generateBtn.textContent = 'Generating...';
  try {
    await api('/api/mock/detect', { method: 'POST' });
    await refreshAll();
  } finally {
    els.generateBtn.disabled = false;
    els.generateBtn.textContent = 'Generate mock detection';
  }
});

els.searchBtn.addEventListener('click', () => loadEvents(els.searchInput.value.trim()));
els.clearBtn.addEventListener('click', () => {
  els.searchInput.value = '';
  loadEvents();
});
els.searchInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') loadEvents(els.searchInput.value.trim());
});

refreshAll();
setInterval(loadStatus, 3000);
setInterval(loadStats, 10000);
