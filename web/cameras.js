let cameras = [];
let pendingDeleteIndex = null;
const cameraResolutions = {};
const cameraFps = {};

const messageEl = document.getElementById('cameraMessage');
const gridEl = document.getElementById('cameraGrid');
const emptyEl = document.getElementById('cameraEmpty');
const deleteModal = document.getElementById('deleteModal');

// Stats + filter state
const stats = {
  total: document.getElementById('statTotalCameras'),
  recording: document.getElementById('statRecordingOn'),
  zones: document.getElementById('statWithZones'),
  health: document.getElementById('statCameraHealth'),
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

function buildEditFormHtml(camera, index) {
  const backend = camera.backend || 'onvif';
  const isRtsp = backend === 'rtsp';
  const rowId = 'edit-row-' + index;
  const formId = 'edit-form-' + index;
  const escapeAttr = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  return '<tr class="camera-edit-row" id="' + rowId + '"><td colspan="7"><div class="camera-edit-panel">' +
    '<div class="modal-tabs" role="tablist">' +
      '<button class="modal-tab active" data-tab="connection" data-form="' + formId + '" type="button" role="tab" aria-selected="true">Connection</button>' +
      '<button class="modal-tab" data-tab="streams" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">Streams</button>' +
      '<button class="modal-tab" data-tab="recording" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">Recording</button>' +
      '<button class="modal-tab" data-tab="ptz" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">PTZ</button>' +
      '<button class="modal-tab" data-tab="advanced" data-form="' + formId + '" type="button" role="tab" aria-selected="false" tabindex="-1">Advanced</button>' +
    '</div>' +
    '<form class="camera-edit-form modal-body" data-camera-index="' + index + '" id="' + formId + '" novalidate autocomplete="off">' +
      '<input type="hidden" name="camera_index" value="' + index + '" />' +

      // Connection tab
      '<div class="modal-tab-panel" data-panel="connection">' +
        '<div class="form-grid">' +
          '<label><span>Camera Name</span><input name="name" placeholder="e.g. Front Door" required value="' + escapeAttr(camera.name || '') + '" /></label>' +
          '<label><span>Camera ID</span><input name="id" placeholder="e.g. front-door" value="' + escapeAttr(camera.id || '') + '" /></label>' +
          '<label class="full-width"><span>Backend</span>' +
            '<select name="backend" class="cam-edit-backend">' +
              '<option value="onvif"' + (backend === 'onvif' ? ' selected' : '') + '>ONVIF / RTSP (Auto-Build URL)</option>' +
              '<option value="rtsp"' + (backend === 'rtsp' ? ' selected' : '') + '>RTSP (Manual URL)</option>' +
            '</select>' +
          '</label>' +
        '</div>' +
        '<div class="form-group cam-rtsp-fields"' + (isRtsp ? '' : ' hidden') + '>' +
          '<p class="form-group-label">RTSP URL</p>' +
          '<div class="form-grid">' +
            '<label class="full-width"><span>Stream URL</span><input name="stream_url" placeholder="rtsp://user:pass@192.168.1.100:554/stream1" value="' + escapeAttr(camera.stream_url || '') + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="form-group cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '>' +
          '<p class="form-group-label">Connection details</p>' +
          '<div class="form-grid">' +
            '<label><span>Host / IP</span><input name="host" placeholder="192.168.1.100" value="' + escapeAttr(camera.host || '') + '" /></label>' +
            '<label><span>Port</span><input name="port" type="number" min="1" max="65535" placeholder="554" value="' + (camera.port || 554) + '" /></label>' +
            '<label><span>Username</span><input name="username" placeholder="admin" autocomplete="off" value="' + escapeAttr(camera.username || '') + '" /></label>' +
            '<label class="full-width"><span>Password</span><input name="password" type="password" autocomplete="new-password" placeholder="' + (camera.has_password ? '(saved - type to change)' : '(No Password)') + '" /></label>' +
          '</div>' +
        '</div>' +
        '<div class="button-row" style="margin-top:8px">' +
          '<button class="btn-info cam-test-conn-btn" data-form="' + formId + '" type="button">Test Connection</button>' +
          '<span class="muted cam-test-conn-result" data-form="' + formId + '" style="font-size:13px;align-self:center"></span>' +
        '</div>' +
      '</div>' +

      // Streams tab
      '<div class="modal-tab-panel" data-panel="streams" hidden>' +
        '<div class="form-grid">' +
              '<label class="full-width cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '><span>Detection Stream Path</span><input name="path" placeholder="stream1" value="' + escapeAttr(camera.path || 'stream1') + '" /></label>' +
          '<label class="full-width"><span>Recording Stream Path <span class="info-tip" data-tip="Optional: the path for the high-res recording stream (e.g. stream2). Uses the same host, port, and login details as the primary stream. Leave empty to use the primary stream for recording." title="Optional: the path for the high-res recording stream (e.g. stream2). Uses the same host, port, and login details as the primary stream. Leave empty to use the primary stream for recording." tabindex="0" aria-label="Help: Optional path for the high-res recording stream. Uses the same host, port, and login details."></span></span><input name="recording_stream_path" placeholder="e.g. stream2" value="' + escapeAttr(camera.recording_stream_path || '') + '" /></label>' +
        '</div>' +
        (isRtsp
          ? '<p class="form-help muted">Recording Stream Path is optional and points to a higher-resolution stream used for recordings.</p>'
              : '<p class="form-help muted">Detection Stream Path is the primary detection stream (e.g. stream1). Recording Stream Path is optional and points to a higher-resolution stream used for recordings.</p>') +
      '</div>' +

      // Recording tab
      '<div class="modal-tab-panel" data-panel="recording" hidden>' +
        '<div class="form-grid">' +
          '<label><span>Continuous Recording</span>' +
            '<select name="continuous">' +
              '<option value="false"' + (camera.recording?.continuous ? '' : ' selected') + '>Disabled</option>' +
              '<option value="true"' + (camera.recording?.continuous ? ' selected' : '') + '>Enabled</option>' +
            '</select>' +
          '</label>' +
        '</div>' +
        '<p class="form-help muted">When enabled, the camera writes an uninterrupted stream regardless of events. Otherwise, clips are recorded per detection rule (configured on the Zones page per object).</p>' +
      '</div>' +

      // PTZ tab
      '<div class="modal-tab-panel" data-panel="ptz" hidden>' +
        '<div class="form-grid">' +
          '<label><span>PTZ Control</span>' +
            '<select name="ptz_enabled">' +
              '<option value="false"' + (camera.ptz?.enabled ? '' : ' selected') + '>Disabled</option>' +
              '<option value="true"' + (camera.ptz?.enabled ? ' selected' : '') + '>Enabled</option>' +
            '</select>' +
          '</label>' +
          '<label class="full-width"><span>Protocol <span class="info-tip" data-tip="ONVIF uses standard PTZ over the camera&#39;s HTTP port. TCP PelcoD sends raw binary commands to the Command Port." title="ONVIF uses standard PTZ over the camera&#39;s HTTP port. TCP PelcoD sends raw binary commands to the Command Port." tabindex="0" aria-label="Help: ONVIF uses standard PTZ over the camera&#39;s HTTP port. TCP PelcoD sends raw binary commands to the Command Port."></span></span>' +
            '<select name="ptz_protocol">' +
              '<option value="onvif"' + ((camera.ptz?.protocol || 'onvif') === 'onvif' ? ' selected' : '') + '>ONVIF (Recommended)</option>' +
              '<option value="tcp_pelcod"' + (camera.ptz?.protocol === 'tcp_pelcod' ? ' selected' : '') + '>TCP PelcoD (Legacy Cameras)</option>' +
            '</select></label>' +
          '<label><span>HTTP Port <span class="info-tip" data-tip="Camera web port used by HTTP CGI (default 80)." title="Camera web port used by HTTP CGI (default 80)." tabindex="0" aria-label="Help: Camera web port used by HTTP CGI (default 80)."></span></span><input name="ptz_http_port" type="number" min="1" max="65535" placeholder="80" value="' + (camera.ptz?.http_port || 80) + '" /></label>' +
          '<label><span>Command Port <span class="info-tip" data-tip="Port for TCP PelcoD only (default 6060)." title="Port for TCP PelcoD only (default 6060)." tabindex="0" aria-label="Help: Port for TCP PelcoD only (default 6060)."></span></span><input name="ptz_port" type="number" min="1" max="65535" placeholder="6060" value="' + (camera.ptz?.port || 6060) + '" /></label>' +
          '<label><span>PTZ Address <span class="info-tip" data-tip="PelcoD device address (default 1, TCP PelcoD only)." title="PelcoD device address (default 1, TCP PelcoD only)." tabindex="0" aria-label="Help: PelcoD device address (default 1, TCP PelcoD only)."></span></span><input name="ptz_address" type="number" min="1" max="255" placeholder="1" value="' + (camera.ptz?.address || 1) + '" /></label>' +
          '<label><span>Speed <span class="info-tip" data-tip="Movement speed (1-8, default 5)." title="Movement speed (1-8, default 5)." tabindex="0" aria-label="Help: Movement speed (1-8, default 5)."></span></span><input name="ptz_speed" type="number" min="1" max="8" placeholder="5" value="' + (camera.ptz?.speed || 5) + '" /></label>' +
          '<label><span>Step Duration (s) <span class="info-tip" data-tip="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." title="How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)." tabindex="0" aria-label="Help: How long each press keeps the camera moving. Hold longer for continuous pan; short values act like fixed-step nudges (0.1-5 s, default 0.4)."></span></span><input name="ptz_step_duration" type="number" min="0.1" max="5" step="0.1" placeholder="0.4" value="' + (camera.ptz?.step_duration != null ? Number(camera.ptz.step_duration).toFixed(2) : '') + '" /></label>' +
        '</div>' +
        '<p class="form-help muted">Enable PTZ and save to show the control pad on the Live page. The camera&#39;s username and password from the Connection tab are used for HTTP CGI authentication.</p>' +
      '</div>' +

      // Advanced tab
      '<div class="modal-tab-panel" data-panel="advanced" hidden>' +
        '<div class="form-grid">' +
          '<label><span>FPS <span class="info-tip" data-tip="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." title="Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong." tabindex="0" aria-label="Help: Leave empty to auto-detect from the stream. Enter a value only if the detected FPS is wrong."></span></span><input name="fps" type="number" min="1" max="120" placeholder="Auto" value="' + (camera.fps != null ? camera.fps : '') + '" /></label>' +
          '<label><span>Frame Buffer Drains <span class="info-tip" data-tip="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)." title="Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)." tabindex="0" aria-label="Help: Stale frames to discard before reading the latest. Lower = faster response, higher = more stable. Leave empty for auto (FPS/4)."></span></span><input name="stale_frame_grabs" type="number" min="0" max="20" placeholder="Auto" value="' + (camera.stale_frame_grabs != null ? camera.stale_frame_grabs : '') + '" /></label>' +
        '</div>' +
        '<p class="form-help muted">Frame-buffer drains is a hint passed to the stream decoder. Leave FPS empty to auto-detect it from the stream; override it only if the detected value is wrong.</p>' +
        '<div class="form-group">' +
          '<p class="form-group-label">Motion Detection Overrides</p>' +
          '<p class="form-help muted">Override the global motion settings for this camera only. Leave blank to use the global defaults from Live Detection settings.</p>' +
          '<div class="form-grid">' +
            '<label><span>Pixel Threshold <span class="info-tip" data-tip="Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras." title="Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras." tabindex="0" aria-label="Help: Pixel intensity change required to count as motion (1-255). Raise for noisy IR cameras."></span></span><input name="motion_pixel_threshold" type="number" min="1" max="255" step="1" placeholder="Global default (30)" value="' + (camera.motion_pixel_threshold != null ? camera.motion_pixel_threshold : '') + '" /></label>' +
            '<label><span>Gate Fraction <span class="info-tip" data-tip="Minimum fraction of pixels that must change before motion is declared." title="Minimum fraction of pixels that must change before motion is declared." tabindex="0" aria-label="Help: Minimum fraction of pixels that must change before motion is declared."></span></span><input name="motion_gate_fraction" type="number" min="0.0001" max="0.5" step="0.0001" placeholder="Global default (0.003)" value="' + (camera.motion_gate_fraction != null ? camera.motion_gate_fraction : '') + '" /></label>' +
            '<label><span>Scale Fraction <span class="info-tip" data-tip="Pixel change fraction that maps to 100% motion confidence." title="Pixel change fraction that maps to 100% motion confidence." tabindex="0" aria-label="Help: Pixel change fraction that maps to 100% motion confidence."></span></span><input name="motion_scale_fraction" type="number" min="0.001" max="1.0" step="0.001" placeholder="Global default (0.03)" value="' + (camera.motion_scale_fraction != null ? camera.motion_scale_fraction : '') + '" /></label>' +
            '<label><span>Background Alpha <span class="info-tip" data-tip="How fast the background model adapts when no motion is detected." title="How fast the background model adapts when no motion is detected." tabindex="0" aria-label="Help: How fast the background model adapts when no motion is detected."></span></span><input name="motion_background_alpha" type="number" min="0.001" max="0.5" step="0.001" placeholder="Global default (0.05)" value="' + (camera.motion_background_alpha != null ? camera.motion_background_alpha : '') + '" /></label>' +
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

  // Form submit
  form.addEventListener('submit', async function(e) {
    e.preventDefault();
    var data = collectFormData(form, index);
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

function collectFormData(form, index) {
  var getVal = function(name) { var el = form.querySelector('[name="' + name + '"]'); return el ? el.value : ''; };
  var getName = function(name) { return getVal(name).trim(); };
  var getInt = function(name, def) { var v = parseInt(getVal(name), 10); return isNaN(v) ? def : v; };
  var backend = getName('backend') || 'onvif';

  return {
    id: getName('id') || ('camera-' + (cameras.length + 1)),
    name: getName('name'),
    backend: backend,
    stream_url: backend === 'rtsp' ? getName('stream_url') : '',
    recording_stream_path: getName('recording_stream_path'),
    host: backend !== 'rtsp' ? getName('host') : '',
    port: getInt('port', 554),
    path: backend !== 'rtsp' ? getName('path') : '',
    username: getName('username'),
    password: getVal('password'),
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
    motion_pixel_threshold: (function() { var v = getName('motion_pixel_threshold'); return v !== '' ? parseInt(v, 10) : null; })(),
    motion_gate_fraction: (function() { var v = getName('motion_gate_fraction'); return v !== '' ? Number(v) : null; })(),
    motion_scale_fraction: (function() { var v = getName('motion_scale_fraction'); return v !== '' ? Number(v) : null; })(),
    motion_background_alpha: (function() { var v = getName('motion_background_alpha'); return v !== '' ? Number(v) : null; })(),
  };
}

// ─── Camera row rendering ─────────────────────────────────────────────────────

function renderCameraRow(camera, index) {
  var name = escapeHtml(camera.name || camera.id || ('Camera ' + (index + 1)));
  var id = escapeHtml(camera.id || '');
  var backend = camera.backend === 'rtsp' ? 'RTSP' : 'ONVIF';

  var zones = camera.detection?.zones || [];
  var zoneCount = zones.length;
  var ruleCount = zones.reduce(function(n, z) { return n + (z.object_rules?.length || 0); }, 0);

  var sound = camera.detection?.sound;
  var soundEnabled = sound?.enabled === true;

  var continuous = camera.recording?.continuous === true;

  var zonesHtml = zoneCount === 0
    ? '<span class="chip chip-warn">No zones</span>'
    : '<span class="chip chip-green">' + zoneCount + ' zone' + (zoneCount !== 1 ? 's' : '') + '</span>' + (ruleCount > 0 ? ' <span class="chip chip-info">' + ruleCount + ' rule' + (ruleCount !== 1 ? 's' : '') + '</span>' : '');

  var soundHtml = soundEnabled
    ? '<span class="chip chip-green">On</span>'
    : '<span class="chip chip-dim">Off</span>';

  var recordingHtml = continuous
    ? '<span class="chip chip-green">Continuous</span>'
    : '<span class="chip chip-info">On Alert</span>';

  var hasStream = !!(camera.stream_url || camera.host);
  var healthHtml = hasStream
    ? '<span class="health-dot online"></span><span>Online</span>'
    : '<span class="health-dot offline"></span><span>Offline</span>';

  var rowHtml = '';
  rowHtml += '<tr draggable="true" data-drag-camera="' + index + '" data-camera-index="' + index + '">';
  rowHtml += '<td class="cell-drag"><span class="drag-handle" title="Drag to reorder">' + ICONS.grip + '</span></td>';
  var resolution = cameraResolutions[camera.id];
  var resolutionText = '';
  if (resolution && resolution.width > 0 && resolution.height > 0) {
    resolutionText = resolution.width + ' x ' + resolution.height;
  } else if (resolution === null) {
    resolutionText = 'No signal';
  }
  var fps = cameraFps[camera.id];
  var fpsText = '';
  if (fps && fps.source === 'detected' && Number(fps.detected) > 0) {
    fpsText = Number(fps.detected) + ' FPS';
  } else if (fps && fps.source === 'configured' && Number(fps.configured) > 0) {
    fpsText = Number(fps.configured) + ' FPS (override)';
  } else if (fps && fps.source === 'fallback') {
    // The backend's 15 FPS fallback is only for buffer-drain calculations;
    // never present it as the camera's hardware/source rate.
    fpsText = 'Detecting FPS…';
  } else if (fps === null) {
    fpsText = '';
  }
  var streamMetaText = resolutionText && fpsText
    ? resolutionText + ' @ ' + fpsText
    : (resolutionText || fpsText);
  var resolutionHtml = streamMetaText
    ? '<span class="cam-resolution">' + streamMetaText + '</span>'
    : '<span class="cam-resolution muted">-</span>';

  rowHtml += '<td class="cell-camera">';
  rowHtml += '<div class="cam-info"><span class="cam-name">' + name + '</span>' + (id ? '<span class="cam-id">' + id + '</span>' : '') + resolutionHtml + '</div>';
  rowHtml += '<div class="cell-actions">';
  rowHtml += '<button class="secondary cam-edit-btn" data-index="' + index + '" type="button" title="Edit camera">' + ICONS.edit + '</button>';
  rowHtml += '<button class="delete-btn secondary cam-remove-btn" data-index="' + index + '" type="button" title="Remove camera">' + ICONS.remove + '</button>';
  rowHtml += '</div>';
  rowHtml += '</td>';
  rowHtml += '<td><span class="chip">' + backend + '</span></td>';
  rowHtml += '<td class="cell-zones">' + zonesHtml + '</td>';
  rowHtml += '<td class="cell-center">' + soundHtml + '</td>';
  rowHtml += '<td>' + recordingHtml + '</td>';
  rowHtml += '<td class="cell-health">' + healthHtml + '</td>';
  rowHtml += '</tr>';
  return rowHtml;
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
  var rowsHtml = filtered.map(function(cam) {
    var realIndex = cameras.indexOf(cam);
    return renderCameraRow(cam, realIndex);
  }).join('');
  var tableHtml = '<div class="cameras-table-wrap"><table class="cameras-table"><thead><tr><th class="cell-drag"></th><th>Camera</th><th>Backend</th><th>Zones</th><th class="cell-center">Sound</th><th>Record</th><th>Health</th></tr></thead><tbody>' + rowsHtml + '</tbody></table></div>';
  gridEl.innerHTML = tableHtml;
  updateFilterHint(filtered.length);

  gridEl.querySelectorAll('.cam-edit-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var idx = Number(btn.dataset.index);
      toggleEditForm(cameras[idx], idx);
    });
  });
  gridEl.querySelectorAll('.cam-remove-btn').forEach(function(btn) {
    btn.addEventListener('click', function() { openDeleteModal(Number(btn.dataset.index)); });
  });

  // Drag-and-drop reorder
  var table = gridEl.querySelector('table');
  gridEl.querySelectorAll('[data-drag-camera]').forEach(function(row) {
    row.addEventListener('dragstart', function(event) {
      closeAllEditForms();
      row.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(row.dataset.dragCamera));
    });
    row.addEventListener('dragend', function() {
      row.classList.remove('dragging');
      if (table) table.querySelectorAll('tr').forEach(function(r) { r.classList.remove('drag-over'); });
    });
    row.addEventListener('dragover', function(event) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      if (table) table.querySelectorAll('tr[data-drag-camera]').forEach(function(r) { r.classList.remove('drag-over'); });
      row.classList.add('drag-over');
    });
    row.addEventListener('drop', async function(event) {
      event.preventDefault();
      row.classList.remove('drag-over');
      var draggedIndex = Number(event.dataTransfer.getData('text/plain'));
      var targetIndex = Number(row.dataset.dragCamera);
      if (!Number.isFinite(draggedIndex) || !Number.isFinite(targetIndex) || draggedIndex === targetIndex) return;
      var camerasBefore = cameras.slice();
      var arr = cameras.splice(draggedIndex, 1);
      var adjustedTarget = targetIndex > draggedIndex ? targetIndex - 1 : targetIndex;
      cameras.splice(adjustedTarget, 0, arr[0]);
      try {
        var result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras: cameras }) });
        cameras = result.cameras || cameras;
        renderGrid();
        setMessage('Camera order updated.');
      } catch (err) {
        cameras.splice(0, cameras.length, ...camerasBefore);
        if (window.daygleAuth?.redirecting) return;
        setMessage(err.message, true);
      }
    });
  });
}

