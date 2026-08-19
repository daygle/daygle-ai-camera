// people.js - People / face enrolment management (people.html).
// Admin-only. Uses api() / escapeHtml / safeHtml / showToast from utils.js.

const addPersonForm = document.getElementById('addPersonForm');
const addPersonBtn = document.getElementById('addPersonBtn');
const peopleList = document.getElementById('peopleList');
const peopleEmpty = document.getElementById('peopleEmpty');
const peopleMessage = document.getElementById('peopleMessage');

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function personCard(person) {
  const count = person.face_count ?? 0;
  // Compose with a plain template + escapeHtml on the (user-supplied) values.
  // Building the notes fragment with safeHtml and interpolating it into another
  // safeHtml`` would double-escape it and render the tags as visible text.
  const notes = person.notes ? `<p class="muted">${escapeHtml(person.notes)}</p>` : '';
  return `
    <div class="model-card" data-person-id="${escapeHtml(String(person.id))}">
      <div class="model-card-head">
        <strong>${escapeHtml(person.name)}</strong>
        <span class="muted">${escapeHtml(String(count))} face${count === 1 ? '' : 's'}</span>
      </div>
      ${notes}
      <div class="button-row">
        <label class="btn-info model-action-btn" style="cursor:pointer">
          ⬆ Enrol Face<input type="file" accept="image/*" data-action="enroll" hidden />
        </label>
        <button class="btn-info model-action-btn" type="button" data-action="faces">Faces</button>
        <button class="btn-info model-action-btn" type="button" data-action="rename">Rename</button>
        <button class="btn-danger model-action-btn" type="button" data-action="delete">Delete</button>
      </div>
      <div class="person-faces" data-faces hidden></div>
    </div>`;
}

async function loadPeople() {
  try {
    const body = await api('/api/persons');
    const people = body.persons || [];
    peopleEmpty.hidden = people.length > 0;
    peopleList.innerHTML = people.map(personCard).join('');
  } catch (err) {
    peopleMessage.textContent = err.message || 'Failed to load people.';
  }
}

async function addPerson(event) {
  event.preventDefault();
  const name = addPersonForm.name.value.trim();
  if (!name) return;
  addPersonBtn.disabled = true;
  try {
    await api('/api/persons', {
      method: 'POST',
      body: JSON.stringify({ name, notes: addPersonForm.notes.value.trim() }),
    });
    addPersonForm.reset();
    showToast('Person added.');
    await loadPeople();
  } catch (err) {
    showToast(err.message || 'Failed to add person.', true);
  } finally {
    addPersonBtn.disabled = false;
  }
}

function cardFor(target) {
  return target.closest('[data-person-id]');
}

async function enrollFace(card, file) {
  const personId = card.dataset.personId;
  showToast('Enrolling face…');
  try {
    await api(`/api/persons/${encodeURIComponent(personId)}/faces`, {
      method: 'POST',
      body: file,
      headers: { 'Content-Type': file.type || 'image/jpeg' },
    });
    showToast('Face enrolled.');
    await loadPeople();
    // Re-open the faces panel if it was showing.
    const panel = card.querySelector('[data-faces]');
    if (panel && !panel.hidden) {
      await showFaces(cardFor(card) || card);
    }
  } catch (err) {
    showToast(err.message || 'Enrolment failed.', true);
  }
}

async function showFaces(card) {
  const panel = card.querySelector('[data-faces]');
  const personId = card.dataset.personId;
  if (!panel.hidden) {
    panel.hidden = true;
    return;
  }
  try {
    const person = await api(`/api/persons/${encodeURIComponent(personId)}`);
    const faces = person.faces || [];
    if (!faces.length) {
      panel.innerHTML = '<p class="muted">No faces enrolled for this person yet.</p>';
    } else {
      panel.innerHTML = faces.map((face) => safeHtml`
        <div class="person-face-row">
          <span class="muted">Face #${String(face.id)} · ${formatDate(face.created_at)}</span>
          <button class="btn-danger model-action-btn" type="button" data-action="delete-face" data-face-id="${String(face.id)}">Delete</button>
        </div>`).join('');
    }
    panel.hidden = false;
  } catch (err) {
    showToast(err.message || 'Failed to load faces.', true);
  }
}

async function renamePerson(card) {
  const personId = card.dataset.personId;
  const current = card.querySelector('strong')?.textContent || '';
  const name = window.prompt('New name for this person:', current);
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) return;
  try {
    await api(`/api/persons/${encodeURIComponent(personId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: trimmed }),
    });
    showToast('Person renamed.');
    await loadPeople();
  } catch (err) {
    showToast(err.message || 'Rename failed.', true);
  }
}

async function deletePerson(card) {
  const personId = card.dataset.personId;
  const name = card.querySelector('strong')?.textContent || 'this person';
  if (!window.confirm(`Delete ${name} and all their enrolled faces?`)) return;
  try {
    await api(`/api/persons/${encodeURIComponent(personId)}`, { method: 'DELETE' });
    showToast('Person deleted.');
    await loadPeople();
  } catch (err) {
    showToast(err.message || 'Delete failed.', true);
  }
}

async function deleteFace(card, faceId) {
  const personId = card.dataset.personId;
  if (!window.confirm('Delete this enrolled face?')) return;
  try {
    await api(`/api/persons/${encodeURIComponent(personId)}/faces/${encodeURIComponent(faceId)}`, { method: 'DELETE' });
    showToast('Face deleted.');
    await loadPeople();
  } catch (err) {
    showToast(err.message || 'Delete failed.', true);
  }
}

peopleList.addEventListener('click', (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const card = cardFor(button);
  if (!card) return;
  const action = button.dataset.action;
  if (action === 'faces') showFaces(card);
  else if (action === 'rename') renamePerson(card);
  else if (action === 'delete') deletePerson(card);
  else if (action === 'delete-face') deleteFace(card, button.dataset.faceId);
});

peopleList.addEventListener('change', (event) => {
  const input = event.target.closest('input[data-action="enroll"]');
  if (!input || !input.files || !input.files.length) return;
  const card = cardFor(input);
  const file = input.files[0];
  input.value = '';
  if (card) enrollFace(card, file);
});

addPersonForm.addEventListener('submit', addPerson);

loadPeople();
