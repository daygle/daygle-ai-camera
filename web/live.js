const liveEls = {
  frame: document.getElementById('liveFrame'),
  frameWrap: document.getElementById('liveFrameWrap'),
  status: document.getElementById('liveStatus'),
  detectionStatus: document.getElementById('liveDetectionStatus'),
  overlayToggle: document.getElementById('overlayToggle'),
  cameraSelect: document.getElementById('cameraSelect'),
  zoneOverlay: document.getElementById('zoneOverlay'),
  zoneList: document.getElementById('zoneList'),
  cameraRecordingControls: document.getElementById('cameraRecordingControls'),
  addZoneBtn: document.getElementById('addZoneBtn'),
  saveZonesBtn: document.getElementById('saveZonesBtn'),
};

const LIVE_REFRESH_MS = 500;
const CLOSE_DRAFT_DISTANCE = 0.035;
let refreshTimer;
let detectionStatusTimer;
let csrfToken = null;
let cameras = [];
let selectedCamera = null;
let selectedZoneIndex = null;
let drawingMode = false;
let draftPolygon = null;
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
  selectedCamera.detection ||= { zones: [] };
  selectedCamera.detection.zones ||= [];
  return selectedCamera.detection;
}

function cameraRecording() {
  selectedCamera.recording ||= { enabled: true, record_on_alert: true, continuous: false };
  selectedCamera.recording.enabled ??= true;
  selectedCamera.recording.record_on_alert ??= true;
  selectedCamera.recording.continuous ??= false;
  return selectedCamera.recording;
}

function clamp(value, min = 0, max = 1) {
  return Math.max(min, Math.min(max, value));
}

function normalizePoint(point) {
  return {
    x: clamp(Number(point?.x) || 0),
    y: clamp(Number(point?.y) || 0),
  };
}

function rectanglePoints(zone) {
  const x = clamp(Number(zone.x) || 0);
  const y = clamp(Number(zone.y) || 0);
  const width = clamp(Number(zone.width) || 0.01, 0.01, 1 - x);
  const height = clamp(Number(zone.height) || 0.01, 0.01, 1 - y);
  return [
    { x, y },
    { x: x + width, y },
    { x: x + width, y: y + height },
    { x, y: y + height },
  ];
}

function updateZoneBounds(zone) {
  const points = zone.points || rectanglePoints(zone);
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const left = Math.min(...xs);
  const top = Math.min(...ys);
  const right = Math.max(...xs);
  const bottom = Math.max(...ys);
  zone.x = roundCoord(left);
  zone.y = roundCoord(top);
  zone.width = roundCoord(Math.max(0.01, right - left));
  zone.height = roundCoord(Math.max(0.01, bottom - top));
}

function roundCoord(value) {
  return Math.round(clamp(value) * 10000) / 10000;
}

function normalizeZone(zone) {
  const sourcePoints = Array.isArray(zone.points) && zone.points.length >= 3 ? zone.points : rectanglePoints(zone);
  zone.points = sourcePoints.map(normalizePoint);
  zone.object_labels = normalizeLabelList(zone.object_labels);
  updateZoneBounds(zone);
  return zone;
}

function normalizeLabelList(value) {
  const source = Array.isArray(value) ? value : String(value || '').split(',');
  const seen = new Set();
  return source.map((label) => String(label).trim().toLowerCase()).filter((label) => {
    if (!label || seen.has(label)) return false;
    seen.add(label);
    return true;
  });
}

function labelListValue(value) {
  return normalizeLabelList(value).join(', ');
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
  if (document.hidden || liveEls.frame.dataset.loading === 'true') return;
  liveEls.frame.dataset.loading = 'true';
  liveEls.frame.src = snapshotUrl();
}

