/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/map.js
   Owner: the map module (this file + css/map.css).

   Ported from ui-captures-2026-09-10/redesign/demo-4.0/js/map.js onto the
   production seams (PORT-PLAN §6 / §9):
     · the Leaflet map is FOLIUM'S — adopted from ctx.leafletMap, never built
       here. Its tile layer carries the CARTO key, the {r} @2x placeholder
       (TILE_DPR_SWITCH_JS) and the crossOrigin CORS fallback
       (TILE_CORS_FALLBACK_JS); this module only removes / re-adds it for the
       offline grid and never rewrites its URL.
     · the rail overlay is docs/transit-layer.js (L.transitLayer) against the
       three R2 LODs in Data.config.TRANSIT — the demo's own GeoJSON slice and
       its re-implementation of the painter are NOT carried into production.
     · locate is the real leaflet.locatecontrol plugin (LOCATE_ASSETS), driven
       by the FAB only — nothing here ever asks for the permission on load.
     · every write goes through App.act.*; Data.* is the only reader.

   Public API (CONTRACT §5 map, plus the test hooks the checks use):
     MapMod.map / bounds() / MV(ids) / visibleIds() / setResultIds(ids|null)
     MapMod.reveal(id, {reason}) / select(id, {reveal}) / flyTo(ll, z) / fitPlace(p)
     MapMod.setTilesOffline(on) / placeTempPin(pin|null) / setTempPin(...)
     MapMod.setRail(kind, on) / setLayers(patch) / locate()
     MapMod.railStatus() / railDetail() / tileStatus() / markerStats()
   Events emitted: map:moveend, map:mv, map:contextmenu, map:marker-click,
     map:tiles, map:locate-request, map:locate, layers:load
   ========================================================================== */
