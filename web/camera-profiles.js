// camera-profiles.js - Settings > Camera Profiles: create, edit, duplicate and
// delete the Day/Night profiles cameras choose from (field definitions in
// profile-fields.js). Profiles are linked: saving one updates every camera that
// uses it (app/profile_presets.py::link_camera_profiles).

const profileEls = {
  editor: document.getElementById('profileEditor'),
  lists: { day: document.getElementById('profileListDay'), night: document.getElementById('profileListNight') },
  message: document.getElementById('profilesMessage'),
};
let cameraProfiles = [];
let globalLiveDefaults = {};

function profileMessage(text, isError = false) {
  if (profileEls.message) {
    profileEls.message.textContent = text;
    profileEls.message.className = isError ? 'error' : 'muted';
  }
  if (text) window.showToast?.(text, isError);
}

function profileModeLabel(mode) {
  return mode === 'night' ? 'Night' : 'Day';
}

function profileRowHtml(profile) {
  const summary = profileSummary(profile.settings) || 'No changes: follows Detection & Live.';
  const usedBy = profile.used_by || [];
  const usage = usedBy.length ? `Used by ${usedBy.join(', ')}` : 'Not used by any camera';
  const id = escapeHtml(profile.id);
  const actions = profile.builtin
    ? `<button type="button" class="secondary" data-profile-view="${id}">View</button>
       <button type="button" class="secondary" data-profile-duplicate="${id}">Duplicate</button>`
    : `<button type="button" class="secondary" data-profile-edit="${id}">Edit</button>
       <button type="button" class="secondary" data-profile-duplicate="${id}">Duplicate</button>
       <button type="button" class="btn-danger" data-profile-delete="${id}"${usedBy.length ? ' disabled title="Choose another profile for the cameras using it first"' : ''}>Delete</button>`;
  return `<div class="profile-row" data-profile-id="${id}">
    <div class="profile-row-main">
      <span class="profile-row-name">${escapeHtml(profile.name)}${profile.builtin ? ' <span class="profile-badge">Built-in</span>' : ''}</span>
      <span class="profile-row-summary">${escapeHtml(summary)}</span>
      <span class="profile-row-usage">${escapeHtml(usage)}</span>
    </div>
    <div class="profile-row-actions">${actions}</div>
  </div>`;
}

function renderProfileLists() {
  for (const mode of ['day', 'night']) {
    const list = profileEls.lists[mode];
    if (!list) continue;
    const profiles = cameraProfiles.filter((profile) => profile.mode === mode)
      // Your own profiles first, then the built-ins.
      .sort((a, b) => Number(a.builtin) - Number(b.builtin) || a.name.localeCompare(b.name));
    list.innerHTML = profiles.map(profileRowHtml).join('') || '<p class="muted">No profiles.</p>';
  }
}

function profileFieldHtml(field, value, readOnly) {
  const name = `profile_${field.key}`;
  const tip = field.tip ? ` <span class="info-tip" data-tip="${escapeHtml(field.tip)}" title="${escapeHtml(field.tip)}" tabindex="0" aria-label="Help: ${escapeHtml(field.tip)}"></span>` : '';
  // Show the global value the field falls back to, in the field's own words.
  const globalValue = globalLiveDefaults[field.key];
  const globalOption = (field.options || []).find((option) => option.value === String(globalValue));
  const globalLabel = globalOption ? globalOption.label : globalValue;
  const globalText = globalValue == null || globalValue === '' ? '' : ` (${globalLabel})`;
  const disabled = readOnly ? ' disabled' : '';
  const current = value == null ? '' : String(value);
  let control;
  if (field.kind === 'select') {
    const options = [{ value: '', label: `Global Default${globalText}` }].concat(field.options);
    control = `<select name="${name}"${disabled}>${options.map((option) => (
      `<option value="${escapeHtml(option.value)}"${option.value === current ? ' selected' : ''}>${escapeHtml(option.label)}</option>`
    )).join('')}</select>`;
  } else if (field.kind === 'pixel') {
    control = `<span class="pixel-threshold-picker">${pixelThresholdPresetHtml(name, { allowDefault: true, defaultLabel: `Global Default${globalText}` })}` +
      `<input name="${name}" type="number" min="1" max="255" step="1" placeholder="Global Default" aria-label="Custom pixel change (1-255)" value="${escapeHtml(current)}"${disabled} /></span>`;
  } else {
    control = `<input name="${name}" type="number" min="${field.min}" max="${field.max}" step="${field.step}" placeholder="Global Default${escapeHtml(globalText || ` (${field.placeholder})`)}" value="${escapeHtml(current)}"${disabled} />`;
  }
  return `<label><span>${escapeHtml(field.label)}${tip}</span>${control}</label>`;
}

