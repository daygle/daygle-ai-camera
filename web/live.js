const liveEls = {
  frame: document.getElementById('liveFrame'),
  frameWrap: document.getElementById('liveFrameWrap'),
  status: document.getElementById('liveStatus'),
  overlayToggle: document.getElementById('overlayToggle'),
  cameraSelect: document.getElementById('cameraSelect'),
  zoneOverlay: document.getElementById('zoneOverlay'),
  zoneList: document.getElementById('zoneList'),
  addZoneBtn: document.getElementById('addZoneBtn'),
  saveZonesBtn: document.getElementById('saveZonesBtn'),
  motionEnabled: document.getElementById('motionEnabled'),
  objectDetectionEnabled: document.getElementById('objectDetectionEnabled'),
};

let refreshTimer;
let csrfToken = null;
let cameras = [];
let selectedCamera = null;
let drawingMode = false;
let draftBox = null;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function cameraDetection() {
  selectedCamera.detection ||= { motion_enabled: true, object_detection_enabled: true, zones: [] };
  selectedCamera.detection.zones ||= [];
  return selectedCamera.detection;
}

function snapshotUrl() {
  const overlay = liveEls.overlayToggle.checked ? '1' : '0';
  const cameraId = encodeURIComponent(selectedCamera?.id || '');
  return `/api/live/snapshot?overlay=${overlay}&camera_id=${cameraId}&t=${Date.now()}`;
}

function refreshFrame() {
  if (!selectedCamera) return;
  liveEls.frame.src = snapshotUrl();
}

function setSelectedCamera(cameraId) {
  selectedCamera = cameras.find((camera) => camera.id === cameraId) || cameras[0];
  if (!selectedCamera) return;
  liveEls.cameraSelect.value = selectedCamera.id;
  liveEls.motionEnabled.value = String(cameraDetection().motion_enabled !== false);
  liveEls.objectDetectionEnabled.value = String(cameraDetection().object_detection_enabled !== false);
  renderZones();
  refreshFrame();
}

function renderCameraOptions() {
  liveEls.cameraSelect.innerHTML = cameras.map((camera) => `<option value="${escapeHtml(camera.id)}">${escapeHtml(camera.name || camera.id)}</option>`).join('');
  setSelectedCamera(liveEls.cameraSelect.value || cameras[0]?.id);
}

function renderZones() {
  if (!selectedCamera) return;
  const zones = cameraDetection().zones;
  liveEls.zoneOverlay.innerHTML = zones.map((zone, index) => `
    <div class="monitor-zone-box" style="left:${zone.x * 100}%;top:${zone.y * 100}%;width:${zone.width * 100}%;height:${zone.height * 100}%">
      <span>${escapeHtml(zone.name || `Zone ${index + 1}`)}</span>
    </div>
  `).join('');

  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No monitoring areas yet. Click “Draw area”, then drag on the footage.</div>';
    return;
  }

  liveEls.zoneList.innerHTML = zones.map((zone, index) => `
    <div class="item zone-row">
      <input data-zone-name="${index}" value="${escapeHtml(zone.name || `Zone ${index + 1}`)}" />
      <label><span>Motion</span><select data-zone-motion="${index}"><option value="true" ${zone.monitor_motion !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_motion === false ? 'selected' : ''}>Off</option></select></label>
      <label><span>Objects</span><select data-zone-objects="${index}"><option value="true" ${zone.monitor_objects !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_objects === false ? 'selected' : ''}>Off</option></select></label>
      <button class="secondary" type="button" data-delete-zone="${index}">Remove</button>
    </div>
  `).join('');

  document.querySelectorAll('[data-zone-name]').forEach((input) => {
    input.addEventListener('input', () => { zones[Number(input.dataset.zoneName)].name = input.value; renderZones(); });
  });
  document.querySelectorAll('[data-zone-motion]').forEach((select) => {
    select.addEventListener('change', () => { zones[Number(select.dataset.zoneMotion)].monitor_motion = select.value === 'true'; });
  });
  document.querySelectorAll('[data-zone-objects]').forEach((select) => {
    select.addEventListener('change', () => { zones[Number(select.dataset.zoneObjects)].monitor_objects = select.value === 'true'; });
  });
  document.querySelectorAll('[data-delete-zone]').forEach((button) => {
    button.addEventListener('click', () => { zones.splice(Number(button.dataset.deleteZone), 1); renderZones(); refreshFrame(); });
  });
}

