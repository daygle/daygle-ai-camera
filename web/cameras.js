let cameras = [];
let cameraProfilePresets = [];
let pendingDeleteIndex = null;
const cameraResolutions = {};
const cameraFps = {};

const messageEl = document.getElementById('cameraMessage');
const gridEl = document.getElementById('cameraGrid');

// Below 600px the table stacks into one card per camera (.stack-table); in
// the narrow-tablet range it keeps its columns (its headers are the sort
// controls) and scrolls sideways inside its card. Tell the operator that, but
// only while it ACTUALLY overflows - a hint promising a scroll the layout does
// not need is worse than none. A ResizeObserver covers the table appearing and
// every re-render; the resize listener covers rotate / breakpoint changes.
function updateScrollHint() {
  var hint = document.querySelector('.cameras-scroll-hint');
  if (!hint) return;
  var wrap = gridEl.querySelector('.cameras-table-wrap');
  hint.hidden = !wrap || wrap.scrollWidth <= wrap.clientWidth + 1;
}
if (gridEl) {
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(function() { updateScrollHint(); }).observe(gridEl);
  }
  var scrollHintFrame = null;
  window.addEventListener('resize', function() {
    if (scrollHintFrame !== null) return;
    scrollHintFrame = requestAnimationFrame(function() {
      scrollHintFrame = null;
      updateScrollHint();
    });
  });
  updateScrollHint();
}
const emptyEl = document.getElementById('cameraEmpty');
const deleteModal = document.getElementById('deleteModal');

// Health + filter state
const cameraHealth = {};
const filter = {
  text: document.getElementById('cameraFilter'),
  backend: document.getElementById('cameraBackendFilter'),
  reset: document.getElementById('cameraFilterResetBtn'),
  form: document.getElementById('camerasFilterForm'),
};

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? 'error' : 'muted';
  if (text) window.showToast?.(text, isError);
}

// ─── Inline edit form builder ─────────────────────────────────────────────────

// Day/Night profiles are chosen per camera here and managed on Settings >
// Camera Profiles (camera-profiles.js). The camera stores which profile each
// mode uses; the server copies that profile's values in whenever the camera or
// the profile is saved (app/profile_presets.py::link_camera_profiles).
const LEGACY_MOTION_LABELS = {
  motion_gate_fraction: 'Wake-Up Threshold',
  motion_scale_fraction: 'Motion Score Scale',
  motion_background_alpha: 'Background Adapt Speed',
  motion_algorithm: 'Background Model',
};

function globalDefaultProfileId(mode) {
  return 'global-default-' + mode;
}

function cameraProfileById(id, mode) {
  return cameraProfilePresets.find(function(preset) { return preset.id === id && preset.mode === mode; });
}

// Options for a camera's Day or Night profile select. A camera that predates
// linked profiles (or was written by the API without one) keeps its own
// values, shown as "Custom (This Camera Only)" until another profile is chosen.
function profileSelectOptionsHtml(mode, selectedId) {
  var option = function(id, label) {
    return '<option value="' + escapeHtml(id) + '"' + (id === selectedId ? ' selected' : '') + '>' + escapeHtml(label) + '</option>';
  };
  var byName = function(a, b) { return a.name.localeCompare(b.name); };
  var forMode = cameraProfilePresets.filter(function(preset) { return preset.mode === mode; });
  var own = forMode.filter(function(preset) { return !preset.builtin; }).sort(byName);
  var builtin = forMode.filter(function(preset) { return preset.builtin; });
  var known = selectedId === globalDefaultProfileId(mode) || Boolean(cameraProfileById(selectedId, mode));
  return (known ? '' : '<option value="" selected>Custom (This Camera Only)</option>') +
    option(globalDefaultProfileId(mode), 'Global Default') +
    (own.length ? '<optgroup label="Your Profiles">' + own.map(function(preset) { return option(preset.id, preset.name); }).join('') + '</optgroup>' : '') +
    (builtin.length ? '<optgroup label="Built-in">' + builtin.map(function(preset) { return option(preset.id, preset.name); }).join('') + '</optgroup>' : '');
}

function profileSelectSummary(camera, mode, selectedId) {
  if (selectedId === globalDefaultProfileId(mode)) return 'Follows Detection & Live on the Settings page.';
  var preset = cameraProfileById(selectedId, mode);
  var settings = preset ? preset.settings : ((camera.detection_profiles || {})[mode] || {});
  var values = {};
  Object.keys(settings || {}).forEach(function(key) {
    if (!Object.prototype.hasOwnProperty.call(LEGACY_MOTION_LABELS, key)) values[key] = settings[key];
  });
  return profileSummary(values) || 'No changes: follows Detection & Live.';
}

// Motion-engine values an older version stored on this camera. They moved to
// the global Advanced Motion Engine and are not part of any profile, but still
// apply until cleared, so they ride along in hidden fields and are listed.
function legacyMotionOverridesHtml(camera, mode) {
  var stored = ((camera.detection_profiles || {})[mode]) || {};
  var keys = Object.keys(LEGACY_MOTION_LABELS);
  var hidden = keys.map(function(key) {
    var value = stored[key];
    return '<input type="hidden" name="' + mode + '_' + key + '" data-legacy-motion-override="' + mode + '" value="' + escapeHtml(value == null ? '' : String(value)) + '" />';
  }).join('');
  var set = keys.filter(function(key) { return stored[key] != null && stored[key] !== ''; });
  var label = mode === 'night' ? 'Night' : 'Day';
  var note = set.length
    ? '<p class="form-help muted" data-legacy-motion-note="' + mode + '">In ' + label + ' this camera also overrides ' +
      escapeHtml(set.map(function(key) { return LEGACY_MOTION_LABELS[key] + ' (' + stored[key] + ')'; }).join(', ')) +
      ', set before these moved to the global Advanced Motion Engine. <button type="button" class="secondary legacy-motion-clear" data-clear-legacy-motion="' + mode + '">Use the global values</button></p>'
    : '';
  return hidden + note;
}

function profileLinkSectionHtml(camera) {
  var profiles = camera.detection_profiles || {};
  var field = function(mode) {
    var label = mode === 'night' ? 'Night Profile' : 'Day Profile';
    var selected = profiles[mode + '_preset_id'] || '';
    return '<label><span>' + label + '</span><select name="profile_' + mode + '_preset" data-profile-select="' + mode + '">' +
      profileSelectOptionsHtml(mode, selected) + '</select>' +
      '<small class="profile-select-summary" data-profile-summary="' + mode + '">' + escapeHtml(profileSelectSummary(camera, mode, selected)) + '</small></label>';
  };
  return '<div class="cam-edit-section">' +
    '<h4 class="cam-edit-section-title">Day &amp; Night Profiles</h4>' +
    '<p class="form-help muted">Each profile is a named set of detection settings. Create and edit them on <a href="/settings#profiles">Settings → Camera Profiles</a>; editing a profile there updates every camera that uses it.</p>' +
    '<div class="form-grid profile-link-grid">' + field('day') + field('night') + '</div>' +
    legacyMotionOverridesHtml(camera, 'day') + legacyMotionOverridesHtml(camera, 'night') +
    '</div>';
}

