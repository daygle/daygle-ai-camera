// system.js - Admin > System > Health (system.html). Admin-only.
// Host CPU / load / RAM and the NVIDIA GPU (temperature, utilisation, VRAM)
// from /api/system/resources, refreshed every 5 seconds. Moved here from the
// old dashboard. Uses api() / startPageInterval from utils.js.

const els = {
  cpuValue: document.getElementById('cpuValue'),
  cpuSub: document.getElementById('cpuSub'),
  loadValue: document.getElementById('loadValue'),
  loadSub: document.getElementById('loadSub'),
  ramValue: document.getElementById('ramValue'),
  ramSub: document.getElementById('ramSub'),
  gpuCard: document.getElementById('gpuCard'),
  gpuValue: document.getElementById('gpuValue'),
  gpuSub: document.getElementById('gpuSub'),
  vramCard: document.getElementById('vramCard'),
  vramValue: document.getElementById('vramValue'),
  vramSub: document.getElementById('vramSub'),
  gpuNote: document.getElementById('systemGpuNote'),
  decodeSummary: document.getElementById('videoDecodeSummary'),
  decodeCameras: document.getElementById('videoDecodeCameras'),
};

// ─── System resource cards (CPU / Load / RAM) ───────────────────────────────
// RAM is conventionally reported in binary units (1 GB = 1024^3 bytes),
// matching how operating systems and users talk about installed memory, so
// the "GB" label here is binary by design - name and unit label now agree.
function formatGB(bytes) {
  const gb = Number(bytes) / (1024 ** 3);
  if (!Number.isFinite(gb)) return '-';
  return `${gb.toFixed(gb >= 10 ? 0 : 1)} GB`;
}

function renderSystemResources(res) {
  const cpu = res?.cpu_percent;
  if (els.cpuValue) els.cpuValue.textContent = Number.isFinite(cpu) ? `${cpu}%` : ' - ';
  if (els.cpuSub) {
    const cores = res?.cpu_count;
    els.cpuSub.textContent = Number.isFinite(cores) ? `${cores} core${cores === 1 ? '' : 's'}` : 'Processor usage';
  }

  const load = res?.load_average;
  if (els.loadValue) els.loadValue.textContent = Array.isArray(load) && load.length ? load[0].toFixed(2) : ' - ';
  if (els.loadSub) {
    els.loadSub.textContent = Array.isArray(load) && load.length === 3
      ? `${load[0].toFixed(2)} / ${load[1].toFixed(2)} / ${load[2].toFixed(2)} · 1/5/15 min`
      : '1 / 5 / 15 min average';
  }

  const mem = res?.memory;
  const pct = mem?.percent;
  if (els.ramValue) els.ramValue.textContent = Number.isFinite(pct) ? `${pct}%` : ' - ';
  if (els.ramSub) {
    els.ramSub.textContent = mem && Number.isFinite(mem.used) && Number.isFinite(mem.total)
      ? `${formatGB(mem.used)} / ${formatGB(mem.total)} used`
      : 'Memory usage';
  }

  renderGpuStatus(res?.gpu);
  renderVramStatus(res?.gpu);
  renderVideoDecode(res?.video_decode);
}

// ─── Video decoding card ────────────────────────────────────────────────────
// Which decoder each camera's ingest uses (app.video_decode): the NVIDIA
// GPU's NVDEC, or the CPU. Set under Settings > Detection & Live.
const DECODE_SETTING_LABELS = { auto: 'Automatic', gpu: 'GPU', cpu: 'CPU' };

function renderVideoDecode(status) {
  if (!els.decodeSummary || !status) return;
  const cameras = Array.isArray(status.cameras) ? status.cameras : [];
  const onGpu = cameras.filter((camera) => camera.decode === 'gpu').length;
  const setting = DECODE_SETTING_LABELS[status.setting] || 'Automatic';
  let summary = `Setting: ${setting}. `;
  if (!status.ffmpeg_cuda) {
    summary += 'This ffmpeg cannot decode on an NVIDIA GPU (no "cuda" in ffmpeg -hwaccels), so video is decoded on the CPU.';
  } else if (!cameras.length) {
    summary += 'No camera is connected yet.';
  } else {
    summary += `${onGpu} of ${cameras.length} camera${cameras.length === 1 ? '' : 's'} decoding on the GPU.`;
  }
  els.decodeSummary.textContent = summary;
  if (!els.decodeCameras) return;
  els.decodeCameras.innerHTML = cameras.map((camera) => {
    const gpu = camera.decode === 'gpu';
    const note = camera.gpu_fallback ? ' (GPU failed; using CPU)' : '';
    return `<div class="decode-row"><strong>${escapeHtml(camera.name || camera.camera_id || '')}</strong>`
      + `<span class="status-badge">${gpu ? 'GPU (NVDEC)' : 'CPU'}${escapeHtml(note)}</span></div>`;
  }).join('');
}

