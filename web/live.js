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
let selectedZoneIndex = null;
let drawingMode = false;
let draftBox = null;
let zoneDrag = null;

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

function clamp(value, min = 0, max = 1) {
  return Math.max(min, Math.min(max, value));
}

function normalizeZone(zone) {
  zone.x = clamp(Number(zone.x) || 0);
  zone.y = clamp(Number(zone.y) || 0);
  zone.width = clamp(Number(zone.width) || 0.01, 0.01, 1 - zone.x);
  zone.height = clamp(Number(zone.height) || 0.01, 0.01, 1 - zone.y);
  return zone;
}

function visibleImageRect() {
  const frameRect = liveEls.frame.getBoundingClientRect();
  const naturalWidth = liveEls.frame.naturalWidth || selectedCamera?.width || 16;
  const naturalHeight = liveEls.frame.naturalHeight || selectedCamera?.height || 9;
  const imageRatio = naturalWidth / naturalHeight;
  const frameRatio = frameRect.width / frameRect.height;
  let width = frameRect.width;
  let height = frameRect.height;
  let left = frameRect.left;
  let top = frameRect.top;

  if (frameRatio > imageRatio) {
    width = height * imageRatio;
    left += (frameRect.width - width) / 2;
  } else {
    height = width / imageRatio;
    top += (frameRect.height - height) / 2;
  }
  return { left, top, width, height };
}

function syncZoneOverlayToImage() {
  const wrapRect = liveEls.frameWrap.getBoundingClientRect();
  const imageRect = visibleImageRect();
  liveEls.zoneOverlay.style.left = `${imageRect.left - wrapRect.left}px`;
  liveEls.zoneOverlay.style.top = `${imageRect.top - wrapRect.top}px`;
  liveEls.zoneOverlay.style.width = `${imageRect.width}px`;
  liveEls.zoneOverlay.style.height = `${imageRect.height}px`;
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
  selectedZoneIndex = null;
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

function renderZoneBox(zone, index) {
  const selected = index === selectedZoneIndex ? ' selected' : '';
  return `
    <div class="monitor-zone-box${selected}" data-zone-index="${index}" style="left:${zone.x * 100}%;top:${zone.y * 100}%;width:${zone.width * 100}%;height:${zone.height * 100}%">
      <span>${escapeHtml(zone.name || `Zone ${index + 1}`)}</span>
      <i class="zone-handle zone-handle-nw" data-zone-index="${index}" data-resize-zone="nw"></i>
      <i class="zone-handle zone-handle-ne" data-zone-index="${index}" data-resize-zone="ne"></i>
      <i class="zone-handle zone-handle-sw" data-zone-index="${index}" data-resize-zone="sw"></i>
      <i class="zone-handle zone-handle-se" data-zone-index="${index}" data-resize-zone="se"></i>
    </div>
  `;
}

function updateSelectionStyles() {
  liveEls.zoneOverlay.querySelectorAll('[data-zone-index]').forEach((box) => {
    box.classList.toggle('selected', Number(box.dataset.zoneIndex) === selectedZoneIndex);
  });
  liveEls.zoneList.querySelectorAll('[data-select-zone]').forEach((row) => {
    row.classList.toggle('selected', Number(row.dataset.selectZone) === selectedZoneIndex);
  });
}

function renderZones() {
  if (!selectedCamera) return;
  syncZoneOverlayToImage();
  const zones = cameraDetection().zones.map(normalizeZone);
  liveEls.zoneOverlay.innerHTML = zones.map(renderZoneBox).join('');

  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No monitoring areas yet. Click "Draw area", then drag on the footage.</div>';
    return;
  }

  liveEls.zoneList.innerHTML = zones.map((zone, index) => `
    <div class="item zone-row ${index === selectedZoneIndex ? 'selected' : ''}" data-select-zone="${index}">
      <input data-zone-name="${index}" value="${escapeHtml(zone.name || `Zone ${index + 1}`)}" />
      <label><span>Motion</span><select data-zone-motion="${index}"><option value="true" ${zone.monitor_motion !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_motion === false ? 'selected' : ''}>Off</option></select></label>
      <label><span>Objects</span><select data-zone-objects="${index}"><option value="true" ${zone.monitor_objects !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_objects === false ? 'selected' : ''}>Off</option></select></label>
      <button class="secondary" type="button" data-delete-zone="${index}">Remove</button>
    </div>
  `).join('');

  document.querySelectorAll('[data-zone-name]').forEach((input) => {
    input.addEventListener('focus', () => { selectedZoneIndex = Number(input.dataset.zoneName); updateSelectionStyles(); });
    input.addEventListener('input', () => {
      const index = Number(input.dataset.zoneName);
      zones[index].name = input.value;
      const label = liveEls.zoneOverlay.querySelector(`[data-zone-index="${index}"] span`);
      if (label) label.textContent = input.value || `Zone ${index + 1}`;
    });
  });
  document.querySelectorAll('[data-zone-motion]').forEach((select) => {
    select.addEventListener('change', () => {
      selectedZoneIndex = Number(select.dataset.zoneMotion);
      zones[selectedZoneIndex].monitor_motion = select.value === 'true';
      renderZones();
    });
  });
  document.querySelectorAll('[data-zone-objects]').forEach((select) => {
    select.addEventListener('change', () => {
      selectedZoneIndex = Number(select.dataset.zoneObjects);
      zones[selectedZoneIndex].monitor_objects = select.value === 'true';
      renderZones();
    });
  });
  document.querySelectorAll('[data-delete-zone]').forEach((button) => {
    button.addEventListener('click', () => {
      zones.splice(Number(button.dataset.deleteZone), 1);
      selectedZoneIndex = null;
      renderZones();
      refreshFrame();
    });
  });
  document.querySelectorAll('[data-select-zone]').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('input, select, button')) return;
      selectedZoneIndex = Number(row.dataset.selectZone);
      renderZones();
    });
  });
}

