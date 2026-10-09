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

// Seconds into the clip at which its triggering event fired, or null when it
// cannot be placed on the bar. ``started_at`` is the wall-clock start of the
// rendered footage and the event's ``created_at`` its trigger frame's time.
function clipTriggerOffset(duration) {
  const startMs = Date.parse(activeRecording?.started_at || '');
  const triggerMs = Date.parse(activeRecording?.event?.created_at || '');
  if (!Number.isFinite(startMs) || !Number.isFinite(triggerMs)) return null;
  const offset = (triggerMs - startMs) / 1000;
  return offset >= 0 && offset <= duration ? offset : null;
}

// What kept the clip going past its first post-event window, as clip-relative
// seconds: ``recording.extensions`` (see app.recording_extension) stores runs of
// extensions with one reason each - the object, motion after it, or the object
// standing still - and where the clip would have ended without them.
const CLIP_EXTENSION_REASONS = {
  object: 'kept recording',
  motion: 'Motion kept recording',
  still: 'kept recording while still',
};

function clipExtensions(duration) {
  const ext = activeRecording?.extensions;
  const startMs = Date.parse(activeRecording?.started_at || '');
  if (!ext || !Number.isFinite(startMs)) return null;
  const at = (iso) => {
    const ms = Date.parse(iso || '');
    return Number.isFinite(ms) ? (ms - startMs) / 1000 : null;
  };
  const runs = (Array.isArray(ext.runs) ? ext.runs : [])
    .map((run) => ({ ...run, startAt: at(run.start), endAt: at(run.end) }))
    .filter((run) => run.startAt !== null && run.startAt >= 0 && run.startAt <= duration)
    .map((run) => ({ ...run, endAt: Math.min(duration, Math.max(run.startAt, run.endAt ?? run.startAt)) }));
  const originalEnd = at(ext.original_end);
  return {
    runs,
    // Only meaningful when the clip actually ran past it.
    originalEnd: originalEnd !== null && originalEnd > 0 && originalEnd < duration - 0.25 ? originalEnd : null,
  };
}

function clipExtensionText(run) {
  const label = run.label && run.label !== 'motion' ? titleCase(run.label) : '';
  const what = run.reason === 'motion' ? CLIP_EXTENSION_REASONS.motion
    : `${label || 'Object'} ${CLIP_EXTENSION_REASONS[run.reason] || 'kept recording'}`;
  const span = run.endAt - run.startAt >= 0.05
    ? `${fmtClipSeconds(run.startAt)}–${fmtClipSeconds(run.endAt)}`
    : `at ${fmtClipSeconds(run.startAt)}`;
  return `${what} ${span}`;
}