// Auto-tracking section of the PTZ tab. Values mirror
// app/ptz_tracking.py::normalize_auto_track_settings (DEFAULTS and bounds).
function ptzAutoTrackSectionHtml(camera, index) {
  var at = (camera.ptz && camera.ptz.auto_track) || {};
  var tip = function(text) {
    return ' <span class="info-tip" data-tip="' + escapeHtml(text) + '" title="' + escapeHtml(text) + '" tabindex="0" aria-label="Help: ' + escapeHtml(text) + '"></span>';
  };
  var onOff = function(name, on) {
    return '<select name="' + name + '">' +
      '<option value="false"' + (on ? '' : ' selected') + '>Disabled</option>' +
      '<option value="true"' + (on ? ' selected' : '') + '>Enabled</option>' +
    '</select>';
  };
  var num = function(value, fallback) { return value == null || value === '' ? fallback : value; };
  var labels = Array.isArray(at.labels) ? at.labels.join(', ') : 'person';
  return '<div class="cam-edit-section">' +
    '<h4 class="cam-edit-section-title">Auto-Tracking</h4>' +
    '<div class="form-grid">' +
      '<label class="full-width"><span>Auto-Tracking' + tip('Steer the camera to keep a detected object in the middle of the picture. Using the PTZ pad pauses it for 30 seconds.') + '</span>' + onOff('ptz_auto_track_enabled', at.enabled === true) + '</label>' +
      // A div, not a <label>: clicking a chip inside a label would also
      // activate the label's first button and toggle the list.
      '<div class="full-width ptz-track-field"><span>Follow These Objects' + tip('Pick the objects to follow. A group such as Pet follows any of its members. Tracking starts only for an object that passes this camera\'s zones and confirmation.') + '</span>' +
        '<input name="ptz_auto_track_labels" type="hidden" value="' + escapeHtml(labels) + '" />' +
        '<div class="multi-select" data-ptz-track-picker>' +
          '<div class="multi-select-chips" data-ptz-track-chips></div>' +
          '<button type="button" class="multi-select-toggle" data-ptz-track-toggle aria-haspopup="listbox" aria-expanded="false"><span>Select objects…</span><span class="multi-select-caret">▼</span></button>' +
          '<div class="multi-select-dropdown" data-ptz-track-dropdown role="listbox" aria-multiselectable="true" hidden>' +
            '<div class="multi-select-search"><input type="text" data-ptz-track-filter placeholder="Filter…" autocomplete="off" aria-label="Filter objects" /></div>' +
            '<div class="multi-select-options" data-ptz-track-options></div>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<label><span>Tracking Speed' + tip('How fast the camera turns towards the object (1-8, default 4). Lower is smoother; raise it for fast subjects.') + '</span><input name="ptz_auto_track_speed" type="number" min="1" max="8" step="1" placeholder="4" value="' + escapeHtml(String(num(at.speed, 4))) + '" /></label>' +
      '<label><span>Dead Zone (%)' + tip('How far from the centre the object may drift before the camera moves, as a share of the frame (5-40, default 15). Larger = calmer camera.') + '</span><input name="ptz_auto_track_dead_zone" type="number" min="5" max="40" step="1" placeholder="15" value="' + escapeHtml(Math.round(num(at.dead_zone, 0.15) * 100)) + '" /></label>' +
      '<label><span>Lost After (s)' + tip('How long the object can be out of sight before tracking lets go (1-30 s, default 3).') + '</span><input name="ptz_auto_track_lost_seconds" type="number" min="1" max="30" step="0.5" placeholder="3" value="' + escapeHtml(num(at.lost_seconds, 3)) + '" /></label>' +
      '<label><span>Return Home After (s)' + tip('With nothing to follow for this long, go back to the home position (0 = stay where it stopped; default 30).') + '</span><input name="ptz_auto_track_return_home_seconds" type="number" min="0" max="3600" step="1" placeholder="30" value="' + escapeHtml(num(at.return_home_seconds, 30)) + '" /></label>' +
      '<label class="full-width"><span>Home Position' + tip('Blank = the camera\'s own home position (ONVIF). Or pick a saved preset. Pelco-D cameras need a preset number here.') + '</span><input name="ptz_auto_track_home_preset" type="text" list="ptzPresets-' + index + '" placeholder="Camera home position" value="' + escapeHtml(at.home_preset || '') + '" /><datalist id="ptzPresets-' + index + '"></datalist></label>' +
      '<label><span>Zoom While Tracking' + tip('Zoom in on a small object once it is centred, and back out when it nears the edge or is lost.') + '</span>' + onOff('ptz_auto_track_zoom', at.zoom === true) + '</label>' +
      '<label><span>Target Size (%)' + tip('With zoom on: how much of the frame height the object should fill (5-80, default 30).') + '</span><input name="ptz_auto_track_target_size" type="number" min="5" max="80" step="1" placeholder="30" value="' + escapeHtml(Math.round(num(at.target_size, 0.3) * 100)) + '" /></label>' +
    '</div>' +
    '<p class="form-help muted">While the camera is moving, moving/still cannot be told apart, so an object only alerts if it is set to Moving &amp; Still on the Objects page. Tracking starts on a moving object; to also start on one sitting still, set its mode to Moving &amp; Still.</p>' +
  '</div>';
}

// Choices for the Follow These Objects picker: the user's object groups
// (Objects page) and the classes the loaded model can detect. Fetched once per
// page load and shared by every camera's form.
var ptzTrackChoicesPromise = null;
function loadPtzTrackChoices() {
  if (!ptzTrackChoicesPromise) {
    ptzTrackChoicesPromise = api('/api/settings/label_groups').then(function(payload) {
      return {
        groups: Object.keys((payload && payload.groups) || {}).sort(),
        labels: ((payload && payload.available_labels) || []).filter(function(label) {
          return label && label !== 'motion' && label !== 'face';
        }),
      };
    }).catch(function() {
      ptzTrackChoicesPromise = null;  // retry on the next form open
      return { groups: [], labels: [] };
    });
  }
  return ptzTrackChoicesPromise;
}

function parsePtzTrackLabels(value) {
  var seen = {};
  return String(value || '').split(',').map(function(label) { return label.trim().toLowerCase(); })
    .filter(function(label) {
      if (!label || seen[label]) return false;
      seen[label] = true;
      return true;
    });
}

// Chip multi-select for Follow These Objects. The hidden input keeps the
// comma-separated value the save code (and the backend) already expect, so a
// saved "cat, dog" opens as two chips.
function bindPtzTrackPicker(form) {
  var field = form.querySelector('.ptz-track-field');
  if (!field) return;
  var input = field.querySelector('[name="ptz_auto_track_labels"]');
  var picker = field.querySelector('[data-ptz-track-picker]');
  var chips = field.querySelector('[data-ptz-track-chips]');
  var toggle = field.querySelector('[data-ptz-track-toggle]');
  var dropdown = field.querySelector('[data-ptz-track-dropdown]');
  var filter = field.querySelector('[data-ptz-track-filter]');
  var options = field.querySelector('[data-ptz-track-options]');
  var choices = { groups: [], labels: [] };
  var selected = parsePtzTrackLabels(input.value);

  function commit() {
    input.value = selected.join(', ');
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function renderChips() {
    chips.innerHTML = selected.map(function(label) {
      var name = escapeHtml(titleCase(label));
      return '<span class="multi-select-chip">' + name + '<span class="multi-select-chip-remove" role="button" tabindex="0" data-remove="' + escapeHtml(label) + '" title="Stop following ' + name + '" aria-label="Stop following ' + name + '">&times;</span></span>';
    }).join('');
    // Nothing picked saves as the default (person), so say so.
    toggle.querySelector('span').textContent = selected.length ? selected.length + ' selected' : 'Select objects… (Person if none)';
  }

  function optionHtml(label) {
    var checked = selected.indexOf(label) !== -1 ? ' checked' : '';
    return '<label class="multi-select-option"><input type="checkbox" value="' + escapeHtml(label) + '"' + checked + '><span class="multi-select-option-label">' + escapeHtml(titleCase(label)) + '</span></label>';
  }

  function renderOptions() {
    var query = (filter.value || '').trim().toLowerCase();
    var match = function(label) { return !query || label.indexOf(query) !== -1 || titleCase(label).toLowerCase().indexOf(query) !== -1; };
    var groups = choices.groups.filter(match);
    var groupSet = {};
    choices.groups.forEach(function(name) { groupSet[name] = true; });
    // A saved label the model no longer lists stays visible so it can be removed.
    var labels = choices.labels.concat(selected.filter(function(label) {
      return !groupSet[label] && choices.labels.indexOf(label) === -1;
    })).filter(function(label) { return !groupSet[label] && match(label); });
    if (!groups.length && !labels.length) {
      options.innerHTML = '<div class="multi-select-empty">' + (query ? 'No matching objects.' : 'No objects are available yet. Load an ONNX model first.') + '</div>';
      return;
    }
    options.innerHTML =
      (groups.length ? '<div class="multi-select-heading">Groups</div>' + groups.map(optionHtml).join('') : '') +
      (labels.length ? (groups.length ? '<div class="multi-select-heading">Objects</div>' : '') + labels.map(optionHtml).join('') : '');
  }

  function setOpen(open) {
    dropdown.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (open) {
      filter.value = '';
      renderOptions();
      filter.focus();
    }
  }

  function remove(label) {
    selected = selected.filter(function(item) { return item !== label; });
    commit();
    renderChips();
    if (!dropdown.hidden) renderOptions();
  }

  toggle.addEventListener('click', function(event) {
    event.preventDefault();
    setOpen(dropdown.hidden);
  });
  filter.addEventListener('input', renderOptions);
  filter.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') { setOpen(false); toggle.focus(); }
  });
  options.addEventListener('change', function(event) {
    var box = event.target;
    if (!box || box.type !== 'checkbox') return;
    if (box.checked) {
      if (selected.indexOf(box.value) === -1) selected.push(box.value);
    } else {
      selected = selected.filter(function(item) { return item !== box.value; });
    }
    commit();
    renderChips();
  });
  chips.addEventListener('click', function(event) {
    var button = event.target.closest('[data-remove]');
    if (button) remove(button.dataset.remove);
  });
  chips.addEventListener('keydown', function(event) {
    var button = event.target.closest('[data-remove]');
    if (button && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      remove(button.dataset.remove);
    }
  });
  document.addEventListener('click', function(event) {
    if (!dropdown.hidden && !picker.contains(event.target)) setOpen(false);
  });

  renderChips();
  loadPtzTrackChoices().then(function(loaded) {
    choices = loaded;
    if (!dropdown.hidden) renderOptions();
  });
}

