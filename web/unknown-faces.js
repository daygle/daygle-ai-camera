// unknown-faces.js - Review tab for the Face Recognition page.
// Manages the unknown face capture review workflow: list, assign, dismiss.

(function () {
  const PAGE_SIZE = 20;

  const grid = document.getElementById('reviewGrid');
  const emptyMsg = document.getElementById('reviewEmpty');
  const message = document.getElementById('reviewMessage');
  const pager = document.getElementById('reviewPager');
  const prevBtn = document.getElementById('reviewPrev');
  const nextBtn = document.getElementById('reviewNext');
  const pageInfo = document.getElementById('reviewPageInfo');
  const badge = document.getElementById('reviewBadge');

  if (!grid) return; // not on the face-recognition page

  let offset = 0;
  let total = 0;
  let faces = [];
  let persons = []; // cached for the assign dropdown

  // ── Load helpers ──────────────────────────────────────────────────
  async function loadPersons() {
    try {
      const body = await api('/api/persons');
      persons = body.persons || [];
    } catch { persons = []; }
  }

  async function loadFaces() {
    message.textContent = '';
    try {
      const body = await api(`/api/unknown-faces?status=pending&limit=${PAGE_SIZE}&offset=${offset}`);
      faces = body.faces || [];
      total = body.total || 0;
    } catch (err) {
      message.textContent = err.message || 'Failed to load unknown faces.';
      faces = [];
      total = 0;
    }
    render();
  }

  // ── Render ────────────────────────────────────────────────────────
  function render() {
    grid.innerHTML = '';
    if (!faces.length) {
      emptyMsg.hidden = false;
      pager.hidden = true;
    } else {
      emptyMsg.hidden = true;
      pager.hidden = total <= PAGE_SIZE;
      faces.forEach((face) => grid.appendChild(createCard(face)));
    }
    updatePager();
    updateBadge();
  }

  function createCard(face) {
    const card = document.createElement('div');
    card.className = 'unknown-face-card';
    card.dataset.faceId = face.id;

    // Thumbnail
    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'unknown-face-thumb';
    const img = document.createElement('img');
    img.src = `/api/unknown-faces/${face.id}/thumbnail`;
    img.alt = 'Unknown face';
    img.loading = 'lazy';
    img.onerror = () => { img.style.display = 'none'; };
    thumbWrap.appendChild(img);
    card.appendChild(thumbWrap);

    // Info
    const info = document.createElement('div');
    info.className = 'unknown-face-info';

    const time = document.createElement('div');
    time.className = 'unknown-face-time';
    time.textContent = formatTime(face.created_at);
    info.appendChild(time);

    if (face.camera_id) {
      const cam = document.createElement('div');
      cam.className = 'unknown-face-camera muted';
      cam.textContent = face.camera_id;
      info.appendChild(cam);
    }
    if (face.confidence != null) {
      const conf = document.createElement('div');
      conf.className = 'muted';
      conf.textContent = `Confidence: ${(face.confidence * 100).toFixed(0)}%`;
      info.appendChild(conf);
    }
    card.appendChild(info);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'unknown-face-actions';

    // Assign dropdown
    const assignWrap = document.createElement('div');
    assignWrap.className = 'unknown-face-assign';
    const select = document.createElement('select');
    select.className = 'unknown-face-select';
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = 'Assign to...';
    select.appendChild(defaultOpt);

    // "Create new" option
    const newOpt = document.createElement('option');
    newOpt.value = '__new__';
    newOpt.textContent = '+ Create new person';
    select.appendChild(newOpt);

    persons.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = String(p.id);
      opt.textContent = p.name;
      select.appendChild(opt);
    });
    assignWrap.appendChild(select);

    const assignBtn = document.createElement('button');
    assignBtn.className = 'btn-success btn-sm';
    assignBtn.textContent = 'Assign';
    assignBtn.type = 'button';
    assignBtn.addEventListener('click', () => handleAssign(face.id, select));
    assignWrap.appendChild(assignBtn);
    actions.appendChild(assignWrap);

    // Dismiss button
    const dismissBtn = document.createElement('button');
    dismissBtn.className = 'secondary btn-sm';
    dismissBtn.textContent = 'Dismiss';
    dismissBtn.type = 'button';
    dismissBtn.addEventListener('click', () => handleDismiss(face.id));
    actions.appendChild(dismissBtn);

    card.appendChild(actions);
    return card;
  }

  // ── Actions ───────────────────────────────────────────────────────
  async function handleAssign(faceId, select) {
    const val = select.value;
    if (!val) return;

    let body;
    if (val === '__new__') {
      const name = prompt('Enter a name for the new person:');
      if (!name || !name.trim()) return;
      body = JSON.stringify({ name: name.trim() });
    } else {
      body = JSON.stringify({ person_id: parseInt(val, 10) });
    }

    const card = grid.querySelector(`[data-face-id="${faceId}"]`);
    const btn = card?.querySelector('.btn-success');
    if (btn) btn.disabled = true;

    try {
      const result = await api(`/api/unknown-faces/${faceId}/assign`, { method: 'POST', body });
      showToast(`Assigned to ${result.person_name || 'person'}.`);
      await loadPersons();
      await loadFaces();
    } catch (err) {
      showToast(err.message || 'Failed to assign face.', true);
      if (btn) btn.disabled = false;
    }
  }

  async function handleDismiss(faceId) {
    const card = grid.querySelector(`[data-face-id="${faceId}"]`);
    const btn = card?.querySelector('.secondary');
    if (btn) btn.disabled = true;

    try {
      await api(`/api/unknown-faces/${faceId}/dismiss`, { method: 'POST' });
      showToast('Face dismissed.');
      await loadFaces();
    } catch (err) {
      showToast(err.message || 'Failed to dismiss face.', true);
      if (btn) btn.disabled = false;
    }
  }

  // ── Pager ─────────────────────────────────────────────────────────
  function updatePager() {
    if (total <= PAGE_SIZE) {
      pager.hidden = true;
      return;
    }
    pager.hidden = false;
    const totalPages = Math.ceil(total / PAGE_SIZE);
    const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
    pageInfo.textContent = `Page ${currentPage} of ${totalPages} (${total} faces)`;
    prevBtn.disabled = offset <= 0;
    nextBtn.disabled = offset + PAGE_SIZE >= total;
  }

  function updateBadge() {
    if (!badge) return;
    if (total > 0) {
      badge.textContent = total > 99 ? '99+' : String(total);
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
  }

  prevBtn?.addEventListener('click', () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadFaces();
  });

  nextBtn?.addEventListener('click', () => {
    offset += PAGE_SIZE;
    loadFaces();
  });

  // ── Time formatting ───────────────────────────────────────────────
  function formatTime(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toLocaleString();
    } catch { return iso; }
  }

  // ── Initialise ────────────────────────────────────────────────────
  // Expose a refresh function so the tab can be reloaded on activation.
  window.refreshUnknownFaces = async function () {
    await loadPersons();
    await loadFaces();
  };

  // Preload on page load (the tab may already be active via URL hash).
  loadPersons().then(() => loadFaces());
})();
