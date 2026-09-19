let cameras = [];
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

function cameraProfileValue(camera, mode, key) {
  const profiles = camera.detection_profiles || {};
  const profile = profiles[mode] || {};
  if (Object.prototype.hasOwnProperty.call(profile, key)) return profile[key];
  if (mode === (profiles.active || 'day')) return camera[key];
  return null;
}

function buildEditFormHtml(camera, index) {
  const backend = camera.backend || 'onvif';
  const isRtsp = backend === 'rtsp';
  const rowId = 'edit-row-' + index;
  const formId = 'edit-form-' + index;
  return '<tr class="camera-edit-row" id="' + rowId + '"><td colspan="6"><div class="camera-edit-panel">' +
    '<div class="cam-edit-head">' +
      '<span class="cam-edit-head-title">Editing <strong>' + escapeHtml(camera.name || camera.id || ('Camera ' + (index + 1))) + '</strong></span>' +
      (camera.id ? '<span class="cam-edit-head-id">ID · ' + escapeHtml(camera.id) + '</span>' : '') +
    '</div>' +
    '<div class="modal-tabs" role="tablist">' +
      '<button class="modal-tab active" data-tab="connection" data-form="' + formId + '" type="button" role="tab" aria-selected="true">Connection</button>' +
      '<button class="modal-tab" data-tab="recording" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">Recording</button>' +
      '<button class="modal-tab" data-tab="ptz" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">PTZ</button>' +
      '<button class="modal-tab" data-tab="advanced" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">Advanced</button>' +
    '</div>' +
    '<form class="camera-edit-form modal-body" data-camera-index="' + index + '" id="' + formId + '" novalidate autocomplete="off">' +
      '<input type="hidden" name="camera_index" value="' + index + '" />' +

      // Connection tab
      '<div class="modal-tab-panel" data-panel="connection">' +
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
              '<label><span>Port</span><input name="port" type="number" min="1" max="65535" placeholder="554" value="' + (camera.port || 554) + '" /></label>' +
              '<label><span>Username</span><input name="username" placeholder="admin" autocomplete="off" value="' + escapeHtml(camera.username || '') + '" /></label>' +
              '<label class="full-width"><span>Password</span><input name="password" type="password" autocomplete="new-password" placeholder="' + (camera.has_password ? '(saved - type to change)' : '(No Password)') + '" /></label>' +
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
        '<div class="button-row cam-test-conn-row">' +
          '<button class="btn-info cam-test-conn-btn" data-form="' + formId + '" type="button">Test Connection</button>' +
          '<span class="muted cam-test-conn-result" data-form="' + formId + '"></span>' +
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
            '<label><span>HTTP Port <span class="info-tip" data-tip="Camera web port used by HTTP CGI (default 80)." title="Camera web port used by HTTP CGI (default 80)." tabindex="0" aria-label="Help: Camera web port used by HTTP CGI (default 80)."></span></span><input name="ptz_http_port" type="number" min="1" max="65535" placeholder="80" value="' + (camera.ptz?.http_port || 80) + '" /></label>' +
            '<label><span>Command Port <span class="info-tip" data-tip="Port for TCP PelcoD only (default 6060)." title="Port for TCP PelcoD only (default 6060)." tabindex="0" aria-label="Help: Port for TCP PelcoD only (default 6060)."></span></span><input name="ptz_port" type="number" min="1" max="65535" placeholder="6060" value="' + (camera.ptz?.port || 6060) + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Movement</h4>' +
          '<div class="form-grid">' +
            '<label><span>PTZ Address <span class="info-tip" data-tip="PelcoD device address (default 1, TCP PelcoD only)." title="PelcoD device address (default 1, TCP PelcoD only)." tabindex="0" aria-label="Help: PelcoD device address (default 1, TCP PelcoD only)."></span></span><input name="ptz_address" type="number" min="1" max="255" placeholder="1" value="' + (camera.ptz?.address || 1) + '" /></label>' +
            '<label><span>Speed <span class="info-tip" data-tip="Movement speed (1-8, default 5)." title="Movement speed (1-8, default 5)." tabindex="0" aria-label="Help: Movement speed (1-8, default 5)."></span></span><input name="ptz_speed" type="number" min="1" max="8" placeholder="5" value="' + (camera.ptz?.speed || 5) + '" /></label>' +
            '<label class="full-width"><span>Step Duration (s) <span class="info-tip" data-tip="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." title="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." tabindex="0" aria-label="Help: How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)."></span></span><input name="ptz_step_duration" type="number" min="0.1" max="5" step="0.1" placeholder="0.4" value="' + (camera.ptz?.step_duration != null ? Number(camera.ptz.step_duration).toFixed(2) : '') + '" /></label>' +
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
              '<option value="onvif"' + (camera.detection_profiles?.source === 'onvif' ? ' selected' : '') + '>ONVIF IR state (fallback schedule)</option>' +
            '</select></label>' +
            '<label><span>Day Starts</span><input name="profile_day_start" type="time" value="' + escapeHtml(camera.detection_profiles?.day_start || '07:00') + '" /></label>' +
            '<label><span>Night Starts</span><input name="profile_night_start" type="time" value="' + escapeHtml(camera.detection_profiles?.night_start || '19:00') + '" /></label>' +
            '<label><span>Camera Timezone</span><input name="timezone" placeholder="e.g. Australia/Sydney" value="' + escapeHtml(camera.timezone || 'UTC') + '" /></label>' +
            '<label><span>Latitude</span><input name="latitude" type="number" min="-90" max="90" step="0.000001" placeholder="e.g. -33.8688" value="' + (camera.latitude != null ? camera.latitude : '') + '" /></label>' +
            '<label><span>Longitude</span><input name="longitude" type="number" min="-180" max="180" step="0.000001" placeholder="e.g. 151.2093" value="' + (camera.longitude != null ? camera.longitude : '') + '" /></label>' +
          '</div>' +
          '<div class="button-row">' +
            '<button type="button" class="secondary profile-suggest-btn">Suggest Sunrise/Sunset</button>' +
            '<button type="button" class="secondary ir-check-btn">Check IR State Now</button>' +
            '<span class="form-help muted profile-action-result" aria-live="polite"></span>' +
          '</div>' +
          '<p class="form-help muted">Choose which profile is active now. The motion overrides below are edited for the selected profile. Existing cameras inherit their legacy settings into both profiles.</p>' +
          '<p class="form-help muted">Runtime: <strong>' + escapeHtml(camera.profile_status?.active || camera.detection_profiles?.active || 'day') + '</strong> (' + escapeHtml(camera.profile_status?.selected_by || camera.detection_profiles?.source || 'manual') + '). ONVIF IR detection falls back to the schedule when unsupported.</p>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Day/Night Performance</h4>' +
          '<p class="form-help muted">These settings override Live Performance for this camera and profile. Leave values at their defaults unless this camera needs different day/night resource usage.</p>' +
          '<div class="form-grid">' +
            '<label><span>Background Detection</span><select name="profile_background_detection_enabled">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'background_detection_enabled') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="true"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'background_detection_enabled') === true ? ' selected' : '') + '>Enabled</option>' +
              '<option value="false"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'background_detection_enabled') === false ? ' selected' : '') + '>Disabled</option>' +
            '</select></label>' +
            '<label><span>Detection Interval (s)</span><input name="profile_detection_interval_seconds" type="number" min="0.1" max="10" step="0.05" placeholder="Global default (0.5)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'detection_interval_seconds') ?? '') + '" /></label>' +
            '<label><span>Detection Frame Rate (fps)</span><input name="profile_ingest_frame_fps" type="number" min="1" max="30" step="1" placeholder="Global default (4)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'ingest_frame_fps') ?? '') + '" /></label>' +
            '<label><span>Confirm Frames</span><input name="profile_detection_confirm_frames" type="number" min="1" max="10" step="1" placeholder="Global default (2)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'detection_confirm_frames') ?? '') + '" /></label>' +
            '<label><span>Confirm Window</span><input name="profile_detection_confirm_window" type="number" min="1" max="30" step="1" placeholder="Global default (3)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'detection_confirm_window') ?? '') + '" /></label>' +
            '<label><span>Confirm Location (IoU)</span><input name="profile_detection_confirm_iou" type="number" min="0" max="0.9" step="0.05" placeholder="Global default (0)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'detection_confirm_iou') ?? '') + '" /></label>' +
            '<label><span>Always Run Object Detection</span><select name="profile_always_run_object_detection">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'always_run_object_detection') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="true"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'always_run_object_detection') === true ? ' selected' : '') + '>Enabled</option>' +
              '<option value="false"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'always_run_object_detection') === false ? ' selected' : '') + '>Disabled</option>' +
            '</select></label>' +
            '<label><span>Region Boost</span><select name="profile_object_detection_region_boost">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_region_boost') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="true"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_region_boost') === true ? ' selected' : '') + '>Enabled</option>' +
              '<option value="false"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_region_boost') === false ? ' selected' : '') + '>Disabled</option>' +
            '</select></label>' +
            '<label><span>Object Detection Tiling</span><select name="profile_object_detection_tiling">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_tiling') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="off"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_tiling') === 'off' ? ' selected' : '') + '>Off</option>' +
              '<option value="2x2"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_tiling') === '2x2' ? ' selected' : '') + '>2 × 2</option>' +
              '<option value="3x3"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_tiling') === '3x3' ? ' selected' : '') + '>3 × 3</option>' +
              '<option value="4x4"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'object_detection_tiling') === '4x4' ? ' selected' : '') + '>4 × 4</option>' +
            '</select></label>' +
            '<label><span>Periodic Scan (s)</span><input name="profile_periodic_scan_interval_seconds" type="number" min="0" max="3600" step="1" placeholder="Global default (0)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'periodic_scan_interval_seconds') ?? '') + '" /></label>' +
            '<label><span>Motion Frame Width</span><input name="profile_motion_frame_width" type="number" min="40" max="640" step="1" placeholder="Global default (320)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_frame_width') ?? '') + '" /></label>' +
            '<label><span>Motion Frame Height</span><input name="profile_motion_frame_height" type="number" min="30" max="480" step="1" placeholder="Global default (240)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_frame_height') ?? '') + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Stream</h4>' +
          '<div class="form-grid">' +
            '<label><span>FPS <span class="info-tip" data-tip="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." title="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." tabindex="0" aria-label="Help: Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong."></span></span><input name="fps" type="number" min="1" max="120" placeholder="Auto" value="' + (camera.fps != null ? camera.fps : '') + '" /></label>' +
            '<label><span>Frame Buffer Drains <span class="info-tip" data-tip="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)." title="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)." tabindex="0" aria-label="Help: Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)."></span></span><input name="stale_frame_grabs" type="number" min="0" max="20" placeholder="Auto" value="' + (camera.stale_frame_grabs != null ? camera.stale_frame_grabs : '') + '" /></label>' +
          '</div>' +
          '<p class="form-help muted">Frame-buffer drains is a hint passed to the stream decoder. Leave FPS empty to auto-detect it from the stream; override it only if the detected value is wrong.</p>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Motion Detection Overrides</h4>' +
          '<p class="form-help muted">Override the global motion settings for this camera only. Leave blank to use the global defaults from Live Detection settings.</p>' +
          '<div class="form-grid">' +
            '<label><span>Pixel Threshold <span class="info-tip" data-tip="Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras." title="Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras." tabindex="0" aria-label="Help: Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras."></span></span><input name="motion_pixel_threshold" type="number" min="1" max="255" step="1" placeholder="Global default (30)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_pixel_threshold') != null ? cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_pixel_threshold') : '') + '" /></label>' +
            '<label><span>Gate Fraction <span class="info-tip" data-tip="Minimum fraction of pixels that must change before motion is declared." title="Minimum fraction of pixels that must change before motion is declared." tabindex="0" aria-label="Help: Minimum fraction of pixels that must change before motion is declared."></span></span><input name="motion_gate_fraction" type="number" min="0.0001" max="0.5" step="0.0001" placeholder="Global default (0.005)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_gate_fraction') != null ? cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_gate_fraction') : '') + '" /></label>' +
            '<label><span>Scale Fraction <span class="info-tip" data-tip="Pixel change fraction that maps to 100% motion confidence." title="Pixel change fraction that maps to 100% motion confidence." tabindex="0" aria-label="Help: Pixel change fraction that maps to 100% motion confidence."></span></span><input name="motion_scale_fraction" type="number" min="0.001" max="1.0" step="0.001" placeholder="Global default (0.03)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_scale_fraction') != null ? cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_scale_fraction') : '') + '" /></label>' +
            '<label><span>Background Alpha <span class="info-tip" data-tip="How fast the background model adapts when no motion is detected." title="How fast the background model adapts when no motion is detected." tabindex="0" aria-label="Help: How fast the background model adapts when no motion is detected."></span></span><input name="motion_background_alpha" type="number" min="0.001" max="0.5" step="0.001" placeholder="Global default (0.05)" value="' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_background_alpha') != null ? cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_background_alpha') : '') + '" /></label>' +
            '<label><span>Motion Engine <span class="info-tip" data-tip="Background-subtraction engine for this camera. Leave on Global default unless this camera needs a different engine." title="Background-subtraction engine for this camera. Leave on Global default unless this camera needs a different engine." tabindex="0" aria-label="Help: Per-camera motion engine override."></span></span><select name="motion_algorithm">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_algorithm') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="mog2"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_algorithm') === 'mog2' ? ' selected' : '') + '>MOG2</option>' +
              '<option value="diff"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_algorithm') === 'diff' ? ' selected' : '') + '>Diff (legacy)</option>' +
            '</select></label>' +
            '<label><span>Denoise <span class="info-tip" data-tip="Morphological denoise of the motion mask for this camera. Leave on Global default to follow the global setting." title="Morphological denoise of the motion mask for this camera. Leave on Global default to follow the global setting." tabindex="0" aria-label="Help: Per-camera denoise override."></span></span><select name="motion_denoise">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_denoise') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="true"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_denoise') === true ? ' selected' : '') + '>Enabled</option>' +
              '<option value="false"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_denoise') === false ? ' selected' : '') + '>Disabled</option>' +
            '</select></label>' +
            '<label><span>Shadow Suppression <span class="info-tip" data-tip="Reject cast shadows from motion alerts for this camera (MOG2 only). It does not filter YOLO object detections. On = always; Off = never (dark/IR scenes); Automatic = only while bright. Leave on Global default to follow the global setting." title="Reject cast shadows from motion alerts for this camera (MOG2 only). It does not filter YOLO object detections. On / Off / Automatic. Leave on Global default to follow the global setting." tabindex="0" aria-label="Help: Per-camera shadow suppression override for motion alerts only (on/off/automatic)."></span></span><select name="motion_shadow_suppression">' +
              '<option value=""' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_shadow_suppression') == null ? ' selected' : '') + '>Global default</option>' +
              '<option value="on"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_shadow_suppression') === 'on' ? ' selected' : '') + '>On</option>' +
              '<option value="off"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_shadow_suppression') === 'off' ? ' selected' : '') + '>Off</option>' +
              '<option value="auto"' + (cameraProfileValue(camera, camera.detection_profiles?.active || 'day', 'motion_shadow_suppression') === 'auto' ? ' selected' : '') + '>Automatic (Day Only)</option>' +
            '</select></label>' +
          '</div>' +
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
  gridEl.querySelectorAll('.camera-edit-row').forEach(function(row) { row.remove(); });
  gridEl.querySelectorAll('.camera-row-editing').forEach(function(row) { row.classList.remove('camera-row-editing'); });
}

