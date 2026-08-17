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

const groupsList = document.getElementById('groupsList');
const groupsMessage = document.getElementById('groupsMessage');
const groupNameInput = document.getElementById('groupNameInput');
const groupMembersInput = document.getElementById('groupMembersInput');
const groupMembersDatalist = document.getElementById('groupMembersDatalist');
const groupAddBtn = document.getElementById('groupAddBtn');
const groupCancelBtn = document.getElementById('groupCancelBtn');
const groupModesBody = document.getElementById('groupModesBody');
const groupModesTableWrap = document.getElementById('groupModesTableWrap');
const groupModesEmpty = document.getElementById('groupModesEmpty');

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
let groups = {}; // group name -> [member labels]
let groupModes = {}; // group name -> 'any' | 'moving' | 'still'
let editingGroupName = null; // set while editing an existing group

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
  groupModes = {};
  if (settings.group_modes && typeof settings.group_modes === 'object') {
    for (const [group, mode] of Object.entries(settings.group_modes)) {
      if (MODE_LABELS[mode]) groupModes[group] = mode;
    }
  }
  renderTable();
  renderGroupModes();
}

// ─── Object Groups ────────────────────────────────────────────────────────

function parseMembers(raw) {
  const seen = new Set();
  return String(raw || '')
    .split(',')
    .map((member) => member.trim().toLowerCase())
    .filter((member) => member && !seen.has(member) && seen.add(member));
}

function renderGroups() {
  if (!groupsList) return;
  const names = Object.keys(groups).sort();
  if (!names.length) {
    groupsList.innerHTML = '<p class="muted empty-message">No groups yet. Add one above - e.g. <code>vehicle</code> → <code>car, truck, bus</code>.</p>';
    return;
  }
  groupsList.innerHTML = names.map((name) => {
    const members = (groups[name] || [])
      .map((member) => `<span class="chip">${escapeHtml(titleCase(member))}</span>`)
      .join('');
    return `
      <div class="group-row" data-group-name="${escapeHtml(name)}">
        <div class="group-row-main">
          <span class="group-name">${escapeHtml(titleCase(name))}</span>
          <div class="group-members">${members || '<span class="muted">no members</span>'}</div>
        </div>
        <div class="group-row-actions">
          <button type="button" class="secondary" data-group-edit="${escapeHtml(name)}">Edit</button>
          <button type="button" class="btn-danger" data-group-remove="${escapeHtml(name)}">Remove</button>
        </div>
      </div>`;
  }).join('');
  groupsList.querySelectorAll('[data-group-edit]').forEach((btn) => {
    btn.addEventListener('click', () => startEditGroup(btn.dataset.groupEdit));
  });
  groupsList.querySelectorAll('[data-group-remove]').forEach((btn) => {
    btn.addEventListener('click', () => removeGroup(btn.dataset.groupRemove));
  });
}

function renderGroupModes() {
  if (!groupModesBody) return;
  const names = Object.keys(groups).sort();
  if (!names.length) {
    if (groupModesTableWrap) groupModesTableWrap.hidden = true;
    if (groupModesEmpty) groupModesEmpty.hidden = false;
    return;
  }
  if (groupModesTableWrap) groupModesTableWrap.hidden = false;
  if (groupModesEmpty) groupModesEmpty.hidden = true;
  const defaultMode = defaultSelect.value || 'moving';
  groupModesBody.innerHTML = names.map((name) => {
    const title = escapeHtml(titleCase(name));
    const members = (groups[name] || []).map((member) => titleCase(member)).join(', ');
    const override = groupModes[name];
    const effective = override || defaultMode;
    return `
      <tr data-group-mode="${escapeHtml(name)}">
        <td class="cell-label">${title}</td>
        <td class="muted">${escapeHtml(members)}</td>
        <td>
          <select data-group-mode-select="${escapeHtml(name)}" aria-label="Detection mode for ${title}">
            <option value="inherit" ${override ? '' : 'selected'}>Inherit (${escapeHtml(modeLabel(defaultMode))})</option>
            <option value="any" ${override === 'any' ? 'selected' : ''}>Moving &amp; Still</option>
            <option value="moving" ${override === 'moving' ? 'selected' : ''}>Moving Only</option>
            <option value="still" ${override === 'still' ? 'selected' : ''}>Still Only</option>
          </select>
        </td>
        <td><span class="model-status ${effective === 'any' ? 'model-status-installed' : 'model-status-active'}">${escapeHtml(modeLabel(effective))}</span></td>
      </tr>`;
  }).join('');
  groupModesBody.querySelectorAll('select[data-group-mode-select]').forEach((select) => {
    select.addEventListener('change', () => {
      const name = select.dataset.groupModeSelect;
      const value = select.value;
      if (value === 'inherit') delete groupModes[name];
      else groupModes[name] = value;
      renderGroupModes();
      markUnsaved();
    });
  });
}

