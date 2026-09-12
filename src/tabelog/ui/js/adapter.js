/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/adapter.js
   THE ONE SEAM between the 4.0 presentation (core.js + six modules) and the
   production business layer (business.js, transplanted verbatim from 3.2.x).

   Direction of calls, and nothing else:
       modules ──▶ App.act.* / Data.* ──▶ Adapter ──▶ Business.api
       Business.api ──▶ hooks.* ──▶ Adapter ──▶ App.set / App.emit ──▶ modules

   Rules this file enforces (CLAUDE.md "Backwards compatibility"):
     · every localStorage key is spelled exactly as before; the ones the
       business layer does not own (mapView, lastLocation, listView, the four
       tabelog.show* toggles, filterState, seenIntro, oovHintDismissed) are
       read and written HERE, in the shapes 3.2.x wrote, so a refresh after
       the deploy shows the same map, filters, sort and layers;
     · modules never touch state.user directly — act.toggleFav / act.setBlack /
       the pin and list actions below go through Business and the result is
       mirrored back into App.state.user by the hooks;
     · the KV blob, the Worker calls and the sync state machine are not in
       this file at all. If you find yourself adding a fetch() here, stop.

   Owner: architect. Module agents extend App.act / Data only by filing a
   request; the surface below is the contract they build against (PORT-PLAN
   §6 lists every signature).
   ========================================================================== */