// Fill the Home Position suggestions with the camera's saved presets (ONVIF).
// Best-effort: a camera that is offline or does not list presets just keeps
// the free-text field.
function loadPtzPresetSuggestions(camera, index) {
  if (!camera || !camera.id || !(camera.ptz && camera.ptz.enabled)) return;
  var list = document.getElementById('ptzPresets-' + index);
  if (!list) return;
  api('/api/cameras/' + encodeURIComponent(camera.id) + '/ptz/presets').then(function(payload) {
    list.innerHTML = (payload.presets || []).map(function(preset) {
      return '<option value="' + escapeHtml(preset.token) + '">' + escapeHtml(preset.name) + '</option>';
    }).join('');
  }).catch(function() { /* presets are optional */ });
}

function buildEditFormHtml(camera, index) {
  const backend = camera.backend || 'onvif';
  const isRtsp = backend === 'rtsp';
  const rowId = 'edit-row-' + index;
  const formId = 'edit-form-' + index;
  const htmlAttr = (value) => escapeHtml(value == null ? '' : String(value));
  const runtimeActive = camera.profile_status?.active || camera.detection_profiles?.active || 'day';
  const runtimeSource = camera.detection_profiles?.source || 'manual';
  const runtimeSourceLabel = runtimeSource === 'solar' ? 'Solar' : runtimeSource === 'schedule' ? 'Scheduled' : runtimeSource === 'onvif' ? 'ONVIF' : 'Manual';
  const runtimeNote = runtimeSource === 'solar'
    ? 'Solar sunrise and sunset times update daily using this camera\'s coordinates and timezone.'
    : runtimeSource === 'onvif'
      ? 'ONVIF IR detection falls back to the schedule when unsupported.'
      : runtimeSource === 'schedule'
        ? 'Scheduled times use this camera\'s timezone.'
        : 'Manual profile selection is active.';
  return '<tr class="camera-edit-row" id="' + htmlAttr(rowId) + '"><td colspan="6"><div class="camera-edit-panel">' +
    '<div class="cam-edit-head">' +
      '<span class="cam-edit-head-title">Editing <strong>' + escapeHtml(camera.name || camera.id || ('Camera ' + (index + 1))) + '</strong></span>' +
      (camera.id ? '<span class="cam-edit-head-id">ID · ' + escapeHtml(camera.id) + '</span>' : '') +
      '<button type="button" class="secondary cam-edit-collapse-btn" data-index="' + htmlAttr(index) + '" title="Collapse camera settings" aria-label="Collapse camera settings">' + ICONS.chevronUp + '</button>' +
    '</div>' +
    '<div class="modal-tabs" role="tablist">' +
      '<button class="modal-tab active" data-tab="connection" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="true"><span class="modal-tab-icon" aria-hidden="true">🔌</span>Connection</button>' +
      '<button class="modal-tab" data-tab="recording" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="false" tabindex="-1"><span class="modal-tab-icon" aria-hidden="true">🎬</span>Recording</button>' +
      '<button class="modal-tab" data-tab="ptz" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="false" tabindex="-1"><span class="modal-tab-icon" aria-hidden="true">🎯</span>PTZ</button>' +
      '<button class="modal-tab" data-tab="advanced" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="false" tabindex="-1"><span class="modal-tab-icon" aria-hidden="true">🛠️</span>Advanced</button>' +
    '</div>' +
    '<form class="camera-edit-form modal-body" data-camera-index="' + htmlAttr(index) + '" id="' + htmlAttr(formId) + '" novalidate autocomplete="off">' +
      '<input type="hidden" name="camera_index" value="' + htmlAttr(index) + '" />' +

      // Connection tab
      '<div class="modal-tab-panel" data-panel="connection">' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Status</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width"><span>Camera Enabled</span>' +
              '<select name="enabled">' +
                '<option value="true"' + (camera.enabled !== false ? ' selected' : '') + '>Enabled</option>' +
                '<option value="false"' + (camera.enabled === false ? ' selected' : '') + '>Disabled</option>' +
              '</select>' +
            '</label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Identity</h4>' +
          '<div class="form-grid">' +
            '<label><span>Camera Name</span><input name="name" placeholder="e.g. Front Door" required value="' + escapeHtml(camera.name || '') + '" /></label>' +
            '<label><span>Camera ID</span><input name="id" placeholder="e.g. front-door" value="' + escapeHtml(camera.id || '') + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Backend</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width"><span>Backend</span>' +
              '<select name="backend" class="cam-edit-backend">' +
                '<option value="onvif"' + (backend === 'onvif' ? ' selected' : '') + '>ONVIF / RTSP (Auto-Build URL)</option>' +
                '<option value="rtsp"' + (backend === 'rtsp' ? ' selected' : '') + '>RTSP (Manual URL)</option>' +
              '</select>' +
            '</label>' +
          '</div>' +
          '<div class="cam-rtsp-fields"' + (isRtsp ? '' : ' hidden') + '>' +
            '<div class="form-grid">' +
              '<label class="full-width"><span>Stream URL</span><input name="stream_url" placeholder="rtsp://user:pass@192.168.1.100:554/stream1" value="' + escapeHtml(camera.stream_url || '') + '" /></label>' +
            '</div>' +
          '</div>' +
          '<div class="cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '>' +
            '<div class="form-grid">' +
              '<label><span>Host / IP</span><input name="host" placeholder="192.168.1.100" value="' + escapeHtml(camera.host || '') + '" /></label>' +
              '<label><span>Port</span><input name="port" type="number" min="1" max="65535" placeholder="554" value="' + htmlAttr(camera.port || 554) + '" /></label>' +
              '<label><span>Username</span><input name="username" placeholder="admin" autocomplete="off" value="' + escapeHtml(camera.username || '') + '" /></label>' +
              '<label class="full-width"><span>Password</span><input name="password" type="password" autocomplete="new-password" placeholder="' + htmlAttr(camera.has_password ? '(saved - type to change)' : '(No Password)') + '" /></label>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Stream</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '><span>Stream Path</span><input name="path" placeholder="e.g. stream1" value="' + escapeHtml(camera.path || '') + '" /></label>' +
          '</div>' +
          '<p class="form-help muted">This single stream is used for live view, detection, and recordings.</p>' +
        '</div>' +
        '<div class="button-row cam-test-conn-row">' +
          '<button class="btn-info cam-test-conn-btn" data-form="' + htmlAttr(formId) + '" type="button">Test Connection</button>' +
          '<span class="muted cam-test-conn-result" data-form="' + htmlAttr(formId) + '"></span>' +
        '</div>' +
      '</div>' +

      // Recording tab
      '<div class="modal-tab-panel" data-panel="recording" hidden>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Recording</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width"><span>Continuous Recording</span>' +
              '<select name="continuous">' +
                '<option value="false"' + (camera.recording?.continuous ? '' : ' selected') + '>Disabled</option>' +
                '<option value="true"' + (camera.recording?.continuous ? ' selected' : '') + '>Enabled</option>' +
              '</select>' +
            '</label>' +
          '</div>' +
          '<p class="form-help muted">When enabled, the camera writes an uninterrupted stream regardless of events. Otherwise, clips are recorded per detection rule (configured on the Zones page per object).</p>' +
        '</div>' +
      '</div>' +

      // PTZ tab
      '<div class="modal-tab-panel" data-panel="ptz" hidden>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Control</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width"><span>PTZ Control</span>' +
              '<select name="ptz_enabled">' +
                '<option value="false"' + (camera.ptz?.enabled ? '' : ' selected') + '>Disabled</option>' +
                '<option value="true"' + (camera.ptz?.enabled ? ' selected' : '') + '>Enabled</option>' +
              '</select>' +
            '</label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Connection</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width"><span>Protocol <span class="info-tip" data-tip="ONVIF uses standard PTZ over the camera&#39;s HTTP port. TCP PelcoD sends raw binary commands to the Command Port." title="ONVIF uses standard PTZ over the camera&#39;s HTTP port. TCP PelcoD sends raw binary commands to the Command Port." tabindex="0" aria-label="Help: ONVIF uses standard PTZ over the camera&#39;s HTTP port. TCP PelcoD sends raw binary commands to the Command Port."></span></span>' +
              '<select name="ptz_protocol">' +
                '<option value="onvif"' + ((camera.ptz?.protocol || 'onvif') === 'onvif' ? ' selected' : '') + '>ONVIF (Recommended)</option>' +
                '<option value="tcp_pelcod"' + (camera.ptz?.protocol === 'tcp_pelcod' ? ' selected' : '') + '>TCP PelcoD (Legacy Cameras)</option>' +
              '</select></label>' +
            '<label><span>HTTP Port <span class="info-tip" data-tip="Camera web (ONVIF) port, usually 80 (ONVIF only)." title="Camera web (ONVIF) port, usually 80 (ONVIF only)." tabindex="0" aria-label="Help: Camera web (ONVIF) port, usually 80 (ONVIF only)."></span></span><input name="ptz_http_port" type="number" min="1" max="65535" placeholder="80" value="' + htmlAttr(camera.ptz?.http_port || 80) + '" /></label>' +
            '<label><span>Command Port <span class="info-tip" data-tip="Port for TCP PelcoD only (default 6060)." title="Port for TCP PelcoD only (default 6060)." tabindex="0" aria-label="Help: Port for TCP PelcoD only (default 6060)."></span></span><input name="ptz_port" type="number" min="1" max="65535" placeholder="6060" value="' + htmlAttr(camera.ptz?.port || 6060) + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Movement</h4>' +
          '<div class="form-grid">' +
            '<label><span>PTZ Address <span class="info-tip" data-tip="PelcoD device address (default 1, TCP PelcoD only)." title="PelcoD device address (default 1, TCP PelcoD only)." tabindex="0" aria-label="Help: PelcoD device address (default 1, TCP PelcoD only)."></span></span><input name="ptz_address" type="number" min="1" max="255" placeholder="1" value="' + htmlAttr(camera.ptz?.address || 1) + '" /></label>' +
            '<label><span>Speed <span class="info-tip" data-tip="Movement speed (1-8, default 5)." title="Movement speed (1-8, default 5)." tabindex="0" aria-label="Help: Movement speed (1-8, default 5)."></span></span><input name="ptz_speed" type="number" min="1" max="8" placeholder="5" value="' + htmlAttr(camera.ptz?.speed || 5) + '" /></label>' +
            '<label class="full-width"><span>Step Duration (s) <span class="info-tip" data-tip="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." title="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." tabindex="0" aria-label="Help: How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)."></span></span><input name="ptz_step_duration" type="number" min="0.1" max="5" step="0.1" placeholder="0.4" value="' + htmlAttr(camera.ptz?.step_duration != null ? Number(camera.ptz.step_duration).toFixed(2) : '') + '" /></label>' +
            '<label class="full-width"><span>PTZ Motion Detection <span class="info-tip" data-tip="Spot camera movement this app did not make (the camera&#39;s own patrol, return to home, or another app) from the whole picture changing. While it moves, objects cannot be judged moving or still, so only objects set to Moving &amp; Still alert, and the motion background is re-learned once it stops. Moves made from this app are always handled. Automatic = on when PTZ is enabled here. Use On for a PTZ moved by another app, Off if large close subjects filling the view pause alerts." title="Spot camera movement this app did not make (the camera&#39;s own patrol, return to home, or another app) from the whole picture changing. While it moves, objects cannot be judged moving or still, so only objects set to Moving &amp; Still alert, and the motion background is re-learned once it stops. Moves made from this app are always handled. Automatic = on when PTZ is enabled here. Use On for a PTZ moved by another app, Off if large close subjects filling the view pause alerts." tabindex="0" aria-label="Help: Spot camera movement this app did not make (the camera&#39;s own patrol, return to home, or another app) from the whole picture changing. While it moves, objects cannot be judged moving or still, so only objects set to Moving &amp; Still alert, and the motion background is re-learned once it stops. Moves made from this app are always handled. Automatic = on when PTZ is enabled here. Use On for a PTZ moved by another app, Off if large close subjects filling the view pause alerts."></span></span><select name="ptz_motion_detection">' +
              '<option value="auto"' + ((camera.detection?.ptz_motion_detection || 'auto') === 'auto' ? ' selected' : '') + '>Automatic (Follow PTZ)</option>' +
              '<option value="on"' + (camera.detection?.ptz_motion_detection === 'on' ? ' selected' : '') + '>On</option>' +
              '<option value="off"' + (camera.detection?.ptz_motion_detection === 'off' ? ' selected' : '') + '>Off</option>' +
            '</select></label>' +
          '</div>' +
          '<p class="form-help muted">Enable PTZ and save to show the control pad on the Live page. The camera&#39;s username and password from the Connection tab are used for ONVIF authentication.</p>' +
        '</div>' +
        ptzAutoTrackSectionHtml(camera, index) +
      '</div>' +

      // Advanced tab
      '<div class="modal-tab-panel" data-panel="advanced" hidden>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Detection Profile</h4>' +
          '<div class="form-grid">' +
            '<label><span>Active Profile</span><select name="detection_profile">' +
              '<option value="day"' + ((camera.detection_profiles?.active || 'day') === 'day' ? ' selected' : '') + '>Day</option>' +
              '<option value="night"' + (camera.detection_profiles?.active === 'night' ? ' selected' : '') + '>Night</option>' +
            '</select></label>' +
            '<label><span>Automatic Selection</span><select name="profile_source">' +
              '<option value="manual"' + ((camera.detection_profiles?.source || 'manual') === 'manual' ? ' selected' : '') + '>Manual</option>' +
              '<option value="schedule"' + (camera.detection_profiles?.source === 'schedule' ? ' selected' : '') + '>Schedule</option>' +
              '<option value="solar"' + (camera.detection_profiles?.source === 'solar' ? ' selected' : '') + '>Solar (Daily Sunrise/Sunset)</option>' +
              '<option value="onvif"' + (camera.detection_profiles?.source === 'onvif' ? ' selected' : '') + '>ONVIF IR State (Fallback Schedule)</option>' +
            '</select></label>' +
            '<label><span>Day Starts</span><input name="profile_day_start" type="time" value="' + escapeHtml(camera.detection_profiles?.day_start || '07:00') + '" /></label>' +
            '<label><span>Night Starts</span><input name="profile_night_start" type="time" value="' + escapeHtml(camera.detection_profiles?.night_start || '19:00') + '" /></label>' +
            '<label><span>Camera Timezone</span><input name="timezone" placeholder="e.g. Australia/Sydney" value="' + escapeHtml(camera.timezone || 'UTC') + '" /></label>' +
            '<label><span>Latitude</span><input name="latitude" type="number" min="-90" max="90" step="0.000001" placeholder="e.g. -33.8688" value="' + htmlAttr(camera.latitude != null ? camera.latitude : '') + '" /></label>' +
            '<label><span>Longitude</span><input name="longitude" type="number" min="-180" max="180" step="0.000001" placeholder="e.g. 151.2093" value="' + htmlAttr(camera.longitude != null ? camera.longitude : '') + '" /></label>' +
          '</div>' +
          '<div class="button-row">' +
            '<button type="button" class="secondary profile-suggest-btn">Suggest Sunrise/Sunset</button>' +
            '<span class="form-help muted profile-action-result" aria-live="polite"></span>' +
          '</div>' +
          '<p class="form-help muted">Choose which profile is active right now. Solar mode refreshes sunrise/sunset times daily using this camera’s coordinates and timezone. The Day and Night profiles below decide what runs in each.</p>' +
          '<p class="form-help muted">Runtime: <strong>' + escapeHtml(runtimeActive.charAt(0).toUpperCase() + runtimeActive.slice(1)) + '</strong> (' + escapeHtml(runtimeSourceLabel) + '). ' + escapeHtml(runtimeNote) + '</p>' +
        '</div>' +
        profileLinkSectionHtml(camera) +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Stream</h4>' +
          '<div class="form-grid">' +
            '<label><span>FPS <span class="info-tip" data-tip="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." title="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." tabindex="0" aria-label="Help: Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong."></span></span><input name="fps" type="number" min="1" max="120" placeholder="Automatic" value="' + htmlAttr(camera.fps != null ? camera.fps : '') + '" /></label>' +
            '<label><span>Frame Buffer Drains <span class="info-tip" data-tip="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for automatic (FPS/4)." title="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for automatic (FPS/4)." tabindex="0" aria-label="Help: Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for automatic (FPS/4)."></span></span><input name="stale_frame_grabs" type="number" min="0" max="20" placeholder="Automatic" value="' + htmlAttr(camera.stale_frame_grabs != null ? camera.stale_frame_grabs : '') + '" /></label>' +
          '</div>' +
          '<p class="form-help muted">Frame-buffer drains is a hint passed to the stream decoder. Leave FPS empty to auto-detect it from the stream; override it only if the detected value is wrong.</p>' +
        '</div>' +
      '</div>' +

      '<div class="modal-footer">' +
        '<button class="secondary cam-edit-cancel-btn" data-index="' + index + '" type="button">Cancel</button>' +
        '<button type="submit" class="cam-edit-save-btn">Save Camera</button>' +
      '</div>' +
    '</form>' +
  '</div></td></tr>';
}

