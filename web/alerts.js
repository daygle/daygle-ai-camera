requireElements(['cameraSelect', 'zoneSelect', 'alertTypeSelect', 'addAlertBtn', 'saveAlertsBtn', 'alertsList']);

let cameras = [];
let soundClasses = [];
let enrolledPeople = [];
let faceRulesPayload = { rules: [] };
let cameraIndex = 0;
let zoneIndex = 0;
let alertType = 'object';

// Keep the selector useful before any alert policy exists in a zone. These are
// the standard detector classes; existing custom/model labels are added below.
const STANDARD_OBJECT_LABELS = [
  'person', 'bicycle', 'car', 'motorcycle', 'bus', 'train', 'truck', 'boat',
  'traffic light', 'stop sign', 'parking meter', 'bench', 'bird', 'cat', 'dog',
  'horse', 'sheep', 'cow', 'elephant', 'bear', 'zebra', 'giraffe', 'backpack',
  'umbrella', 'handbag', 'suitcase', 'bottle', 'cup', 'laptop', 'cell phone',
];

const $ = (id) => document.getElementById(id);
const currentCamera = () => cameras[cameraIndex];
const currentZone = () => currentCamera()?.detection?.zones?.[zoneIndex];
const objectLabels = () => [...new Set([
  ...STANDARD_OBJECT_LABELS,
  'motion',
  'face',
  ...cameras.flatMap((camera) => (camera.detection?.zones || []).flatMap((zone) => (zone.object_rules || []).map((rule) => rule.label))),
].filter(Boolean))].sort();
const currentPeopleRules = () => (faceRulesPayload.rules || []).filter((rule) => {
  const cameraMatches = !rule.camera_id || String(rule.camera_id) === String(currentCamera()?.id || '');
  const zoneMatches = !rule.zone_id || String(rule.zone_id) === String(currentZone()?.id || '');
  return cameraMatches && zoneMatches;
});
const currentRules = () => {
  if (alertType === 'sound') return currentCamera()?.detection?.sound?.rules || [];
  if (alertType === 'people') return currentPeopleRules();
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
  return [...objectPolicies, ...soundPolicies, ...(faceRulesPayload.rules || [])].filter((rule) => rule.email_enabled || rule.push_enabled);
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
}

function ruleLabel(rule) {
  if (alertType === 'sound') return titleCase(rule.name || soundClasses.find((item) => item.id === rule.class)?.label || rule.class);
  if (alertType === 'people') return rule.name || 'Unknown Person';
  return String(rule.label || '').replace(/\b\w/g, (char) => char.toUpperCase());
}