(function () {
  'use strict';

  var B = window.Business;
  var App = window.App;
  var util = App.util;
  var A = window.Adapter = { ready: false, booted: false };
  var biz = null;            // Business.api after Business.init
  var map = null, mapEl = null;

  var UI_TO_LANG = { zh: 'zh-CN', tw: 'zh-TW', en: 'en', ja: 'ja' };
  var LANG_TO_UI = { 'zh-CN': 'zh', 'zh-TW': 'tw', en: 'en', ja: 'ja' };
  A.uiLang = function () { return LANG_TO_UI[B.lang()] || 'zh'; };

  function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
  function lsJSON(k) { try { return JSON.parse(lsGet(k) || 'null'); } catch (_) { return null; } }

  /* ========================================================================
     Prefs — the per-device keys the business layer does not own. Names and
     shapes are the 3.2.x ones; readers tolerate anything (CLAUDE.md).
     ==================================================================== */
  var Prefs = A.prefs = {};
  var KEY_VIEW = 'tabelog.mapView';
  var KEY_LASTLOC = 'tabelog.lastLocation';
  var KEY_LIST = 'tabelog.listView';
  var KEY_FILTER = 'tabelog.filterState';
  var KEY_SEEN_INTRO = 'tabelog.seenIntro';
  var KEY_OOV = 'tabelog.oovHintDismissed';
  var LAYER_KEYS = {
    long: 'tabelog.showTransitLong',
    city: 'tabelog.showTransitCity',
    landmarks: 'tabelog.showAttractions',
    pins: 'tabelog.showBookmarks'
  };
  var KEY_ATTRACTIONS = LAYER_KEYS.landmarks;
  var LAYER_DEFAULTS = { long: false, city: false, landmarks: true, pins: true, hiddenLandmarks: false };

  // tabelog.showAttractions is the one layer key that is NOT a boolean: 3.2.x
  // wired the landmarks FAB as a tri-state — '0' off, '1' on, '2' on and also
  // showing the built-ins the user has hidden (the popover's 也显示已隐藏的景点
  // row). Reading it as `v === '1'` would turn the layer OFF for everyone
  // sitting on '2', and writing a plain boolean back would silently drop that
  // preference, so the two 4.0 booleans fold into the one stored value.
  Prefs.readLayers = function () {
    var out = {};
    ['long', 'city', 'pins'].forEach(function (k) {
      var v = lsGet(LAYER_KEYS[k]);
      out[k] = v === null ? LAYER_DEFAULTS[k] : (v === '1');
    });
    var a = lsGet(KEY_ATTRACTIONS);
    if (a === '0') { out.landmarks = false; out.hiddenLandmarks = false; }
    else if (a === '2') { out.landmarks = true; out.hiddenLandmarks = true; }
    else { out.landmarks = LAYER_DEFAULTS.landmarks; out.hiddenLandmarks = false; }   // '1', missing or junk
    return out;
  };
  Prefs.writeLayers = function (layers) {
    ['long', 'city', 'pins'].forEach(function (k) {
      if (typeof layers[k] === 'boolean') lsSet(LAYER_KEYS[k], layers[k] ? '1' : '0');
    });
    if (typeof layers.landmarks !== 'boolean') return;
    lsSet(KEY_ATTRACTIONS, !layers.landmarks ? '0' : (layers.hiddenLandmarks ? '2' : '1'));
  };

  // {lat, lon, zoom} — restored before Leaflet by VIEW_RESTORE_SNIPPET (map.py);
  // saved here on every moveend, debounced, flushed on pagehide (3.2.x logic).
  var viewSaveTimer = 0;
  function saveViewNow() {
    if (!map) return;
    try {
      var c = map.getCenter();
      localStorage.setItem(KEY_VIEW, JSON.stringify({ lat: c.lat, lon: c.lng, zoom: map.getZoom() }));
    } catch (e) {}
  }
  Prefs.bindMapView = function (m) {
    m.on('moveend', function () {
      clearTimeout(viewSaveTimer);
      viewSaveTimer = setTimeout(saveViewNow, 250);
    });
    // The pagehide flush is registered by wireUnloadFlush(), deliberately
    // after Business's own — see the comment there.
    // Persist every successful fix so a reopen can paint the last position
    // immediately (before the live fix arrives).
    m.on('locationfound', function (e) {
      // The freshest fix this session. Data.sort('distance') prefers it over
      // both the caller's `from` and the persisted one, so the list's 距离最近
      // and the map's own ordering can never disagree. (list coreRequest #3)
      A.liveFix = { lat: e.latlng.lat, lng: e.latlng.lng };
      try {
        localStorage.setItem(KEY_LASTLOC, JSON.stringify({
          lat: e.latlng.lat, lon: e.latlng.lng, acc: e.accuracy || null, ts: Date.now()
        }));
      } catch (_) {}
    });
  };
  Prefs.readLastLocation = function () {
    var f = lsJSON(KEY_LASTLOC);
    if (!f || typeof f.lat !== 'number' || !isFinite(f.lat) || Math.abs(f.lat) > 90 ||
        typeof f.lon !== 'number' || !isFinite(f.lon) || Math.abs(f.lon) > 180) return null;
    return { lat: f.lat, lon: f.lon, acc: typeof f.acc === 'number' ? f.acc : null,
             ts: typeof f.ts === 'number' && isFinite(f.ts) ? f.ts : null };
  };

  // tabelog.listView — {sort, select, tab, leftCollapsed, planningContext, nearbyActive}
  // (3.2.x wbLoadListView / wbSaveListView tolerance, additive fields only).
  var SORT_TO_UI = { rating: 'rating', price: 'price', award: 'awards', distance: 'distance', name: 'name' };
  var SORT_TO_BIZ = { rating: 'rating', price: 'price', awards: 'award', distance: 'distance', name: 'name' };
  Prefs.readListView = function () {
    var out = { sort: 'rating', select: false, tab: 'results', leftCollapsed: false, planningContext: null, nearbyActive: false };
    var o = lsJSON(KEY_LIST);
    if (!o || typeof o !== 'object') return out;
    if (SORT_TO_UI[o.sort]) out.sort = SORT_TO_UI[o.sort];
    out.select = (o.select === true);
    if (o.tab === 'fav') out.tab = 'saved';
    else if (o.tab === 'filter') out.tab = 'filters';
    else out.tab = 'results';
    out.leftCollapsed = (o.leftCollapsed === true);
    var p = o.planningContext;
    if (p && (p.region == null || (Number.isInteger(p.region) && p.region >= 0 && p.region <= 46))
        && SORT_TO_UI[p.sort] && Array.isArray(p.center) && p.center.length === 2
        && p.center.every(function (n) { return typeof n === 'number' && isFinite(n); })
        && Math.abs(p.center[0]) <= 90 && Math.abs(p.center[1]) <= 180
        && typeof p.zoom === 'number' && p.zoom >= 0 && p.zoom <= 22) {
      out.planningContext = p;
      out.nearbyActive = o.nearbyActive === true;
    }
    return out;
  };
  Prefs.writeListView = function (s) {
    var tab = s.sheet.tab === 'saved' ? 'fav' : (s.sheet.tab === 'filters' ? 'filter' : 'results');
    lsSet(KEY_LIST, JSON.stringify({
      sort: SORT_TO_BIZ[s.sort] || 'rating',
      select: !!s.multi.active,
      tab: tab,
      leftCollapsed: s.columns.userLeftPreference === 'closed',
      planningContext: (s.nearby && s.nearby.planning) || null,
      nearbyActive: !!(s.nearby && s.nearby.active)
    }));
  };

  // tabelog.filterState — the 3.2.x shape, both the legacy checked lists and
  // the M-016 unchecked complements, so an older tab still reads it.
  var PRICE_KEYS = [], ALL_CUISINES = [], AWARD_SLUGS = ['gold', 'silver', 'bronze', 'hyaku', 'hot'];
  Prefs.readFilterState = function (defaults) {
    var f = defaults;
    // The budget / cuisine sets are LITERAL: a full set means everything is
    // ticked, an empty set means the reader cleared the section and expects no
    // results, which is what 3.2.x did (`if (!fs.pSet[d.bucket]) return false`
    // and "If none checked, hide all"). They used to share the empty set as
    // both "default" and "cleared", so 全清 wrote a value that was read back as
    // "all" — the button did the opposite of its label. Materialise the default
    // here, where the key lists are loaded, so empty only ever means cleared.
    if (!f.budgets || !f.budgets.size) f.budgets = new Set(PRICE_KEYS);
    if (!f.cuisines || !f.cuisines.size) f.cuisines = new Set(ALL_CUISINES);
    var s = lsJSON(KEY_FILTER);
    if (!s || typeof s !== 'object') return f;
    if (typeof s.rating === 'number' && isFinite(s.rating)) f.ratingMin = util.clamp(s.rating, 3.4, 4.5);
    function fromLists(unchecked, checked, all) {
      // null → the key was not stored at all, keep the materialised default.
      if (Array.isArray(unchecked)) {
        var off = new Set(unchecked);
        return new Set(all.filter(function (k) { return !off.has(k); }));
      }
      if (Array.isArray(checked)) {
        var on = new Set(checked);
        return new Set(all.filter(function (k) { return on.has(k); }));
      }
      return null;
    }
    var storedB = fromLists(s.uncheckedPrices, s.prices, PRICE_KEYS);
    if (storedB) f.budgets = storedB;
    var storedC = fromLists(s.uncheckedGenres, s.genres, ALL_CUISINES);
    if (storedC) f.cuisines = storedC;
    if (Array.isArray(s.awards)) f.awards = new Set(s.awards.filter(function (a) { return AWARD_SLUGS.indexOf(a) >= 0; }));
    if (typeof s.bookableOnly === 'boolean') f.bookableOnly = s.bookableOnly;
    else if (typeof s.bookable === 'string') f.bookableOnly = (s.bookable === 'yes');   // pre-2.0 radio shape
    if (typeof s.onlyFav === 'boolean') f.favOnly = s.onlyFav;
    if (typeof s.hideBlack === 'boolean') f.hideBlack = s.hideBlack;
    if (typeof s.hideForeign === 'boolean') f.hideForeign = s.hideForeign;
    if (typeof s.gcalOnly === 'boolean') f.gcalOnly = s.gcalOnly;
    if (typeof s.region === 'number' && isFinite(s.region) && s.region === Math.floor(s.region)
        && s.region >= 0 && s.region <= 46) f.region = s.region; else f.region = null;
    return f;
  };
  Prefs.writeFilterState = function (f) {
    // literal, so a cleared section round-trips as cleared — 3.2.x reads
    // prices: [] as "nothing selected" too, which is the same meaning.
    var prices = Array.from(f.budgets);
    var genres = Array.from(f.cuisines);
    var pOn = new Set(prices), gOn = new Set(genres);
    lsSet(KEY_FILTER, JSON.stringify({
      rating: f.ratingMin,
      prices: prices,
      genres: genres,
      uncheckedPrices: PRICE_KEYS.filter(function (k) { return !pOn.has(k); }),
      uncheckedGenres: ALL_CUISINES.filter(function (k) { return !gOn.has(k); }),
      awards: Array.from(f.awards),
      bookableOnly: !!f.bookableOnly,
      onlyFav: !!f.favOnly,
      hideBlack: !!f.hideBlack,
      hideForeign: !!f.hideForeign,
      region: f.region == null ? null : f.region,
      gcalOnly: !!f.gcalOnly
    }));
  };
  Prefs.seenIntro = function () { return lsGet(KEY_SEEN_INTRO) === '1'; };
  Prefs.markIntroSeen = function () { lsSet(KEY_SEEN_INTRO, '1'); };
  Prefs.oovDismissed = function () { return lsGet(KEY_OOV) === '1'; };
  Prefs.dismissOov = function () { lsSet(KEY_OOV, '1'); };
  Prefs.langChosen = function () { return lsGet(B.LANG_KEY) !== null; };

  /* ========================================================================
     Data — the demo's Data.* facade over the production rows
     ==================================================================== */
  var Data = window.Data = {};
  var config = Data.config = {};
  var _M = { key: '', ids: null };     // applyFilters cache
  var _userSeq = 0;                    // bumps on every user:changed (busts _M)

  function buildConfig() {
    var C = B.constants;
    var short = (biz && biz.WB_BUCKET_SHORT) || {};
    config.PRICE_BUCKETS = C.PRICE_BUCKETS.map(function (p, i) {
      var key = p[0];
      return { key: key, label: p[1], short: short[key] || p[1], color: C.BUCKET_COLOR[key] || '#9ca3af',
               fg: (key === 'lt1k' || key === 'ge20k') ? '#FFFFFF' : '#111A43', lo: p[2], hi: p[3], rank: i };
    });
    config.PRICE_BY_KEY = {};
    config.PRICE_BUCKETS.forEach(function (b) { config.PRICE_BY_KEY[b.key] = b; });
    PRICE_KEYS = config.PRICE_BUCKETS.map(function (b) { return b.key; });
    config.AWARD_TAGS = [
      { slug: 'gold', label: 'Gold', emoji: '🥇' }, { slug: 'silver', label: 'Silver', emoji: '🥈' },
      { slug: 'bronze', label: 'Bronze', emoji: '🥉' }, { slug: 'hyaku', label: '百名店', emoji: '💯' },
      { slug: 'hot', label: '热门餐厅 2026', emoji: '🔥' }
    ];
    config.AWARD_BY_SLUG = {};
    config.AWARD_TAGS.forEach(function (a, i) { a.rank = i; config.AWARD_BY_SLUG[a.slug] = a; });
    // map_data.py MEAL_GROUPS, injected at build time (dict → [{name, buckets}])
    var mg = window.MEAL_GROUPS || {};
    config.MEAL_GROUPS = Object.keys(mg).map(function (name) { return { name: name, buckets: mg[name].slice() }; });
    config.GROUP_OF = {};
    config.MEAL_GROUPS.forEach(function (g) { g.buckets.forEach(function (b) { config.GROUP_OF[b] = g.name; }); });
    config.ALL_CUISINES = config.MEAL_GROUPS.reduce(function (a, g) { return a.concat(g.buckets); }, []);
    ALL_CUISINES = config.ALL_CUISINES.slice();
    config.DEFAULT_OFF_GENRES = Array.from(biz.FOREIGN_GENRES);
    config.FOREIGN_SET = biz.FOREIGN_GENRES;
    config.GENRE_EMOJI = C.GENRE_EMOJI;
    // PREFS: [{ja, sc, tc, en, n}] index-aligned with row.pref
    // the six 地区 groups: zh keys, translated by t() (ui/i18n/ui-strings.json)
    config.REGION_GROUPS = [
      { from: 0, to: 6, name: '北海道·东北' }, { from: 7, to: 13, name: '关东' },
      { from: 14, to: 22, name: '中部' }, { from: 23, to: 29, name: '近畿' },
      { from: 30, to: 38, name: '中国·四国' }, { from: 39, to: 46, name: '九州·冲绳' }
    ];
    config.REGIONS = (C.PREFS || []).map(function (p, i) {
      var g = config.REGION_GROUPS.filter(function (gr) { return i >= gr.from && i <= gr.to; })[0];
      return { code: i, zh: p.sc, tw: p.tc, en: p.en, ja: p.ja, n: p.n || 0, group: g ? g.name : '' };
    });
    config.SORTS = [
      { key: 'rating', label: '评分从高到低' }, { key: 'price', label: '价格从低到高' },
      { key: 'awards', label: '奖项优先' }, { key: 'distance', label: '距离最近', needsLocation: true },
      { key: 'name', label: '按店名' }
    ];
    config.RATING_MIN = 3.4; config.RATING_MAX = 4.5; config.RATING_QUICK = [3.6, 3.8, 4.0, 4.2];
    config.BOOKMARK_QUICK_EMOJI = ['📍', '🏠', '🏨', '🍴', '⭐', '❤️', '🛍️', '⛩️', '♨️', '🚉'];
    config.LIST_QUICK_EMOJI = ['📁', '🍣', '🍜', '☕', '🍶', '⭐'];
    config.layout = { narrowLt: 750, wideGte: 1100 };
    // The rail overlay: content-hashed, gzip-encoded R2 objects (M-009), same
    // three LODs and breaks 3.2.x passed to L.transitLayer.
    config.TRANSIT = {
      lodUrls: {
        low: 'https://assets.jpfoodmap.com/japan-low.3df7fc5442.geojson',
        mid: 'https://assets.jpfoodmap.com/japan-mid.b12465fa7b.geojson',
        high: 'https://assets.jpfoodmap.com/japan.0f546984b1.geojson'
      },
      lodBreaks: { mid: 9, high: 14 }, opacity: 0.4, casingOpacity: 0.2
    };
    config.API_BASE = C.API_BASE;
    config.GOOGLE_CLIENT_ID = C.GOOGLE_CLIENT_ID;
  }

  function buildData(rows) {
    Data.restaurants = rows;
    for (var i = 0; i < rows.length; i++) {
      var d = rows[i];
      if (d.id === undefined) d.id = d.detail_url;   // the demo modules key on `id`
    }
    Data.landmarks = B.constants.EMBEDDED_FAVORITES_BUILTIN;
    Data.places = [];                                // Nominatim results are async (overlays)
  }

  // rowByUrl is a plain object keyed by a user-reachable string, so an id of
  // 'constructor' / 'toString' must not hand a module a function.
  Data.byId = function (id) {
    if (!biz || typeof id !== 'string') return null;
    return Object.prototype.hasOwnProperty.call(biz.rowByUrl, id) ? biz.rowByUrl[id] : null;
  };
  Data.landmarkById = function (id) {
    var L = Data.landmarks || [];
    for (var i = 0; i < L.length; i++) if (L[i].id === id) return L[i];
    return null;
  };
  Data.placeById = function () { return null; };
  Data.cityOf = function (r) { return r && r.city ? r.city : ''; };
  Data.regionName = function (code, lang) { var r = config.REGIONS[code]; return r ? (r[lang] || r.zh) : ''; };
  Data.regionGroupName = function (g) { return window.t(g.name); };
  Data.landmarkName = function (lm) { return biz ? biz.bmDisplayName(lm) : (lm.name_sc || lm.name_src || ''); };
  Data.pinName = Data.landmarkName;
  Data.placeName = function (p, lang) { return p.name ? (p.name[lang] || p.name.zh || '') : (p.label || ''); };
  Data.genreEmoji = function (r) { return config.GENRE_EMOJI[(r.categories && r.categories[0]) || '其他'] || '🍽️'; };
  Data.cuisineLabel = function (cat) { return window.t(cat); };
  /** awards on a production row are a slug array ('gold','hyaku',…); years are in the popups ribbons (slot 8). */
  Data.awardSlugs = function (r) { return Array.isArray(r.awards) ? r.awards : []; };
  Data.awardObjs = function (r) { return Data.awardSlugs(r).map(function (s) { return { slug: s, year: null }; }); };
  Data.awardLabel = function (a) {
    var slug = typeof a === 'string' ? a : a.slug;
    var meta = config.AWARD_BY_SLUG[slug]; if (!meta) return slug;
    if (slug === 'gold' || slug === 'silver' || slug === 'bronze') return meta.label.toUpperCase();
    return window.t(meta.label);
  };
  Data.awardShort = function (slug) {
    var m = { gold: '金奖', silver: '银奖', bronze: '铜奖', hyaku: '百名店', hot: '热门餐厅 2026' };
    return window.t(m[slug] || slug);
  };
  Data.bestAwardRank = function (r) {
    var best = 99; Data.awardSlugs(r).forEach(function (s) { var m = config.AWARD_BY_SLUG[s]; if (m && m.rank < best) best = m.rank; });
    return best;
  };
  Data.bucketOf = function (dinner, lunch) {
    var n = (dinner === null || dinner === undefined || dinner === '') ? null : Number(dinner);
    if (n === null || isNaN(n)) n = (lunch === null || lunch === undefined || lunch === '') ? null : Number(lunch);
    if (n === null || isNaN(n)) return 'na';
    for (var i = 0; i < config.PRICE_BUCKETS.length; i++) {
      var b = config.PRICE_BUCKETS[i]; if (b.key === 'na') continue;
      if ((b.lo === null || n >= b.lo) && (b.hi === null || n < b.hi)) return b.key;
    }
    return 'na';
  };
  Data.priceBucketOf = function (n) { return Data.bucketOf(n, null); };
  Data.policyLabel = function (det) {
    var b = det && det.policy ? det.policy.b : null;
    if (b === 2) return window.t('需要预约');
    if (b === 1) return window.t('可以预约');
    if (b === 0) return window.t('无法预订');
    return window.t('预约信息未提供');
  };

  /**
   * detail(id) → Promise<Detail|null>. The popups payload for the active
   * language, one 12-slot positional array per restaurant (CLAUDE.md): slots
   * 9–11 are length-checked because a pre-2.0 SW cache still serves 9-slot
   * arrays. `slots` is the raw array for anything not mapped here.
   */
  var _popupsByLang = {};
  /** popupsFor(lang|null) — null/active language reuses Business's single
   *  memoised map; any other language gets its own lazily-fetched copy. The
   *  literal URLs below are what map.py's ?v= stamping rewrites, so each one
   *  is content-hashed and served from the SW's data cache like every other. */
  function popupsFor(lang) {
    if (!lang || lang === A.uiLang()) return B.loadPopups();
    var url = lang === 'ja' ? 'data/popups-ja.json'
            : lang === 'tw' ? 'data/popups-tw.json'
            : lang === 'en' ? 'data/popups-en.json'
            : 'data/popups.json';
    if (_popupsByLang[lang]) return _popupsByLang[lang];
    _popupsByLang[lang] = B.fetchBounded(url, { cache: 'force-cache' }, 20000)
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .catch(function (e) { _popupsByLang[lang] = null; throw e; });
    return _popupsByLang[lang];
  }
  Data.detail = function (id, lang) {
    return popupsFor(lang).then(function (m) {
      var arr = m && m[id];
      if (!Array.isArray(arr)) return null;
      return {
        id: id, slots: arr,
        genreText: arr[0] || '', dinnerUpper: arr[1], lunchUpper: arr[2],
        seat: arr[3] || null, station: arr[4] || null, address: arr[5] || null,
        policyText: arr[6] || null, photos: Array.isArray(arr[7]) ? arr[7] : [],
        ribbons: arr[8] || '',
        policy: arr.length > 9 ? (arr[9] || null) : null,
        holiday: arr.length > 10 ? (arr[10] || null) : null,
        stationM: arr.length > 11 ? (arr[11] == null ? null : arr[11]) : null
      };
    });
  };

  // ---- filters: FILTER-01 semantics on top of the verbatim passesFilter() --
  function toBizFilter(f) {
    var pSet = {}, gSet = {}, aSet = {}, gAny = false, aAny = false;
    // Literal: an empty set reaches passesFilter as an empty pSet / gAny=false,
    // which is exactly how 3.2.x produced "no results" for a cleared section.
    (f.budgets ? Array.from(f.budgets) : []).forEach(function (k) { pSet[k] = 1; });
    (f.cuisines ? Array.from(f.cuisines) : []).forEach(function (c) { gSet[c] = 1; gAny = true; });
    (f.awards ? Array.from(f.awards) : []).forEach(function (a) { aSet[a] = 1; aAny = true; });
    return {
      minRating: typeof f.ratingMin === 'number' ? f.ratingMin : 3.4,
      region: (f.region === null || f.region === undefined) ? null : f.region,
      pSet: pSet, gSet: gSet, gAny: gAny, aSet: aSet, aAny: aAny,
      bookableOnly: !!f.bookableOnly, onlyFav: !!f.favOnly, hideBlack: !!f.hideBlack,
      hideForeign: !!f.hideForeign, gcalOnly: !!f.gcalOnly
    };
  }
  function filterKey(f) {
    return JSON.stringify([f.region, f.ratingMin, Array.from(f.budgets || []).sort(), Array.from(f.cuisines || []).sort(),
      Array.from(f.awards || []).sort(), !!f.bookableOnly, !!f.favOnly, !!f.hideBlack, !!f.hideForeign, !!f.gcalOnly, _userSeq,
      App.state.saved && App.state.saved.onlyList]);
  }
  Data.filterMatch = function (r, f) {
    biz.setFilterState(toBizFilter(f));
    return biz.passesFilter(r);
  };
  Data.applyFilters = function (f) {
    var key = filterKey(f);
    if (_M.ids && _M.key === key) return _M.ids;
    biz.setFilterState(toBizFilter(f));
    var out = [], rows = Data.restaurants;
    for (var i = 0; i < rows.length; i++) if (biz.passesFilter(rows[i])) out.push(rows[i].id);
    _M = { key: key, ids: out };
    return out;
  };
  Data.M = function (state) { return Data.applyFilters(state.filters, state.user); };
  Data.inBounds = function (r, b) { return !!b && r.lat >= b.south && r.lat <= b.north && r.lon >= b.west && r.lon <= b.east; };
  Data.MV = function (ids, bounds) {
    if (!bounds) return ids.slice();
    return ids.filter(function (id) { var r = Data.byId(id); return r && Data.inBounds(r, bounds); });
  };
  /** sort(ids, key, {from:[lat,lon]|null}) — the 3.2.x comparators (Business.sortRows). */
  Data.sort = function (ids, key, ctx) {
    ctx = ctx || {};
    var rows = ids.map(Data.byId).filter(Boolean);
    var bizKey = SORT_TO_BIZ[key] || 'rating';
    var center = null;
    if (bizKey === 'distance') {
      // The live fix the map just got beats both the caller's `from` and the
      // map centre, so 距离最近 always agrees with the map's own ordering.
      var live = A.liveFix || (App.state.nearby && App.state.nearby.fix) || null;
      if (live && typeof live.lat === 'number') center = { lat: live.lat, lng: (live.lng != null ? live.lng : live.lon) };
      else if (ctx.from) center = { lat: ctx.from[0], lng: ctx.from[1] };
      else if (map) { var c = map.getCenter(); center = { lat: c.lat, lng: c.lng }; }
      else bizKey = 'rating';
    }
    return biz.sortRows(rows, bizKey, center).map(function (r) { return r.id; });
  };
  /** counts(filters) — FILTER-02: every option's count with the other conditions unchanged. */
  var _counts = { key: '', out: null };
  Data.counts = function (f) {
    var key = 'c' + filterKey(f);
    if (_counts.out && _counts.key === key) return _counts.out;
    var out = { total: 0, region: {}, budgets: {}, cuisines: {}, groups: {}, awards: {}, bookable: 0, fav: 0, gcal: 0, foreignBlocked: 0, hiddenBlack: 0, rating: {} };
    var clone = function (over) { return Object.assign({}, f, over || {}); };
    var count = function (ff) {
      biz.setFilterState(toBizFilter(ff));
      var n = 0, rows = Data.restaurants;
      for (var i = 0; i < rows.length; i++) if (biz.passesFilter(rows[i])) n++;
      return n;
    };
    out.total = count(f);
    var fNoRegion = toBizFilter(clone({ region: null }));
    biz.setFilterState(fNoRegion);
    Data.restaurants.forEach(function (r) { if (biz.passesFilter(r) && r.pref != null) out.region[r.pref] = (out.region[r.pref] || 0) + 1; });
    config.PRICE_BUCKETS.forEach(function (b) { out.budgets[b.key] = count(clone({ budgets: new Set([b.key]) })); });
    config.ALL_CUISINES.forEach(function (c) { out.cuisines[c] = count(clone({ cuisines: new Set([c]) })); });
    config.MEAL_GROUPS.forEach(function (g) { out.groups[g.name] = count(clone({ cuisines: new Set(g.buckets) })); });
    config.AWARD_TAGS.forEach(function (a) { out.awards[a.slug] = count(clone({ awards: new Set([a.slug]) })); });
    out.bookable = count(clone({ bookableOnly: true }));
    out.fav = count(clone({ favOnly: true }));
    out.gcal = count(clone({ gcalOnly: true }));
    out.foreignBlocked = count(clone({ hideForeign: false })) - count(clone({ hideForeign: true }));
    out.hiddenBlack = count(clone({ hideBlack: false })) - count(clone({ hideBlack: true }));
    config.RATING_QUICK.forEach(function (q) { out.rating[q] = count(clone({ ratingMin: q })); });
    out.rating[config.RATING_MIN] = count(clone({ ratingMin: config.RATING_MIN }));
    biz.setFilterState(toBizFilter(f));     // leave the live state where it was
    _counts = { key: key, out: out };
    return out;
  };
  Data.summaryGroups = function (f, counts) {
    var g = [];
    if (f.region !== null && f.region !== undefined) g.push({ key: 'region', label: '地区：{name}', params: { name: Data.regionName(f.region, App.state.lang) }, clear: { region: null } });
    if (f.ratingMin > config.RATING_MIN + 1e-9) g.push({ key: 'rating', label: '评分 ≥ {n}', params: { n: f.ratingMin.toFixed(2) }, clear: { ratingMin: config.RATING_MIN } });
    if (f.budgets && f.budgets.size < config.PRICE_BUCKETS.length) g.push({ key: 'budgets', label: '预算：{n} 档', params: { n: f.budgets.size }, clear: { budgets: new Set(PRICE_KEYS) } });
    if (f.cuisines && f.cuisines.size < config.ALL_CUISINES.length) g.push({ key: 'cuisines', label: '菜系：{n} 类', params: { n: f.cuisines.size }, clear: { cuisines: new Set(ALL_CUISINES) } });
    if (f.awards && f.awards.size) g.push({ key: 'awards', label: '获奖：{n} 种', params: { n: f.awards.size }, clear: { awards: new Set() } });
    if (f.bookableOnly) g.push({ key: 'bookableOnly', label: '只看可网订', clear: { bookableOnly: false } });
    if (f.favOnly) g.push({ key: 'favOnly', label: '只看已收藏', clear: { favOnly: false } });
    if (f.hideForeign && counts && counts.foreignBlocked > 0) g.push({ key: 'hideForeign', label: '已启用：隐藏非日本料理', clear: { hideForeign: false } });
    return g;
  };
  Data.summaryCount = function (f, counts) { return Data.summaryGroups(f, counts).length; };

  /** inJapan({lat,lng}) — the production coverage test (bbox + the west-edge
   *  staircase that keeps a Korean or Russian fix out). One copy, Business's. */
  Data.inJapan = function (ll) { try { return !!biz.uxInJapan(ll); } catch (e) { return false; } };

  /**
   * search(query, {view:{zoom, bounds:{W,E,S,N}}|null, dropLoc}) — the 3.2.x
   * local matcher (name canonicalisation, location token, viewport bias) plus
   * the cuisine shortcuts. Places (Nominatim) are the overlays module's own
   * async fetch; `places` here is always [].
   */
  Data.search = function (query, opts) {
    opts = opts || {};
    var res = { cuisines: [], restaurants: [], places: [], local: null };
    var q = (query || '').trim();
    if (!q) return res;
    var local = biz.ssMatchLocal(q, !!opts.dropLoc, opts.view || null);
    res.local = local;
    res.restaurants = local.items.slice();
    res.cuisines = biz.ssMatchGenres(q).map(function (g) {
      return { cat: g.bucket, count: g.n, group: config.GROUP_OF[g.bucket] || null,
               foreign: biz.FOREIGN_GENRES.has(g.bucket) ? 1 : 0, emoji: config.GENRE_EMOJI[g.bucket] };
    });
    return res;
  };

  // ---- bookmarks / lists / landmarks (production shapes) ------------------
  /**
   * The landmark tombstones — `{id:'fb-…', category:'hidden'}` rows inside the
   * bookmarks array. Derived from the array on every read rather than read off
   * Business's own `hiddenBuiltinIds`: that Set is only ever *maintained*
   * (hideLandmark adds, unhideLandmark deletes, a pull rebuilds it), and the
   * 3.2.x call that built it at boot lived inside renderFavoritesBuiltin —
   * presentation code that 4.0 replaced. Without this, a user who hid a
   * built-in saw it come back on the next load. Same predicate as
   * `rebuildHiddenIds()`, which stays the only writer of the Business Set.
   */
  function deriveHiddenLandmarks() {
    var out = new Set();
    (biz ? biz.bookmarks : []).forEach(function (bm) {
      if (bm && bm.category === 'hidden' && typeof bm.id === 'string' && bm.id.indexOf('fb-') === 0) out.add(bm.id);
    });
    return out;
  }
  Data.hiddenLandmarkIds = deriveHiddenLandmarks;
  Data.visibleLandmarks = function (user, layers) {
    var hidden = Data.hiddenLandmarkIds();
    return (Data.landmarks || []).filter(function (lm) { return layers.hiddenLandmarks || !hidden.has(lm.id); });
  };
  /** pins(bookmarks) — real places only. The coordinate guard is the load-bearing
   *  one (verify_build `subcollections`): a metadata entry must never become a marker. */
  Data.pins = function (bookmarks) {
    return (bookmarks || []).filter(function (bm) {
      if (!bm || bm.category === 'hidden' || bm.category === 'meta') return false;
      if (typeof bm.lat !== 'number' || typeof bm.lon !== 'number') return false;
      return true;
    });
  };
  Data.lists = function () { return biz ? biz.flLists() : []; };
  Data.members = function (bookmarks, listId) { return biz ? biz.flMembers(listId) : []; };
  Data.listsOf = function (bookmarks, ref) { return biz ? biz.flListsOf(ref) : []; };
  Data.memberIsOrphan = function () { return false; };   // flMembers() already hides orphans (swept on write)
  Data.newListId = function () { return null; };          // ids are minted by Business.flCreate
  Data.memberId = function () { return null; };
  Data.newPinId = function () { return null; };
  /** savedGroups(state) → [{key, label, emoji, ids, list, kind}] via the 3.2.x favBuildGroups(). */
  Data.savedGroups = function (state) {
    var groups = biz.favBuildGroups(state.saved.groupBy === 'list' ? 'list' : 'city');
    return groups.map(function (g) {
      return {
        key: g.key, label: g.label, emoji: g.emoji || '', ja: !!g.ja, listId: g.listId, unfiled: !!g.unfiled,
        list: g.listId ? biz.flFindList(g.listId) : null,
        ids: g.items.map(function (it) { return it.ref; }),
        items: g.items
      };
    });
  };

  /* ========================================================================
     mirrors: Business → App.state
     ==================================================================== */
  // App.set compares Sets by content but arrays by identity, so handing it a
  // fresh biz.bookmarks.slice() on every fav toggle marks `user.bookmarks`
  // changed and makes map and list repaint every pin and landmark for an edit
  // that touched neither. Only hand over a new array when the store actually
  // moved. The bookmarks array is tens of entries, so the stringify costs far
  // less than the repaint it avoids.
  var lastBmJson = null, lastBmArr = null;
  function mirroredBookmarks() {
    var json = JSON.stringify(biz.bookmarks);
    if (json !== lastBmJson || lastBmArr === null) { lastBmJson = json; lastBmArr = biz.bookmarks.slice(); }
    return lastBmArr;
  }
  function mirrorUser(kind, id, added) {
    if (!biz) return;
    _userSeq++;
    if (!A.booted) return;
    App.set({ user: { fav: new Set(biz.state.fav), black: new Set(biz.state.black), bookmarks: mirroredBookmarks() } });
    App.emit('user:changed', { kind: kind || 'sync', id: id || null, added: added });
  }
  function mirrorAccount() {
    var a = B.configured();
    var patch = { signedIn: !!a, account: { signedIn: !!a, email: a && a.email || '', name: a && a.name || '', picture: a && a.picture || '' } };
    if (!A.booted) return patch;
    App.set(patch);
    App.emit('account:changed', patch.account);
  }
  function mirrorSync(s) {
    if (!A.booted || !s) return;
    App.set({ sync: { text: s.text || '', kind: s.kind || '', dirty: !!s.dirty, retryVisible: !!s.retryVisible,
                      storageInfo: s.storageInfo || '', hintDismissed: biz ? biz.hintDismissed() : true } });
    App.emit('sync:changed', App.state.sync);
  }

  /* ========================================================================
     hooks: Business → Adapter
     ==================================================================== */
  var pendingSw = false, pendingOnline = null;
  var hooks = {
    onStatus: function (s) { mirrorSync(s); },
    onSyncUi: function (s) {
      mirrorSync(s);
      // M-013: the first time this browser holds something of its own to lose.
      if (s && s.dirty && biz) biz.requestPersistOnce();
    },
    onStorageInfo: function () { if (biz) mirrorSync(biz.syncStatus); },
    onAuthChanged: function () { mirrorAccount(); },
    onAuthMessage: function (text, color) {
      if (!A.booted) return;
      App.set({ account: { message: text || '', messageColor: color || '' } });
    },
    onUserChanged: function () { mirrorUser('sync'); },
    onBookmarksChanged: function () { mirrorUser('bookmarks'); },
    toast: function (text, opts) {
      opts = opts || {};
      if (!A.booted) return;
      var t = { kind: opts.onAction ? 'undo' : 'info', text: text, count: 0, undo: null };
      // additive: business toasts carry a callback rather than an undo record
      var id;
      // The action dismisses its own toast, so `list` only has to call run()
      // — the same contract as the built-in undo record.
      if (opts.onAction) {
        t.action = { label: opts.actionLabel || window.t('撤销'), run: function () {
          try { opts.onAction(); } finally { App.act.dismissToast(id); }
        } };
      }
      // core's showToast() now carries `action` and an explicit `remainingMs`
      // through (it used to rebuild the record from the fields it knew and drop
      // them, which left every Business undo without its button).
      if (typeof opts.ms === 'number') t.remainingMs = opts.ms;
      id = App.act.showToast(t);
    },
    announce: function (text) {
      var el = document.getElementById('sr-live');
      if (el) el.textContent = text || '';
    },
    onSwUpdate: function () { pendingSw = true; if (A.booted) App.set({ notices: { newVersion: true } }); },
    onOnline: function (online) { pendingOnline = online; if (A.booted) App.set({ notices: { offline: !online } }); },
    onInstallState: function (st) { if (A.booted) App.set({ install: st }); },
    showInstallHelp: function (info) { if (A.booted) App.act.openOverlay('help', { section: 'install', install: info }); },
    openListForm: function (listId, cb) {
      if (!A.booted) return;
      var cur = listId ? biz.flFindList(listId) : null;
      App.act.openOverlay('listForm', { mode: cur ? 'edit' : 'create', listId: listId || null,
        draft: { name: cur ? cur.name : '', emoji: cur ? cur.emoji : biz.FL_DEFAULT_EMOJI }, onSaved: cb || null });
    },
    openAccount: function () { if (A.booted) App.act.openOverlay('account', null); },
    onDynamicObservers: function () { /* attached in wireObservers() once the shell exists */ },
    onBootStatus: function (text) { A.bootStatus = text; App.emit('boot:status', text); },
    openRestaurant: function (row) { if (row && A.booted) App.act.openDetail(row.detail_url, 'search'); },
    onNativeSettings: function (rec) { if (A.booted) App.set({ nativeSettings: { label: rec.label } }); },
    onData: function (data) { onData(data); }
  };

  /* ========================================================================
     act: the intents modules call. Business is the only writer of user data.
     ==================================================================== */
  function wireActs() {
    var act = App.act;
    /** validRef(id) — a Saved/Hidden entry is always a Tabelog detail_url that
     *  is in the corpus. Junk (null, a number, a stale id) would go straight
     *  into omakase_state_cache_v2 and from there into the synced KV blob, so
     *  it is refused at the seam. (map coreRequest #5) */
    function validRef(id) { return typeof id === 'string' && !!Data.byId(id); }
    act.toggleFav = function (id) {
      if (!validRef(id)) { console.warn('[adapter] toggleFav ignored a ref that is not a known restaurant:', id); return false; }
      var was = biz.state.fav.has(id);
      biz.toggleFav(id);                       // → schedulePush → saveCache → onUserChanged
      mirrorUser('fav', id, !was);
      return !was;
    };
    act.setBlack = function (id, on) {
      if (!validRef(id)) { console.warn('[adapter] setBlack ignored a ref that is not a known restaurant:', id); return false; }
      var was = biz.state.black.has(id);
      if (was === !!on) return false;
      biz.toggleBlack(id);
      mirrorUser('black', id, !!on);
      return true;
    };
    /** batch(fn) — N toggles, one push: fn(toggleFav, toggleBlack) with opts.defer, then one schedulePush. */
    act.batch = function (fn) {
      fn(function (id) { if (validRef(id)) biz.toggleFav(id, { defer: true }); },
         function (id) { if (validRef(id)) biz.toggleBlack(id, { defer: true }); });
      biz.schedulePush();
      mirrorUser('batch');
    };
    // pins / landmarks
    act.addPin = function (spec) { var bm = biz.addPin(spec); mirrorUser('bookmarks', bm.id, true); return bm; };
    act.removePin = function (bm) {
      var r = biz.removePin(bm); if (!r) return null;
      mirrorUser('bookmarks', bm.id, false);
      hooks.toast(window.t('已删除') + ' ' + biz.bmDisplayName(bm), { actionLabel: window.t('撤销'), ms: 9000,
        onAction: function () { if (biz.restorePin(r.removed, r.at)) { mirrorUser('bookmarks', bm.id, true); hooks.announce(window.t('已恢复') + ' ' + biz.bmDisplayName(r.removed)); } } });
      return r;
    };
    /** editPin(bm, {name, emoji, category}) — mutate a user pin in place.
     *  The id is deliberately NOT editable: every category:'meta' member row
     *  references the pin by id, and a new id would orphan all of them. */
    act.editPin = function (bm, patch) {
      if (!bm || typeof bm.id !== 'string' || !patch) return false;
      var ok = biz.editPin(bm, patch);
      if (ok) mirrorUser('bookmarks', bm.id);
      return ok;
    };
    act.hideLandmark = function (lm) { biz.hideLandmark(lm); mirrorUser('bookmarks', lm.id, false); };
    act.unhideLandmark = function (lm) { biz.unhideLandmark(lm); mirrorUser('bookmarks', lm.id, true); };
    // lists (sub-collections stay inside the bookmarks array — Business decides the shape)
    act.createList = function (name, emoji) { var id = biz.flCreate(name, emoji); mirrorUser('bookmarks', id, true); return id; };
    act.renameList = function (id, name, emoji) { var ok = biz.flRename(id, name, emoji); mirrorUser('bookmarks', id); return ok; };
    act.deleteList = function (id) { var ok = biz.flDelete(id); mirrorUser('bookmarks', id, false); return ok; };
    act.addToList = function (listId, ref) { var ok = biz.flAdd(listId, ref); mirrorUser('bookmarks', ref, true); return ok; };
    act.removeFromList = function (listId, ref) { var ok = biz.flRemove(listId, ref); mirrorUser('bookmarks', ref); return ok; };
    act.setListFocus = function (listId) {
      var refs = listId ? new Set(biz.flMembers(listId).filter(function (r) { return r.indexOf('http') === 0; })) : null;
      biz.setFavFocus(refs);
      App.set({ saved: { onlyList: listId || null } });
      _userSeq++;
      App.emit('filters:changed', App.state.filters);
    };
    // layers — persisted here as well as in the render subscription below:
    // the subscription runs on the next animation frame, and a toggle
    // followed straight by a tab close (or a pagehide) would otherwise be
    // lost. Both writes are idempotent.
    act.setLayers = function (patch) {
      App.set({ layers: patch });
      persistLayers(App.state.layers, true);
      applyTileDpr(App.state.layers);
    };
    // account / sync
    act.signIn = function (container) { biz.renderSignInButton(container); };
    act.signOut = function (clearLocal) { B.signOut({ clearLocal: !!clearLocal }); };
    act.retrySync = function () { biz.retrySyncNow(); };
    act.deleteCloud = function (cb) { biz.deleteCloudData(cb); };
    act.dismissSyncHint = function () { biz.dismissSyncHint(); mirrorSync(biz.syncStatus); };
    act.refreshStorage = function () { biz.refreshStorageEstimate(); biz.refreshPersisted(); };
    // backup
    act.exportBackup = function () { biz.downloadBackup(); };
    act.readImportFile = function (file, cb) { biz.readImportFile(file, cb); };
    act.applyImport = function (norm, picks) { var r = biz.importApply(norm, picks); mirrorUser('import'); return r; };
    // language: a full navigation, carrying the open card and the typed query
    act.setLanguage = function (uiLang) {
      var s = App.state;
      biz.setLanguage(UI_TO_LANG[uiLang] || 'zh-CN', { u: s.selected.id || '', q: s.search.query || '' });
    };
    // service worker / install
    act.acceptUpdate = function () { B.swAcceptUpdate(); };
    act.laterUpdate = function () { App.set({ notices: { newVersion: false } }); };
    act.install = function () { biz.obTriggerInstall(null); };
    act.snoozeInstall = function () { biz.obSnooze(30); };
    act.neverInstall = function () { biz.obNever(); };
    act.recordInstallSnack = function () { biz.obRecordSnackShown(); };
    // Android shell only — the row is rendered from state.nativeSettings.label
    // and this opens the shell's own screen (text size, notifications).
    act.openNativeSettings = function () { if (B.nativeSettings) B.nativeSettings.open(); };
    act.markIntroSeen = function () { Prefs.markIntroSeen(); App.set({ notices: { introSeen: true } }); };
    act.dismissOovForever = function () { Prefs.dismissOov(); App.set({ notices: { oov: { dismissedForever: true } } }); };
    // share / translate
    act.share = function (id, ev) { var d = Data.byId(id); if (d) biz.shareRestaurant(d, ev); };
    act.shareUrl = function (id) { var d = Data.byId(id); return d ? biz.shareUrlFor(d) : ''; };
    act.translateJa = function (text) { return biz.googleTranslateJa(text); };
    // nearby / geolocation are the map module's (plugin); the planning
    // context it needs persisted rides in tabelog.listView through the
    // `nearby` state slice: {active, planning:{region, sort, center, zoom}}
  }

  /* ========================================================================
     persistence subscriptions
     ==================================================================== */
  // 3.2.2: @2x tiles only while neither rail bucket is on (TILE_DPR_SWITCH_JS
  // owns the flag; this only flips it and asks the base layer to repaint).
  function applyTileDpr(layers) {
    var hi = !(layers.long || layers.city);
    if (window.__tilesHiDpi === hi) return;
    window.__tilesHiDpi = hi;
    try {
      var base = null;
      map.eachLayer(function (l) { if (!base && window.L && l instanceof L.TileLayer && l._url) base = l; });
      if (base) base.redraw();
    } catch (_) {}
  }

  /**
   * persistLayers(layers, fromUser) — the four tabelog.show* keys.
   *
   * A rail bucket that is off because its R2 overlay failed to load is NOT a
   * preference change: the map module turns the toggle off and raises
   * `layers.error[bucket]`, and writing that straight through means one
   * offline start (or one flaky fetch) permanently erases a setting the user
   * chose. So the render subscription skips an errored bucket; an explicit
   * act.setLayers() still writes whatever the user just chose.
   */
  function persistLayers(layers, fromUser) {
    var out = layers;
    if (!fromUser && layers.error) {
      out = Object.assign({}, layers);
      if (layers.error.long) delete out.long;
      if (layers.error.city) delete out.city;
    }
    Prefs.writeLayers(out);
  }

  function wirePersistence() {
    App.on('filters:changed', function () { Prefs.writeFilterState(App.state.filters); Prefs.writeListView(App.state); });
    App.subscribe(function (state, changed) {
      if (App.changedAny(changed, ['layers'])) {
        persistLayers(state.layers, false);
        applyTileDpr(state.layers);
      }
      if (App.changedAny(changed, ['sort', 'sheet', 'columns', 'multi', 'nearby'])) Prefs.writeListView(state);
    });
  }

  /**
   * The one pagehide flush, and it is registered LAST on purpose.
   *
   * Business's flushOnHide() is the M-045 keepalive PUT: the edit still inside
   * the 2.5 s push debounce when the tab closes. pagehide listeners run in
   * registration order, the browser is already tearing the page down, and a
   * keepalive request issued a few milliseconds late is simply dropped — which
   * is a lost cloud write, i.e. exactly the failure CLAUDE.md's cardinal rule
   * is about. Writing tabelog.mapView / the layer toggles / listView is
   * cosmetic by comparison, so it queues behind the network call rather than
   * in front of it. (tests/sync scenario G3 is the regression that found this:
   * with these three listeners registered before startSync(), A's keepalive
   * PUT never reached the Worker.)
   */
  function wireUnloadFlush() {
    window.addEventListener('pagehide', function () {
      try { saveViewNow(); } catch (_) {}
      try { persistLayers(App.state.layers, false); } catch (_) {}
      try { Prefs.writeListView(App.state); } catch (_) {}
    });
  }

  /* ========================================================================
     compatibility shims — what the pre-4.0 tests and the Android shell reach
     for by name. Thin, delegating, and listed in PORT-PLAN §7.
     ==================================================================== */
  function wireCompatShims() {
    // tests/sync/mc_lib.py taps a synthetic .ff-fav-btn / .ff-black-btn[data-url]
    document.addEventListener('click', function (e) {
      var favBtn = e.target.closest && e.target.closest('.ff-fav-btn');
      var blackBtn = e.target.closest && e.target.closest('.ff-black-btn');
      var btn = favBtn || blackBtn;
      if (!btn) return;
      var url = btn.getAttribute('data-url');
      if (!url || !Data.byId(url)) return;
      if (favBtn) App.act.toggleFav(url); else App.act.setBlack(url, !biz.state.black.has(url));
    });
    window.__wbMode = function () { var m = App.state.layout.mode; return m === 'narrow' ? 'phone' : m; };
    window.__wbSetTab = function (tab) { App.act.setTab(tab === 'fav' ? 'saved' : (tab === 'filter' ? 'filters' : 'results')); };
    window.__wbFavDrawer = {
      open: function () { App.act.setTab('saved'); },
      close: function () { if (App.state.layout.mode === 'narrow') App.act.setSheet('collapsed'); },
      isOpen: function () { return App.state.layout.mode === 'narrow' && App.state.sheet.state !== 'collapsed'; }
    };
    window.__favCount = function () { return biz.state.fav.size; };
    window.__favFocus = function (listId) { App.act.setListFocus(listId); };
    window.__uxFindNearby = function () { App.emit('map:locate-request'); };
    window.__adapter = A;
  }

  function wireObservers() {
    // Apple-PNG emoji swap for text that modules did not build through
    // ctx.emoji (user-typed names, popups HTML). Text localisation is t()'s
    // job — every module string goes through it — so i18n=false here: a
    // second run-based pass over the roots would re-translate Japanese
    // source text that lacks lang="ja". Modules mark Japanese with lang="ja".
    ['list-root', 'detail-root', 'detail-foot', 'filters-root', 'search-root', 'overlay-root', 'modal-root', 'notice-root', 'sheet-head', 'col-left-head', 'col-detail-head']
      .forEach(function (id) { var el = document.getElementById(id); if (el) B.observeDynamic(el, true, false); });
  }

  /* ========================================================================
     boot sequence
     ==================================================================== */
  function seedState(s) {
    var lv = Prefs.readListView();
    s.filters = Prefs.readFilterState(s.filters);
    s.sort = lv.sort;
    s.sheet.tab = lv.tab;
    s.layers = Object.assign(s.layers, Prefs.readLayers());
    // tabelog.mapView is restored into Leaflet itself by map.py's
    // VIEW_RESTORE_SNIPPET, before any of this runs — so read it off the map
    // rather than off the key, and the two can never disagree.
    try {
      var c0 = map.getCenter();
      s.mapView = { center: [c0.lat, c0.lng], zoom: map.getZoom() };
    } catch (_) {}
    s.columns.userLeftPreference = lv.leftCollapsed ? 'closed' : null;
    s.nearby = { active: lv.nearbyActive, planning: lv.planningContext, pending: false, fix: Prefs.readLastLocation() };
    s.user = { fav: new Set(biz.state.fav), black: new Set(biz.state.black), bookmarks: mirroredBookmarks() };
    var acc = mirrorAccount();
    s.signedIn = acc.signedIn; s.account = Object.assign(s.account, acc.account);
    s.sync = Object.assign(s.sync, { text: biz.syncStatus.text, kind: biz.syncStatus.kind, dirty: !!biz.syncStatus.dirty,
      retryVisible: !!biz.syncStatus.retryVisible, storageInfo: biz.syncStatus.storageInfo || '', hintDismissed: biz.hintDismissed() });
    s.notices.offline = pendingOnline === null ? (navigator.onLine === false) : !pendingOnline;
    s.notices.newVersion = pendingSw;
    s.notices.oov.dismissedForever = Prefs.oovDismissed();
    s.notices.introSeen = Prefs.seenIntro();
    s.notices.langChosen = Prefs.langChosen();
    s.install = biz.installState();
    // The Android shell's appBridge runs inside Business.init, i.e. before
    // App exists, so its onNativeSettings hook has nowhere to land. Seed it.
    if (B.nativeSettings) s.nativeSettings = { label: B.nativeSettings.label };
    s.buildMeta = readBuildMeta();
    return s;
  }
  function readBuildMeta() {
    var el = document.getElementById('build-meta');
    var d = el ? el.dataset : {};
    return { appVersion: d.appVersion || '', scrapedAt: d.scrapedAt || '', latestScrape: d.latestScrape || '' };
  }

  function onData(data) {
    biz = B.api;
    // …and hand the same answer back to Business, so the Set it maintains
    // incrementally starts from what is on disk instead of from empty.
    deriveHiddenLandmarks().forEach(function (id) { biz.hiddenBuiltinIds.add(id); });
    buildConfig();
    buildData(data);
    // folium owns the Leaflet map (tiles, DPR switch, CORS fallback, saved-view
    // restore all key off its globals); the 4.0 shell adopts the element.
    var root = document.getElementById('map-root');
    if (root && mapEl && mapEl.parentNode !== root) root.appendChild(mapEl);
    try { map.invalidateSize(); } catch (_) {}
    Prefs.bindMapView(map);
    // Before App.boot, because App.boot runs every module's init(ctx) and a
    // module may hold on to an App.act.* reference there. These three only
    // define functions / subscribe; nothing they do reads state.
    wireActs();
    wirePersistence();
    wireCompatShims();
    App.boot({ leafletMap: map, mapEl: mapEl, lang: A.uiLang(), seed: seedState });
    A.booted = true;
    wireObservers();
    mirrorUser('boot');
    // sync starts after the UI exists so every status paint has a home
    biz.startSync();
    wireUnloadFlush();          // after startSync: see the comment on it
    // ?r= deep link and the language-switch hand-off (read once, then gone)
    var lsw = biz.consumeLangSwitch();
    var shareRow = biz.consumeShareParam(lsw && lsw.u);
    if (lsw && lsw.u && Data.byId(lsw.u)) App.act.openDetail(lsw.u, 'search');
    else if (shareRow) App.act.openDetail(shareRow.detail_url, 'search');
    else if (lsw && lsw.q) App.set({ search: { query: lsw.q } });
    A.ready = true;
    App.emit('adapter:ready');
  }

  A.start = function () {
    var tries = 0;
    (function waitForMap() {
      var el = document.querySelector('.folium-map');
      var m = el && window[el.id];
      if (!el || !m || typeof L === 'undefined' || !L.markerClusterGroup) {
        if (++tries > 200) { B.showBootFailure('deps', function () { location.reload(); }); return; }
        setTimeout(waitForMap, 50);
        return;
      }
      map = m; mapEl = el;
      B.boot(hooks);
    })();
  };
})();
