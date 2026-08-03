let users = [];

const usersEl = document.getElementById('users');
const emptyEl = document.getElementById('usersEmpty');
const messageEl = document.getElementById('userMessage');
const createCard = document.getElementById('createUserCard');
const createForm = document.getElementById('createUserForm');
const addUserBtn = document.getElementById('addUserBtn');
const addUserEmptyBtn = document.getElementById('addUserEmptyBtn');
const cancelCreateBtn = document.getElementById('cancelCreateUserBtn');

if (!usersEl || !emptyEl || !messageEl || !createCard || !createForm) {
  console.error('[users.js] required page element missing');
  throw new Error('Users page is missing required elements; check users.html.');
}

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.className = isError ? 'error users-list-status' : 'muted users-list-status';
  if (text) window.showToast?.(text, isError);
}

function escapeAttr(value) {
  return escapeHtml(value ?? '');
}

function roleLabel(value) {
  const normalized = String(value || '').trim().toLowerCase();
  return normalized === 'admin' ? 'Admin' : 'Viewer';
}

function formatDate(value) {
  if (!value) return 'Never';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Unknown';
  return date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function userDisplayName(user) {
  return [user.first_name, user.last_name].filter(Boolean).join(' ') || 'No name set';
}

function buildEditForm(user) {
  const formId = `user-edit-form-${user.id}`;
  return `
    <tr class="user-edit-row">
      <td colspan="5">
        <div class="user-edit-panel">
          <div class="user-edit-head">
            <span>Editing <strong>${escapeHtml(user.username)}</strong></span>
            <span class="user-edit-id">User ID · ${escapeHtml(user.id)}</span>
          </div>
          <form id="${formId}" class="user-edit-form form-grid" data-user-id="${escapeAttr(user.id)}" autocomplete="off">
            <label><span>Username</span><input name="username" required value="${escapeAttr(user.username)}" /></label>
            <label><span>First Name</span><input name="first_name" value="${escapeAttr(user.first_name)}" /></label>
            <label><span>Last Name</span><input name="last_name" value="${escapeAttr(user.last_name)}" /></label>
            <label><span>Email</span><input name="email" type="email" value="${escapeAttr(user.email)}" /></label>
            <label><span>Role</span><select name="role"><option value="viewer" ${user.role === 'viewer' ? 'selected' : ''}>Viewer</option><option value="admin" ${user.role === 'admin' ? 'selected' : ''}>Admin</option></select></label>
            <label><span>Account Status</span><select name="is_active"><option value="true" ${user.is_active ? 'selected' : ''}>Active</option><option value="false" ${!user.is_active ? 'selected' : ''}>Disabled</option></select></label>
            <label class="user-password-field"><span>New Password <small>(optional)</small></span><input name="password" type="password" placeholder="Leave blank to keep current" autocomplete="new-password" /></label>
            <div class="user-edit-footer">
              <button class="secondary user-cancel-edit" type="button" data-id="${escapeAttr(user.id)}">Cancel</button>
              <button type="submit">Save User</button>
            </div>
          </form>
        </div>
      </td>
    </tr>`;
}

function renderUserRow(user) {
  const name = escapeHtml(userDisplayName(user));
  const username = escapeHtml(user.username);
  const active = Boolean(user.is_active);
  const roleClass = user.role === 'admin' ? 'user-role-admin' : 'user-role-viewer';
  return `
    <tr class="user-row${active ? '' : ' user-row-disabled'}" data-user-id="${escapeAttr(user.id)}">
      <td class="user-account-cell"><strong>${username}</strong><span>${name}</span>${user.email ? `<small>${escapeHtml(user.email)}</small>` : ''}</td>
      <td><span class="user-role-pill ${roleClass}">${roleLabel(user.role)}</span></td>
      <td><span class="user-status-pill ${active ? 'user-status-active' : 'user-status-disabled'}"><span class="user-status-dot"></span>${active ? 'Active' : 'Disabled'}</span></td>
      <td class="user-last-login">${escapeHtml(formatDate(user.last_login_at))}</td>
      <td class="user-actions-cell"><div class="user-actions">
        <button class="secondary user-edit-btn" type="button" data-id="${escapeAttr(user.id)}" title="Edit user" aria-label="Edit ${username}">${ICONS.edit}</button>
        <button class="secondary user-toggle-btn" type="button" data-id="${escapeAttr(user.id)}" title="${active ? 'Disable user' : 'Enable user'}" aria-label="${active ? 'Disable' : 'Enable'} ${username}">${ICONS.power}</button>
        <button class="secondary user-reset-btn" type="button" data-id="${escapeAttr(user.id)}" title="Reset password" aria-label="Reset password for ${username}">Reset</button>
      </div></td>
    </tr>`;
}

function renderUsers() {
  if (!users.length) {
    usersEl.innerHTML = '';
    emptyEl.hidden = false;
    return;
  }
  emptyEl.hidden = true;
  const rows = users.map((user) => renderUserRow(user)).join('');
  usersEl.innerHTML = `<div class="users-table-wrap"><table class="users-table"><thead><tr><th scope="col">Account</th><th scope="col">Role</th><th scope="col">Status</th><th scope="col">Last Login</th><th scope="col" class="users-actions-heading">Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  bindUserActions();
}

function openCreateUser() {
  createCard.hidden = false;
  createCard.classList.add('user-editor-card-open');
  createForm.querySelector('[name="username"]')?.focus();
  createCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeCreateUser() {
  createForm.reset();
  createCard.hidden = true;
  createCard.classList.remove('user-editor-card-open');
}

function closeEditRows() {
  usersEl.querySelectorAll('.user-edit-row').forEach((row) => row.remove());
  usersEl.querySelectorAll('.user-row-editing').forEach((row) => row.classList.remove('user-row-editing'));
}

function openEditUser(id) {
  const user = users.find((entry) => String(entry.id) === String(id));
  const row = Array.from(usersEl.querySelectorAll('tr.user-row')).find((entry) => entry.dataset.userId === String(id));
  if (!user || !row) return;
  const existing = row.nextElementSibling;
  closeEditRows();
  if (existing?.classList.contains('user-edit-row')) return;
  row.classList.add('user-row-editing');
  row.insertAdjacentHTML('afterend', buildEditForm(user));
  const form = row.nextElementSibling?.querySelector('.user-edit-form');
  form?.addEventListener('submit', saveEditedUser);
  row.nextElementSibling?.querySelector('.user-cancel-edit')?.addEventListener('click', closeEditRows);
  form?.querySelector('[name="username"]')?.focus();
}

async function saveEditedUser(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const payload = {
    username: String(data.get('username') || '').trim(),
    first_name: String(data.get('first_name') || '').trim(),
    last_name: String(data.get('last_name') || '').trim(),
    email: String(data.get('email') || '').trim(),
    role: data.get('role'),
    is_active: data.get('is_active') === 'true',
  };
  const password = String(data.get('password') || '');
  if (password) payload.password = password;
  try {
    await api(`/api/users/${form.dataset.userId}`, { method: 'PATCH', body: JSON.stringify(payload) });
    setMessage('User updated.');
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
}

async function resetPassword(id) {
  const password = window.prompt('Enter the new password:');
  if (!password) return;
  try {
    await api(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify({ password }) });
    setMessage('Password reset.');
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
}

async function toggleUser(id) {
  const user = users.find((entry) => String(entry.id) === String(id));
  if (!user) return;
  try {
    await api(`/api/users/${id}`, { method: 'PATCH', body: JSON.stringify({ is_active: !user.is_active }) });
    setMessage(user.is_active ? 'User disabled.' : 'User enabled.');
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
}

function bindUserActions() {
  usersEl.querySelectorAll('.user-edit-btn').forEach((button) => button.addEventListener('click', () => openEditUser(button.dataset.id)));
  usersEl.querySelectorAll('.user-toggle-btn').forEach((button) => button.addEventListener('click', () => toggleUser(button.dataset.id)));
  usersEl.querySelectorAll('.user-reset-btn').forEach((button) => button.addEventListener('click', () => resetPassword(button.dataset.id)));
}

async function loadUsers() {
  await window.daygleAuthReady;
  users = await api('/api/users');
  renderUsers();
}

createForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    const data = new FormData(createForm);
    await api('/api/users', { method: 'POST', body: JSON.stringify(Object.fromEntries(data.entries())) });
    closeCreateUser();
    setMessage('User created.');
    await loadUsers();
  } catch (error) {
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
});

addUserBtn?.addEventListener('click', openCreateUser);
addUserEmptyBtn?.addEventListener('click', openCreateUser);
cancelCreateBtn?.addEventListener('click', closeCreateUser);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeEditRows();
    if (!createCard.hidden) closeCreateUser();
  }
});

loadUsers().catch((error) => {
  if (window.daygleAuth?.redirecting) return;
  setMessage(error.message, true);
});
