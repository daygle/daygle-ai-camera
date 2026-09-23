requireElements(['cameraSelect', 'zoneSelect', 'alertTypeSelect', 'addAlertBtn', 'saveAlertsBtn', 'alertsList']);

let cameras = [];
let soundClasses = [];
let enrolledPeople = [];
let faceRulesPayload = { rules: [] };
let cameraIndex = 0;
let zoneIndex = 0;
let alertType = 'object';

const $ = (id) => document.getElementById(id);
const currentCamera = () => cameras[cameraIndex];
const currentZone = () => currentCamera()?.detection?.zones?.[zoneIndex];
const currentPeopleRules = () => (faceRulesPayload.rules || []).filter((rule) => {
  const cameraMatches = !rule.camera_id || String(rule.camera_id) === String(currentCamera()?.id || '');
  const zoneMatches = !rule.zone_id || String(rule.zone_id) === String(currentZone()?.id || '');
  return cameraMatches && zoneMatches;
});
const currentRules = () => {
  if (alertType === 'sound') return currentCamera()?.detection?.sound?.rules || [];
  if (alertType === 'people') return currentPeopleRules();
  if (alertType === 'tripwire') {
    const wire = currentZone()?.tripwire;
    return wire ? [wire] : [];
  }
  if (alertType === 'loiter') {
    const rule = currentZone()?.loiter;
    return rule ? [rule] : [];
  }
  return currentZone()?.object_rules || [];
};

function defaultPeopleRule(personId, personName) {
  const cameraId = String(currentCamera()?.id || '');
  const zoneId = String(currentZone()?.id || '');
  const unknown = !personId;
  return {
    id: unknown ? `_unknown:${cameraId}:${zoneId}` : `zone:${zoneId}:person:${personId}`,
    person_id: unknown ? null : personId,
    name: personName || 'Unknown Person',
    enabled: true,
    email_enabled: false,
    push_enabled: false,
    email_recipients: '',
    cooldown_minutes: 5,
    min_confidence: null,
    camera_id: cameraId,
    zone_id: zoneId,
  };
}

