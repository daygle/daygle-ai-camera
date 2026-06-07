let csrfToken = null;
const aiForm = document.getElementById('aiSettingsForm');
const emailForm = document.getElementById('emailSettingsForm');
const messageEl = document.getElementById('settingsMessage');
const statusPanel = document.getElementById('aiStatusPanel');
const testEmailRecipient = document.getElementById('testEmailRecipient');
const testEmailBtn = document.getElementById('testEmailBtn');

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
  data.enabled = data.enabled === 'true';
  for (const key of ['confidence', 'iou_threshold']) if (data[key] !== '') data[key] = Number(data[key]);
  if (data.input_size !== '') data.input_size = Number.parseInt(data.input_size, 10);
  return data;
}

function emailPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['enabled', 'use_tls', 'use_ssl']) if (key in data) data[key] = data[key] === 'true';
  if (data.port !== '') data.port = Number.parseInt(data.port, 10);
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

function renderEmail(settings) {
  if (!emailForm) return;
  for (const [key, value] of Object.entries(settings)) {
    if (emailForm.elements[key]) emailForm.elements[key].value = String(value ?? '');
  }
  if (!emailForm.elements.port.value) emailForm.elements.port.value = '587';
  if (testEmailRecipient && !testEmailRecipient.value) testEmailRecipient.value = settings.from_address || '';
}

async function loadAll() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const [aiSettings, emailSettings] = await Promise.all([
    api('/api/settings/ai'),
    emailForm ? api('/api/settings/alert-email') : Promise.resolve(null),
  ]);
  renderAi(aiSettings);
  if (emailSettings) renderEmail(emailSettings);
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

emailForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderEmail(await api('/api/settings/alert-email', { method: 'PUT', body: JSON.stringify(emailPayload(emailForm)) }));
    setMessage('Mail server settings saved.');
  } catch (error) {
    setMessage(error.message);
  }
});

testEmailBtn?.addEventListener('click', async () => {
  const recipient = testEmailRecipient.value.trim() || emailForm.elements.from_address.value.trim();
  if (!recipient) {
    setMessage('Enter a test recipient email address.');
    return;
  }
  testEmailBtn.disabled = true;
  setMessage('Sending test email...');
  try {
    await api('/api/settings/alert-email/test', {
      method: 'POST',
      body: JSON.stringify({ settings: emailPayload(emailForm), recipient }),
    });
    setMessage(`Test email sent to ${recipient}.`);
  } catch (error) {
    setMessage(error.message);
  } finally {
    testEmailBtn.disabled = false;
  }
});

document.getElementById('checkModelBtn').addEventListener('click', () => runAction('checkModelBtn', '/api/settings/ai/check-model', 'Checking model'));
document.getElementById('downloadModelBtn').addEventListener('click', () => runAction('downloadModelBtn', '/api/settings/ai/download-yolov8n', 'Downloading YOLOv8n ONNX'));
document.getElementById('reloadDetectorBtn').addEventListener('click', () => runAction('reloadDetectorBtn', '/api/settings/ai/reload', 'Reloading detector'));
document.getElementById('testDetectorBtn').addEventListener('click', () => runAction('testDetectorBtn', '/api/settings/ai/test-detector', 'Testing detector'));

loadAll().catch((error) => setMessage(error.message));
