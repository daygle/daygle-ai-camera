let csrfToken = null;
let currentUser = null;
const messageEl = document.getElementById('anprMessage');
const recentPlatesEl = document.getElementById('recentPlates');
const plateResultsEl = document.getElementById('plateResults');
const plateDetailsEl = document.getElementById('plateDetails');
const alertRulesEl = document.getElementById('plateAlertRules');
const alertForm = document.getElementById('plateAlertRuleForm');
const searchInput = document.getElementById('plateSearchInput');
let selectedCameraId = '';

function queryWithCamera(path) {
  const separator = path.includes('?') ? '&' : '?';
  return selectedCameraId ? `${path}${separator}camera_id=${encodeURIComponent(selectedCameraId)}` : path;
}

function getCameraFilterSelect() {
  return document.getElementById('anprCameraFilter');
}

function ensureCameraFilterControl() {
  const searchRow = document.querySelector('.search-row');
  if (!searchRow) return null;
  searchRow.classList.add('anpr-search-row');
  let select = getCameraFilterSelect();
  if (!select) {
    select = document.createElement('select');
    select.id = 'anprCameraFilter';
    select.setAttribute('aria-label', 'Filter by camera');
    select.innerHTML = '<option value="">All cameras</option>';
    searchRow.prepend(select);
  }
  if (!select.dataset.boundChangeHandler) {
    select.addEventListener('change', async () => {
      selectedCameraId = select.value;
      plateResultsEl.innerHTML = '';
      plateDetailsEl.innerHTML = '';
      await loadAll();
      setMessage(selectedCameraId ? 'Showing ANPR results for selected camera.' : 'Showing ANPR results for all cameras.');
    });
    select.dataset.boundChangeHandler = 'true';
  }
  return select;
}

function renderCameraOptions(cameras) {
  const select = ensureCameraFilterControl();
  if (!select) return;
  const previous = selectedCameraId || select.value;
  const options = ['<option value="">All cameras</option>'];
  for (const camera of cameras || []) {
    const id = String(camera.id || '').trim();
    if (!id) continue;
    const label = String(camera.name || id).trim();
    options.push(`<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`);
  }
  select.innerHTML = options.join('');
  if (previous && Array.from(select.options).some((option) => option.value === previous)) {
    select.value = previous;
    selectedCameraId = previous;
  } else {
    select.value = '';
    selectedCameraId = '';
  }
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : 'Unknown time';
}

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  if (text) window.showToast?.(text, isError);
}