(function () {
  'use strict';

  var M = window.MapMod = {};

  var ctx, App, Data, util, t, icon, emoji, motion, layoutApi, act;
  var map = null, mapRoot = null, fabEl = null;

  /* tiles (folium's layer — we only detach / reattach it) */
  var tileLayer = null, offline = false, offlineNote = null;
  var tileErrors = 0, tileOk = false;

  /* markers */
  var cluster = null, markers = {}, shown = {}, resultOverride = null;
  var landmarkLayer = null, pinLayer = null, tempLayer = null;
  // MAP-02 §3: the selected restaurant leaves the cluster and lives in its own
  // pane, so its ring, its price tag and its click target survive any density.
  var selLayer = null, selId = null;

  /* rail (docs/transit-layer.js) */
  var rail = null, railBuckets = { long: false, city: false }, railStatus = { long: 'idle', city: 'idle' };
  var railLoading = false;

  /* bubble */
  var bubbleEl = null, bubbleAnchor = null, bubbleFor = null, bubblePhoto = {};

  /* movement bookkeeping */
  var programDepth = 0, moveRaf = 0, lastMV = -1, lastSig = '', lastMapRectSig = '';
  var lastRenderedIds = null, lastSelected = null, lastFavSig = '', lastLang = '';

  var farScale = null;
  var CLUSTER_S = 32, CLUSTER_M = 40, CLUSTER_L = 44;
  var LONGPRESS_MS = 700;
  var BUBBLE_MIN_MAP_H = 210;         // §6.3 / LAY-04: no name bubble in a short map strip
  var BUBBLE_HALF_W = 150;            // half the widest name bubble, for reveal keep-out
  var BUBBLE_KEEPOUT_TOP = 108;       // bubble height + its offset above the marker

  /* ======================================================================
     init — adopt folium's map, never create one
     ================================================================== */
  M.init = function (c) {
    ctx = c; App = c.App; Data = c.Data; util = c.util; t = c.t; icon = c.icon;
    emoji = c.emoji; motion = c.motion; layoutApi = c.layout; act = c.act;
    mapRoot = c.roots.mapRoot || document.getElementById('map-root');
    fabEl = c.roots.fabRoot || document.getElementById('fab-root');

    map = M.map = c.leafletMap || null;
    if (!map) { console.warn('[map] no Leaflet map to adopt'); return; }

    var s = App.state;
    offline = !!(s.notices && s.notices.offline);

    // The cold-start view belongs to the page, not to this module: folium sets
    // the national view and VIEW_RESTORE_SNIPPET replays tabelog.mapView before
    // Leaflet initialises. Adopt whatever is on screen into state so the first
    // render does not yank the map to core's default centre (and so the
    // map-vs-state race at boot has only one possible winner).
    try {
      var c0 = map.getCenter();
      App.set({ mapView: { center: [c0.lat, c0.lng], zoom: map.getZoom() } }, { silent: true });
    } catch (_) {}

    // folium builds the TileLayer (key + {r} + crossOrigin). Hold the handle so
    // the offline grid can detach it; never touch its URL or options.
    findTileLayer();
    if (tileLayer) {
      tileLayer.on('tileerror', function () {
        tileErrors += 1;
        if (tileErrors === 6 && navigator.onLine !== false) App.emit('map:tiles', { status: 'error' });
      });
      tileLayer.on('tileload', function () {
        tileErrors = 0;
        if (!tileOk) { tileOk = true; App.emit('map:tiles', { status: 'ok' }); }
      });
    }

    // attribution + scale stay inside the VISIBLE map rect — css/map.css moves
    // the corner by --col-left-w / --sheet-h; here we only normalise them.
    try {
      if (map.attributionControl) {
        map.attributionControl.setPrefix('');
        map.attributionControl.setPosition('bottomleft');
      }
      if (L.control && L.control.scale && !M._scale) {
        M._scale = L.control.scale({ position: 'bottomleft', imperial: false, maxWidth: 120 }).addTo(map);
      }
    } catch (_) {}

    // panes: pins / landmarks above the restaurant cluster. The rail overlay
    // brings its own canvas in the default overlayPane (below markerPane).
    ensurePane('mp-pins', 610);
    ensurePane('mp-landmarks', 620);
    ensurePane('mp-sel', 630);

    cluster = L.markerClusterGroup({
      maxClusterRadius: 40,
      // MAP-02 (2026-09-12 revision): disableClusteringAtZoom is GONE. It
      // predates the price tags — past z17 two restaurants in the same
      // building simply drew on top of each other, the lower one was
      // unreachable and the collision pass then dropped one of the two tags.
      // Clustering at every zoom + spiderfy is the documented replacement.
      showCoverageOnHover: false,
      spiderfyOnMaxZoom: true,
      zoomToBoundsOnClick: true,
      animate: !motion.reduced,
      chunkedLoading: true,
      iconCreateFunction: clusterIcon
    });
    selLayer = L.layerGroup([], { pane: 'mp-sel' });
    landmarkLayer = L.layerGroup([], { pane: 'mp-landmarks' });
    pinLayer = L.layerGroup([], { pane: 'mp-pins' });
    tempLayer = L.layerGroup([], { pane: 'mp-pins' });
    map.addLayer(cluster); map.addLayer(selLayer); map.addLayer(landmarkLayer); map.addLayer(pinLayer); map.addLayer(tempLayer);

    // MarkerCluster mounts markers in chunks and animates cluster splits, so the
    // tag pass has to run again once it has settled or the last batch keeps
    // whatever show/hide state the previous frame gave it.
    cluster.on('animationend spiderfied unspiderfied', scheduleTagLayout);

    bindMap();
    bindFab();
    if (offline) M.setTilesOffline(true);
  };

  function ensurePane(name, z) {
    try {
      var p = map.getPane(name) || map.createPane(name);
      p.style.zIndex = z;
      return p;
    } catch (_) { return null; }
  }

  function findTileLayer() {
    if (tileLayer) return tileLayer;
    try {
      map.eachLayer(function (l) { if (!tileLayer && L.TileLayer && l instanceof L.TileLayer && l._url) tileLayer = l; });
    } catch (_) {}
    return tileLayer;
  }

  /* ======================================================================
     tiles / offline (LAY-04: the credit stays even with no tiles)
     ================================================================== */
  M.setTilesOffline = function (on) {
    on = !!on;
    offline = on;
    if (!mapRoot) return;
    mapRoot.classList.toggle('is-offline', offline);
    if (offline) {
      if (tileLayer && map.hasLayer(tileLayer)) map.removeLayer(tileLayer);
      if (!offlineNote) {
        offlineNote = document.createElement('div');
        offlineNote.className = 'mp-offline-note';
        offlineNote.setAttribute('role', 'status');
        mapRoot.appendChild(offlineNote);
      }
      offlineNote.innerHTML = '<span class="mp-offline-title">' + util.esc(t('底图暂不可用')) + '</span>' +
        '<span class="mp-offline-sub">' + util.esc(t('标记与列表仍可用')) + '</span>';
      App.emit('map:tiles', { status: 'offline' });
    } else {
      if (offlineNote) { offlineNote.remove(); offlineNote = null; }
      if (tileLayer && !map.hasLayer(tileLayer)) { map.addLayer(tileLayer); try { tileLayer.redraw(); } catch (_) {} }
      App.emit('map:tiles', { status: 'ok' });
    }
  };

  M.tileStatus = function () {
    return { source: 'carto', offline: offline, errors: tileErrors, attached: !!(tileLayer && map && map.hasLayer(tileLayer)) };
  };

  /* ======================================================================
     restaurant markers
     ================================================================== */
  function priceLabel(r) {
    return r.bucket === 'na' ? t('未知') : util.fmtPrice(r.bucket, { short: true });
  }

  // Match Leaflet's 120 CSS-pixel metric scale, including its 1/2/3/5 rounding.
  function scaleMetres() {
    var y = map.getSize().y / 2;
    var metres = map.distance(map.containerPointToLatLng([0, y]), map.containerPointToLatLng([120, y]));
    var pow = Math.pow(10, Math.floor(Math.log(metres) / Math.LN10));
    var d = metres / pow;
    return pow * (d >= 10 ? 10 : d >= 5 ? 5 : d >= 3 ? 3 : d >= 2 ? 2 : 1);
  }
  M.scaleMetres = scaleMetres;

  function markerHtml(r, selected, fav) {
    var label = priceLabel(r), far = scaleMetres() >= 500 && !selected;
    return '<div class="mp-mk' + (far ? ' is-far' : '') + (selected ? ' is-selected' : '') + (fav ? ' is-fav' : '') + '" role="button" aria-label="' +
      util.esc(r.name + ' · ' + label) + '">' +
      '<span class="mp-dot">' + emoji.img(Data.genreEmoji(r), far ? 16 : 22) + '</span>' +
      '<span class="mp-tag price-tag price-' + r.bucket + ' num">' + util.esc(label) + '</span>' +
      '</div>';
  }

  function makeIcon(r, selected, fav) {
    return L.divIcon({ html: markerHtml(r, selected, fav), className: 'mp-mk-wrap', iconSize: [0, 0], iconAnchor: [0, 0] });
  }

  /** Markers are minted on first use — 10,250 rows is a corpus, not a viewport. */
  function getMarker(id) {
    var mk = markers[id];
    if (mk) return mk;
    var r = Data.byId(id);
    if (!r || typeof r.lat !== 'number' || typeof r.lon !== 'number') return null;
    mk = L.marker([r.lat, r.lon], {
      icon: makeIcon(r, false, false), keyboard: true, title: r.name, alt: r.name, riseOnHover: true
    });
    mk._rid = id;
    mk.on('click', function () {
      App.emit('map:marker-click', { id: id, kind: 'restaurant' });
      M.select(id, { reveal: false });
    });
    mk.on('keypress', function (e) {
      if (e.originalEvent && (e.originalEvent.key === 'Enter' || e.originalEvent.key === ' ')) M.select(id, { reveal: false });
    });
    markers[id] = mk;
    return mk;
  }

  function clusterIcon(cl) {
    var n = cl.getChildCount();
    var sz = n < 10 ? CLUSTER_S : (n < 50 ? CLUSTER_M : CLUSTER_L);
    return L.divIcon({
      html: '<span class="mp-cluster mp-cluster-' + (n < 10 ? 's' : (n < 50 ? 'm' : 'l')) + ' num" style="width:' + sz + 'px;height:' + sz + 'px">' + util.fmtCount(n) + '</span>',
      className: 'mp-cluster-wrap',
      iconSize: L.point(sz, sz)
    });
  }

  /** the restaurant ids the map should show right now (M plus the selected one, DETAIL-02). */
  function renderIds(s) {
    var ids = resultOverride || Data.applyFilters(s.filters);
    if (s.selected.id && ids.indexOf(s.selected.id) < 0 && Data.byId(s.selected.id)) ids = ids.concat([s.selected.id]);
    return ids;
  }

  function syncMarkers(s) {
    var ids = renderIds(s);
    var nextFar = scaleMetres() >= 500;
    var scaleChanged = nextFar !== farScale;
    farScale = nextFar;
    var sig = ids.join('|');
    var favSig = Array.from(s.user.fav).sort().join('|');
    var selChanged = s.selected.id !== lastSelected;
    var langChanged = s.lang !== lastLang;
    if (sig === lastSig && favSig === lastFavSig && !selChanged && !langChanged && !scaleChanged) return false;

    var want = {}, i;
    for (i = 0; i < ids.length; i++) want[ids[i]] = 1;
    var add = [], remove = [];
    Object.keys(shown).forEach(function (id) {
      if (want[id]) return;
      var mk = markers[id];
      if (mk) {
        if (cluster.hasLayer(mk)) remove.push(mk);
        if (selLayer.hasLayer(mk)) selLayer.removeLayer(mk);
      }
      delete shown[id];
    });
    for (i = 0; i < ids.length; i++) {
      if (shown[ids[i]]) continue;
      var mk2 = getMarker(ids[i]);
      if (!mk2) continue;
      shown[ids[i]] = 1;
      if (ids[i] === selId) selLayer.addLayer(mk2); else add.push(mk2);
    }
    if (remove.length) cluster.removeLayers(remove);
    if (add.length) cluster.addLayers(add);
    setSelectedMarker(s.selected.id);

    // icons: only the ones whose visual state actually changed
    for (i = 0; i < ids.length; i++) {
      var id = ids[i], r = Data.byId(id); if (!r) continue;
      var mk3 = markers[id]; if (!mk3) continue;
      var sel = id === s.selected.id, fav = s.user.fav.has(id);
      var key = (sel ? 's' : '') + (fav ? 'f' : '') + s.lang + farScale;
      if (mk3._vis === key) continue;
      mk3._vis = key;
      mk3.setIcon(makeIcon(r, sel, fav));
      mk3.setZIndexOffset(sel ? 1200 : (fav ? 200 : 0));
    }
    lastSig = sig; lastFavSig = favSig; lastSelected = s.selected.id; lastLang = s.lang; lastRenderedIds = ids;
    return true;
  }

  /** The selected marker is pulled out of the cluster group into `selLayer`
   *  (pane mp-sel) so density can never swallow the current selection, and put
   *  back the moment the selection moves on. */
  function setSelectedMarker(id) {
    if (!selLayer) return;
    id = (id && shown[id]) ? id : null;
    if (selId === id) return;
    if (selId) {
      var old = markers[selId];
      if (old) {
        if (selLayer.hasLayer(old)) selLayer.removeLayer(old);
        if (shown[selId] && !cluster.hasLayer(old)) cluster.addLayer(old);
      }
    }
    selId = id;
    if (selId) {
      var mk = getMarker(selId);
      if (mk) {
        if (cluster.hasLayer(mk)) cluster.removeLayer(mk);
        if (!selLayer.hasLayer(mk)) selLayer.addLayer(mk);
      }
    }
  }

  M.markerStats = function () {
    var n = 0; Object.keys(shown).forEach(function () { n++; });
    return { minted: Object.keys(markers).length, inCluster: n, rendered: lastRenderedIds ? lastRenderedIds.length : 0 };
  };

  /* ----- price-tag collision (MAP-02 §2 / §5) -------------------------- */
  var _tagProbe = null, _tagW = {};
  function tagWidth(label) {
    var fs = App.state.fontScale;
    var k = fs + '|' + label;
    if (_tagW[k] !== undefined) return _tagW[k];
    if (!_tagProbe) {
      _tagProbe = document.createElement('span');
      _tagProbe.className = 'mp-tag price-tag price-na num mp-probe';
      mapRoot.appendChild(_tagProbe);
    }
    _tagProbe.textContent = label;
    var w = _tagProbe.getBoundingClientRect().width || (label.length * 8 + 14);
    _tagW[k] = w;
    return w;
  }

  function overlaps(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  /**
   * layoutTags() — a price tag participates in collision with its whole text
   * box (MAP-02 §2). Priority: selected → saved → best award → rating. Before
   * a tag is dropped it is nudged sideways (MAP-02 §3, 2026-09-12 revision);
   * the emoji itself always stays and clustering handles real crowding.
   */
  function layoutTags() {
    if (!map || !lastRenderedIds) return;
    var s = App.state;
    var obstacles = [], items = [], seen = {};
    var vb = map.getBounds().pad(0.3);
    for (var n = 0; n < lastRenderedIds.length; n++) {
      var id = lastRenderedIds[n];
      var r = Data.byId(id); if (!r) continue;
      if (r.lat < vb.getSouth() || r.lat > vb.getNorth() || r.lon < vb.getWest() || r.lon > vb.getEast()) continue;
      var mk = markers[id]; if (!mk || !shown[id]) continue;
      // the selected marker sits outside the cluster group — it is its own parent
      var vp = cluster.hasLayer(mk) ? cluster.getVisibleParent(mk) : mk;
      if (!vp) continue;
      if (vp !== mk) {
        var cid = L.Util.stamp(vp);
        if (seen[cid]) continue;
        seen[cid] = 1;
        var cn = vp.getChildCount ? vp.getChildCount() : 1;
        var csz = cn < 10 ? CLUSTER_S : (cn < 50 ? CLUSTER_M : CLUSTER_L);
        var cp = map.latLngToContainerPoint(vp.getLatLng());
        obstacles.push({ x: cp.x - csz / 2, y: cp.y - csz / 2, w: csz, h: csz });
        continue;
      }
      var p = map.latLngToContainerPoint(mk.getLatLng());
      var far = farScale && id !== s.selected.id;
      var radius = far ? 10 : 14;
      obstacles.push({ x: p.x - radius, y: p.y - radius, w: radius * 2, h: radius * 2 });
      var w = far ? 6 : tagWidth(priceLabel(r));
      items.push({
        mk: mk, id: id, far: far,
        rect: { x: p.x - w / 2, y: p.y + (far ? 12 : 16), w: w, h: far ? 6 : 22 },
        pri: (id === s.selected.id ? 0 : (s.user.fav.has(id) ? 1 : 2)),
        award: Data.bestAwardRank(r),
        rating: r.rating === null || r.rating === undefined ? -1 : r.rating
      });
    }
    items.sort(function (a, b) { return a.pri - b.pri || a.award - b.award || b.rating - a.rating; });
    var placed = [];
    var OFFSETS = [0, -20, 20, -38, 38];
    function fits(rect) {
      var i;
      for (i = 0; i < obstacles.length; i++) if (overlaps(rect, obstacles[i])) return false;
      for (i = 0; i < placed.length; i++) if (overlaps(rect, placed[i])) return false;
      return true;
    }
    var painted = 0, hidden = 0, nudged = 0;
    items.forEach(function (it) {
      var dx = null, k;
      for (k = 0; k < (it.far ? 1 : OFFSETS.length) && dx === null; k++) {
        var cand = { x: it.rect.x + OFFSETS[k], y: it.rect.y, w: it.rect.w, h: it.rect.h };
        if (fits(cand)) { dx = OFFSETS[k]; placed.push(cand); }
      }
      if (dx === null && it.pri === 0) { dx = 0; placed.push(it.rect); }   // selected always keeps its tag
      var el = it.mk.getElement();
      var inner = el && el.firstElementChild;
      if (!inner) return;                       // not in the DOM yet — nothing to paint
      if (dx === null) hidden++; else { painted++; if (dx !== 0) nudged++; }
      inner.classList.toggle('is-tagless', dx === null);
      inner.style.setProperty('--mp-tag-dx', (dx || 0) + 'px');
    });
    // `painted` / `hidden` count the markers that actually have an element on
    // screen; `candidates` also holds the ones MarkerCluster has not mounted.
    M._lastTagRun = { candidates: items.length, painted: painted, nudged: nudged, hidden: hidden, zoom: map.getZoom() };
  }
  M.tagStats = function () { return M._lastTagRun || null; };

  var tagRaf = 0;
  function scheduleTagLayout() {
    if (tagRaf) return;
    tagRaf = util.raf(function () { tagRaf = 0; layoutTags(); });
  }

  /* ----- landmarks / pins / temp --------------------------------------- */
  function plateHtml(cls, char, size, name, lang) {
    return '<div class="' + cls + '">' + emoji.img(char, size) +
      (name ? '<span class="mp-plate"' + (lang ? ' lang="' + lang + '"' : '') + '>' + util.esc(name) + '</span>' : '') + '</div>';
  }

  function renderLandmarks(s) {
    if (!landmarkLayer) return;
    landmarkLayer.clearLayers();
    plateMarks = [];
    if (!s.layers.landmarks) { schedulePlateLayout(); return; }
    var hiddenIds = Data.hiddenLandmarkIds();
    Data.visibleLandmarks(s.user, s.layers).forEach(function (lm) {
      if (typeof lm.lat !== 'number' || typeof lm.lon !== 'number') return;
      var name = Data.landmarkName(lm, s.lang);
      var hidden = hiddenIds.has(lm.id);
      // .bm-mk-attraction / .bm-mk-hidden are the 3.2.x class names the compat
      // suite looks for; the 4.0 look comes from .mp-lm.
      var cls = 'mp-lm bm-mk-attraction' + (hidden ? ' is-hidden bm-mk-hidden' : '');
      var mk = L.marker([lm.lat, lm.lon], {
        pane: 'mp-landmarks', keyboard: false, title: name, alt: name,
        icon: L.divIcon({ html: plateHtml(cls, lm.emoji || '📍', 26, name, Data.placeNameLanguage(lm, name)), className: 'mp-mk-wrap', iconSize: [0, 0], iconAnchor: [0, 0] })
      }).on('click', function (e) { openPlaceMarker(e, lm, 'landmark'); }).addTo(landmarkLayer);
      plateMarks.push({ mk: mk, lat: lm.lat, lon: lm.lon, pri: 1 });
    });
    schedulePlateLayout();
  }

  function renderPins(s) {
    if (!pinLayer) return;
    pinLayer.clearLayers();
    plateMarks = plateMarks.filter(function (r) { return r.pri !== 0; });
    if (!s.layers.pins) { schedulePlateLayout(); return; }
    Data.pins(s.user.bookmarks).forEach(function (b) {
      var name = Data.pinName(b, s.lang) || b.name || '';
      var mk = L.marker([b.lat, b.lon], {
        pane: 'mp-pins', keyboard: false, title: name, alt: name,
        icon: L.divIcon({ html: plateHtml('mp-pin bm-mk-bookmark', b.emoji || '📍', 24, name, Data.placeNameLanguage(b, name)), className: 'mp-mk-wrap', iconSize: [0, 0], iconAnchor: [0, 0] })
      }).on('click', function (e) { openPlaceMarker(e, b, 'pin'); }).addTo(pinLayer);
      plateMarks.push({ mk: mk, lat: b.lat, lon: b.lon, pri: 0 });
    });
    schedulePlateLayout();
  }

  /** A landmark / pin tap: announce it and open the place menu for this object.
   *  overlays owns the menu body; the payload carries the entry so it can offer
   *  编辑 / 删除 / 隐藏景点 (see coreRequests). */
  function openPlaceMarker(e, bm, kind) {
    var U = mapOrigin();
    var pt = e && e.containerPoint ? e.containerPoint : map.latLngToContainerPoint([bm.lat, bm.lon]);
    App.emit('map:marker-click', { id: bm.id, kind: kind, lat: bm.lat, lon: bm.lon, x: pt.x + U.x, y: pt.y + U.y, bm: bm });
    act.openOverlay('placeMenu', {
      lat: bm.lat, lon: bm.lon, x: pt.x + U.x, y: pt.y + U.y,
      kind: kind, bm: bm, id: bm.id,
      place: { id: bm.id, name: kind === 'landmark' ? Data.landmarkName(bm, App.state.lang) : Data.pinName(bm, App.state.lang) }
    });
  }

  /**
   * layoutPlates() — MAP-02 §5 puts landmark / pin NAMES at the bottom of the
   * label priority list, so they are the first thing density takes away. Below
   * z9 (the country view) no plate is drawn at all — 219 built-in landmarks
   * stacked their names into one white slab over Honshu; above it, a plate that
   * would cover an already-placed one is dropped. The emoji always stays, and
   * the name is still in the marker's title / the place menu.
   */
  var plateMarks = [], plateRaf = 0, PLATE_MIN_Z = 9;
  function schedulePlateLayout() {
    if (plateRaf) return;
    plateRaf = util.raf(function () { plateRaf = 0; layoutPlates(); });
  }
  function layoutPlates() {
    if (!map || !plateMarks.length) return;
    var z = map.getZoom(), placed = [], i;
    var U = mapOrigin(), rect = App.state.layout.mapRect;
    var x0 = rect.x - U.x - 60, x1 = x0 + rect.w + 120, y0 = rect.y - U.y - 60, y1 = y0 + rect.h + 120;
    var list = plateMarks.slice().sort(function (a, b) { return a.pri - b.pri; });   // pins first
    for (i = 0; i < list.length; i++) {
      var el = list[i].mk.getElement();
      var inner = el && el.firstElementChild;
      if (!inner) continue;
      var plate = inner.querySelector('.mp-plate');
      if (!plate) continue;
      if (z < PLATE_MIN_Z) { inner.classList.add('is-plateless'); continue; }
      var p = map.latLngToContainerPoint([list[i].lat, list[i].lon]);
      if (p.x < x0 || p.x > x1 || p.y < y0 || p.y > y1) { inner.classList.remove('is-plateless'); continue; }
      inner.classList.remove('is-plateless');
      var w = plate.offsetWidth || 60, h = plate.offsetHeight || 16;
      var r = { x: p.x - w / 2, y: p.y + 14, w: w, h: h };
      var hit = false;
      for (var k = 0; k < placed.length && !hit; k++) if (overlaps(r, placed[k])) hit = true;
      if (hit) inner.classList.add('is-plateless'); else placed.push(r);
    }
    M._lastPlateRun = { total: plateMarks.length, placed: placed.length, zoom: z };
  }
  M.plateStats = function () { return M._lastPlateRun || null; };

  /** placeTempPin({lat,lon,label}|null) — the search / contextmenu 📍. */
  M.placeTempPin = function (pin) {
    if (!tempLayer) return;
    tempLayer.clearLayers();
    if (!pin) return;
    L.marker([pin.lat, pin.lon], {
      pane: 'mp-pins', keyboard: false, title: pin.label || '',
      icon: L.divIcon({ html: plateHtml('mp-pin is-temp', pin.emoji || '📍', 26, pin.label || ''), className: 'mp-mk-wrap', iconSize: [0, 0], iconAnchor: [0, 0] })
    }).addTo(tempLayer);
  };
  M.setTempPin = function (p) {
    if (!p) return M.placeTempPin(null);
    if (Array.isArray(p)) return M.placeTempPin({ lat: p[0], lon: p[1] });
    return M.placeTempPin(p);
  };

  /* ----- selected name bubble ------------------------------------------ */
  function bubbleHtml(r, s) {
    var photo = bubblePhoto[r.id];
    var media = photo
      ? '<img class="mp-bubble-photo" src="' + util.esc(PhotoUrls.url(photo, 320)) + '" alt="" draggable="false" referrerpolicy="no-referrer">'
      : '<span class="mp-bubble-photo mp-bubble-photo-fallback">' + emoji.img(Data.genreEmoji(r), 22) + '</span>';
    var rating = (r.rating === null || r.rating === undefined)
      ? '<span class="rating-missing t-badge">' + util.esc(t('评分暂无')) + '</span>'
      : '<span class="rating num">' + icon('starFill', { fill: true, cls: 'ic-sm' }) + util.fmtRating(r.rating) + '</span>';
    return media + '<span class="mp-bubble-text"><span class="mp-bubble-name clamp-1" lang="ja">' + util.esc(r.name) + '</span>' +
      '<span class="mp-bubble-meta">' + rating + '<span class="mp-bubble-price price-text num">' + util.esc(priceLabel(r)) + '</span></span></span>';
  }

  /** The photo lives in the popups payload (slot 7), which is a lazy ~6 MB
   *  fetch — the same one the detail card triggers. Upgrade the bubble when it
   *  resolves; never block on it. */
  function wantPhoto(r) {
    if (bubblePhoto[r.id] !== undefined) return;
    bubblePhoto[r.id] = null;
    try {
      Data.detail(r.id).then(function (d) {
        var p = d && d.photos && d.photos[0];
        if (!p) return;
        bubblePhoto[r.id] = p;
        if (bubbleFor === r.id && bubbleEl) {
          bubbleEl.innerHTML = bubbleHtml(r, App.state);
          bindBubblePhoto(r);
          positionBubble(r, App.state.layout.mapRect);
        }
      }).catch(function () {});
    } catch (_) {}
  }

  /** A Tabelog photo that fails to load must not leave a broken-image box in
   *  the bubble: fall back to the cuisine emoji and stop asking for that URL. */
  function bindBubblePhoto(r) {
    var im = bubbleEl && bubbleEl.querySelector('img.mp-bubble-photo');
    if (!im) return;
    im.addEventListener('error', function () {
      var span = document.createElement('span');
      span.className = 'mp-bubble-photo mp-bubble-photo-fallback';
      span.innerHTML = emoji.img(Data.genreEmoji(r), 22);
      if (im.parentNode) im.parentNode.replaceChild(span, im);
    }, { once: true });
  }

  var bubbleSuppressedId = null;
  function renderBubble(s) {
    var id = s.selected.id;
    var r = id ? Data.byId(id) : null;
    var rect = s.layout.mapRect;
    var allow = !!r && bubbleSuppressedId !== id && rect.h >= BUBBLE_MIN_MAP_H && rect.w >= 220 && !s.search.active;
    if (!allow) { hideBubble(); return; }
    if (!bubbleEl) {
      bubbleAnchor = document.createElement('div');
      bubbleAnchor.className = 'mp-bubble-anchor';
      bubbleEl = document.createElement('div');
      bubbleEl.className = 'mp-bubble';
      bubbleEl.setAttribute('aria-hidden', 'true');
      bubbleAnchor.appendChild(bubbleEl);
      map.getPanes().popupPane.appendChild(bubbleAnchor);
      bubbleEl.addEventListener('click', function () { if (bubbleFor) M.select(bubbleFor, { reveal: false }); });
    }
    wantPhoto(r);
    if (bubbleFor !== id || bubbleEl.dataset.lang !== s.lang) {
      bubbleEl.innerHTML = bubbleHtml(r, s);
      bubbleEl.dataset.lang = s.lang;
      bubbleFor = id;
      bindBubblePhoto(r);
    }
    bubbleAnchor.hidden = false;
    positionBubble(r, rect);
  }

  function hideBubble() {
    if (bubbleAnchor) bubbleAnchor.hidden = true;
    bubbleFor = null;
  }

  function positionBubble(r, rect) {
    if (!bubbleEl || !bubbleAnchor || bubbleAnchor.hidden) return;
    var U = mapOrigin();
    var ll = L.latLng(r.lat, r.lon);
    L.DomUtil.setPosition(bubbleAnchor, map.latLngToLayerPoint(ll));
    var bw = bubbleEl.offsetWidth || 220, bh = bubbleEl.offsetHeight || 56;
    var p = map.latLngToContainerPoint(ll);
    var x0 = rect.x - U.x + 8, y0 = rect.y - U.y + 8;
    var x1 = rect.x - U.x + rect.w - 8, y1 = rect.y - U.y + rect.h - 8;
    var cands = [
      { cls: 'is-above', x: p.x - bw / 2, y: p.y - 22 - bh, slide: true },
      { cls: 'is-right', x: p.x + 24, y: p.y - bh / 2, slide: false },
      { cls: 'is-left', x: p.x - 24 - bw, y: p.y - bh / 2, slide: false },
      { cls: 'is-below', x: p.x - bw / 2, y: p.y + 42, slide: true }
    ];
    var pick = cands[0], dx = 0;
    for (var i = 0; i < cands.length; i++) {
      var c = cands[i], slide = 0;
      if (c.slide) {
        if (c.x < x0) slide = x0 - c.x; else if (c.x + bw > x1) slide = x1 - (c.x + bw);
        if (bw > x1 - x0) slide = x0 - c.x;             // wider than the rect: pin to the left edge
      } else if (c.x < x0 || c.x + bw > x1) { continue; }
      if (c.y >= y0 && c.y + bh <= y1) { pick = c; dx = slide; break; }
    }
    bubbleEl.classList.remove('is-above', 'is-right', 'is-left', 'is-below');
    bubbleEl.classList.add(pick.cls);
    bubbleEl.style.setProperty('--mp-bubble-dx', Math.round(dx) + 'px');
  }

  /* ======================================================================
     rail overlay — docs/transit-layer.js against the three R2 LODs
     ================================================================== */
  function ensureRail() {
    if (rail) return rail;
    if (typeof L === 'undefined' || typeof L.transitLayer !== 'function') return null;
    var T = (Data.config && Data.config.TRANSIT) || {};
    rail = L.transitLayer({
      lodUrls: T.lodUrls, lodBreaks: T.lodBreaks,
      opacity: T.opacity, casingOpacity: T.casingOpacity
    });
    rail.on('lodloadstart', function () {
      railLoading = true;
      var patch = { long: railBuckets.long, city: railBuckets.city };
      railStatus.long = railBuckets.long ? 'loading' : railStatus.long;
      railStatus.city = railBuckets.city ? 'loading' : railStatus.city;
      App.set({ layers: { loading: patch, error: { long: false, city: false } } });
      App.emit('layers:load', { kind: railBuckets.long ? 'long' : 'city', status: 'loading' });
    });
    rail.on('lodload', function () {
      railLoading = false;
      railStatus.long = railStatus.city = 'ok';
      App.set({ layers: { loading: { long: false, city: false } } });
      App.emit('layers:load', { kind: 'long', status: 'ok' });
      scheduleStationPolish();
    });
    rail.on('lodloaderror', function (e) {
      railLoading = false;
      App.set({ layers: { loading: { long: false, city: false }, error: { long: true, city: true } } });
      App.emit('layers:load', { kind: 'long', status: 'error' });
      // A failed fetch leaves the toggle exactly where the user put it. It used
      // to bounce both buckets back off, which (a) made a flaky start look like
      // the user's own switch flipping itself and (b) — until the adapter
      // stopped persisting an error-driven off — wrote '0' over their choice in
      // tabelog.showTransitLong / …City. The state is "on, but failed": the
      // layers popover renders that from layers.error and offers 重试.
      if (!e || !e.hasData) railStatus.long = railStatus.city = 'error';
      act.showToast({ kind: 'error', text: t('交通图层加载失败，请稍后再试'),
        action: { label: t('重试'), run: function () { M.retryRail(); } } });
    });
    return rail;
  }

  function syncRail(s) {
    var want = { long: !!s.layers.long, city: !!s.layers.city };
    var changed = want.long !== railBuckets.long || want.city !== railBuckets.city;
    railBuckets = want;
    var any = want.long || want.city;
    var r = any ? ensureRail() : rail;
    if (!r) {
      if (any) {
        App.set({ layers: { error: { long: true, city: true } } });
        App.emit('layers:load', { kind: 'long', status: 'error' });
      }
      return;
    }
    if (changed || !r._map) {
      r.setVisibleBuckets({ long: want.long, city: want.city });
      if (any && !map.hasLayer(r)) map.addLayer(r);
      if (!any && map.hasLayer(r)) map.removeLayer(r);
    }
    if (any) scheduleStationPolish();
  }

  /** retryRail() — re-run the current LOD fetch after a failure, leaving the
   *  buckets alone. The layers popover's 重试 emits `layers:retry`. */
  M.retryRail = function () {
    if (!railBuckets.long && !railBuckets.city) return false;
    App.set({ layers: { error: { long: false, city: false } } });
    railStatus.long = railStatus.city = 'loading';
    var r = ensureRail();
    if (!r) { App.set({ layers: { error: { long: true, city: true } } });
              App.emit('layers:load', { kind: 'long', status: 'error' }); return false; }
    try {
      if (!map.hasLayer(r)) map.addLayer(r);
      if (typeof r.reload === 'function') r.reload();
      else { map.removeLayer(r); rail = null; ensureRail(); syncRail(App.state); }
    } catch (err) { console.error('[map] rail retry', err); return false; }
    return true;
  };

  /** setRail(kind, on) — also drivable from tests / the layers popover. */
  M.setRail = function (kind, on) {
    var patch = {}; patch[kind] = !!on;
    act.setLayers(patch);
  };
  M.setLayers = function (patch) { act.setLayers(patch || {}); };
  M.railStatus = function () { return { long: railStatus.long, city: railStatus.city, loading: railLoading }; };
  M.railDetail = function () {
    var drawn = 0, byBucket = { long: 0, city: 0 }, byClass = {};
    try {
      rail._onMap.forEach(function (f) {
        drawn++;
        byBucket[f._bucket] = (byBucket[f._bucket] || 0) + 1;
        byClass[f._class] = (byClass[f._class] || 0) + 1;
      });
    } catch (_) {}
    return {
      attached: !!(rail && map && map.hasLayer(rail)),
      loaded: rail && rail._allLines ? rail._allLines.length : 0,
      stations: rail && rail._allStations ? rail._allStations.length : 0,
      stationDots: rail && rail._stationsOn ? rail._stationsOn.size : 0,
      lod: rail ? rail._currentLodKey : null,
      drawn: drawn, byBucket: byBucket, byClass: byClass,
      buckets: { long: railBuckets.long, city: railBuckets.city },
      zoom: map ? map.getZoom() : null
    };
  };

  /* §6.5: the permanent station names drop the "(N lines)" suffix — the count is a
     nearby-lines estimate, not an official transfer number, and it is what made
     the labels wide enough to bury restaurants. transit-layer.js binds the
     tooltip text, so this trims the rendered labels after each paint; the hover
     tooltip keeps the full string. */
  // \u7ebf / \u7dda are escaped so the build's CJK-run scan does not read a
  // regex alternation as page copy needing a translation entry.
  var STN_SUFFIX_RE = /\s*[\uff08(]\s*\d+\s*(?:\u7ebf|\u7dda|lines)\s*[)\uff09]\s*$/;
  var polishRaf = 0;
  function scheduleStationPolish() {
    if (polishRaf) return;
    polishRaf = util.raf(function () {
      polishRaf = 0;
      var nodes = document.querySelectorAll('.leaflet-tooltip.transit-station-label');
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (el._mpTrim) continue;
        var txt = el.textContent || '';
        var cut = txt.replace(STN_SUFFIX_RE, '');
        var run = el.querySelector('[lang]');
        if (run) el.lang = run.lang;
        if (cut !== txt) el.textContent = cut;
        el._mpTrim = 1;
      }
    });
  }

  /* ======================================================================
     movement, bounds, reveal
     ================================================================== */
  function mapOrigin() {
    var el = map.getContainer(), r = el.getBoundingClientRect();
    return { x: r.left + el.clientLeft, y: r.top + el.clientTop };
  }

  var geometryDepth = 0;
  function invalidateGeometry() {
    geometryDepth += 1; programDepth += 1;
    try { map.invalidateSize({ pan: false }); } catch (_) {}
    finally { geometryDepth -= 1; programDepth = Math.max(0, programDepth - 1); }
  }

  var mapPointers = new Set(), gestureMove = false, gestureZoom = false, settledPending = false;
  function gestureBusy() { return mapPointers.size > 0 || gestureMove || gestureZoom; }
  function refreshGeometry() {
    if (gestureBusy()) { settledPending = true; return; }
    settledPending = false;
    invalidateGeometry();
    afterMove(true);
    flushLocation();
    if (App.state.selected.id) M.reveal(App.state.selected.id, { reason: 'viewport' });
  }
  function gestureReleased() {
    if (gestureBusy() || !(settledPending || locationPending || revealPending)) return;
    util.raf(function () {
      if (gestureBusy()) return;
      if (settledPending) refreshGeometry();
      else {
        flushLocation();
        if (revealPending) M.reveal(revealPending.id, { reason: revealPending.reason });
      }
    });
  }
  function bindMap() {
    mapRoot.addEventListener('pointerdown', function (e) { mapPointers.add(e.pointerId); });
    function pointerUp(e) { mapPointers.delete(e.pointerId); gestureReleased(); }
    window.addEventListener('pointerup', pointerUp);
    window.addEventListener('pointercancel', pointerUp);
    map.on('movestart', function () { if (!programDepth) gestureMove = true; });
    map.on('zoomstart', function () { if (!programDepth) gestureZoom = true; });
    map.on('moveend', function () { if (!geometryDepth) gestureMove = false; gestureReleased(); });
    map.on('zoomend', function () { gestureZoom = false; gestureReleased(); });
    map.on('movestart zoomstart', function () {
      document.documentElement.setAttribute('data-map-moving', '');
      if (bubbleEl) bubbleEl.classList.add('is-moving');
    });
    map.on('dragstart', function () {
      revealPending = null; locationPending = null;
      if (!App.state.userDraggedMap) App.set({ userDraggedMap: true }, { silent: true });   // ENV-02 §3
    });
    map.on('moveend zoomend', function () {
      document.documentElement.removeAttribute('data-map-moving');
      if (bubbleEl) bubbleEl.classList.remove('is-moving');
      var byUser = programDepth === 0;
      if (moveRaf) cancelAnimationFrame(moveRaf);
      moveRaf = util.raf(function () { moveRaf = 0; afterMove(false, byUser); });
    });
    // A tap on empty map dismisses the open card — the affordance 3.2.x had
    // and tests/ux/back_history.py's `close-map` scenario protects. It is a
    // dismissal, not a navigation, so it goes through closeDetail (NAV-01)
    // and leaves no history entry behind.
    map.on('click', function () {
      if (Date.now() - lastLongPress < 900) return;
      if (App.state.selected.id) act.closeDetail();
    });
    map.on('contextmenu', function (e) {
      // A programmatic map.fire('contextmenu', {latlng}) carries no
      // originalEvent and no containerPoint — guard both rather than throwing
      // out of a Leaflet event handler.
      if (e && e.originalEvent) L.DomEvent.preventDefault(e.originalEvent);
      if (Date.now() - lastLongPress < 900) return;           // the long press already opened it
      if (!e || !e.latlng) return;
      var pt = e.containerPoint || map.latLngToContainerPoint(e.latlng);
      emitPlaceMenu(e.latlng, pt);
    });
    mapRoot.addEventListener('click', function (e) {
      if (Date.now() - lastLongPress < 500) { e.stopPropagation(); e.preventDefault(); }
    }, true);
    bindLongPress();
    bindLocateEvents();

    App.on('map:reveal', function (p) { if (p && p.id) M.reveal(p.id, { reason: p.reason }); });
    App.on('map:locate-request', function (p) { if (!p || p.from !== 'map') M.locate(); });
    // overlays' 重试 on a failed rail row — refetch, toggles untouched.
    App.on('layers:retry', function () { M.retryRail(); });
    App.on('layout:settled', refreshGeometry);
    App.on('sheet:snapped', function () {
      App.set({ userDraggedMap: false }, { silent: true });
      if (App.state.selected.id) M.reveal(App.state.selected.id, { reason: 'sheet' });
    });
    App.on('detail:open', function () { locationPending = null; locationIntent = false; bubbleSuppressedId = null; });
    App.on('detail:close', function () { revealPending = null; hideBubble(); });
  }

  function afterMove(silentEmit, eventByUser) {
    var s = App.state;
    var c = map.getCenter(), z = map.getZoom();
    var byUser = eventByUser === undefined ? programDepth === 0 : eventByUser;
    if (Math.abs(c.lat - s.mapView.center[0]) > 1e-7 || Math.abs(c.lng - s.mapView.center[1]) > 1e-7 || z !== s.mapView.zoom) {
      App.set({ mapView: { center: [c.lat, c.lng], zoom: z } }, { silent: true });
    }
    syncMarkers(s);
    layoutTags();
    layoutPlates();
    if (s.layers.long || s.layers.city) scheduleStationPolish();
    if (s.selected.id) renderBubble(s);
    var vis = M.visibleIds();
    if (!silentEmit) App.emit('map:moveend', { bounds: M.bounds(), byUser: byUser, center: [c.lat, c.lng], zoom: z, visibleIds: vis });
    if (vis.length !== lastMV) { lastMV = vis.length; App.emit('map:mv', { MV: lastMV }); App.requestRender('mapView'); }
  }

  function program(fn) {
    programDepth += 1;
    var done = false;
    var release = function () {
      if (done) return;
      if (geometryDepth) { map.once('moveend', release); return; }
      done = true; programDepth = Math.max(0, programDepth - 1);
      if (!programDepth && revealPending) util.raf(function () {
        if (revealPending) M.reveal(revealPending.id, { reason: revealPending.reason });
      });
    };
    map.once('moveend', release);
    setTimeout(release, (motion.fly || 400) + 400);
    try { fn(); } catch (e) { release(); throw e; }
  }

  /** bounds() — the VISIBLE map rectangle after panels / columns (MAP-02 §6). */
  M.bounds = function () {
    if (!map) return null;
    var s = App.state, rect = s.layout.mapRect, U = mapOrigin();
    var x = rect.x - U.x, y = rect.y - U.y;
    var nw = map.containerPointToLatLng([x, y]);
    var se = map.containerPointToLatLng([x + rect.w, y + rect.h]);
    return { north: nw.lat, west: nw.lng, south: se.lat, east: se.lng };
  };
  M.MV = function (ids) { return Data.MV(ids || renderIds(App.state), M.bounds()); };
  M.visibleIds = function () { return M.MV(resultOverride || Data.applyFilters(App.state.filters)); };
  M.setResultIds = function (ids) { resultOverride = ids || null; lastSig = ''; App.requestRender('filters'); };

  // All location entry points wait for the panels to publish their final rect.
  var locationPending = null, locationQueued = false, locationIntent = false;
  function queueLocation(fn) {
    revealPending = null;
    locationPending = fn; locationIntent = true;
    App.set({ userDraggedMap: false }, { silent: true });
    if (locationQueued) return;
    locationQueued = true;
    util.raf(function () { util.raf(function () {
      motion.afterGeometry(function () { locationQueued = false; flushLocation(); });
    }); });
  }
  function visibleFrames(gap) {
    gap = gap === undefined ? 4 : gap;
    var s = App.state, r = s.layout.mapRect, U = mapOrigin();
    var box = { left: r.x - U.x, top: r.y - U.y, right: r.x - U.x + r.w, bottom: r.y - U.y + r.h };
    var search = document.getElementById('search-root');
    if (s.layout.mode === 'narrow' && search) {
      box.top = Math.max(box.top, search.getBoundingClientRect().bottom - U.y);
    }
    var frames = [box];
    var obstacles = [fabEl, mapRoot.querySelector('.leaflet-control-attribution'), mapRoot.querySelector('.leaflet-control-scale'),
      document.querySelector('#candidates-host > #candidates-root')];
    obstacles = obstacles.concat(Array.prototype.slice.call(document.querySelectorAll('#notice-root > *')));
    obstacles.forEach(function (el) {
      if (!el || getComputedStyle(el).visibility === 'hidden') return;
      var b = el.getBoundingClientRect();
      if (!b.width || !b.height) return;
      var o = { left: b.left - U.x - gap, top: b.top - U.y - gap,
                right: b.right - U.x + gap, bottom: b.bottom - U.y + gap };
      var next = [];
      frames.forEach(function (f) {
        if (o.left >= f.right || o.right <= f.left || o.top >= f.bottom || o.bottom <= f.top) { next.push(f); return; }
        next.push({ left: f.left, top: f.top, right: Math.min(f.right, o.left), bottom: f.bottom });
        next.push({ left: Math.max(f.left, o.right), top: f.top, right: f.right, bottom: f.bottom });
        next.push({ left: f.left, top: f.top, right: f.right, bottom: Math.min(f.bottom, o.top) });
        next.push({ left: f.left, top: Math.max(f.top, o.bottom), right: f.right, bottom: f.bottom });
      });
      frames = next.filter(function (f) { return f.right - f.left >= 36 && f.bottom - f.top >= 36; });
    });
    return frames.filter(function (f) { return f.right - f.left >= 36 && f.bottom - f.top >= 36; });
  }
  function visibleFrame() {
    var frames = visibleFrames();
    frames.sort(function (a, b) { return (b.right - b.left) * (b.bottom - b.top) - (a.right - a.left) * (a.bottom - a.top); });
    return frames[0] || null;
  }
  function flushLocation() {
    if (!locationPending || !map || motion.isGeometryBusy() || gestureBusy()) return;
    var b = visibleFrame();
    if (!b) return;
    var job = locationPending; locationPending = null;
    job(b);
  }
  function fitOptions(box, zoom) {
    var size = map.getSize(), pad = Math.min(24, (box.bottom - box.top) / 4, (box.right - box.left) / 4);
    return { animate: !motion.reduced, maxZoom: zoom,
      paddingTopLeft: [box.left + pad, box.top + pad],
      paddingBottomRight: [size.x - box.right + pad, size.y - box.bottom + pad] };
  }
  M.flyTo = function (latlng, zoom) {
    if (!map) return;
    var z = zoom === undefined ? map.getZoom() : zoom;
    queueLocation(function (box) {
      var size = map.getSize(), target = L.point((box.left + box.right) / 2, (box.top + box.bottom) / 2);
      var centre = map.unproject(map.project(latlng, z).subtract(target.subtract(size.divideBy(2))), z);
      program(function () {
        if (motion.reduced) map.setView(centre, z, { animate: false });
        else map.flyTo(centre, z, { duration: (motion.fly || 400) / 1000 });
      });
    });
  };
  // A saved view contains a map centre, not a place to align inside the panels.
  M.restoreView = function (center, zoom) {
    if (!map) return;
    var z = zoom === undefined ? map.getZoom() : zoom;
    queueLocation(function () {
      program(function () {
        if (motion.reduced) map.setView(center, z, { animate: false });
        else map.flyTo(center, z, { duration: (motion.fly || 400) / 1000 });
      });
    });
  };
  M.fitBounds = function (bounds, zoom) {
    if (!map) return;
    queueLocation(function (box) { program(function () { map.fitBounds(bounds, fitOptions(box, zoom || 16)); }); });
  };
  M.fitPlace = function (p) {
    if (!p || !map) return false;
    var bbox = p.bbox;
    if (Array.isArray(bbox) && bbox.length === 4 && typeof bbox[0] === 'number') {
      bbox = L.latLngBounds([bbox[0], bbox[2]], [bbox[1], bbox[3]]);
    }
    var lon = p.lon != null ? p.lon : p.lng;
    if (bbox) M.fitBounds(bbox, p.zoom || 16);
    else if (typeof p.lat === 'number' && typeof lon === 'number') M.flyTo([p.lat, lon], p.zoom || 16);
    else return false;
    return true;
  };

  /**
   * reveal(id, {reason}) — LAY-05: at most one minimal pan so the marker sits
   * inside the visible map rect, with the FAB group's real rectangle and the
   * name bubble's space held out. A user drag switches implicit reveals off
   * (ENV-02 §3) until the next explicit select.
   */
  var revealPending = null, revealQueued = false;
  M.reveal = function (id, opts) {
    if (!id || !Data.byId(id) || !map || App.state.selected.id !== id) return;
    var reason = (opts && opts.reason) || 'select';
    if (locationIntent && reason !== 'select') return;
    if (reason === 'select') { locationIntent = false; locationPending = null; }
    if (reason !== 'select' && App.state.userDraggedMap) return;
    revealPending = { id: id, reason: reason };
    if (revealQueued) return;
    revealQueued = true;
    util.raf(function () { util.raf(function () {
      motion.afterGeometry(function () {
        revealQueued = false;
        var job = revealPending; revealPending = null;
        if (job && App.state.selected.id === job.id) {
          if (gestureBusy() || programDepth) revealPending = job;
          else doReveal(job.id, job.reason);
        }
      });
    }); });
  };

  function doReveal(id, reason) {
    var r = Data.byId(id);
    if (!r || !map || App.state.selected.id !== id) return;
    if (reason !== 'select' && App.state.userDraggedMap) return;
    var st = App.state, rect = st.layout.mapRect, U = mapOrigin();
    var selectedEl = mapRoot.querySelector('.mp-mk.is-selected');
    var dot = selectedEl && selectedEl.querySelector('.mp-dot'), tag = selectedEl && selectedEl.querySelector('.mp-tag');
    if (dot && tag) {
      var dr = dot.getBoundingClientRect(), tr = tag.getBoundingClientRect();
      var actual = { left: Math.min(dr.left - 4, tr.left) - U.x, top: Math.min(dr.top - 4, tr.top) - U.y,
                     right: Math.max(dr.right + 4, tr.right) - U.x, bottom: Math.max(dr.bottom + 4, tr.bottom) - U.y };
      if (dr.width && tr.width && visibleFrames(0).some(function (f) {
        return actual.left >= f.left && actual.top >= f.top && actual.right <= f.right && actual.bottom <= f.bottom;
      })) { renderBubble(st); return; }
    }
    var frames = visibleFrames();
    if (!frames.length) {
      revealPending = { id: id, reason: reason };
      return;
    }
    var p = map.latLngToContainerPoint([r.lat, r.lon]);
    var short = rect.h < 80;
    var padLeft = 18, padRight = 18, padTop = 18, padBottom = short ? 18 : 42;
    if (selectedEl) {
      var mr = selectedEl.getBoundingClientRect();
      padLeft = Math.max(padLeft, p.x + U.x - mr.left + 4);
      padRight = Math.max(padRight, mr.right - p.x - U.x + 4);
      padTop = Math.max(padTop, p.y + U.y - mr.top + 4);
      padBottom = Math.max(short ? 18 : 42, mr.bottom - p.y - U.y + 4);
    }
    var wantBubble = rect.h >= BUBBLE_MIN_MAP_H && rect.w >= 220;
    function choose(withBubble) {
      var best = null;
      frames.forEach(function (f) {
        var l = padLeft, rr = padRight, tt = padTop;
        if (withBubble) { l = Math.max(l, BUBBLE_HALF_W); rr = Math.max(rr, BUBBLE_HALF_W); tt = Math.max(tt, BUBBLE_KEEPOUT_TOP); }
        var x0 = f.left + l, x1 = f.right - rr, y0 = f.top + tt, y1 = f.bottom - padBottom;
        if (x1 < x0 || y1 < y0) return;
        var dx = p.x - util.clamp(p.x, x0, x1), dy = p.y - util.clamp(p.y, y0, y1);
        var cost = dx * dx + dy * dy;
        if (!best || cost < best.cost) best = { dx: dx, dy: dy, cost: cost };
      });
      return best;
    }
    var best = choose(wantBubble);
    bubbleSuppressedId = null;
    if (!best && wantBubble) { best = choose(false); bubbleSuppressedId = id; }
    if (!best) { revealPending = { id: id, reason: reason }; hideBubble(); return; }
    var dx = best.dx, dy = best.dy;
    if (Math.abs(dx) < .01 && Math.abs(dy) < .01) { renderBubble(App.state); return; }
    program(function () { map.panBy([dx < 0 ? Math.floor(dx) : Math.ceil(dx), dy < 0 ? Math.floor(dy) : Math.ceil(dy)], { animate: !motion.reduced, duration: (motion.fly || 400) / 1000 }); });
  }

  /** select(id, {reveal}) — the map's own way into a detail card (NAV-01 origin 'map'). */
  M.select = function (id, opts) {
    if (!Data.byId(id)) return;
    act.openDetail(id, 'map');
    if (opts && opts.reveal) M.reveal(id, { reason: 'select' });
  };

  /* ----- long press → place menu --------------------------------------- */
  var lastLongPress = 0;
  function emitPlaceMenu(latlng, pt) {
    var U = mapOrigin();
    App.emit('map:contextmenu', { lat: latlng.lat, lon: latlng.lng, x: pt.x + U.x, y: pt.y + U.y });
  }

  function bindLongPress() {
    var timer = null, start = null, fired = false;
    var el = mapRoot;
    function clear() { if (timer) { clearTimeout(timer); timer = null; } start = null; }
    el.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse') return;                 // mouse uses contextmenu
      if (e.isPrimary === false) return;
      fired = false;
      start = { x: e.clientX, y: e.clientY };
      timer = setTimeout(function () {
        timer = null;
        if (!start) return;
        fired = true;
        lastLongPress = Date.now();
        var pt = map.mouseEventToContainerPoint(e);
        emitPlaceMenu(map.containerPointToLatLng(pt), pt);
      }, LONGPRESS_MS);
    }, { passive: true });
    el.addEventListener('pointermove', function (e) {
      if (!start) return;
      if (Math.abs(e.clientX - start.x) > 10 || Math.abs(e.clientY - start.y) > 10) clear();
    }, { passive: true });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) {
      el.addEventListener(ev, function (e) {
        if (fired && ev === 'pointerup') { if (e.preventDefault) e.preventDefault(); }
        clear();
      }, { passive: ev !== 'pointerup' });
    });
  }

  /* ======================================================================
     locate + 附近 (MAP-03) — the real leaflet.locatecontrol plugin.
     Only a user gesture reaches here; nothing asks for the permission on load.
     ================================================================== */
  var locateCtl = null, locateTries = 0, nearbyTimer = 0, requestedPlan = null;

  function locateStrings() {
    return {
      title: t('显示我的位置'),
      popup: t('距离约 {distance} {unit}'),
      outsideMapBoundsMsg: t('当前位置在地图范围之外'),
      metersUnit: t('米'),
      feetUnit: t('英尺')
    };
  }

  function attachLocate() {
    if (locateCtl) return true;
    if (typeof L === 'undefined' || !L.control || typeof L.control.locate !== 'function') return false;
    locateCtl = L.control.locate({
      position: 'topleft',
      flyTo: true,
      setView: 'untilPan',
      initialZoomLevel: 16,
      keepCurrentZoomLevel: false,
      cacheLocation: true,
      showCompass: true,
      drawCircle: true,
      drawMarker: true,
      // maximumAge 10 min lets the OS hand back a recent fix instead of
      // re-summoning the GPS chip (and, on iOS, re-prompting).
      locateOptions: { enableHighAccuracy: true, maximumAge: 600000, watch: false, timeout: 15000 },
      onLocationError: function (err) { locateFailed(errText(err)); },
      strings: locateStrings()
    }).addTo(map);
    return true;
  }

  function errText(err) {
    var code = err && (err.code !== undefined ? err.code : err.status);
    if (code === 1) return '定位权限被拒绝，可在浏览器设置中开启';
    if (code === 3) return '定位超时，请再试一次';
    return '无法取得位置';
  }

  // The 3.2.x Japan test, kept verbatim in shape: the bare bbox also holds
  // Jeju, the Korean east coast, Vladivostok and south Sakhalin, so the west
  // edge steps east with latitude. (coreRequest: expose Business.uxInJapan as
  // Data.inJapan so there is one copy.)
  // The coverage test is Business's (bbox + west-edge staircase), reached
  // through the adapter so there is exactly one copy. The literal table below
  // is only the fallback for a no-adapter path.
  var JAPAN_BBOX = { latMin: 20.0, latMax: 46.2, lonMin: 122.5, lonMax: 154.5 };
  var JAPAN_WEST_EDGE = [[32.5, 128.0], [33.5, 128.9], [34.8, 130.0], [37.0, 132.0], [42.0, 139.0], [45.6, 999]];
  function inJapan(ll) {
    if (Data && typeof Data.inJapan === 'function') return Data.inJapan(ll);
    if (!ll || !(ll.lat >= JAPAN_BBOX.latMin && ll.lat <= JAPAN_BBOX.latMax &&
                 ll.lng >= JAPAN_BBOX.lonMin && ll.lng <= JAPAN_BBOX.lonMax)) return false;
    var west = JAPAN_BBOX.lonMin;
    for (var i = 0; i < JAPAN_WEST_EDGE.length; i++) if (ll.lat >= JAPAN_WEST_EDGE[i][0]) west = JAPAN_WEST_EDGE[i][1];
    return ll.lng >= west;
  }

  function snapView(center, zoom) {
    map.setView(center, zoom, { animate: false });
    if (map._animatingZoom) map.once('zoomend', function () { map.setView(center, zoom, { animate: false }); });
  }

  /** MAP-03: one toast with the accurate reason and a way out. Region, sort and
   *  the map itself are left exactly where they were. */
  function locateFailed(key) {
    clearTimeout(nearbyTimer);
    var wasPending = nearbyOf(App.state).pending;
    requestedPlan = null;
    App.set({ nearby: { pending: false } });
    if (!wasPending && !key) return;
    act.showToast({ kind: 'error', text: t(key || '无法取得位置') });
    App.set({ toast: { action: { label: t('选地区'), run: function () { act.setTab('filters'); act.openOverlay('regionPicker'); } } } });
    App.emit('map:locate', { status: 'error', reason: key || '无法取得位置' });
  }

  function restorePlanning() {
    var plan = nearbyOf(App.state).planning;
    if (!plan) return;
    clearTimeout(nearbyTimer);
    try { if (locateCtl) locateCtl.stop(); } catch (_) {}
    App.set({ nearby: { active: false, planning: null, pending: false } });
    act.applyFilters({ region: plan.region === undefined ? null : plan.region });
    App.set({ sort: plan.sort || 'rating' });
    if (plan.center) snapView(plan.center, plan.zoom);
    App.emit('map:locate', { status: 'restored' });
  }
  M.restorePlanning = restorePlanning;

  function bindLocateEvents() {
    map.on('locationfound', function (e) {
      if (!e || !e.latlng) return;
      var s = App.state;
      var nb = nearbyOf(s);
      if (nb.pending && !inJapan(e.latlng)) {
        // MAP-03: a fix outside Japan never switches the scope — the corpus has
        // nothing near it. The plugin has already started flying; put it back.
        var plan = requestedPlan;
        try { if (locateCtl) locateCtl.stop(); } catch (_) {}
        if (plan && plan.center) snapView(plan.center, plan.zoom);
        locateFailed('地图只覆盖日本');
        return;
      }
      var fix = { lat: e.latlng.lat, lon: e.latlng.lng, acc: e.accuracy || null, ts: Date.now() };
      if (!nb.pending) { App.set({ nearby: { fix: fix } }); return; }
      clearTimeout(nearbyTimer);
      var wasNearby = nb.active;
      if (!wasNearby) {
        var plan2 = requestedPlan; requestedPlan = null;
        App.set({ nearby: { active: true, pending: false, fix: fix, planning: plan2 } });
        act.applyFilters({ region: null });
        App.set({ sort: 'distance' });
        act.showToast({ kind: 'info', text: t('已切到全部地区，按距离排序'), undo: restorePlanning });
        App.set({ toast: { action: { label: t('撤销'), run: restorePlanning } } });
      } else {
        App.set({ nearby: { pending: false, fix: fix } });
      }
      App.emit('map:locate', { status: 'ok', latlng: [fix.lat, fix.lon], accuracy: fix.acc });
    });
  }

  /** locate() — the FAB's "find nearby": locate, switch to all regions +
   *  distance, and offer Undo. A second tap while already nearby only
   *  re-locates. Never called except from a user gesture. */
  M.locate = function () {
    if (!map) return;
    App.emit('map:locate-request', { from: 'map' });
    if (nearbyOf(App.state).pending) return;
    if (!attachLocate()) {
      // the plugin is `defer`red and optional (BUG-01): retry briefly, then say so
      if (locateTries++ < 20) { setTimeout(function () { M.locate(); }, 250); return; }
      locateFailed('无法取得位置');
      return;
    }
    try { map.stop(); } catch (_) {}
    var s = App.state;
    if (!nearbyOf(s).active) {
      var c = map.getCenter();
      requestedPlan = nearbyOf(s).planning || { region: s.filters.region === undefined ? null : s.filters.region,
        sort: s.sort, center: [c.lat, c.lng], zoom: map.getZoom() };
    }
    App.set({ nearby: { pending: true } });
    App.emit('map:locate', { status: 'locating' });
    clearTimeout(nearbyTimer);
    nearbyTimer = setTimeout(function () {
      if (!nearbyOf(App.state).pending) return;
      try { if (locateCtl) locateCtl.stop(); } catch (_) {}
      locateFailed('定位超时，请再试一次');
    }, 18000);
    try { locateCtl.stop(); locateCtl.start(); } catch (_) { locateFailed('无法取得位置'); }
  };

  /* ======================================================================
     FAB group (MAP-03 / LAYER-02 anchor)
     ================================================================== */
  function bindFab() {
    if (!fabEl) return;
    util.delegate(fabEl, 'click', '[data-fab]', function (e, el) {
      var k = el.dataset.fab;
      if (k === 'in') { program(function () { map.zoomIn(); }); }
      else if (k === 'out') { program(function () { map.zoomOut(); }); }
      else if (k === 'locate') M.locate();
      else if (k === 'layers') {
        if (App.state.overlay.kind === 'layers') act.closeOverlay('toggle');
        else act.openOverlay('layers');
      }
    });
  }

  function nearbyOf(s) { return (s && s.nearby) || { active: false, planning: null, pending: false, fix: null }; }

  function fabSig(s) {
    var on = ['long', 'city', 'landmarks', 'pins'].filter(function (k) { return s.layers[k]; }).length;
    var nb = nearbyOf(s);
    return [s.layout.mode, s.layout.W >= 700 ? 'z' : '', on, s.overlay.kind === 'layers' ? 'o' : '',
            nb.pending ? 'p' : (nb.active ? 'n' : ''), s.lang].join('|');
  }

  var lastFab = '', lastFabHide = null;
  function renderFab(s) {
    if (!fabEl) return;
    var sig = fabSig(s);
    var hide = !!(s.search.active || (s.overlay.kind && App.nav.MODAL_KINDS.indexOf(s.overlay.kind) >= 0));
    fabEl.classList.toggle('is-hidden', hide);
    fabEl.hidden = false;
    var sigChanged = sig !== lastFab;
    if (sigChanged) {
      lastFab = sig;
      var on = ['long', 'city', 'landmarks', 'pins'].filter(function (k) { return s.layers[k]; }).length;
      var nb = nearbyOf(s);
      var showZoom = s.layout.W >= 700;                       // MAP-03: ± only when the protected area allows it
      var html = '';
      if (showZoom) {
        html += btn('in', 'plus', t('放大'));
        html += btn('out', 'minus', t('缩小'));
      }
      html += '<button type="button" class="mp-fab glass' + (nb.pending ? ' is-pending' : '') + (nb.active ? ' is-on' : '') +
        '" data-fab="locate" aria-label="' + util.esc(nb.pending ? t('正在定位…') : t('定位到我的位置')) +
        '" aria-pressed="' + (nb.active ? 'true' : 'false') + '">' + icon('locate') + '</button>';
      html += '<button type="button" class="mp-fab glass" data-fab="layers" aria-label="' + util.esc(t('地图图层')) +
        '" aria-haspopup="dialog" aria-expanded="' + (s.overlay.kind === 'layers') + '">' + icon('layers') +
        (on ? '<span class="count-badge mp-fab-badge">' + util.fmtCount(on) + '</span>' : '') + '</button>';
      fabEl.innerHTML = html;
    }
    if (sigChanged || hide !== lastFabHide) {            // measure only when the stack really changed
      lastFabHide = hide;
      var rect = fabEl.getBoundingClientRect();
      layoutApi.setFabWidth(hide ? 0 : (rect.width || 44));
      document.documentElement.style.setProperty('--fab-h', Math.round(hide ? 0 : rect.height || 44) + 'px');
    }
  }
  function btn(key, ic, label) {
    return '<button type="button" class="mp-fab glass" data-fab="' + key + '" aria-label="' + util.esc(label) + '">' + icon(ic) + '</button>';
  }

  /* ======================================================================
     render
     ================================================================== */
  M.render = function (s, changed) {
    if (!map) return;
    var any = App.changedAny;
    var full = !changed || changed.has('*');

    if (full) {
      invalidateGeometry();
      lastSig = ''; lastFab = ''; lastFavSig = ''; lastSelected = null; lastLang = '';
      _tagW = {};
    }

    if (full || any(changed, ['notices'])) {
      var wantOffline = !!s.notices.offline;
      if (wantOffline !== offline || (wantOffline && !offlineNote)) M.setTilesOffline(wantOffline);
    }

    var markersChanged = false;
    if (full || any(changed, ['filters', 'user', 'selected', 'lang', 'saved'])) markersChanged = syncMarkers(s);

    if (full || any(changed, ['layers', 'user', 'lang'])) { renderLandmarks(s); renderPins(s); syncRail(s); }

    // geometry: only a real container resize needs invalidateSize; mapRect is logical
    var rectSig = [s.layout.W, s.layout.H, s.layout.U.x, s.layout.U.y, s.layout.U.w, s.layout.U.h].join(',');
    if (rectSig !== lastMapRectSig) { lastMapRectSig = rectSig; if (!full) invalidateGeometry(); }

    document.documentElement.toggleAttribute('data-map-short', s.layout.mapRect.h < 80);
    renderFab(s);

    if (markersChanged || full || any(changed, ['layout', 'sheet', 'columns', 'search', 'selected', 'mapView', 'fontScale', 'lang'])) {
      layoutTags();
      schedulePlateLayout();
      renderBubble(s);
      var vis = M.visibleIds();
      if (vis.length !== lastMV) { lastMV = vis.length; App.emit('map:mv', { MV: lastMV }); }
    }
  };

  M.destroy = function () {
    // folium owns the map object; only our own layers come down.
    try {
      if (cluster) cluster.clearLayers();
      if (selLayer) selLayer.clearLayers();
      if (landmarkLayer) landmarkLayer.clearLayers();
      if (pinLayer) pinLayer.clearLayers();
      if (tempLayer) tempLayer.clearLayers();
      if (rail && map.hasLayer(rail)) map.removeLayer(rail);
    } catch (_) {}
    if (fabEl) fabEl.innerHTML = '';
    markers = {}; shown = {}; selId = null; bubbleEl = null; bubbleFor = null;
  };

  window.App.registerModule('map', M);
})();
