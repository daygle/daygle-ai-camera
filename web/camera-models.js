// Per-camera YOLO model assignment (web/camera-models.html).
//
// The page lists every camera with the object-detection model it currently
// runs (its own assignment or the global default) and offers assign / switch
// / unassign actions against /api/camera-models. Assignments are stored in
// the camera's detection block; the live pipeline picks them up on the next
// detection cycle, so there is no reload step here.

const messageEl = document.getElementById('cameraModelsMessage');
const tableBody = document.getElementById('cameraModelsBody');
const defaultModelInfo = document.getElementById('defaultModelInfo');

let assignmentPayload = null;

function modelInstalled(path) {
  return (assignmentPayload?.models || []).some((model) => model.path === path);
}

function renderDefaultModel() {
  const info = assignmentPayload?.default_model || {};
  if (!info.model_path) {
    defaultModelInfo.textContent = 'No default object model is configured yet - install one on the ONNX page.';
    return;
  }
  const name = String(info.model_path).replace(/\\/g, '/').split('/').pop();
  defaultModelInfo.innerHTML = `<span class="status-badge">${escapeHtml(name)}</span> <code>${escapeHtml(info.model_path)}</code>` +
    (modelInstalled(info.model_path) ? '' : ' <span class="status-badge">file missing</span>');
}

function selectOptions(models, selectedPath) {
  let options = '<option value="">Use the default model</option>';
  for (const model of models) {
    const selected = model.path === selectedPath ? ' selected' : '';
    if (model.assignable) {
      options += `<option value="${escapeHtml(model.path)}"${selected}>${escapeHtml(model.label)}</option>`;
    } else {
      options += `<option value="" disabled>${escapeHtml(model.label)} (face pass)</option>`;
    }
  }
  return options;
}

function renderCameras() {
  const cameras = assignmentPayload?.cameras || [];
  if (!cameras.length) {
    tableBody.innerHTML = '<tr><td colspan="4" class="muted">No cameras configured yet - add one on the Cameras page first.</td></tr>';
    return;
  }
  tableBody.innerHTML = cameras.map((camera) => {
    const assigned = camera.source === 'assigned';
    const currentBadge = assigned
      ? `<span class="status-badge">${escapeHtml(camera.model_name || camera.model_path || '')}</span>`
      : `<span class="status-badge">Default · ${escapeHtml(camera.model_name || 'unknown')}</span>`;
    const missingNote = camera.model_exists ? '' : ' <span class="status-badge">file missing</span>';
    const actions = assigned
      ? `<button type="button" class="btn-info" data-action="assign" data-camera="${escapeHtml(camera.id)}">Change</button>
         <button type="button" data-action="unassign" data-camera="${escapeHtml(camera.id)}">Unassign</button>`
      : `<button type="button" class="btn-info" data-action="assign" data-camera="${escapeHtml(camera.id)}">Assign</button>`;
    return `<tr data-camera-row="${escapeHtml(camera.id)}">
      <td><strong>${escapeHtml(camera.name || camera.id)}</strong><br /><span class="muted">${escapeHtml(camera.id)}</span></td>
      <td><span class="camera-model-source">${currentBadge}${missingNote}</span></td>
      <td><select class="camera-model-select" aria-label="Model for ${escapeHtml(camera.name || camera.id)}">${selectOptions(assignmentPayload.models || [], assigned ? camera.model_path : '')}</select></td>
      <td class="camera-model-actions">${actions}</td>
    </tr>`;
  }).join('');
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

async function assignModel(cameraId) {
  const row = tableBody.querySelector(`tr[data-camera-row="${CSS.escape(cameraId)}"]`);
  const select = row?.querySelector('select');
  const modelPath = String(select?.value || '');
  if (!modelPath) {
    showToast('Pick a model to assign first.', true);
    return;
  }
  try {
    await api(`/api/camera-models/${encodeURIComponent(cameraId)}`, {
      method: 'PUT',
      body: JSON.stringify({ model_path: modelPath }),
    });
    showToast(`Model assigned to ${cameraId}.`);
    await loadAssignments();
  } catch (err) {
    showToast(err.message || 'Could not assign the model.', true);
  }
}

async function unassignModel(cameraId) {
  try {
    await api(`/api/camera-models/${encodeURIComponent(cameraId)}`, { method: 'DELETE' });
    showToast(`${cameraId} now follows the default model.`);
    await loadAssignments();
  } catch (err) {
    showToast(err.message || 'Could not unassign the model.', true);
  }
}

tableBody.addEventListener('click', (event) => {
  const button = event.target instanceof Element ? event.target.closest('button[data-action]') : null;
  if (!button) return;
  const cameraId = button.getAttribute('data-camera') || '';
  if (button.getAttribute('data-action') === 'assign') {
    assignModel(cameraId);
  } else {
    unassignModel(cameraId);
  }
});

loadAssignments();
