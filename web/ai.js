let csrfToken = null;
const aiForm = document.getElementById('aiSettingsForm');
const messageEl = document.getElementById('settingsMessage');
const statusPanel = document.getElementById('aiStatusPanel');

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

function titleCaseWords(value) {
  return String(value || '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter(Boolean)
    .map((word) => {
      const normalized = word.toLowerCase();
      const acronyms = {
        ai: 'AI',
        api: 'API',
        fps: 'FPS',
        id: 'ID',
        iou: 'IoU',
        ocr: 'OCR',
        onnx: 'ONNX',
        onvif: 'ONVIF',
        rtsp: 'RTSP',
        ssl: 'SSL',
        tls: 'TLS',
        url: 'URL',
      };
      if (acronyms[normalized]) return acronyms[normalized];
      return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    })
    .join(' ');
}

function displayValue(value, fallback = 'None') {
  if (value === null || value === undefined || value === '') return fallback;
  return titleCaseWords(String(value));
}

function yesNo(value) { return value ? 'Yes' : 'No'; }
function setMessage(text) { messageEl.textContent = text; }

function formPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  data.enabled = data.enabled === 'true';
  for (const key of ['iou_threshold']) if (data[key] !== '') data[key] = Number(data[key]);
  if (data.input_size !== '') data.input_size = Number.parseInt(data.input_size, 10);
  return data;
}

function renderStatus(status) {
  statusPanel.innerHTML = `
    <div><span>Current Backend</span><strong>${escapeHtml(displayValue(status.current_backend || status.configured_backend, 'Not Set'))}</strong></div>
    <div><span>Model Path</span><strong>${escapeHtml(status.model_path || 'Not Set')}</strong></div>
    <div><span>Labels Path</span><strong>${escapeHtml(status.labels_path || 'Not Set')}</strong></div>
    <div><span>Model exists</span><strong>${yesNo(status.model_exists)}</strong></div>
    <div><span>ONNX Runtime Installed</span><strong>${yesNo(status.onnx_runtime_installed)}</strong></div>
    <div><span>Detector Loaded</span><strong>${yesNo(status.detector_loaded)}</strong></div>
    <div><span>Active Config Source</span><strong>${escapeHtml(displayValue(status.active_config_source, 'None'))}</strong></div>
    <div><span>Mode</span><strong class="ai-mode ${escapeHtml(String(status.mode || '').toLowerCase().replace(/\s+/g, '-'))}">${escapeHtml(displayValue(status.mode, 'None'))}</strong></div>
    <div class="wide"><span>Last Detector Error</span><strong>${escapeHtml(displayValue(status.last_detector_error, 'None'))}</strong></div>
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

async function loadAll() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const aiSettings = await api('/api/settings/ai');
  renderAi(aiSettings);
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

loadAll().catch((error) => setMessage(error.message));
