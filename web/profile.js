let csrfToken = null;
const profileForm = document.getElementById('profileForm');
const passwordForm = document.getElementById('passwordForm');
const messageEl = document.getElementById('profileMessage');
const summaryEl = document.getElementById('profileSummary');

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes((options.method || 'GET').toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) window.location.href = '/login';
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed: ${response.status}`);
  return payload;
}

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  if (text) window.showToast?.(text, isError);
}

function renderProfile(user) {
  profileForm.elements.username.value = user.username || '';
  profileForm.elements.first_name.value = user.first_name || '';
  profileForm.elements.last_name.value = user.last_name || '';
  profileForm.elements.email.value = user.email || '';
  profileForm.elements.timezone.value = user.timezone || 'Australia/Sydney';
  profileForm.elements.date_format.value = user.date_format || 'locale';
  profileForm.elements.time_format.value = user.time_format || '24h';
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ');
  summaryEl.innerHTML = `
    <div><span>Username</span><strong>${escapeHtml(user.username)}</strong></div>
    ${fullName ? `<div><span>Name</span><strong>${escapeHtml(fullName)}</strong></div>` : ''}
    ${user.email ? `<div><span>Email</span><strong>${escapeHtml(user.email)}</strong></div>` : ''}
    <div><span>Role</span><strong>${escapeHtml(user.role)}</strong></div>
    <div><span>Timezone</span><strong>${escapeHtml(user.timezone || 'Australia/Sydney')}</strong></div>
    <div><span>Date/time</span><strong>${escapeHtml(user.date_format || 'locale')} / ${escapeHtml(user.time_format || '24h')}</strong></div>
  `;
}

async function loadProfile() {
  const me = await api('/api/auth/me');
  csrfToken = me.csrf_token;
  renderProfile(me.user);
}

profileForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(profileForm).entries());
  try {
    renderProfile(await api('/api/profile', { method: 'PUT', body: JSON.stringify(payload) }));
    setMessage('Profile saved.');
  } catch (error) { setMessage(error.message, true); }
});

passwordForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(passwordForm).entries());
  if (payload.new_password !== payload.confirm_password) {
    setMessage('Passwords do not match.', true);
    return;
  }
  delete payload.confirm_password;
  try {
    await api('/api/profile/password', { method: 'POST', body: JSON.stringify(payload) });
    passwordForm.reset();
    setMessage('Password changed.');
  } catch (error) { setMessage(error.message, true); }
});

loadProfile().catch((error) => setMessage(error.message, true));