// ─── GPU health card ────────────────────────────────────────────────────────
// Renders the nvidia-smi snapshot served under /api/system/resources. When
// the host has no NVIDIA GPU (or nvidia-smi is unavailable) the backend
// returns null and the card is hidden entirely. Thermal status comes from
// the backend (warn >= 85 C, critical >= 90 C - the Tesla P4 throttle
// ceiling), driving the card's warning styling and message.
function renderGpuStatus(gpu) {
  if (!els.gpuValue) return;
  const primary = gpu?.primary;

  if (!primary) {
    if (els.gpuCard) els.gpuCard.hidden = true;
    return;
  }
  if (els.gpuCard) els.gpuCard.hidden = false;
  const status = primary?.thermal_status;
  els.gpuCard?.classList.toggle('stat-card-warn', status === 'warn');
  els.gpuCard?.classList.toggle('stat-card-danger', status === 'critical');

  const temp = primary.temperature_c;
  els.gpuValue.textContent = Number.isFinite(temp) ? `${Math.round(temp)}°C` : ' - ';

  const detail = [];
  if (Number.isFinite(primary.utilization_percent)) detail.push(`${Math.round(primary.utilization_percent)}% util`);
  if (Number.isFinite(primary.graphics_clock_mhz)) detail.push(`${Math.round(primary.graphics_clock_mhz)} MHz`);
  if (Number.isFinite(primary.power_draw_watts)) {
    const draw = primary.power_draw_watts.toFixed(0);
    detail.push(Number.isFinite(primary.power_limit_watts)
      ? `${draw}/${primary.power_limit_watts.toFixed(0)} W`
      : `${draw} W`);
  }
  // VRAM has its own dedicated card (renderVramStatus) so it stays visible even
  // when a thermal warning replaces this sub-line; keep it out of the detail
  // here to avoid duplicating it and crowding the thermal/util/power summary.
  const criticalTemp = Number.isFinite(gpu?.critical_temp_c) ? gpu.critical_temp_c : 90;
  if (els.gpuSub) {
    if (status === 'critical') {
      els.gpuSub.textContent = `At ${criticalTemp}°C throttle limit - reduce load or improve airflow`;
    } else if (status === 'warn') {
      els.gpuSub.textContent = `Approaching ${criticalTemp}°C throttle limit - check airflow`;
    } else {
      els.gpuSub.textContent = detail.length ? detail.join(' · ') : 'Graphics card';
    }
  }
  if (els.gpuCard) els.gpuCard.title = primary.name ? String(primary.name) : '';
}

// ─── VRAM (GPU memory) card ──────────────────────────────────────────────────
// A dedicated card mirroring the RAM card: percent used as the value, the
// used / total GiB below. Driven by the same nvidia-smi snapshot as the GPU
// card, so it hides whenever no GPU is present, and also when the driver
// doesn't report memory (memory.used/total come back as null). Kept separate
// from the GPU card so VRAM stays visible even when a thermal warning takes
// over the GPU sub-line.
function renderVramStatus(gpu) {
  if (!els.vramValue) return;
  const primary = gpu?.primary;
  const usedMb = primary?.memory_used_mb;
  const totalMb = primary?.memory_total_mb;
  if (!primary || !Number.isFinite(usedMb) || !Number.isFinite(totalMb) || totalMb <= 0) {
    if (els.vramCard) els.vramCard.hidden = true;
    return;
  }
  if (els.vramCard) els.vramCard.hidden = false;
  const percent = Math.round((usedMb / totalMb) * 100);
  els.vramValue.textContent = `${percent}%`;
  // A nearly-full VRAM pool causes CUDA out-of-memory on the inference path, so
  // flag it the same way the GPU card flags thermal pressure.
  els.vramCard?.classList.toggle('stat-card-warn', percent >= 90 && percent < 97);
  els.vramCard?.classList.toggle('stat-card-danger', percent >= 97);
  if (els.vramSub) {
    // backend reports MiB; scale to bytes so formatGB matches the RAM card.
    els.vramSub.textContent = `${formatGB(usedMb * (1024 ** 2))} / ${formatGB(totalMb * (1024 ** 2))} used`;
  }
}

async function loadSystemResources() {
  try {
    const res = await api('/api/system/resources');
    renderSystemResources(res);
  } catch (_err) {
    // Leave the placeholder dashes in place on a transient failure rather
    // than flashing an error toast every 5 seconds.
    if (window.daygleAuth?.redirecting) return;
  }
}

(async function initSystemPage() {
  await window.daygleAuthReady;
  await loadSystemResources();
  if (els.gpuNote) els.gpuNote.hidden = !(els.gpuCard?.hidden);
  startPageInterval(() => { loadSystemResources().catch(() => {}); }, 5000);
})();