function toggleEditForm(camera, index) {
  closeAllEditForms();
  var row = gridEl.querySelector('[data-camera-index="' + index + '"]');
  if (!row) return;

  var existing = row.nextElementSibling;
  if (existing && existing.classList.contains('camera-edit-row')) {
    // Already open - close it
    existing.remove();
    row.classList.remove('camera-row-editing');
    return;
  }

  var formHtml = buildEditFormHtml(camera, index);
  row.classList.add('camera-row-editing');
  row.insertAdjacentHTML('afterend', formHtml);
  wireEditFormHandlers(index);
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

  // Backend toggle
  var backendSelect = form.querySelector('[name="backend"]');
  if (backendSelect) {
    backendSelect.addEventListener('change', function() {
      var manual = this.value === 'rtsp';
      form.querySelectorAll('.cam-rtsp-fields').forEach(function(el) { el.hidden = !manual; });
      form.querySelectorAll('.cam-onvif-fields').forEach(function(el) { el.hidden = manual; });
    });
  }

  // Manual day/night profile selector. The existing motion controls edit the
  // selected profile; switching profiles loads that profile's stored values.
  var profileSelect = form.querySelector('[name="detection_profile"]');
  if (profileSelect) {
    profileSelect.addEventListener('change', function() {
      var camera = cameras[index] || {};
      var mode = this.value === 'night' ? 'night' : 'day';
      PROFILE_FIELDS.forEach(function(key) {
        var fieldName = PROFILE_PERFORMANCE_FIELDS.includes(key) ? 'profile_' + key : key;
        var field = form.querySelector('[name="' + fieldName + '"]');
        if (!field) return;
        var value = cameraProfileValue(camera, mode, key);
        field.value = value == null ? '' : String(value);
      });
    });
  }

  var suggestButton = form.querySelector('.profile-suggest-btn');
  var irCheckButton = form.querySelector('.ir-check-btn');
  var profileResult = form.querySelector('.profile-action-result');
  if (suggestButton) {
    suggestButton.addEventListener('click', async function() {
      suggestButton.disabled = true;
      if (profileResult) profileResult.textContent = 'Calculating…';
      try {
        var cameraId = form.querySelector('[name="id"]')?.value || cameras[index]?.id;
        var suggestion = await api('/api/cameras/' + encodeURIComponent(cameraId) + '/profile-schedule-suggestion');
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
  if (irCheckButton) {
    irCheckButton.addEventListener('click', async function() {
      irCheckButton.disabled = true;
      if (profileResult) profileResult.textContent = 'Checking ONVIF IR state…';
      try {
        var cameraId = form.querySelector('[name="id"]')?.value || cameras[index]?.id;
        var result = await api('/api/cameras/' + encodeURIComponent(cameraId) + '/ir-state', { method: 'POST', body: '{}' });
        if (profileResult) profileResult.textContent = result.supported ? ('Camera reports ' + result.state + '.') : (result.error || 'IR state unavailable; schedule fallback remains active.');
      } catch (err) {
        if (!window.daygleAuth?.redirecting && profileResult) profileResult.textContent = err.message || 'IR check failed.';
      } finally {
        irCheckButton.disabled = false;
      }
    });
  }

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
          resultEl.textContent = res.online ? 'Connected' : (res.message || 'Unreachable');
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
  var profile = { ...(existingProfiles[activeProfile] || {}) };
  var profileValue = function(name, value) { profile[name] = value === '' ? null : value; };
  profileValue('motion_pixel_threshold', (function() { var v = getName('motion_pixel_threshold'); return v !== '' ? parseInt(v, 10) : ''; })());
  profileValue('motion_gate_fraction', (function() { var v = getName('motion_gate_fraction'); return v !== '' ? Number(v) : ''; })());
  profileValue('motion_scale_fraction', (function() { var v = getName('motion_scale_fraction'); return v !== '' ? Number(v) : ''; })());
  profileValue('motion_background_alpha', (function() { var v = getName('motion_background_alpha'); return v !== '' ? Number(v) : ''; })());
  profileValue('motion_algorithm', getName('motion_algorithm'));
  profileValue('motion_denoise', (function() { var v = getName('motion_denoise'); return v !== '' ? (v === 'true') : ''; })());
  profileValue('motion_shadow_suppression', getName('motion_shadow_suppression'));
  var profileNumber = function(name, parser) {
    var value = getName('profile_' + name);
    return value !== '' ? parser(value) : '';
  };
  var profileBool = function(name) {
    var value = getName('profile_' + name);
    return value !== '' ? value === 'true' : '';
  };
  profileValue('background_detection_enabled', profileBool('background_detection_enabled'));
  profileValue('detection_interval_seconds', profileNumber('detection_interval_seconds', Number));
  profileValue('ingest_frame_fps', profileNumber('ingest_frame_fps', function(value) { return parseInt(value, 10); }));
  profileValue('detection_confirm_frames', profileNumber('detection_confirm_frames', function(value) { return parseInt(value, 10); }));
  profileValue('detection_confirm_window', profileNumber('detection_confirm_window', function(value) { return parseInt(value, 10); }));
  profileValue('detection_confirm_iou', profileNumber('detection_confirm_iou', Number));
  profileValue('always_run_object_detection', profileBool('always_run_object_detection'));
  profileValue('object_detection_region_boost', profileBool('object_detection_region_boost'));
  profileValue('object_detection_tiling', getName('profile_object_detection_tiling'));
  profileValue('periodic_scan_interval_seconds', profileNumber('periodic_scan_interval_seconds', function(value) { return parseInt(value, 10); }));
  profileValue('motion_frame_width', profileNumber('motion_frame_width', function(value) { return parseInt(value, 10); }));
  profileValue('motion_frame_height', profileNumber('motion_frame_height', function(value) { return parseInt(value, 10); }));
  var profiles = {
    active: activeProfile === 'night' ? 'night' : 'day',
    source: ['manual', 'schedule', 'onvif'].includes(getName('profile_source')) ? getName('profile_source') : 'manual',
    day_start: getName('profile_day_start') || '07:00',
    night_start: getName('profile_night_start') || '19:00',
    day: { ...(existingProfiles.day || {}) },
    night: { ...(existingProfiles.night || {}) },
  };
  profiles[activeProfile === 'night' ? 'night' : 'day'] = profile;
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
    detection: {},
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

  var rowHtml = '<tr data-camera-index="' + index + '" class="' + (isEnabled ? '' : 'camera-row-disabled') + '">';
  rowHtml += '<td class="cell-camera">';
  rowHtml += '<div class="cam-info"><span class="cam-name">' + name + '</span>' + (id ? '<span class="cam-id">ID · ' + id + '</span>' : '') + '</div>';
  rowHtml += '<div class="cell-actions"><button class="secondary cam-edit-btn" data-index="' + index + '" type="button" title="Edit camera" aria-label="Edit ' + name + '">' + ICONS.edit + '</button><button class="secondary cam-toggle-btn' + (isEnabled ? ' is-enabled' : ' is-disabled') + '" data-index="' + index + '" type="button" title="' + (isEnabled ? 'Disable camera' : 'Enable camera') + '" aria-label="' + (isEnabled ? 'Disable ' : 'Enable ') + name + '">' + ICONS.power + '</button><button class="delete-btn secondary cam-remove-btn" data-index="' + index + '" type="button" title="Remove camera" aria-label="Remove ' + name + '">' + ICONS.remove + '</button></div>';
  rowHtml += '</td>';
  rowHtml += '<td class="cell-connection"><span class="chip camera-backend-chip">' + backend + '</span><span class="camera-endpoint">' + endpoint + '</span></td>';
  rowHtml += '<td class="cell-video"><strong>' + resolution + '</strong><span>' + escapeHtml(fpsText) + '</span></td>';
  rowHtml += '<td class="cell-state">' + healthHtml + '<span class="camera-enabled-label">' + (isEnabled ? 'Enabled' : 'Configuration paused') + '</span></td>';
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
    return renderCameraRow(cam, realIndex);
  }).join('');
  var tableHtml = '<div class="cameras-table-wrap"><table class="cameras-table"><thead><tr>' +
    renderCameraSortHeader('Camera', 'camera') +
    renderCameraSortHeader('Connection', 'connection') +
    renderCameraSortHeader('Video', 'video') +
    renderCameraSortHeader('Status', 'status') +
    renderCameraSortHeader('PTZ', 'ptz') +
    '</tr></thead><tbody>' + rowsHtml + '</tbody></table></div>';
  gridEl.innerHTML = tableHtml;
  updateFilterHint(filtered.length);

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
