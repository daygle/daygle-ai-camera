const aiForm = document.getElementById('aiSettingsForm');
const messageEl = document.getElementById('settingsMessage');
const statusPanel = document.getElementById('aiStatusPanel');
const modelList = document.getElementById('modelList');
const modelUpdatesMessage = document.getElementById('modelUpdatesMessage');
let modelUpdateMap = {};
// Track per-card message timeouts so rapid actions don't clear new messages
const modelMessageTimeouts = {};

// api() is provided by web/utils.js (loaded before this script). 401 still
// throws (after redirecting to /login); reload failures on PUT
// /api/settings/ai flow through the SUCCESS branch as a 200 payload with
// `reload_succeeded: false` / `last_detector_error` rendered by renderAi()
// below, so this page's error messages come from the JSON shape rather than
// the catch handler. No local api() needed.

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
  if (text) window.showToast(text, isError);
}

/**
 * Show a status message inside a specific model card.
 * @param {string} modelId - The model ID (e.g. 'yolo26n')
 * @param {string} text - Message to display
 * @param {string} type - 'loading' | 'success' | 'error' | 'info'
 */
function setModelMessage(modelId, text, type = 'info') {
  // Clear any pending timeout so rapid actions don't clear new messages
  if (modelMessageTimeouts[modelId]) {
    clearTimeout(modelMessageTimeouts[modelId]);
    delete modelMessageTimeouts[modelId];
  }
  const card = document.getElementById(`model-card-${modelId}`);
  if (!card) return;
  let msgEl = card.querySelector('.model-card-message');
  if (!msgEl) {
    msgEl = document.createElement('div');
    msgEl.className = 'model-card-message';
    const actionsEl = card.querySelector('.model-card-actions');
    if (actionsEl) {
      actionsEl.parentNode.insertBefore(msgEl, actionsEl);
    } else {
      card.appendChild(msgEl);
    }
  }
  msgEl.textContent = text;
  msgEl.className = `model-card-message model-card-message-${type}`;
  if (!text) {
    msgEl.classList.add('model-card-message-hidden');
  }
}

function formPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  data.enabled = data.enabled === 'true';
  for (const key of ['iou_threshold']) if (data[key] !== '') data[key] = Number(data[key]);
  for (const key of ['inference_threads', 'max_concurrent_inferences']) {
    if (data[key] !== '') data[key] = Number.parseInt(data[key], 10);
    else delete data[key];
  }
  data.gpu_mem_limit = data.gpu_mem_limit_gb !== ''
    ? Math.round(parseFloat(data.gpu_mem_limit_gb) * 1024 * 1024 * 1024)
    : 0;
  delete data.gpu_mem_limit_gb;
  return data;
}

