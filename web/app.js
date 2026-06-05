const els = {
  statusText: document.getElementById('statusText'),
  totalEvents: document.getElementById('totalEvents'),
  totalAlerts: document.getElementById('totalAlerts'),
  frameNumber: document.getElementById('frameNumber'),
  generateBtn: document.getElementById('generateBtn'),
  userMenuBtn: document.getElementById('userMenuBtn'),
  usersLink: document.getElementById('usersLink'),
  settingsLink: document.getElementById('settingsLink'),
  uploadForm: document.getElementById('uploadForm'),
  imageInput: document.getElementById('imageInput'),
  uploadBtn: document.getElementById('uploadBtn'),
  uploadResult: document.getElementById('uploadResult'),
  searchBtn: document.getElementById('searchBtn'),
  clearBtn: document.getElementById('clearBtn'),
  searchInput: document.getElementById('searchInput'),
  events: document.getElementById('events'),
  alerts: document.getElementById('alerts'),
  objectStats: document.getElementById('objectStats'),
};

let authState = { user: null, csrfToken: null };

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authState.csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = authState.csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    window.location.href = '/login';
    throw new Error('Authentication required');
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;',
  })[char]);
}

function formatDate(value) {
  if (!value) return 'Unknown time';
  return new Date(value).toLocaleString();
}

function detectionBadges(detections = []) {
  if (!detections.length) return '<span class="muted">No detections</span>';
  return detections.map((d) => {
    const confidence = Math.round((d.confidence || 0) * 100);
    return `<span class="detection">${escapeHtml(d.label)} · ${confidence}%</span>`;
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
      <p class="muted">Source: ${escapeHtml(event.source)}</p>
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
        <span>${escapeHtml(alert.rule_name)}</span>
        <span>${formatDate(alert.created_at)}</span>
      </div>
      <p>${escapeHtml(alert.message)}</p>
      <span class="detection">${escapeHtml(alert.label)} · ${Math.round(alert.confidence * 100)}%</span>
    </div>
  `).join('');
}

function renderObjectStats(objects = []) {
  if (!objects.length) {
    els.objectStats.innerHTML = '<span class="muted">No objects indexed yet.</span>';
    return;
  }

  els.objectStats.innerHTML = objects.map((obj) => `
    <button class="chip" data-label="${escapeHtml(obj.label)}">${escapeHtml(obj.label)} · ${obj.count}</button>
  `).join('');

  document.querySelectorAll('[data-label]').forEach((button) => {
    button.addEventListener('click', () => {
      els.searchInput.value = button.dataset.label;
      loadEvents(button.dataset.label);
    });
  });
}

async function loadAuth() {
  const authInfo = await api('/api/auth/me');
  authState = { user: authInfo.user, csrfToken: authInfo.csrf_token };
  els.userMenuBtn.textContent = `${authInfo.user.username} ▼`;
  if (authInfo.user.role !== 'admin') {
    els.usersLink.hidden = true;
    els.settingsLink.hidden = true;
  }
}

async function loadStatus() {
  try {
    const status = await api('/api/status');
    const aiState = status.ai_available === false ? 'AI unavailable' : status.ai_backend;
    els.statusText.textContent = `${status.status} · ${status.mode} · ${aiState}`;
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

els.uploadForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = els.imageInput.files[0];
  if (!file) {
    els.uploadResult.textContent = 'Choose an image file first.';
    return;
  }

  const formData = new FormData();
  formData.append('file', file);
  els.uploadBtn.disabled = true;
  els.uploadBtn.textContent = 'Detecting...';
  els.uploadResult.textContent = 'Running detector and saving event...';
  try {
    const result = await api('/api/detect/test-image', { method: 'POST', body: formData });
    els.uploadResult.textContent = `Created event #${result.event_id} with ${result.detections.length} detection(s).`;
    await refreshAll();
  } catch (error) {
    els.uploadResult.textContent = error.message;
  } finally {
    els.uploadBtn.disabled = false;
    els.uploadBtn.textContent = 'Test image detection';
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

loadAuth().then(refreshAll);
setInterval(loadStatus, 3000);
setInterval(loadStats, 10000);
