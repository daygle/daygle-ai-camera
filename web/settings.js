let csrfToken = null;
const aiForm = document.getElementById('aiSettingsForm');
const ruleForm = document.getElementById('alertRuleForm');
const messageEl = document.getElementById('settingsMessage');
const statusPanel = document.getElementById('aiStatusPanel');
const rulesEl = document.getElementById('alertRules');
const labelOptions = document.getElementById('labelOptions');

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || payload.reload_error || `Request failed: ${response.status}`);
  return payload;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function yesNo(value) { return value ? 'yes' : 'no'; }
function setMessage(text) { messageEl.textContent = text; }

function formPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['enabled']) data[key] = data[key] === 'true';
  for (const key of ['confidence', 'iou_threshold', 'min_confidence']) if (key in data && data[key] !== '') data[key] = Number(data[key]);
  for (const key of ['input_size', 'cooldown_seconds']) if (key in data && data[key] !== '') data[key] = Number.parseInt(data[key], 10);
  for (const key of ['active_start', 'active_end']) if (data[key] === '') data[key] = null;
  return data;
}

function renderStatus(status) {
  statusPanel.innerHTML = `
    <div><span>Current backend</span><strong>${escapeHtml(status.current_backend || status.configured_backend)}</strong></div>
    <div><span>Model path</span><strong>${escapeHtml(status.model_path || 'not set')}</strong></div>
    <div><span>Labels path</span><strong>${escapeHtml(status.labels_path || 'not set')}</strong></div>
    <div><span>Model exists</span><strong>${yesNo(status.model_exists)}</strong></div>
    <div><span>ONNX Runtime installed</span><strong>${yesNo(status.onnx_runtime_installed)}</strong></div>
    <div><span>Detector loaded</span><strong>${yesNo(status.detector_loaded)}</strong></div>
    <div><span>Active config source</span><strong>${escapeHtml(status.active_config_source)}</strong></div>
    <div><span>Mode</span><strong class="ai-mode ${escapeHtml(String(status.mode || '').toLowerCase().replace(/\s+/g, '-'))}">${escapeHtml(status.mode)}</strong></div>
    <div class="wide"><span>Last detector error</span><strong>${escapeHtml(status.last_detector_error || 'none')}</strong></div>
  `;
}

function renderAi(settings) {
  for (const [key, value] of Object.entries(settings)) {
    if (aiForm.elements[key]) aiForm.elements[key].value = String(value ?? '');
  }
  renderStatus(settings);
  if (settings.reload_succeeded === false) setMessage(`Settings saved, but detector reload failed: ${settings.reload_error || settings.last_detector_error}`);
  else setMessage(settings.last_detector_error ? `Detector warning: ${settings.last_detector_error}` : 'AI detector is ready.');
}

function renderRules(rules) {
  rulesEl.innerHTML = rules.length ? rules.map((rule) => `
    <div class="item">
      <div class="item-title"><span>${escapeHtml(rule.name)}</span><span>${rule.enabled ? 'Enabled' : 'Disabled'}</span></div>
      <p>${escapeHtml(rule.object)} · ${Math.round(rule.min_confidence * 100)}% · cooldown ${rule.cooldown_seconds}s</p>
      <p class="muted">Window: ${rule.active_start || 'any'} - ${rule.active_end || 'any'}</p>
      <button class="secondary" data-action="edit" data-rule='${JSON.stringify(rule).replace(/'/g, '&#39;')}'>Edit</button>
      <button class="secondary" data-action="toggle" data-id="${rule.id}" data-enabled="${rule.enabled}">${rule.enabled ? 'Disable' : 'Enable'}</button>
      <button class="secondary" data-action="delete" data-id="${rule.id}">Delete</button>
    </div>
  `).join('') : '<div class="empty">No alert rules configured.</div>';
}

async function loadAll() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  renderAi(await api('/api/settings/ai'));
  const alerts = await api('/api/settings/alerts');
  labelOptions.innerHTML = alerts.available_labels.map((label) => `<option value="${escapeHtml(label)}"></option>`).join('');
  renderRules(alerts.rules);
}

async function runAction(buttonId, path, label) {
  const button = document.getElementById(buttonId);
  button.disabled = true;
  setMessage(`${label}...`);
  try {
    const result = await api(path, { method: 'POST' });
    renderAi(result.status || result);
    setMessage(result.message || `${label} complete.`);
  } catch (error) {
    setMessage(error.message);
    renderAi(await api('/api/settings/ai'));
  } finally {
    button.disabled = false;
  }
}

aiForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderAi(await api('/api/settings/ai', { method: 'PUT', body: JSON.stringify(formPayload(aiForm)) }));
  } catch (error) { setMessage(error.message); }
});

document.getElementById('checkModelBtn').addEventListener('click', () => runAction('checkModelBtn', '/api/settings/ai/check-model', 'Checking model'));
document.getElementById('downloadModelBtn').addEventListener('click', () => runAction('downloadModelBtn', '/api/settings/ai/download-yolov8n', 'Downloading YOLOv8n ONNX'));
document.getElementById('reloadDetectorBtn').addEventListener('click', () => runAction('reloadDetectorBtn', '/api/settings/ai/reload', 'Reloading detector'));
document.getElementById('testDetectorBtn').addEventListener('click', () => runAction('testDetectorBtn', '/api/settings/ai/test-detector', 'Testing detector'));

ruleForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = formPayload(ruleForm);
  const id = payload.id;
  delete payload.id;
  try {
    await api(id ? `/api/settings/alerts/${id}` : '/api/settings/alerts', { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) });
    ruleForm.reset();
    setMessage('Alert rule saved.');
    await loadAll();
  } catch (error) { setMessage(error.message); }
});

document.getElementById('cancelEditRule').addEventListener('click', () => ruleForm.reset());

rulesEl.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action === 'edit') {
    const rule = JSON.parse(button.dataset.rule);
    for (const [key, value] of Object.entries(rule)) if (ruleForm.elements[key]) ruleForm.elements[key].value = String(value ?? '');
  }
  if (button.dataset.action === 'toggle') {
    await api(`/api/settings/alerts/${button.dataset.id}`, { method: 'PUT', body: JSON.stringify({ enabled: button.dataset.enabled !== 'true' }) });
    setMessage('Alert rule updated.');
    await loadAll();
  }
  if (button.dataset.action === 'delete') {
    await api(`/api/settings/alerts/${button.dataset.id}`, { method: 'DELETE' });
    setMessage('Alert rule deleted.');
    await loadAll();
  }
});

loadAll().catch((error) => setMessage(error.message));
