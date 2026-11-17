// ai_insights.js - the AI Insights card on Intelligence > AI (ai.html).
// Admin-only. Reads GET /api/settings/ai-insights (app/ai_insights.py):
// minimum-confidence suggestions from the AI's per-detection verdicts, with
// an Apply button, and AI tags named often that no rule watches yet. Uses
// api() / escapeHtml / titleCase / showToast from utils.js.

const aiTuningList = document.getElementById('aiTuning');
const aiFrequentTagsList = document.getElementById('aiFrequentTags');
const aiIgnoreAreasList = document.getElementById('aiIgnoreAreas');

function aiCameraFrameUrl(cameraId) {
  return cameraId ? `/api/live/snapshot?camera_id=${encodeURIComponent(cameraId)}&width=320` : '';
}

// A preview whose event snapshot is gone (purged, or retention) falls back
// to the camera's current frame, then to the bare outline.
function aiPreviewFallback(img) {
  const fallback = img.dataset.fallback;
  if (fallback && img.getAttribute('src') !== fallback) {
    img.src = fallback;
  } else {
    img.remove();
  }
}

// The area drawn over the latest snapshot the AI rejected there, or over
// the camera's current frame when there is none.
function aiAreaPreview(item) {
  const area = item.area || {};
  const box = `left:${area.x * 100}%;top:${area.y * 100}%;width:${area.width * 100}%;height:${area.height * 100}%`;
  const live = aiCameraFrameUrl(item.camera_id);
  const src = item.sample_event_id != null ? `/api/events/${encodeURIComponent(item.sample_event_id)}/snapshot?thumb=1` : live;
  const alt = item.sample_event_id != null ? 'Latest snapshot the AI rejected there' : 'Current camera view';
  return `<div class="ai-area-preview">${src ? `<img src="${escapeHtml(src)}" data-fallback="${escapeHtml(live)}" alt="${alt}" loading="lazy" />` : ''}<span class="ai-area-box" style="${box}"></span></div>`;
}

function aiIgnoreAreaRow(item) {
  const label = titleCase(String(item.label || '').replace(/_/g, ' '));
  const body = JSON.stringify({ camera_id: item.camera_id, label: item.label, area: item.area });
  return `
    <div class="ai-insight-row">
      ${aiAreaPreview(item)}
      <div class="ai-insight-text">
        <strong>${escapeHtml(item.camera_name || item.camera_id)} (${(item.zones || []).map(escapeHtml).join(', ')}): ${escapeHtml(label)}</strong>
        <p class="muted">The AI rejected ${Number(item.rejected)} ${escapeHtml(label)} detections in the outlined area${item.confirmed ? ` and confirmed ${Number(item.confirmed)}` : ''}. Ignore ${escapeHtml(label)} there?</p>
      </div>
      <button type="button" class="secondary" data-ai-ignore="${escapeHtml(body)}">Ignore Area</button>
    </div>`;
}

function aiIgnoredRow(item) {
  const label = titleCase(String(item.label || '').replace(/_/g, ' '));
  const body = JSON.stringify({ camera_id: item.camera_id, zone_id: item.zone_id, index: item.index });
  return `
    <div class="ai-insight-row">
      ${aiAreaPreview({ area: item, camera_id: item.camera_id })}
      <div class="ai-insight-text">
        <strong>${escapeHtml(item.camera_name)} (${escapeHtml(item.zone_name)}): ${escapeHtml(label)} ignored</strong>
        <p class="muted">${item.in_zone === false
          ? `This spot is now outside the ${escapeHtml(item.zone_name)} area, so it has no effect.`
          : `${escapeHtml(label)} is not detected inside the outlined spot.`} Also shown on the <a href="/zones">Zones</a> page.</p>
      </div>
      <button type="button" class="secondary" data-ai-unignore="${escapeHtml(body)}">Stop Ignoring</button>
    </div>`;
}

function aiPercent(value) {
  return `${Math.round(Number(value) * 100)}%`;
}

function aiTuningRow(item) {
  const label = titleCase(String(item.label || '').replace(/_/g, ' '));
  const zones = (item.zones || []).length ? ` (${item.zones.map(escapeHtml).join(', ')})` : '';
  const lost = Number(item.confirmed_lost || 0);
  const detail = `The AI rejected ${item.rejected_below} of ${item.judged_below} ${escapeHtml(label)} detections below ${aiPercent(item.min_confidence)}`
    + `${lost ? `; ${lost} of ${item.confirmed_total} confirmed ones would be dropped too` : ''}.`;
  return `
    <div class="ai-insight-row">
      <div>
        <strong>${escapeHtml(item.camera_name || item.camera_id)}${zones}: ${escapeHtml(label)}</strong>
        <p class="muted">Raise the minimum confidence from ${aiPercent(item.current_min_confidence)} to ${aiPercent(item.min_confidence)}. ${detail}</p>
      </div>
      <button type="button" class="secondary" data-ai-apply="${escapeHtml(JSON.stringify({ camera_id: item.camera_id, label: item.label, min_confidence: item.min_confidence }))}">Apply</button>
    </div>`;
}