function pointFromEvent(event) {
  const rect = liveEls.zoneOverlay.getBoundingClientRect();
  return {
    x: clamp((event.clientX - rect.left) / rect.width),
    y: clamp((event.clientY - rect.top) / rect.height),
  };
}

function updateDraggedZone(event) {
  if (!zoneDrag) return;
  const point = pointFromEvent(event);
  const zone = cameraDetection().zones[zoneDrag.index];
  if (!zone) return;
  const dx = point.x - zoneDrag.startPoint.x;
  const dy = point.y - zoneDrag.startPoint.y;

  if (zoneDrag.mode === 'move') {
    zone.x = clamp(zoneDrag.startZone.x + dx, 0, 1 - zone.width);
    zone.y = clamp(zoneDrag.startZone.y + dy, 0, 1 - zone.height);
  } else {
    const start = zoneDrag.startZone;
    let left = start.x;
    let top = start.y;
    let right = start.x + start.width;
    let bottom = start.y + start.height;
    if (zoneDrag.mode.includes('w')) left = clamp(start.x + dx, 0, right - 0.01);
    if (zoneDrag.mode.includes('e')) right = clamp(start.x + start.width + dx, left + 0.01, 1);
    if (zoneDrag.mode.includes('n')) top = clamp(start.y + dy, 0, bottom - 0.01);
    if (zoneDrag.mode.includes('s')) bottom = clamp(start.y + start.height + dy, top + 0.01, 1);
    zone.x = left;
    zone.y = top;
    zone.width = right - left;
    zone.height = bottom - top;
  }

  normalizeZone(zone);
  renderZones();
}