// ─── Inline edit management ───────────────────────────────────────────────────

function closeAllEditForms() {
  if (openCameraEditIndex === null) return;
  openCameraEditIndex = null;
  renderGrid();
}

function toggleEditForm(camera, index) {
  openCameraEditIndex = openCameraEditIndex === index ? null : index;
  renderGrid();
}

function wireEditFormHandlers(index) {
  var formId = 'edit-form-' + index;
  var form = document.getElementById(formId);
  if (!form) return;
  var panel = form.closest('.camera-edit-panel');
  if (!panel) return;

  // Tab switching - tabs are siblings of the form, so query from the panel
  panel.querySelectorAll('.modal-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var tabName = tab.dataset.tab;
      panel.querySelectorAll('.modal-tab').forEach(function(t) {
        var active = t.dataset.tab === tabName;
        t.classList.toggle('active', active);
        t.setAttribute('aria-selected', String(active));
      });
      form.querySelectorAll('.modal-tab-panel').forEach(function(panelEl) {
        panelEl.hidden = panelEl.dataset.panel !== tabName;
      });
    });
  });

  var collapseButton = panel.querySelector('.cam-edit-collapse-btn');
  if (collapseButton) collapseButton.addEventListener('click', closeAllEditForms);
  loadPtzPresetSuggestions(cameras[index], index);
  bindPtzTrackPicker(form);

  bindPixelThresholdPresets(form);
  form.querySelectorAll('[data-clear-legacy-motion]').forEach(function(button) {
    button.addEventListener('click', function() {
      var mode = button.dataset.clearLegacyMotion;
      form.querySelectorAll('[data-legacy-motion-override="' + mode + '"]').forEach(function(input) { input.value = ''; });
      var note = form.querySelector('[data-legacy-motion-note="' + mode + '"]');
      if (note) note.textContent = 'Older engine overrides cleared. Save the camera to apply.';
    });
  });

  // Backend toggle
  var backendSelect = form.querySelector('[name="backend"]');
  if (backendSelect) {
    backendSelect.addEventListener('change', function() {
      var manual = this.value === 'rtsp';
      form.querySelectorAll('.cam-rtsp-fields').forEach(function(el) { el.hidden = !manual; });
      form.querySelectorAll('.cam-onvif-fields').forEach(function(el) { el.hidden = manual; });
    });
  }

  var suggestButton = form.querySelector('.profile-suggest-btn');
  var profileResult = form.querySelector('.profile-action-result');
  // The suggestion endpoint accepts the form's current location values as
  // overrides, so newly typed coordinates can be used without saving first.
  function formLocationOverrides() {
    var overrides = {};
    var latitude = parseFloat(form.querySelector('[name="latitude"]')?.value);
    if (Number.isFinite(latitude)) overrides.latitude = latitude;
    var longitude = parseFloat(form.querySelector('[name="longitude"]')?.value);
    if (Number.isFinite(longitude)) overrides.longitude = longitude;
    var timezoneName = (form.querySelector('[name="timezone"]')?.value || '').trim();
    if (timezoneName) overrides.timezone = timezoneName;
    return overrides;
  }
  if (suggestButton) {
    suggestButton.addEventListener('click', async function() {
      suggestButton.disabled = true;
      if (profileResult) profileResult.textContent = 'Calculating…';
      try {
        var cameraId = form.querySelector('[name="id"]')?.value || cameras[index]?.id;
        var locationParams = new URLSearchParams();
        Object.keys(formLocationOverrides()).forEach(function(key) {
          locationParams.set(key, formLocationOverrides()[key]);
        });
        var query = locationParams.toString();
        var suggestion = await api('/api/cameras/' + encodeURIComponent(cameraId) + '/profile-schedule-suggestion' + (query ? '?' + query : ''));
        form.querySelector('[name="profile_day_start"]').value = suggestion.day_start;
        form.querySelector('[name="profile_night_start"]').value = suggestion.night_start;
        if (profileResult) profileResult.textContent = 'Suggested ' + suggestion.day_start + ' / ' + suggestion.night_start + ' for ' + suggestion.date + '.';
      } catch (err) {
        if (!window.daygleAuth?.redirecting && profileResult) profileResult.textContent = err.message || 'Suggestion unavailable.';
      } finally {
        suggestButton.disabled = false;
      }
    });
  }
  // Show what the chosen Day / Night profile changes as soon as it is picked.
  form.querySelectorAll('[data-profile-select]').forEach(function(select) {
    select.addEventListener('change', function() {
      var mode = select.dataset.profileSelect;
      var summary = form.querySelector('[data-profile-summary="' + mode + '"]');
      if (summary) summary.textContent = profileSelectSummary(cameras[index] || {}, mode, select.value);
    });
  });


  // Form submit
  form.addEventListener('submit', async function(e) {
    e.preventDefault();
    var data = collectFormData(form);
    var camerasBefore = cameras.slice();
    var editTargetBefore = cameras[index];

    if (index >= cameras.length) {
      // New camera
      cameras.push(data);
    } else {
      cameras[index] = {
        ...cameras[index],
        ...data,
        detection: { ...(cameras[index].detection || {}), ...data.detection },
      };
    }

    try {
      var result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras: cameras }) });
      cameras = result.cameras || cameras;
      renderGrid();
      setMessage(index >= camerasBefore.length ? 'Camera added.' : 'Camera updated.');
    } catch (err) {
      if (window.daygleAuth?.redirecting) return;
      if (index >= camerasBefore.length) {
        cameras.splice(0, cameras.length, ...camerasBefore);
      } else {
        cameras.splice(index, 1, editTargetBefore);
      }
      setMessage(err.message, true);
    }
  });

  // Cancel button
  var cancelBtn = form.querySelector('.cam-edit-cancel-btn');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', function() { closeAllEditForms(); });
  }

  // Test connection
  var testBtn = form.querySelector('.cam-test-conn-btn');
  if (testBtn) {
    testBtn.addEventListener('click', async function() {
      var resultEl = form.querySelector('.cam-test-conn-result');
      var backend = (form.querySelector('[name="backend"]')?.value || 'onvif');
      var payload;
      if (backend === 'rtsp') {
        payload = { stream_url: (form.querySelector('[name="stream_url"]')?.value || '').trim() };
      } else {
        payload = {
          host: (form.querySelector('[name="host"]')?.value || '').trim(),
          port: parseInt((form.querySelector('[name="port"]')?.value || '554'), 10),
          path: (form.querySelector('[name="path"]')?.value || '').trim(),
          username: (form.querySelector('[name="username"]')?.value || '').trim(),
          password: form.querySelector('[name="password"]')?.value || '',
        };
      }
      testBtn.disabled = true;
      testBtn.textContent = 'Testing…';
      if (resultEl) { resultEl.textContent = ''; resultEl.style.color = ''; }
      try {
        var res = await api('/api/cameras/test-connection', { method: 'POST', body: JSON.stringify(payload) });
        if (resultEl) {
          resultEl.textContent = res.online ? (res.message || 'Connected') : (res.message || 'Unreachable');
          resultEl.style.color = res.online ? 'var(--color-success, #22c55e)' : 'var(--color-error, #ef4444)';
        }
      } catch (err) {
        if (window.daygleAuth?.redirecting) return;
        if (resultEl) {
          resultEl.textContent = err.message || 'Test failed';
          resultEl.style.color = 'var(--color-error, #ef4444)';
        }
      } finally {
        testBtn.disabled = false;
        testBtn.textContent = 'Test Connection';
      }
    });
  }
}