function appendClipLegendItem(swatchClass, text, title) {
  const wrap = document.createElement('span');
  wrap.className = 'clip-legend-item';
  wrap.title = title;
  const swatch = document.createElement('i');
  swatch.className = `clip-legend-swatch ${swatchClass}`;
  wrap.appendChild(swatch);
  wrap.appendChild(document.createTextNode(text));
  els.clipTimelineLegend.appendChild(wrap);
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
  const last = Math.max(first, Math.min(bounds.last, duration));
  // One localized sample (movement caught on a single detection cycle) has no
  // measurable span. Report it as a single detection rather than inventing a
  // duration; the bar still shows it, as a hairline at that moment.
  const singleDetection = last - first < 0.05;
  const pct = (value) => `${Math.max(0, Math.min(100, (value / duration) * 100))}%`;
  const triggerAt = clipTriggerOffset(duration);

  // The bands are MEASURED from the detection track (first/last detection),
  // while the Pre-Event / Post-event settings govern the clip relative to the
  // TRIGGER. The two differ whenever the first detection is not the trigger
  // (motion first, object later) or the pre-roll was trimmed, so each band and
  // legend entry says what it measures, and the trigger gets its own marker.
  const segments = [
    {
      cls: 'pre', label: 'Pre-roll', start: 0, end: first,
      basis: 'clip start to first detection. The Pre-Event setting covers clip start to the trigger.',
    },
    {
      cls: 'event', label: 'Event', start: first, end: last,
      basis: 'first to last detection.',
    },
    {
      cls: 'tail', label: 'Tail', start: last, end: duration,
      basis: 'last detection to clip end. The Post-event setting runs from the trigger, extended by later detections.',
    },
  ];
  els.clipTimelineBar.innerHTML = '';
  for (const seg of segments) {
    const span = seg.end - seg.start;
    if (seg.cls === 'event' && singleDetection) {
      const hairline = document.createElement('div');
      hairline.className = 'clip-seg clip-seg-event clip-seg-event-single';
      hairline.style.left = pct(first);
      hairline.title = `Single Detection at ${fmtClipSeconds(first)}`;
      els.clipTimelineBar.appendChild(hairline);
      continue;
    }
    if (span <= 0.05) continue;
    const div = document.createElement('div');
    div.className = `clip-seg clip-seg-${seg.cls}`;
    div.style.left = pct(seg.start);
    div.style.width = pct(span);
    div.title = `${seg.label}: ${fmtClipSeconds(span)}, measured from ${seg.basis}`;
    els.clipTimelineBar.appendChild(div);
  }
  const marker = document.createElement('div');
  marker.className = 'clip-trigger-marker';
  marker.style.left = pct(first);
  marker.title = `First detection at ${fmtClipSeconds(first)}`;
  els.clipTimelineBar.appendChild(marker);

  if (triggerAt !== null) {
    const trigger = document.createElement('div');
    trigger.className = 'clip-event-trigger';
    trigger.style.left = pct(triggerAt);
    trigger.title = `Trigger at ${fmtClipSeconds(triggerAt)}. Pre-Event covers clip start to here; Post-event runs from here.`;
    els.clipTimelineBar.appendChild(trigger);
  }

  // Extension runs: a strip along the bottom of the bar for each run, notched
  // where it starts, and a dotted line where the clip would have ended.
  const extensions = clipExtensions(duration);
  if (extensions) {
    for (const run of extensions.runs) {
      const strip = document.createElement('div');
      strip.className = `clip-extension clip-extension-${run.reason === 'motion' || run.reason === 'still' ? run.reason : 'object'}`;
      strip.style.left = pct(run.startAt);
      strip.style.width = pct(Math.max(0, run.endAt - run.startAt));
      strip.title = clipExtensionText(run);
      els.clipTimelineBar.appendChild(strip);
    }
    if (extensions.originalEnd !== null) {
      const original = document.createElement('div');
      original.className = 'clip-original-end';
      original.style.left = pct(extensions.originalEnd);
      original.title = `Without extensions the clip would have ended at ${fmtClipSeconds(extensions.originalEnd)}.`;
      els.clipTimelineBar.appendChild(original);
    }
  }

  const playhead = document.createElement('div');
  playhead.className = 'clip-playhead';
  playhead.id = 'clipPlayhead';
  els.clipTimelineBar.appendChild(playhead);

  // Legend shows the actual measured seconds of each region for this clip.
  els.clipTimelineLegend.innerHTML = '';
  for (const seg of segments) {
    const text = seg.cls === 'event' && singleDetection
      ? 'Event: single detection'
      : `${seg.label} ${fmtClipSeconds(Math.max(0, seg.end - seg.start))}`;
    appendClipLegendItem(`clip-seg-${seg.cls}`, text, `${seg.label}: measured from ${seg.basis}`);
  }
  if (triggerAt !== null) {
    appendClipLegendItem(
      'clip-legend-swatch-trigger',
      `Trigger ${fmtClipSeconds(triggerAt)}`,
      'When the triggering event fired. Pre-Event covers clip start to here; Post-event runs from here.',
    );
  }
  if (extensions && extensions.runs.length) {
    appendClipLegendItem(
      'clip-legend-swatch-extension',
      `Extended ×${extensions.runs.length}`,
      extensions.runs.map(clipExtensionText).join('\n'),
    );
  }
  if (extensions && extensions.originalEnd !== null) {
    appendClipLegendItem(
      'clip-legend-swatch-original-end',
      `Original end ${fmtClipSeconds(extensions.originalEnd)}`,
      'Where the clip would have ended (trigger + Post-event) without the extensions.',
    );
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
