// profile-fields.js - the fields a camera Day/Night profile can set, shared by
// the Settings > Camera Profiles editor and the camera page's profile summary.
// Keys and bounds mirror app/recording_settings.py::_normalize_profile_value.
// A field left on Global Default is not stored, so the camera follows the
// Settings page value. The motion-engine values that moved to the global
// Advanced Motion Engine (Wake-Up Threshold and friends) are not profile fields.

const PROFILE_BOOL_OPTIONS = [
  { value: 'true', label: 'Enabled' },
  { value: 'false', label: 'Disabled' },
];

// eslint-disable-next-line no-unused-vars -- ESLint: exported for later scripts
const PROFILE_FIELD_GROUPS = [
  {
    title: 'Detection Timing',
    fields: [
      { key: 'detection_interval_seconds', label: 'Detection Interval (s)', kind: 'number', min: 0.1, max: 10, step: 0.05, placeholder: '0.5', tip: 'How often this camera is checked. Lower = faster alerts, more CPU/GPU.' },
      { key: 'face_detection_interval_seconds', label: 'Face Detection Interval (s)', kind: 'number', min: 0.1, max: 10, step: 0.05, placeholder: '1' },
      { key: 'ingest_frame_fps', label: 'Detection Frame Rate (fps)', kind: 'number', min: 1, max: 30, step: 1, placeholder: '4' },
      { key: 'adaptive_detection_enabled', label: 'Adaptive Cadence', kind: 'select', options: PROFILE_BOOL_OPTIONS, tip: 'Stretch the detection interval on a quiet scene and when the inference queue is backed up, recovering to the full rate on motion. Disable to run every cycle at the exact interval.' },
      { key: 'background_detection_enabled', label: 'Background Detection', kind: 'select', options: PROFILE_BOOL_OPTIONS, tip: 'Keep detecting (alerts, recordings, snapshots) while no Live page is open. Enabled or Disabled overrides the Settings page value.' },
    ],
  },
  {
    title: 'Confirmation',
    fields: [
      { key: 'detection_confirm_frames', label: 'Confirm Frames', kind: 'number', min: 1, max: 10, step: 1, placeholder: '1', tip: 'Require an object in this many recent cycles before it can alert or record. 1 = react on the first frame.' },
      { key: 'detection_confirm_window', label: 'Confirm Window', kind: 'number', min: 1, max: 30, step: 1, placeholder: '1', requires: 'confirm' },
      { key: 'detection_confirm_iou', label: 'Confirm Location (IoU)', kind: 'number', min: 0, max: 0.9, step: 0.05, placeholder: '0', requires: 'confirm' },
    ],
  },
  {
    title: 'Object Detection',
    fields: [
      { key: 'always_run_object_detection', label: 'Always Run Object Detection', kind: 'select', options: PROFILE_BOOL_OPTIONS },
      { key: 'periodic_scan_interval_seconds', label: 'Periodic Scan (s)', kind: 'number', min: 0, max: 3600, step: 1, placeholder: '0', requires: 'motion-gated' },
      { key: 'object_detection_region_boost', label: 'Region Boost', kind: 'select', options: PROFILE_BOOL_OPTIONS },
      { key: 'object_detection_tiling', label: 'Tiling', kind: 'select', options: [
        { value: 'off', label: 'Off' }, { value: '2x2', label: '2 × 2' }, { value: '3x3', label: '3 × 3' }, { value: '4x4', label: '4 × 4' },
      ] },
      { key: 'object_detection_low_light', label: 'Low-Light Enhancement', kind: 'select', tip: 'Boost contrast on the copy of the frame the object detector sees. Automatic only enhances dark frames.', options: [
        { value: 'off', label: 'Off' }, { value: 'auto', label: 'Automatic (Dark Frames)' }, { value: 'on', label: 'Always' },
      ] },
      { key: 'object_detection_second_look', label: 'Second Look', kind: 'select', options: PROFILE_BOOL_OPTIONS, tip: 'Re-check a watched object that scores just under its threshold on a zoomed, full-resolution crop.' },
    ],
  },
  {
    title: 'Motion',
    fields: [
      { key: 'motion_pixel_threshold', label: 'Ignore Small Light Changes', kind: 'pixel', tip: 'How much a single pixel must brighten or darken before it counts as changed. Raise it (High) where the night picture is grainy or flickers under IR.' },
      { key: 'motion_denoise', label: 'Clean Up Speckle Noise', kind: 'select', options: [{ value: 'true', label: 'On' }, { value: 'false', label: 'Off' }] },
      { key: 'motion_shadow_suppression', label: 'Ignore Shadows', kind: 'select', tip: "Don't count moving shadows as motion (MOG2 only). Off for dark/IR scenes; Automatic = only while bright.", options: [
        { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }, { value: 'auto', label: 'Automatic (Day Only)' },
      ] },
      { key: 'motion_frame_width', label: 'Motion Frame Width', kind: 'number', min: 40, max: 640, step: 1, placeholder: '320' },
      { key: 'motion_frame_height', label: 'Motion Frame Height', kind: 'number', min: 30, max: 480, step: 1, placeholder: '240' },
    ],
  },
];

// Typed value for a profile field's raw form value ('' = Global Default -> null).
// eslint-disable-next-line no-unused-vars -- ESLint: exported for later scripts
function parseProfileField(field, raw) {
  const text = String(raw ?? '').trim();
  if (text === '') return null;
  if (field.kind === 'number' || field.kind === 'pixel') {
    const number = Number(text);
    return Number.isFinite(number) ? number : null;
  }
  if (text === 'true') return true;
  if (text === 'false') return false;
  return text;
}

// One-line summary of what a profile changes, e.g.
// "Interval 0.3 s · 8 fps · Tiling 2 × 2 · Second Look On". Empty = Global Default.
// eslint-disable-next-line no-unused-vars -- ESLint: exported for later scripts
function profileSummary(settings) {
  const values = settings || {};
  const parts = [];
  const on = (value) => (value ? 'On' : 'Off');
  const add = (key, format) => {
    if (values[key] != null && values[key] !== '') parts.push(format(values[key]));
  };
  add('detection_interval_seconds', (v) => `Interval ${v} s`);
  add('ingest_frame_fps', (v) => `${v} fps`);
  add('adaptive_detection_enabled', (v) => `Adaptive ${on(v)}`);
  add('detection_confirm_frames', (v) => `Confirm ${v}`);
  add('always_run_object_detection', (v) => `Always Run ${on(v)}`);
  add('object_detection_region_boost', (v) => `Region Boost ${on(v)}`);
  add('object_detection_tiling', (v) => `Tiling ${v === 'off' ? 'Off' : v.replace('x', ' × ')}`);
  add('object_detection_low_light', (v) => `Low-Light ${titleCase(v)}`);
  add('object_detection_second_look', (v) => `Second Look ${on(v)}`);
  add('motion_pixel_threshold', (v) => `Pixel Change ${v}`);
  add('motion_shadow_suppression', (v) => `Shadows ${titleCase(v)}`);
  const shown = new Set([
    'detection_interval_seconds', 'ingest_frame_fps', 'adaptive_detection_enabled', 'detection_confirm_frames',
    'always_run_object_detection', 'object_detection_region_boost', 'object_detection_tiling',
    'object_detection_low_light', 'object_detection_second_look', 'motion_pixel_threshold', 'motion_shadow_suppression',
  ]);
  const others = Object.keys(values).filter((key) => !shown.has(key) && values[key] != null).length;
  if (others) parts.push(`+${others} more`);
  return parts.join(' · ');
}
