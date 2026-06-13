let csrfToken = null;
const aiForm = document.getElementById('aiSettingsForm');
const messageEl = document.getElementById('settingsMessage');
const statusPanel = document.getElementById('aiStatusPanel');
const modelList = document.getElementById('modelList');
const modelUpdatesMessage = document.getElementById('modelUpdatesMessage');
let modelUpdateMap = {};

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
  if (data.gpu_mem_limit_gb !== '') {
    data.gpu_mem_limit = Math.round(parseFloat(data.gpu_mem_limit_gb) * 1024 * 1024 * 1024);
  }
  delete data.gpu_mem_limit_gb;
  return data;
}

function renderStatus(status) {
  const modelDisplay = status.model_name
    ? `${escapeHtml(status.model_name)} <span class="muted" style="font-weight:400;font-size:12px">${escapeHtml(status.model_path || '')}</span>`
    : escapeHtml(status.model_path || 'Not Set');
  statusPanel.innerHTML = `
    <div><span>Current Backend</span><strong>${escapeHtml(displayValue(status.current_backend || status.configured_backend, 'Not Set'))}</strong></div>
    <div><span>Model</span><strong>${modelDisplay}</strong></div>
    <div><span>Labels Path</span><strong>${escapeHtml(status.labels_path || 'Not Set')}</strong></div>
    <div><span>Model exists</span><strong>${yesNo(status.model_exists)}</strong></div>
    <div><span>ONNX Runtime Installed</span><strong>${yesNo(status.onnx_runtime_installed)}</strong></div>
    <div><span>Detector Loaded</span><strong>${yesNo(status.detector_loaded)}</strong></div>
    <div><span>Active Config Source</span><strong>${escapeHtml(displayValue(status.active_config_source, 'None'))}</strong></div>
    <div><span>Mode</span><strong class="ai-mode ${escapeHtml(String(status.mode || '').toLowerCase().replace(/\s+/g, '-'))}">${escapeHtml(displayValue(status.mode, 'None'))}</strong></div>
    <div class="wide"><span>Last Detector Error</span><strong>${escapeHtml(displayValue(status.last_detector_error, 'None'))}</strong></div>
  `;
}

function renderLabels(labels) {
  const el = document.getElementById('labelsList');
  if (!el) return;
  if (!labels || !labels.length) {
    el.innerHTML = '<p class="muted">No labels loaded.</p>';
    return;
  }
  el.innerHTML = labels.map((label) =>
    `<span class="label-tag">${escapeHtml(titleCaseWords(label))}</span>`
  ).join('');
}

function renderAi(settings) {
  for (const [key, value] of Object.entries(settings)) {
    if (aiForm.elements[key]) aiForm.elements[key].value = String(value ?? '');
  }
  if (aiForm.elements['gpu_mem_limit_gb'] && settings.gpu_mem_limit != null) {
    aiForm.elements['gpu_mem_limit_gb'].value = (settings.gpu_mem_limit / (1024 * 1024 * 1024)).toFixed(1);
  }
  renderStatus(settings);
  renderLabels(settings.available_labels);
  if (settings.reload_succeeded === false) setMessage(`Settings saved, but detector reload failed: ${settings.reload_error || settings.last_detector_error}`);
  else messageEl.textContent = settings.last_detector_error ? `Detector warning: ${settings.last_detector_error}` : '';
}

function renderModelList(models) {
  if (!models.length) {
    modelList.innerHTML = '<p class="muted">No models available.</p>';
    return;
  }
  modelList.innerHTML = models.map((m) => {
    const updateInfo = modelUpdateMap[m.id] || {};
    const hasUpdate = updateInfo.update_available === true;
    const sizeMb = m.size_bytes ? `${(m.size_bytes / 1048576).toFixed(0)} MB` : `~${m.approx_mb} MB`;
    const statusBadge = m.active
      ? '<span class="badge badge-active">Active</span>'
      : m.installed ? '<span class="badge badge-installed">Installed</span>' : '';
    const updateBadge = hasUpdate ? '<span class="badge badge-update">Update Available</span>' : '';
    const versionLabel = m.installed_version ? `<span class="muted" style="font-size:11px">v${escapeHtml(m.installed_version)}</span>` : '';
    let action;
    if (!m.installed) {
      action = `<button class="model-download-btn" data-model-id="${escapeHtml(m.id)}">Download &amp; Install</button>`;
    } else if (hasUpdate) {
      const useBtn = m.active ? '' : `<button class="secondary model-use-btn" data-model-id="${escapeHtml(m.id)}" data-model-path="${escapeHtml(m.path)}">Use</button>`;
      action = `${useBtn}<button class="model-update-btn" data-model-id="${escapeHtml(m.id)}">Update</button>`;
    } else if (m.active) {
      action = '<button class="secondary" disabled>In Use</button>';
    } else {
      action = `<button class="secondary model-use-btn" data-model-id="${escapeHtml(m.id)}" data-model-path="${escapeHtml(m.path)}">Use</button>`;
    }
    return `
      <div class="model-row" id="model-row-${escapeHtml(m.id)}">
        <div class="model-row-info">
          <strong>${escapeHtml(m.label)}</strong>${statusBadge}${updateBadge}${versionLabel}
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

  modelList.querySelectorAll('.model-update-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const modelId = btn.dataset.modelId;
      btn.disabled = true;
      btn.textContent = 'Updating…';
      setMessage(`Updating ${modelId}… this may take several minutes.`);
      try {
        const result = await api('/api/settings/ai/update-model', { method: 'POST', body: JSON.stringify({ model: modelId }) });
        renderAi(result.status || result);
        setMessage(result.message || `${modelId} updated.`);
        delete modelUpdateMap[modelId];
        await loadModels();
      } catch (error) {
        setMessage(error.message, true);
        btn.disabled = false;
        btn.textContent = 'Update';
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

async function checkForModelUpdates() {
  const btn = document.getElementById('checkModelUpdatesBtn');
  btn.disabled = true;
  btn.textContent = 'Checking…';
  modelUpdatesMessage.textContent = '';
  try {
    const result = await api('/api/settings/ai/check-model-updates');
    modelUpdateMap = {};
    for (const m of result.models || []) modelUpdateMap[m.id] = m;
    if (result.error) {
      modelUpdatesMessage.textContent = `Update check failed: ${result.error}`;
    } else if (result.any_updates) {
      modelUpdatesMessage.textContent = 'Updates are available for one or more installed models.';
    } else if ((result.models || []).length === 0) {
      modelUpdatesMessage.textContent = 'No models installed yet.';
    } else {
      modelUpdatesMessage.textContent = 'All installed models are up to date.';
    }
    await loadModels();
  } catch (error) {
    modelUpdatesMessage.textContent = `Update check failed: ${error.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check for Updates';
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

document.querySelectorAll('.field-help').forEach((el) => {
  if (!el.title) el.title = el.textContent;
});

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
document.getElementById('checkModelUpdatesBtn').addEventListener('click', checkForModelUpdates);

loadAll().catch((error) => setMessage(error.message, true));
