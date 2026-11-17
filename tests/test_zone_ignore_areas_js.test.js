// The Zones editor's ignore-area helpers (web/zones.js): which spots still
// fall inside their zone, the boxes drawn on the canvas and the removable
// list under the zone row.
//
// Run with:
//   node --test tests/test_zone_ignore_areas_js.test.js

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.resolve(here, '../web/zones.js'), 'utf8');
const start = source.indexOf('// Ignore areas (added from AI Insights');
const end = source.indexOf('function renderZoneBox(');

const context = vm.createContext({
  escapeHtml: (value) => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'),
  titleCase: (value) => String(value).replace(/\b\w/g, (c) => c.toUpperCase()),
  selectedZoneIndex: 0,
});
vm.runInContext(`${source.slice(start, end)}`, context);

const zone = {
  points: [{ x: 0, y: 0 }, { x: 0.5, y: 0 }, { x: 0.5, y: 1 }, { x: 0, y: 1 }],
  ignore_areas: [
    { label: 'dog', x: 0.1, y: 0.6, width: 0.1, height: 0.2 },
    { label: 'dog', x: 0.7, y: 0.6, width: 0.1, height: 0.2 },
  ],
};
const run = (code) => vm.runInContext(code, Object.assign(context, { zone }));

test('a spot counts as inside its zone by its centre', () => {
  assert.equal(run('ignoreAreaInZone(zone, zone.ignore_areas[0])'), true);
  assert.equal(run('ignoreAreaInZone(zone, zone.ignore_areas[1])'), false);
});

test('the selected zone draws its spots, flagging one left outside', () => {
  const html = run('renderIgnoreAreaBoxes(zone, 0)');
  assert.equal((html.match(/class="zone-ignore-area/g) || []).length, 2);
  assert.match(html, /zone-ignore-area is-stale selected/);
  assert.match(html, /left:10%;top:60%;width:10%;height:20%/);
  assert.doesNotMatch(run('renderIgnoreAreaBoxes(zone, 1)'), /selected/);
});

test('each spot is listed with a remove button', () => {
  const list = run('renderIgnoreAreaList(zone, 3)');
  assert.match(list, /data-remove-ignore-area="3:0"/);
  assert.match(list, /data-remove-ignore-area="3:1"/);
  assert.match(list, /Dog · outside area/);
  assert.equal(run('renderIgnoreAreaList({ points: zone.points }, 0)'), '');
});