function pointFromEvent(event) {
  const rect = liveEls.frameWrap.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

liveEls.frameWrap.addEventListener('pointerdown', (event) => {
  if (!drawingMode || !selectedCamera) return;
  const start = pointFromEvent(event);
  draftBox = { start, end: start };
  liveEls.frameWrap.setPointerCapture(event.pointerId);
});

liveEls.frameWrap.addEventListener('pointermove', (event) => {
  if (!draftBox) return;
  draftBox.end = pointFromEvent(event);
  const x = Math.min(draftBox.start.x, draftBox.end.x);
  const y = Math.min(draftBox.start.y, draftBox.end.y);
  const width = Math.abs(draftBox.end.x - draftBox.start.x);
  const height = Math.abs(draftBox.end.y - draftBox.start.y);
  liveEls.zoneOverlay.querySelector('.draft')?.remove();
  liveEls.zoneOverlay.insertAdjacentHTML('beforeend', `<div class="monitor-zone-box draft" style="left:${x * 100}%;top:${y * 100}%;width:${width * 100}%;height:${height * 100}%"><span>New area</span></div>`);
});

liveEls.frameWrap.addEventListener('pointerup', (event) => {
  if (!draftBox) return;
  const end = pointFromEvent(event);
  const x = Math.min(draftBox.start.x, end.x);
  const y = Math.min(draftBox.start.y, end.y);
  const width = Math.abs(end.x - draftBox.start.x);
  const height = Math.abs(end.y - draftBox.start.y);
  draftBox = null;
  drawingMode = false;
  liveEls.addZoneBtn.textContent = 'Draw area';
  if (width >= 0.02 && height >= 0.02) {
    const zones = cameraDetection().zones;
    zones.push({ id: `zone-${Date.now()}`, name: `Zone ${zones.length + 1}`, x, y, width, height, enabled: true, monitor_motion: true, monitor_objects: true });
  }
  renderZones();
  refreshFrame();
});

liveEls.frame.addEventListener('load', () => {
  liveEls.status.textContent = liveEls.overlayToggle.checked
    ? `${selectedCamera?.name || 'Camera'} · object overlay on`
    : `${selectedCamera?.name || 'Camera'} · object overlay off`;
});

liveEls.frame.addEventListener('error', () => {
  liveEls.status.textContent = 'Unable to load live footage. Retrying…';
});

liveEls.overlayToggle.addEventListener('change', refreshFrame);
liveEls.cameraSelect.addEventListener('change', () => setSelectedCamera(liveEls.cameraSelect.value));
liveEls.addZoneBtn.addEventListener('click', () => {
  drawingMode = !drawingMode;
  liveEls.addZoneBtn.textContent = drawingMode ? 'Cancel drawing' : 'Draw area';
});
liveEls.motionEnabled.addEventListener('change', () => { cameraDetection().motion_enabled = liveEls.motionEnabled.value === 'true'; });
liveEls.objectDetectionEnabled.addEventListener('change', () => { cameraDetection().object_detection_enabled = liveEls.objectDetectionEnabled.value === 'true'; refreshFrame(); });
liveEls.saveZonesBtn.addEventListener('click', async () => {
  try {
    liveEls.saveZonesBtn.disabled = true;
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}`, { method: 'PUT', body: JSON.stringify(selectedCamera) });
    const payload = await api('/api/cameras');
    cameras = payload.cameras || [];
    setSelectedCamera(selectedCamera.id);
    liveEls.status.textContent = 'Monitoring areas saved.';
  } catch (error) {
    liveEls.status.textContent = error.message;
  } finally {
    liveEls.saveZonesBtn.disabled = false;
  }
});

async function init() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  const payload = await api('/api/cameras');
  cameras = payload.cameras || [];
  renderCameraOptions();
  refreshTimer = setInterval(refreshFrame, 750);
}

init().catch((error) => { liveEls.status.textContent = error.message; });
window.addEventListener('beforeunload', () => clearInterval(refreshTimer));