function renderStatus(status) {
  // ``Model`` is the only row whose value can be HTML markup (the
  // ``status.model_name`` branch embeds a ``<span class="muted">`` for the
  // secondary path text). Building the row with ``safeHtml`` keeps that
  // literal span intact while still passing ``status.model_name`` /
  // ``status.model_path`` through ``escapeHtml``; the no-name branch falls
  // through to a plain-text row.
  const modelRow = status.model_name
    ? safeHtml`<div><span>Model</span><strong>${status.model_name} <span class="muted" style="font-weight:400;font-size:12px">${status.model_path || ''}</span></strong></div>`
    : safeHtml`<div><span>Model</span><strong>${status.model_path || 'Not Set'}</strong></div>`;
  const rows = [
    safeHtml`<div><span>Current Backend</span><strong>${displayValue(status.current_backend || status.configured_backend, 'Not Set')}</strong></div>`,
    modelRow,
    safeHtml`<div><span>Labels Path</span><strong>${status.labels_path || 'Not Set'}</strong></div>`,
    safeHtml`<div><span>Model exists</span><strong>${yesNo(status.model_exists)}</strong></div>`,
    safeHtml`<div><span>ONNX Runtime Installed</span><strong>${yesNo(status.onnx_runtime_installed)}</strong></div>`,
    safeHtml`<div><span>Detector Loaded</span><strong>${yesNo(status.detector_loaded)}</strong></div>`,
    safeHtml`<div><span>Active Config Source</span><strong>${displayValue(status.active_config_source, 'None')}</strong></div>`,
    safeHtml`<div><span>Mode</span><strong class="ai-mode ${String(status.mode || '').toLowerCase().replace(/\s+/g, '-')}">${displayValue(status.mode, 'None')}</strong></div>`,
    safeHtml`<div class="wide"><span>Last Detector Error</span><strong>${displayValue(status.last_detector_error, 'None')}</strong></div>`,
  ];
  statusPanel.innerHTML = rows.join('');
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
  if (aiForm.elements['gpu_mem_limit_gb']) {
    const limitBytes = settings.gpu_mem_limit;
    aiForm.elements['gpu_mem_limit_gb'].value = (limitBytes != null && limitBytes > 0)
      ? (limitBytes / (1024 * 1024 * 1024)).toFixed(1)
      : '0';
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
    const isInstalled = m.installed;
    const isActive = m.active;
    const versionLabel = m.installed_version ? `v${escapeHtml(m.installed_version)}` : '';

    // Determine card state class
    let cardClass = 'model-card';
    if (isActive) cardClass += ' model-card-active';
    else if (isInstalled) cardClass += ' model-card-installed';
    else cardClass += ' model-card-available';

    // Status indicator
    let statusHtml = '';
    if (isActive) {
      statusHtml = '<span class="model-status model-status-active">\u25CF Active</span>';
    } else if (isInstalled) {
      statusHtml = '<span class="model-status model-status-installed">\u25CB Installed</span>';
    }

    // Update badge
    let updateBadge = '';
    if (hasUpdate) {
      updateBadge = '<span class="model-badge model-badge-update">Update Available</span>';
    }

    // Resolution badge for installed models
    let resBadge = '';
    if (m.installed && m.exported_imgsz) {
      resBadge = `<span class="model-badge model-badge-res">${m.exported_imgsz}\u00d7${m.exported_imgsz}</span>`;
    }

    // Size bar (visual weight indicator)
    const maxMb = 131;
    const barWidth = Math.min(100, Math.round((m.approx_mb / maxMb) * 100));

    // Action buttons
    let actionsHtml = '';
    if (!isInstalled) {
      actionsHtml = `
        <div class="model-download-row">
          <select class="model-res-select" data-model-id="${escapeHtml(m.id)}" title="Model export resolution — this determines the input size the model accepts. Higher = more accurate on small objects, slower inference.">
            <option value="320">320 (Fast)</option>
            <option value="416">416</option>
            <option value="480">480</option>
            <option value="512">512</option>
            <option value="640" selected>640 (Default)</option>
            <option value="768">768</option>
            <option value="800">800</option>
            <option value="896">896</option>
            <option value="1024">1024 (Accurate)</option>
            <option value="1280">1280 (Max)</option>
          </select>
          <button class="btn-info model-action-btn" data-action="download" data-model-id="${escapeHtml(m.id)}">\u2B07 Download</button>
        </div>`;
    } else if (isActive && hasUpdate) {
      // Active model with update: allow re-export in place
      actionsHtml = `
        <span class="model-active-label">\u25CF In Use</span>
        <button class="btn-warning model-action-btn" data-action="update" data-model-id="${escapeHtml(m.id)}">\u21BB Update</button>`;
    } else if (isActive) {
      actionsHtml = '<button class="btn-success model-action-btn" disabled>\u2713 In Use</button>';
    } else {
      const updateBtn = hasUpdate
        ? `<button class="btn-warning model-action-btn" data-action="update" data-model-id="${escapeHtml(m.id)}">\u21BB Update</button>`
        : '';
      actionsHtml = `
        <button class="btn-success model-action-btn" data-action="use" data-model-id="${escapeHtml(m.id)}" data-model-path="${escapeHtml(m.path)}">\u25B6 Use</button>
        ${updateBtn}
        <button class="btn-danger model-action-btn" data-action="delete" data-model-id="${escapeHtml(m.id)}">\u2715 Delete</button>`;
    }

    return `
      <div class="${cardClass}" id="model-card-${escapeHtml(m.id)}">
        <div class="model-card-header">
          <div class="model-card-title">
            <h3>${escapeHtml(m.label)}</h3>
            <div class="model-card-meta">
              ${statusHtml}${updateBadge}${resBadge}${versionLabel ? `<span class="model-version">${versionLabel}</span>` : ''}
            </div>
          </div>
          <div class="model-card-size">
            <span class="model-size-value">${sizeMb}</span>
            <div class="model-size-bar"><div class="model-size-fill" style="width:${barWidth}%"></div></div>
          </div>
        </div>
        <p class="model-card-desc">${escapeHtml(m.description)}</p>
        <div class="model-card-message model-card-message-hidden"></div>
        <div class="model-card-actions">${actionsHtml}</div>
      </div>`;
  }).join('');

  // Unified action handler for all model card buttons
  modelList.querySelectorAll('.model-action-btn[data-action]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const action = btn.dataset.action;
      const modelId = btn.dataset.modelId;
      const modelPath = btn.dataset.modelPath;
      const originalText = btn.textContent;

      if (action === 'delete') {
        if (!confirm(`Delete ${modelId}? This cannot be undone.`)) return;
      }

      btn.disabled = true;
      btn.classList.add('model-action-loading');

      // Show progress message inside the model card
      const loadingMessages = {
        download: `Downloading ${modelId}\u2026 this may take several minutes.`,
        use: `Switching to ${modelId}\u2026`,
        update: `Updating ${modelId}\u2026 this may take several minutes.`,
        delete: `Deleting ${modelId}\u2026`,
      };
      setModelMessage(modelId, loadingMessages[action] || '', 'loading');

      try {
        let result;
        if (action === 'download') {
          btn.textContent = 'Downloading\u2026';
          // Read selected resolution from the dropdown next to the button
          const resSelect = document.querySelector(`.model-res-select[data-model-id="${modelId}"]`);
          const imgsz = resSelect ? parseInt(resSelect.value, 10) : 640;
          result = await api('/api/settings/ai/download-model', { method: 'POST', body: JSON.stringify({ model: modelId, imgsz }) });
        } else if (action === 'use') {
          btn.textContent = 'Switching\u2026';
          const current = await api('/api/settings/ai');
          result = await api('/api/settings/ai', { method: 'PUT', body: JSON.stringify({ ...current, model_path: modelPath }) });
        } else if (action === 'update') {
          btn.textContent = 'Updating\u2026';
          result = await api('/api/settings/ai/update-model', { method: 'POST', body: JSON.stringify({ model: modelId }) });
          delete modelUpdateMap[modelId];
        } else if (action === 'delete') {
          btn.textContent = 'Deleting\u2026';
          result = await api(`/api/settings/ai/models/${encodeURIComponent(modelId)}`, { method: 'DELETE' });
        }

        if (action === 'use') {
          renderAi(result);
        } else {
          renderAi(result.status || result);
        }
        // Show success inside the model card (no toast — feedback is local)
        const successMessages = {
          download: `${modelId} installed successfully.`,
          use: `Switched to ${modelId}.`,
          update: `${modelId} updated successfully.`,
          delete: `${modelId} deleted.`,
        };
        setModelMessage(modelId, result.message || successMessages[action] || `${modelId} ${action}d.`, 'success');
        // Clear message after 5 seconds
        setTimeout(() => setModelMessage(modelId, '', 'info'), 5000);
        await loadModels();
      } catch (error) {
        if (window.daygleAuth?.redirecting) return;
        setModelMessage(modelId, error.message, 'error');
        btn.disabled = false;
        btn.classList.remove('model-action-loading');
        btn.textContent = originalText;
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
  btn.textContent = 'Checking\u2026';
  modelUpdatesMessage.textContent = '';
  try {
    const result = await api('/api/settings/ai/check-model-updates');
    modelUpdateMap = {};
    for (const m of result.models || []) modelUpdateMap[m.id] = m;
    let msg;
    let isError = false;
    if (result.error) {
      msg = `Update check failed: ${result.error}`;
      isError = true;
    } else if (result.any_updates) {
      msg = 'Updates are available for one or more installed models.';
    } else if ((result.models || []).length === 0) {
      msg = 'No models installed yet.';
    } else {
      msg = 'All installed models are up to date.';
    }
    modelUpdatesMessage.textContent = msg;
    window.showToast(msg, isError);
    await loadModels();
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    const msg = `Update check failed: ${error.message}`;
    modelUpdatesMessage.textContent = msg;
    window.showToast(msg, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check for Updates';
  }
}

async function loadAll() {
  // nav.js's daygleAuthReady IIFE has already populated window.daygleAuth.{user, csrfToken}.
  await window.daygleAuthReady;
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
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
    try { renderAi(await api('/api/settings/ai')); } catch { /* ignore refresh errors */ }
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
    const result = await api('/api/settings/ai', { method: 'PUT', body: JSON.stringify(formPayload(aiForm)) });
    renderAi(result);
    if (result.reload_succeeded !== false) {
      setMessage(result.last_detector_error
        ? `Settings saved. Detector warning: ${result.last_detector_error}`
        : 'Settings saved.');
    }
    await loadModels();
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
});

document.getElementById('checkModelBtn').addEventListener('click', () => runAction('checkModelBtn', '/api/settings/ai/check-model', 'Checking model'));
document.getElementById('reloadDetectorBtn').addEventListener('click', () => runAction('reloadDetectorBtn', '/api/settings/ai/reload', 'Reloading detector'));
document.getElementById('testDetectorBtn').addEventListener('click', () => runAction('testDetectorBtn', '/api/settings/ai/test-detector', 'Testing detector'));
document.getElementById('checkModelUpdatesBtn').addEventListener('click', checkForModelUpdates);

loadAll().catch((error) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(error.message, true);
});
