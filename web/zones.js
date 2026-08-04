// zones.js - Zone drawing, editing, and object detection rules.
// Loaded after live.js on zones.html only. Accesses live.js globals:
//   isZonesPage, selectedCamera, liveEls, availableLabels,
//   clamp, normalizePoint, roundCoord, normalizeLabelList,
//   api, cameraDetection, refreshFrame, refreshDetectionStatus,
//   CLOSE_DRAFT_DISTANCE_PX.

let selectedZoneIndex = null;
let drawingMode = false;
let draftPolygon = null;
let zoneDrag = null;
let expandedZoneRules = new Set();

// Update only the label text on the Draw Area button so its icon (a sibling
// <svg>) survives. Setting button.textContent would replace all child nodes,
// wiping the icon.
function setAddZoneLabel(text) {
  const label = liveEls.addZoneBtn?.querySelector('.zone-btn-label');
  if (label) label.textContent = text;
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

function defaultObjectRule(label = '') {
  const isMotion = String(label || '').trim().toLowerCase() === 'motion';
  return {
    label: String(label || '').trim().toLowerCase(),
    enabled: true,
    record_on_detect: true,
    min_confidence: isMotion ? 0.45 : 0.5,
    max_confidence: 1,
    cooldown_seconds: 60,
    email_enabled: false,
    email_recipients: [],
    push_enabled: false,
    active_start: null,
    active_end: null,
    notify_start: null,
    notify_end: null,
  };
}

function normalizeObjectRules(zone) {
  if (Array.isArray(zone.object_rules) && zone.object_rules.length) {
    const seen = new Set();
    return zone.object_rules.map((rule) => ({ ...defaultObjectRule(rule?.label), ...rule }))
      .map((rule) => ({
        ...rule,
        label: String(rule.label || '').trim().toLowerCase(),
        enabled: rule.enabled !== false,
        record_on_detect: rule.record_on_detect !== false,
        min_confidence: clamp(Number(rule.min_confidence ?? 0.5), 0, 1),
        // Upper bound of the confidence window. Defaults to 1 (no cap) and is
        // never allowed below min_confidence so the [min, max] band is valid.
        max_confidence: Math.max(
          clamp(Number(rule.min_confidence ?? 0.5), 0, 1),
          clamp(Number(rule.max_confidence ?? 1), 0, 1),
        ),
        cooldown_seconds: Math.max(0, Number.parseInt(rule.cooldown_seconds ?? 60, 10) || 0),
        email_enabled: rule.email_enabled === true,
        email_recipients: normalizeEmailList(rule.email_recipients),
        push_enabled: rule.push_enabled === true,
        active_start: rule.active_start || null,
        active_end: rule.active_end || null,
        notify_start: rule.notify_start || null,
        notify_end: rule.notify_end || null,
      }))
      .filter((rule) => {
        if (!rule.label || seen.has(rule.label)) return false;
        seen.add(rule.label);
        return true;
      });
  }
  return normalizeLabelList(zone.object_labels).map(defaultObjectRule);
}

function normalizeZone(zone) {
  const sourcePoints = Array.isArray(zone.points) && zone.points.length >= 3 ? zone.points : rectanglePoints(zone);
  zone.points = sourcePoints.map(normalizePoint);
  zone.object_rules = normalizeObjectRules(zone);
  zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion').map((rule) => rule.label);
  // Keep the legacy `monitor_motion` flag in sync with the actual rule list
  // so a removed or disabled Motion rule stays gone after save. The backend's
  // normalize_monitoring_zones() re-inserts Motion when it sees
  // ``monitor_motion=true`` but no Motion rule in ``object_rules`` -- without
  // this sync that legacy-migration path would resurrect the rule on every
  // save round-trip, making the delete appear to be ignored.
  zone.monitor_motion = zone.object_rules.some(
    (rule) => String(rule.label || '').trim().toLowerCase() === 'motion'
      && rule.enabled !== false
  );
  updateZoneBounds(zone);
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
  if (!liveEls.zoneOverlay || !liveEls.frameWrap || !liveEls.frame) return;
  const wrapRect = liveEls.frameWrap.getBoundingClientRect();
  const imageRect = visibleImageRect();
  liveEls.zoneOverlay.style.left = `${imageRect.left - wrapRect.left}px`;
  liveEls.zoneOverlay.style.top = `${imageRect.top - wrapRect.top}px`;
  liveEls.zoneOverlay.style.width = `${imageRect.width}px`;
  liveEls.zoneOverlay.style.height = `${imageRect.height}px`;
}

function updateZonesStats() {
  if (!selectedCamera) return;
  const detection = cameraDetection();
  const zones = detection.zones || [];
  const ruleCount = zones.reduce((sum, zone) => sum + (zone.object_rules?.length || 0), 0);
  const alertCount = zones.reduce((sum, zone) => sum + (zone.object_rules || []).filter((r) => r.email_enabled || r.push_enabled).length, 0);
  if (liveEls.statZoneCount) liveEls.statZoneCount.textContent = String(zones.length);
  if (liveEls.statRuleCount) liveEls.statRuleCount.textContent = String(ruleCount);
  if (liveEls.statAlertRules) liveEls.statAlertRules.textContent = String(alertCount);
  if (liveEls.statCameraName) {
    liveEls.statCameraName.textContent = selectedCamera.name || selectedCamera.id || '-';
  }
  const zonesListCount = document.getElementById('zonesListCount');
  if (zonesListCount) {
    zonesListCount.textContent = `${zones.length} area${zones.length === 1 ? '' : 's'}`;
  }
}

function renderZoneBox(zone, index) {
  const selected = index === selectedZoneIndex ? ' selected' : '';
  const points = zone.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ');
  const labelPoint = { x: zone.x, y: zone.y };
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
  liveEls.zoneOverlay?.querySelectorAll('.monitor-zone-polygon, .zone-label').forEach((element) => {
    element.classList.toggle('selected', Number(element.dataset.zoneIndex) === selectedZoneIndex);
  });
  liveEls.zoneList?.querySelectorAll('[data-select-zone]').forEach((row) => {
    row.classList.toggle('selected', Number(row.dataset.selectZone) === selectedZoneIndex);
  });
}

function objectRuleOptions(selectedLabel) {
  const labels = [...new Set([...availableLabels, selectedLabel].filter((l) => Boolean(l) && l !== 'motion'))];
  // Display labels in title case for readability; the value attribute stays
  // raw lowercase because rule.label is the canonical lookup key used by
  // defaultObjectRule, normalizeObjectRules, and backend filters.
  const coco = labels.map((label) => `<option value="${escapeHtml(label)}" ${label === selectedLabel ? 'selected' : ''}>${escapeHtml(titleCase(label))}</option>`).join('');
  const motionSelected = selectedLabel === 'motion';
  return `<option value="">Add Object...</option><option value="motion" ${motionSelected ? 'selected' : ''}>Motion</option>${coco}`;
}

function renderObjectRules(zone, zoneIndex) {
  zone.object_rules = normalizeObjectRules(zone);
  if (!zone.object_rules.length) {
    return '<div class="empty compact-empty">No object rules yet. Choose an object above to add detection settings for this zone.</div>';
  }
  return `<div class="cameras-table-wrap"><table class="rule-table" data-zone-rules-table="${zoneIndex}">
    <thead><tr>
      <th class="cell-drag-header"></th>
      <th>Object</th>
      <th class="cell-center">Enabled</th>
      <th class="cell-center">Record</th>
      <th class="cell-center">Email</th>
      <th class="cell-center">Push</th>
      <th>Min Conf</th>
      <th>Max Conf</th>
      <th>Cooldown (s)</th>
      <th></th>
    </tr></thead>
    <tbody>${zone.object_rules.map((rule, ruleIndex) => {
      const key = `${zoneIndex}:${ruleIndex}`;
      const label = escapeHtml(titleCase(rule.label));
      const expanded = expandedZoneRules.has(key);
      return `          <tr draggable="true" data-drag-zone-rule="${key}">
            <td class="cell-drag"><span class="drag-handle" title="Drag to reorder">${ICONS.grip}</span></td>
            <td class="cell-label">${label}</td>
          <td class="cell-center"><input type="checkbox" data-zone-rule-enabled="${key}" ${rule.enabled !== false ? 'checked' : ''} /></td>
          <td class="cell-center"><input type="checkbox" data-zone-rule-record="${key}" ${rule.record_on_detect !== false ? 'checked' : ''} /></td>
          <td class="cell-center"><input type="checkbox" data-zone-rule-email="${key}" ${rule.email_enabled === true ? 'checked' : ''} /></td>
          <td class="cell-center"><input type="checkbox" data-zone-rule-push="${key}" ${rule.push_enabled === true ? 'checked' : ''} /></td>
          <td><input type="number" data-zone-rule-confidence="${key}" value="${escapeHtml(rule.min_confidence)}" min="0" max="1" step="0.05" title="Minimum confidence (0-1). Overrides the global ONNX slider for this object in this zone." /></td>
          <td><input type="number" data-zone-rule-max-confidence="${key}" value="${escapeHtml(rule.max_confidence ?? 1)}" min="0" max="1" step="0.05" title="Maximum confidence (0-1). Detections above this are ignored for this rule. 1 = no upper limit." /></td>
          <td><input type="number" data-zone-rule-cooldown="${key}" value="${escapeHtml(rule.cooldown_seconds)}" min="0" max="3600" step="5" /></td>
          <td><div class="cell-actions">
            <button class="rule-expand-btn secondary" type="button" data-expand-zone-rule="${key}">${expanded ? ICONS.chevronUp : ICONS.email}</button>
            <button class="delete-btn secondary zone-action-btn" type="button" data-delete-zone-rule="${key}">${ICONS.remove}</button>
          </div></td>
        </tr>
        ${renderRuleExpandRow('zone-rule', key, rule, expanded)}`;
    }).join('')}</tbody>
  </table></div>`;
}

function renderZones() {
  if (!selectedCamera) return;
  syncZoneOverlayToImage();
  const zones = cameraDetection().zones;
  zones.forEach(normalizeZone);
  liveEls.zoneOverlay.innerHTML = zones.map((zone, index) => (zone.enabled === false ? '' : renderZoneBox(zone, index))).join('');
  updateZonesStats();
  if (!zones.length) {
    liveEls.zoneList.innerHTML = '<div class="empty">No Zone Areas yet. Click "Draw Area", place corner dots on the footage, then click the first dot to close the area.</div>';
    renderObjectDetectionRules();
    return;
  }
  liveEls.zoneList.innerHTML = zones.map((zone, index) => `
    <div class="item zone-row ${index === selectedZoneIndex ? 'selected' : ''}${zone.enabled === false ? ' disabled' : ''}" data-select-zone="${index}">
      <div class="zone-row-main">
        <div class="zone-name-field">
          ${ICONS.edit}
          <input data-zone-name="${index}" value="${escapeHtml(zone.name || `Zone ${index + 1}`)}" placeholder="Zone name…" aria-label="Name for ${escapeHtml(zone.name || `Zone ${index + 1}`)}" />
        </div>
        <div class="zone-visibility-field">
          <span>Visibility</span>
          <button class="secondary zone-visibility-toggle ${zone.enabled !== false ? 'is-shown' : 'is-hidden'}" type="button" data-zone-enabled="${index}" aria-label="${zone.enabled !== false ? 'Hide' : 'Show'} ${escapeHtml(zone.name || `Zone ${index + 1}`)} area" aria-pressed="${zone.enabled !== false}">${zone.enabled !== false ? 'Shown' : 'Hidden'}</button>
        </div>
        <button class="btn-danger zone-action-btn" type="button" data-delete-zone="${index}">${ICONS.remove}Remove</button>
      </div>
    </div>
  `).join('');
  bindZoneControls(zones);
  renderObjectDetectionRules();
}

function renderObjectDetectionRules() {
  const container = document.getElementById('objectDetectionRules');
  if (!container) return;
  if (!selectedCamera) { container.innerHTML = ''; return; }
  const zones = cameraDetection().zones;
  if (!zones.length) {
    container.innerHTML = '<p class="muted empty-message">No Zone Areas configured. Draw an area above first.</p>';
    return;
  }
  container.innerHTML = zones.map((zone, zoneIndex) => {
    zone.object_rules = normalizeObjectRules(zone);
    const zoneName = escapeHtml(zone.name || `Zone ${zoneIndex + 1}`);
    const addOptions = objectRuleOptions('');
    const rulesHtml = zone.object_rules.length
      ? renderObjectRules(zone, zoneIndex)
      : '<p class="muted empty-message">No rules yet. Add an object below.</p>';
    return `
      <div class="zone-object-rules" data-zone-rules-for="${zoneIndex}">
        <div class="zone-name-card"><span class="zone-name-kicker">Area</span><strong>${zoneName}</strong></div>
        <div class="zone-object-rules-header">
          <select data-add-zone-rule="${zoneIndex}" class="rule-add-select">${addOptions}</select>
        </div>
        ${rulesHtml}
      </div>`;
  }).join('');
  bindObjectRuleControls();
}

function bindObjectRuleControls() {
  document.querySelectorAll('[data-add-zone-rule]').forEach((select) => {
    select.addEventListener('change', () => {
      const label = select.value;
      if (!label) return;
      const zones = cameraDetection().zones;
      const zone = zones[Number(select.dataset.addZoneRule)];
      zone.object_rules = normalizeObjectRules(zone);
      if (!zone.object_rules.some((rule) => rule.label === label)) zone.object_rules.push(defaultObjectRule(label));
      zone.object_labels = zone.object_rules.filter((r) => r.label !== 'motion').map((rule) => rule.label);
      renderZones();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-expand-zone-rule]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.expandZoneRule;
      if (expandedZoneRules.has(key)) expandedZoneRules.delete(key);
      else expandedZoneRules.add(key);
      renderObjectDetectionRules();
    });
  });
  // Drag-and-drop reorder handlers for zone object rules
  document.querySelectorAll('[data-drag-zone-rule]').forEach((row) => {
    const table = row.closest('table[data-zone-rules-table]');
    row.addEventListener('dragstart', (event) => {
      row.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', row.dataset.dragZoneRule);
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      if (table) table.querySelectorAll('tr').forEach((r) => r.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      if (table) table.querySelectorAll('tr[data-drag-zone-rule]').forEach((r) => r.classList.remove('drag-over'));
      row.classList.add('drag-over');
    });
    row.addEventListener('drop', (event) => {
      event.preventDefault();
      row.classList.remove('drag-over');
      const draggedKey = event.dataTransfer.getData('text/plain');
      const targetKey = row.dataset.dragZoneRule;
      if (!draggedKey || draggedKey === targetKey) return;
      const [dZoneI, dRuleI] = draggedKey.split(':').map(Number);
      const [tZoneI, tRuleI] = targetKey.split(':').map(Number);
      if (dZoneI !== tZoneI) return;
      const zones = cameraDetection().zones;
      const zone = zones[dZoneI];
      if (!zone) return;
      const rules = zone.object_rules;
      const dragged = rules.splice(dRuleI, 1)[0];
      rules.splice(tRuleI, 0, dragged);
      zone.object_labels = rules.filter((r) => r.label !== 'motion').map((r) => r.label);
      renderObjectDetectionRules();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-delete-zone-rule]').forEach((button) => {
    button.addEventListener('click', () => {
      const zones = cameraDetection().zones;
      const { zoneIndex, ruleIndex } = parseZoneRuleKey(button.dataset.deleteZoneRule);
      const removedRule = zones[zoneIndex]?.object_rules?.[ruleIndex];
      if (!removedRule) return;
      const displayLabel = titleCase(removedRule.label || '');
      const enabledFlip = [];
      if (removedRule.enabled !== false) enabledFlip.push('detection');
      if (removedRule.record_on_detect !== false) enabledFlip.push('recording');
      if (removedRule.email_enabled === true) enabledFlip.push('email alerts');
      if (removedRule.push_enabled === true) enabledFlip.push('push notifications');
      const activeHint = enabledFlip.length
        ? ` This rule currently has ${enabledFlip.join(' and ')} enabled.`
        : '';
      if (!window.confirm(`Delete the ${displayLabel} rule from this zone?${activeHint}`)) return;
      expandedZoneRules.delete(button.dataset.deleteZoneRule);
      const removedLabel = String(removedRule.label || '').trim().toLowerCase();
      zones[zoneIndex].object_rules.splice(ruleIndex, 1);
      if (removedLabel === 'motion') {
        // Belt-and-suspenders alongside the normalizeZone() sync above: clear
        // the legacy flag immediately so the in-memory model is consistent
        // even if the next save path bypasses a renderZones() re-render.
        zones[zoneIndex].monitor_motion = false;
      }
      zones[zoneIndex].object_labels = zones[zoneIndex].object_rules.filter((r) => r.label !== 'motion').map((r) => r.label);
      renderZones();
      markZoneUnsaved();
    });
  });
  bindRuleFields();
}

function parseZoneRuleKey(value) {
  const [zoneIndex, ruleIndex] = String(value).split(':').map((part) => Number.parseInt(part, 10));
  return { zoneIndex, ruleIndex, rule: cameraDetection().zones[zoneIndex]?.object_rules?.[ruleIndex] };
}

function bindZoneControls(zones) {
  document.querySelectorAll('[data-zone-name]').forEach((input) => {
    input.addEventListener('focus', () => { selectedZoneIndex = Number(input.dataset.zoneName); updateSelectionStyles(); });
    input.addEventListener('input', () => {
      const index = Number(input.dataset.zoneName);
      zones[index].name = input.value;
      const label = liveEls.zoneOverlay.querySelector(`.zone-label[data-zone-index="${index}"]`);
      if (label) label.textContent = input.value || `Zone ${index + 1}`;
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-zone-enabled]').forEach((button) => {
    button.addEventListener('click', () => {
      selectedZoneIndex = Number(button.dataset.zoneEnabled);
      zones[selectedZoneIndex].enabled = zones[selectedZoneIndex].enabled === false;
      renderZones();
      refreshFrame();
      markZoneUnsaved();
    });
  });
  document.querySelectorAll('[data-delete-zone]').forEach((button) => {
    button.addEventListener('click', () => {
      const index = Number(button.dataset.deleteZone);
      const zone = zones[index];
      const ruleCount = Array.isArray(zone?.object_rules) ? zone.object_rules.length : 0;
      const label = escapeHtml(zone?.name || `Zone ${index + 1}`);
      const ruleHint = ruleCount
        ? ` This zone has ${ruleCount} object rule${ruleCount === 1 ? '' : 's'} that will also be deleted.`
        : '';
      if (!window.confirm(`Delete the ${label} area?${ruleHint}`)) return;
      zones.splice(index, 1);
      selectedZoneIndex = null;
      renderZones();
      refreshFrame();
      markZoneUnsaved();
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

function bindRuleFields() {
  const checkboxBindings = [
    ['zoneRuleEnabled', 'enabled'],
    ['zoneRuleRecord', 'record_on_detect'],
    ['zoneRuleEmail', 'email_enabled'],
    ['zoneRulePush', 'push_enabled'],
  ];
  checkboxBindings.forEach(([datasetKey, ruleKey]) => {
    document.querySelectorAll(`input[type="checkbox"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((cb) => {
      cb.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(cb.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = cb.checked;
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
        markZoneUnsaved();
      });
    });
  });
  const numberBindings = [
    ['zoneRuleConfidence', 'min_confidence', (value) => clamp(Number(value || 0), 0, 1)],
    // Stored as-typed in [0, 1]; normalizeObjectRules re-clamps max >= min on
    // the next render, and the server does the same on save. A CLEARED field
    // arrives as '' (not nullish), so map empty back to the documented default
    // of 1 (no cap) -- otherwise Number('') would be 0 and clear-and-save would
    // collapse the rule to a [min, min] window that suppresses everything.
    ['zoneRuleMaxConfidence', 'max_confidence', (value) => {
      const text = String(value ?? '').trim();
      return text === '' ? 1 : clamp(Number(text), 0, 1);
    }],
    ['zoneRuleCooldown', 'cooldown_seconds', (value) => Math.max(0, Number.parseInt(value || 0, 10) || 0)],
  ];
  numberBindings.forEach(([datasetKey, ruleKey, transform]) => {
    document.querySelectorAll(`input[type="number"][data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}]`).forEach((inp) => {
      inp.addEventListener('change', () => {
        const { zoneIndex, rule } = parseZoneRuleKey(inp.dataset[datasetKey]);
        if (!rule) return;
        rule[ruleKey] = transform(inp.value);
        cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
        markZoneUnsaved();
      });
    });
  });
  document.querySelectorAll('input[data-zone-rule-email-recipients]').forEach((input) => {
    input.addEventListener('change', () => {
      const { zoneIndex, rule } = parseZoneRuleKey(input.dataset.zoneRuleEmailRecipients);
      if (!rule) return;
      rule.email_recipients = normalizeEmailList(input.value);
      cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
      markZoneUnsaved();
    });
  });
  [
    ['zoneRuleActiveStart', 'active_start'],
    ['zoneRuleActiveEnd', 'active_end'],
    ['zoneRuleNotifyStart', 'notify_start'],
    ['zoneRuleNotifyEnd', 'notify_end'],
  ].forEach(([datasetKey, ruleKey]) => {
    const attr = `data-${datasetKey.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`)}`;
    document.querySelectorAll(`[${attr}]`).forEach((wrap) => {
      wrap.querySelectorAll('select').forEach((sel) => {
        sel.addEventListener('change', () => {
          const { zoneIndex, rule } = parseZoneRuleKey(wrap.dataset[datasetKey]);
          if (!rule) return;
          rule[ruleKey] = timeSelectValue(wrap);
          cameraDetection().zones[zoneIndex].object_labels = normalizeObjectRules(cameraDetection().zones[zoneIndex]).filter((item) => item.label !== 'motion').map((item) => item.label);
          markZoneUnsaved();
        });
      });
    });
  });
}

function pointFromEvent(event) {
  const rect = liveEls.zoneOverlay.getBoundingClientRect();
  return { x: clamp((event.clientX - rect.left) / rect.width), y: clamp((event.clientY - rect.top) / rect.height) };
}

function pointDistancePx(first, second, rect) {
  const dx = (first.x - second.x) * rect.width;
  const dy = (first.y - second.y) * rect.height;
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
    const safeDx = clamp(dx, -Math.min(...xs), 1 - Math.max(...xs));
    const safeDy = clamp(dy, -Math.min(...ys), 1 - Math.max(...ys));
    zone.points = zoneDrag.startPoints.map((startPoint) => ({ x: roundCoord(startPoint.x + safeDx), y: roundCoord(startPoint.y + safeDy) }));
  } else if (zoneDrag.mode === 'point') {
    zone.points[zoneDrag.pointIndex] = normalizePoint(point);
  }
  normalizeZone(zone);
  renderZones();
  markZoneUnsaved();
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
    object_labels: [],
    object_rules: [],
  });
  selectedZoneIndex = zones.length - 1;
  normalizeZone(zones[selectedZoneIndex]);
  draftPolygon = null;
  drawingMode = false;
  setAddZoneLabel('Draw Area');
  renderZones();
  refreshFrame();
  markZoneUnsaved();
}

function addFullFrameZone() {
  if (!selectedCamera) return;
  const zones = cameraDetection().zones;
  zones.push({
    id: `zone-${Date.now()}`,
    name: `Zone ${zones.length + 1}`,
    points: [
      { x: 0, y: 0 },
      { x: 1, y: 0 },
      { x: 1, y: 1 },
      { x: 0, y: 1 },
    ],
    enabled: true,
    object_labels: [],
    object_rules: [],
  });
  selectedZoneIndex = zones.length - 1;
  draftPolygon = null;
  drawingMode = false;
  zoneDrag = null;
  setAddZoneLabel('Draw Area');
  normalizeZone(zones[selectedZoneIndex]);
  renderZones();
  refreshFrame();
  markZoneUnsaved();
}

function bindZoneDrawing() {
  if (!liveEls.zoneOverlay) return;
  liveEls.zoneOverlay.addEventListener('pointerdown', (event) => {
    if (!selectedCamera) return;
    if (drawingMode) {
      event.preventDefault();
      const point = pointFromEvent(event);
      const firstPoint = draftPolygon?.points[0];
      const overlayRect = liveEls.zoneOverlay.getBoundingClientRect();
      const closeToFirstPoint = firstPoint && draftPolygon.points.length >= 3 && pointDistancePx(point, firstPoint, overlayRect) <= CLOSE_DRAFT_DISTANCE_PX;
      if (event.target.closest('[data-close-draft]') || closeToFirstPoint) {
        finishDraftPolygon();
        return;
      }
      draftPolygon ||= { points: [], preview: point };
      draftPolygon.points.push(point);
      draftPolygon.preview = point;
      setAddZoneLabel(draftPolygon.points.length >= 3 ? 'Finish area' : 'Cancel drawing');
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
        startPoints: zone.points.map((zonePoint) => ({ ...zonePoint })),
      };
      liveEls.zoneOverlay.setPointerCapture(event.pointerId);
      renderZones();
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
    }
  });
  liveEls.zoneOverlay.addEventListener('pointercancel', () => {
    zoneDrag = null;
    renderZones();
    if (draftPolygon) renderDraftPolygon();
  });
}

function toggleDrawingMode() {
  if (drawingMode && draftPolygon?.points.length >= 3) {
    finishDraftPolygon();
    return;
  }
  drawingMode = !drawingMode;
  draftPolygon = null;
  zoneDrag = null;
  setAddZoneLabel(drawingMode ? 'Cancel drawing' : 'Draw Area');
  renderZones();
}

liveEls.addZoneBtn?.addEventListener('click', toggleDrawingMode);

liveEls.fullFrameZoneBtn?.addEventListener('click', () => {
  addFullFrameZone();
  liveEls.status.textContent = 'Full-frame zone added - click Save Zones to apply.';
});

let hasUnsavedZoneChanges = false;

function markZoneUnsaved() {
  if (hasUnsavedZoneChanges) return;
  hasUnsavedZoneChanges = true;
  const btn = document.getElementById('saveZonesBtnHeader');
  if (btn) btn.style.display = '';
  liveEls.status.textContent = 'Unsaved changes - click Save Zones to apply.';
  liveEls.status.classList.add('has-unsaved');
}

function markZoneSaved() {
  hasUnsavedZoneChanges = false;
  const btn = document.getElementById('saveZonesBtnHeader');
  if (btn) btn.style.display = 'none';
  liveEls.status.classList.remove('has-unsaved');
}

async function saveZones() {
  try {
    liveEls.saveZonesBtn.disabled = true;
    const headerBtn = document.getElementById('saveZonesBtnHeader');
    if (headerBtn) headerBtn.disabled = true;
    cameraDetection().zones.forEach(normalizeZone);
    await api(`/api/cameras/${encodeURIComponent(selectedCamera.id)}`, { method: 'PUT', body: JSON.stringify(selectedCamera) });
    const payload = await api('/api/cameras');
    const cameraId = selectedCamera.id;
    cameras = payload.cameras || [];
    setSelectedCamera(cameraId);
    markZoneSaved();
    liveEls.status.textContent = 'Zones saved successfully.';
    window.showToast?.('Zones saved successfully.');
    await refreshDetectionStatus();
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    liveEls.status.textContent = error.message;
    window.showToast?.(error.message, true);
  } finally {
    liveEls.saveZonesBtn.disabled = false;
    const headerBtn = document.getElementById('saveZonesBtnHeader');
    if (headerBtn) headerBtn.disabled = false;
  }
}

liveEls.saveZonesBtn?.addEventListener('click', saveZones);
document.getElementById('saveZonesBtnHeader')?.addEventListener('click', saveZones);

window.addEventListener('resize', syncZoneOverlayToImage);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && drawingMode) {
    drawingMode = false;
    draftPolygon = null;
    setAddZoneLabel('Draw Area');
    renderDraftPolygon();
  }
});

window.addEventListener('beforeunload', (event) => {
  if (!hasUnsavedZoneChanges) return;
  event.preventDefault();
  event.returnValue = '';
});

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 's') {
    event.preventDefault();
    if (hasUnsavedZoneChanges) saveZones();
  }
});
