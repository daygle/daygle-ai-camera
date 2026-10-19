// Recording detection log: what the live monitor saw on every check during a
// clip, what it kept, and what it dropped and why. Built from the recording's
// saved detection track (``recording.track``): each sample is one detection
// cycle, ``detections`` are the boxes that counted and ``dropped`` the ones a
// check threw away, each with its ``reason`` (app/live_monitor.py), and
// ``ptz`` what PTZ auto-tracking decided on that check (app/ptz_tracking.py). Loaded
// after utils.js / overlay.js and before the playback page script.

// A dropped box is drawn while playback is within this many seconds after its
// sample (or 1.5x the local sample spacing, if longer), so it shows on the
// frames that sample describes without lingering into the next check.
const DROPPED_HOLD_SECONDS = 0.75;
const DROPPED_BOX_COLOR = '#f87171';

function _logEscape(value) {
  return typeof escapeHtml === 'function' ? escapeHtml(value) : String(value ?? '');
}

function _logTitle(value) {
  const text = String(value || 'object');
  return typeof titleCase === 'function' ? titleCase(text) : text;
}

function formatLogTime(seconds) {
  const t = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(t / 60);
  const rest = (t - minutes * 60).toFixed(1).padStart(4, '0');
  return `${minutes}:${rest}`;
}

