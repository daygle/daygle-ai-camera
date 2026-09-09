// ESLint flat config for the Daygle AI Camera dashboard (web/) and its
// node:test suites (tests/*.test.js).
//
// The dashboard is browser-vanilla ES2021 (no bundler, no framework): scripts
// share state through `window` globals and are loaded in a fixed order per
// page. The tests run under Node's test runner in ESM. Language options are
// therefore split per-directory rather than set once globally.
//
// Style rules are kept to the recommended set of correctness-only checks (no
// formatting/pedantic rules) - ruff already gates app/ and this config is not
// trying to reformat a 15k-line codebase on introduction. HTML-string
// construction in web/ consistently pairs with the escapeHtml/safeHtml
// helpers in web/utils.js; treat any lint finding there as review feedback,
// not an automatic fix.
//
// web/ scripts are classic (non-module) scripts that share state through
// `window` globals: every page loads a fixed script order (e.g. nav.js →
// utils.js → icons.js → overlay.js → live.js → <page>.js), so page scripts
// legitimately reference helpers and element handles defined in earlier
// scripts. Single-file analysis cannot see those bindings.
//
// The cross-script bindings are therefore declared EXPLICITLY in
// WEB_SHARED_GLOBALS below (each name verified to be defined by the listed
// source script). With the manifest in place, `no-undef` is an ERROR again
// for web/ - a genuinely undefined name (typo, renamed helper) fails lint
// instead of hiding inside a warning baseline. `no-unused-vars` stays a
// warning: helpers consumed only from later scripts legitimately read as
// unused in single-file analysis.
//
// Maintenance rule: when a web/ script starts referencing a helper defined
// in an earlier script, ADD the name to WEB_SHARED_GLOBALS in the same PR.
// A missing entry surfaces immediately as a no-undef error.

import js from '@eslint/js';
import globals from 'globals';

// Cross-script (window) globals between the page scripts: every name here is
// defined by an earlier-loaded script and consumed by later ones. Entries are
// grouped by their defining file (script order per page: nav.js → utils.js →
// icons.js → overlay.js → live.js → <page>.js) so a future editor can see
// where the binding comes from. Names are unique (no-dupe-keys); a name
// defined in several files (e.g. page-local `cameras`/`selectedZoneIndex`
// state) gets ONE entry from its canonical home. When a new cross-script
// binding is introduced, add it here in the same PR; a missing entry fails
// lint as a no-undef error.
const WEB_SHARED_GLOBALS = {
  // web/nav.js - auth bootstrap shared with every page.
  setApiAuth: 'readonly',

  // web/utils.js - shared foundation loaded on every page (API helper,
  // formatters, detection-pill/summary builders, auth/CSRF plumbing, DOM
  // utilities).
  api: 'readonly',
  cameraLabel: 'readonly',
  collectRecordingFaceIdentities: 'readonly',
  continuousPill: 'readonly',
  daygleSinceParamForRange: 'readonly',
  DETECTION_CONTINUOUS_ICON: 'readonly',
  DETECTION_EYE_ICON: 'readonly',
  DETECTION_MOTION_ICON: 'readonly',
  detectionPill: 'readonly',
  escapeHtml: 'readonly',
  eventFaceIdentities: 'readonly',
  faceIdentityPills: 'readonly',
  formatDate: 'readonly',
  formatDateTime: 'readonly',
  formatLogTime: 'readonly',
  formatUserClock: 'readonly',
  formatUserDate: 'readonly',
  GENERIC_TRIGGER_LABELS: 'readonly',
  initDaygleTabs: 'readonly',
  isContinuousOnlyRecording: 'readonly',
  isMotionOnlyEvent: 'readonly',
  isMotionOnlyEventItem: 'readonly',
  isMotionOnlyRecording: 'readonly',
  isSoundLabel: 'readonly',
  isSoundRecording: 'readonly',
  LIVE_AI_TRACK_KEY: 'readonly',
  LOG_PAGE_SIZE: 'readonly',
  markSupersededMotionRows: 'readonly',
  matchesFaceFilter: 'readonly',
  motionConfidenceFor: 'readonly',
  motionPill: 'readonly',
  normalizeEmailList: 'readonly',
  recordingDetectionSummary: 'readonly',
  recordingHasMotion: 'readonly',
  recordingTriggerLabel: 'readonly',
  recordingTriggerType: 'readonly',
  recordingZoneNames: 'readonly',
  renderRuleExpandFields: 'readonly',
  renderTimeSelect: 'readonly',
  requireElements: 'readonly',
  safeHtml: 'readonly',
  setTimeSelectValue: 'readonly',
  showToast: 'readonly',
  stillAlertBadge: 'readonly',
  timeAgo: 'readonly',
  timeSelectValue: 'readonly',
  titleCase: 'readonly',
  RECORDINGS_OVERLAY_TOGGLE_KEY: 'readonly',
  TIMELINE_OVERLAY_TOGGLE_KEY: 'readonly',

  // web/icons.js - SVG icon registry consumed across pages.
  ICONS: 'readonly',

  // web/overlay.js - canvas overlay helpers (recordings/timeline playback).
  drawDetectionBoxesOnCanvas: 'readonly',
  projectDetections: 'readonly',
  resizeOverlayCanvas: 'readonly',
  sampleTrackAtTime: 'readonly',// web/live.js - live-view page frame: shared element handles and page
// state; page scripts loaded after live.js reference these. The four mutable
// page-state names are 'writable' (declared with `let` in their defining
// script and reassigned by consumer scripts).
  CLOSE_DRAFT_DISTANCE_PX: 'readonly',
  cameraDetection: 'readonly',
  clamp: 'readonly',
  liveEls: 'readonly',
  normalizeLabelList: 'readonly',
  normalizePoint: 'readonly',
  refreshDetectionStatus: 'readonly',
  refreshFrame: 'readonly',
  roundCoord: 'readonly',
  selectedCamera: 'writable',
  selectedZoneIndex: 'writable',
  setSelectedCamera: 'readonly',
  availableLabels: 'writable',

  // web/cameras.js - camera list state referenced by later page scripts.
  cameras: 'writable',

  // web/zones.js - zone-editor render entry points invoked from live.js
  // hooks on the zones page.
  bindZoneDrawing: 'readonly',
  renderZones: 'readonly',
  syncZoneOverlayToImage: 'readonly',
  updateZonesStats: 'readonly',
};

export default [
  {
    ignores: ['node_modules/', 'coverage/', 'data/', 'models/', '.venv/', 'app/'],
  },
  js.configs.recommended,
  {
    files: ['web/**/*.js'],
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: 'script',
      globals: {
        ...globals.browser,
        ...WEB_SHARED_GLOBALS,
      },
    },
    rules: {
      // Helpers consumed only from later scripts read as unused in
      // single-file analysis; still surfaced for eventual module conversion.
      // no-undef is intentionally NOT relaxed: WEB_SHARED_GLOBALS above
      // covers the cross-script bindings, so a genuine typo now errors.
      'no-unused-vars': 'warn',
      // WEB_SHARED_GLOBALS names are declared with top-level const/let/function
      // in their defining scripts (that lexical binding IS the cross-script
      // sharing mechanism), so this rule fires a false positive in every
      // defining file. Its protection is redundant for classic scripts: a
      // duplicate lexical declaration is a SyntaxError at parse/load time
      // (same file) or when the second script loads (cross-file).
      'no-redeclare': 'off',
    },
  },
  {
    files: ['tests/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.node,
      },
    },
  },
];