function aiTagsRow(item) {
  const chips = (item.tags || []).map((tag) => `<span class="detection detection-ai-tag">🤖 ${escapeHtml(titleCase(tag.tag))} · ${Number(tag.count)}</span>`).join('');
  return `
    <div class="ai-insight-row">
      <div>
        <strong>${escapeHtml(item.camera_name || item.camera_id)}</strong>
        <div class="ai-insight-tags">${chips}</div>
      </div>
      <a class="secondary" href="/zones">Set Up AI Tag Alert</a>
    </div>`;
}

function renderAiInsights(data) {
  if (aiTuningList) {
    const tuning = data?.tuning || [];
    aiTuningList.innerHTML = tuning.length
      ? tuning.map(aiTuningRow).join('')
      : `<span class="muted">No changes suggested. Suggestions appear once the AI has judged enough detections (last ${Number(data?.tuning_window_days || 14)} days).</span>`;
  }
  if (aiIgnoreAreasList) {
    const suggested = data?.ignore_areas || [];
    const active = data?.ignored_areas || [];
    aiIgnoreAreasList.innerHTML = (suggested.map(aiIgnoreAreaRow).join('') + active.map(aiIgnoredRow).join(''))
      || '<span class="muted">No areas suggested and none ignored.</span>';
  }
  if (aiFrequentTagsList) {
    const tags = data?.frequent_tags || [];
    aiFrequentTagsList.innerHTML = tags.length
      ? tags.map(aiTagsRow).join('')
      : `<span class="muted">No frequent AI tags without an alert in the last ${Number(data?.tags_window_days || 7)} days.</span>`;
  }
}

function loadAiInsights() {
  if (!aiTuningList && !aiFrequentTagsList && !aiIgnoreAreasList) return Promise.resolve();
  return api('/api/settings/ai-insights').then(renderAiInsights).catch(() => {
    if (aiTuningList) aiTuningList.innerHTML = '<span class="muted">Could not load AI insights.</span>';
    if (aiFrequentTagsList) aiFrequentTagsList.innerHTML = '';
    if (aiIgnoreAreasList) aiIgnoreAreasList.innerHTML = '';
  });
}

aiTuningList?.addEventListener('click', async (clickEvent) => {
  const button = clickEvent.target.closest('[data-ai-apply]');
  if (!button) return;
  let body;
  try { body = JSON.parse(button.dataset.aiApply); } catch (_err) { return; }
  const label = titleCase(String(body.label || '').replace(/_/g, ' '));
  if (!confirm(`Raise the minimum confidence for ${label} on this camera to ${aiPercent(body.min_confidence)}? Detections below it will no longer alert or record.`)) return;
  button.disabled = true;
  try {
    const result = await api('/api/settings/ai-insights/apply', { method: 'POST', body: JSON.stringify(body) });
    const changed = Number(result?.rules_changed || 0);
    window.showToast?.(changed ? `Updated ${changed} rule${changed === 1 ? '' : 's'}.` : 'Already at or above that confidence.');
    await loadAiInsights();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(`Could not apply: ${error.message}`, true);
    button.disabled = false;
  }
});

// Image errors do not bubble, so listen in the capture phase.
aiIgnoreAreasList?.addEventListener('error', (errorEvent) => {
  if (errorEvent.target instanceof HTMLImageElement) aiPreviewFallback(errorEvent.target);
}, true);

aiIgnoreAreasList?.addEventListener('click', async (clickEvent) => {
  const ignore = clickEvent.target.closest('[data-ai-ignore]');
  const unignore = clickEvent.target.closest('[data-ai-unignore]');
  const button = ignore || unignore;
  if (!button) return;
  let body;
  try { body = JSON.parse(ignore ? button.dataset.aiIgnore : button.dataset.aiUnignore); } catch (_err) { return; }
  const label = titleCase(String(body.label || '').replace(/_/g, ' '));
  if (ignore && !confirm(`Ignore ${label} detections inside the outlined area? A real ${label} there will not be detected.`)) return;
  button.disabled = true;
  try {
    if (ignore) {
      const result = await api('/api/settings/ai-insights/ignore-area', { method: 'POST', body: JSON.stringify(body) });
      const zones = Number(result?.zones_changed || 0);
      window.showToast?.(zones ? `Ignoring ${label} there in ${zones} zone${zones === 1 ? '' : 's'}.` : 'No zone with that rule covers the area.');
    } else {
      await api('/api/settings/ai-insights/ignore-area/remove', { method: 'POST', body: JSON.stringify(body) });
      window.showToast?.('Area no longer ignored.');
    }
    await loadAiInsights();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    window.showToast?.(`Could not update: ${error.message}`, true);
    button.disabled = false;
  }
});

(async () => {
  await window.daygleAuthReady;
  await loadAiInsights();
})();
