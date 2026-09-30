// The sidebar layout: daily pages at the top, set-once configuration in
// collapsible groups, sibling pages as tabs of one section, and every old
// page still reachable.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const nav = readFileSync(path.resolve(here, '../web/nav.js'), 'utf8');
const css = readFileSync(path.resolve(here, '../web/styles.css'), 'utf8');

test('Live is the home entry, followed by Events and Recordings', () => {
  const live = nav.indexOf("{ href: '/', matches: ['/'], label: 'Live'");
  const events = nav.indexOf("{ href: '/events', matches: ['/events'], label: 'Events'");
  const recordings = nav.indexOf("{ href: '/recordings', matches: ['/recordings', '/snapshots'], label: 'Recordings'");
  assert.ok(live > 0 && live < events && events < recordings);
  assert.doesNotMatch(nav, /label: 'Dashboard'/);
});

test('configuration lives in admin-only Setup, Intelligence and Admin groups', () => {
  for (const group of ['Setup', 'Intelligence', 'Admin']) {
    assert.match(nav, new RegExp(`label: '${group}',\\s*admin: true,`));
  }
});

test('every page is either a sidebar entry or a section tab', () => {
  for (const href of ['/cameras', '/zones', '/alerts', '/detection', '/detection/sounds', '/detection/faces', '/ai',
    '/models', '/models/faces', '/models/cameras', '/models/sound', '/models/settings', '/settings', '/users', '/system', '/camera-log',
    '/application-log', '/audit', '/recordings/timeline', '/snapshots']) {
    assert.ok(nav.includes(`href: '${href}'`), `${href} is not reachable from the navigation`);
  }
});

test('section tabs render under the heading and the eyebrow becomes a breadcrumb', () => {
  assert.match(nav, /strip\.className = 'page-tabs'/);
  assert.match(nav, /hero\.after\(strip\)/);
  assert.match(nav, /eyebrow\.textContent = ownerGroup \? `\$\{ownerGroup\.label\} › \$\{ownerLink\.label\}`/);
});

test('the sidebar is fixed on desktop and a drawer on phones', () => {
  assert.match(css, /body\.has-sidebar \{ padding-left: var\(--sidebar-w\);/);
  assert.match(css, /\.app-nav\.nav-open \.app-nav-body \{ transform: none; \}/);
  assert.match(css, /body\.has-sidebar \.shell \{ width: auto; max-width: none;/);
});

test('a theme chosen in the sidebar is saved to the profile', () => {
  // Every page applies the profile's theme on load, so a choice that was
  // only applied locally reverted on the next page.
  assert.match(nav, /window\.api\('\/api\/profile', \{ method: 'PUT', body: JSON\.stringify\(\{ theme \}\) \}\)/);
  for (const id of ['navThemeSystem', 'navThemeLight', 'navThemeDark']) {
    assert.match(nav, new RegExp(`getElementById\\('${id}'\\)\\?\\.addEventListener\\('click', \\(\\) => chooseTheme\\(`));
  }
  assert.match(nav, /id="navThemeToggle"/);
  assert.match(nav, /chooseTheme\(document\.documentElement\.classList\.contains\('light'\) \? 'dark' : 'light'\)/);
});

test('every section tab shows an icon beside its text', () => {
  const tabs = [...nav.matchAll(/\{ href: '([^']+)', (icon: '[^']+', )?label: '[^']+'(, match: [^}]+)? \}/g)];
  const sectionTabs = tabs.filter((match) => nav.indexOf(match[0]) > nav.indexOf('const PAGE_TABS'));
  assert.ok(sectionTabs.length >= 15);
  for (const match of sectionTabs) assert.ok(match[2], `${match[1]} has no icon`);
  assert.match(nav, /<span class="page-tab-icon" aria-hidden="true">\$\{tab\.icon\}<\/span>/);
});