function collectFormData(form) {
  var getVal = function(name) { var el = form.querySelector('[name="' + name + '"]'); return el ? el.value : ''; };
  var getName = function(name) { return getVal(name).trim(); };
  var getInt = function(name, def) { var v = parseInt(getVal(name), 10); return isNaN(v) ? def : v; };
  var backend = getName('backend') || 'onvif';
  var cameraIndex = parseInt(getVal('camera_index'), 10);
  var existingProfiles = cameras[cameraIndex]?.detection_profiles || {};
  var activeProfile = getName('detection_profile') || existingProfiles.active || 'day';
  // Only which profile each mode uses is sent; the server copies the profile's
  // values in. The hidden legacy motion fields go too, so "Use the global
  // values" can clear them (an empty field is sent as null).
  var legacyOverrides = function(mode) {
    var values = {};
    form.querySelectorAll('[data-legacy-motion-override="' + mode + '"]').forEach(function(input) {
      var key = input.name.slice(mode.length + 1);
      var raw = String(input.value || '').trim();
      values[key] = raw === '' ? null : (key === 'motion_algorithm' ? raw : Number(raw));
    });
    return values;
  };
  var profiles = {
    active: activeProfile === 'night' ? 'night' : 'day',
    source: ['manual', 'schedule', 'solar', 'onvif'].includes(getName('profile_source')) ? getName('profile_source') : 'manual',
    day_preset_id: getName('profile_day_preset') || null,
    night_preset_id: getName('profile_night_preset') || null,
    day_start: getName('profile_day_start') || '07:00',
    night_start: getName('profile_night_start') || '19:00',
    day: legacyOverrides('day'),
    night: legacyOverrides('night'),
  };
  return {
    id: getName('id') || ('camera-' + (cameras.length + 1)),
    name: getName('name'),
    enabled: getVal('enabled') !== 'false',
    backend: backend,
    stream_url: backend === 'rtsp' ? getName('stream_url') : '',
    host: backend !== 'rtsp' ? getName('host') : '',
    port: getInt('port', 554),
    path: backend !== 'rtsp' ? getName('path') : '',
    username: getName('username'),
    password: getVal('password'),
    timezone: getName('timezone') || 'UTC',
    latitude: (function() { var v = getName('latitude'); return v !== '' ? Number(v) : null; })(),
    longitude: (function() { var v = getName('longitude'); return v !== '' ? Number(v) : null; })(),
    fps: (function() { var v = getName('fps'); return v !== '' ? parseInt(v, 10) : null; })(),
    stale_frame_grabs: (function() { var v = getName('stale_frame_grabs'); return v !== '' ? parseInt(v, 10) : null; })(),
    recording: {
      continuous: getVal('continuous') === 'true',
    },
    ptz: {
      enabled: getVal('ptz_enabled') === 'true',
      protocol: getName('ptz_protocol') || 'onvif',
      http_port: getInt('ptz_http_port', 80),
      port: getInt('ptz_port', 6060),
      address: getInt('ptz_address', 1),
      speed: getInt('ptz_speed', 5),
      step_duration: (function() { var raw = parseFloat(getVal('ptz_step_duration')); return isFinite(raw) ? raw : 0.4; })(),
      auto_track: {
        enabled: getVal('ptz_auto_track_enabled') === 'true',
        labels: getName('ptz_auto_track_labels') || 'person',
        speed: getInt('ptz_auto_track_speed', 4),
        dead_zone: (function() { var v = parseFloat(getVal('ptz_auto_track_dead_zone')); return isFinite(v) ? v / 100 : 0.15; })(),
        lost_seconds: (function() { var v = parseFloat(getVal('ptz_auto_track_lost_seconds')); return isFinite(v) ? v : 3; })(),
        return_home_seconds: (function() { var v = parseFloat(getVal('ptz_auto_track_return_home_seconds')); return isFinite(v) ? v : 30; })(),
        home_preset: getName('ptz_auto_track_home_preset'),
        zoom: getVal('ptz_auto_track_zoom') === 'true',
        target_size: (function() { var v = parseFloat(getVal('ptz_auto_track_target_size')); return isFinite(v) ? v / 100 : 0.3; })(),
      },
    },
    detection: { ptz_motion_detection: getName('ptz_motion_detection') || 'auto' },
    detection_profiles: profiles,
  };
}

