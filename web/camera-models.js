// Per-camera YOLO model assignment (web/camera-models.html).
//
// The page lists every camera with one model picker. Its first option is
// "Default (...)" - choosing it unassigns the camera - and picking anything
// else assigns that model. A change saves immediately against
// /api/camera-models; assignments are stored in the camera's detection block
// and the live pipeline picks them up on the next detection cycle, so there
// is no reload step here.

const messageEl = document.getElementById('cameraModelsMessage');
const tableBody = document.getElementById('cameraModelsBody');
const defaultModelInfo = document.getElementById('defaultModelInfo');

let assignmentPayload = null;

function fileName(modelPath) {
  return String(modelPath || '').replace(/\\/g, '/').split('/').pop();
}

function modelInstalled(modelPath) {
  return (assignmentPayload?.models || []).some((model) => model.path === modelPath);
}

// The default model by its catalog label (as every other page names it),
// falling back to a camera row that already resolved it, then the file name.
function defaultModelLabel() {
  const modelPath = assignmentPayload?.default_model?.model_path;
  if (!modelPath) return '';
  const model = (assignmentPayload?.models || []).find((m) => m.path === modelPath);
  if (model) return model.label;
  const camera = (assignmentPayload?.cameras || []).find((c) => c.source === 'default' && c.model_name);
  return camera?.model_name || fileName(modelPath);
}

function renderDefaultModel() {
  const modelPath = assignmentPayload?.default_model?.model_path;
  if (!modelPath) {
    defaultModelInfo.innerHTML = '<span>Default model</span><strong>None installed</strong> - install one on Object Models first.';
    return;
  }
  const missing = modelInstalled(modelPath) ? '' : ' <span class="camera-model-status is-missing">⚠ File missing</span>';
  defaultModelInfo.innerHTML = `<span>Default model</span><strong title="${escapeHtml(modelPath)}">${escapeHtml(defaultModelLabel())}</strong>${missing}`;
}

// One picker per camera. The camera's current assignment is always the
// selected option - even when its file is gone, so the row can never look
// like it follows the default when it does not.
function selectOptions(camera) {
  const assigned = camera.source === 'assigned';
  const label = defaultModelLabel();
  let options = `<option value=""${assigned ? '' : ' selected'}>${escapeHtml(label ? `Default (${label})` : 'Default')}</option>`;
  const objectModels = (assignmentPayload?.models || []).filter((model) => model.assignable);
  if (assigned && !objectModels.some((model) => model.path === camera.model_path)) {
    const missingLabel = `${camera.model_name || fileName(camera.model_path)} - file missing`;
    options += `<option value="${escapeHtml(camera.model_path || '')}" selected>${escapeHtml(missingLabel)}</option>`;
  }
  for (const model of objectModels) {
    const selected = assigned && model.path === camera.model_path ? ' selected' : '';
    options += `<option value="${escapeHtml(model.path)}"${selected}>${escapeHtml(model.label)}</option>`;
  }
  return options;
}

function statusPill(camera) {
  if (!camera.model_exists) return '<span class="camera-model-status is-missing">⚠ Missing</span>';
  if (camera.source === 'assigned') return '<span class="camera-model-status is-dedicated">Dedicated</span>';
  return '<span class="camera-model-status is-default">Default</span>';
}

// The id is only worth showing when it is not just the name in lower case.
function cameraIdNote(camera) {
  const slug = String(camera.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return camera.name && slug !== camera.id ? `<span class="muted">${escapeHtml(camera.id)}</span>` : '';
}

function renderCameras() {
  const cameras = assignmentPayload?.cameras || [];
  if (!cameras.length) {
    tableBody.innerHTML = '<tr><td colspan="3" class="muted">No cameras configured yet - add one on the Cameras page first.</td></tr>';
    return;
  }
  tableBody.innerHTML = cameras.map((camera) => `<tr data-camera-row="${escapeHtml(camera.id)}">
      <td data-label="Camera" class="camera-model-camera"><strong>${escapeHtml(camera.name || camera.id)}</strong>${cameraIdNote(camera)}</td>
      <td data-label="Model" class="camera-model-select-cell"><select class="camera-model-select" data-camera="${escapeHtml(camera.id)}" aria-label="Model for ${escapeHtml(camera.name || camera.id)}">${selectOptions(camera)}</select></td>
      <td data-label="Status" class="camera-model-status-cell">${statusPill(camera)}</td>
    </tr>`).join('');
}

function render() {
  renderDefaultModel();
  renderCameras();
}

async function loadAssignments() {
  try {
    assignmentPayload = await api('/api/camera-models');
    messageEl.textContent = '';
    render();
  } catch (err) {
    messageEl.textContent = '';
    showToast(err.message || 'Could not load camera models.', true);
  }
}

function cameraName(cameraId) {
  const camera = (assignmentPayload?.cameras || []).find((c) => c.id === cameraId);
  return camera?.name || cameraId;
}

// Save on change: "Default" unassigns, anything else assigns that model.
async function saveCameraModel(select) {
  const cameraId = select.getAttribute('data-camera') || '';
  const modelPath = String(select.value || '');
  const row = select.closest('tr');
  const statusCell = row?.querySelector('.camera-model-status-cell');
  select.disabled = true;
  if (statusCell) statusCell.innerHTML = '<span class="camera-model-status is-saving">Saving…</span>';
  try {
    if (modelPath) {
      await api(`/api/camera-models/${encodeURIComponent(cameraId)}`, {
        method: 'PUT',
        body: JSON.stringify({ model_path: modelPath }),
      });
      const label = select.selectedOptions[0]?.textContent || modelPath;
      showToast(`${cameraName(cameraId)} now runs ${label}.`);
    } else {
      await api(`/api/camera-models/${encodeURIComponent(cameraId)}`, { method: 'DELETE' });
      showToast(`${cameraName(cameraId)} now follows the default model.`);
    }
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    showToast(err.message || 'Could not save the model for this camera.', true);
  }
  // Re-render from the server either way, so a failed save snaps the picker
  // back to what the camera really runs.
  await loadAssignments();
}

tableBody.addEventListener('change', (event) => {
  const select = event.target instanceof Element ? event.target.closest('select.camera-model-select') : null;
  if (select) saveCameraModel(select);
});

loadAssignments();