liveEls.zoneOverlay.addEventListener('pointerdown', (event) => {
  if (!selectedCamera) return;
  const resizeHandle = event.target.closest('[data-resize-zone]');
  const zoneBox = event.target.closest('[data-zone-index]');

  if (resizeHandle || zoneBox) {
    event.preventDefault();
    const index = Number((resizeHandle || zoneBox).dataset.zoneIndex);
    const zone = cameraDetection().zones[index];
    selectedZoneIndex = index;
    zoneDrag = {
      index,
      mode: resizeHandle?.dataset.resizeZone || 'move',
      startPoint: pointFromEvent(event),
      startZone: { ...zone },
    };
    liveEls.zoneOverlay.setPointerCapture(event.pointerId);
    renderZones();
    return;
  }

  if (!drawingMode) return;
  const start = pointFromEvent(event);
  draftBox = { start, end: start };
  liveEls.zoneOverlay.setPointerCapture(event.pointerId);
});

liveEls.zoneOverlay.addEventListener('pointermove', (event) => {
  if (zoneDrag) {
    updateDraggedZone(event);
    return;
  }

  if (!draftBox) return;
  draftBox.end = pointFromEvent(event);
  const x = Math.min(draftBox.start.x, draftBox.end.x);
  const y = Math.min(draftBox.start.y, draftBox.end.y);
  const width = Math.abs(draftBox.end.x - draftBox.start.x);
  const height = Math.abs(draftBox.end.y - draftBox.start.y);
  liveEls.zoneOverlay.querySelector('.draft')?.remove();
  liveEls.zoneOverlay.insertAdjacentHTML('beforeend', `<div class="monitor-zone-box draft" style="left:${x * 100}%;top:${y * 100}%;width:${width * 100}%;height:${height * 100}%"><span>New area</span></div>`);
});

liveEls.zoneOverlay.addEventListener('pointerup', (event) => {
  if (zoneDrag) {
    updateDraggedZone(event);
    zoneDrag = null;
    renderZones();
    return;
  }

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
    selectedZoneIndex = zones.length - 1;
  }
  renderZones();
  refreshFrame();
});

liveEls.zoneOverlay.addEventListener('pointercancel', () => {
  draftBox = null;
  zoneDrag = null;
  renderZones();
});

liveEls.frame.addEventListener('load', () => {
  syncZoneOverlayToImage();
  renderZones();
  liveEls.status.textContent = liveEls.overlayToggle.checked
    ? `${selectedCamera?.name || 'Camera'} - object overlay on`
    : `${selectedCamera?.name || 'Camera'} - object overlay off`;
});

liveEls.frame.addEventListener('error', () => {
  liveEls.status.textContent = 'Unable to load live footage. Retrying...';
});

liveEls.overlayToggle.addEventListener('change', refreshFrame);
liveEls.cameraSelect.addEventListener('change', () => setSelectedCamera(liveEls.cameraSelect.value));
liveEls.addZoneBtn.addEventListener('click', () => {
  drawingMode = !drawingMode;
  draftBox = null;
  zoneDrag = null;
  liveEls.addZoneBtn.textContent = drawingMode ? 'Cancel drawing' : 'Draw area';
});
liveEls.motionEnabled.addEventListener('change', () => { cameraDetection().motion_enabled = liveEls.motionEnabled.value === 'true'; });
liveEls.objectDetectionEnabled.addEventListener('change', () => { cameraDetection().object_detection_enabled = liveEls.objectDetectionEnabled.value === 'true'; refreshFrame(); });
liveEls.saveZonesBtn.addEventListener('click', async () => {
  try {
    liveEls.saveZonesBtn.disabled = true;
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}`, { method: 'PUT', body: JSON.stringify(selectedCamera) });
    const payload = await api('/api/cameras');
    const cameraId = selectedCamera.id;
    cameras = payload.cameras || [];
    setSelectedCamera(cameraId);
    liveEls.status.textContent = 'Monitoring areas saved.';
  } catch (error) {
    liveEls.status.textContent = error.message;
  } finally {
    liveEls.saveZonesBtn.disabled = false;
  }
});

window.addEventListener('resize', renderZones);

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