// ─── Camera row rendering ─────────────────────────────────────────────────────

function formatCameraEndpoint(camera) {
  var host = String(camera.host || '').trim();
  var port = camera.port ? ':' + camera.port : '';
  var path = String(camera.path || '').trim();
  if (!host && camera.stream_url) {
    try {
      var parsed = new URL(camera.stream_url);
      host = parsed.hostname || '';
      port = parsed.port ? ':' + parsed.port : '';
      path = parsed.pathname || '';
    } catch (_err) {
      return 'Manual stream URL';
    }
  }
  if (!host) return 'Not configured';
  return host + port + (path ? path.charAt(0) === '/' ? path : '/' + path : '');
}

function formatCameraResolution(camera, runtimeResolution) {
  var configured = camera.width && camera.height ? camera.width + ' × ' + camera.height : '';
  if (runtimeResolution && runtimeResolution.width > 0 && runtimeResolution.height > 0) {
    var live = runtimeResolution.width + ' × ' + runtimeResolution.height;
    return live + (configured && live !== configured ? ' live' : '');
  }
  return configured ? configured + ' configured' : 'Automatic';
}

function renderCameraRow(camera, index) {
  var name = escapeHtml(camera.name || camera.id || ('Camera ' + (index + 1)));
  var id = escapeHtml(camera.id || '');
  var backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';
  var isEnabled = camera.enabled !== false;
  var runtimeHealth = cameraHealth[camera.id];
  var healthState = !isEnabled ? 'disabled' : runtimeHealth ? (runtimeHealth.online ? 'online' : 'offline') : 'checking';
  var healthLabel = healthState === 'disabled' ? 'Disabled' : healthState === 'online' ? 'Online' : healthState === 'offline' ? 'Offline' : 'Checking';
  var healthDotState = healthState === 'online' ? 'online' : healthState === 'checking' ? 'checking' : 'offline';
  var healthHtml = '<span class="camera-status-pill camera-status-' + healthState + '"><span class="health-dot ' + healthDotState + '"></span>' + healthLabel + '</span>';
  var endpoint = escapeHtml(formatCameraEndpoint(camera));
  var runtimeResolution = cameraResolutions[camera.id];
  var resolution = escapeHtml(formatCameraResolution(camera, runtimeResolution));
  var fps = cameraFps[camera.id];
  var fpsText = camera.fps ? Math.round(Number(camera.fps)) + ' FPS configured' : 'FPS auto-detect';
  if (fps && fps.source === 'detected' && Number(fps.detected) > 0) fpsText = Math.round(Number(fps.detected)) + ' FPS Detected';
  else if (fps && fps.source === 'configured' && Number(fps.configured) > 0) fpsText = Math.round(Number(fps.configured)) + ' FPS configured';
  var ptzEnabled = camera.ptz?.enabled === true;
  var profiles = camera.detection_profiles || {};
  var activeProfile = profiles.active === 'night' ? 'night' : 'day';
  var profileSource = profiles.source === 'schedule' ? 'Scheduled' : profiles.source === 'solar' ? 'Solar' : profiles.source === 'onvif' ? 'ONVIF' : 'Manual';
  var dayPreset = cameraProfilePresets.find(function(item) { return item.id === profiles.day_preset_id; });
  var nightPreset = cameraProfilePresets.find(function(item) { return item.id === profiles.night_preset_id; });
  var profilesHtml = '<div class="camera-profile-pills">' +
    '<span class="camera-profile-pill ' + (activeProfile === 'day' ? 'is-active' : '') + '">Day</span>' +
    '<span class="camera-profile-pill ' + (activeProfile === 'night' ? 'is-active' : '') + '">Night</span>' +
    '</div><span class="camera-profile-source">' + escapeHtml((activeProfile.charAt(0).toUpperCase() + activeProfile.slice(1)) + ' · ' + profileSource) + '</span>' +
    (dayPreset ? '<span class="camera-profile-preset">Day: ' + escapeHtml(dayPreset.name) + '</span>' : '') +
    (nightPreset ? '<span class="camera-profile-preset">Night: ' + escapeHtml(nightPreset.name) + '</span>' : '');

  var rowHtml = '<tr data-camera-index="' + index + '" class="' + (isEnabled ? '' : 'camera-row-disabled') + '">';
  rowHtml += '<td class="cell-camera">';
  rowHtml += '<div class="cam-info"><span class="cam-name">' + name + '</span>' + (id ? '<span class="cam-id">ID · ' + id + '</span>' : '') + '</div>';
  rowHtml += '<div class="cell-actions"><button class="secondary cam-edit-btn" data-index="' + index + '" type="button" title="Edit camera" aria-label="Edit ' + name + '">' + ICONS.edit + '</button><button class="secondary cam-toggle-btn' + (isEnabled ? ' is-enabled' : ' is-disabled') + '" data-index="' + index + '" type="button" title="' + (isEnabled ? 'Disable camera' : 'Enable camera') + '" aria-label="' + (isEnabled ? 'Disable ' : 'Enable ') + name + '">' + ICONS.power + '</button><button class="delete-btn secondary cam-remove-btn" data-index="' + index + '" type="button" title="Remove camera" aria-label="Remove ' + name + '">' + ICONS.remove + '</button></div>';
  rowHtml += '</td>';
  rowHtml += '<td class="cell-connection" data-label="Connection"><span class="chip camera-backend-chip">' + backend + '</span><span class="camera-endpoint">' + endpoint + '</span></td>';
  rowHtml += '<td class="cell-video" data-label="Video"><strong>' + resolution + '</strong><span>' + escapeHtml(fpsText) + '</span></td>';
  rowHtml += '<td class="cell-state" data-label="Status">' + healthHtml + '<span class="camera-enabled-label">' + (isEnabled ? 'Enabled' : 'Configuration paused') + '</span></td>';
  rowHtml += '<td class="cell-profiles" data-label="Profiles">' + profilesHtml + '</td>';
  rowHtml += '<td class="cell-ptz" data-label="PTZ"><span class="camera-feature-pill ' + (ptzEnabled ? 'is-ready' : '') + '">' + (ptzEnabled ? 'PTZ Enabled' : 'Fixed') + '</span></td>';
  rowHtml += '</tr>';
  return rowHtml;
}