function resetGroupForm() {
  editingGroupName = null;
  groupNameInput.value = '';
  groupMembersInput.value = '';
  groupAddBtn.textContent = 'Add Group';
  groupCancelBtn.hidden = true;
}

function startEditGroup(name) {
  editingGroupName = name;
  groupNameInput.value = titleCase(name);
  groupMembersInput.value = (groups[name] || []).join(', ');
  groupAddBtn.textContent = 'Save Group';
  groupCancelBtn.hidden = false;
  groupNameInput.focus();
}

async function persistGroups(next) {
  try {
    const result = await api('/api/settings/label_groups', {
      method: 'PUT',
      body: JSON.stringify({ groups: next }),
    });
    groups = (result && result.groups) || {};
    renderGroups();
    renderGroupModes();
    groupsMessage.textContent = 'Object groups saved.';
    window.showToast('Object groups saved.');
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    groupsMessage.textContent = error.message;
    window.showToast(error.message, true);
  }
}

async function saveGroup() {
  const name = groupNameInput.value.trim().toLowerCase();
  const members = parseMembers(groupMembersInput.value);
  if (!name) {
    window.showToast('Enter a group name.', true);
    return;
  }
  if (/\s|,/.test(name)) {
    window.showToast('Group name must be a single word (no spaces or commas).', true);
    return;
  }
  if (!members.length) {
    window.showToast('Add at least one member label.', true);
    return;
  }
  if (name !== editingGroupName && Object.prototype.hasOwnProperty.call(groups, name)) {
    window.showToast('A group with that name already exists.', true);
    return;
  }
  const next = { ...groups };
  if (editingGroupName && editingGroupName !== name) {
    delete next[editingGroupName];
    if (Object.prototype.hasOwnProperty.call(groupModes, editingGroupName)) {
      groupModes[name] = groupModes[editingGroupName];
      delete groupModes[editingGroupName];
    }
  }
  next[name] = members;
  await persistGroups(next);
  resetGroupForm();
}

async function removeGroup(name) {
  const next = { ...groups };
  delete next[name];
  delete groupModes[name];
  if (editingGroupName === name) resetGroupForm();
  await persistGroups(next);
}

async function loadAll() {
  await window.daygleAuthReady;
  const [objectSettings, aiSettings, groupSettings] = await Promise.all([
    api('/api/settings/objects'),
    api('/api/settings/ai'),
    api('/api/settings/label_groups'),
  ]);
  availableLabels = Array.isArray(aiSettings.available_labels)
    ? aiSettings.available_labels.filter((label) => String(label || '').trim())
    : [];
  groups = (groupSettings && groupSettings.groups) || {};
  render(objectSettings);
  renderGroups();
  if (groupMembersDatalist) {
    groupMembersDatalist.innerHTML = availableLabels
      .map((label) => `<option value="${escapeHtml(label)}">${escapeHtml(titleCase(label))}</option>`)
      .join('');
  }
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
    const groupModesPayload = {};
    for (const [group, mode] of Object.entries(groupModes)) {
      if (Object.prototype.hasOwnProperty.call(groups, group)) groupModesPayload[group] = mode;
    }
    const payload = {
      default_mode: defaultSelect.value || 'moving',
      labels,
      group_modes: groupModesPayload,
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

groupAddBtn?.addEventListener('click', saveGroup);
groupCancelBtn?.addEventListener('click', resetGroupForm);

window.addEventListener('beforeunload', (event) => {
  if (!hasUnsavedChanges) return;
  event.preventDefault();
  event.returnValue = '';
});

loadAll().catch((error) => {
  if (window.daygleAuth?.redirecting) return;
  messageEl.textContent = error.message;
});
