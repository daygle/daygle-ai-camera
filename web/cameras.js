let cameras = [];
let cameraProfilePresets = [];
let pendingDeleteIndex = null;
const cameraResolutions = {};
const cameraFps = {};

const messageEl = document.getElementById('cameraMessage');
const gridEl = document.getElementById('cameraGrid');
const emptyEl = document.getElementById('cameraEmpty');
const deleteModal = document.getElementById('deleteModal');

// Stats + filter state
const cameraHealth = {};
const stats = {
  total: document.getElementById('statTotalCameras'),
  online: document.getElementById('statOnlineCameras'),
  offline: document.getElementById('statOfflineCameras'),
};
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

const PROFILE_PERFORMANCE_FIELDS = [
  'background_detection_enabled', 'detection_interval_seconds',
  'ingest_frame_fps', 'detection_confirm_frames', 'detection_confirm_window',
  'detection_confirm_iou', 'always_run_object_detection',
  'object_detection_region_boost', 'object_detection_tiling',
  'periodic_scan_interval_seconds', 'motion_frame_width', 'motion_frame_height',
];
const PROFILE_MOTION_FIELDS = [
  'motion_pixel_threshold', 'motion_gate_fraction', 'motion_scale_fraction',
  'motion_background_alpha', 'motion_algorithm', 'motion_denoise',
  'motion_shadow_suppression',
];
const PROFILE_FIELDS = PROFILE_PERFORMANCE_FIELDS.concat(PROFILE_MOTION_FIELDS);

function profilePresetOptionsHtml(mode, selectedId) {
  return cameraProfilePresets.filter(function(preset) { return preset.mode === mode; }).map(function(preset) {
    return '<option value="' + escapeHtml(preset.id) + '"' + (preset.id === selectedId ? ' selected' : '') + '>' + escapeHtml(preset.name) + (preset.builtin ? ' (Built-in)' : '') + '</option>';
  }).join('');
}

function cameraProfileValue(camera, mode, key) {
  const profiles = camera.detection_profiles || {};
  const profile = profiles[mode] || {};
  if (Object.prototype.hasOwnProperty.call(profile, key)) return profile[key];
  if (mode === (profiles.active || 'day')) return camera[key];
  return null;
}

// Field-name prefix for the split Day/Night profile editors. Performance
// controls keep their historical `profile_<key>` suffix under a mode prefix
// (`day_profile_...`), motion controls are prefixed directly (`day_motion_...`).
function profileFieldName(key) {
  return PROFILE_PERFORMANCE_FIELDS.includes(key) ? 'profile_' + key : key;
}

function parseProfileFieldValue(key, raw) {
  if (raw === '') return null;
  switch (key) {
    case 'motion_pixel_threshold':
    case 'ingest_frame_fps':
    case 'detection_confirm_frames':
    case 'detection_confirm_window':
    case 'periodic_scan_interval_seconds':
    case 'motion_frame_width':
    case 'motion_frame_height': {
      const intValue = parseInt(raw, 10);
      return Number.isNaN(intValue) ? null : intValue;
    }
    case 'detection_interval_seconds':
    case 'detection_confirm_iou':
    case 'motion_gate_fraction':
    case 'motion_scale_fraction':
    case 'motion_background_alpha': {
      const numberValue = Number(raw);
      return Number.isNaN(numberValue) ? null : numberValue;
    }
    case 'background_detection_enabled':
    case 'always_run_object_detection':
    case 'object_detection_region_boost':
    case 'motion_denoise':
      return raw === 'true';
    default:
      return raw;
  }
}

// Read one profile (day or night) from its own field group. A missing or
// empty control reads as null, i.e. "Global Default" / inherit.
function readProfileFromForm(form, mode) {
  const profile = {};
  PROFILE_FIELDS.forEach(function(key) {
    const field = form.querySelector('[name="' + mode + '_' + profileFieldName(key) + '"]');
    const raw = field ? String(field.value).trim() : '';
    profile[key] = parseProfileFieldValue(key, raw);
  });
  return profile;
}

