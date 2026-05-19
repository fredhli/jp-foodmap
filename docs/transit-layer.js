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

  L.TransitLayer = L.Layer.extend({
    options: {
      geojsonUrl: 'transit/japan.geojson',
      opacity: 0.7,         // line opacity when overlaid on a base map
      casingOpacity: 0.45,  // white casing underneath, less prominent
      padding: 0.25,
      grid: 0.4,            // grid cell size in degrees
      hitWeight: 14         // invisible wider polyline for hover
    },

    initialize: function(options) {
      L.Util.setOptions(this, options);
      // Cached across add/remove cycles
      this._loaded = false;
      this._loading = false;
      this._allLines = [];
      this._allStations = [];
      this._lineIndex = new Map();
      // Which buckets the renderer is currently allowed to draw. Both
      // default on so a bare addTo() keeps the historical behavior; map.py
      // calls setVisibleBuckets({long, city}) from the FAB wiring to switch.
      this._buckets = { long: true, city: true };
      // Volatile (cleared on remove)
      this._onMap = new Set();
      this._lastZoom = null;
      this._lastDrawCasing = null;
      this._rafToken = 0;
    },

    setVisibleBuckets: function(opts) {
      if (opts && typeof opts.long === 'boolean') this._buckets.long = opts.long;
      if (opts && typeof opts.city === 'boolean') this._buckets.city = opts.city;
      if (this._map) this._scheduleRedraw();
      return this;
    },

    onAdd: function(map) {
      this._map = map;
      this._rCasing = L.canvas({ padding: this.options.padding }).addTo(map);
      this._rImp = {};
      for (var imp = 1; imp <= 5; imp++) {
        this._rImp[imp] = L.canvas({ padding: this.options.padding }).addTo(map);
      }
      this._rHit = L.canvas({ padding: this.options.padding }).addTo(map);
      this._stationsLayer = L.layerGroup().addTo(map);
      this._scheduleRedrawBound = this._scheduleRedraw.bind(this);
      map.on('moveend', this._scheduleRedrawBound);
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
      if (this._rafToken) {
        cancelAnimationFrame(this._rafToken);
        this._rafToken = 0;
      }
      // Detach polylines and drop refs — they're tied to the renderers we're
      // about to throw away, so caching them across add/remove would just
      // pin dead references.
      var it = this._onMap.values(), v;
      while (!(v = it.next()).done) {
        var f = v.value;
        if (f._pl) { f._pl.remove(); f._pl = null; }
        if (f._pl_casing) {
          if (f._pl_casing._map) f._pl_casing.remove();
          f._pl_casing = null;
        }
        if (f._pl_hit) { f._pl_hit.remove(); f._pl_hit = null; }
      }
      this._onMap.clear();
      if (this._rCasing) this._rCasing.remove();
      if (this._rImp) {
        for (var imp = 1; imp <= 5; imp++) this._rImp[imp].remove();
      }
      if (this._rHit) this._rHit.remove();
      if (this._stationsLayer) this._stationsLayer.remove();
      this._rCasing = this._rImp = this._rHit = this._stationsLayer = null;
      this._lastZoom = null;
      this._lastDrawCasing = null;
      this._map = null;
      return this;
    },

    _load: function() {
      if (this._loaded || this._loading) return;
      this._loading = true;
      var self = this;
      fetch(this.options.geojsonUrl)
        .then(function(r) { return r.json(); })
        .then(function(gj) {
          for (var i = 0; i < gj.features.length; i++) {
            var f = gj.features[i];
            if (f.geometry.type === 'LineString') {
              self._allLines.push(f);
              self._indexLine(f);
            } else if (f.geometry.type === 'Point') {
              self._allStations.push(f);
            }
          }
          self._loaded = true;
          self._loading = false;
          if (self._map) self._scheduleRedraw();
        })
        .catch(function(e) {
          console.error('[TransitLayer] load failed:', e);
          self._loading = false;
        });
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
      f._color = colorFor(f.properties, cls);
      var ll = new Array(coords.length);
      for (var j = 0; j < coords.length; j++) ll[j] = [coords[j][1], coords[j][0]];
      f._latlngs = ll;
      f.geometry = null;  // free the [lon,lat] copy

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
        renderer: this._rCasing, color: '#ffffff', weight: cls.w + 1.6,
        opacity: cop, lineCap: 'round', lineJoin: 'round', interactive: false
      });
      f._pl = L.polyline(f._latlngs, {
        renderer: this._rImp[cls.importance], color: f._color, weight: cls.w,
        opacity: op, lineCap: 'round', lineJoin: 'round', interactive: false
      });
      f._pl_hit = L.polyline(f._latlngs, {
        renderer: this._rHit, color: '#000', weight: this.options.hitWeight,
        opacity: 0, lineCap: 'round', lineJoin: 'round',
        bubblingMouseEvents: false
      });
      var label = f.properties.route_name || f.properties.name;
      if (label) {
        f._pl_hit.bindTooltip(label, {
          sticky: true, direction: 'top', offset: [0, -8],
          className: 'transit-line-label', opacity: 1
        });
      }
    },

    _scheduleRedraw: function() {
      if (this._rafToken) return;
      var self = this;
      this._rafToken = requestAnimationFrame(function() {
        self._rafToken = 0;
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
        if (cls.bucket === 'long' && !bk.long) continue;
        if (cls.bucket === 'city' && !bk.city) continue;
        desired.add(f);
      }

      // Remove
      var toRemove = [];
      this._onMap.forEach(function(f) { if (!desired.has(f)) toRemove.push(f); });
      for (var r = 0; r < toRemove.length; r++) {
        var fr = toRemove[r];
        if (fr._pl) { fr._pl.remove(); fr._pl = null; }
        if (fr._pl_hit) { fr._pl_hit.remove(); fr._pl_hit = null; }
        if (fr._pl_casing) {
          if (fr._pl_casing._map) fr._pl_casing.remove();
          fr._pl_casing = null;
        }
        this._onMap.delete(fr);
      }

      // Add / restyle
      var self = this;
      desired.forEach(function(f) {
        self._ensurePolylines(f);
        var c = CLASSES[f._class];
        if (!self._onMap.has(f)) {
          f._pl.setStyle({ weight: c.w + zoomBoost });
          f._pl.addTo(self._map);
          f._pl_hit.addTo(self._map);
          if (drawCasing) {
            f._pl_casing.setStyle({ weight: c.w + zoomBoost + 1.6 });
            f._pl_casing.addTo(self._map);
          }
          self._onMap.add(f);
        } else {
          if (zoomChanged) {
            f._pl.setStyle({ weight: c.w + zoomBoost });
            if (f._pl_casing._map) {
              f._pl_casing.setStyle({ weight: c.w + zoomBoost + 1.6 });
            }
          }
          if (casingChanged) {
            if (drawCasing && !f._pl_casing._map) {
              f._pl_casing.setStyle({ weight: c.w + zoomBoost + 1.6 });
              f._pl_casing.addTo(self._map);
            } else if (!drawCasing && f._pl_casing._map) {
              f._pl_casing.remove();
            }
          }
        }
      });

      this._lastZoom = zoom;
      this._lastDrawCasing = drawCasing;

      // Stations: lightweight recreate (small count visible at zoom >= 12).
      // Transfer hubs get a noticeably bigger circle so they read at a
      // glance: line_count >= 6 ("mega-hub" — 渋谷 / 新宿 / 上野 / 池袋 /
      // 京都...) and >= 3 ("regular hub") are precomputed by
      // transit_postprocess.py. Stations on the wrong bucket — e.g., a
      // pure shinkansen-only halt while the 长途 toggle is off — get
      // skipped entirely so the dots don't outlive their lines.
      this._stationsLayer.clearLayers();
      if (zoom >= 12) {
        var b = this._map.getBounds().pad(0.1);
        var W = b.getWest(), E = b.getEast(), S2 = b.getSouth(), N = b.getNorth();
        var showLabel = zoom >= 14;
        for (var s = 0; s < this._allStations.length; s++) {
          var stn = this._allStations[s];
          var lon = stn.geometry.coordinates[0];
          var lat = stn.geometry.coordinates[1];
          if (lon < W || lon > E || lat < S2 || lat > N) continue;
          // Hide the dot if its only nearby lines belong to a bucket that's
          // off. Legacy stations without the per-bucket flags fall back to
          // "show if any bucket is on" so old geojsons keep working.
          var sHasLong = stn.properties.has_long_line;
          var sHasCity = stn.properties.has_city_line;
          var hasFlags = (typeof sHasLong !== 'undefined') ||
                         (typeof sHasCity !== 'undefined');
          var visibleByBucket = hasFlags
            ? ((sHasLong && bk.long) || (sHasCity && bk.city))
            : (bk.long || bk.city);
          if (!visibleByBucket) continue;
          var lc = stn.properties.line_count | 0;
          var radius;
          if (lc >= 6)      radius = zoom >= 15 ? 8 : zoom >= 13 ? 6.5 : 5.5;
          else if (lc >= 3) radius = zoom >= 15 ? 6 : zoom >= 13 ? 5   : 4.2;
          else              radius = zoom >= 15 ? 4 : zoom >= 13 ? 3.2 : 2.6;
          var isTram = stn.properties.railway === 'tram_stop';
          var isHub = lc >= 3;
          var dot = L.circleMarker([lat, lon], {
            radius: radius,
            weight: isHub ? 2 : 1.5,
            color: isTram ? '#c62828' : (isHub ? '#111' : '#222'),
            fillColor: isHub ? '#fffbea' : '#ffffff',
            fillOpacity: this.options.opacity,
            opacity: this.options.opacity
          });
          var nm = stn.properties.name;
          if (nm) {
            var opts = { className: 'transit-station-label' };
            if (showLabel) {
              opts.permanent = true;
              opts.direction = 'top';
              opts.offset = [0, -4];
            }
            var label = nm;
            if (lc >= 3) label = nm + '  (' + lc + '线)';
            dot.bindTooltip(label, opts);
          }
          this._stationsLayer.addLayer(dot);
        }
      }
    }
  });

  L.transitLayer = function(options) { return new L.TransitLayer(options); };
})();
