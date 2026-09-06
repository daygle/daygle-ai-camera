// arcface.js - ArcFace embedding models page (arcface.html).
// Admin-only. Uses api() / escapeHtml / safeHtml / showToast from utils.js.

const arcfaceStatusPanel = document.getElementById('arcfaceStatusPanel');
const arcfaceModelList = document.getElementById('arcfaceModelList');
const arcfaceModelsMessage = document.getElementById('arcfaceModelsMessage');
const arcfaceModelCount = document.getElementById('arcfaceModelCount');
const arcfaceModelsCard = document.getElementById('arcfaceModelsCard');
const arcfaceModelUpdatesMessage = document.getElementById('arcfaceModelUpdatesMessage');
const checkArcfaceModelUpdatesBtn = document.getElementById('checkArcfaceModelUpdatesBtn');

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
  arcfaceStatusPanel.innerHTML = html;
}

async function loadStatus() {
  try {
    const status = await api('/api/settings/face-recognition');
    renderStatus(status);
  } catch (err) {
    arcfaceStatusPanel.innerHTML = safeHtml`<div class="muted">${err.message || 'Failed to load status.'}</div>`;
  }
}

// Mirrors the ONNX models page card format (web/onnx.js): an active/
// installed/available card state, a status badge, license/dimension badges,
// a size indicator, and a Download / Use / Refresh / Delete action set.
function renderModels(models) {
  if (!models.length) {
    arcfaceModelList.innerHTML = '';
    arcfaceModelCount.textContent = '0 installed · 0 available';
    arcfaceModelsMessage.hidden = false;
    arcfaceModelsMessage.textContent = 'No embedding models available.';
    return;
  }
  arcfaceModelsMessage.hidden = true;
  arcfaceModelsCard.hidden = false;
  const installedCount = models.filter((m) => m.installed).length;
  arcfaceModelCount.textContent = `${installedCount} installed · ${models.length} available`;
  const maxMb = Math.max(1, ...models.map((m) => m.approx_mb || 0));
  arcfaceModelList.innerHTML = models.map((model) => {
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

    // Update = re-download the trusted catalog file (repair / refresh).
    // These are fixed pre-built files with no version feed, so unlike the
    // ONNX page there is no "update available" state to discover.
    const refreshBtn = `<button class="btn-warning model-action-btn" data-action="update" data-model-id="${id}" title="Re-download this model's file (repair / refresh)">↻ Refresh</button>`;
    let actionsHtml;
    if (!isInstalled) {
      actionsHtml = `<button class="btn-info model-action-btn" data-action="download" data-model-id="${id}">⬇ Download (~${escapeHtml(String(model.approx_mb))} MB)</button>`;
    } else if (isActive) {
      // The active model can't be deleted (recognition points at it); offer a
      // refresh only, matching the ONNX page's "In Use" state.
      actionsHtml = `<button class="btn-success model-action-btn" disabled>✓ In Use</button>${refreshBtn}`;
    } else {
      actionsHtml = `
        <button class="btn-success model-action-btn" data-action="select" data-model-id="${id}">▶ Use</button>
        ${refreshBtn}
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
        <div class="model-card-meta">
          <span class="arcface-badge" title="Output embedding dimension">${escapeHtml(String(model.dim))}-d embeddings</span>
          <span class="arcface-badge" title="Model input size">${escapeHtml(String(model.input_size))}×${escapeHtml(String(model.input_size))} input</span>
          <span class="model-badge" title="License">⚖ ${escapeHtml(model.license)}</span>
        </div>
        <div class="model-card-message model-card-message-hidden"></div>
        <div class="model-card-actions">${actionsHtml}</div>
      </div>`;
  }).join('');
}

// Every mutating endpoint returns the combined status + models payload, so a
// single response refreshes both the status header and the cards.
function applyModelsPayload(payload) {
  renderStatus(payload);
  renderModels(payload.models || []);
}

async function loadModels() {
  try {
    const body = await api('/api/settings/face-recognition/embedding-models');
    renderModels(body.models || []);
  } catch (err) {
    arcfaceModelsMessage.hidden = false;
    arcfaceModelsMessage.textContent = err.message || 'Failed to load models.';
  }
}

// Parity with the ONNX page's per-tab "Check for updates" affordance. The
// ArcFace catalog ships fixed pre-built files with no version feed, so this
// verifies install/active state against the server and tells the operator
// exactly what a refresh does, instead of pretending to compare versions.
function setUpdateCheckButtonState(isChecking) {
  if (!checkArcfaceModelUpdatesBtn) return;
  checkArcfaceModelUpdatesBtn.disabled = isChecking;
  checkArcfaceModelUpdatesBtn.classList.toggle('is-checking', isChecking);
  const label = checkArcfaceModelUpdatesBtn.querySelector('.model-update-check-label');
  if (label) label.textContent = isChecking ? 'Checking…' : 'Check for updates';
}

async function checkForModelUpdates() {
  setUpdateCheckButtonState(true);
  arcfaceModelUpdatesMessage.textContent = 'Checking the embedding-model catalog…';
  arcfaceModelUpdatesMessage.className = 'model-library-message is-loading';
  try {
    const [body] = await Promise.all([
      api('/api/settings/face-recognition/embedding-models'),
      loadStatus(),
    ]);
    const models = body.models || [];
    applyModelsPayload(body);
    const installed = models.filter((m) => m.installed);
    const active = models.find((m) => m.active);
    let message;
    let isError = false;
    if (!models.length) {
      message = 'The embedding-model catalog is empty.';
      isError = true;
    } else if (!installed.length) {
      message = 'No embedding models installed yet. Download one below to get started.';
    } else if (!active) {
      message = `${installed.length} model${installed.length === 1 ? '' : 's'} installed, but none is selected. Press Use on a card to select it.`;
    } else {
      message = `${installed.length} model${installed.length === 1 ? '' : 's'} installed and ${active.label} is selected. Use ↻ Refresh on a card to re-download a model file if it looks damaged.`;
    }
    arcfaceModelUpdatesMessage.textContent = message;
    arcfaceModelUpdatesMessage.className = `model-library-message ${isError ? 'is-error' : 'is-success'}`;
    window.showToast(message, isError);
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    const message = `Update check failed: ${err.message}`;
    arcfaceModelUpdatesMessage.textContent = message;
    arcfaceModelUpdatesMessage.className = 'model-library-message is-error';
    window.showToast(message, true);
  } finally {
    setUpdateCheckButtonState(false);
  }
}

if (checkArcfaceModelUpdatesBtn) {
  checkArcfaceModelUpdatesBtn.addEventListener('click', checkForModelUpdates);
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

arcfaceModelList.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  runModelAction(button.dataset.action, button.dataset.modelId, button);
});

// Group the ArcFace cards into Status / Models tabs.
// Shared implementation (ARIA tabs + URL-hash deep-linking) lives in utils.js.
initDaygleTabs();

loadStatus();
loadModels();
