// objects.js - Per-label still/moving object detection settings (Objects page).
// Loaded on objects.html only. Uses api() / escapeHtml / titleCase /
// showToast from web/utils.js.

const defaultSelect = document.querySelector('select[name="default_mode"]');
const tableBody = document.getElementById('objectsTableBody');
const tableWrap = document.getElementById('objectsTableWrap');
const emptyEl = document.getElementById('objectsEmpty');
const messageEl = document.getElementById('objectsMessage');
const saveBtn = document.getElementById('saveObjectsBtn');
const saveBtnHeader = document.getElementById('saveObjectsBtnHeader');

const MODE_LABELS = {
  any: 'Moving & Still',
  moving: 'Moving Only',
  still: 'Still Only',
};

function modeLabel(mode) {
  return MODE_LABELS[mode] || 'Moving & Still';
}

let hasUnsavedChanges = false;
let availableLabels = [];
let labels = {}; // label -> 'any' | 'moving' | 'still' (explicit overrides only)
let stillAlerts = {}; // label -> minutes for the "still for N minutes" dwell alert

function markUnsaved() {
  if (hasUnsavedChanges) return;
  hasUnsavedChanges = true;
  if (saveBtnHeader) saveBtnHeader.style.display = '';
}

function markSaved() {
  hasUnsavedChanges = false;
  if (saveBtnHeader) saveBtnHeader.style.display = 'none';
}

function renderTable() {
  if (!availableLabels.length) {
    tableWrap.hidden = true;
    emptyEl.hidden = false;
    return;
  }
  tableWrap.hidden = false;
  emptyEl.hidden = true;
  const defaultMode = defaultSelect.value || 'moving';
  tableBody.innerHTML = availableLabels.map((label) => {
    const title = escapeHtml(titleCase(label));
    const override = labels[label];
    const effective = override || defaultMode;
    const stillMinutes = stillAlerts[label] || 0;
    return `
      <tr data-object-label="${escapeHtml(label)}">
        <td class="cell-label">${title}</td>
        <td>
          <select data-object-mode="${escapeHtml(label)}" aria-label="Detection mode for ${title}">
            <option value="inherit" ${override ? '' : 'selected'}>Inherit (${escapeHtml(modeLabel(defaultMode))})</option>
            <option value="any" ${override === 'any' ? 'selected' : ''}>Moving &amp; Still</option>
            <option value="moving" ${override === 'moving' ? 'selected' : ''}>Moving Only</option>
            <option value="still" ${override === 'still' ? 'selected' : ''}>Still Only</option>
          </select>
        </td>
        <td><span class="model-status ${effective === 'any' ? 'model-status-installed' : 'model-status-active'}">${escapeHtml(modeLabel(effective))}</span></td>
        <td>
          <input type="number" min="0" step="1" inputmode="numeric" class="still-alert-input" value="${stillMinutes}" data-still-alert="${escapeHtml(label)}" aria-label="Still alert after minutes for ${title}" title="Alert after this object has been detected continuously still for this many minutes (0 = off)">
        </td>
      </tr>`;
  }).join('');

  tableBody.querySelectorAll('select[data-object-mode]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = select.dataset.objectMode;
      const value = select.value;
      if (value === 'inherit') delete labels[label];
      else labels[label] = value;
      renderTable();
      markUnsaved();
    });
  });

  tableBody.querySelectorAll('input[data-still-alert]').forEach((input) => {
    // Values are read fresh from the inputs at save time, so this listener
    // only needs to flag the form dirty.
    input.addEventListener('change', markUnsaved);
  });
}

function render(settings) {
  defaultSelect.value = settings.default_mode || 'moving';
  labels = {};
  if (settings.labels && typeof settings.labels === 'object') {
    for (const [label, mode] of Object.entries(settings.labels)) {
      if (MODE_LABELS[mode]) labels[label] = mode;
    }
  }
  stillAlerts = {};
  if (settings.still_alerts && typeof settings.still_alerts === 'object') {
    for (const [label, minutes] of Object.entries(settings.still_alerts)) {
      const parsed = Number.parseInt(minutes, 10);
      if (Number.isFinite(parsed) && parsed > 0) stillAlerts[label] = parsed;
    }
  }
  renderTable();
}

async function loadAll() {
  await window.daygleAuthReady;
  const [objectSettings, aiSettings] = await Promise.all([
    api('/api/settings/objects'),
    api('/api/settings/ai'),
  ]);
  availableLabels = Array.isArray(aiSettings.available_labels)
    ? aiSettings.available_labels.filter((label) => String(label || '').trim())
    : [];
  render(objectSettings);
}

defaultSelect.addEventListener('change', () => {
  renderTable();
  markUnsaved();
});

async function saveObjects() {
  try {
    saveBtn.disabled = true;
    if (saveBtnHeader) saveBtnHeader.disabled = true;
    const stillAlertsPayload = {};
    tableBody.querySelectorAll('input[data-still-alert]').forEach((input) => {
      const label = input.dataset.stillAlert;
      const minutes = Number.parseInt(input.value, 10) || 0;
      if (minutes > 0) stillAlertsPayload[label] = minutes;
    });
    const payload = {
      default_mode: defaultSelect.value || 'moving',
      labels,
      still_alerts: stillAlertsPayload,
    };
    const result = await api('/api/settings/objects', { method: 'PUT', body: JSON.stringify(payload) });
    render(result);
    markSaved();
    messageEl.textContent = 'Object detection behavior saved.';
    window.showToast('Object detection behavior saved.');
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    messageEl.textContent = error.message;
    window.showToast(error.message, true);
  } finally {
    saveBtn.disabled = false;
    if (saveBtnHeader) saveBtnHeader.disabled = false;
  }
}

saveBtn.addEventListener('click', saveObjects);
saveBtnHeader?.addEventListener('click', saveObjects);

window.addEventListener('beforeunload', (event) => {
  if (!hasUnsavedChanges) return;
  event.preventDefault();
  event.returnValue = '';
});

loadAll().catch((error) => {
  if (window.daygleAuth?.redirecting) return;
  messageEl.textContent = error.message;
});
