// ai-ollama.js - the "Local Model (Ollama)" card on the AI page (ai.html).
// Installs Ollama on this server and manages its models through
// /api/settings/ollama (app/ollama_manager.py). Installs and downloads run
// on the server; this page polls their progress. "Use" points the Model
// Server form (ai.js) at the chosen model and saves it.
// Uses api() / escapeHtml / showToast from utils.js.

const ollamaEls = {
  card: document.getElementById('ollamaCard'),
  status: document.getElementById('ollamaStatus'),
  install: document.getElementById('ollamaInstallBtn'),
  start: document.getElementById('ollamaStartBtn'),
  restart: document.getElementById('ollamaRestartBtn'),
  stop: document.getElementById('ollamaStopBtn'),
  check: document.getElementById('ollamaCheckBtn'),
  updateAll: document.getElementById('ollamaUpdateAllBtn'),
  checked: document.getElementById('ollamaCheckedText'),
  logBox: document.getElementById('ollamaInstallLogBox'),
  log: document.getElementById('ollamaInstallLog'),
  models: document.getElementById('ollamaModels'),
  rows: document.getElementById('ollamaModelRows'),
  pullName: document.getElementById('ollamaPullName'),
  pullBtn: document.getElementById('ollamaPullBtn'),
  suggested: document.getElementById('ollamaSuggested'),
  suggestions: document.getElementById('ollamaSuggestions'),
  progress: document.getElementById('ollamaPullProgress'),
  bar: document.getElementById('ollamaPullBar'),
  barText: document.getElementById('ollamaPullText'),
};
const OLLAMA_POLL_MS = 2000;
let ollamaState = null;
let ollamaPollTimer = null;
let ollamaBusy = false;

function ollamaToast(text, isError = false) {
  if (text) window.showToast?.(text, isError);
}

function ollamaSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)} GB`;
  if (value >= 1e6) return `${Math.round(value / 1e6)} MB`;
  return value ? `${Math.round(value / 1e3)} kB` : '-';
}

// "http://localhost:11434/v1/" and "http://127.0.0.1:11434/v1" are the same
// server, so a saved form value still shows its model as in use.
function ollamaSameServer(a, b) {
  const norm = (url) => String(url || '').trim().replace(/\/+$/, '').replace('//localhost:', '//127.0.0.1:').toLowerCase();
  return Boolean(a) && norm(a) === norm(b);
}

// Ollama stores "moondream" as "moondream:latest"; both name the same model.
function ollamaTagged(name) {
  const value = String(name || '').trim();
  return !value || value.includes(':') ? value : `${value}:latest`;
}

function ollamaInUse(state, name) {
  const current = window.daygleAiCurrentModelServer?.() || {};
  return ollamaSameServer(current.serverUrl, state?.openai_url) && ollamaTagged(current.model) === ollamaTagged(name);
}

// True when dotted version a is newer than b.
function ollamaNewer(a, b) {
  const parse = (value) => String(value || '').split('.').map((part) => Number.parseInt(part, 10) || 0);
  const left = parse(a);
  const right = parse(b);
  if (!a || !b) return false;
  for (let i = 0; i < Math.max(left.length, right.length); i += 1) {
    if ((left[i] || 0) !== (right[i] || 0)) return (left[i] || 0) > (right[i] || 0);
  }
  return false;
}

function ollamaAgo(seconds) {
  if (!seconds) return '';
  const age = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (age < 60) return 'just now';
  if (age < 3600) return `${Math.round(age / 60)} min ago`;
  if (age < 86400) return `${Math.round(age / 3600)} h ago`;
  return `${Math.round(age / 86400)} d ago`;
}

function ollamaCheckedText(state) {
  if (!state.release_checked_at && !state.models_checked_at) return '';
  const when = ollamaAgo(Math.max(state.release_checked_at || 0, state.models_checked_at || 0));
  const outdated = (state.models || []).filter((model) => model.update_available).length;
  const parts = [`Checked for updates ${when}.`];
  if (state.release_check_error) parts.push(state.release_check_error);
  else if (state.installed && !state.update_available && !ollamaNewer(state.latest_version, state.version)) parts.push('Ollama is up to date.');
  if (state.models_checked_at && state.models?.length) parts.push(outdated ? `${outdated} model update${outdated === 1 ? '' : 's'} available.` : 'Models are up to date.');
  return parts.join(' ');
}

function ollamaStatusText(state) {
  const install = state.install || {};
  if (install.running) {
    return state.installed
      ? `Updating Ollama to ${state.install_version}… This downloads about 1.5 GB; AI checks pause while the server restarts. You can leave this page.`
      : 'Installing Ollama… This downloads about 1.5 GB and can take several minutes. You can leave this page.';
  }
  const parts = [];
  if (!state.installed) {
    parts.push('Ollama is not installed on this server.');
    if (state.install_unavailable_reason) parts.push(state.install_unavailable_reason);
  } else if (state.reachable) {
    parts.push(`Ollama ${state.version || ''} is running.`.replace(/\s+\./, '.'));
    if (state.update_available) parts.push(`Version ${state.install_version} is available.`);
    if (!state.managed && state.binary) {
      const newer = ollamaNewer(state.latest_version, state.version);
      parts.push(newer
        ? `Version ${state.latest_version} is available, but this Ollama was installed outside Daygle, so update it yourself.`
        : 'It was installed outside Daygle, so updates are up to you.');
    }
    if (!state.models?.length) parts.push('Next, download a vision model below.');
  } else {
    parts.push(`Ollama is installed but not answering${state.service ? ` (service: ${state.service})` : ''}.`);
    if (state.can_control_service) parts.push('Start it below.');
  }
  if (install.ok === false && install.error) parts.push(`Last install failed: ${install.error}`);
  return parts.join(' ');
}

function renderOllamaButtons(state) {
  const install = state.install || {};
  const canInstall = Boolean(state.can_install) && !install.running && (!state.installed || state.update_available);
  if (ollamaEls.install) {
    ollamaEls.install.hidden = !canInstall;
    ollamaEls.install.textContent = state.installed ? `Update Ollama to ${state.install_version}` : 'Install Ollama';
    ollamaEls.install.disabled = ollamaBusy;
  }
  if (ollamaEls.check) {
    ollamaEls.check.hidden = !state.installed;
    ollamaEls.check.disabled = ollamaBusy || Boolean(install.running);
  }
  if (ollamaEls.checked) ollamaEls.checked.textContent = ollamaCheckedText(state);
  const control = Boolean(state.can_control_service) && !install.running;
  if (ollamaEls.start) { ollamaEls.start.hidden = !control || state.reachable; ollamaEls.start.disabled = ollamaBusy; }
  if (ollamaEls.restart) { ollamaEls.restart.hidden = !control || !state.reachable; ollamaEls.restart.disabled = ollamaBusy; }
  if (ollamaEls.stop) { ollamaEls.stop.hidden = !control || !state.reachable; ollamaEls.stop.disabled = ollamaBusy; }
}

function renderOllamaLog(install) {
  const lines = Array.isArray(install?.log) ? install.log : [];
  if (!ollamaEls.logBox || !ollamaEls.log) return;
  ollamaEls.logBox.hidden = !lines.length;
  ollamaEls.log.textContent = lines.join('\n');
  if (install.running) ollamaEls.logBox.open = true;
  ollamaEls.log.scrollTop = ollamaEls.log.scrollHeight;
}

function ollamaModelRow(state, model, pulling) {
  const name = String(model.name || '');
  const vision = model.vision === true ? 'Yes' : model.vision === false ? 'No' : '?';
  const details = [model.parameter_size, model.quantization].filter(Boolean).join(' · ');
  const inUse = ollamaInUse(state, name);
  const useBtn = inUse
    ? '<span class="ollama-in-use">In use</span>'
    : `<button class="secondary" type="button" data-ollama-use="${escapeHtml(name)}"${model.vision === false ? ' disabled title="This model cannot read images."' : ''}>Use</button>`;
  const updateBtn = model.update_available
    ? `<button class="btn-info" type="button" data-ollama-update="${escapeHtml(name)}"${pulling ? ' disabled' : ''}>Update</button>`
    : '';
  const badge = model.update_available
    ? '<br><span class="ollama-update-badge">Update available</span>'
    : model.update_error ? `<br><small class="muted" title="${escapeHtml(model.update_error)}">Updates not checked</small>` : '';
  return `<tr>
    <td><strong>${escapeHtml(name)}</strong>${details ? `<br><small class="muted">${escapeHtml(details)}</small>` : ''}${badge}</td>
    <td>${escapeHtml(ollamaSize(model.size))}</td>
    <td>${vision}</td>
    <td class="ollama-model-actions">${updateBtn}${useBtn}
      <button class="btn-danger" type="button" data-ollama-delete="${escapeHtml(name)}"${pulling || inUse ? ' disabled' : ''}${inUse ? ' title="Switch the AI to another model first."' : ''}>Delete</button></td>
  </tr>`;
}

function renderOllamaModels(state) {
  if (!ollamaEls.models) return;
  ollamaEls.models.hidden = !state.reachable;
  if (!state.reachable) return;
  const pull = state.pull || {};
  const models = Array.isArray(state.models) ? state.models : [];
  if (ollamaEls.rows) {
    ollamaEls.rows.innerHTML = models.length
      ? models.map((model) => ollamaModelRow(state, model, pull.running)).join('')
      : `<tr><td colspan="4" class="muted">${escapeHtml(state.models_error || 'No models yet. Download one below.')}</td></tr>`;
  }
  const installed = new Set(models.map((model) => ollamaTagged(model.name)));
  const suggested = (state.suggested_models || []).filter((model) => !installed.has(ollamaTagged(model.name)));
  if (ollamaEls.suggested) {
    ollamaEls.suggested.innerHTML = (state.suggested_models || [])
      .map((model) => `<option value="${escapeHtml(model.name)}"></option>`).join('');
  }
  if (ollamaEls.suggestions) {
    ollamaEls.suggestions.innerHTML = suggested.map((model) => `<li><button class="ollama-suggest" type="button" data-ollama-suggest="${escapeHtml(model.name)}">${escapeHtml(model.name)}</button>
      ${escapeHtml(`${model.size_gb} GB download, ~${model.memory_gb} GB GPU memory. ${model.note}`)}</li>`).join('');
  }
  if (ollamaEls.pullBtn) ollamaEls.pullBtn.disabled = Boolean(pull.running) || ollamaBusy;
  const outdated = models.filter((model) => model.update_available).length;
  if (ollamaEls.updateAll) {
    ollamaEls.updateAll.hidden = outdated < 2;
    ollamaEls.updateAll.textContent = `Update All (${outdated})`;
    ollamaEls.updateAll.disabled = Boolean(pull.running) || ollamaBusy;
  }
  if (ollamaEls.pullName && !ollamaEls.pullName.value && !models.length) ollamaEls.pullName.value = suggested[0]?.name || '';
  renderOllamaPull(pull);
}

function renderOllamaPull(pull) {
  if (!ollamaEls.progress) return;
  const show = Boolean(pull?.running || (pull?.ok === false && pull?.error));
  ollamaEls.progress.hidden = !show;
  if (!show) return;
  const total = Number(pull.total) || 0;
  const done = Number(pull.completed) || 0;
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  if (ollamaEls.bar) {
    ollamaEls.bar.hidden = !pull.running;
    if (total) ollamaEls.bar.value = percent;
    else ollamaEls.bar.removeAttribute('value');
  }
  if (ollamaEls.barText) {
    const verb = pull.kind === 'update' ? 'Updating' : 'Downloading';
    const queue = Array.isArray(pull.queue) && pull.queue.length > 1 ? ` (${pull.index} of ${pull.queue.length})` : '';
    ollamaEls.barText.textContent = pull.running
      ? `${verb} ${pull.model}${queue}: ${total ? `${percent}% (${ollamaSize(done)} of ${ollamaSize(total)})` : pull.status || 'starting'}`
      : `${pull.kind === 'update' ? 'Update' : 'Download'} failed: ${pull.error}`;
  }
}

// Toasts for a background job that finished while the page was open.
function announceOllamaChanges(previous, state) {
  if (!previous) return;
  if (previous.install?.running && !state.install?.running) {
    if (state.install?.ok) ollamaToast(previous.installed ? `Ollama updated to ${state.version || state.install_version}.` : 'Ollama installed. Now download a vision model.');
    else ollamaToast(`Ollama install failed: ${state.install?.error || 'see the install log'}`, true);
  }
  if (previous.pull?.running && !state.pull?.running) {
    const updated = state.pull?.updated || [];
    if (state.pull?.kind === 'update') {
      if (state.pull.ok) ollamaToast(`Updated ${updated.length === 1 ? updated[0] : `${updated.length} models`}.`);
      else ollamaToast(`Model update failed: ${state.pull.error || 'unknown error'}`, true);
    } else if (state.pull?.ok) ollamaToast(`Downloaded ${state.pull.model}. Click Use to switch the AI to it.`);
    else ollamaToast(`Download of ${state.pull?.model} failed: ${state.pull?.error || 'unknown error'}`, true);
  }
}

function renderOllama(state) {
  if (!state || !ollamaEls.card) return;
  announceOllamaChanges(ollamaState, state);
  ollamaState = state;
  if (ollamaEls.status) ollamaEls.status.textContent = ollamaStatusText(state);
  renderOllamaButtons(state);
  renderOllamaLog(state.install);
  renderOllamaModels(state);
  scheduleOllamaPoll(state);
}

function scheduleOllamaPoll(state) {
  if (ollamaPollTimer || !(state.install?.running || state.pull?.running)) return;
  ollamaPollTimer = setTimeout(async () => {
    ollamaPollTimer = null;
    try {
      renderOllama(await api('/api/settings/ollama'));
    } catch (_error) {
      // A restart of the app (e.g. during an update) drops one poll; retry.
      scheduleOllamaPoll(state);
    }
  }, OLLAMA_POLL_MS);
}

async function ollamaAction(fn) {
  if (ollamaBusy) return;
  ollamaBusy = true;
  if (ollamaState) renderOllamaButtons(ollamaState);
  try {
    const next = await fn();
    if (next) renderOllama(next);
  } catch (error) {
    if (!window.daygleAuth?.redirecting) ollamaToast(error.message, true);
  } finally {
    ollamaBusy = false;
    if (ollamaState) renderOllama(ollamaState);
  }
}

function ollamaPost(path, body) {
  return api(path, { method: 'POST', body: JSON.stringify(body || {}) });
}

ollamaEls.install?.addEventListener('click', () => ollamaAction(async () => {
  const update = Boolean(ollamaState?.installed);
  const question = update
    ? `Update Ollama to ${ollamaState.install_version}? It downloads about 1.5 GB, then the model server restarts, so AI checks pause for a minute (alerts are still sent).`
    : `Install Ollama ${ollamaState?.install_version || ''} on this server? It downloads about 1.5 GB and needs about 6 GB of disk, plus 2-8 GB per model.`;
  if (!window.confirm(question)) return null;
  return ollamaPost(update ? '/api/settings/ollama/update' : '/api/settings/ollama/install');
}));

[['start', ollamaEls.start], ['restart', ollamaEls.restart], ['stop', ollamaEls.stop]].forEach(([action, button]) => {
  button?.addEventListener('click', () => ollamaAction(async () => {
    if (action === 'stop' && !window.confirm('Stop Ollama? AI checks are skipped (alerts are still sent) until it is started again.')) return null;
    const state = await ollamaPost('/api/settings/ollama/service', { action });
    ollamaToast(`Ollama ${action === 'stop' ? 'stopped' : action === 'start' ? 'started' : 'restarted'}.`);
    return state;
  }));
});

ollamaEls.check?.addEventListener('click', () => ollamaAction(async () => {
  const state = await ollamaPost('/api/settings/ollama/check-updates', { force: true });
  ollamaToast(ollamaCheckedText(state) || 'Checked for updates.');
  return state;
}));

ollamaEls.updateAll?.addEventListener('click', () => ollamaAction(async () => {
  if (!window.confirm('Download the newer version of every model with an update? Only the changed parts are downloaded.')) return null;
  return ollamaPost('/api/settings/ollama/models/update', { all: true });
}));

ollamaEls.pullBtn?.addEventListener('click', () => ollamaAction(async () => {
  const model = String(ollamaEls.pullName?.value || '').trim();
  if (!model) throw new Error('Enter a model name, e.g. gemma3:4b.');
  return ollamaPost('/api/settings/ollama/models/pull', { model });
}));

ollamaEls.pullName?.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    ollamaEls.pullBtn?.click();
  }
});

ollamaEls.card?.addEventListener('click', (event) => {
  const target = event.target instanceof Element ? event.target.closest('button') : null;
  if (!target) return;
  const suggest = target.getAttribute('data-ollama-suggest');
  const use = target.getAttribute('data-ollama-use');
  const remove = target.getAttribute('data-ollama-delete');
  const update = target.getAttribute('data-ollama-update');
  if (update) {
    ollamaAction(() => ollamaPost('/api/settings/ollama/models/update', { models: [update] }));
    return;
  }
  if (suggest && ollamaEls.pullName) {
    ollamaEls.pullName.value = suggest;
    ollamaEls.pullName.focus();
  } else if (use) {
    ollamaAction(async () => {
      await window.daygleAiUseLocalModel?.(ollamaState.openai_url, use);
      return ollamaState;
    });
  } else if (remove) {
    ollamaAction(async () => {
      if (!window.confirm(`Delete ${remove} from this server? It can be downloaded again later.`)) return null;
      const state = await ollamaPost('/api/settings/ollama/models/delete', { model: remove });
      ollamaToast(`Deleted ${remove}.`);
      return state;
    });
  }
});

async function loadOllama() {
  if (!ollamaEls.card) return;
  await window.daygleAuthReady;
  try {
    renderOllama(await api('/api/settings/ollama'));
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    if (ollamaEls.status) ollamaEls.status.textContent = `Could not check Ollama: ${error.message}`;
    return;
  }
  // Look for Ollama and model updates in the background. The server keeps
  // the result for 6 hours, so this rarely leaves the server.
  if (ollamaState?.installed) {
    try {
      const state = await ollamaPost('/api/settings/ollama/check-updates', { force: false });
      if (!ollamaBusy) renderOllama(state);
    } catch (_error) {
      // Offline or rate limited: the page works without update information.
    }
  }
}

document.addEventListener('daygle:ai-settings-loaded', () => {
  if (ollamaState) renderOllama(ollamaState);
});

loadOllama();