function formatDetectionStatus(payload) {
  if (!payload) return 'Live AI status unavailable.';
  const labels = (payload.detected_labels || []).join(', ') || 'none';
  const alerts = (payload.triggered_alerts || []).map((alert) => alert.rule_name).join(', ') || 'none';
  const recording = payload.recording_state
    ? `recording ${payload.recording_state}${payload.recording_id ? ` #${payload.recording_id}` : ''}`
    : 'recording pending';
  const email = payload.email_attempted
    ? `email sent/attempted to ${(payload.email_recipients || []).join(', ')}`
    : payload.email_enabled_rules
      ? 'email rule matched but delivery was not attempted'
      : 'no email-enabled matching rule';
  if (payload.state === 'alerted') return `Live AI: alert matched (${alerts}); ${email}; ${recording}.`;
  if (payload.state === 'checked') return `Live AI: checked; labels: ${labels}; ${payload.reason}`;
  return `Live AI: ${payload.state || 'waiting'} - ${payload.reason || payload.ai_error || 'waiting for frames'}`;
}

async function refreshDetectionStatus() {
  if (!selectedCamera || !liveEls.detectionStatus) return;
  try {
    const cameraId = encodeURIComponent(selectedCamera.id);
    liveEls.detectionStatus.textContent = formatDetectionStatus(await api(`/api/live/detection-status?camera_id=${cameraId}`));
  } catch (error) {
    liveEls.detectionStatus.textContent = `Live AI status unavailable: ${error.message}`;
  }
}

function setSelectedCamera(cameraId) {
  selectedCamera = cameras.find((camera) => camera.id === cameraId) || cameras[0];
  if (!selectedCamera) return;
  selectedZoneIndex = null;
  liveEls.cameraSelect.value = selectedCamera.id;
  renderZones();
  renderCameraRecordingControls();
  refreshFrame();
  refreshDetectionStatus();
}

function renderCameraOptions() {
  liveEls.cameraSelect.innerHTML = cameras.map((camera) => `<option value="${escapeHtml(camera.id)}">${escapeHtml(camera.name || camera.id)}</option>`).join('');
  setSelectedCamera(liveEls.cameraSelect.value || cameras[0]?.id);
}

function renderZoneBox(zone, index) {
  const selected = index === selectedZoneIndex ? ' selected' : '';
  const points = zone.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const labelPoint = zone.points[0] || { x: zone.x, y: zone.y };
  const handles = zone.points.map((point, pointIndex) => (
    `<i class="zone-handle zone-point-handle" data-zone-index="${index}" data-point-index="${pointIndex}" style="left:${point.x * 100}%;top:${point.y * 100}%"></i>`
  )).join('');
  return `
    <svg class="monitor-zone-polygon${selected}" data-zone-index="${index}" viewBox="0 0 100 100" preserveAspectRatio="none">
      <polygon data-zone-index="${index}" points="${points}"></polygon>
    </svg>
    <span class="zone-label${selected}" data-zone-index="${index}" style="left:${labelPoint.x * 100}%;top:${labelPoint.y * 100}%">${escapeHtml(zone.name || `Zone ${index + 1}`)}</span>
    ${handles}
  `;
}

function updateSelectionStyles() {
  liveEls.zoneOverlay.querySelectorAll('.monitor-zone-polygon, .zone-label').forEach((element) => {
    element.classList.toggle('selected', Number(element.dataset.zoneIndex) === selectedZoneIndex);
  });
  liveEls.zoneList.querySelectorAll('[data-select-zone]').forEach((row) => {
    row.classList.toggle('selected', Number(row.dataset.selectZone) === selectedZoneIndex);
  });
}