// One independently editable profile block (Day or Night). Both blocks render
// side by side, each from ITS OWN stored values, so switching the Active
// Profile select never re-fills or discards what is typed here.
function profileSectionHtml(camera, mode) {
  const label = mode === 'night' ? 'Night' : 'Day';
  const attr = (value) => escapeHtml(value == null ? '' : String(value));
  const storedValue = (key) => {
    const value = cameraProfileValue(camera, mode, key);
    return value === undefined ? null : value;
  };
  const fieldName = (key) => mode + '_' + profileFieldName(key);
  const infoTip = (text) => '<span class="info-tip" data-tip="' + escapeHtml(text) + '" title="' + escapeHtml(text) + '" tabindex="0" aria-label="Help: ' + escapeHtml(text) + '"></span>';
  const labelSpan = (opts) => '<span>' + opts.label + (opts.tip ? ' ' + infoTip(opts.tip) : '') + '</span>';
  const numberField = (key, opts) =>
    '<label>' + labelSpan(opts) +
    '<input name="' + fieldName(key) + '" type="number" min="' + opts.min + '" max="' + opts.max + '" step="' + opts.step +
    '" placeholder="' + escapeHtml(opts.placeholder) + '" value="' + attr(storedValue(key) ?? '') + '" /></label>';
  const selectField = (key, opts) => {
    const current = storedValue(key);
    const items = [{ value: null, attr: '', label: 'Global Default' }].concat(opts.options);
    return '<label>' + labelSpan(opts) + '<select name="' + fieldName(key) + '">' +
      items.map(function(item) {
        return '<option value="' + escapeHtml(item.attr) + '"' + (current === item.value ? ' selected' : '') + '>' + item.label + '</option>';
      }).join('') +
      '</select></label>';
  };
  const boolOptions = [
    { value: true, attr: 'true', label: 'Enabled' },
    { value: false, attr: 'false', label: 'Disabled' },
  ];
  const performanceControls =
    selectField('background_detection_enabled', { label: 'Background Detection', options: boolOptions }) +
    numberField('detection_interval_seconds', { label: 'Detection Interval (s)', min: '0.1', max: '10', step: '0.05', placeholder: 'Global Default (0.5)' }) +
    numberField('ingest_frame_fps', { label: 'Detection Frame Rate (fps)', min: '1', max: '30', step: '1', placeholder: 'Global Default (4)' }) +
    numberField('detection_confirm_frames', { label: 'Confirm Frames', min: '1', max: '10', step: '1', placeholder: 'Global Default (1)' }) +
    numberField('detection_confirm_window', { label: 'Confirm Window', min: '1', max: '30', step: '1', placeholder: 'Global Default (1)' }) +
    numberField('detection_confirm_iou', { label: 'Confirm Location (IoU)', min: '0', max: '0.9', step: '0.05', placeholder: 'Global Default (0)' }) +
    selectField('always_run_object_detection', { label: 'Always Run Object Detection', options: boolOptions }) +
    selectField('object_detection_region_boost', { label: 'Region Boost', options: boolOptions }) +
    selectField('object_detection_tiling', { label: 'Object Detection Tiling', options: [
      { value: 'off', attr: 'off', label: 'Off' },
      { value: '2x2', attr: '2x2', label: '2 × 2' },
      { value: '3x3', attr: '3x3', label: '3 × 3' },
      { value: '4x4', attr: '4x4', label: '4 × 4' },
    ] }) +
    numberField('periodic_scan_interval_seconds', { label: 'Periodic Scan (s)', min: '0', max: '3600', step: '1', placeholder: 'Global Default (0)' }) +
    numberField('motion_frame_width', { label: 'Motion Frame Width', min: '40', max: '640', step: '1', placeholder: 'Global Default (320)' }) +
    numberField('motion_frame_height', { label: 'Motion Frame Height', min: '30', max: '480', step: '1', placeholder: 'Global Default (240)' });
  const motionControls =
    numberField('motion_pixel_threshold', { label: 'Pixel Threshold', tip: 'Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras.', min: '1', max: '255', step: '1', placeholder: 'Global Default (30)' }) +
    numberField('motion_gate_fraction', { label: 'Gate Fraction', tip: 'Minimum fraction of pixels that must change before motion is declared.', min: '0.0001', max: '0.5', step: '0.0001', placeholder: 'Global Default (0.005)' }) +
    numberField('motion_scale_fraction', { label: 'Scale Fraction', tip: 'Pixel change fraction that maps to 100% motion confidence.', min: '0.001', max: '1.0', step: '0.001', placeholder: 'Global Default (0.03)' }) +
    numberField('motion_background_alpha', { label: 'Background Alpha', tip: 'How fast the background model adapts when no motion is detected.', min: '0.001', max: '0.5', step: '0.001', placeholder: 'Global Default (0.05)' }) +
    selectField('motion_algorithm', { label: 'Motion Engine', tip: 'Background-subtraction engine for this camera. Leave on Global Default unless this camera needs a different engine.', options: [
      { value: 'mog2', attr: 'mog2', label: 'MOG2' },
      { value: 'diff', attr: 'diff', label: 'Diff (Legacy)' },
    ] }) +
    selectField('motion_denoise', { label: 'Denoise', tip: 'Morphological denoise of the motion mask for this camera. Leave on Global Default to follow the global setting.', options: boolOptions }) +
    selectField('motion_shadow_suppression', { label: 'Shadow Suppression', tip: 'Reject cast shadows from motion alerts for this camera (MOG2 only). It does not filter YOLO object detections. On = always; Off = never (dark/IR scenes); Automatic = only while bright. Leave on Global Default to follow the global setting.', options: [
      { value: 'on', attr: 'on', label: 'On' },
      { value: 'off', attr: 'off', label: 'Off' },
      { value: 'auto', attr: 'auto', label: 'Automatic (Day Only)' },
    ] });
  return '<div class="cam-edit-section">' +
    '<h4 class="cam-edit-section-title">' + label + ' Profile</h4>' +
    '<p class="form-help muted">' + label + ' settings apply while the ' + label + ' profile is active. Leave a value on Global Default to follow the live detection settings.</p>' +
    '<div class="form-grid">' + performanceControls + '</div>' +
    '<p class="form-help muted">' + label + ' motion overrides for this camera only. Leave blank to use the global defaults from Live Detection settings.</p>' +
    '<div class="form-grid">' + motionControls + '</div>' +
    '</div>';
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
      '<button class="modal-tab active" data-tab="connection" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="true">Connection</button>' +
      '<button class="modal-tab" data-tab="recording" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="false" tabindex="-1">Recording</button>' +
      '<button class="modal-tab" data-tab="ptz" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="false" tabindex="-1">PTZ</button>' +
      '<button class="modal-tab" data-tab="advanced" data-form="' + htmlAttr(formId) + '" type="button" role="tab" aria-selected="false" tabindex="-1">Advanced</button>' +
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
          '<h4 class="cam-edit-section-title">Streams</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '><span>Detection Stream Path</span><input name="path" placeholder="e.g. stream1" value="' + escapeHtml(camera.path || '') + '" /></label>' +
            '<label class="full-width"><span>Recording Stream Path <span class="info-tip" data-tip="Optional: the path for the high-res recording stream (e.g. stream2). Leave empty to use the primary stream for recording." title="Optional: the path for the high-res recording stream (e.g. stream2). Leave empty to use the primary stream for recording." tabindex="0" aria-label="Help: Optional path for the high-res recording stream."></span></span><input name="recording_stream_path" placeholder="e.g. stream2" value="' + escapeHtml(camera.recording_stream_path || '') + '" /></label>' +
          '</div>' +
          '<p class="form-help muted">Recording Stream Path is optional and points to a higher-resolution stream used for recordings. Leave empty to use the primary stream for both detection and recording.</p>' +
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
            '<label><span>HTTP Port <span class="info-tip" data-tip="Camera web port used by HTTP CGI (default 80)." title="Camera web port used by HTTP CGI (default 80)." tabindex="0" aria-label="Help: Camera web port used by HTTP CGI (default 80)."></span></span><input name="ptz_http_port" type="number" min="1" max="65535" placeholder="80" value="' + htmlAttr(camera.ptz?.http_port || 80) + '" /></label>' +
            '<label><span>Command Port <span class="info-tip" data-tip="Port for TCP PelcoD only (default 6060)." title="Port for TCP PelcoD only (default 6060)." tabindex="0" aria-label="Help: Port for TCP PelcoD only (default 6060)."></span></span><input name="ptz_port" type="number" min="1" max="65535" placeholder="6060" value="' + htmlAttr(camera.ptz?.port || 6060) + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Movement</h4>' +
          '<div class="form-grid">' +
            '<label><span>PTZ Address <span class="info-tip" data-tip="PelcoD device address (default 1, TCP PelcoD only)." title="PelcoD device address (default 1, TCP PelcoD only)." tabindex="0" aria-label="Help: PelcoD device address (default 1, TCP PelcoD only)."></span></span><input name="ptz_address" type="number" min="1" max="255" placeholder="1" value="' + htmlAttr(camera.ptz?.address || 1) + '" /></label>' +
            '<label><span>Speed <span class="info-tip" data-tip="Movement speed (1-8, default 5)." title="Movement speed (1-8, default 5)." tabindex="0" aria-label="Help: Movement speed (1-8, default 5)."></span></span><input name="ptz_speed" type="number" min="1" max="8" placeholder="5" value="' + htmlAttr(camera.ptz?.speed || 5) + '" /></label>' +
            '<label class="full-width"><span>Step Duration (s) <span class="info-tip" data-tip="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." title="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." tabindex="0" aria-label="Help: How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)."></span></span><input name="ptz_step_duration" type="number" min="0.1" max="5" step="0.1" placeholder="0.4" value="' + htmlAttr(camera.ptz?.step_duration != null ? Number(camera.ptz.step_duration).toFixed(2) : '') + '" /></label>' +
          '</div>' +
          '<p class="form-help muted">Enable PTZ and save to show the control pad on the Live page. The camera&#39;s username and password from the Connection tab are used for HTTP CGI authentication.</p>' +
        '</div>' +
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
            '<label><span>PTZ Motion Detection</span><select name="ptz_motion_detection">' +
              '<option value="auto"' + ((camera.detection?.ptz_motion_detection || 'auto') === 'auto' ? ' selected' : '') + '>Auto (Follow PTZ)</option>' +
              '<option value="on"' + (camera.detection?.ptz_motion_detection === 'on' ? ' selected' : '') + '>On</option>' +
              '<option value="off"' + (camera.detection?.ptz_motion_detection === 'off' ? ' selected' : '') + '>Off</option>' +
            '</select></label>' +
          '</div>' +
          '<div class="button-row">' +
            '<button type="button" class="secondary profile-suggest-btn">Suggest Sunrise/Sunset</button>' +
            '<span class="form-help muted profile-action-result" aria-live="polite"></span>' +
          '</div>' +
          '<p class="form-help muted">Choose which profile is active right now. Solar mode refreshes sunrise/sunset times daily using this camera’s coordinates and timezone. The Day and Night profiles below are edited independently - each keeps its own performance and motion overrides, and values left on Global Default follow the live detection settings. Existing cameras inherit their legacy settings into both profiles.</p>' +
          '<p class="form-help muted">Runtime: <strong>' + escapeHtml(runtimeActive.charAt(0).toUpperCase() + runtimeActive.slice(1)) + '</strong> (' + escapeHtml(runtimeSourceLabel) + '). ' + escapeHtml(runtimeNote) + '</p>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Profile Presets</h4>' +
          '<p class="form-help muted">Day and Night each have their own mode-specific presets. Each preset contains settings for only one mode, so applying a Night preset can never change the Day profile. Nothing is saved until you save the camera.</p>' +
          '<div class="form-grid profile-preset-grid">' +
            '<label><span>Day Preset</span><select name="profile_day_preset"><option value="">Choose a Preset…</option>' + profilePresetOptionsHtml('day', camera.detection_profiles?.day_preset_id) + '</select></label>' +
            '<label><span>Night Preset</span><select name="profile_night_preset"><option value="">Choose a Preset…</option>' + profilePresetOptionsHtml('night', camera.detection_profiles?.night_preset_id) + '</select></label>' +
          '</div>' +
          '<div class="button-row profile-preset-row">' +
            '<button type="button" class="secondary profile-apply-day-btn">Apply to Day</button>' +
            '<button type="button" class="secondary profile-apply-night-btn">Apply to Night</button>' +
            '<button type="button" class="secondary profile-save-day-preset-btn">Save Day Preset</button>' +
            '<button type="button" class="secondary profile-save-night-preset-btn">Save Night Preset</button>' +
            '<button type="button" class="secondary profile-update-day-preset-btn" disabled>Update Day Preset</button>' +
            '<button type="button" class="secondary profile-update-night-preset-btn" disabled>Update Night Preset</button>' +
            '<button type="button" class="secondary profile-delete-day-preset-btn" disabled>Delete Day Preset</button>' +
            '<button type="button" class="secondary profile-delete-night-preset-btn" disabled>Delete Night Preset</button>' +
          '</div>' +
        '</div>' +
        profileSectionHtml(camera, 'day') +
        profileSectionHtml(camera, 'night') +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Stream</h4>' +
          '<div class="form-grid">' +
            '<label><span>FPS <span class="info-tip" data-tip="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." title="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." tabindex="0" aria-label="Help: Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong."></span></span><input name="fps" type="number" min="1" max="120" placeholder="Auto" value="' + htmlAttr(camera.fps != null ? camera.fps : '') + '" /></label>' +
            '<label><span>Frame Buffer Drains <span class="info-tip" data-tip="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)." title="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)." tabindex="0" aria-label="Help: Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)."></span></span><input name="stale_frame_grabs" type="number" min="0" max="20" placeholder="Auto" value="' + htmlAttr(camera.stale_frame_grabs != null ? camera.stale_frame_grabs : '') + '" /></label>' +
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

  // Backend toggle
  var backendSelect = form.querySelector('[name="backend"]');
  if (backendSelect) {
    backendSelect.addEventListener('change', function() {
      var manual = this.value === 'rtsp';
      form.querySelectorAll('.cam-rtsp-fields').forEach(function(el) { el.hidden = !manual; });
      form.querySelectorAll('.cam-onvif-fields').forEach(function(el) { el.hidden = manual; });
    });
  }

  // Day and Night each have their own always-visible editor section, so the
  // Active Profile select is purely the runtime choice now. Changing it must
  // NOT re-fill any field: the old handler reloaded the stored camera into
  // the form and silently discarded unsaved edits (settings appeared to
  // revert whenever the profile select was touched).

  var suggestButton = form.querySelector('.profile-suggest-btn');
  var dayPresetSelect = form.querySelector('[name="profile_day_preset"]');
  var nightPresetSelect = form.querySelector('[name="profile_night_preset"]');
  var applyDayButton = form.querySelector('.profile-apply-day-btn');
  var applyNightButton = form.querySelector('.profile-apply-night-btn');
  var savePresetButtons = {
    day: form.querySelector('.profile-save-day-preset-btn'),
    night: form.querySelector('.profile-save-night-preset-btn'),
  };
  var updatePresetButtons = {
    day: form.querySelector('.profile-update-day-preset-btn'),
    night: form.querySelector('.profile-update-night-preset-btn'),
  };
  var deletePresetButtons = {
    day: form.querySelector('.profile-delete-day-preset-btn'),
    night: form.querySelector('.profile-delete-night-preset-btn'),
  };
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
  function selectedPreset(mode) {
    var select = mode === 'day' ? dayPresetSelect : nightPresetSelect;
    return cameraProfilePresets.find(function(preset) { return preset.id === select?.value; });
  }
  function syncPresetButtons(mode) {
    var preset = selectedPreset(mode);
    var editable = Boolean(preset && !preset.builtin);
    if (updatePresetButtons[mode]) updatePresetButtons[mode].disabled = !editable;
    if (deletePresetButtons[mode]) deletePresetButtons[mode].disabled = !editable;
  }
  // Fill one profile section from a preset. The other mode and its selected
  // preset are never touched, so Day and Night remain fully independent.
  function applyPendingProfile(mode, preset, message) {
    var values = preset.settings || {};
    PROFILE_FIELDS.forEach(function(key) {
      var field = form.querySelector('[name="' + mode + '_' + profileFieldName(key) + '"]');
      if (!field) return;
      var hasValue = Object.prototype.hasOwnProperty.call(values, key) && values[key] != null;
      field.value = hasValue ? String(values[key]) : '';
    });
    form.__selectedPresetByMode = form.__selectedPresetByMode || {};
    form.__selectedPresetByMode[mode] = preset.id || null;
    var select = mode === 'day' ? dayPresetSelect : nightPresetSelect;
    if (select) select.value = preset.id || '';
    syncPresetButtons(mode);
    if (profileResult) profileResult.textContent = message;
  }
  function requestApplyPreset(mode) {
    var preset = selectedPreset(mode);
    if (!preset) {
      if (profileResult) profileResult.textContent = 'Choose a ' + (mode === 'day' ? 'Day' : 'Night') + ' preset first.';
      return;
    }
    var slotLabel = mode === 'day' ? 'the Day profile' : 'the Night profile';
    if (!window.confirm('Apply the ' + preset.name + ' preset to ' + slotLabel + '? The other profile will not change. Nothing is saved until you save the camera.')) return;
    applyPendingProfile(mode, preset, preset.name + ' loaded into ' + slotLabel + '. Review and save the camera.');
  }
  if (dayPresetSelect) dayPresetSelect.addEventListener('change', function() { syncPresetButtons('day'); });
  if (nightPresetSelect) nightPresetSelect.addEventListener('change', function() { syncPresetButtons('night'); });
  if (applyDayButton) applyDayButton.addEventListener('click', function() { requestApplyPreset('day'); });
  if (applyNightButton) applyNightButton.addEventListener('click', function() { requestApplyPreset('night'); });
  ['day', 'night'].forEach(function(mode) {
    var saveButton = savePresetButtons[mode];
    if (saveButton) saveButton.addEventListener('click', async function() {
      var modeLabel = mode === 'day' ? 'Day' : 'Night';
      var name = window.prompt('Name this ' + modeLabel + ' preset:');
      if (!name || !name.trim()) return;
      saveButton.disabled = true;
      try {
        var current = collectFormData(form).detection_profiles;
        var created = await api('/api/camera-profile-presets', {
          method: 'POST',
          body: JSON.stringify({ name: name.trim(), mode: mode, settings: current[mode] }),
        });
        cameraProfilePresets.push(created);
        var select = mode === 'day' ? dayPresetSelect : nightPresetSelect;
        select?.insertAdjacentHTML('beforeend', '<option value="' + escapeHtml(created.id) + '">' + escapeHtml(created.name) + '</option>');
        if (profileResult) profileResult.textContent = modeLabel + ' preset saved: ' + created.name + '.';
      } catch (err) {
        if (!window.daygleAuth?.redirecting && profileResult) profileResult.textContent = err.message || 'Could not save preset.';
      } finally { saveButton.disabled = false; }
    });
  });
  ['day', 'night'].forEach(function(mode) {
    var updateButton = updatePresetButtons[mode];
    if (updateButton) updateButton.addEventListener('click', async function() {
      var preset = selectedPreset(mode);
      if (!preset || preset.builtin) return;
      var modeLabel = mode === 'day' ? 'Day' : 'Night';
      if (!window.confirm('Update the ' + preset.name + ' preset with the current ' + modeLabel + ' values? The other values in the preset will not change.')) return;
      updateButton.disabled = true;
      try {
        var current = collectFormData(form).detection_profiles;
        var updated = await api('/api/camera-profile-presets/' + encodeURIComponent(preset.id), {
          method: 'PUT',
          body: JSON.stringify({
            name: preset.name,
            mode: mode,
            settings: current[mode],
          }),
        });
        cameraProfilePresets = cameraProfilePresets.map(function(item) { return item.id === updated.id ? updated : item; });
        if (profileResult) profileResult.textContent = 'Preset ' + modeLabel + ' values updated: ' + updated.name + '.';
      } catch (err) {
        if (!window.daygleAuth?.redirecting && profileResult) profileResult.textContent = err.message || 'Could not update preset.';
      } finally { syncPresetButtons(mode); }
    });

    var deleteButton = deletePresetButtons[mode];
    if (deleteButton) deleteButton.addEventListener('click', async function() {
      var preset = selectedPreset(mode);
      if (!preset || preset.builtin || !window.confirm('Delete the ' + preset.name + ' preset?')) return;
      deleteButton.disabled = true;
      try {
        await api('/api/camera-profile-presets/' + encodeURIComponent(preset.id), { method: 'DELETE' });
        cameraProfilePresets = cameraProfilePresets.filter(function(item) { return item.id !== preset.id; });
        [dayPresetSelect, nightPresetSelect].forEach(function(select) {
          if (select?.value !== preset.id) return;
          Array.from(select.options).find(function(option) { return option.value === preset.id; })?.remove();
          select.value = '';
          form.__selectedPresetByMode = form.__selectedPresetByMode || {};
          form.__selectedPresetByMode[select === dayPresetSelect ? 'day' : 'night'] = null;
        });
        if (profileResult) profileResult.textContent = 'Preset deleted.';
      } catch (err) {
        if (!window.daygleAuth?.redirecting && profileResult) profileResult.textContent = err.message || 'Could not delete preset.';
      } finally {
        syncPresetButtons('day');
        syncPresetButtons('night');
      }
    });
  });
  syncPresetButtons('day');
  syncPresetButtons('night');


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
      updateStats();
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
  var dayProfile = readProfileFromForm(form, 'day');
  var nightProfile = readProfileFromForm(form, 'night');
  var selectedPresetByMode = form.__selectedPresetByMode || {};
  var dayPresetId = Object.prototype.hasOwnProperty.call(selectedPresetByMode, 'day')
    ? (selectedPresetByMode.day || null)
    : (existingProfiles.day_preset_id || null);
  var nightPresetId = Object.prototype.hasOwnProperty.call(selectedPresetByMode, 'night')
    ? (selectedPresetByMode.night || null)
    : (existingProfiles.night_preset_id || null);
  var profiles = {
    active: activeProfile === 'night' ? 'night' : 'day',
    source: ['manual', 'schedule', 'solar', 'onvif'].includes(getName('profile_source')) ? getName('profile_source') : 'manual',
    day_preset_id: dayPresetId,
    night_preset_id: nightPresetId,
    day_start: getName('profile_day_start') || '07:00',
    night_start: getName('profile_night_start') || '19:00',
    day: { ...dayProfile },
    night: { ...nightProfile },
  };
  return {
    id: getName('id') || ('camera-' + (cameras.length + 1)),
    name: getName('name'),
    enabled: getVal('enabled') !== 'false',
    backend: backend,
    stream_url: backend === 'rtsp' ? getName('stream_url') : '',
    recording_stream_path: getName('recording_stream_path'),
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
  return configured ? configured + ' configured' : 'Auto';
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
  rowHtml += '<td class="cell-connection"><span class="chip camera-backend-chip">' + backend + '</span><span class="camera-endpoint">' + endpoint + '</span></td>';
  rowHtml += '<td class="cell-video"><strong>' + resolution + '</strong><span>' + escapeHtml(fpsText) + '</span></td>';
  rowHtml += '<td class="cell-state">' + healthHtml + '<span class="camera-enabled-label">' + (isEnabled ? 'Enabled' : 'Configuration paused') + '</span></td>';
  rowHtml += '<td class="cell-profiles">' + profilesHtml + '</td>';
  rowHtml += '<td class="cell-ptz"><span class="camera-feature-pill ' + (ptzEnabled ? 'is-ready' : '') + '">' + (ptzEnabled ? 'PTZ Enabled' : 'Fixed') + '</span></td>';
  rowHtml += '</tr>';
  return rowHtml;
}

// ─── Click-to-sort column headers ─────────────────────────────────────────
// Headers re-order the currently displayed cameras client-side. `null` means
// the config order (the order saved by drag-and-drop) applies; clicking a
// column cycles asc → desc → back to config order. The sort survives health /
// resolution refreshes and filter changes, and clears when the user drags a
// camera to reorder (drag is the config-order control, like the Sort By
// select on the recordings page).
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

// ─── Filter + stats ───────────────────────────────────────────────────────────

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
  var tableHtml = '<div class="cameras-table-wrap"><table class="cameras-table"><thead><tr>' +
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
        updateStats();
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

function updateStats() {
  if (stats.total) stats.total.textContent = String(cameras.length);
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
    updateStats();
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
  updateStats();
  renderGrid();
  fetchCameraResolutions().catch(function() {});
}

async function updateHealthStats() {
  try {
    var data = await api('/api/cameras/health');
    var s = data.summary;
    Object.keys(cameraHealth).forEach(function(key) { delete cameraHealth[key]; });
    Object.keys(data.cameras || {}).forEach(function(cameraId) {
      cameraHealth[cameraId] = data.cameras[cameraId];
    });
    if (stats.online) {
      var online = s.online || 0;
      stats.online.textContent = String(online);
      stats.online.style.color = online > 0 ? 'var(--success-color, #2ecc71)' : '';
    }
    if (stats.offline) {
      var offline = s.offline || 0;
      stats.offline.textContent = String(offline);
      stats.offline.style.color = offline > 0 ? 'var(--danger-color, #e74c3c)' : '';
    }
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
setInterval(updateHealthStats, 10000);
setInterval(function() { fetchCameraResolutions().catch(function() {}); }, 10000);
updateHealthStats();