function renderPlateCards(container, plates) {
  container.innerHTML = plates.length ? plates.map((plate) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(plate.plate_number)}</span><span>${plate.sighting_count} sighting(s)</span></div>
      <p class="muted">First seen: ${formatDate(plate.first_seen)} · Last seen: ${formatDate(plate.last_seen)}</p>
      <p class="muted">${plate.is_blacklisted ? 'Blacklisted' : plate.is_whitelisted ? 'Whitelisted' : 'Unknown'} ${plate.notes ? `· ${escapeHtml(plate.notes)}` : ''}</p>
      <button class="secondary" data-action="details" data-id="${plate.id}">Details</button>
      ${currentUser?.role === 'admin' ? `<button class="secondary" data-action="whitelist" data-plate="${escapeHtml(plate.plate_number)}">Whitelist</button><button class="secondary" data-action="blacklist" data-plate="${escapeHtml(plate.plate_number)}">Blacklist</button>` : ''}
    </div>
  `).join('') : '<div class="empty">No plates found.</div>';
}

function renderSightings(events) {
  plateResultsEl.innerHTML = events.length ? events.map((event) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(event.plate_number)}</span><span>${Math.round(event.confidence * 100)}%</span></div>
      <p class="muted">${formatDate(event.created_at)} · Event #${event.event_id}</p>
      <p class="muted">Image: ${escapeHtml(event.image_path || 'none')}</p>
      <div>${(event.event?.recordings || []).map((recording) => `<a class="link-button" href="/recordings?recording_id=${recording.id}">Play clip #${recording.id}</a>`).join('') || '<span class="muted">No recording</span>'}</div>
    </div>
  `).join('') : '<div class="empty">No plate sightings match.</div>';
}

function renderDetails(plate) {
  plateDetailsEl.innerHTML = `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(plate.plate_number)}</span><span>${plate.sighting_count} sighting(s)</span></div>
      <p class="muted">First seen: ${formatDate(plate.first_seen)} · Last seen: ${formatDate(plate.last_seen)}</p>
      <p class="muted">${plate.is_blacklisted ? 'Blacklisted' : plate.is_whitelisted ? 'Whitelisted' : 'Unknown'} ${plate.notes ? `· ${escapeHtml(plate.notes)}` : ''}</p>
    </div>
    ${(plate.events || []).map((event) => `
      <div class="item">
        <div class="item-title"><span>Event #${event.event_id}</span><span>${Math.round(event.confidence * 100)}%</span></div>
        <p class="muted">${formatDate(event.created_at)}</p>
        <div>${(event.event?.recordings || []).map((recording) => `<a class="link-button" href="/recordings?recording_id=${recording.id}">Play clip #${recording.id}</a>`).join('') || '<span class="muted">No recording</span>'}</div>
      </div>
    `).join('')}
  `;
}

function renderAlertRules(rules) {
  alertRulesEl.innerHTML = rules.length ? rules.map((rule) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(rule.rule_name)}</span><span>${rule.enabled ? 'Enabled' : 'Disabled'}</span></div>
      <p class="muted">${escapeHtml(rule.rule_type)} ${rule.plate_pattern ? `· ${escapeHtml(rule.plate_pattern)}` : ''} · cooldown ${rule.cooldown_seconds}s</p>
      ${currentUser?.role === 'admin' ? `<button class="secondary" data-action="edit-rule" data-rule='${JSON.stringify(rule).replace(/'/g, '&#39;')}'>Edit</button><button class="secondary" data-action="delete-rule" data-id="${rule.id}">Delete</button>` : ''}
    </div>
  `).join('') : '<div class="empty">No plate alert rules configured.</div>';
}

async function loadAll() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  currentUser = me.user;
  const camerasPayload = await api('/api/cameras');
  renderCameraOptions(camerasPayload.cameras || []);
  const plates = await api(queryWithCamera('/api/plates'));
  renderPlateCards(recentPlatesEl, plates);
  renderAlertRules(await api('/api/plate-alerts'));
  const deleteAllBtn = document.getElementById('deleteAllPlatesBtn');
  if (deleteAllBtn) deleteAllBtn.hidden = currentUser?.role !== 'admin' || plates.length === 0;
}

async function searchPlates() {
  renderSightings(await api(queryWithCamera(`/api/plates/search?q=${encodeURIComponent(searchInput.value.trim())}`)));
}

async function updatePlateStatus(action, plateNumber) {
  const notes = window.prompt('Notes for this plate:', '') || '';
  const updated = await api(`/api/plates/${action}`, { method: 'POST', body: JSON.stringify({ plate_number: plateNumber, notes }) });
  renderDetails(updated);
  await loadAll();
  setMessage(`${plateNumber} updated.`);
}

document.getElementById('plateSearchBtn').addEventListener('click', searchPlates);
document.getElementById('plateClearBtn').addEventListener('click', async () => {
  searchInput.value = '';
  await loadAll();
  plateResultsEl.innerHTML = '';
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action === 'details') renderDetails(await api(queryWithCamera(`/api/plates/${button.dataset.id}`)));
  if (button.dataset.action === 'whitelist') await updatePlateStatus('whitelist', button.dataset.plate);
  if (button.dataset.action === 'blacklist') await updatePlateStatus('blacklist', button.dataset.plate);
  if (button.dataset.action === 'edit-rule') {
    const rule = JSON.parse(button.dataset.rule);
    for (const [key, value] of Object.entries(rule)) if (alertForm.elements[key]) alertForm.elements[key].value = String(value ?? '');
  }
  if (button.dataset.action === 'delete-rule') {
    await api(`/api/plate-alerts/${button.dataset.id}`, { method: 'DELETE' });
    renderAlertRules(await api('/api/plate-alerts'));
    setMessage('Plate alert rule deleted.');
  }
  if (button.id === 'deleteAllPlatesBtn') {
    if (!confirm('Delete ALL plates and sightings? This cannot be undone.')) return;
    try {
      const result = await api('/api/plates', { method: 'DELETE' });
      plateResultsEl.innerHTML = '';
      plateDetailsEl.innerHTML = '';
      await loadAll();
      setMessage(`Deleted ${result.deleted} plate(s).`);
    } catch (error) {
      setMessage(`Failed: ${error.message}`, true);
    }
  }
});

alertForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(alertForm).entries());
  data.enabled = data.enabled === 'true';
  data.cooldown_seconds = Number.parseInt(data.cooldown_seconds || '60', 10);
  const id = data.id;
  delete data.id;
  await api(id ? `/api/plate-alerts/${id}` : '/api/plate-alerts', { method: id ? 'PUT' : 'POST', body: JSON.stringify(data) });
  alertForm.reset();
  renderAlertRules(await api('/api/plate-alerts'));
  setMessage('Plate alert rule saved.');
});

document.getElementById('cancelPlateRuleEdit').addEventListener('click', () => alertForm.reset());
loadAll().catch((error) => setMessage(error.message, true));