function defaultRule(label = 'person', type = alertType) {
  if (type === 'sound') {
    const sound = soundClasses.find((item) => item.id === label);
    return { class: label, name: sound?.label || label, enabled: true, record_on_detect: true, confidence_threshold: sound?.default_threshold ?? 0.35, cooldown_seconds: sound?.default_cooldown ?? 30, email_enabled: false, email_recipients: [], push_enabled: false, active_start: null, active_end: null, notify_start: null, notify_end: null };
  }
  return { id: `${label}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, label, enabled: true, record_on_detect: true, min_confidence: label === 'motion' || label === 'face' ? 0.45 : 0.5, max_confidence: 1, cooldown_seconds: 60, email_enabled: false, email_recipients: [], push_enabled: false, active_start: null, active_end: null, notify_start: null, notify_end: null };
}

function timeSelect(value, attr) {
  const options = ['<option value="">Any time</option>'];
  for (let hour = 0; hour < 24; hour += 1) for (let minute = 0; minute < 60; minute += 15) {
    const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
    options.push(`<option value="${time}" ${time === value ? 'selected' : ''}>${time}</option>`);
  }
  return `<select ${attr}>${options.join('')}</select>`;
}

function allPolicies() {
  const objectPolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).flatMap((zone) => zone.object_rules || []));
  const soundPolicies = cameras.flatMap((camera) => camera.detection?.sound?.rules || []);
  const tripwirePolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).map((zone) => zone.tripwire).filter(Boolean));
  const loiterPolicies = cameras.flatMap((camera) => (camera.detection?.zones || []).map((zone) => zone.loiter).filter(Boolean));
  return [...objectPolicies, ...soundPolicies, ...tripwirePolicies, ...loiterPolicies, ...(faceRulesPayload.rules || [])].filter((rule) => rule.email_enabled || rule.push_enabled);
}

function updateStats() {
  const policies = allPolicies();
  $('alertCount').textContent = String(policies.length);
  $('enabledCount').textContent = String(policies.filter((rule) => rule.enabled !== false).length);
  $('channelCount').textContent = String(policies.filter((rule) => rule.email_enabled).length + policies.filter((rule) => rule.push_enabled).length);
}

function renderSelectors() {
  $('cameraSelect').innerHTML = cameras.map((camera, index) => `<option value="${index}">${escapeHtml(camera.name || camera.id)}</option>`).join('') || '<option>No cameras</option>';
  $('cameraSelect').value = String(cameraIndex);
  const zones = currentCamera()?.detection?.zones || [];
  zoneIndex = Math.min(zoneIndex, Math.max(0, zones.length - 1));
  $('zoneSelect').innerHTML = zones.map((zone, index) => `<option value="${index}">${escapeHtml(zone.name || `Zone ${index + 1}`)}</option>`).join('') || '<option>No zones</option>';
  $('zoneSelect').value = String(zoneIndex);
  $('zoneSelect').disabled = alertType === 'sound' || !zones.length;
  // Objects and sounds are assigned on the Zones/Sounds pages and appear here
  // automatically, so only recognized-person alerts are added from this page.
  $('addAlertBtn').style.display = alertType === 'people' ? '' : 'none';
  const hint = $('alertScopeHint');
  if (hint) {
    hint.textContent = alertType === 'object'
      ? 'Objects, motion, and faces are assigned - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
      : alertType === 'sound'
        ? 'Sound classes are assigned - and set to record - per camera on the Sounds page. Here you choose how each one notifies you.'
        : alertType === 'tripwire'
          ? 'Line crossings are drawn - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
          : alertType === 'loiter'
            ? 'Loitering is enabled - and set to record - per area on the Zones page. Here you choose how each one notifies you.'
            : 'Add recognized-person and stranger alerts here. Enrol people on the Face Recognition page.';
  }
}

function ruleLabel(rule) {
  if (alertType === 'sound') return titleCase(rule.name || soundClasses.find((item) => item.id === rule.class)?.label || rule.class);
  if (alertType === 'people') return rule.name || 'Unknown Person';
  if (alertType === 'tripwire') return titleCase(rule.name || 'Tripwire');
  if (alertType === 'loiter') return titleCase(rule.name || 'Loitering');
  return String(rule.label || '').replace(/\b\w/g, (char) => char.toUpperCase());
}

// Only recognized-person policies still pick their subject here; objects and
// sound classes are fixed by their assignment on the Zones and Sounds pages.
function ruleOptions(rule) {
  const people = [{ id: '', name: 'Unknown Person' }, ...(enrolledPeople || []).map((person) => ({ id: String(person.id), name: person.name }))];
  return people.map((person) => `<option value="${escapeHtml(person.id)}" ${String(rule.person_id || '') === person.id ? 'selected' : ''}>${escapeHtml(person.name)}</option>`).join('');
}

function scopeLabel() {
  if (alertType === 'sound') return currentCamera() ? `Camera Sound · ${currentCamera().name || currentCamera().id}` : 'Camera Sound';
  return currentZone() ? `Zone · ${currentZone().name || `Zone ${zoneIndex + 1}`}` : 'Zone';
}

// Sound alerts are per-camera and never need a zone; object and
// recognized-person policies are scoped to a specific zone.
function scopeRequiresZone() {
  return alertType !== 'sound';
}

function renderPolicies() {
  const rules = currentRules();
  updateStats();
  if (!currentCamera()) {
    $('alertsList').innerHTML = '<div class="empty">Add a camera first.</div>';
    return;
  }
  if (scopeRequiresZone() && !currentZone()) {
    $('alertsList').innerHTML = '<div class="empty">Draw a zone on the Zones page first, then add object or person policies here.</div>';
    return;
  }
  if (!rules.length) {
    const emptyMessage = alertType === 'object'
      ? 'No objects are assigned to this area yet. Open the <a href="/zones">Zones</a> page to add object, motion, or face detection, then configure alerts here.'
      : alertType === 'sound'
        ? 'No sound classes are assigned to this camera yet. Open the <a href="/sounds">Sounds</a> page to enable classes, then configure alerts here.'
        : alertType === 'tripwire'
          ? 'No line crossing is configured for this area yet. Open the <a href="/zones">Zones</a> page, turn on Line crossing and draw a line, then set its alerts here.'
          : alertType === 'loiter'
            ? 'No loitering rule is configured for this area yet. Open the <a href="/zones">Zones</a> page, turn on Loitering, then set its alerts here.'
            : 'No recognized-person alert policies yet. Add one below to get started.';
    $('alertsList').innerHTML = `<div class="empty">${emptyMessage}</div>`;
    return;
  }
  $('alertsList').innerHTML = rules.map((rule, index) => {
    const people = alertType === 'people';
    const sound = alertType === 'sound';
    const tripwire = alertType === 'tripwire';
    const loiter = alertType === 'loiter';
    // Tripwire and loiter are single per-zone behaviour rules: no confidence
    // axis, and detection runs whenever enabled, so they show only a Notify
    // window (no Detect-from/until) - just how each one notifies you.
    const behaviour = tripwire || loiter;
    const confidence = people ? rule.min_confidence : sound ? rule.confidence_threshold : rule.min_confidence;
    const cooldown = people ? rule.cooldown_minutes : rule.cooldown_seconds;
    const confidenceLabel = people ? 'Minimum Recognition Confidence' : sound ? 'Confidence Threshold' : 'Minimum Confidence';
    const cooldownLabel = people ? 'Cooldown (Minutes)' : 'Cooldown (Seconds)';
    return `
    <article class="alerts-policy ${rule.enabled !== false ? 'is-enabled' : ''}" data-rule-index="${index}">
      <div class="alerts-policy-head"><div><span class="zones-panel-kicker">${escapeHtml(scopeLabel())} · Policy ${index + 1}</span><h3>${escapeHtml(ruleLabel(rule))}</h3></div><label class="toggle-control"><input data-field="enabled" type="checkbox" ${rule.enabled !== false ? 'checked' : ''}><span>${rule.enabled !== false ? 'Enabled' : 'Disabled'}</span></label></div>
      <div class="alerts-policy-grid">
        ${people ? `<label><span>Person</span><select data-field="person_id">${ruleOptions(rule)}</select></label>` : ''}
        ${behaviour ? '' : `<label><span>${confidenceLabel}</span><input data-field="${people ? 'min_confidence' : sound ? 'confidence_threshold' : 'min_confidence'}" type="number" min="0" max="1" step="0.01" value="${escapeHtml(String(confidence ?? (people ? '' : 0.5)))}"></label>`}
        ${!people && !sound && !behaviour ? '<label><span>Maximum Confidence</span><input data-field="max_confidence" type="number" min="0" max="1" step="0.01" value="' + escapeHtml(String(rule.max_confidence ?? 1)) + '"></label>' : ''}
        <label><span>${cooldownLabel}</span><input data-field="${people ? 'cooldown_minutes' : 'cooldown_seconds'}" type="number" min="0" max="${people ? '1440' : '3600'}" step="${people ? '1' : '5'}" value="${escapeHtml(String(cooldown ?? (people ? 5 : 60)))}"></label>
      </div>
      <div class="alerts-channel-row"><label><input data-field="email_enabled" type="checkbox" ${rule.email_enabled ? 'checked' : ''}> Email</label><label><input data-field="push_enabled" type="checkbox" ${rule.push_enabled ? 'checked' : ''}> Push</label></div>
      ${people ? '' : behaviour
        ? `<div class="alerts-policy-grid alerts-schedule-grid"><label><span>Notify From</span>${timeSelect(rule.notify_start, 'data-field="notify_start"')}</label><label><span>Notify Until</span>${timeSelect(rule.notify_end, 'data-field="notify_end"')}</label></div>`
        : `<div class="alerts-policy-grid alerts-schedule-grid"><label><span>Detect From</span>${timeSelect(rule.active_start, 'data-field="active_start"')}</label><label><span>Detect Until</span>${timeSelect(rule.active_end, 'data-field="active_end"')}</label><label><span>Notify From</span>${timeSelect(rule.notify_start, 'data-field="notify_start"')}</label><label><span>Notify Until</span>${timeSelect(rule.notify_end, 'data-field="notify_end"')}</label></div>`}
      <label class="alerts-recipient-field"><span>Email Recipients</span><input data-field="email_recipients" type="text" value="${escapeHtml(Array.isArray(rule.email_recipients) ? rule.email_recipients.join(', ') : rule.email_recipients || '')}" placeholder="alerts@example.com, me@example.com"></label>
      <div class="alerts-policy-actions"><span class="muted">${people ? 'Recognized-person and stranger alerts use the face recognition rule store.' : sound ? 'Assigned on the Sounds page. Removing here unassigns this sound class from the camera.' : tripwire ? 'Drawn on the Zones page. Removing here deletes the line from the area.' : loiter ? 'Enabled on the Zones page. Removing here turns loitering off for the area.' : 'Assigned on the Zones page. Removing here unassigns this item from the area.'}</span><button class="btn-danger" data-delete-rule type="button">${people ? 'Remove Policy' : 'Remove'}</button></div>
    </article>`;
  }).join('');
  $('alertsList').querySelectorAll('.alerts-policy').forEach((card) => {
    const rule = rules[Number(card.dataset.ruleIndex)];
    card.querySelectorAll('[data-field]').forEach((field) => field.addEventListener('change', () => {
      const key = field.dataset.field;
      if (field.type === 'checkbox') rule[key] = field.checked;
      else if (key === 'email_recipients') rule[key] = alertType === 'people' ? field.value : field.value.split(',').map((item) => item.trim()).filter(Boolean);
      else if (['min_confidence', 'max_confidence', 'confidence_threshold', 'cooldown_seconds', 'cooldown_minutes'].includes(key)) rule[key] = field.value === '' ? null : Number(field.value);
      else if (key === 'person_id') {
        rule.person_id = field.value || null;
        rule.name = enrolledPeople.find((person) => String(person.id) === field.value)?.name || 'Unknown Person';
      } else rule[key] = field.value || null;
      if (key === 'class') rule.name = soundClasses.find((sound) => sound.id === field.value)?.label || field.value;
      card.classList.toggle('is-enabled', rule.enabled !== false);
      updateStats();
    }));
    card.querySelector('[data-delete-rule]').addEventListener('click', () => {
      if (alertType === 'people') {
        const actualIndex = (faceRulesPayload.rules || []).indexOf(rule);
        if (actualIndex >= 0) faceRulesPayload.rules.splice(actualIndex, 1);
      } else if (alertType === 'tripwire') {
        // The tripwire is a single per-zone object drawn on Zones; removing its
        // policy here deletes the line.
        const zone = currentZone();
        if (zone) delete zone.tripwire;
      } else if (alertType === 'loiter') {
        const zone = currentZone();
        if (zone) delete zone.loiter;
      } else {
        const allRules = alertType === 'sound' ? currentCamera().detection.sound.rules : currentZone().object_rules;
        const actualIndex = allRules.indexOf(rule);
        if (actualIndex >= 0) allRules.splice(actualIndex, 1);
      }
      renderPolicies();
    });
  });
}

$('cameraSelect').addEventListener('change', () => { cameraIndex = Number($('cameraSelect').value); zoneIndex = 0; renderSelectors(); renderPolicies(); });
$('zoneSelect').addEventListener('change', () => { zoneIndex = Number($('zoneSelect').value); renderPolicies(); });
$('alertTypeSelect').addEventListener('change', () => { alertType = $('alertTypeSelect').value; renderSelectors(); renderPolicies(); });
// Objects and sound classes are assigned on the Zones and Sounds pages, so the
// only thing added from here is a recognized-person alert. The button is hidden
// for the other alert types (see renderSelectors).
$('addAlertBtn').addEventListener('click', () => {
  if (alertType !== 'people' || !currentCamera() || !currentZone()) return;
  const existing = currentPeopleRules();
  const candidates = [{ id: '', name: 'Unknown Person' }, ...(enrolledPeople || []).map((person) => ({ id: String(person.id), name: person.name }))];
  const available = candidates.find((person) => !existing.some((rule) => String(rule.person_id || '') === person.id));
  if (!available) {
    window.showToast?.('Every enrolled person already has an alert in this zone.', true);
    return;
  }
  faceRulesPayload.rules.push(defaultPeopleRule(available.id, available.name));
  renderPolicies();
});

$('saveAlertsBtn').addEventListener('click', async () => {
  const button = $('saveAlertsBtn'); button.disabled = true;
  try {
    cameras.forEach((camera) => {
      (camera.detection?.zones || []).forEach((zone) => {
        zone.object_rules = (zone.object_rules || []).map((rule, index) => ({ ...defaultRule(rule.label, 'object'), ...rule, id: rule.id || `${rule.label}-${index + 1}` }));
        zone.object_labels = zone.object_rules.filter((rule) => !['motion', 'face'].includes(rule.label)).map((rule) => rule.label);
      });
      if (camera.detection?.sound) camera.detection.sound.rules = (camera.detection.sound.rules || []).map((rule) => ({ ...defaultRule(rule.class, 'sound'), ...rule }));
    });
    if (alertType === 'people') {
      faceRulesPayload = await api('/api/settings/face-detection-rules', { method: 'PUT', body: JSON.stringify({ rules: faceRulesPayload.rules || [] }) });
    } else {
      const result = await api('/api/cameras', { method: 'PUT', body: JSON.stringify({ cameras }) });
      cameras = result.cameras || cameras;
      faceRulesPayload = await api('/api/settings/face-detection-rules', { method: 'PUT', body: JSON.stringify({ rules: faceRulesPayload.rules || [] }) });
    }
    renderSelectors(); renderPolicies(); window.showToast?.('Alert policies saved.');
  } catch (error) { if (!window.daygleAuth?.redirecting) window.showToast?.(error.message, true); }
  finally { button.disabled = false; }
});

(async function init() {
  try {
    const [cameraPayload, soundPayload, peoplePayload, rulesPayload] = await Promise.all([
      api('/api/cameras'), api('/api/sound/classes'), api('/api/persons'), api('/api/settings/face-detection-rules'),
    ]);
    cameras = cameraPayload.cameras || [];
    soundClasses = soundPayload.classes || [];
    enrolledPeople = peoplePayload.persons || [];
    faceRulesPayload = rulesPayload || { rules: [] };
    cameras.forEach((camera) => (camera.detection?.zones || []).forEach((zone) => { zone.object_rules ||= []; }));
    renderSelectors(); renderPolicies();
  } catch (error) { $('alertsList').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
}());