// Dim fields another field in this profile (or its global default) makes moot,
// as the Detection & Live card does.
function syncProfileEditorDependencies(form) {
  const effective = (key) => {
    const value = form.elements[`profile_${key}`]?.value;
    return value !== undefined && value !== '' ? value : globalLiveDefaults[key];
  };
  const alwaysRun = String(effective('always_run_object_detection')) !== 'false';
  setFieldInactive(form.elements.profile_periodic_scan_interval_seconds, alwaysRun,
    'Not used while Always Run Object Detection is Enabled.');
  const frames = Number.parseInt(effective('detection_confirm_frames'), 10) || 1;
  for (const key of ['detection_confirm_window', 'detection_confirm_iou']) {
    setFieldInactive(form.elements[`profile_${key}`], frames <= 1, 'Only used when Confirm Frames is above 1.');
  }
}

// Open the editor. ``profile`` is an existing profile (edit / view) or a draft
// ({mode, name, settings}) for a new or duplicated one.
function openProfileEditor(profile, { readOnly = false, isNew = false } = {}) {
  const editor = profileEls.editor;
  if (!editor) return;
  const settings = profile.settings || {};
  const usedBy = profile.used_by || [];
  const title = readOnly ? profile.name : (isNew ? `New ${profileModeLabel(profile.mode)} Profile` : `Edit ${profile.name}`);
  const groups = PROFILE_FIELD_GROUPS.map((group) => `
    <div class="settings-group">
      <h4 class="settings-group-title">${escapeHtml(group.title)}</h4>
      <div class="form-grid">${group.fields.map((field) => profileFieldHtml(field, settings[field.key], readOnly)).join('')}</div>
    </div>`).join('');
  editor.innerHTML = `<form class="profile-editor-form" novalidate>
    <div class="profile-editor-head">
      <h3>${escapeHtml(title)}</h3>
      <span class="profile-badge">${profileModeLabel(profile.mode)}</span>
    </div>
    ${readOnly ? '<p class="muted">Built-in profiles cannot be changed. Duplicate one to make your own version.</p>' : `
    <div class="form-grid">
      <label class="full-width"><span>Name</span><input name="profile_name" type="text" maxlength="80" required value="${escapeHtml(profile.name || '')}" /></label>
    </div>`}
    ${!readOnly && usedBy.length ? `<p class="muted">Saving updates ${usedBy.length === 1 ? 'the camera' : 'all cameras'} using this profile: ${escapeHtml(usedBy.join(', '))}.</p>` : ''}
    ${groups}
    <div class="button-row">
      ${readOnly ? '' : '<button type="submit" class="primary">Save Profile</button>'}
      ${readOnly ? `<button type="button" class="secondary" data-profile-duplicate="${escapeHtml(profile.id)}">Duplicate</button>` : ''}
      <button type="button" class="secondary" data-profile-close>${readOnly ? 'Close' : 'Cancel'}</button>
    </div>
  </form>`;
  editor.hidden = false;
  const form = editor.querySelector('form');
  bindPixelThresholdPresets(form);
  if (readOnly) form.querySelectorAll('select, input').forEach((control) => { control.disabled = true; });
  syncProfileEditorDependencies(form);
  form.addEventListener('input', () => syncProfileEditorDependencies(form));
  form.addEventListener('change', () => syncProfileEditorDependencies(form));
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    saveProfile(form, profile, isNew);
  });
  editor.scrollIntoView({ behavior: 'smooth', block: 'start' });
  (form.elements.profile_name || form.querySelector('button'))?.focus({ preventScroll: true });
}

