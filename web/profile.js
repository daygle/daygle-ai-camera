const profileForm = document.getElementById('profileForm');
const passwordForm = document.getElementById('passwordForm');
const messageEl = document.getElementById('profileMessage');
const summaryEl = document.getElementById('profileSummary');

// api() is provided by web/utils.js (loaded before this script). The local
// duplicate + page-local csrfToken were removed so every page shares the
// same fetch contract (CSRF on state-changing verbs, 401 -> /login,
// JSON Content-Type only on bodies).

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  if (text) window.showToast(text, isError);
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
  // nav.js's daygleAuthReady IIFE has already populated window.daygleAuth.{user, csrfToken}.
  await window.daygleAuthReady;
  renderProfile(window.daygleAuth.user);
}

profileForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(profileForm).entries());
  try {
    const updated = await api('/api/profile', { method: 'PUT', body: JSON.stringify(payload) });
    renderProfile(updated);
    // Apply the new display preferences locally so this tab's timestamps
    // refresh immediately, then broadcast so every other open Daygle tab
    // re-renders without a manual refresh.
    // utils.js is loaded by profile.html before this script, so
    // setDaygleDatePrefs + broadcastDaygleDatePrefs are reliably available.
    window.setDaygleDatePrefs({
      date_format: updated.date_format,
      time_format: updated.time_format,
    });
    window.broadcastDaygleDatePrefs({
      dateFormat: updated.date_format,
      timeFormat: updated.time_format,
    });
    setMessage('Profile saved.');
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
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
  } catch (error) {
    // Skip UI updates if api() triggered a 401 redirect
    if (window.daygleAuth?.redirecting) return;
    setMessage(error.message, true);
  }
});

document.querySelectorAll('.field-help').forEach((el) => {
  if (!el.title) el.title = el.textContent;
});

loadProfile().catch((error) => {
  // Skip UI updates if api() triggered a 401 redirect
  if (window.daygleAuth?.redirecting) return;
  setMessage(error.message, true);
});
