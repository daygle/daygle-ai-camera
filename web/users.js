let csrfToken = null;
const usersEl = document.getElementById('users');
const form = document.getElementById('createUserForm');
const message = document.getElementById('userMessage');

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

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
  const me = await api('/api/auth/me', { headers: {} });
  csrfToken = me.csrf_token;
  renderUsers(await api('/api/users', { headers: {} }));
}

usersEl.addEventListener('change', async (event) => {
  if (event.target.dataset.action !== 'role') return;
  await api(`/api/users/${event.target.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ role: event.target.value }) });
  setMessage('Role updated.');
  await loadUsers();
});

usersEl.addEventListener('click', async (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.action === 'toggle') {
    await api(`/api/users/${button.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ is_active: button.dataset.active !== 'true' }) });
    setMessage('User status updated.');
  }
  if (button.dataset.action === 'reset') {
    const password = window.prompt('Enter the new password:');
    if (!password) return;
    await api(`/api/users/${button.dataset.id}`, { method: 'PATCH', body: JSON.stringify({ password }) });
    setMessage('Password reset.');
  }
  await loadUsers();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = new FormData(form);
  await api('/api/users', { method: 'POST', body: JSON.stringify(Object.fromEntries(data.entries())) });
  form.reset();
  setMessage('User created.');
  await loadUsers();
});

document.querySelectorAll('.field-help').forEach((el) => {
  if (!el.title) el.title = el.textContent;
});

loadUsers().catch((error) => setMessage(error.message, true));
