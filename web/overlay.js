// Shared canvas overlay utilities used by live.js, recordings.js, and timeline.js.

function normalizeDetectionBox(box, frameWidth, frameHeight) {
  const rawX = Number(box?.x ?? 0);
  const rawY = Number(box?.y ?? 0);
  const rawWidth = Number(box?.width ?? 0);
  const rawHeight = Number(box?.height ?? 0);
  if (!Number.isFinite(rawX) || !Number.isFinite(rawY) || !Number.isFinite(rawWidth) || !Number.isFinite(rawHeight)) {
    return null;
  }
  if (rawWidth <= 0 || rawHeight <= 0) return null;
  // Detector returns [0,1] normalized coords; only divide if clearly pixel-space.
  if (rawX <= 1 && rawY <= 1 && rawWidth <= 1 && rawHeight <= 1) {
    return { x: rawX, y: rawY, width: rawWidth, height: rawHeight };
  }
  if (frameWidth <= 0 || frameHeight <= 0) return null;
  return {
    x: Math.max(0, Math.min(1, rawX / frameWidth)),
    y: Math.max(0, Math.min(1, rawY / frameHeight)),
    width: Math.max(0, Math.min(1, rawWidth / frameWidth)),
    height: Math.max(0, Math.min(1, rawHeight / frameHeight)),
  };
}

// Extrapolates detections forward from cur using the velocity (cur - prev).
// t=0 returns cur exactly; t=1 projects one full interval ahead.
// This keeps the box on top of a moving object between inference updates
// rather than lagging behind it.
function interpolateDetections(prev, cur, t) {
  if (!prev || !cur) return cur;
  const tClamped = Math.min(1, Math.max(0, t));
  if (tClamped === 0) return cur;
  return cur.map((curDet) => {
    const prevDet = prev.find((p) => p.label === curDet.label);
    if (!prevDet?.box) return curDet;
    const extrapolate = (a, b) => Math.max(0, Math.min(1, b + (b - a) * tClamped));
    return {
      ...curDet,
      box: {
        x: extrapolate(prevDet.box.x, curDet.box.x),
        y: extrapolate(prevDet.box.y, curDet.box.y),
        width: extrapolate(prevDet.box.width, curDet.box.width),
        height: extrapolate(prevDet.box.height, curDet.box.height),
      },
    };
  });
}

function resizeOverlayCanvas(canvas, referenceEl) {
  if (!canvas || !referenceEl) return;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(referenceEl.clientWidth * dpr));
  const h = Math.max(1, Math.round(referenceEl.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
}

// Draws detection bounding boxes onto canvas. referenceEl is the <video> or <img>
// whose display size and intrinsic dimensions are used for coordinate mapping.
// Assumes the canvas has already been cleared by the caller.
function drawDetectionBoxesOnCanvas(canvas, detections, referenceEl) {
  if (!canvas || !referenceEl || !detections?.length) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const cssWidth = Math.max(1, referenceEl.clientWidth);
  const cssHeight = Math.max(1, referenceEl.clientHeight);
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const srcW = Math.max(1, Number(referenceEl.videoWidth || referenceEl.naturalWidth || cssWidth));
  const srcH = Math.max(1, Number(referenceEl.videoHeight || referenceEl.naturalHeight || cssHeight));
  const scale = Math.min(cssWidth / srcW, cssHeight / srcH);
  const renderWidth = srcW * scale;
  const renderHeight = srcH * scale;
  const offsetX = (cssWidth - renderWidth) / 2;
  const offsetY = (cssHeight - renderHeight) / 2;

  ctx.font = '12px Inter, ui-sans-serif, system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  ctx.lineWidth = 2;

  for (const detection of detections) {
    const box = detection?.box || detection || {};
    const x = Math.min(Math.max(Number(box.x ?? 0), 0), 1);
    const y = Math.min(Math.max(Number(box.y ?? 0), 0), 1);
    const w = Math.min(Math.max(Number(box.width ?? 0), 0), 1);
    const h = Math.min(Math.max(Number(box.height ?? 0), 0), 1);
    if (w <= 0 || h <= 0) continue;
    const dx = offsetX + x * renderWidth;
    const dy = offsetY + y * renderHeight;
    const dw = w * renderWidth;
    const dh = h * renderHeight;
    if (dw < 2 || dh < 2) continue;

    ctx.strokeStyle = '#49e6a3';
    ctx.strokeRect(dx, dy, dw, dh);

    const confidence = Math.round(Number(detection.confidence || 0) * 100);
    const label = `${String(detection.label || 'object')} ${confidence}%`;
    const textWidth = ctx.measureText(label).width;
    const labelHeight = 20;
    const labelWidth = textWidth + 12;
    const labelY = dy > labelHeight + 4 ? dy - labelHeight - 4 : dy + 4;
    ctx.fillStyle = 'rgba(7, 11, 19, 0.86)';
    ctx.fillRect(dx, labelY, labelWidth, labelHeight);
    ctx.fillStyle = '#49e6a3';
    ctx.fillText(label, dx + 6, labelY + labelHeight / 2);
  }
}