function updateStats() {
  if (stats.total) stats.total.textContent = String(cameras.length);
  if (stats.recording) {
    var continuous = cameras.filter(function(c) { return c.recording?.continuous === true; }).length;
    stats.recording.textContent = (cameras.length - continuous) + ' / ' + continuous;
  }
  if (stats.zones) {
    var withZones = cameras.filter(function(c) { return (c.detection?.zones || []).length > 0; }).length;
    stats.zones.textContent = String(withZones);
  }
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
  var newCam = { id: ('camera-' + (cameras.length + 1)), name: ('Camera ' + (cameras.length + 1)), backend: 'onvif', port: 554, path: 'stream1', recording: { continuous: false }, detection: {}, ptz: { enabled: false, protocol: 'onvif', http_port: 80, port: 6060, address: 1, speed: 5, step_duration: 0.4 } };
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
    } catch (err) {
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
filter.reset?.addEventListener('click', function() { setTimeout(function() { renderGrid(); }, 0); });
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
    if (stats.health) {
      var online = s.online || 0;
      var offline = s.offline || 0;
      stats.health.textContent = online + ' / ' + offline;
      if (offline > 0) {
        stats.health.style.color = 'var(--danger-color, #e74c3c)';
      } else if (online > 0) {
        stats.health.style.color = 'var(--success-color, #2ecc71)';
      }
    }
  } catch (e) {
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