function renderZones() {
  if (!selectedCamera) return;
  syncZoneOverlayToImage();
  const zones = cameraDetection().zones;
  zones.forEach(normalizeZone);
  liveEls.zoneOverlay.innerHTML = zones.map((zone, index) => (zone.enabled === false ? '' : renderZoneBox(zone, index))).join('');

  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No monitoring areas yet. Click "Draw area", place corner dots on the footage, then click the first dot to close the area.</div>';
    return;
  }

  liveEls.zoneList.innerHTML = zones.map((zone, index) => `
    <div class="item zone-row ${index === selectedZoneIndex ? 'selected' : ''}${zone.enabled === false ? ' disabled' : ''}" data-select-zone="${index}">
      <input data-zone-name="${index}" value="${escapeHtml(zone.name || `Zone ${index + 1}`)}" />
      <label><span>Zone</span><select data-zone-enabled="${index}"><option value="true" ${zone.enabled !== false ? 'selected' : ''}>Shown</option><option value="false" ${zone.enabled === false ? 'selected' : ''}>Hidden</option></select></label>
      <label><span>Motion</span><select data-zone-motion="${index}"><option value="true" ${zone.monitor_motion !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_motion === false ? 'selected' : ''}>Off</option></select></label>
      <label><span>Objects</span><select data-zone-objects="${index}"><option value="true" ${zone.monitor_objects !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_objects === false ? 'selected' : ''}>Off</option></select></label>
      <label class="zone-object-labels"><span>Object labels</span><input data-zone-object-labels="${index}" value="${escapeHtml(labelListValue(zone.object_labels))}" placeholder="person, cat" /></label>
      <label><span>ANPR</span><select data-zone-anpr="${index}"><option value="true" ${zone.monitor_anpr !== false ? 'selected' : ''}>On</option><option value="false" ${zone.monitor_anpr === false ? 'selected' : ''}>Off</option></select></label>
      <button class="secondary" type="button" data-delete-zone="${index}">Remove</button>
    </div>
  `).join('');

  document.querySelectorAll('[data-zone-name]').forEach((input) => {
    input.addEventListener('focus', () => { selectedZoneIndex = Number(input.dataset.zoneName); updateSelectionStyles(); });
    input.addEventListener('input', () => {
      const index = Number(input.dataset.zoneName);
      zones[index].name = input.value;
      const label = liveEls.zoneOverlay.querySelector(`.zone-label[data-zone-index="${index}"]`);
      if (label) label.textContent = input.value || `Zone ${index + 1}`;
    });
  });
  document.querySelectorAll('[data-zone-enabled]').forEach((select) => {
    select.addEventListener('change', () => {
      selectedZoneIndex = Number(select.dataset.zoneEnabled);
      zones[selectedZoneIndex].enabled = select.value === 'true';
      renderZones();
      refreshFrame();
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
  document.querySelectorAll('[data-zone-object-labels]').forEach((input) => {
    input.addEventListener('focus', () => { selectedZoneIndex = Number(input.dataset.zoneObjectLabels); updateSelectionStyles(); });
    input.addEventListener('input', () => {
      const index = Number(input.dataset.zoneObjectLabels);
      zones[index].object_labels = normalizeLabelList(input.value);
    });
  });
  document.querySelectorAll('[data-zone-anpr]').forEach((select) => {
    select.addEventListener('change', () => {
      selectedZoneIndex = Number(select.dataset.zoneAnpr);
      zones[selectedZoneIndex].monitor_anpr = select.value === 'true';
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

function renderCameraRecordingControls() {
  if (!selectedCamera || !liveEls.cameraRecordingControls) return;
  const recording = cameraRecording();
  liveEls.cameraRecordingControls.innerHTML = `
    <label><span>Recording</span><select data-camera-recording="enabled"><option value="true" ${recording.enabled !== false ? 'selected' : ''}>Enabled</option><option value="false" ${recording.enabled === false ? 'selected' : ''}>Disabled</option></select></label>
    <label><span>Alert clips</span><select data-camera-recording="record_on_alert"><option value="true" ${recording.record_on_alert !== false ? 'selected' : ''}>Enabled</option><option value="false" ${recording.record_on_alert === false ? 'selected' : ''}>Disabled</option></select></label>
    <label><span>Continuous</span><select data-camera-recording="continuous"><option value="false" ${recording.continuous !== true ? 'selected' : ''}>Disabled</option><option value="true" ${recording.continuous === true ? 'selected' : ''}>Enabled</option></select></label>
  `;
  liveEls.cameraRecordingControls.querySelectorAll('[data-camera-recording]').forEach((select) => {
    select.addEventListener('change', () => {
      cameraRecording()[select.dataset.cameraRecording] = select.value === 'true';
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

function pointDistance(first, second) {
  const dx = first.x - second.x;
  const dy = first.y - second.y;
  return Math.sqrt((dx * dx) + (dy * dy));
}

function updateDraggedZone(event) {
  if (!zoneDrag) return;
  const point = pointFromEvent(event);
  const zone = cameraDetection().zones[zoneDrag.index];
  if (!zone) return;
  const dx = point.x - zoneDrag.startPoint.x;
  const dy = point.y - zoneDrag.startPoint.y;

  if (zoneDrag.mode === 'move') {
    const xs = zoneDrag.startPoints.map((startPoint) => startPoint.x);
    const ys = zoneDrag.startPoints.map((startPoint) => startPoint.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const safeDx = clamp(dx, -minX, 1 - maxX);
    const safeDy = clamp(dy, -minY, 1 - maxY);
    zone.points = zoneDrag.startPoints.map((startPoint) => ({
      x: roundCoord(startPoint.x + safeDx),
      y: roundCoord(startPoint.y + safeDy),
    }));
  } else if (zoneDrag.mode === 'point') {
    zone.points[zoneDrag.pointIndex] = normalizePoint(point);
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

function draftPolygonMarkup() {
  if (!draftPolygon?.points.length) return '';
  const points = [...draftPolygon.points, draftPolygon.preview].filter(Boolean);
  const pointList = points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const completedPointList = draftPolygon.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const handles = draftPolygon.points.map((point, index) => {
    const closesShape = index === 0 && draftPolygon.points.length >= 3;
    return `<i class="zone-handle zone-point-handle draft-point${closesShape ? ' close-draft-point' : ''}" ${closesShape ? 'data-close-draft="true" title="Close area"' : ''} style="left:${point.x * 100}%;top:${point.y * 100}%"></i>`;
  }).join('');
  return `
    <svg class="monitor-zone-polygon draft" viewBox="0 0 100 100" preserveAspectRatio="none">
      ${draftPolygon.points.length >= 3 ? `<polygon class="draft-fill" points="${completedPointList}"></polygon>` : ''}
      <polyline points="${pointList}"></polyline>
    </svg>
    ${handles}
  `;
}

function renderDraftPolygon() {
  liveEls.zoneOverlay.querySelectorAll('.draft, .draft-point').forEach((element) => element.remove());
  liveEls.zoneOverlay.insertAdjacentHTML('beforeend', draftPolygonMarkup());
}

function finishDraftPolygon() {
  if (!draftPolygon || draftPolygon.points.length < 3) return;
  const zones = cameraDetection().zones;
  zones.push({
    id: `zone-${Date.now()}`,
    name: `Zone ${zones.length + 1}`,
    points: draftPolygon.points.map(normalizePoint),
    enabled: true,
    monitor_motion: true,
    monitor_objects: true,
    object_labels: [],
    monitor_anpr: true,
  });
  selectedZoneIndex = zones.length - 1;
  normalizeZone(zones[selectedZoneIndex]);
  draftPolygon = null;
  drawingMode = false;
  liveEls.addZoneBtn.textContent = 'Draw area';
  renderZones();
  refreshFrame();
}

liveEls.zoneOverlay.addEventListener('pointerdown', (event) => {
  if (!selectedCamera) return;

  if (drawingMode) {
    event.preventDefault();
    const point = pointFromEvent(event);
    const firstPoint = draftPolygon?.points[0];
    const closeToFirstPoint = firstPoint && draftPolygon.points.length >= 3 && pointDistance(point, firstPoint) <= CLOSE_DRAFT_DISTANCE;
    if (event.target.closest('[data-close-draft]') || closeToFirstPoint) {
      finishDraftPolygon();
      return;
    }
    draftPolygon ||= { points: [], preview: point };
    draftPolygon.points.push(point);
    draftPolygon.preview = point;
    liveEls.addZoneBtn.textContent = draftPolygon.points.length >= 3 ? 'Finish area' : 'Cancel drawing';
    renderDraftPolygon();
    liveEls.zoneOverlay.setPointerCapture(event.pointerId);
    return;
  }

  const pointHandle = event.target.closest('[data-point-index]');
  const zoneBox = event.target.closest('.monitor-zone-polygon[data-zone-index], .zone-label[data-zone-index], polygon[data-zone-index]');

  if (pointHandle || zoneBox) {
    event.preventDefault();
    const index = Number((pointHandle || zoneBox).dataset.zoneIndex);
    const zone = cameraDetection().zones[index];
    selectedZoneIndex = index;
    zoneDrag = {
      index,
      mode: pointHandle ? 'point' : 'move',
      pointIndex: pointHandle ? Number(pointHandle.dataset.pointIndex) : null,
      startPoint: pointFromEvent(event),
      startZone: { ...zone },
      startPoints: zone.points.map((zonePoint) => ({ ...zonePoint })),
    };
    liveEls.zoneOverlay.setPointerCapture(event.pointerId);
    renderZones();
    return;
  }
});

liveEls.zoneOverlay.addEventListener('pointermove', (event) => {
  if (zoneDrag) {
    updateDraggedZone(event);
    return;
  }

  if (!draftPolygon) return;
  draftPolygon.preview = pointFromEvent(event);
  renderDraftPolygon();
});

liveEls.zoneOverlay.addEventListener('pointerup', (event) => {
  if (zoneDrag) {
    updateDraggedZone(event);
    zoneDrag = null;
    renderZones();
    return;
  }
});

liveEls.zoneOverlay.addEventListener('pointercancel', () => {
  zoneDrag = null;
  renderZones();
  if (draftPolygon) renderDraftPolygon();
});

liveEls.frame.addEventListener('load', () => {
  liveEls.frame.dataset.loading = 'false';
  syncZoneOverlayToImage();
  liveEls.status.textContent = liveEls.overlayToggle.checked
    ? `${selectedCamera?.name || 'Camera'} - object overlay on`
    : `${selectedCamera?.name || 'Camera'} - object overlay off`;
});

liveEls.frame.addEventListener('error', () => {
  liveEls.frame.dataset.loading = 'false';
  liveEls.status.textContent = 'Unable to load live footage. Retrying...';
});

liveEls.overlayToggle.addEventListener('change', refreshFrame);
liveEls.cameraSelect.addEventListener('change', () => setSelectedCamera(liveEls.cameraSelect.value));
liveEls.addZoneBtn.addEventListener('click', () => {
  if (drawingMode && draftPolygon?.points.length >= 3) {
    finishDraftPolygon();
    return;
  }
  drawingMode = !drawingMode;
  draftPolygon = null;
  zoneDrag = null;
  liveEls.addZoneBtn.textContent = drawingMode ? 'Cancel drawing' : 'Draw area';
  renderZones();
});
liveEls.saveZonesBtn.addEventListener('click', async () => {
  try {
    liveEls.saveZonesBtn.disabled = true;
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}`, { method: 'PUT', body: JSON.stringify(selectedCamera) });
    const payload = await api('/api/cameras');
    const cameraId = selectedCamera.id;
    cameras = payload.cameras || [];
    setSelectedCamera(cameraId);
    liveEls.status.textContent = 'Monitoring areas saved.';
    await refreshDetectionStatus();
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
  refreshTimer = setInterval(refreshFrame, LIVE_REFRESH_MS);
  detectionStatusTimer = setInterval(refreshDetectionStatus, 2000);
}

init().catch((error) => { liveEls.status.textContent = error.message; });
window.addEventListener('beforeunload', () => {
  clearInterval(refreshTimer);
  clearInterval(detectionStatusTimer);
});