// Rows for the log: one per check that saw something (kept or dropped) or
// where auto-tracking acted, and one "nothing seen" row per run of empty
// checks between them.
function detectionLogRows(track) {
  const rows = [];
  let gap = null;
  for (const sample of Array.isArray(track) ? track : []) {
    const kept = Array.isArray(sample?.detections) ? sample.detections : [];
    const dropped = Array.isArray(sample?.dropped) ? sample.dropped : [];
    const ptz = sample?.ptz && typeof sample.ptz === 'object' ? sample.ptz : null;
    const t = Number(sample?.t) || 0;
    if (!kept.length && !dropped.length && !ptz) {
      if (gap) {
        gap.to = t;
        gap.count += 1;
      } else {
        gap = { kind: 'gap', from: t, to: t, count: 1 };
        rows.push(gap);
      }
      continue;
    }
    gap = null;
    rows.push({ kind: 'sample', t, kept, dropped, ptz });
  }
  return rows;
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js
function detectionLogSummary(track) {
  const samples = Array.isArray(track) ? track : [];
  let kept = 0;
  let dropped = 0;
  for (const sample of samples) {
    kept += Array.isArray(sample?.detections) ? sample.detections.length : 0;
    dropped += Array.isArray(sample?.dropped) ? sample.dropped.length : 0;
  }
  return { checks: samples.length, kept, dropped };
}

// How a kept detection was classified, in words: "moving (mask) · #125 · seen 2x".
function describeKeptDetection(detection) {
  const parts = [];
  if (detection?.motion_event === true || String(detection?.label || '').toLowerCase() === 'motion') {
    parts.push('motion');
  } else if (detection?.motion_state) {
    const basis = detection.motion_basis ? String(detection.motion_basis).replace(/_/g, ' ') : '';
    const displacement = Number.isFinite(Number(detection.track_displacement)) && detection.track_displacement !== undefined
      ? ` ${Number(detection.track_displacement).toFixed(3)}`
      : '';
    parts.push(basis ? `${detection.motion_state} (${basis}${basis === 'displacement' ? displacement : ''})` : detection.motion_state);
  } else {
    parts.push('camera moving');
  }
  if (Number.isInteger(detection?.track_id) && detection.track_id > 0) parts.push(`#${detection.track_id}`);
  if (Number.isInteger(detection?.track_age) && detection.track_age > 0) parts.push(`seen ${detection.track_age}x`);
  if (detection?.confirmed_by === 'confidence') parts.push('confirmed by confidence');
  if (detection?.confirmed_by === 'group') parts.push('confirmed by label group');
  return parts.join(' · ');
}

// Why a detection was dropped, led by its tracker id so a parked car dropped
// on every check reads as that same car: "#176 · seen 886x · Judged still…".
function describeDroppedDetection(detection) {
  const parts = [];
  if (Number.isInteger(detection?.track_id) && detection.track_id > 0) parts.push(`#${detection.track_id}`);
  if (Number.isInteger(detection?.track_age) && detection.track_age > 1) parts.push(`seen ${detection.track_age}x`);
  parts.push(String(detection?.reason || 'Dropped'));
  return parts.join(' · ');
}

function _signed(value) {
  const number = Number(value) || 0;
  return `${number >= 0 ? '+' : '−'}${Math.abs(number).toFixed(2)}`;
}

// "right + up for 0.8s" from a pulse's pan/tilt/zoom velocities.
function _ptzMoveText(move) {
  const parts = [];
  const pan = Number(move?.pan) || 0;
  const tilt = Number(move?.tilt) || 0;
  const zoom = Number(move?.zoom) || 0;
  if (pan) parts.push(pan > 0 ? 'right' : 'left');
  if (tilt) parts.push(tilt > 0 ? 'up' : 'down');
  if (zoom) parts.push(zoom > 0 ? 'zoom in' : 'zoom out');
  const seconds = Number(move?.seconds);
  return `${parts.join(' + ') || 'move'}${Number.isFinite(seconds) ? ` for ${seconds.toFixed(1)}s` : ''}`;
}

const PTZ_EVENT_TEXT = {
  centred: 'centred (inside the dead zone)',
  wait_picture: 'waiting for the picture to settle after the last move',
  wait_move: 'waiting for the last move to finish',
  holding: 'target not seen this check; holding still',
};

// What auto-tracking decided on one check, in words:
// "Following Person #12 · off centre x +0.22 y −0.03 · moved right for 0.8s".
function describePtzDecision(ptz) {
  if (!ptz || typeof ptz !== 'object') return '';
  if (ptz.state === 'paused') return 'Paused for manual control';
  if (ptz.event === 'home' || ptz.state === 'returning') return 'Nothing to follow; returning home';
  const parts = [];
  if (ptz.event === 'lost') {
    const id = Number.isInteger(ptz.lost_track_id) ? ` #${ptz.lost_track_id}` : '';
    parts.push(`Lost the ${_logTitle(ptz.lost_label)}${id}`);
  }
  if (ptz.state === 'tracking') {
    const id = Number.isInteger(ptz.track_id) ? ` #${ptz.track_id}` : '';
    let following = `${ptz.acquired ? 'Started following' : 'Following'} ${_logTitle(ptz.label)}${id}`;
    if (ptz.switched_from !== undefined && ptz.switched_from !== null) following += ` (was #${ptz.switched_from})`;
    parts.push(following);
  }
  if (Array.isArray(ptz.error) && ptz.error.length === 2) {
    parts.push(`off centre x ${_signed(ptz.error[0])} y ${_signed(ptz.error[1])}`);
  }
  if (ptz.event === 'move') {
    parts.push(`moved ${_ptzMoveText(ptz.move)}${Number(ptz.boost) > 1 ? ` (catch-up ${Number(ptz.boost).toFixed(1)}x)` : ''}`);
  } else if (ptz.event === 'edge_push') {
    parts.push(`last seen at the edge; turned ${_ptzMoveText(ptz.move)} after it`);
  } else if (ptz.event === 'zoom_out') {
    parts.push('zooming back out');
  } else if (PTZ_EVENT_TEXT[ptz.event]) {
    parts.push(PTZ_EVENT_TEXT[ptz.event]);
  }
  return parts.join(' · ');
}

function _ptzEntryHtml(ptz) {
  const detail = describePtzDecision(ptz);
  if (!detail) return '';
  return '<li class="detection-log-entry is-ptz">'
    + '<span class="detection-log-status">PTZ</span>'
    + `<span class="detection-log-detail">${_logEscape(detail)}</span></li>`;
}

function _logEntryHtml(detection, droppedEntry) {
  const confidence = Math.round(Number(detection?.confidence || 0) * 100);
  const name = `${_logTitle(detection?.label)} ${confidence}%`;
  const detail = droppedEntry ? describeDroppedDetection(detection) : describeKeptDetection(detection);
  const status = droppedEntry ? 'Dropped' : 'Kept';
  return `<li class="detection-log-entry ${droppedEntry ? 'is-dropped' : 'is-kept'}">`
    + `<span class="detection-log-status">${status}</span>`
    + `<strong>${_logEscape(name)}</strong>`
    + `<span class="detection-log-detail">${_logEscape(detail)}</span></li>`;
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js
function detectionLogHtml(track) {
  const rows = detectionLogRows(track);
  if (!rows.length) return '<p class="muted">No detection checks were saved for this recording.</p>';
  return rows.map((row) => {
    if (row.kind === 'gap') {
      const span = row.count > 1
        ? `${formatLogTime(row.from)} – ${formatLogTime(row.to)} (${row.count} checks)`
        : `${formatLogTime(row.from)}`;
      return `<div class="detection-log-row is-gap"><button type="button" class="detection-log-time" data-seek="${row.from}">${_logEscape(formatLogTime(row.from))}</button>`
        + `<span class="muted">Nothing seen · ${_logEscape(span)}</span></div>`;
    }
    const entries = row.kept.map((d) => _logEntryHtml(d, false)).concat(row.dropped.map((d) => _logEntryHtml(d, true)));
    if (row.ptz) entries.push(_ptzEntryHtml(row.ptz));
    return `<div class="detection-log-row"><button type="button" class="detection-log-time" data-seek="${row.t}">${_logEscape(formatLogTime(row.t))}</button>`
      + `<ul class="detection-log-entries">${entries.join('')}</ul></div>`;
  }).join('');
}

// The dropped boxes to draw at playback time ``t``: those of the last check at
// or before ``t``, while ``t`` is still within that check's hold window.
// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js
function droppedDetectionsAtTime(track, t) {
  if (!Array.isArray(track) || !track.length) return [];
  const time = Number(t) || 0;
  let index = -1;
  for (let i = 0; i < track.length; i += 1) {
    if ((Number(track[i]?.t) || 0) <= time) index = i;
    else break;
  }
  if (index < 0) return [];
  const sample = track[index];
  const sampleT = Number(sample?.t) || 0;
  const next = track[index + 1];
  const spacing = next ? (Number(next.t) || 0) - sampleT : 0;
  const hold = Math.max(DROPPED_HOLD_SECONDS, spacing * 1.5);
  if (time - sampleT > hold) return [];
  return Array.isArray(sample?.dropped) ? sample.dropped : [];
}

// "Car 77% · #176 · Dropped": the tracker id ties the box to its Detection
// Log rows (and to the same object's kept box, when it has one).
function droppedBoxLabel(detection) {
  const parts = [`${_logTitle(detection?.label)} ${Math.round(Number(detection?.confidence || 0) * 100)}%`];
  if (Number.isInteger(detection?.track_id) && detection.track_id > 0) parts.push(`#${detection.track_id}`);
  parts.push('Dropped');
  return parts.join(' · ');
}

// Dashed red boxes for dropped detections, labelled with their score, drawn on
// the same letterboxed geometry as the regular overlay.
// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js
function drawDroppedBoxesOnCanvas(canvas, dropped, referenceEl) {
  if (!canvas || !referenceEl || !dropped?.length) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const cssWidth = referenceEl.clientWidth || 0;
  const cssHeight = referenceEl.clientHeight || 0;
  const dpr = canvas.width && cssWidth ? canvas.width / cssWidth : 1;
  ctx.save();
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const srcW = Math.max(1, Number(referenceEl.videoWidth || cssWidth));
  const srcH = Math.max(1, Number(referenceEl.videoHeight || cssHeight));
  const scale = Math.min(cssWidth / srcW, cssHeight / srcH);
  const renderWidth = srcW * scale;
  const renderHeight = srcH * scale;
  const offsetX = (cssWidth - renderWidth) / 2;
  const offsetY = (cssHeight - renderHeight) / 2;
  ctx.setLineDash([6, 4]);
  ctx.lineWidth = 2;
  ctx.font = '600 12px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  for (const detection of dropped) {
    const box = detection?.box || {};
    const x = Math.min(Math.max(Number(box.x) || 0, 0), 1);
    const y = Math.min(Math.max(Number(box.y) || 0, 0), 1);
    const w = Math.min(Math.max(Number(box.width) || 0, 0), 1);
    const h = Math.min(Math.max(Number(box.height) || 0, 0), 1);
    if (w <= 0 || h <= 0) continue;
    const dx = offsetX + x * renderWidth;
    const dy = offsetY + y * renderHeight;
    const dw = w * renderWidth;
    const dh = h * renderHeight;
    ctx.strokeStyle = DROPPED_BOX_COLOR;
    ctx.strokeRect(dx, dy, dw, dh);
    const label = droppedBoxLabel(detection);
    const labelWidth = ctx.measureText(label).width + 12;
    const labelY = dy + dh + 4;
    ctx.fillStyle = 'rgba(7, 11, 19, 0.7)';
    ctx.fillRect(dx, labelY, labelWidth, 20);
    ctx.fillStyle = DROPPED_BOX_COLOR;
    ctx.fillText(label, dx + 6, labelY + 10);
  }
  ctx.restore();
}
