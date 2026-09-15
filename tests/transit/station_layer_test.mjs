import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('docs/transit-layer.js', 'utf8');
const raf = [];
const fetches = [];
const badgeSizes = [];
const pathValues = [];

function extend(props) {
  const Parent = this;
  function Child(options) {
    if (this.initialize) this.initialize(options);
  }
  Child.prototype = Object.create(Parent.prototype || {});
  Object.assign(Child.prototype, props);
  Child.extend = extend;
  return Child;
}

function Layer() {}
Layer.prototype.fire = function (name, detail) {
  (this._fired ||= []).push({ name, detail });
};
Layer.extend = extend;

function Canvas() {}
Canvas.prototype._update = function () {};
Canvas.prototype.addTo = function () { return this; };
Canvas.prototype.remove = function () {};
Canvas.extend = extend;

const L = {
  Layer,
  Canvas,
  Browser: { retina: true },
  Util: { setOptions(obj, opts) { obj.options = { ...obj.options, ...(opts || {}) }; } },
  layerGroup() {
    const layers = [];
    return {
      layers,
      addTo() { return this; },
      addLayer(layer) { layers.push(layer); },
      removeLayer(layer) { const i = layers.indexOf(layer); if (i >= 0) layers.splice(i, 1); },
      clearLayers() { layers.length = 0; },
      remove() { layers.length = 0; },
    };
  },
  DomUtil: { setPosition(node, point) { node._point = point; } },
  tooltip() { return { remove() {}, setContent() {}, setLatLng() {}, addTo() {} }; },
  polyline() { throw new Error('station-only mode must not create polylines'); },
};

const bounds = {
  pad() { return this; },
  getWest() { return 138; }, getEast() { return 141; },
  getSouth() { return 34; }, getNorth() { return 37; },
};
const map = {
  zoom: 11,
  on() {}, off() {},
  panes: {},
  getPane(name) { return this.panes[name]; },
  createPane(name) { return (this.panes[name] = { style: {}, appendChild(node) { node.parentNode = this; this.node = node; } }); },
  getZoom() { return this.zoom; },
  getBounds() { return bounds; },
  getSize() { return { x: 400, y: 300 }; },
  containerPointToLayerPoint(p) { return { x: p[0], y: p[1] }; },
  latLngToContainerPoint(ll) { return { x: (ll[1] - 138) * 100, y: (37 - ll[0]) * 100 }; },
  latLngToLayerPoint(ll) { return this.latLngToContainerPoint(ll); },
};

const fakeCtx = {
  beginPath() {}, moveTo() {}, lineTo() {}, quadraticCurveTo() {}, closePath() {},
  fill() {}, stroke() {}, save() {}, restore() {}, translate() {}, scale() {}, arc() {},
  clearRect() {}, fillText() {}, measureText(text) { return { width: text.length * 6 }; },
};

const context = {
  L,
  Map,
  Set,
  URL,
  console,
  navigator: {},
  localStorage: { getItem() { return null; } },
  window: {
    location: { href: 'https://example.test/' },
    matchMedia() { return { matches: false }; },
  },
  matchMedia() { return { matches: false }; },
  document: {
    head: { appendChild() {} },
    getElementById() { return null; },
    createElement(name) {
      if (name === 'canvas') return { style: {}, classList: { add() {} }, getContext() { return fakeCtx; } };
      return { style: {}, classList: { add() {} }, textContent: '' };
    },
  },
  Path2D: class Path2D { constructor(path) { pathValues.push(path); } },
  AbortController,
  setTimeout,
  clearTimeout,
  requestAnimationFrame(fn) { raf.push(fn); return raf.length; },
  cancelAnimationFrame() {},
  fetch(url) {
    fetches.push(url);
    return Promise.resolve({ ok: true, json: async () => ({ v: 1, stations: [] }) });
  },
};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(source, context);

const layer = context.L.transitLayer({
  lodUrls: { low: '/rail-low', mid: '/rail-mid', high: '/rail-high' },
  lodBreaks: { mid: 9, high: 14 },
  stationUrl: '/stations',
});
layer.setVisibleBuckets({ long: false, city: false });
layer.setStationsVisible(true);
layer.onAdd(map);
while (raf.length) raf.shift()();
assert.deepEqual(fetches, [], 'below stationMinZoom must not fetch any payload');

map.zoom = 12;
layer._scheduleRedraw();
while (raf.length) raf.shift()();
await layer._stationInflight;
assert.deepEqual(fetches, ['/stations'], 'station-only mode must fetch only stationUrl');
assert.equal(layer._rLines, undefined, 'station-only mode must not allocate the rail canvas');
assert.equal(layer._loaded, false, 'station-only mode must not parse a rail LOD');

layer._allStations = layer._parseStationPayload({ v: 1, stations: [
  [139, 35, 'A', '', 'station', 2],
  [139.1, 35.1, 'B', '', 'station', 3],
  [139.2, 35.2, 'C', '', 'station', 6],
] });
layer._indexStations(layer._allStations);
layer._stationLoaded = true;
map.zoom = 13;
const drawBadge = layer._drawStationBadge.bind(layer);
layer._drawStationBadge = function(ctx, x, y, size) { badgeSizes.push(size); return drawBadge(ctx, x, y, size); };
layer._redraw();
assert.deepEqual(badgeSizes, [14, 18, 22]);
assert.ok(pathValues.includes('M5 18.5v-8C5 6.2 8 3.8 12 3.8s7 2.4 7 6.7v8'),
  'canvas must use the selected tunnel/train vector');
assert.equal(map.panes.transitStations.style.zIndex, '450');
assert.equal(layer._stationCanvas.parentNode, map.panes.transitStations,
  'station canvas must stay below the restaurant marker pane');

const stationRecords = layer._allStations;
const stationIndex = layer._stationIndex;
const oldMixedStations = [];
layer._parseInto({ features: [{
  geometry: { type: 'Point', coordinates: [139.7, 35.6] },
  properties: { name: 'legacy mixed point' },
}] }, [], oldMixedStations, new Map());
assert.equal(oldMixedStations.length, 0,
  'a configured stationUrl must ignore Point rows in old mixed LODs');
layer._lodCache = {
  mid: { lines: [], stations: [{ name: 'legacy point must be ignored' }], lineIndex: new Map() },
};
layer._activateLod('mid');
assert.equal(layer._allStations, stationRecords,
  'activating an old mixed R2 LOD must retain station-only records');
assert.equal(layer._stationIndex, stationIndex,
  'activating an old mixed R2 LOD must retain the station-only index');

layer._lodCache = { high: { large: true } };
layer._allLines = [{ fake: true }];
layer._lineIndex.set('x', [{}]);
layer._loaded = true;
layer.setVisibleBuckets({ long: false, city: false });
assert.equal(layer._allLines.length, 0);
assert.equal(layer._lineIndex.size, 0);
assert.equal(Object.keys(layer._lodCache).length, 0);
assert.equal(layer._allStations.length, 3, 'releasing rail must retain station data');
assert.equal(layer._stationsOn.size, 3, 'releasing rail must retain viewport markers');

console.log('station layer contract: ok');
