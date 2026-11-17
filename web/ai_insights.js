// ai_insights.js - the AI Insights card on Intelligence > AI (ai.html).
// Admin-only. Reads GET /api/settings/ai-insights (app/ai_insights.py):
// minimum-confidence suggestions from the AI's per-detection verdicts, with
// an Apply button, and AI tags named often that no rule watches yet. Uses
// api() / escapeHtml / titleCase / showToast from utils.js.

const aiTuningList = document.getElementById('aiTuning');
const aiFrequentTagsList = document.getElementById('aiFrequentTags');

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
  if (aiFrequentTagsList) {
    const tags = data?.frequent_tags || [];
    aiFrequentTagsList.innerHTML = tags.length
      ? tags.map(aiTagsRow).join('')
      : `<span class="muted">No frequent AI tags without an alert in the last ${Number(data?.tags_window_days || 7)} days.</span>`;
  }
}

function loadAiInsights() {
  if (!aiTuningList && !aiFrequentTagsList) return Promise.resolve();
  return api('/api/settings/ai-insights').then(renderAiInsights).catch(() => {
    if (aiTuningList) aiTuningList.innerHTML = '<span class="muted">Could not load AI insights.</span>';
    if (aiFrequentTagsList) aiFrequentTagsList.innerHTML = '';
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

(async () => {
  await window.daygleAuthReady;
  await loadAiInsights();
})();
