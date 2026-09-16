/* L.TransitLayer — a Leaflet overlay that renders Japan's railway network
   and a separately loadable station layer.

   Usage:
     var layer = new L.TransitLayer({ geojsonUrl: 'transit/japan.geojson',
                                      stationUrl: 'transit/japan-stations.json',
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
  function pickLineLabel(props) {
    if (ACTIVE_LANG === 'en') {
      return props.route_name_en || props.name_en
          || props.route_name    || props.name;
    }
    return props.route_name || props.name;
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

  // Geometry mirrors docs/img/station-tunnel-v1.svg, vectorized from the
  // user-provided tunnel/train reference. The flat two-color silhouette stays
  // legible in the 14px tier and is cached as Path2D once per layer.
  var STATION_TUNNEL_PATH = 'M5 18.5v-8C5 6.2 8 3.8 12 3.8s7 2.4 7 6.7v8';
  var STATION_TRAIN_BODY_PATH = 'M8.2 8.2c0-1.1.9-2 2-2h3.6c1.1 0 2 .9 2 2v6.2c0 1.2-1 2.2-2.2 2.2h-3.2c-1.2 0-2.2-1-2.2-2.2z';
  var STATION_RAIL_PATH = 'M9.9 16.1l-2 3.2m6.2-3.2 2 3.2';
  var STATION_TIE_PATH = 'M9.35 18.05h5.3l.65 1.1H8.7z';
  var STATION_BADGE_URL = 'img/station-icon-v2.png?v=c98b8c002d';
  var STATION_BADGE_CROP = [185, 182, 885, 887];
  var STATION_SIZES = [11, 14, 18];
  var STATION_MIN_ZOOM = 12;
  var STATION_DPR_MAX = 3;
  var STATION_LABEL_BASE_PX = 11;
  var STATION_LABEL_HALO_PX = 3;
  var STATION_ICON_GUARD_PX = 2;
  var STATION_FONT_STACK = 'system-ui,-apple-system,"Hiragino Sans","Noto Sans CJK JP",sans-serif';
  var STATION_BOUNDS = [[20, 122], [46, 154]];

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

  var StationGridLayer = L.GridLayer.extend({
    initialize: function(owner, options) {
      this._owner = owner;
      L.GridLayer.prototype.initialize.call(this, options);
    },
    createTile: function(coords) {
      return this._owner._createStationTile(coords, this.getTileSize());
    },
    onRemove: function(map) {
      L.GridLayer.prototype.onRemove.call(this, map);
      this._owner = null;
    }
  });

  L.TransitLayer = L.Layer.extend({
    options: {
      // Legacy single-file mode. Used iff lodUrls is not set.
      geojsonUrl: 'transit/japan.geojson',
      // Independent station payload. Production passes its content-addressed
      // URL; the local name remains a development fallback.
      stationUrl: 'transit/japan-stations.json',
      stationPane: 'transitStations',
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
      grid: 0.4,            // line-index cell size in degrees
      stationGrid: 0.1,     // point-index cell size (~9-11 km in Japan)
      stationMinZoom: STATION_MIN_ZOOM,
      stationMaxZoom: null,
      stationBounds: STATION_BOUNDS,
      stationKeepBuffer: 2,
      stationProfileKey: null,
      stationUiDensity: 1,
      stationFontScale: 100,
      stationFont: STATION_FONT_STACK
    },

    initialize: function(options) {
      L.Util.setOptions(this, options);
      // Cached across add/remove cycles
      this._loaded = false;
      this._loading = false;
      this._allLines = [];
      this._allStations = [];
      this._lineIndex = new Map();
      this._stationIndex = new Map();
      this._stationLoaded = false;
      this._stationLoading = false;
      this._stationInflight = null;
      this._stationAbort = null;
      this._stationError = null;
      this._stationRequestGeneration = 0;
      this._stationGridLayer = null;
      this._stationPayloadMeta = null;
      this._stationPlacement = null;
      this._stationPlacementStatus = 'not-loaded';
      this._stationSuppressLabels = false;
      this._stationProfileKey = this.options.stationProfileKey ||
        (ACTIVE_LANG === 'en' ? 'en-max130' : 'local-max130');
      this._stationUiDensity = this._normaliseDensity(this.options.stationUiDensity, 1);
      this._stationFontScale = this._normaliseFontScale(this.options.stationFontScale, 100);
      this._stationFont = this.options.stationFont || STATION_FONT_STACK;
      this._stationDpr = this._effectiveStationDpr();
      this._stationSprites = {};
      this._stationBadgeImage = null;
      this._stationBadgeImageStatus = 'idle';
      this._stationMaxLabelWidth = 0;
      this._stationCounters = {
        tileCreate: 0, tileDraw: 0, tileEmpty: 0, tileRemove: 0, redraw: 0
      };
      this._stationTileDrawDurationMs = [];
      this._legacyAbort = null;
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
      this._stationsVisible = true;
      // Volatile (cleared on remove)
      this._onMap = new Set();
      this._lastZoom = null;
      this._lastDrawCasing = null;
      this._rafToken = 0;
      this._lastHover = 0;
      this._hoverOpen = false;
    },

    setVisibleBuckets: function(opts) {
      if (opts && typeof opts.long === 'boolean') this._buckets.long = opts.long;
      if (opts && typeof opts.city === 'boolean') this._buckets.city = opts.city;
      if (this._map) {
        if (this._buckets.long || this._buckets.city) {
          this._loadRail();
          this._syncLineHover(true);
        } else {
          this._releaseRailData();
          this._syncLineHover(false);
        }
        this._scheduleRedraw();
      }
      return this;
    },

    setStationsVisible: function(visible) {
      visible = !!visible;
      if (this._stationsVisible === visible) return this;
      this._stationsVisible = visible;
      if (!visible) {
        this._stationRequestGeneration += 1;
        if (this._stationAbort) {
          try { this._stationAbort.abort(); } catch (_) {}
        }
        this._stationAbort = null;
        this._stationInflight = null;
        this._stationLoading = false;
        this._clearStationMarkers();
      } else if (this._map) {
        this._maybeLoadStations();
        this._syncStationGrid();
      }
      return this;
    },

    setStationProfile: function(profile) {
      profile = profile || {};
      var key = profile.key || this._stationProfileKey;
      var density = this._normaliseDensity(profile.uiDensity, this._stationUiDensity || 1);
      var fontScale = this._normaliseFontScale(profile.fontScale, this._stationFontScale || 100);
      var font = profile.font || this._stationFont || STATION_FONT_STACK;
      if (key === this._stationProfileKey && density === this._stationUiDensity &&
          fontScale === this._stationFontScale && font === this._stationFont &&
          this._stationDpr === this._effectiveStationDpr()) return this;
      this._stationProfileKey = key;
      this._stationUiDensity = density;
      this._stationFontScale = fontScale;
      this._stationFont = font;
      this._selectStationPlacement();
      this._refreshStationTiles();
      return this;
    },

    refreshStations: function() {
      this._selectStationPlacement();
      this._refreshStationTiles();
      return this;
    },

    stationsVisible: function() {
      return this._stationsVisible;
    },

    stationCount: function() {
      return this._stationLoaded ? this._allStations.length : null;
    },

    stationDetail: function() {
      var live = this._stationTileStats();
      return {
        renderer: 'grid',
        tileZoom: this._stationGridLayer && this._stationGridLayer._tileZoom != null
          ? this._stationGridLayer._tileZoom : null,
        count: this._stationVisibleCount(),
        total: this.stationCount(),
        loaded: this._stationLoaded,
        loading: this._stationLoading,
        visible: this._stationsVisible,
        error: this._stationError,
        dpr: this._stationDpr,
        placementStatus: this._stationPlacementStatus,
        badgeImageStatus: this._stationBadgeImageStatus,
        profileKey: this._stationProfileKey,
        labelsSuppressed: this._stationSuppressLabels,
        liveCanvasCount: live.count,
        liveCanvasBytes: live.bytes,
        liveLevelCount: live.levels,
        tileDrawDurationMs: this._stationTileDrawDurationMs.slice(),
        counters: {
          tileCreate: this._stationCounters.tileCreate,
          tileDraw: this._stationCounters.tileDraw,
          tileEmpty: this._stationCounters.tileEmpty,
          tileRemove: this._stationCounters.tileRemove,
          redraw: this._stationCounters.redraw
        }
      };
    },

    retryStations: function() {
      this._stationRequestGeneration += 1;
      this._stationError = null;
      if (this._stationAbort) {
        try { this._stationAbort.abort(); } catch (_) {}
      }
      this._clearStationMarkers();
      this._allStations = [];
      this._stationIndex = new Map();
      this._stationLoaded = false;
      this._stationPlacement = null;
      this._stationPlacementStatus = 'not-loaded';
      this._stationPayloadMeta = null;
      this._stationInflight = null;
      this._stationLoading = false;
      this._ensureStationBadgeImage(true);
      if (this._map && this._stationsVisible) return this._loadStations(true);
      return Promise.resolve();
    },

    _normaliseScale: function(value, fallback) {
      value = +value;
      return isFinite(value) && value > 0 ? value : fallback;
    },

    _normaliseDensity: function(value, fallback) {
      value = this._normaliseScale(value, fallback);
      return value > 10 ? value / 100 : value;
    },

    _normaliseFontScale: function(value, fallback) {
      value = this._normaliseScale(value, fallback);
      return value <= 3 ? value * 100 : value;
    },

    _effectiveStationDpr: function() {
      // Three device pixels per CSS pixel preserves high-density detail while
      // bounding each 256px tile at 2.25 MiB of RGBA backing memory.
      var value = +window.devicePixelRatio || 1;
      return Math.min(STATION_DPR_MAX, Math.max(1, value));
    },

    _stationTileStats: function() {
      var grid = this._stationGridLayer;
      if (!grid) return {count: 0, bytes: 0, levels: 0};
      var count = 0, bytes = 0;
      var tiles = grid._tiles || {};
      Object.keys(tiles).forEach(function(key) {
        var tile = tiles[key] && tiles[key].el;
        if (tile && tile.dataset && tile.dataset.stationTile === '1') {
          count += 1;
          bytes += +tile.dataset.stationTileBytes || 0;
        }
      });
      return {count: count, bytes: bytes, levels: Object.keys(grid._levels || {}).length};
    },

    _stationStyleScale: function() {
      return Math.max(0.75, Math.min(1, this._stationUiDensity || 1));
    },

    _stationFontPx: function() {
      var scale = (this._stationFontScale || 100) / 100;
      return STATION_LABEL_BASE_PX * this._stationStyleScale() * Math.max(0.75, Math.min(1.3, scale));
    },

    _stationName: function(station) {
      return this._stationProfileKey === 'en-max130'
        ? (station.name_en || station.name || '') : (station.name || '');
    },

    _selectStationPlacement: function() {
      var payload = this._stationPayloadMeta;
      var placement = payload && payload.placement;
      var rowsLength = this._allStations.length;
      this._stationPlacement = null;
      this._stationSuppressLabels = false;
      if (!payload || payload.v !== 2 || !placement) {
        this._stationPlacementStatus = payload ? 'fallback-legacy-v1' : 'not-loaded';
      } else if (placement.version !== 1 || placement.zoomMin !== 12 ||
                 placement.zoomMax !== 19 || !placement.profiles) {
        this._stationPlacementStatus = 'fallback-placement-version';
        this._stationSuppressLabels = true;
      } else if (!this._stationMeasurementValid(placement.measurement)) {
        this._stationPlacementStatus = 'fallback-invalid-metadata';
        this._stationSuppressLabels = true;
      } else if (this._stationUiDensity > 1 || this._stationFontScale > 130 ||
                 this._stationFont !== STATION_FONT_STACK) {
        this._stationPlacementStatus = 'fallback-profile-range';
        this._stationSuppressLabels = true;
      } else {
        var profile = placement.profiles[this._stationProfileKey];
        if (!profile) {
          this._stationPlacementStatus = 'fallback-profile-miss';
          this._stationSuppressLabels = true;
        } else if (!Array.isArray(profile.visibleMaskByItem) ||
                   !Array.isArray(profile.labelWidthByItem) ||
                   profile.visibleMaskByItem.length !== rowsLength ||
                   profile.labelWidthByItem.length !== rowsLength) {
          this._stationPlacementStatus = 'fallback-invalid-profile';
          this._stationSuppressLabels = true;
        } else {
          var validProfile = true;
          var maskLimit = (1 << (placement.zoomMax - placement.zoomMin + 1)) - 1;
          for (var pi = 0; pi < rowsLength; pi++) {
            var mask = profile.visibleMaskByItem[pi];
            var profileWidth = profile.labelWidthByItem[pi];
            if (typeof mask !== 'number' || mask !== (mask | 0) || mask < 0 || mask > maskLimit ||
                typeof profileWidth !== 'number' || !isFinite(profileWidth) || profileWidth < 0) {
              validProfile = false;
              break;
            }
          }
          if (validProfile) {
            this._stationPlacement = profile;
            this._stationPlacementStatus = 'ready';
          } else {
            this._stationPlacementStatus = 'fallback-invalid-profile';
            this._stationSuppressLabels = true;
          }
        }
      }
      var maxWidth = 0;
      if (this._stationPlacement) {
        var widths = this._stationPlacement.labelWidthByItem;
        for (var i = 0; i < widths.length; i++) {
          var width = +widths[i];
          if (isFinite(width) && width > maxWidth) maxWidth = width;
        }
      } else {
        var fontPx = this._stationFontPx();
        for (var j = 0; j < this._allStations.length; j++) {
          var text = this._stationName(this._allStations[j]);
          var fallbackWidth = Array.from(text).length * fontPx * 1.2;
          if (fallbackWidth > maxWidth) maxWidth = fallbackWidth;
        }
      }
      this._stationMaxLabelWidth = maxWidth;
    },

    _stationMeasurementValid: function(measurement) {
      if (!measurement || measurement.baseFontCssPx !== STATION_LABEL_BASE_PX ||
          measurement.fontWeight !== 600 || measurement.measuredAtFontScale !== 130 ||
          measurement.measuredAtUiDensity !== 1 ||
          measurement.measurementCanvasFont !== '600 14.3px ' + STATION_FONT_STACK ||
          measurement.haloCssPx !== STATION_LABEL_HALO_PX ||
          measurement.labelGapCssPx !== 3 ||
          measurement.iconGuardCssPx !== STATION_ICON_GUARD_PX ||
          measurement.widthIncludesHalo !== false ||
          !measurement.fontStacks) return false;
      return measurement.fontStacks['local-max130'] === STATION_FONT_STACK &&
             measurement.fontStacks['en-max130'] === STATION_FONT_STACK;
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
      var stationPane = map.getPane(this.options.stationPane) || map.createPane(this.options.stationPane);
      stationPane.style.zIndex = '450';
      stationPane.style.pointerEvents = 'none';
      this._scheduleRedrawBound = this._scheduleRedraw.bind(this);
      map.on('moveend', this._scheduleRedrawBound);
      this._stationResizeBound = this._onStationEnvironmentChange.bind(this);
      map.on('resize', this._stationResizeBound);
      // Line-name hover, pointer devices only — touch never hovers, so
      // phones skip the listener (and its per-move segment math) entirely.
      this._syncLineHover(this._buckets.long || this._buckets.city);
      this._load();
      this._scheduleRedraw();
      return this;
    },

    onRemove: function(map) {
      if (this._scheduleRedrawBound) {
        map.off('moveend', this._scheduleRedrawBound);
        this._scheduleRedrawBound = null;
      }
      if (this._stationResizeBound) {
        map.off('resize', this._stationResizeBound);
        this._stationResizeBound = null;
      }
      if (this._onMouseMoveBound) {
        map.off('mousemove', this._onMouseMoveBound);
        map.off('mouseout', this._onMouseOutBound);
        this._onMouseMoveBound = this._onMouseOutBound = null;
      }
      if (this._onStationMouseMoveBound) {
        map.off('mousemove', this._onStationMouseMoveBound);
        map.off('mouseout', this._onStationMouseOutBound);
        this._onStationMouseMoveBound = this._onStationMouseOutBound = null;
      }
      if (this._onStationClickBound) {
        map.off('click', this._onStationClickBound);
        this._onStationClickBound = null;
      }
      if (this._stationHoverTip) this._stationHoverTip.remove();
      this._stationHoverTip = null;
      this._hideHover();
      this._hoverTip = null;
      if (this._rafToken) {
        cancelAnimationFrame(this._rafToken);
        this._rafToken = 0;
      }
      this._teardownActiveLayers();
      if (this._rLines) this._rLines.remove();
      this._rLines = null;
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
      this._stationRequestGeneration += 1;
      if (this._stationAbort) {
        try { this._stationAbort.abort(); } catch (_) {}
      }
      this._stationAbort = null;
      this._stationInflight = null;
      if (this._legacyAbort) {
        try { this._legacyAbort.abort(); } catch (_) {}
      }
      this._legacyAbort = null;
      this._lodCache = {};
      this._lodInflight = {};
      this._allLines = [];
      this._allStations = [];
      this._lineIndex = new Map();
      this._stationIndex = new Map();
      this._stationLoaded = false;
      this._stationLoading = false;
      this._stationError = null;
      this._stationPlacement = null;
      this._stationPlacementStatus = 'not-loaded';
      this._stationPayloadMeta = null;
      this._stationSprites = {};
      this._stationMaxLabelWidth = 0;
      this._stationTileDrawDurationMs = [];
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

    _clearLineLayers: function() {
      var it = this._onMap.values(), v;
      while (!(v = it.next()).done) {
        var f = v.value;
        if (f._pl) { if (f._pl._map) f._pl.remove(); f._pl = null; }
        if (f._pl_casing) {
          if (f._pl_casing._map) f._pl_casing.remove();
          f._pl_casing = null;
        }
      }
      this._onMap.clear();
      if (this._rLines) {
        this._rLines.remove();
        this._rLines = null;
      }
      this._hideHover();
    },

    _releaseRailData: function() {
      this._abortLods(null);
      if (this._legacyAbort) {
        try { this._legacyAbort.abort(); } catch (_) {}
        this._legacyAbort = null;
      }
      this._clearLineLayers();
      this._lodCache = {};
      this._lodInflight = {};
      this._allLines = [];
      this._lineIndex = new Map();
      this._currentLodKey = null;
      this._loaded = false;
      this._loading = false;
      this._lastZoom = null;
      this._lastDrawCasing = null;
    },

    _syncLineHover: function(enabled) {
      if (!this._map) return;
      var canHover = window.matchMedia && matchMedia('(hover: hover)').matches;
      if (enabled && canHover && !this._onMouseMoveBound) {
        this._onMouseMoveBound = this._onMouseMove.bind(this);
        this._onMouseOutBound = this._hideHover.bind(this);
        this._map.on('mousemove', this._onMouseMoveBound);
        this._map.on('mouseout', this._onMouseOutBound);
        this._hoverTip = L.tooltip({
          className: 'transit-line-label', direction: 'top',
          offset: [0, -8], opacity: 1
        });
      } else if ((!enabled || !canHover) && this._onMouseMoveBound) {
        this._map.off('mousemove', this._onMouseMoveBound);
        this._map.off('mouseout', this._onMouseOutBound);
        this._onMouseMoveBound = this._onMouseOutBound = null;
        this._hideHover();
        this._hoverTip = null;
      }
    },

    // Detach polylines for everything currently on the map and drop the
    // refs. The polylines belong to the renderer we may be about to throw
    // away (onRemove) or to a soon-to-be-swapped LOD (LOD switch), so
    // holding onto them would pin dead state. Station markers belong to
    // the active LOD's features too, so the registry goes with them.
    _teardownActiveLayers: function() {
      this._clearLineLayers();
      this._clearStationMarkers();
      this._hideHover();
    },

    _clearStationMarkers: function() {
      this._destroyStationGrid();
      this._syncStationInteraction(false);
      this._hideStationHover();
    },

    _destroyStationGrid: function() {
      var grid = this._stationGridLayer;
      if (!grid) return;
      if (grid._map) grid.remove();
      grid.off('tileunload', this._onStationTileUnload, this);
      this._stationGridLayer = null;
      this._stationSprites = {};
    },

    _baseTileOptions: function() {
      var found = null;
      if (this._map) {
        this._map.eachLayer(function(layer) {
          if (!found && L.TileLayer && layer instanceof L.TileLayer) found = layer.options || {};
        });
      }
      return found || {};
    },

    _syncStationGrid: function() {
      if (!this._map) return;
      var zoom = this._map.getZoom();
      if (!this._stationsVisible || !this._stationLoaded || zoom < this.options.stationMinZoom) {
        this._destroyStationGrid();
        this._syncStationInteraction(false);
        return;
      }
      this._ensureStationBadgeImage(false);
      this._onStationEnvironmentChange();
      if (!this._stationGridLayer) {
        var base = this._baseTileOptions();
        var mapMax = this._map.getMaxZoom();
        var maxZoom = this.options.stationMaxZoom;
        if (maxZoom == null || !isFinite(maxZoom)) maxZoom = isFinite(mapMax) ? mapMax : 19;
        var updateWhenIdle = typeof base.updateWhenIdle === 'boolean'
          ? base.updateWhenIdle : !!L.Browser.mobile;
        this._stationGridLayer = new StationGridLayer(this, {
          pane: this.options.stationPane,
          minZoom: this.options.stationMinZoom,
          maxZoom: maxZoom,
          bounds: this.options.stationBounds,
          noWrap: true,
          keepBuffer: Math.max(0, this.options.stationKeepBuffer | 0),
          updateWhenIdle: updateWhenIdle,
          updateWhenZooming: false
        });
        this._stationGridLayer.on('tileunload', this._onStationTileUnload, this);
        this._stationGridLayer.addTo(this._map);
      }
      this._syncStationInteraction(zoom < 14);
    },

    _refreshStationTiles: function() {
      this._stationDpr = this._effectiveStationDpr();
      this._stationSprites = {};
      if (this._stationGridLayer) {
        this._stationCounters.redraw += 1;
        this._stationGridLayer.redraw();
      } else {
        this._syncStationGrid();
      }
    },

    _onStationEnvironmentChange: function() {
      var dpr = this._effectiveStationDpr();
      if (dpr !== this._stationDpr) {
        this._stationDpr = dpr;
        this._stationSprites = {};
        if (this._stationGridLayer) {
          this._stationCounters.redraw += 1;
          this._stationGridLayer.redraw();
        }
      }
    },

    _load: function() {
      if (this._buckets.long || this._buckets.city) this._loadRail();
      this._maybeLoadStations();
    },

    _loadRail: function() {
      if (this.options.lodUrls && this.options.lodBreaks) {
        // LOD mode: figure out the current zoom's target LOD and load it.
        var target = this._targetLodKey() || 'low';
        return this._loadLod(target);
      } else {
        return this._loadLegacy();
      }
    },

    _loadLegacy: function() {
      if (this._loaded || this._loading) return Promise.resolve();
      this._loading = true;
      var self = this;
      var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      if (ctrl) this._legacyAbort = ctrl;
      return fetch(this.options.geojsonUrl, ctrl ? { signal: ctrl.signal } : undefined)
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(gj) {
          if (!self._map || (self._buckets &&
              !self._buckets.long && !self._buckets.city)) return;
          self._parseInto(gj, self._allLines, self._allStations, self._lineIndex);
          if (!self.options.stationUrl && self._allStations.length) {
            self._indexStations(self._allStations);
            self._stationLoaded = true;
          }
          self._loaded = true;
          self._loading = false;
          if (self._map) self._scheduleRedraw();
        })
        .catch(function(e) {
          if (e && e.name === 'AbortError') return;
          console.error('[TransitLayer] load failed:', e);
          self._loading = false;
        })
        .finally(function() {
          if (self._legacyAbort === ctrl) self._legacyAbort = null;
        });
    },

    _maybeLoadStations: function() {
      if (!this._map || !this._stationsVisible || this._stationLoaded ||
          this._stationLoading) return;
      // A nationwide cloud of points has no useful country-scale rendering.
      // Delay both transfer and JSON.parse until markers can actually appear.
      if (this._map.getZoom() < this.options.stationMinZoom) return;
      this._loadStations(false);
    },

    _loadStations: function(force) {
      if (!this.options.stationUrl) return Promise.resolve();
      if (this._stationLoaded && !force) return Promise.resolve();
      if (this._stationInflight) return this._stationInflight;
      var self = this;
      var generation = ++this._stationRequestGeneration;
      var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      if (ctrl) this._stationAbort = ctrl;
      this._stationLoading = true;
      this._stationError = null;
      this.fire('stationloadstart');
      var timer;
      var deadline = new Promise(function(_, reject) {
        timer = setTimeout(function() {
          var error = new Error('Station request timed out'); error.name = 'TimeoutError';
          reject(error);
          if (ctrl) ctrl.abort();
        }, 15000);
      });
      var request = fetch(
        this.options.stationUrl,
        ctrl ? { signal: ctrl.signal } : undefined
      ).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
      var p = Promise.race([request, deadline])
        .then(function(payload) {
          if (generation !== self._stationRequestGeneration ||
              !self._map || !self._stationsVisible) return;
          var stations = self._parseStationPayload(payload);
          self._allStations = stations;
          self._indexStations(stations);
          self._stationPayloadMeta = payload;
          self._selectStationPlacement();
          self._stationLoaded = true;
          self._stationError = null;
          self.fire('stationload', { count: stations.length });
          self._scheduleRedraw();
        })
        .catch(function(error) {
          if (generation !== self._stationRequestGeneration) return;
          if (error && error.name === 'AbortError') return;
          self._stationError = error;
          console.error('[TransitLayer] station load failed:', error);
          self.fire('stationloaderror', {
            error: error,
            hasData: self._stationLoaded && self._allStations.length > 0
          });
        })
        .finally(function() {
          clearTimeout(timer);
          if (generation === self._stationRequestGeneration) {
            self._stationLoading = false;
            if (self._stationInflight === p) self._stationInflight = null;
            if (self._stationAbort === ctrl) self._stationAbort = null;
          }
        });
      this._stationInflight = p;
      return p;
    },

    _parseStationPayload: function(payload) {
      var out = [];
      var rows = payload && payload.stations;
      if (Array.isArray(rows)) {
        var fields = Array.isArray(payload.fields) ? payload.fields : [];
        var fieldIndex = {};
        for (var fi = 0; fi < fields.length; fi++) {
          if (typeof fields[fi] === 'string' && fieldIndex[fields[fi]] == null) {
            fieldIndex[fields[fi]] = fi;
          }
        }
        var isV2 = payload.v === 2;
        var lonAt = isV2 ? fieldIndex.lon : (fieldIndex.lon != null ? fieldIndex.lon : 0);
        var latAt = isV2 ? fieldIndex.lat : (fieldIndex.lat != null ? fieldIndex.lat : 1);
        var nameAt = isV2 ? fieldIndex.name : (fieldIndex.name != null ? fieldIndex.name : 2);
        var nameEnAt = isV2 ? fieldIndex.name_en : (fieldIndex.name_en != null ? fieldIndex.name_en : 3);
        var railwayAt = isV2 ? fieldIndex.railway : (fieldIndex.railway != null ? fieldIndex.railway : 4);
        var countAt = isV2 ? fieldIndex.line_count : (fieldIndex.line_count != null ? fieldIndex.line_count : 5);
        if (lonAt == null || latAt == null || nameAt == null || nameEnAt == null ||
            railwayAt == null || countAt == null) {
          throw new Error('Invalid station fields');
        }
        for (var i = 0; i < rows.length; i++) {
          var r = rows[i];
          if (!Array.isArray(r) || r.length < 2) {
            if (isV2) throw new Error('Invalid station row');
            continue;
          }
          var lon = +r[lonAt], lat = +r[latAt];
          if (!isFinite(lon) || !isFinite(lat)) {
            if (isV2) throw new Error('Invalid station coordinate');
            continue;
          }
          out.push({
            lon: lon, lat: lat, name: r[nameAt] || '', name_en: r[nameEnAt] || '',
            railway: r[railwayAt] || 'station', line_count: Math.max(0, r[countAt] | 0),
            _placementIndex: i
          });
        }
        return out;
      }
      // Transitional compatibility for locally generated GeoJSON fixtures.
      var features = payload && payload.features;
      if (!Array.isArray(features)) throw new Error('Invalid station payload');
      for (var j = 0; j < features.length; j++) {
        var f = features[j];
        if (!f.geometry || f.geometry.type !== 'Point') continue;
        var c = f.geometry.coordinates || [];
        var p = f.properties || {};
        var x = +c[0], y = +c[1];
        if (!isFinite(x) || !isFinite(y)) continue;
        out.push({
          lon: x, lat: y, name: p.name || '', name_en: p.name_en || '',
          railway: p.railway || 'station', line_count: Math.max(0, p.line_count | 0)
        });
      }
      return out;
    },

    _indexStations: function(stations) {
      var index = new Map();
      var grid = this.options.stationGrid;
      for (var i = 0; i < stations.length; i++) {
        var s = stations[i];
        if (s._placementIndex == null) s._placementIndex = i;
        var key = Math.floor(s.lon / grid) + ',' + Math.floor(s.lat / grid);
        var cell = index.get(key);
        if (!cell) { cell = []; index.set(key, cell); }
        cell.push(s);
      }
      this._stationIndex = index;
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
          } else if (f.geometry.type === 'Point' && !this.options.stationUrl) {
            var p = f.properties || {};
            var c = f.geometry.coordinates || [];
            stations.push({
              lon: +c[0],
              lat: +c[1],
              name: p.name,
              name_en: p.name_en,
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
          if (!self._map || (self._buckets &&
              !self._buckets.long && !self._buckets.city)) return;
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
    // _allLines / _lineIndex at the cached LOD data. Station-only state is
    // intentionally untouched.
    _activateLod: function(key) {
      var data = this._lodCache[key];
      if (!data || this._currentLodKey === key) return;
      // LOD switching is a rail-only operation. Never clear or replace the
      // independently loaded station payload/point index here; production
      // may keep using older R2 LODs that still contain ignored Point rows.
      this._clearLineLayers();
      this._allLines = data.lines;
      this._lineIndex = data.lineIndex;
      if (!this.options.stationUrl) {
        this._clearStationMarkers();
        this._allStations = data.stations;
        this._indexStations(this._allStations);
        this._stationLoaded = true;
      }
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
      if (!this._buckets.long && !this._buckets.city) return;
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
      if (!this._rLines && this._map) {
        this._rLines = new LoResCanvas({ padding: this.options.padding }).addTo(this._map);
      }
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
        self._maybeLoadStations();
        self._redraw();
        self._syncStationGrid();
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
      var bk = this._buckets;
      var candidates = (bk.long || bk.city) && this._loaded
        ? this._visibleLines() : [];
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

    },

    _roundRect: function(ctx, x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
      ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
      ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
      ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
    },

    _drawStationBadge: function(ctx, x, y, size) {
      var l = x - size / 2, t = y - size / 2, u = size / 24;
      if (this._stationBadgeImageStatus === 'ready' && this._stationBadgeImage) {
        var crop = STATION_BADGE_CROP;
        ctx.save();
        this._roundRect(ctx, l, t, size, size, size * .23);
        ctx.clip();
        ctx.drawImage(this._stationBadgeImage, crop[0], crop[1], crop[2], crop[3],
          l, t, size, size);
        ctx.restore();
        return;
      }
      this._roundRect(ctx, l + .5, t + .5, size - 1, size - 1, size * .23);
      ctx.fillStyle = '#111a43'; ctx.fill();
      if (typeof Path2D === 'function') {
        if (!this._stationTunnelPath) {
          this._stationTunnelPath = new Path2D(STATION_TUNNEL_PATH);
          this._stationTrainBodyPath = new Path2D(STATION_TRAIN_BODY_PATH);
          this._stationRailPath = new Path2D(STATION_RAIL_PATH);
          this._stationTiePath = new Path2D(STATION_TIE_PATH);
        }
        ctx.save(); ctx.translate(l, t); ctx.scale(u, u);
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.75;
        ctx.lineCap = 'round'; ctx.lineJoin = 'round';
        ctx.stroke(this._stationTunnelPath);
        ctx.fillStyle = '#fff'; ctx.fill(this._stationTrainBodyPath);
        ctx.fill(this._stationTiePath);
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.25; ctx.stroke(this._stationRailPath);
        ctx.fillStyle = '#111a43';
        this._roundRect(ctx, 9.35, 9, 5.3, 3.25, .75); ctx.fill();
        this._roundRect(ctx, 10.45, 7.15, 3.1, .7, .35); ctx.fill();
        ctx.beginPath(); ctx.arc(10, 14.25, .72, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(14, 14.25, .72, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
      }
    },

    _ensureStationBadgeImage: function(force) {
      if (!force && (this._stationBadgeImageStatus === 'loading' ||
          this._stationBadgeImageStatus === 'ready')) return;
      var owner = this;
      var image = new Image();
      this._stationBadgeImage = image;
      this._stationBadgeImageStatus = 'loading';
      image.onload = function() {
        if (owner._stationBadgeImage !== image) return;
        owner._stationBadgeImageStatus = 'ready';
        owner._stationSprites = {};
        if (owner._map && owner._stationGridLayer) owner._refreshStationTiles();
      };
      image.onerror = function() {
        if (owner._stationBadgeImage !== image) return;
        owner._stationBadgeImageStatus = 'error';
        owner._stationBadgeImage = null;
      };
      image.src = STATION_BADGE_URL;
    },

    _stationTier: function(station) {
      var count = station.line_count | 0;
      return count >= 6 ? 2 : count >= 3 ? 1 : 0;
    },

    _stationShown: function(station, zoom) {
      if (zoom < this.options.stationMinZoom) return false;
      var count = station.line_count | 0;
      if (zoom === 12) return count >= 6;
      if (zoom === 13) return count >= 3;
      return zoom >= 14;
    },

    _stationBadgeSize: function(station, zoom) {
      if (zoom <= 14) return STATION_SIZES[0] * this._stationStyleScale();
      return STATION_SIZES[this._stationTier(station)] * this._stationStyleScale();
    },

    _stationLabelVisible: function(station, zoom) {
      if (this._stationSuppressLabels) return false;
      if (this._stationPlacement) {
        var placement = this._stationPayloadMeta.placement;
        if (zoom < placement.zoomMin || zoom > placement.zoomMax) return false;
        var bit = zoom - placement.zoomMin;
        var mask = this._stationPlacement.visibleMaskByItem[station._placementIndex] | 0;
        return bit >= 0 && bit < 31 && (mask & (1 << bit)) !== 0;
      }
      var count = station.line_count | 0;
      if (zoom === 12) return false;
      if (zoom === 13) return count >= 6;
      return zoom >= 14;
    },

    _stationTemporaryNameAllowed: function(station, zoom) {
      if (zoom === 12) return this._stationShown(station, zoom);
      if (zoom === 13) return this._stationShown(station, zoom) &&
        !this._stationLabelVisible(station, zoom);
      return false;
    },

    _stationLabelWidth: function(station) {
      if (this._stationPlacement) {
        var value = +this._stationPlacement.labelWidthByItem[station._placementIndex];
        if (isFinite(value) && value >= 0) return value;
      }
      return Array.from(this._stationName(station)).length * this._stationFontPx() * 1.2;
    },

    _stationSprite: function(size) {
      var dpr = this._stationDpr;
      var key = size + '@' + dpr;
      if (this._stationSprites[key]) return this._stationSprites[key];
      var canvas = document.createElement('canvas');
      var width = Math.max(1, Math.round(size * dpr));
      canvas.width = width;
      canvas.height = width;
      var ctx = canvas.getContext && canvas.getContext('2d');
      if (!ctx) return null;
      ctx.scale(width / size, width / size);
      this._drawStationBadge(ctx, size / 2, size / 2, size);
      this._stationSprites[key] = canvas;
      return canvas;
    },

    _rectIntersectsTile: function(rect, size) {
      return rect[2] >= 0 && rect[0] <= size.x && rect[3] >= 0 && rect[1] <= size.y;
    },

    _createStationTile: function(coords, tileSize) {
      var drawStarted = typeof performance !== 'undefined' ? performance.now() : Date.now();
      this._stationCounters.tileCreate += 1;
      var map = this._map;
      if (!map || !this._stationsVisible || !this._stationLoaded) {
        this._stationCounters.tileEmpty += 1;
        var early = document.createElement('div');
        early.className = 'transit-station-tile-empty';
        return early;
      }
      var zoom = coords.z;
      var scale = this._stationStyleScale();
      var maxBadge = STATION_SIZES[STATION_SIZES.length - 1] * scale;
      var fontPx = this._stationFontPx();
      var mayDrawLabels = !this._stationSuppressLabels && zoom >= 12;
      var margin = Math.ceil(mayDrawLabels
        ? Math.max(maxBadge / 2 + 2,
            this._stationMaxLabelWidth / 2 + STATION_LABEL_HALO_PX + 2,
            maxBadge / 2 + 3 + fontPx + STATION_LABEL_HALO_PX)
        : maxBadge / 2 + 2);
      var origin = coords.scaleBy(tileSize);
      var a = map.unproject([origin.x - margin, origin.y - margin], zoom);
      var b = map.unproject([origin.x + tileSize.x + margin,
                             origin.y + tileSize.y + margin], zoom);
      var bounds = L.latLngBounds(a, b);
      var candidates = this._visibleStations(bounds);
      var entries = [];
      for (var i = 0; i < candidates.length; i++) {
        var station = candidates[i];
        if (!this._stationShown(station, zoom)) continue;
        var point = map.project([station.lat, station.lon], zoom).subtract(origin);
        var tier = this._stationTier(station);
        var badge = zoom <= 14 ? STATION_SIZES[0] * scale : STATION_SIZES[tier] * scale;
        var iconRect = [point.x - badge / 2 - 1, point.y - badge / 2 - 1,
                        point.x + badge / 2 + 1, point.y + badge / 2 + 1];
        var text = this._stationName(station);
        var showLabel = !!text && this._stationLabelVisible(station, zoom);
        var labelRect = null;
        if (showLabel) {
          var labelWidth = this._stationLabelWidth(station);
          var labelBottom = point.y - badge / 2 - 3;
          labelRect = [point.x - labelWidth / 2 - STATION_LABEL_HALO_PX,
                       labelBottom - fontPx - STATION_LABEL_HALO_PX,
                       point.x + labelWidth / 2 + STATION_LABEL_HALO_PX,
                       labelBottom + STATION_LABEL_HALO_PX];
        }
        if (!this._rectIntersectsTile(iconRect, tileSize) &&
            (!labelRect || !this._rectIntersectsTile(labelRect, tileSize))) continue;
        entries.push({station: station, point: point, badge: badge,
                      text: text, showLabel: showLabel});
      }
      if (!entries.length) {
        this._stationCounters.tileEmpty += 1;
        var empty = document.createElement('div');
        empty.className = 'transit-station-tile-empty';
        return empty;
      }
      entries.sort(function(left, right) {
        return (left.station.line_count - right.station.line_count) ||
          (left.station._placementIndex - right.station._placementIndex);
      });
      var canvas = document.createElement('canvas');
      canvas.className = 'transit-station-tile';
      canvas.dataset.stationTile = '1';
      canvas.style.width = tileSize.x + 'px';
      canvas.style.height = tileSize.y + 'px';
      canvas.style.pointerEvents = 'none';
      var backingWidth = Math.max(1, Math.round(tileSize.x * this._stationDpr));
      var backingHeight = Math.max(1, Math.round(tileSize.y * this._stationDpr));
      canvas.width = backingWidth;
      canvas.height = backingHeight;
      var ctx = canvas.getContext && canvas.getContext('2d');
      if (!ctx) {
        this._stationCounters.tileEmpty += 1;
        var noContext = document.createElement('div');
        noContext.className = 'transit-station-tile-empty';
        return noContext;
      }
      var contextScaleX = backingWidth / tileSize.x;
      var contextScaleY = backingHeight / tileSize.y;
      ctx.scale(contextScaleX, contextScaleY);
      var snapX = function(value) { return Math.round(value * contextScaleX) / contextScaleX; };
      var snapY = function(value) { return Math.round(value * contextScaleY) / contextScaleY; };
      for (var j = 0; j < entries.length; j++) {
        var entry = entries[j];
        var sprite = this._stationSprite(entry.badge);
        if (sprite) {
          ctx.drawImage(sprite, snapX(entry.point.x - entry.badge / 2),
            snapY(entry.point.y - entry.badge / 2), entry.badge, entry.badge);
        }
      }
      ctx.font = '600 ' + fontPx + 'px ' + this._stationFont;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.lineJoin = 'round';
      ctx.strokeStyle = 'rgba(255,255,255,.96)';
      ctx.lineWidth = STATION_LABEL_HALO_PX;
      ctx.fillStyle = '#3a4358';
      for (var k = 0; k < entries.length; k++) {
        var label = entries[k];
        if (!label.showLabel) continue;
        var tx = snapX(label.point.x);
        var ty = snapY(label.point.y - label.badge / 2 - 3);
        ctx.strokeText(label.text, tx, ty);
        ctx.fillText(label.text, tx, ty);
      }
      var bytes = backingWidth * backingHeight * 4;
      canvas.dataset.stationTileBytes = String(bytes);
      this._stationCounters.tileDraw += 1;
      var drawEnded = typeof performance !== 'undefined' ? performance.now() : Date.now();
      this._stationTileDrawDurationMs.push(drawEnded - drawStarted);
      if (this._stationTileDrawDurationMs.length > 128) this._stationTileDrawDurationMs.shift();
      return canvas;
    },

    _onStationTileUnload: function(event) {
      var tile = event && event.tile;
      if (!tile || tile.dataset.stationTile !== '1') return;
      tile.dataset.stationTile = '0';
      this._stationCounters.tileRemove += 1;
      tile.width = 0;
      tile.height = 0;
    },

    _syncStationInteraction: function(enabled) {
      if (!this._map) return;
      if (enabled && !this._stationHoverTip) {
        this._stationHoverTip = L.tooltip({className:'transit-station-label',direction:'top',offset:[0,-8],opacity:1});
      }
      if (enabled && !this._onStationClickBound) {
        this._onStationClickBound = this._onStationClick.bind(this);
        this._map.on('click', this._onStationClickBound);
      } else if (!enabled && this._onStationClickBound) {
        this._map.off('click', this._onStationClickBound);
        this._onStationClickBound = null;
      }
      if (enabled && !this._onStationMoveStartBound) {
        this._onStationMoveStartBound = this._hideStationHover.bind(this);
        this._map.on('movestart', this._onStationMoveStartBound);
        this._map.on('zoomstart', this._onStationMoveStartBound);
      } else if (!enabled && this._onStationMoveStartBound) {
        this._map.off('movestart', this._onStationMoveStartBound);
        this._map.off('zoomstart', this._onStationMoveStartBound);
        this._onStationMoveStartBound = null;
      }
      var canHover = window.matchMedia && matchMedia('(hover: hover)').matches;
      if (enabled && canHover && !this._onStationMouseMoveBound) {
        this._onStationMouseMoveBound = this._onStationMouseMove.bind(this);
        this._onStationMouseOutBound = this._hideStationHover.bind(this);
        this._map.on('mousemove', this._onStationMouseMoveBound);
        this._map.on('mouseout', this._onStationMouseOutBound);
      } else if ((!enabled || !canHover) && this._onStationMouseMoveBound) {
        this._map.off('mousemove', this._onStationMouseMoveBound);
        this._map.off('mouseout', this._onStationMouseOutBound);
        this._onStationMouseMoveBound = this._onStationMouseOutBound = null;
      }
      if (!enabled && this._stationHoverTip) {
        this._stationHoverTip.remove();
        this._stationHoverTip = null;
      }
    },

    _hideStationHover: function() {
      if (this._stationHoverTip) this._stationHoverTip.remove();
    },

    _eventOwnedByInteractiveLayer: function(event) {
      var node = event && event.originalEvent && event.originalEvent.target;
      while (node && node !== this._map._container) {
        if (node.classList && (node.classList.contains('leaflet-marker-icon') ||
            node.classList.contains('leaflet-interactive') ||
            node.classList.contains('marker-cluster'))) return true;
        node = node.parentNode;
      }
      return false;
    },

    _hitStation: function(latlng) {
      if (!this._map || !this._stationLoaded || !this._stationsVisible ||
          this._map.getZoom() < this.options.stationMinZoom) return null;
      var point = this._map.latLngToContainerPoint(latlng);
      var tolerance = STATION_SIZES[2] * this._stationStyleScale() / 2 + 6;
      var one = this._map.containerPointToLatLng([point.x - tolerance, point.y - tolerance]);
      var two = this._map.containerPointToLatLng([point.x + tolerance, point.y + tolerance]);
      var candidates = this._visibleStations(L.latLngBounds(one, two));
      var zoom = Math.floor(this._map.getZoom());
      var best = null, bestDistance = Infinity;
      for (var i = 0; i < candidates.length; i++) {
        var station = candidates[i];
        if (!this._stationTemporaryNameAllowed(station, zoom)) continue;
        var projected = this._map.latLngToContainerPoint([station.lat, station.lon]);
        var dx = projected.x - point.x, dy = projected.y - point.y;
        var radius = this._stationBadgeSize(station, zoom) / 2 + 5;
        var distance = dx * dx + dy * dy;
        if (distance <= radius * radius && distance < bestDistance) {
          best = station;
          bestDistance = distance;
        }
      }
      return best;
    },

    _showStationTooltip: function(station) {
      if (!station || !this._stationHoverTip || !this._map) return;
      var node = document.createElement('span');
      var name = this._stationName(station);
      if (/[^\x00-\x7f]/.test(name)) node.lang = 'ja';
      node.textContent = name;
      this._stationHoverTip.setContent(node)
        .setLatLng([station.lat, station.lon]).addTo(this._map);
    },

    _onStationMouseMove: function(e) {
      if (!this._stationHoverTip || !this._map || this._eventOwnedByInteractiveLayer(e)) {
        this._hideStationHover(); return;
      }
      var hit = this._hitStation(e.latlng);
      if (!hit) { this._hideStationHover(); return; }
      this._showStationTooltip(hit);
    },

    _onStationClick: function(e) {
      if (!this._stationHoverTip || !this._map || this._eventOwnedByInteractiveLayer(e)) return;
      var hit = this._hitStation(e.latlng);
      if (hit) this._showStationTooltip(hit);
      else this._hideStationHover();
    },

    _stationVisibleCount: function() {
      if (!this._map || !this._stationLoaded || !this._stationsVisible ||
          this._map.getZoom() < this.options.stationMinZoom) return 0;
      var zoom = Math.floor(this._map.getZoom());
      var list = this._visibleStations(this._map.getBounds());
      var count = 0;
      for (var i = 0; i < list.length; i++) {
        if (this._stationShown(list[i], zoom)) count += 1;
      }
      return count;
    },

    _visibleStations: function(bounds) {
      var W = bounds.getWest(), E = bounds.getEast();
      var S = bounds.getSouth(), N = bounds.getNorth();
      var grid = this.options.stationGrid;
      var gx0 = Math.floor(W / grid), gx1 = Math.floor(E / grid);
      var gy0 = Math.floor(S / grid), gy1 = Math.floor(N / grid);
      var out = [];
      for (var gx = gx0; gx <= gx1; gx++) {
        for (var gy = gy0; gy <= gy1; gy++) {
          var cell = this._stationIndex.get(gx + ',' + gy);
          if (!cell) continue;
          for (var i = 0; i < cell.length; i++) {
            var stn = cell[i];
            if (stn.lon >= W && stn.lon <= E && stn.lat >= S && stn.lat <= N) {
              out.push(stn);
            }
          }
        }
      }
      return out;
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
