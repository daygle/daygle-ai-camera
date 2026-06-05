const liveEls = {
  frame: document.getElementById('liveFrame'),
  status: document.getElementById('liveStatus'),
  overlayToggle: document.getElementById('overlayToggle'),
};

let refreshTimer;

function snapshotUrl() {
  const overlay = liveEls.overlayToggle.checked ? '1' : '0';
  return `/api/live/snapshot?overlay=${overlay}&t=${Date.now()}`;
}

function refreshFrame() {
  liveEls.frame.src = snapshotUrl();
}

liveEls.frame.addEventListener('load', () => {
  liveEls.status.textContent = liveEls.overlayToggle.checked
    ? 'Live footage · object sighting overlay on'
    : 'Live footage · object sighting overlay off';
});

liveEls.frame.addEventListener('error', () => {
  liveEls.status.textContent = 'Unable to load live footage. Retrying…';
});

liveEls.overlayToggle.addEventListener('change', refreshFrame);

refreshFrame();
refreshTimer = setInterval(refreshFrame, 750);
window.addEventListener('beforeunload', () => clearInterval(refreshTimer));
