// Clip segment timeline - shared by the Recordings (recordings.js) and Timeline
// (timeline.js) playback pages, which load this script before their page script.
//
// Classic script (no modules): the functions below read each page's own `els`
// element handles and `activeRecording` state, which the page script declares
// at top level. That works because all classic scripts on a page share one
// global scope and these functions only run after the page script has loaded.
//
// A compact bar under the player that shows the clip's pre-roll, the event span,
// and the post-motion tail  -  the recording settings "in action" on real footage.
// Boundaries come from the detection track (real detection times), not the
// configured pre/post values, so the bar reflects what this clip actually
// captured (including a truncated pre-roll).

function clipAuthoritativeDuration() {
  const videoDuration = Number(els.clipPlayer?.duration);
  if (Number.isFinite(videoDuration) && videoDuration > 0) return videoDuration;
  const metaDuration = Number(activeRecording?.duration_seconds);
  return Number.isFinite(metaDuration) && metaDuration > 0 ? metaDuration : 0;
}

// First and last playback times (seconds) where the track localized a detection.
function clipEventBounds(track) {
  if (!Array.isArray(track) || !track.length) return null;
  let first = null;
  let last = null;
  for (const sample of track) {
    if (!sample || !Array.isArray(sample.detections) || !sample.detections.length) continue;
    const t = Number(sample.t);
    if (!Number.isFinite(t) || t < 0) continue;
    if (first === null) first = t;
    last = t;
  }
  return first === null ? null : { first, last };
}

function fmtClipSeconds(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  return `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, '0')}`;
}

function resetClipTimeline() {
  if (!els.clipTimeline) return;
  els.clipTimeline.hidden = true;
  if (els.clipTimelineBar) els.clipTimelineBar.innerHTML = '';
  if (els.clipTimelineLegend) els.clipTimelineLegend.innerHTML = '';
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js/timeline.js
function renderClipTimeline() {
  if (!els.clipTimeline || !els.clipTimelineBar) return;
  const duration = clipAuthoritativeDuration();
  const track = Array.isArray(activeRecording?.track) ? activeRecording.track : null;
  const bounds = clipEventBounds(track);
  // Only annotate event clips whose track actually located the event. Continuous
  // recordings and clips with no localized detections get no segmentation.
  if (!duration || !bounds) {
    resetClipTimeline();
    return;
  }
  const first = Math.max(0, Math.min(bounds.first, duration));
  // A motion clip can contain only one localized sample when movement starts
  // just before the next capture/extension cycle. Keep that real trigger visible
  // instead of collapsing the green event segment to zero width.
  const minimumEventSpan = Math.min(1, duration);
  const last = Math.min(Math.max(bounds.last, first + minimumEventSpan), duration);
  const pct = (value) => `${Math.max(0, Math.min(100, (value / duration) * 100))}%`;

  const segments = [
    { cls: 'pre', label: 'Pre-roll', start: 0, end: first },
    { cls: 'event', label: 'Event', start: first, end: last },
    { cls: 'tail', label: 'Tail', start: last, end: duration },
  ];
  els.clipTimelineBar.innerHTML = '';
  for (const seg of segments) {
    const span = seg.end - seg.start;
    if (span <= 0.05) continue;
    const div = document.createElement('div');
    div.className = `clip-seg clip-seg-${seg.cls}`;
    div.style.left = pct(seg.start);
    div.style.width = pct(span);
    div.title = `${seg.label}: ${fmtClipSeconds(span)}`;
    els.clipTimelineBar.appendChild(div);
  }
  const marker = document.createElement('div');
  marker.className = 'clip-trigger-marker';
  marker.style.left = pct(first);
  marker.title = `Event trigger at ${fmtClipSeconds(first)}`;
  els.clipTimelineBar.appendChild(marker);

  const playhead = document.createElement('div');
  playhead.className = 'clip-playhead';
  playhead.id = 'clipPlayhead';
  els.clipTimelineBar.appendChild(playhead);

  // Legend shows the actual measured seconds of each region for this clip.
  const legendItems = [
    { cls: 'pre', label: 'Pre-roll', secs: first },
    { cls: 'event', label: 'Event', secs: last - first },
    { cls: 'tail', label: 'Tail', secs: duration - last },
  ];
  els.clipTimelineLegend.innerHTML = '';
  for (const item of legendItems) {
    const wrap = document.createElement('span');
    wrap.className = 'clip-legend-item';
    const swatch = document.createElement('i');
    swatch.className = `clip-legend-swatch clip-seg-${item.cls}`;
    wrap.appendChild(swatch);
    wrap.appendChild(document.createTextNode(`${item.label} ${fmtClipSeconds(Math.max(0, item.secs))}`));
    els.clipTimelineLegend.appendChild(wrap);
  }
  els.clipTimeline.hidden = false;
  els.clipTimelineBar.setAttribute('aria-valuemax', duration.toFixed(1));
  updateClipTimelinePlayhead();
}

function updateClipTimelinePlayhead() {
  if (!els.clipTimeline || els.clipTimeline.hidden) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const playhead = document.getElementById('clipPlayhead');
  if (!playhead) return;
  const current = Number(els.clipPlayer?.currentTime) || 0;
  playhead.style.left = `${Math.max(0, Math.min(100, (current / duration) * 100))}%`;
  els.clipTimelineBar?.setAttribute('aria-valuenow', current.toFixed(1));
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js/timeline.js
function seekClipFromClientX(clientX) {
  if (!els.clipTimelineBar || !els.clipPlayer) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const rect = els.clipTimelineBar.getBoundingClientRect();
  if (rect.width <= 0) return;
  const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  try {
    els.clipPlayer.currentTime = fraction * duration;
  } catch (_error) {
    /* not seekable yet */
  }
  updateClipTimelinePlayhead();
}

// eslint-disable-next-line no-unused-vars -- ESLint: exported for recordings.js/timeline.js
function nudgeClipTime(deltaSeconds) {
  if (!els.clipPlayer) return;
  const duration = clipAuthoritativeDuration();
  if (!duration) return;
  const next = Math.max(0, Math.min(duration, (Number(els.clipPlayer.currentTime) || 0) + deltaSeconds));
  try {
    els.clipPlayer.currentTime = next;
  } catch (_error) {
    /* not seekable yet */
  }
  updateClipTimelinePlayhead();
}
