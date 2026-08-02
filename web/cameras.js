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
  ptz: document.getElementById('statPtzCameras'),
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

  return '<tr class="camera-edit-row" id="' + rowId + '"><td colspan="6"><div class="camera-edit-panel">' +
    '<div class="cam-edit-head">' +
      '<span class="cam-edit-head-title">Editing <strong>' + escapeAttr(camera.name || camera.id || ('Camera ' + (index + 1))) + '</strong></span>' +
      (camera.id ? '<span class="cam-edit-head-id">ID · ' + escapeAttr(camera.id) + '</span>' : '') +
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
            '<label><span>Camera Name</span><input name="name" placeholder="e.g. Front Door" required value="' + escapeAttr(camera.name || '') + '" /></label>' +
            '<label><span>Camera ID</span><input name="id" placeholder="e.g. front-door" value="' + escapeAttr(camera.id || '') + '" /></label>' +
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
              '<label class="full-width"><span>Stream URL</span><input name="stream_url" placeholder="rtsp://user:pass@192.168.1.100:554/stream1" value="' + escapeAttr(camera.stream_url || '') + '" /></label>' +
            '</div>' +
          '</div>' +
          '<div class="cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '>' +
            '<div class="form-grid">' +
              '<label><span>Host / IP</span><input name="host" placeholder="192.168.1.100" value="' + escapeAttr(camera.host || '') + '" /></label>' +
              '<label><span>Port</span><input name="port" type="number" min="1" max="65535" placeholder="554" value="' + (camera.port || 554) + '" /></label>' +
              '<label><span>Username</span><input name="username" placeholder="admin" autocomplete="off" value="' + escapeAttr(camera.username || '') + '" /></label>' +
              '<label class="full-width"><span>Password</span><input name="password" type="password" autocomplete="new-password" placeholder="' + (camera.has_password ? '(saved - type to change)' : '(No Password)') + '" /></label>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="cam-edit-section">' +
          '<h4 class="cam-edit-section-title">Streams</h4>' +
          '<div class="form-grid">' +
            '<label class="full-width cam-onvif-fields"' + (isRtsp ? ' hidden' : '') + '><span>Detection Stream Path</span><input name="path" placeholder="e.g. stream1" value="' + escapeAttr(camera.path || '') + '" /></label>' +
            '<label class="full-width"><span>Recording Stream Path <span class="info-tip" data-tip="Optional: the path for the high-res recording stream (e.g. stream2). Leave empty to use the primary stream for recording." title="Optional: the path for the high-res recording stream (e.g. stream2). Leave empty to use the primary stream for recording." tabindex="0" aria-label="Help: Optional path for the high-res recording stream."></span></span><input name="recording_stream_path" placeholder="e.g. stream2" value="' + escapeAttr(camera.recording_stream_path || '') + '" /></label>' +
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
  var backend = getName('backend') || 'onvif';    return {
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
    } catch (err) {
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

  var rowHtml = '<tr draggable="true" data-drag-camera="' + index + '" data-camera-index="' + index + '" class="' + (isEnabled ? '' : 'camera-row-disabled') + '">';
  rowHtml += '<td class="cell-drag"><span class="drag-handle" title="Drag to reorder">' + ICONS.grip + '</span></td>';
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
  var tableHtml = '<div class="cameras-table-wrap"><table class="cameras-table"><thead><tr><th class="cell-drag" scope="col"></th><th scope="col">Camera</th><th scope="col">Connection</th><th scope="col">Video</th><th scope="col">Status</th><th scope="col">PTZ</th></tr></thead><tbody>' + rowsHtml + '</tbody></table></div>';
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
  if (stats.ptz) stats.ptz.textContent = String(cameras.filter(function(c) { return c.ptz?.enabled === true; }).length);
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
