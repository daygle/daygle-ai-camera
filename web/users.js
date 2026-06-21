const usersEl = document.getElementById('users');
const form = document.getElementById('createUserForm');
const message = document.getElementById('userMessage');

// api() is provided by web/utils.js (loaded before this script). It reads
// window.daygleAuth.csrfToken for state-changing verbs, redirects to /login
// on 401, and sets Content-Type: application/json only on JSON bodies (GETs
// no longer carry a forced Content-Type). The local duplicate + page-local
// csrfToken were removed so every page shares the same fetch contract.

function setMessage(text, isError = false) {
  message.textContent = text;
  if (text) window.showToast?.(text, isError);
}

function roleLabel(value) {
  const normalized = String(value || '').trim().toLowerCase();
  if (!normalized) return 'Unknown';
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

function renderUsers(users) {
  usersEl.innerHTML = users.map((user) => {
    const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ');
    const subtitle = [escapeHtml(roleLabel(user.role)), user.is_active ? 'Active' : 'Disabled', fullName ? escapeHtml(fullName) : '', user.email ? escapeHtml(user.email) : ''].filter(Boolean).join(' · ');
    return `
    <div class="item user-row">
      <div><strong>${escapeHtml(user.username)}</strong><p class="muted">${subtitle}</p></div>
      <select data-action="role" data-id="${user.id}">
        <option value="viewer" ${user.role === 'viewer' ? 'selected' : ''}>Viewer</option>
        <option value="admin" ${user.role === 'admin' ? 'selected' : ''}>Admin</option>
      </select>
      <button class="secondary" data-action="toggle" data-id="${user.id}" data-active="${user.is_active}">${user.is_active ? 'Disable' : 'Enable'}</button>
      <button class="secondary" data-action="reset" data-id="${user.id}">Reset Password</button>
    </div>
  `;
  }).join('');
}

async function loadUsers() {
  // nav.js's daygleAuthReady IIFE has already populated window.daygleAuth.{user, csrfToken}.
  await window.daygleAuthReady;
  renderUsers(await api('/api/users'));
}

usersEl.addEventListener('change', async (event) => {
  if (event.target.dataset.action !== 'role') return;
  try {
    await api(`/api/users/${event.target.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ role: event.target.value }) });
    setMessage('Role updated.');
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
});

usersEl.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  try {
    if (button.dataset.action === 'toggle') {
      await api(`/api/users/${button.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ is_active: button.dataset.active !== 'true' }) });
      setMessage('User status updated.');
    } else if (button.dataset.action === 'reset') {
      const password = window.prompt('Enter the new password:');
      if (!password) return;
      await api(`/api/users/${button.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ password }) });
      setMessage('Password reset.');
    }
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const data = new FormData(form);
    await api('/api/users', { method: 'POST', body: JSON.stringify(Object.fromEntries(data.entries())) });
    form.reset();
    setMessage('User created.');
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
});

document.querySelectorAll('.field-help').forEach((el) => {
  if (!el.title) el.title = el.textContent;
});

loadUsers().catch((error) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(error.message, true);
});
