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
// scripts. Single-file analysis cannot see those bindings, which turns
// no-undef into ~920 false positives and no-unused-vars into ~50 (helpers
// used only from later scripts). Turning both into warnings for web/ keeps
// the genuinely destructive recommended rules (no-dupe-keys, no-unreachable,
// valid-typeof, ...) as errors while the real signal in web/ stays in the
// test suites. tests/ is self-contained ESM, so both stay as errors there.

import js from '@eslint/js';
import globals from 'globals';

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
      },
    },
    rules: {
      // Cross-script (window) globals between the page scripts; see the
      // block comment above.
      'no-undef': 'warn',
      'no-unused-vars': 'warn',
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
