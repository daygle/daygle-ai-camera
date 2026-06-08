let csrfToken = null;
const aiForm = document.getElementById('aiSettingsForm');
const messageEl = document.getElementById('settingsMessage');
const statusPanel = document.getElementById('aiStatusPanel');
const modelList = document.getElementById('modelList');

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
function setMessage(text, isError = false) {
  messageEl.textContent = text;
  if (text) window.showToast?.(text, isError);
}

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
  else messageEl.textContent = settings.last_detector_error ? `Detector warning: ${settings.last_detector_error}` : '';
}

function renderModelList(models) {
  if (!models.length) {
    modelList.innerHTML = '<p class="muted">No models available.</p>';
    return;
  }
  modelList.innerHTML = models.map((m) => {
    const sizeMb = m.size_bytes ? `${(m.size_bytes / 1048576).toFixed(0)} MB` : `~${m.approx_mb} MB`;
    const badge = m.active
      ? '<span class="badge badge-active">Active</span>'
      : m.installed ? '<span class="badge badge-installed">Installed</span>' : '';
    const action = m.active
      ? '<button class="secondary" disabled>In Use</button>'
      : m.installed
        ? `<button class="secondary model-use-btn" data-model-id="${escapeHtml(m.id)}" data-model-path="${escapeHtml(m.path)}">Use</button>`
        : `<button class="model-download-btn" data-model-id="${escapeHtml(m.id)}">Download &amp; Install</button>`;
    return `
      <div class="model-row" id="model-row-${escapeHtml(m.id)}">
        <div class="model-row-info">
          <strong>${escapeHtml(m.label)}</strong>${badge}
          <span class="muted">${escapeHtml(m.description)}</span>
        </div>
        <div class="model-row-meta"><span class="muted">${sizeMb}</span></div>
        <div class="model-row-action">${action}</div>
      </div>`;
  }).join('');

  modelList.querySelectorAll('.model-download-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const modelId = btn.dataset.modelId;
      btn.disabled = true;
      btn.textContent = 'Downloading…';
      setMessage(`Downloading ${modelId}… this may take several minutes.`);
      try {
        const result = await api('/api/settings/ai/download-model', { method: 'POST', body: JSON.stringify({ model: modelId }) });
        renderAi(result.status || result);
        setMessage(result.message || `${modelId} installed.`);
        await loadModels();
      } catch (error) {
        setMessage(error.message, true);
        btn.disabled = false;
        btn.textContent = 'Download & Install';
      }
    });
  });

  modelList.querySelectorAll('.model-use-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const modelId = btn.dataset.modelId;
      const modelPath = btn.dataset.modelPath;
      btn.disabled = true;
      btn.textContent = 'Switching…';
      setMessage(`Switching to ${modelId}…`);
      try {
        const current = await api('/api/settings/ai');
        const result = await api('/api/settings/ai', { method: 'PUT', body: JSON.stringify({ ...current, model_path: modelPath }) });
        renderAi(result);
        setMessage(`Switched to ${modelId}.`);
        await loadModels();
      } catch (error) {
        setMessage(error.message, true);
        btn.disabled = false;
        btn.textContent = 'Use';
      }
    });
  });
}

async function loadModels() {
  try {
    renderModelList(await api('/api/settings/ai/models'));
  } catch {
    modelList.innerHTML = '<p class="muted">Could not load model list.</p>';
  }
}

async function loadAll() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const [aiSettings] = await Promise.all([api('/api/settings/ai'), loadModels()]);
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
    await loadModels();
  } catch (error) {
    setMessage(error.message, true);
    renderAi(await api('/api/settings/ai'));
  } finally {
    button.disabled = false;
  }
}

aiForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    renderAi(await api('/api/settings/ai', { method: 'PUT', body: JSON.stringify(formPayload(aiForm)) }));
    await loadModels();
  } catch (error) { setMessage(error.message, true); }
});

document.getElementById('checkModelBtn').addEventListener('click', () => runAction('checkModelBtn', '/api/settings/ai/check-model', 'Checking model'));
document.getElementById('reloadDetectorBtn').addEventListener('click', () => runAction('reloadDetectorBtn', '/api/settings/ai/reload', 'Reloading detector'));
document.getElementById('testDetectorBtn').addEventListener('click', () => runAction('testDetectorBtn', '/api/settings/ai/test-detector', 'Testing detector'));

loadAll().catch((error) => setMessage(error.message, true));
