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

function renderModels(models) {
  if (!models.length) {
    frModelList.innerHTML = '';
    frModelsMessage.textContent = 'No embedding models available.';
    return;
  }
  frModelsMessage.textContent = '';
  // NOTE: compose with a plain template + escapeHtml on the leaf values. Do NOT
  // build sub-fragments with safeHtml and interpolate them into another
  // safeHtml`` -- safeHtml escapes every interpolation, so a nested HTML string
  // would render as visible markup instead of a live element.
  frModelList.innerHTML = models.map((model) => {
    const installed = model.installed
      ? '<span class="model-status model-status-installed">○ Installed</span>'
      : '';
    const button = model.installed
      ? ''
      : `<button class="btn-info model-action-btn" data-action="download" data-model-id="${escapeHtml(model.id)}">⬇ Download (~${escapeHtml(String(model.approx_mb))} MB)</button>`;
    return `
      <div class="model-card">
        <div class="model-card-head">
          <strong>${escapeHtml(model.label)}</strong>
        </div>
        <p class="muted">${escapeHtml(model.description)}</p>
        <p class="muted">License: ${escapeHtml(model.license)} · ${escapeHtml(String(model.dim))}-d</p>
        <div class="button-row">${button}</div>
        <div class="model-status-slot">${installed}</div>
      </div>`;
  }).join('');
}

async function loadModels() {
  try {
    const body = await api('/api/settings/face-recognition/embedding-models');
    renderModels(body.models || []);
  } catch (err) {
    frModelsMessage.textContent = err.message || 'Failed to load models.';
  }
}

async function downloadModel(modelId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Downloading…';
  try {
    const status = await api(`/api/settings/face-recognition/embedding-models/${encodeURIComponent(modelId)}/download`, { method: 'POST' });
    renderStatus(status);
    fillForm(status);
    showToast('Embedding model downloaded and selected.');
    await loadModels();
  } catch (err) {
    showToast(err.message || 'Model download failed.', true);
    button.disabled = false;
    button.textContent = original;
  }
}

frModelList.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-action="download"]');
  if (button) {
    downloadModel(button.dataset.modelId, button);
  }
});

frForm.addEventListener('submit', saveSettings);
frReloadBtn.addEventListener('click', reloadService);

loadStatus();
loadModels();
