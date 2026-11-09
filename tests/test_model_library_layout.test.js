// Regression tests for the Object/Face Models library layout.
//
// The models API returns one row per installed resolution PLUS a "-new"
// download row for every installed model, all sharing the catalog id. The
// page used to render those rows 1:1, so an installed model appeared two or
// three times, scattered through the catalog. The page now groups rows by
// model: one card per installed model (its sizes inside it) and a catalog of
// the models that are not installed.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.resolve(here, '../web', name), 'utf8');
const source = read('models.js');
const arcfaceSource = read('arcface.js');

// Pull groupModels (and the key map it fills) out of the page script and run it.
function loadGroupModels() {
  const start = source.indexOf('const cardIdByKey = {};');
  const end = source.indexOf('const RESOLUTION_OPTIONS');
  assert.ok(start !== -1 && end > start, 'groupModels block not found');
  return new Function(`${source.slice(start, end)}; return { groupModels, modelCardId };`)();
}

const row = (id, variantId, extra = {}) => ({
  id, variant_id: variantId, label: `${id} label`, installed: false, active: false, ...extra,
});

test('an installed model is one group, whatever its number of sizes', () => {
  const { groupModels, modelCardId } = loadGroupModels();
  const groups = groupModels([
    row('yolo26s', 'yolo26s-new'),
    row('yolo26s', 'yolo26s-768', { installed: true, exported_imgsz: 768, label: 'yolo26s label · 768×768' }),
    row('yolo26s', 'yolo26s-1024', { installed: true, exported_imgsz: 1024, label: 'yolo26s label · 1024×1024' }),
    row('yolo26n', 'yolo26n'),
  ]);
  assert.equal(groups.length, 2);
  const [installed, catalog] = groups;
  assert.equal(installed.variants.length, 2);
  assert.equal(installed.label, 'yolo26s label');
  assert.equal(installed.downloadKey, 'yolo26s-new');
  assert.equal(catalog.variants.length, 0);
  // Every variant key resolves to its model's single card.
  for (const key of ['yolo26s-new', 'yolo26s-768', 'yolo26s-1024']) assert.equal(modelCardId(key), 'yolo26s');
});

test('the library renders installed cards and a catalog, not one card per row', () => {
  const block = source.slice(source.indexOf('function renderLibrary'), source.indexOf('const cardIdByKey'));
  assert.match(block, /groupModels\(familyModels\)/);
  assert.match(block, /renderInstalledCard/);
  assert.match(block, /renderCatalog\(catalogGroups\)/);
  // Counts are models, not rendered rows.
  assert.match(block, /\$\{installedGroups\.length\} installed · \$\{groups\.length\} available/);
  assert.doesNotMatch(source, /function renderCard\(/);
});

test('"add another size" cannot re-export a size that is already installed', () => {
  const block = source.slice(source.indexOf('function downloadControlsHtml'), source.indexOf('function cardMessageHtml'));
  assert.match(block, /taken\.has\(size\) \? ' disabled' : ''/);
});

test('status and update controls live inside the one library card', () => {
  const html = read('models.html');
  const card = html.slice(html.indexOf('id="objectModelsCard"'), html.indexOf('</section>', html.indexOf('id="objectModelsCard"')));
  for (const id of ['objectStatusPanel', 'checkObjectModelUpdatesBtn', 'objectModelList', 'firstRunNotice']) {
    assert.match(card, new RegExp(`id="${id}"`), `${id} is outside the library card`);
  }
  assert.doesNotMatch(html, /model-library-toolbar"/);
  const faces = read('arcface.html');
  assert.equal((faces.match(/class="card model-library-card/g) || []).length, 2);
});

test('face recognition names the active model by its label', () => {
  assert.match(arcfaceSource, /activeModel\?\.label \|\| fileName/);
});