function closeProfileEditor() {
  if (!profileEls.editor) return;
  profileEls.editor.hidden = true;
  profileEls.editor.innerHTML = '';
}

function readProfileEditor(form) {
  const settings = {};
  for (const group of PROFILE_FIELD_GROUPS) {
    for (const field of group.fields) {
      const value = parseProfileField(field, form.elements[`profile_${field.key}`]?.value);
      if (value !== null) settings[field.key] = value;
    }
  }
  return settings;
}

async function saveProfile(form, profile, isNew) {
  const name = String(form.elements.profile_name?.value || '').trim();
  if (!name) {
    profileMessage('Give the profile a name.', true);
    form.elements.profile_name?.focus();
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  if (button) button.disabled = true;
  try {
    const body = JSON.stringify({ name, mode: profile.mode, settings: readProfileEditor(form) });
    const saved = isNew
      ? await api('/api/camera-profile-presets', { method: 'POST', body })
      : await api(`/api/camera-profile-presets/${encodeURIComponent(profile.id)}`, { method: 'PUT', body });
    closeProfileEditor();
    await loadCameraProfiles();
    const used = saved.used_by || [];
    profileMessage(isNew
      ? `${saved.name} created. Choose it for a camera on the Cameras page.`
      : `${saved.name} saved${used.length ? ` and applied to ${used.join(', ')}` : ''}.`);
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    profileMessage(error.message || 'Could not save the profile.', true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function deleteProfile(profile) {
  if (!window.confirm(`Delete the ${profile.name} profile?`)) return;
  try {
    await api(`/api/camera-profile-presets/${encodeURIComponent(profile.id)}`, { method: 'DELETE' });
    closeProfileEditor();
    await loadCameraProfiles();
    profileMessage(`${profile.name} deleted.`);
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    profileMessage(error.message || 'Could not delete the profile.', true);
  }
}

function findProfile(id) {
  return cameraProfiles.find((profile) => profile.id === id);
}

document.getElementById('panel-profiles')?.addEventListener('click', (event) => {
  const target = event.target.closest('button');
  if (!target) return;
  const { profileNew, profileEdit, profileView, profileDuplicate, profileDelete } = target.dataset;
  if (profileNew) {
    openProfileEditor({ mode: profileNew, name: '', settings: {} }, { isNew: true });
  } else if (profileEdit && findProfile(profileEdit)) {
    openProfileEditor(findProfile(profileEdit));
  } else if (profileView && findProfile(profileView)) {
    openProfileEditor(findProfile(profileView), { readOnly: true });
  } else if (profileDuplicate && findProfile(profileDuplicate)) {
    const source = findProfile(profileDuplicate);
    const name = source.name.replace(/\s*\((Day|Night)\)$/, '');
    openProfileEditor({ mode: source.mode, name: `${name} Copy`, settings: { ...source.settings } }, { isNew: true });
  } else if (profileDelete && findProfile(profileDelete)) {
    deleteProfile(findProfile(profileDelete));
  } else if (target.hasAttribute('data-profile-close')) {
    closeProfileEditor();
  }
});

async function loadCameraProfiles() {
  await window.daygleAuthReady;
  const [payload, system] = await Promise.all([
    api('/api/camera-profile-presets'),
    api('/api/settings/system').catch(() => ({})),
  ]);
  cameraProfiles = Array.isArray(payload?.presets) ? payload.presets : [];
  globalLiveDefaults = system?.live || globalLiveDefaults;
  renderProfileLists();
}

if (profileEls.lists.day) {
  loadCameraProfiles().catch((error) => {
    if (window.daygleAuth?.redirecting) return;
    profileMessage(error.message || 'Could not load camera profiles.', true);
  });
}
