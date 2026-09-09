/* L.TransitLayer — a Leaflet overlay that renders Japan's railway network
   from a precomputed GeoJSON (data/transit/japan.geojson).

   Usage:
     var layer = new L.TransitLayer({ geojsonUrl: 'transit/japan.geojson',
                                      opacity: 0.7 });
     layer.addTo(map);
     ...
     map.removeLayer(layer);

   The first addTo triggers a fetch + parse + index. Subsequent toggles reuse
   the cached features; only the polyline objects (which hold per-renderer
   refs) are rebuilt. */

(function() {
  'use strict';
  if (typeof L === 'undefined') {
    console.error('[TransitLayer] Leaflet not loaded');
    return;
  }

  // ---- Language detection ------------------------------------------------
  // Mirrors the dispatch in map.py: ?lang= URL param beats localStorage,
  // and zh-CN is the silent default. The transit layer is standalone JS so
  // it can't import the value from there; resolved once at module load.
  // Picks name_en / route_name_en when active lang is 'en'; falls back to
  // the Japanese fields when OSM has no English tag (coverage is partial,
  // especially outside major metros). Line classification regexes still
  // see the Japanese name so 新幹線 etc. keep matching.
  var ACTIVE_LANG = (function() {
    var p = null;
    try {
      var u = new URL(window.location.href).searchParams.get('lang');
      if (u) p = u.toLowerCase();
    } catch (_) {}
    if (p === 'tw' || p === 'zh-tw') return 'zh-TW';
    if (p === 'cn' || p === 'zh-cn') return 'zh-CN';
    if (p === 'en') return 'en';
    try {
      var s = localStorage.getItem('tabelog.lang');
      if (s === 'zh-TW' || s === 'zh-CN' || s === 'en') return s;
    } catch (_) {}
    return 'zh-CN';
  })();
  var LINES_SUFFIX = ACTIVE_LANG === 'en' ? ' lines'
                   : ACTIVE_LANG === 'zh-TW' ? '線' : '线';
  function pickLineLabel(props) {
    if (ACTIVE_LANG === 'en') {
      return props.route_name_en || props.name_en
          || props.route_name    || props.name;
    }
    return props.route_name || props.name;
  }
  function pickStationName(props) {
    if (ACTIVE_LANG === 'en') return props.name_en || props.name;
    return props.name;
  }

  // Inject tooltip styles once.
  if (!document.getElementById('transit-layer-styles')) {
    var style = document.createElement('style');
    style.id = 'transit-layer-styles';
    style.textContent =
      '.leaflet-tooltip.transit-line-label{background:rgba(33,33,33,0.92);' +
      'border:none;color:#fff;border-radius:6px;padding:3px 8px;font-size:12px;' +
      'font-weight:600;box-shadow:0 2px 4px rgba(0,0,0,0.18);white-space:nowrap;}' +
      '.leaflet-tooltip.transit-line-label::before{display:none;}' +
      '.leaflet-tooltip.transit-station-label{background:rgba(255,255,255,0.92);' +
      'border:1px solid rgba(0,0,0,0.08);border-radius:6px;padding:2px 6px;' +
      'font-size:11px;color:#333;box-shadow:0 1px 2px rgba(0,0,0,0.08);' +
      'white-space:nowrap;}';
    document.head.appendChild(style);
  }

  // ---- classification (structural: minZ + weight + importance + bucket) --
  // Each line is in exactly one bucket — 'long' (shinkansen / JR long-haul
  // mainlines that the postprocess pass tagged with is_longhaul) or 'city'
  // (everything commuter-scale and below). The two FABs in map.py each
  // toggle one bucket.
  //
  // minZ values are tuned more aggressively than they used to be: the
  // country-scale view (z<7) shows only shinkansen, z7-8 brings in JR
  // mainlines, z9 adds JR city + private suburban rail, and subways /
  // trams / monorails / narrow gauge only appear at z11+ where the bbox
  // is already neighborhood-scale.

  var SHINKANSEN_RE = /新幹線|Shinkansen/i;
  var JR_OP_RE = /^(東日本旅客鉄道|西日本旅客鉄道|東海旅客鉄道|九州旅客鉄道|北海道旅客鉄道|四国旅客鉄道|日本貨物鉄道)/;
  var PRIVATE_OP_RE = /(東急|京急|京王|小田急|京成|東武|西武|相鉄|京阪|阪急|阪神|近鉄|南海|名鉄|西鉄|東京メトロ|都営|大阪メトロ|Osaka Metro|名古屋市|札幌市|福岡市|京都市|神戸市|横浜市|広島電鉄|長崎電気軌道|熊本市|鹿児島市|岡山電気軌道|江ノ島電鉄|つくばエクスプレス|京浜急行|相模鉄道)/;

  var CLASSES = {
    shinkansen: { w: 4.0, minZ: 5,  importance: 5, bucket: 'long' },
    jr_long:    { w: 2.6, minZ: 7,  importance: 4, bucket: 'long' },
    jr_city:    { w: 2.4, minZ: 9,  importance: 3, bucket: 'city' },
    subway:     { w: 2.4, minZ: 11, importance: 3, bucket: 'city' },
    monorail:   { w: 2.0, minZ: 12, importance: 2, bucket: 'city' },
    narrow:     { w: 1.9, minZ: 12, importance: 2, bucket: 'city' },
    private:    { w: 2.2, minZ: 9,  importance: 3, bucket: 'city' },
    tram:       { w: 1.8, minZ: 12, importance: 2, bucket: 'city' },
    other:      { w: 1.6, minZ: 12, importance: 1, bucket: 'city' }
  };

  function classify(props) {
    var rw = props.railway;
    var op = props.operator || '';
    var name = props.name || '';
    var rn = props.route_name || '';
    if (SHINKANSEN_RE.test(name) || SHINKANSEN_RE.test(op) || SHINKANSEN_RE.test(rn)) return 'shinkansen';
    if (rw === 'subway') return 'subway';
    if (rw === 'tram' || rw === 'light_rail') return 'tram';
    if (rw === 'monorail') return 'monorail';
    if (rw === 'narrow_gauge') return 'narrow';
    if (JR_OP_RE.test(op)) {
      // is_longhaul flag baked in by src/tabelog/scrape/transit_postprocess.py
      // (shinkansen / 特急 / JR mainline allowlist). Without it we have no
      // way to tell the Yamanote loop from the Tokaido main line — they're
      // both railway=rail with a JR operator.
      return props.is_longhaul ? 'jr_long' : 'jr_city';
    }
    if (PRIVATE_OP_RE.test(op)) return 'private';
    return 'other';
  }

  // ---- color resolution ------------------------------------------------

  var SHINKANSEN_FALLBACK = [
    { match: /西九州新幹線/, color: '#a8201a' },
    { match: /九州新幹線/,   color: '#e60012' },
    { match: /北海道新幹線/, color: '#84499b' },
    { match: /東海道新幹線/, color: '#0072bc' },
    { match: /山陽新幹線/,   color: '#0072bc' },
    { match: /東北新幹線/,   color: '#22ac38' },
    { match: /北陸新幹線/,   color: '#b07f4e' },
    { match: /上越新幹線/,   color: '#d83b6e' },
    { match: /山形新幹線/,   color: '#d4af37' },
    { match: /秋田新幹線/,   color: '#c9305c' }
  ];
  var SK_DEFAULT = '#0b3d91';

  var PALETTE = [
    '#e63946','#f4a261','#2a9d8f','#264653','#457b9d',
    '#bc4749','#386641','#6a994e','#fb8500','#fb6f92',
    '#ff006e','#8338ec','#3a86ff','#06d6a0','#ffbe0b',
    '#9c27b0','#3f51b5','#009688','#795548','#607d8b',
    '#ad1457','#6a1b9a','#283593','#1565c0','#00838f',
    '#00695c','#2e7d32','#5d4037','#455a64','#d84315'
  ];

  function hashStr(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = (((h << 5) + h) + s.charCodeAt(i)) | 0;
    return h >>> 0;
  }

  // Visual de-noising: city-bucket lines are context, not content — at
  // z14 in central Tokyo they used to shout at full saturation over the
  // restaurant layer. Mixing toward white (pastel) keeps every line
  // traceable and color-distinguishable while pushing the whole tier
  // into the background. Long-haul lines (shinkansen / JR mainline) stay
  // at full strength: they're sparse, and they're what the 长途 user is
  // actually looking at.
  function muteColor(hex) {
    if (!/^#[0-9a-f]{6}$/i.test(hex)) return hex;
    var t = 0.45;   // fraction mixed toward white
    var out = '#';
    for (var i = 1; i < 7; i += 2) {
      var c = parseInt(hex.slice(i, i + 2), 16);
      c = Math.round(c + (255 - c) * t);
      out += (c < 16 ? '0' : '') + c.toString(16);
    }
    return out;
  }

  var CLASS_DEFAULT_COLOR = {
    shinkansen: SK_DEFAULT,
    jr:         '#2e7d32',
    subway:     '#37474f',
    monorail:   '#e65100',
    narrow:     '#558b2f',
    private:    '#5b6e8a',
    tram:       '#c62828',
    other:      '#90a4ae'
  };

  function colorFor(props, classKey) {
    var c = props.colour;
    if (c && /^#[0-9a-f]{6}$/i.test(c)) return c;
    var name = props.name || '';
    var op = props.operator || '';
    var rn = props.route_name || '';
    if (classKey === 'shinkansen') {
      for (var i = 0; i < SHINKANSEN_FALLBACK.length; i++) {
        var sk = SHINKANSEN_FALLBACK[i];
        if (sk.match.test(name) || sk.match.test(rn) || sk.match.test(op)) return sk.color;
      }
      return SK_DEFAULT;
    }
    var seed = rn || name || op;
    if (seed && classKey !== 'other') {
      return PALETTE[hashStr(seed) % PALETTE.length];
    }
    return CLASS_DEFAULT_COLOR[classKey];
  }

  // ---- the layer -------------------------------------------------------

  // M-009: detail ordering of the three LOD files, so a "cap" can be
  // expressed as "never go finer than this" without a pile of if/else.
  var LOD_RANK = { low: 0, mid: 1, high: 2 };

  // M-3.2.2-02: the line canvas is painted at one device pixel per CSS
  // pixel, where stock Leaflet always doubles it on a hidpi screen.
  //
  // Measured at 932x704 CSS px / DPR 2.625 with a 4x CPU throttle, long-haul
  // bucket on, ~320 lines in view, five 10-segment drags: 310 ms of main
  // thread per drag with the 2x canvas, 246 ms with this one, against 203 ms
  // with no rail layer on the map at all. Breaking that 107 ms down, 68 ms is
  // the mere presence of the backing store (an empty canvas of the same size
  // costs 271 ms) and only 39 ms is the stroking — so it is the 3.9-megapixel
  // surface that gets composited on every frame of a pan, not the line count,
  // and quartering it takes 60% of the layer's whole cost away.
  //
  // Only the lines soften. Station dots are circleMarkers on a plain
  // layerGroup and the map runs preferCanvas:false, so they and their labels
  // are SVG/DOM and untouched; and map.py already drops the base map to 1x
  // tiles whenever either rail bucket is on, so the whole map is at one
  // device pixel in this mode rather than half of it being sharp.
  //
  // Leaflet 1.9.3's Canvas._update reads the module-level Browser.retina —
  // not devicePixelRatio, not an option — so flipping that flag around the
  // one call is the only seam. Synchronous, and restored in a finally.
  var LoResCanvas = L.Canvas.extend({
    _update: function() {
      var retina = L.Browser.retina;
      L.Browser.retina = false;
      try {
        L.Canvas.prototype._update.call(this);
      } finally {
        L.Browser.retina = retina;
      }
    }
  });

  L.TransitLayer = L.Layer.extend({
    options: {
      // Legacy single-file mode. Used iff lodUrls is not set.
      geojsonUrl: 'transit/japan.geojson',
      // Multi-LOD mode. Map of { low, mid, high } -> URL. When set, the
      // layer loads only the LOD appropriate for the current zoom and
      // hot-swaps on zoom changes that cross a break point. lodBreaks
      // gives the [mid, high) boundaries — e.g. { mid: 9, high: 14 }
      // means LOD 'low' is used for z<9, 'mid' for 9<=z<14, 'high' for
      // z>=14.
      lodUrls: null,
      lodBreaks: null,
      opacity: 0.7,         // long-haul line opacity over the base map
      cityOpacity: 0.45,    // city-bucket lines sit further back (see muteColor)
      casingOpacity: 0.45,  // white casing underneath, less prominent
      padding: 0.25,
      grid: 0.4             // grid cell size in degrees
    },

    initialize: function(options) {
      L.Util.setOptions(this, options);
      // Cached across add/remove cycles
      this._loaded = false;
      this._loading = false;
      this._allLines = [];
      this._allStations = [];
      this._lineIndex = new Map();
      // Per-LOD parsed-and-indexed data. Once an LOD is loaded it stays
      // in this map; switching back is just a pointer swap, no refetch.
      this._lodCache = {};
      this._lodInflight = {};
      // M-010: one AbortController per in-flight LOD, so a toggle-off or a
      // zoom that moves the target elsewhere cancels the transfer instead
      // of paying for megabytes nobody will ever render.
      this._lodAbort = {};
      this._currentLodKey = null;
      // Which buckets the renderer is currently allowed to draw. Both
      // default on so a bare addTo() keeps the historical behavior; map.py
      // calls setVisibleBuckets({long, city}) from the FAB wiring to switch.
      this._buckets = { long: true, city: true };
      // Volatile (cleared on remove)
      this._onMap = new Set();
      // station feature -> {marker, radius, showLabel, label}; lets a pan
      // diff the visible set instead of destroying and recreating every
      // circle + permanent label on each moveend.
      this._stationsOn = new Map();
      this._lastZoom = null;
      this._lastDrawCasing = null;
      this._rafToken = 0;
      this._lastHover = 0;
      this._hoverOpen = false;
    },

    setVisibleBuckets: function(opts) {
      if (opts && typeof opts.long === 'boolean') this._buckets.long = opts.long;
      if (opts && typeof opts.city === 'boolean') this._buckets.city = opts.city;
      if (this._map) this._scheduleRedraw();
      return this;
    },

    onAdd: function(map) {
      this._map = map;
      // ONE canvas for all line work. The old scheme stacked seven
      // full-viewport canvases (casing + five importance tiers + an
      // invisible hit layer) — ~50MB of backing store EACH at hidpi, and
      // every one repainted per moveend. Paint order inside a single
      // canvas is insertion order, so the importance tiers are preserved
      // by (re)attaching polylines sorted whenever membership changes;
      // hover hit-testing is done in coordinate space (no hit canvas).
      this._rLines = new LoResCanvas({ padding: this.options.padding }).addTo(map);
      this._stationsLayer = L.layerGroup().addTo(map);
      this._scheduleRedrawBound = this._scheduleRedraw.bind(this);
      map.on('moveend', this._scheduleRedrawBound);
      // Line-name hover, pointer devices only — touch never hovers, so
      // phones skip the listener (and its per-move segment math) entirely.
      if (window.matchMedia && matchMedia('(hover: hover)').matches) {
        this._onMouseMoveBound = this._onMouseMove.bind(this);
        this._onMouseOutBound = this._hideHover.bind(this);
        map.on('mousemove', this._onMouseMoveBound);
        map.on('mouseout', this._onMouseOutBound);
        this._hoverTip = L.tooltip({
          className: 'transit-line-label', direction: 'top',
          offset: [0, -8], opacity: 1
        });
      }
      if (this._loaded) {
        this._scheduleRedraw();
      } else {
        this._load();
      }
      return this;
    },

    onRemove: function(map) {
      if (this._scheduleRedrawBound) {
        map.off('moveend', this._scheduleRedrawBound);
        this._scheduleRedrawBound = null;
      }
      if (this._onMouseMoveBound) {
        map.off('mousemove', this._onMouseMoveBound);
        map.off('mouseout', this._onMouseOutBound);
        this._onMouseMoveBound = this._onMouseOutBound = null;
      }
      this._hideHover();
      this._hoverTip = null;
      if (this._rafToken) {
        cancelAnimationFrame(this._rafToken);
        this._rafToken = 0;
      }
      this._teardownActiveLayers();
      if (this._rLines) this._rLines.remove();
      if (this._stationsLayer) this._stationsLayer.remove();
      this._rLines = this._stationsLayer = null;
      this._lastZoom = null;
      this._lastDrawCasing = null;
      this._map = null;
      // M-010: the parsed LOD data is by far the biggest thing this layer
      // holds (tens of MB of coordinate arrays for the high LOD). Keeping it
      // across a toggle-off meant "off" gave back exactly zero memory —
      // measured 43.1 MB before AND after closing the layer. Since M-009 the
      // LOD files are gzipped, content-hashed, immutable R2 objects, so
      // re-opening re-reads them from the browser's HTTP cache: cheap enough
      // to trade for the heap.
      this._abortLods(null);
      this._lodCache = {};
      this._lodInflight = {};
      this._allLines = [];
      this._allStations = [];
      this._lineIndex = new Map();
      this._currentLodKey = null;
      this._loaded = false;
      this._loading = false;
      return this;
    },

    // M-010: abort every in-flight LOD fetch except `keep` (pass null to
    // abort all). The aborted promise settles through the catch below,
    // which swallows AbortError.
    _abortLods: function(keep) {
      var keys = Object.keys(this._lodAbort || {});
      for (var i = 0; i < keys.length; i++) {
        var k = keys[i];
        if (k === keep) continue;
        try { this._lodAbort[k].abort(); } catch (_) {}
        delete this._lodAbort[k];
        delete this._lodInflight[k];
      }
    },

    // Detach polylines for everything currently on the map and drop the
    // refs. The polylines belong to the renderer we may be about to throw
    // away (onRemove) or to a soon-to-be-swapped LOD (LOD switch), so
    // holding onto them would pin dead state. Station markers belong to
    // the active LOD's features too, so the registry goes with them.
    _teardownActiveLayers: function() {
      var it = this._onMap.values(), v;
      while (!(v = it.next()).done) {
        var f = v.value;
        if (f._pl) { f._pl.remove(); f._pl = null; }
        if (f._pl_casing) {
          if (f._pl_casing._map) f._pl_casing.remove();
          f._pl_casing = null;
        }
      }
      this._onMap.clear();
      if (this._stationsLayer) this._stationsLayer.clearLayers();
      this._stationsOn.clear();
      this._hideHover();
    },

    _load: function() {
      if (this.options.lodUrls && this.options.lodBreaks) {
        // LOD mode: figure out the current zoom's target LOD and load it.
        var target = this._targetLodKey() || 'low';
        this._loadLod(target);
      } else {
        this._loadLegacy();
      }
    },

    _loadLegacy: function() {
      if (this._loaded || this._loading) return;
      this._loading = true;
      var self = this;
      fetch(this.options.geojsonUrl)
        .then(function(r) { return r.json(); })
        .then(function(gj) {
          self._parseInto(gj, self._allLines, self._allStations, self._lineIndex);
          self._loaded = true;
          self._loading = false;
          if (self._map) self._scheduleRedraw();
        })
        .catch(function(e) {
          console.error('[TransitLayer] load failed:', e);
          self._loading = false;
        });
    },

    // Walk a parsed GeoJSON FeatureCollection, sorting LineStrings into
    // `lines` (and indexing them spatially into `lineIndex`) and Points
    // into `stations`. _indexLine reads/writes this._lineIndex, so we
    // temporarily swap it to the target index — keeps the existing
    // indexing logic intact whether we're loading the legacy single
    // file or one LOD into its own cache slot.
    // M-010: neither branch keeps the source feature. Holding the parsed
    // GeoJSON objects kept the whole OSM property bag (kind / ref /
    // route_ref / operator / both name pairs) and, for stations, the
    // [lon,lat] geometry wrapper alive for the life of the layer. Lines get
    // a throwaway carrier that _indexLine strips down to its derived fields;
    // stations get a flat record holding only what _redraw reads. The field
    // list below is the complete set of `.properties.` / `.geometry.` reads
    // in this file — grep before adding one.
    _parseInto: function(gj, lines, stations, lineIndex) {
      var savedIndex = this._lineIndex;
      this._lineIndex = lineIndex;
      try {
        for (var i = 0; i < gj.features.length; i++) {
          var f = gj.features[i];
          if (!f.geometry) continue;
          if (f.geometry.type === 'LineString') {
            // _indexLine nulls .geometry / .properties once it has derived
            // _latlngs / _bbox / _class / _bucket / _color / _imp / _label.
            var line = { geometry: f.geometry, properties: f.properties || {} };
            lines.push(line);
            this._indexLine(line);
          } else if (f.geometry.type === 'Point') {
            var p = f.properties || {};
            var c = f.geometry.coordinates || [];
            stations.push({
              lon: +c[0],
              lat: +c[1],
              name: p.name,                     // pickStationName
              name_en: p.name_en,               // pickStationName (en)
              railway: p.railway,               // tram_stop styling
              line_count: p.line_count | 0,     // hub sizing + label suffix
              // Left undefined when absent on purpose: _redraw's legacy
              // fallback keys off `typeof ... === 'undefined'`.
              has_long_line: p.has_long_line,
              has_city_line: p.has_city_line
            });
          }
        }
      } finally {
        this._lineIndex = savedIndex;
      }
    },

    // Pick the LOD whose zoom band the map is currently in, then cap it by
    // what is actually renderable and what the connection can afford.
    // Returns null if LOD mode isn't configured (caller falls back to legacy
    // load).
    _targetLodKey: function() {
      if (!this._map || !this.options.lodBreaks) return null;
      var z = this._map.getZoom();
      var b = this.options.lodBreaks;
      var key = 'low';
      if (b.high != null && z >= b.high) key = 'high';
      else if (b.mid != null && z >= b.mid) key = 'mid';

      // M-009: zoom alone used to decide this, so a user with ONLY the
      // long-haul FAB on still pulled the 4.2 MB 'high' file at z15 —
      // everything it adds over 'mid' is city-bucket geometry and city
      // station detail that _redraw immediately culls.
      //
      // The cap is 'mid', not 'low', and that is deliberate: the low LOD
      // written by transit_postprocess.py::_write_lods carries long-haul
      // LINES ONLY (verified: 51,598 LineStrings, 0 Points in
      // docs/transit/japan-low.geojson), so capping there would silently
      // delete every station dot and label above z12 — and its
      // LOD_LOW_EPSILON of 0.015 deg (~1.5 km) is several hundred px of
      // geometry error at z14+.
      var cap = null;
      if (!this._buckets.city) cap = 'mid';
      // Data saver / slow link: never auto-escalate to the biggest file.
      if (this._slowLink()) cap = 'mid';
      if (cap && LOD_RANK[key] > LOD_RANK[cap]) key = cap;
      return key;
    },

    // M-009: navigator.connection is Chromium-only — absent in Safari and
    // Firefox, where we just behave as before.
    _slowLink: function() {
      var c = navigator.connection || navigator.mozConnection ||
              navigator.webkitConnection;
      if (!c) return false;
      if (c.saveData === true) return true;
      var et = c.effectiveType;
      return et === 'slow-2g' || et === '2g' || et === '3g';
    },

    // Fetch + parse one LOD into the cache. If the LOD is already cached
    // (hit on revisit, or arrived from a previous fetch), activates
    // immediately. If a fetch is already in flight for this LOD, returns
    // its promise so concurrent triggers coalesce.
    _loadLod: function(key) {
      if (!this.options.lodUrls || !this.options.lodUrls[key]) return Promise.resolve();
      if (this._lodCache[key]) {
        this._activateLod(key);
        return Promise.resolve();
      }
      if (this._lodInflight[key]) return this._lodInflight[key];
      var self = this;
      // M-010: cancellable transfer (see _abortLods).
      var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
      if (ctrl) this._lodAbort[key] = ctrl;
      var timer;
      var deadline = new Promise(function(_, reject) {
        timer = setTimeout(function() {
          var error = new Error('Transit request timed out'); error.name = 'TimeoutError';
          reject(error);
          if (ctrl) ctrl.abort();
        }, 20000);
        if (ctrl) ctrl.signal.addEventListener('abort', function() {
          var error = new Error('Transit request cancelled'); error.name = 'AbortError';
          reject(error);
        }, {once: true});
      });
      // M-193: the three lifecycle events map.py's FAB wiring listens on.
      // _loading was request de-duplication only and drove no UI at all, so
      // a slow or failed LOD looked exactly like "this area has no lines".
      this.fire('lodloadstart', { key: key });
      var request = Promise.resolve().then(function() {
          return fetch(self.options.lodUrls[key], ctrl ? { signal: ctrl.signal } : undefined);
        })
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        });
      var p = Promise.race([request, deadline])
        .then(function(gj) {
          // M-010: the layer was removed while this was in flight (its
          // caches are already cleared) — parsing now would re-inflate the
          // heap we just gave back, for data nobody is looking at.
          if (!self._map) { self.fire('lodload', { key: key }); return; }
          var data = { lines: [], stations: [], lineIndex: new Map() };
          self._parseInto(gj, data.lines, data.stations, data.lineIndex);
          self._lodCache[key] = data;
          // Only activate if the user is still in this LOD's zoom band.
          // Otherwise just cache for when they come back — activating
          // out-of-band would briefly render the wrong detail level.
          if (self._targetLodKey() === key) {
            self._activateLod(key);
          }
          self.fire('lodload', { key: key });
        })
        .catch(function(e) {
          // M-010: a cancel is a decision we made, not a failure — no
          // console noise, no error event, no FAB rollback.
          if (e && e.name === 'AbortError') return;
          console.error('[TransitLayer] LOD ' + key + ' load failed:', e);
          // BUG-16: hasData says whether anything is still on screen. A
          // failed upgrade over a working LOD must not knock the FAB out.
          self.fire('lodloaderror',
                    { key: key, error: e, hasData: !!self._currentLodKey });
        })
        .finally(function() {
          clearTimeout(timer);
          if (self._lodInflight[key] === p) delete self._lodInflight[key];
          if (self._lodAbort[key] === ctrl) delete self._lodAbort[key];
        });
      this._lodInflight[key] = p;
      return p;
    },

    // Switch the active feature set to the given LOD. Tears down the
    // currently-rendered polylines (they belong to the previous LOD's
    // features and have to be rebuilt on the new geometry) and points
    // _allLines / _allStations / _lineIndex at the cached LOD data.
    _activateLod: function(key) {
      var data = this._lodCache[key];
      if (!data || this._currentLodKey === key) return;
      this._teardownActiveLayers();
      this._allLines = data.lines;
      this._allStations = data.stations;
      this._lineIndex = data.lineIndex;
      this._currentLodKey = key;
      this._loaded = true;
      // Force the next redraw to re-evaluate everything — the casing
      // threshold and zoom-vs-minZ gates are LOD-independent, but
      // _lastZoom/_lastDrawCasing should not be considered "still
      // current" across a LOD swap.
      this._lastZoom = null;
      this._lastDrawCasing = null;
      if (this._map) this._scheduleRedraw();
    },

    // Called inside the rAF wrapper before each redraw — if the current
    // zoom calls for a different LOD than the one loaded, kick off the
    // fetch. The redraw proceeds with the existing LOD; the fresh one
    // takes over when its fetch resolves and triggers another redraw.
    _maybeSwapLod: function() {
      if (!this.options.lodUrls || !this.options.lodBreaks) return;
      var target = this._targetLodKey();
      if (!target || target === this._currentLodKey) return;
      // M-009: never spend a download to move to a COARSER LOD. Zooming out
      // from z15 to z8, or switching the city FAB off at z15, used to refetch
      // a file strictly less detailed than the one already parsed. If the
      // coarse LOD is already cached the swap is free (and worth it — the
      // renderer walks fewer candidates); if it isn't, keep what we have.
      if (this._currentLodKey &&
          LOD_RANK[target] < LOD_RANK[this._currentLodKey] &&
          !this._lodCache[target]) return;
      // M-010: whatever else was downloading is now dead weight.
      this._abortLods(target);
      this._loadLod(target);
    },

    _indexLine: function(f) {
      var coords = f.geometry.coordinates;
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (var i = 0; i < coords.length; i++) {
        var x = coords[i][0], y = coords[i][1];
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      }
      f._bbox = [minX, minY, maxX, maxY];
      var cls = classify(f.properties);
      f._class = cls;
      // Bucket is decided by is_longhaul (set by transit_postprocess.py),
      // not by the visual class. Otherwise a 南海本線 way carrying サザン
      // gets visually classified as 'private' (city bucket) even though the
      // postprocess pass tagged is_longhaul=true, and stations end up
      // disagreeing with lines about which filter they belong to.
      f._bucket = f.properties.is_longhaul ? 'long' : 'city';
      f._color = colorFor(f.properties, cls);
      f._drawColor = f._bucket === 'city' ? muteColor(f._color) : f._color;
      f._imp = CLASSES[cls].importance;
      f._label = pickLineLabel(f.properties) || '';
      var ll = new Array(coords.length);
      for (var j = 0; j < coords.length; j++) ll[j] = [coords[j][1], coords[j][0]];
      f._latlngs = ll;
      f.geometry = null;    // free the [lon,lat] copy
      f.properties = null;  // M-010: every field above is now derived; the
                            // OSM property bag has no reader left

      var GRID = this.options.grid;
      var gx0 = Math.floor(minX / GRID), gx1 = Math.floor(maxX / GRID);
      var gy0 = Math.floor(minY / GRID), gy1 = Math.floor(maxY / GRID);
      for (var gx = gx0; gx <= gx1; gx++) {
        for (var gy = gy0; gy <= gy1; gy++) {
          var k = gx + ',' + gy;
          var cell = this._lineIndex.get(k);
          if (!cell) { cell = []; this._lineIndex.set(k, cell); }
          cell.push(f);
        }
      }
    },

    _visibleLines: function() {
      var b = this._map.getBounds().pad(0.15);
      var W = b.getWest(), E = b.getEast(), S = b.getSouth(), N = b.getNorth();
      var GRID = this.options.grid;
      var gx0 = Math.floor(W / GRID), gx1 = Math.floor(E / GRID);
      var gy0 = Math.floor(S / GRID), gy1 = Math.floor(N / GRID);
      var seen = new Set();
      var out = [];
      for (var gx = gx0; gx <= gx1; gx++) {
        for (var gy = gy0; gy <= gy1; gy++) {
          var cell = this._lineIndex.get(gx + ',' + gy);
          if (!cell) continue;
          for (var i = 0; i < cell.length; i++) {
            var f = cell[i];
            if (seen.has(f)) continue;
            seen.add(f);
            var bb = f._bbox;
            if (bb[2] < W || bb[0] > E || bb[3] < S || bb[1] > N) continue;
            out.push(f);
          }
        }
      }
      return out;
    },

    _ensurePolylines: function(f) {
      if (f._pl) return;
      var cls = CLASSES[f._class];
      var op = this.options.opacity;
      var cop = this.options.casingOpacity;
      f._pl_casing = L.polyline(f._latlngs, {
        renderer: this._rLines, color: '#ffffff', weight: cls.w + 1.6,
        opacity: cop, lineCap: 'round', lineJoin: 'round', interactive: false
      });
      f._pl = L.polyline(f._latlngs, {
        renderer: this._rLines, color: f._drawColor || f._color, weight: cls.w,
        opacity: f._bucket === 'city' ? this.options.cityOpacity : op,
        lineCap: 'round', lineJoin: 'round', interactive: false
      });
      // No hit polyline: hover resolution runs in coordinate space against
      // the visible set (_hitTest) — the old invisible weight-14 copy of
      // every line cost a seventh full-viewport canvas that repainted for
      // nothing on every pan.
    },

    _scheduleRedraw: function() {
      if (this._rafToken) return;
      var self = this;
      this._rafToken = requestAnimationFrame(function() {
        self._rafToken = 0;
        // Check LOD first so a zoom crossing kicks off the right fetch.
        // No-op in legacy single-file mode.
        self._maybeSwapLod();
        self._redraw();
      });
    },

    _redraw: function() {
      if (!this._map) return;
      var zoom = this._map.getZoom();
      var drawCasing = zoom >= 9;
      var zoomChanged = zoom !== this._lastZoom;
      var casingChanged = drawCasing !== this._lastDrawCasing;
      var zoomBoost = Math.max(0, (zoom - 10) * 0.18);

      var desired = new Set();
      var candidates = this._visibleLines();
      var bk = this._buckets;
      for (var i = 0; i < candidates.length; i++) {
        var f = candidates[i];
        var cls = CLASSES[f._class];
        if (zoom < cls.minZ) continue;
        if (f._bucket === 'long' && !bk.long) continue;
        if (f._bucket === 'city' && !bk.city) continue;
        desired.add(f);
      }

      // Membership diff. Any change to the visible set (or the casing
      // threshold) triggers a full re-attach in paint order: casings
      // first (bottom), then lines by importance ascending — insertion
      // order IS z-order inside the single canvas. The canvas repaints
      // once either way (Leaflet coalesces via rAF), so the re-attach
      // costs only bookkeeping, not extra raster passes.
      var self = this;
      var membershipChanged = false;
      this._onMap.forEach(function(f) {
        if (!desired.has(f)) membershipChanged = true;
      });
      if (!membershipChanged) {
        desired.forEach(function(f) {
          if (!self._onMap.has(f)) membershipChanged = true;
        });
      }

      if (membershipChanged || casingChanged) {
        this._onMap.forEach(function(f) {
          if (f._pl && f._pl._map) f._pl.remove();
          if (f._pl_casing && f._pl_casing._map) f._pl_casing.remove();
        });
        var list = [];
        desired.forEach(function(f) { list.push(f); });
        list.sort(function(a, b) { return a._imp - b._imp; });
        var i, f;
        if (drawCasing) {
          for (i = 0; i < list.length; i++) {
            f = list[i];
            self._ensurePolylines(f);
            f._pl_casing.setStyle({ weight: CLASSES[f._class].w + zoomBoost + 1.6 });
            f._pl_casing.addTo(self._map);
          }
        }
        for (i = 0; i < list.length; i++) {
          f = list[i];
          self._ensurePolylines(f);
          f._pl.setStyle({ weight: CLASSES[f._class].w + zoomBoost });
          f._pl.addTo(self._map);
        }
        this._onMap = desired;
      } else if (zoomChanged) {
        // Same set, new zoom — restyle in place.
        desired.forEach(function(f) {
          var c = CLASSES[f._class];
          f._pl.setStyle({ weight: c.w + zoomBoost });
          if (f._pl_casing && f._pl_casing._map) {
            f._pl_casing.setStyle({ weight: c.w + zoomBoost + 1.6 });
          }
        });
      }

      this._lastZoom = zoom;
      this._lastDrawCasing = drawCasing;

      // Stations: DIFFED against the previous frame — the old code did
      // clearLayers() + full recreate on every moveend, which at z>=14 in
      // central Tokyo destroyed and rebuilt 100+ SVG circles plus their
      // permanent tooltip DOM nodes per pan (GC churn + a layout storm at
      // the end of every drag). Now a pan only touches the stations that
      // actually entered or left the viewport; existing dots get a cheap
      // setRadius when the zoom band shifts, and only a permanent-label
      // flip (z14 crossing) forces a rebind of that one marker.
      // Transfer hubs get a noticeably bigger circle so they read at a
      // glance: line_count >= 6 ("mega-hub" — 渋谷 / 新宿 / 上野 / 池袋 /
      // 京都...) and >= 3 ("regular hub") are precomputed by
      // transit_postprocess.py. Stations on the wrong bucket — e.g., a
      // pure shinkansen-only halt while the 长途 toggle is off — get
      // skipped entirely so the dots don't outlive their lines.
      var want = new Map();
      if (zoom >= 12) {
        var b2 = this._map.getBounds().pad(0.1);
        var W2 = b2.getWest(), E2 = b2.getEast(), S2 = b2.getSouth(), N2 = b2.getNorth();
        // Permanent-label tiering: z14 labels only transfer hubs (the
        // stations people navigate by); every station gets its label at
        // z15+, and hover always works. Flat z14 labeling put 150-220
        // white pills over central Tokyo — over the restaurant markers
        // this was most of the "unreadable map" complaint.
        var labelAll  = zoom >= 15;
        var labelHubs = zoom >= 14;
        for (var s = 0; s < this._allStations.length; s++) {
          var stn = this._allStations[s];   // M-010: flat record from _parseInto
          var lon = stn.lon;
          var lat = stn.lat;
          if (lon < W2 || lon > E2 || lat < S2 || lat > N2) continue;
          // Hide the dot if its only nearby lines belong to a bucket that's
          // off. Legacy stations without the per-bucket flags fall back to
          // "show if any bucket is on" so old geojsons keep working.
          var sHasLong = stn.has_long_line;
          var sHasCity = stn.has_city_line;
          var hasFlags = (typeof sHasLong !== 'undefined') ||
                         (typeof sHasCity !== 'undefined');
          var visibleByBucket = hasFlags
            ? ((sHasLong && bk.long) || (sHasCity && bk.city))
            : (bk.long || bk.city);
          if (!visibleByBucket) continue;
          var lc = stn.line_count | 0;
          var radius;
          if (lc >= 6)      radius = zoom >= 15 ? 8 : zoom >= 13 ? 6.5 : 5.5;
          else if (lc >= 3) radius = zoom >= 15 ? 6 : zoom >= 13 ? 5   : 4.2;
          else              radius = zoom >= 15 ? 4 : zoom >= 13 ? 3.2 : 2.6;
          var perm = labelAll || (labelHubs && lc >= 3);
          want.set(stn, { radius: radius, showLabel: perm, lat: lat, lon: lon });
        }
      }
      var stOn = this._stationsOn;
      var stLayer = this._stationsLayer;
      var stRemove = [];
      stOn.forEach(function(rec, stn) { if (!want.has(stn)) stRemove.push(stn); });
      for (var r2 = 0; r2 < stRemove.length; r2++) {
        stLayer.removeLayer(stOn.get(stRemove[r2]).marker);
        stOn.delete(stRemove[r2]);
      }
      var op2 = this.options.opacity;
      want.forEach(function(p, stn) {
        var rec = stOn.get(stn);
        if (rec && rec.showLabel === p.showLabel) {
          if (rec.radius !== p.radius) {
            rec.marker.setRadius(p.radius);
            rec.radius = p.radius;
          }
          return;
        }
        if (rec) {
          // Label mode flipped (crossed z14) — rebuild just this marker so
          // the tooltip's permanent-ness matches.
          stLayer.removeLayer(rec.marker);
          stOn.delete(stn);
        }
        var lc2 = stn.line_count | 0;
        var isTram = stn.railway === 'tram_stop';
        var isHub = lc2 >= 3;
        var dot = L.circleMarker([p.lat, p.lon], {
          radius: p.radius,
          weight: isHub ? 2 : 1.5,
          color: isTram ? '#c62828' : (isHub ? '#111' : '#222'),
          fillColor: isHub ? '#fffbea' : '#ffffff',
          fillOpacity: op2,
          opacity: op2
        });
        var nm = pickStationName(stn);   // M-010: reads .name / .name_en
        if (nm) {
          var opts = { className: 'transit-station-label' };
          if (p.showLabel) {
            opts.permanent = true;
            opts.direction = 'top';
            opts.offset = [0, -4];
          }
          var label = nm;
          if (lc2 >= 3) label = nm + '  (' + lc2 + LINES_SUFFIX + ')';
          dot.bindTooltip(label, opts);
        }
        stLayer.addLayer(dot);
        stOn.set(stn, { marker: dot, radius: p.radius, showLabel: p.showLabel });
      });
    },

    // ---- line-name hover, no hit canvas ---------------------------------
    // Point-to-segment distance against the *visible* line set, in a
    // screen-proportional coordinate frame (lng, lat/cos φ). Throttled to
    // ~25 fps; bbox pre-check skips almost every line per move.
    _hideHover: function() {
      if (this._hoverOpen && this._hoverTip) this._hoverTip.remove();
      this._hoverOpen = false;
    },
    _onMouseMove: function(e) {
      var now = Date.now();
      if (now - this._lastHover < 40) return;
      this._lastHover = now;
      var hit = this._hitTest(e.latlng);
      if (hit && hit._label) {
        this._hoverTip.setContent(hit._label);
        this._hoverTip.setLatLng(e.latlng);
        if (!this._hoverOpen) {
          this._hoverTip.addTo(this._map);
          this._hoverOpen = true;
        }
      } else {
        this._hideHover();
      }
    },
    _hitTest: function(latlng) {
      if (!this._map) return null;
      var degPerPx = 360 / (256 * Math.pow(2, this._map.getZoom()));
      var tol = 8 * degPerPx;                       // ~8px, in lng-degrees
      var cosLat = Math.cos(latlng.lat * Math.PI / 180);
      if (cosLat < 1e-6) cosLat = 1e-6;
      var ux = latlng.lng, uy = latlng.lat / cosLat;
      var best = null, bestD = tol * tol;
      var latTol = tol * cosLat;
      var it = this._onMap.values(), v;
      while (!(v = it.next()).done) {
        var f = v.value;
        var bb = f._bbox;   // [minLng, minLat, maxLng, maxLat]
        if (latlng.lng < bb[0] - tol   || latlng.lng > bb[2] + tol)   continue;
        if (latlng.lat < bb[1] - latTol || latlng.lat > bb[3] + latTol) continue;
        var ll = f._latlngs;   // [ [lat, lng], ... ]
        for (var i = 1; i < ll.length; i++) {
          var d = this._segDist2(
            ux, uy,
            ll[i - 1][1], ll[i - 1][0] / cosLat,
            ll[i][1],     ll[i][0] / cosLat);
          if (d < bestD) { bestD = d; best = f; }
        }
      }
      return best;
    },
    _segDist2: function(px, py, ax, ay, bx, by) {
      var dx = bx - ax, dy = by - ay;
      var len2 = dx * dx + dy * dy;
      var t = 0;
      if (len2 > 0) {
        t = ((px - ax) * dx + (py - ay) * dy) / len2;
        if (t < 0) t = 0; else if (t > 1) t = 1;
      }
      var ex = ax + t * dx - px, ey = ay + t * dy - py;
      return ex * ex + ey * ey;
    }
  });

  L.transitLayer = function(options) { return new L.TransitLayer(options); };
})();
