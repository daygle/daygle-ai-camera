// face-recognition.js - Face Recognition settings page (face-recognition.html).
// Admin-only. Uses api() / escapeHtml / safeHtml / showToast from utils.js.

const frForm = document.getElementById('frForm');
const frStatusPanel = document.getElementById('frStatusPanel');
const frMessage = document.getElementById('frMessage');
const frSaveBtn = document.getElementById('frSaveBtn');
const frReloadBtn = document.getElementById('frReloadBtn');
const frModelList = document.getElementById('frModelList');
const frModelsMessage = document.getElementById('frModelsMessage');

function yesNo(value) {
  return value ? 'Yes' : 'No';
}

// Track per-card message timeouts so rapid actions don't clear new messages.
const modelMessageTimeouts = {};

/**
 * Show a status message inside a specific model card.
 * @param {string} modelId - The model ID (e.g. 'arcface-r100')
 * @param {string} text - Message to display (empty hides it)
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

function renderStatus(status) {
  const modelName = status.model_path ? status.model_path.split('/').pop() : '(none)';
  const rows = [
    ['Enabled', yesNo(status.enabled)],
    ['Model Loaded', yesNo(status.model_loaded)],
    ['Active Model', modelName],
    ['Embedding Size', status.embedding_dim ? String(status.embedding_dim) : '-'],
    ['Enrolled People', String(status.enrolled_people ?? 0)],
    ['Enrolled Faces', String(status.enrolled_faces ?? 0)],
  ];
  let html = rows.map(([k, v]) => safeHtml`<div><span>${k}</span><strong>${v}</strong></div>`).join('');
  if (status.enabled && !status.model_loaded && status.unavailable_reason) {
    html += safeHtml`<div><span>Reason</span><strong>${status.unavailable_reason}</strong></div>`;
  }
  frStatusPanel.innerHTML = html;
}

function fillForm(status) {
  frForm.enabled.value = status.enabled ? 'true' : 'false';
  frForm.alert_unknown.value = status.alert_unknown ? 'true' : 'false';
  frForm.match_threshold.value = status.match_threshold ?? 0.5;
  frForm.min_face_pixels.value = status.min_face_pixels ?? 0;
  frForm.retention_days.value = status.retention_days ?? 0;
  // Managed by the Models tab (download), not the form UI - carry the current
  // values through so a save never wipes the active embedding model (the
  // backend treats a missing model_path as "no model selected").
  frForm.model_path.value = status.model_path ?? '';
  frForm.model_id.value = status.model_id ?? 'arcface';
}

async function loadStatus() {
  try {
    const status = await api('/api/settings/face-recognition');
    renderStatus(status);
    fillForm(status);
  } catch (err) {
    frMessage.textContent = err.message || 'Failed to load settings.';
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const body = {
    enabled: frForm.enabled.value === 'true',
    alert_unknown: frForm.alert_unknown.value === 'true',
    match_threshold: parseFloat(frForm.match_threshold.value),
    min_face_pixels: parseInt(frForm.min_face_pixels.value || '0', 10),
    retention_days: parseInt(frForm.retention_days.value || '0', 10),
    model_path: frForm.model_path.value,
    model_id: frForm.model_id.value,
  };
  frSaveBtn.disabled = true;
  try {
    const status = await api('/api/settings/face-recognition', { method: 'PUT', body: JSON.stringify(body) });
    renderStatus(status);
    fillForm(status);
    showToast('Face recognition settings saved.');
    if (status.enabled && status.reload_error) {
      showToast(status.reload_error, true);
    }
  } catch (err) {
    showToast(err.message || 'Failed to save settings.', true);
  } finally {
    frSaveBtn.disabled = false;
  }
}

async function reloadService() {
  frReloadBtn.disabled = true;
  try {
    const status = await api('/api/settings/face-recognition/reload', { method: 'POST' });
    renderStatus(status);
    showToast('Recognition service reloaded.');
  } catch (err) {
    showToast(err.message || 'Reload failed.', true);
  } finally {
    frReloadBtn.disabled = false;
  }
}

// Mirrors the ONNX models page card format (app/web/onnx.js): an active/
// installed/available card state, a status badge, a size indicator, and a
// Download / Use / Update / Delete action set. NOTE: compose with a plain
// template + escapeHtml on the leaf values -- do NOT build sub-fragments with
// safeHtml and interpolate them into another safeHtml`` (safeHtml escapes every
// interpolation, so nested HTML would render as visible markup).
function renderModels(models) {
  if (!models.length) {
    frModelList.innerHTML = '';
    frModelsMessage.textContent = 'No embedding models available.';
    return;
  }
  frModelsMessage.textContent = '';
  const maxMb = Math.max(1, ...models.map((m) => m.approx_mb || 0));
  frModelList.innerHTML = models.map((model) => {
    const id = escapeHtml(model.id);
    const isInstalled = !!model.installed;
    const isActive = !!model.active;

    let cardClass = 'model-card';
    if (isActive) cardClass += ' model-card-active';
    else if (isInstalled) cardClass += ' model-card-installed';
    else cardClass += ' model-card-available';

    let statusHtml = '';
    if (isActive) statusHtml = '<span class="model-status model-status-active">● Active</span>';
    else if (isInstalled) statusHtml = '<span class="model-status model-status-installed">○ Installed</span>';

    const sizeMb = `~${escapeHtml(String(model.approx_mb))} MB`;
    const barWidth = Math.min(100, Math.round(((model.approx_mb || 0) / maxMb) * 100));

    const updateBtn = `<button class="btn-warning model-action-btn" data-action="update" data-model-id="${id}" title="Re-download this model's file (repair / refresh)">↻ Update</button>`;
    let actionsHtml;
    if (!isInstalled) {
      actionsHtml = `<button class="btn-info model-action-btn" data-action="download" data-model-id="${id}">⬇ Download (~${escapeHtml(String(model.approx_mb))} MB)</button>`;
    } else if (isActive) {
      // The active model can't be deleted (recognition points at it); offer a
      // refresh only, matching the ONNX page's "In Use" state.
      actionsHtml = `<button class="btn-success model-action-btn" disabled>✓ In Use</button>${updateBtn}`;
    } else {
      actionsHtml = `
        <button class="btn-success model-action-btn" data-action="select" data-model-id="${id}">▶ Use</button>
        ${updateBtn}
        <button class="btn-danger model-action-btn" data-action="delete" data-model-id="${id}">✕ Delete</button>`;
    }

    return `
      <div class="${cardClass}" id="model-card-${id}">
        <div class="model-card-header">
          <div class="model-card-title">
            <h3>${escapeHtml(model.label)}</h3>
            <div class="model-card-meta">${statusHtml}</div>
          </div>
          <div class="model-card-size">
            <span class="model-size-value">${sizeMb}</span>
            <div class="model-size-bar"><div class="model-size-fill" style="width:${barWidth}%"></div></div>
          </div>
        </div>
        <p class="model-card-desc">${escapeHtml(model.description)}</p>
        <p class="muted">License: ${escapeHtml(model.license)} · ${escapeHtml(String(model.dim))}-d</p>
        <div class="model-card-message model-card-message-hidden"></div>
        <div class="model-card-actions">${actionsHtml}</div>
      </div>`;
  }).join('');
}

// Every mutating endpoint returns the combined status + models payload, so a
// single response refreshes the status header, the settings form, and the cards.
function applyModelsPayload(payload) {
  renderStatus(payload);
  fillForm(payload);
  renderModels(payload.models || []);
  if (payload.enabled && payload.reload_error) {
    showToast(payload.reload_error, true);
  }
}

async function loadModels() {
  try {
    const body = await api('/api/settings/face-recognition/embedding-models');
    renderModels(body.models || []);
  } catch (err) {
    frModelsMessage.textContent = err.message || 'Failed to load models.';
  }
}

const MODEL_ACTIONS = {
  download: {
    method: 'POST',
    path: (id) => `/api/settings/face-recognition/embedding-models/${encodeURIComponent(id)}/download`,
    progress: () => 'Downloading… this may take several minutes.',
    done: 'Embedding model downloaded and selected.',
    fail: 'Model download failed.',
  },
  select: {
    method: 'POST',
    path: (id) => `/api/settings/face-recognition/embedding-models/${encodeURIComponent(id)}/select`,
    progress: () => 'Switching to this model…',
    done: 'This model is now selected.',
    fail: 'Could not switch to this model.',
  },
  update: {
    method: 'POST',
    path: (id) => `/api/settings/face-recognition/embedding-models/${encodeURIComponent(id)}/update`,
    progress: () => 'Re-downloading model file…',
    done: 'Model file refreshed.',
    fail: 'Model update failed.',
  },
  delete: {
    method: 'DELETE',
    path: (id) => `/api/settings/face-recognition/embedding-models/${encodeURIComponent(id)}`,
    progress: () => 'Deleting model file…',
    done: 'Model deleted.',
    fail: 'Could not delete the model.',
    confirm: 'Delete this downloaded model file? You can download it again later.',
  },
};

async function runModelAction(action, modelId, button) {
  const spec = MODEL_ACTIONS[action];
  if (!spec) return;
  if (spec.confirm && !window.confirm(spec.confirm)) return;
  const original = button.textContent;
  button.disabled = true;
  button.classList.add('model-action-loading');
  setModelMessage(modelId, spec.progress(modelId), 'loading');
  try {
    const payload = await api(spec.path(modelId), { method: spec.method });
    applyModelsPayload(payload);
    // renderModels() just replaced the card DOM; set the message on the fresh
    // card and let it fade out on its own.
    setModelMessage(modelId, spec.done, 'success');
    setTimeout(() => setModelMessage(modelId, '', 'info'), 5000);
  } catch (err) {
    setModelMessage(modelId, err.message || spec.fail, 'error');
    button.disabled = false;
    button.classList.remove('model-action-loading');
    button.textContent = original;
  }
}

frModelList.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  runModelAction(button.dataset.action, button.dataset.modelId, button);
});

frForm.addEventListener('submit', saveSettings);
frReloadBtn.addEventListener('click', reloadService);

// Group the Face Recognition cards into Status / Models / Settings tabs.
// Shared implementation (ARIA tabs + URL-hash deep-linking) lives in utils.js.
initDaygleTabs();

loadStatus();
loadModels();