// ─── Click-to-sort column headers ─────────────────────────────────────────
// Headers re-order the currently displayed cameras client-side. `null` means
// the saved config order applies; clicking a column cycles asc → desc → back
// to config order. The sort survives health / resolution refreshes and filter
// changes, and clears on Reset Filters.
let cameraSortState = null;
let openCameraEditIndex = null;

function cameraSortValue(camera, key) {
  switch (key) {
    case 'camera': return String(camera.name || camera.id || '').toLowerCase();
    case 'connection': {
      const host = String(camera.host || '').trim() || String(camera.stream_url || '').trim();
      return (String(camera.backend || 'onvif') + ' ' + host).toLowerCase();
    }
    case 'video': {
      const fps = cameraFps[camera.id];
      if (fps && fps.source === 'detected' && Number(fps.detected) > 0) return Number(fps.detected);
      if (fps && fps.source === 'configured' && Number(fps.configured) > 0) return Number(fps.configured);
      return Number(camera.fps) > 0 ? Number(camera.fps) : 0;
    }
    case 'status': {
      if (camera.enabled === false) return 3; // disabled always sorts last
      const health = cameraHealth[camera.id];
      if (!health) return 2; // checking (no health payload yet)
      return health.online ? 0 : 1;
    }
    case 'ptz': return camera.ptz?.enabled === true ? 1 : 0;
    default: return 0;
  }
}

function compareCameras(left, right) {
  if (!cameraSortState) return 0;
  const leftValue = cameraSortValue(left, cameraSortState.key);
  const rightValue = cameraSortValue(right, cameraSortState.key);
  let result;
  if (typeof leftValue === 'number' && typeof rightValue === 'number') {
    result = leftValue - rightValue;
  } else {
    result = String(leftValue).localeCompare(String(rightValue), undefined, { numeric: true, sensitivity: 'base' });
  }
  return cameraSortState.dir === 'asc' ? result : -result;
}

function renderCameraSortHeader(label, key) {
  const active = cameraSortState && cameraSortState.key === key;
  const ariaSort = active ? (cameraSortState.dir === 'asc' ? 'ascending' : 'descending') : 'none';
  const glyph = active ? (cameraSortState.dir === 'asc' ? '▲' : '▼') : '⇅';
  const cls = active ? 'table-sort-btn is-active' : 'table-sort-btn';
  return `<th scope="col" aria-sort="${ariaSort}"><button type="button" class="${cls}" data-sort-key="${key}" aria-label="Sort by ${label}">${label}<span class="table-sort-glyph" aria-hidden="true">${glyph}</span></button></th>`;
}

function bindCameraSortHeaders() {
  gridEl.querySelectorAll('[data-sort-key]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sortKey;
      if (cameraSortState && cameraSortState.key === key) {
        cameraSortState = cameraSortState.dir === 'asc'
          ? { key, dir: 'desc' }
          : null;
      } else {
        cameraSortState = { key, dir: 'asc' };
      }
      renderGrid();
    });
  });
}

// ─── Filter ───────────────────────────────────────────────────────────

function currentFilterValues() {
  return {
    text: (filter.text?.value || '').trim().toLowerCase(),
    backend: filter.backend?.value || '',
  };
}

function applyFilter(list) {
  var vals = currentFilterValues();
  return list.filter(function(camera) {
    if (vals.backend && (camera.backend || 'onvif') !== vals.backend) return false;
    if (!vals.text) return true;
    var haystack = (camera.name || '').toLowerCase() + ' ' + (camera.id || '').toLowerCase();
    return haystack.indexOf(vals.text) !== -1;
  });
}

function updateFilterHint(filteredCount) {
  var vals = currentFilterValues();
  var parts = [];
  if (vals.text) parts.push('matching \u201c' + vals.text + '\u201d');
  if (vals.backend === 'onvif') parts.push('using ONVIF');
  else if (vals.backend === 'rtsp') parts.push('using RTSP');
  if (!parts.length) {
    messageEl.textContent = cameras.length ? ('Showing all ' + cameras.length + ' cameras.') : '';
    return;
  }
  messageEl.textContent = 'Showing ' + filteredCount + ' of ' + cameras.length + ' cameras ' + parts.join(' and ') + '.';
}