function ruleOptions(rule) {
  if (alertType === 'sound') return soundClasses.map((sound) => `<option value="${escapeHtml(sound.id)}" ${sound.id === rule.class ? 'selected' : ''}>${escapeHtml(sound.label)}</option>`).join('');
  if (alertType === 'people') {
    const people = [{ id: '', name: 'Unknown Person' }, ...(enrolledPeople || []).map((person) => ({ id: String(person.id), name: person.name }))];
    return people.map((person) => `<option value="${escapeHtml(person.id)}" ${String(rule.person_id || '') === person.id ? 'selected' : ''}>${escapeHtml(person.name)}</option>`).join('');
  }
  return objectLabels().map((label) => {
    const displayLabel = titleCase(String(label).replace(/[_-]+/g, ' '));
    return `<option value="${escapeHtml(label)}" ${label === rule.label ? 'selected' : ''}>${escapeHtml(displayLabel)}</option>`;
  }).join('');
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
    $('alertsList').innerHTML = `<div class="empty">No ${escapeHtml(alertType === 'people' ? 'recognized-person' : alertType)} alert policies in this scope. Add one below to get started.</div>`;
    return;
  }
  $('alertsList').innerHTML = rules.map((rule, index) => {
    const people = alertType === 'people';
    const sound = alertType === 'sound';
    const confidence = people ? rule.min_confidence : sound ? rule.confidence_threshold : rule.min_confidence;
    const cooldown = people ? rule.cooldown_minutes : rule.cooldown_seconds;
    const confidenceLabel = people ? 'Minimum Recognition Confidence' : sound ? 'Confidence Threshold' : 'Minimum Confidence';
    const cooldownLabel = people ? 'Cooldown (Minutes)' : 'Cooldown (Seconds)';
    return `
    <article class="alerts-policy ${rule.enabled !== false ? 'is-enabled' : ''}" data-rule-index="${index}">
      <div class="alerts-policy-head"><div><span class="zones-panel-kicker">${escapeHtml(scopeLabel())} · Policy ${index + 1}</span><h3>${escapeHtml(ruleLabel(rule))}</h3></div><label class="toggle-control"><input data-field="enabled" type="checkbox" ${rule.enabled !== false ? 'checked' : ''}><span>${rule.enabled !== false ? 'Enabled' : 'Disabled'}</span></label></div>
      <div class="alerts-policy-grid">
        <label><span>${people ? 'Person' : sound ? 'Sound' : 'Object'}</span><select data-field="${people ? 'person_id' : sound ? 'class' : 'label'}">${ruleOptions(rule)}</select></label>
        <label><span>${confidenceLabel}</span><input data-field="${people ? 'min_confidence' : sound ? 'confidence_threshold' : 'min_confidence'}" type="number" min="0" max="1" step="0.01" value="${escapeHtml(String(confidence ?? (people ? '' : 0.5)))}"></label>
        ${!people && !sound ? '<label><span>Maximum Confidence</span><input data-field="max_confidence" type="number" min="0" max="1" step="0.01" value="' + escapeHtml(String(rule.max_confidence ?? 1)) + '"></label>' : ''}
        <label><span>${cooldownLabel}</span><input data-field="${people ? 'cooldown_minutes' : 'cooldown_seconds'}" type="number" min="0" max="${people ? '1440' : '3600'}" step="${people ? '1' : '5'}" value="${escapeHtml(String(cooldown ?? (people ? 5 : 60)))}"></label>
      </div>
      <div class="alerts-channel-row"><label><input data-field="email_enabled" type="checkbox" ${rule.email_enabled ? 'checked' : ''}> Email</label><label><input data-field="push_enabled" type="checkbox" ${rule.push_enabled ? 'checked' : ''}> Push</label>${!people ? '<label><input data-field="record_on_detect" type="checkbox" ' + (rule.record_on_detect !== false ? 'checked' : '') + '> Record</label>' : ''}</div>
      ${!people ? `<div class="alerts-policy-grid alerts-schedule-grid"><label><span>Detect From</span>${timeSelect(rule.active_start, 'data-field="active_start"')}</label><label><span>Detect Until</span>${timeSelect(rule.active_end, 'data-field="active_end"')}</label><label><span>Notify From</span>${timeSelect(rule.notify_start, 'data-field="notify_start"')}</label><label><span>Notify Until</span>${timeSelect(rule.notify_end, 'data-field="notify_end"')}</label></div>` : ''}
      <label class="alerts-recipient-field"><span>Email Recipients</span><input data-field="email_recipients" type="text" value="${escapeHtml(Array.isArray(rule.email_recipients) ? rule.email_recipients.join(', ') : rule.email_recipients || '')}" placeholder="alerts@example.com, me@example.com"></label>
      <div class="alerts-policy-actions"><span class="muted">${people ? 'Recognized-Person And Stranger Alerts Use The Face Recognition Rule Store.' : sound ? 'Sound Alert Policies Are Managed On This Page.' : ''}</span><button class="btn-danger" data-delete-rule type="button">Remove Policy</button></div>
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
$('addAlertBtn').addEventListener('click', () => {
  if (!currentCamera()) return;
  if (scopeRequiresZone() && !currentZone()) return;
  if (alertType === 'sound') {
    const camera = currentCamera();
    if (!soundClasses.length) {
      window.showToast?.('No sound classes are available. Check the sound model on the Sounds page.', true);
      return;
    }
    const available = soundClasses.find((sound) => !(camera.detection?.sound?.rules || []).some((rule) => rule.class === sound.id));
    if (!available) {
      window.showToast?.('Every sound class already has a policy on this camera.', true);
      return;
    }
    camera.detection ||= {};
    camera.detection.sound ||= { enabled: false, rules: [] };
    camera.detection.sound.rules.push(defaultRule(available.id, 'sound'));
  } else if (alertType === 'people') {
    const existing = currentPeopleRules();
    const candidates = [{ id: '', name: 'Unknown Person' }, ...(enrolledPeople || []).map((person) => ({ id: String(person.id), name: person.name }))];
    const available = candidates.find((person) => !existing.some((rule) => String(rule.person_id || '') === person.id));
    if (!available) return;
    faceRulesPayload.rules.push(defaultPeopleRule(available.id, available.name));
  } else {
    currentZone().object_rules ||= [];
    currentZone().object_rules.push(defaultRule(objectLabels()[0] || 'person', 'object'));
  }
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