function renderGrid() {
  var filtered = applyFilter(cameras);
  if (cameras.length === 0) {
    gridEl.innerHTML = '';
    emptyEl.hidden = false;
    updateFilterHint(0);
    return;
  }
  emptyEl.hidden = true;
  if (filtered.length === 0) {
    gridEl.innerHTML = '<div class="camera-empty-state"><div class="camera-empty-icon" aria-hidden="true"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><h2>No cameras match these filters</h2><p class="muted">Try clearing the search or selecting a different backend.</p></div>';
    updateFilterHint(0);
    return;
  }
  // Sort a copy of the filtered list so the rows keep their real config
  // index (edit / toggle / delete / drag handlers all address the cameras
  // array by index, so the visual order must not disturb that mapping).
  var sorted = cameraSortState
    ? filtered.slice().sort(compareCameras)
    : filtered;
  var rowsHtml = sorted.map(function(cam) {
    var realIndex = cameras.indexOf(cam);
    var row = renderCameraRow(cam, realIndex);
    return realIndex === openCameraEditIndex
      ? row + buildEditFormHtml(cam, realIndex)
      : row;
  }).join('');
  var tableHtml = '<div class="cameras-table-wrap"><table class="cameras-table stack-table"><thead><tr>' +
    renderCameraSortHeader('Camera', 'camera') +
    renderCameraSortHeader('Connection', 'connection') +
    renderCameraSortHeader('Video', 'video') +
    renderCameraSortHeader('Status', 'status') +
    '<th scope="col">Profiles</th>' +
    renderCameraSortHeader('PTZ', 'ptz') +
    '</tr></thead><tbody>' + rowsHtml + '</tbody></table></div>';
  gridEl.innerHTML = tableHtml;
  updateFilterHint(filtered.length);
  if (openCameraEditIndex !== null) wireEditFormHandlers(openCameraEditIndex);

  gridEl.querySelectorAll('.cam-edit-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var idx = Number(btn.dataset.index);
      toggleEditForm(cameras[idx], idx);
    });
  });
  gridEl.querySelectorAll('.cam-toggle-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      var idx = Number(btn.dataset.index);
      if (!cameras[idx]) return;
      var camerasBefore = cameras.slice();
      var newEnabled = cameras[idx].enabled !== false ? false : true;
      var next = cameras.map(function(c, i) {
        if (i !== idx) return c;
        return { ...c, enabled: newEnabled };
      });
      try {
        var result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras: next }) });
        cameras = result.cameras || next;
        renderGrid();
        setMessage(newEnabled ? 'Camera enabled.' : 'Camera disabled.');
      } catch (err) {
        cameras.splice(0, cameras.length, ...camerasBefore);
        if (window.daygleAuth?.redirecting) return;
        setMessage(err.message, true);
      }
    });
  });
  gridEl.querySelectorAll('.cam-remove-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { openDeleteModal(Number(btn.dataset.index)); });
  });
  bindCameraSortHeaders();


}

// ─── Delete modal ─────────────────────────────────────────────────────────────

function openModal(el) {
  el.hidden = false;
  document.body.classList.add('modal-open');
  el.focus?.();
}

function closeModal(el) {
  el.hidden = true;
  document.body.classList.remove('modal-open');
}

function openDeleteModal(index) {
  pendingDeleteIndex = index;
  var camera = cameras[index];
  var name = camera?.name || camera?.id || ('Camera ' + (index + 1));
  document.getElementById('deleteModalBody').textContent = 'Remove "' + name + '" from your configuration? Existing recordings are kept.';
  openModal(deleteModal);
}

document.getElementById('deleteConfirmBtn').addEventListener('click', async function() {
  if (pendingDeleteIndex === null) return;
  var originalIndex = pendingDeleteIndex;
  var camerasBefore = cameras.slice();
  var payloadCameras = camerasBefore.slice(0, originalIndex).concat(camerasBefore.slice(originalIndex + 1));
  try {
    var result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras: payloadCameras }) });
    cameras = result.cameras || payloadCameras;
    renderGrid();
    setMessage('Camera removed.');
  } catch (err) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(err.message, true);
  }
  closeModal(deleteModal);
  pendingDeleteIndex = null;
});

// ─── Add camera ───────────────────────────────────────────────────────────────

function addNewCamera() {
  var newCam = { id: ('camera-' + (cameras.length + 1)), name: ('Camera ' + (cameras.length + 1)), enabled: true, backend: 'onvif', port: 554, path: '', recording: { continuous: false }, detection: {}, ptz: { enabled: false, protocol: 'onvif', http_port: 80, port: 6060, address: 1, speed: 5, step_duration: 0.4 } };
  cameras.push(newCam);
  renderGrid();
  toggleEditForm(newCam, cameras.length - 1);
}

// ─── Detected resolution ─────────────────────────────────────────────────────

async function fetchCameraResolutions() {
  await Promise.all(cameras.map(async function(camera) {
    if (!camera.id) return;
    try {
      var status = await api('/api/status?camera_id=' + encodeURIComponent(camera.id));
      if (status && status.resolution && status.resolution.width > 0 && status.resolution.height > 0) {
        cameraResolutions[camera.id] = status.resolution;
      } else {
        cameraResolutions[camera.id] = null;
      }
      if (status && status.fps && status.fps.effective > 0) {
        cameraFps[camera.id] = status.fps;
      } else {
        cameraFps[camera.id] = null;
      }
    } catch (_err) {
      delete cameraResolutions[camera.id];
      delete cameraFps[camera.id];
    }
  }));

  // Don't clobber an open inline edit form while the user is editing.
  if (document.querySelector('.camera-edit-row')) return;

  renderGrid();
}

document.getElementById('addCameraBtn').addEventListener('click', addNewCamera);
document.getElementById('addCameraEmptyBtn').addEventListener('click', addNewCamera);

// ─── Delete modal close ───────────────────────────────────────────────────────

document.getElementById('deleteModalCloseBtn').addEventListener('click', function() { closeModal(deleteModal); });
document.getElementById('deleteCancelBtn').addEventListener('click', function() { closeModal(deleteModal); });

deleteModal.addEventListener('click', function(e) { if (e.target === deleteModal) closeModal(deleteModal); });

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    var openEdits = gridEl.querySelector('.camera-edit-row');
    if (openEdits) { closeAllEditForms(); return; }
    if (!deleteModal.hidden) closeModal(deleteModal);
  }
});

// ─── Filter handlers ──────────────────────────────────────────────────────────

filter.text?.addEventListener('input', function() { renderGrid(); });
filter.backend?.addEventListener('change', function() { renderGrid(); });
filter.reset?.addEventListener('click', function() {
  // Reset Filters also clears any active column sort (mirrors the recordings
  // page), so the table returns to the config order the user sees on load.
  cameraSortState = null;
  setTimeout(function() { renderGrid(); }, 0);
});
filter.form?.addEventListener('submit', function(e) { e.preventDefault(); });

window.daygleDatePrefsChanged = function daygleDatePrefsChanged() { /* no-op */ };

// ─── Load ─────────────────────────────────────────────────────────────────────

async function loadCameras() {
  await window.daygleAuthReady;
  var settings = await api('/api/settings/system');
  cameras = settings.cameras || (settings.camera ? [settings.camera] : []);
  cameraProfilePresets = settings.profile_presets || [];
  // Clear stale entries so removed cameras don't linger.
  Object.keys(cameraResolutions).forEach(function(key) { delete cameraResolutions[key]; });
  Object.keys(cameraFps).forEach(function(key) { delete cameraFps[key]; });
  renderGrid();
  fetchCameraResolutions().catch(function() {});
}

async function updateHealthStats() {
  try {
    var data = await api('/api/cameras/health');
    Object.keys(cameraHealth).forEach(function(key) { delete cameraHealth[key]; });
    Object.keys(data.cameras || {}).forEach(function(cameraId) {
      cameraHealth[cameraId] = data.cameras[cameraId];
    });
    // Keep an open inline editor intact during the periodic health refresh.
    if (!document.querySelector('.camera-edit-row')) renderGrid();
  } catch (_err) {
    // silently ignore
  }
}

loadCameras().catch(function(err) {
  if (window.daygleAuth?.redirecting) return;
  setMessage(err.message, true);
});
// Health + resolution polling is suspended while the tab is hidden and both
// refresh on refocus, so a background camera list stops hammering /api/cameras
// (startPageInterval, web/utils.js).
startPageInterval(updateHealthStats, 10000);
startPageInterval(function() { fetchCameraResolutions().catch(function() {}); }, 10000);
updateHealthStats();
