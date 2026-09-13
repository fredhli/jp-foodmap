/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/overlays.js
   Ported from ui-captures-2026-09-10/redesign/demo-4.0/js/overlays.js and
   wired to the production business layer through Data.* / App.act.* only.

   Owns: search (capsule / chips / active head / grouped suggestions /
   keyboard / the real Nominatim place lookup), the layers popover
   (LAYER-01..03), the forms (bookmark, collection, member picker, import),
   the account & data panel (A08 — real Google sign-in, sync status, backup,
   privacy, cloud deletion), the place context menu, the region picker, the
   sort menu, share, help (legend / about / shortcuts / install), the
   lightbox shell, the system notices (FEEDBACK-02) and the first-run
   language gate + intro row (M-109).

   Renders into: #search-root, #topbar-right, #overlay-root, #modal-root,
   #notice-root.

   Public API (CONTRACT.md §overlays):
     Overlays.focusSearch()          → activate search and focus the input
     Overlays.anchorRect(kind)       → anchor rect used to place that popover
     Overlays.lightboxOpen(payload)  → open the lightbox with a FLIP
   Additions:
     Overlays.pick(i) / Overlays.options()        → suggestion list (tests)
     Overlays.openTransient(kind)                 → emoji picker / lang menu
     Overlays.openBookmarkForm({lat,lon,...})     → map contextmenu entry
     Overlays.setLocation(fix|null)               → publish a location fix

   Owner of this file and css/overlays.css: the overlays agent, nobody else.
   ========================================================================== */
(function () {
  'use strict';

  var O = window.Overlays = {};
  var ctx, App, Data, t, u, ic, emoji, motion, layout, nav, act;
  var R = {};                 // roots + hosts
  var sig = {};               // per-surface render signatures
  var draft = null;           // live form draft (never re-rendered under the caret)
  var draftKey = null;
  var draftErr = {};
  var saving = false;
  var lastTrigger = null;     // element that opened the current overlay
  var transient = null;       // {kind:'emojiPicker'|'langMenu', data}
  var returnChain = null;
  var localNotices = {};      // dismissible module-local notices
  var options = [];           // current suggestion options
  var composing = false;
  var deleteArmedAt = 0, deleteTimer = null, deleteMsg = null;
  var signOutArmed = false;          // two-step 退出 confirmation
  var importPreview = null;   // {token, file, norm, counts, picks, error}
  var layersFallback = { prevSheet: null, full: false };
  var lightboxFrom = null;
  var listFilter = '';        // region picker inline filter
  var lbCustom = false;
  var capRectBefore = null;
  var langGateDone = false;
  var snackOn = false;
  var pickerLoaded = null;    // emoji-picker-element module promise

  var LANGS = [
    { key: 'zh', label: '简体' }, { key: 'tw', label: '繁體' },
    { key: 'en', label: 'EN' }, { key: 'ja', label: '日本語' }
  ];

  /* ======================================================================
     small helpers
     ==================================================================== */
  function el(id) { return document.getElementById(id); }
  function esc(s) { return u.esc(s); }
  function S() { return App.state; }
  function isNarrow(s) { return (s || S()).layout.mode === 'narrow'; }
  function clamp(v, lo, hi) { return u.clamp(v, lo, hi); }
  function langLabel(lang) { for (var i = 0; i < LANGS.length; i++) if (LANGS[i].key === lang) return LANGS[i].label; return LANGS[0].label; }
  function counts(s) {
    if (window.Filters && typeof window.Filters.counts === 'function') { try { return window.Filters.counts(); } catch (e) { /* fall through */ } }
    return Data.counts(s.filters);
  }
  function badgeCount(s) {
    if (window.Filters && typeof window.Filters.badge === 'function') { try { return window.Filters.badge(); } catch (e) { /* fall through */ } }
    return Data.summaryCount(s.filters, counts(s));
  }
  function regionLabel(s) {
    return (s.filters.region === null || s.filters.region === undefined)
      ? t('全部地区') : Data.regionName(s.filters.region, s.lang);
  }
  function priceShort(r) {
    return (!r.bucket || r.bucket === 'na') ? t('未知') : u.fmtPrice(r.bucket, { short: true });
  }
  function listName(l) { return (l && l.name) || t('未命名收藏夹'); }
  function focusIn(root) {
    if (!root) return;
    var first = root.querySelector('input:not([type=hidden]),button,[tabindex="0"],select,textarea');
    if (first) { try { first.focus({ preventScroll: true }); } catch (e) { first.focus(); } }
  }
  /** openOverlay with a clean payload (core.act.openOverlay already nulls it). */
  function openOverlay(kind, payload) { act.openOverlay(kind, payload === undefined ? null : payload); }

  function rememberTrigger() {
    var a = document.activeElement;
    lastTrigger = (a && a !== document.body && a.getBoundingClientRect) ? a : null;
  }
  function restoreTrigger() {
    // Only put the caret back on a trigger that is still THERE and visible.
    // A trigger inside a card the reader has since closed is detached or
    // hidden, and focusing it sends the caret to <body> — which is how a
    // closed detail could leave the keyboard with nowhere to be.
    if (lastTrigger && document.contains(lastTrigger) &&
        lastTrigger.getClientRects().length && !lastTrigger.closest('[inert]') &&
        !lastTrigger.closest('#parking')) {
      try { lastTrigger.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
    }
    lastTrigger = null;
  }
  function installState(s) { return (s && s.install) || {}; }
  function nearbyState(s) { return (s && s.nearby) || {}; }

  /* ======================================================================
     init
     ==================================================================== */
  O.init = function (c) {
    ctx = c; App = c.App; Data = c.Data; t = c.t; u = c.util; ic = c.icon;
    emoji = c.emoji; motion = c.motion; layout = c.layout; nav = c.nav; act = c.act;
    R.search = c.roots.searchRoot; R.pop = c.roots.overlayRoot; R.modal = c.roots.modalRoot;
    R.notice = c.roots.noticeRoot; R.fab = c.roots.fabRoot;
    R.app = el('app'); R.topRight = el('topbar-right'); R.topSearch = el('topbar-search');

    bindActions();
    bindFields();
    bindKeys();
    // The hidden <input type="file"> exists from init so it is a stable handle
    // (the 3.2.x page had #ssm-import-file in its markup); a lazily-created one
    // cannot be driven before the picker has been clicked once.
    try { ensureFileInput(); } catch (e) { console.error('[overlays] file input', e); }

    App.on('overlay:open', onOverlayOpen);
    App.on('overlay:closed', onOverlayClosed);
    App.on('map:contextmenu', function (p) {
      lastTrigger = null;
      openOverlay('placeMenu', { lat: p.lat, lon: p.lon, x: p.x, y: p.y });
    });
    App.on('map:locate-request', onLocateRequest);
    App.on('map:locate', onLocateResult);
    App.on('layers:load', onLayersLoad);
    App.on('search:cancel', onSearchLeft);
    App.on('lang:changed', function () { sig = {}; });
    // ENV-01: the install offer rides the first Save of this page load only.
    App.on('user:changed', function (p) {
      if (!p || p.kind !== 'fav' || !p.added || snackOn) return;
      var st = installState(App.state);
      if (!st.snackEligible) return;
      snackOn = true;
      try { act.recordInstallSnack(); } catch (e) { /* ignore */ }
      sig.notice = null; App.requestRender('notice');
    });
    window.addEventListener('resize', function () { sig.popPlaced = null; });
  };

  /* ---- global click / key wiring --------------------------------------- */
  function bindActions() {
    [R.search, R.pop, R.modal, R.notice, R.topRight].forEach(function (host) {
      if (!host) return;
      u.delegate(host, 'click', '[data-ov]', function (e, node) { runAction(node.dataset.ov, node, e); });
    });
    // outside click closes a non-modal popover (SEARCH-03 / LAYER-01)
    document.addEventListener('pointerdown', function (e) {
      var s = S();
      if (transient) return;
      if (s.overlay.kind && nav.SECONDARY_KINDS.indexOf(s.overlay.kind) >= 0) {
        if (R.pop.contains(e.target)) return;
        if (lastTrigger && lastTrigger.contains && lastTrigger.contains(e.target)) return;
        if (R.fab && R.fab.contains(e.target)) return;
        act.closeOverlay('outside');
        return;
      }
      if (s.search.active && !isNarrow(s)) {
        if (R.search.contains(e.target)) return;
        var panel = el('ov-search-panel');
        if (panel && panel.contains(e.target)) return;
        App.emit('search:cancel', { reason: 'outside' });
        App.set({ search: { active: false, activeIndex: -1 } });
      }
    }, true);
  }

  function bindKeys() {
    document.addEventListener('keydown', function (e) {
      if (e.isComposing || e.keyCode === 229 || composing) return;
      var tag = (e.target && e.target.tagName || '').toLowerCase();
      var typing = tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable);
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey) { e.preventDefault(); O.focusSearch(); return; }
      if (e.key === '?' && !typing) { e.preventDefault(); rememberTrigger(); openOverlay('help', { topic: 'shortcuts' }); return; }
      if (S().overlay.kind === 'lightbox' && !lbCustom) {
        if (e.key === 'ArrowLeft') { e.preventDefault(); stepLightbox(-1); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); stepLightbox(1); }
      }
    }, false);
  }

  /* ======================================================================
     overlay lifecycle — focus trap, inert background, returnTo chains
     ==================================================================== */
  function onOverlayOpen(p) {
    if (!p) return;
    if (!lastTrigger) rememberTrigger();
    if (p.kind === 'bookmarkForm' || p.kind === 'listForm') initDraft(p.kind, p.payload);
    if (p.kind === 'account') { deleteArmedAt = 0; deleteMsg = null; signOutArmed = false; }
    if (p.kind === 'regionPicker') listFilter = '';
    transient = null;
  }
  function onOverlayClosed(p) {
    setInert(false);
    var back = returnChain;
    returnChain = null;
    if (back && back.kind) { openOverlay(back.kind, back.payload); return; }
    draft = null; draftKey = null; draftErr = {}; saving = false;
    if (p && p.kind === 'layers') restoreLayersFallback();
    if (p && p.kind === 'importDialog') importPreview = null;
    restoreTrigger();
  }

  /* DEVICE PASS (DESIGN §5.3 / §14.2): the account sheet is one of the temporary
     tasks whose row in the container table says the modal layer locks background
     focus. It is rendered through the SECONDARY popover path for layering
     reasons, so the containment is applied to that node rather than by moving the
     kind into MODAL_KINDS (which would change the nav stack). `keepPop` leaves
     the popover root itself reachable. Layers / more / help stay non-modal per
     the same table, so this is keyed off the kind, not off "is a popover". */
  var MODAL_POPS = ['account'];
  function setInert(on, keepPop) {
    if (!R.app) return;
    Array.prototype.forEach.call(R.app.children, function (child) {
      if (child === R.modal) return;
      // #sr-live is the page's one aria-live region (M-089). `inert` takes a
      // subtree out of the accessibility tree, so inerting it swallows every
      // announcement made while a modal is up — including the import / export
      // results, which are raised from inside the account sheet.
      if (child.id === 'sr-live') return;
      if (keepPop && child === R.pop) { child.removeAttribute('inert'); return; }
      if (on) child.setAttribute('inert', ''); else child.removeAttribute('inert');
    });
  }

  /* ======================================================================
     render
     ==================================================================== */
  O.render = function (s) {
    renderSearch(s);
    renderChips(s);
    renderPopover(s);
    renderModal(s);
    renderNotices(s);
  };

  /* ----------------------------------------------------------------------
     1. SEARCH (SEARCH-01..05)
     -------------------------------------------------------------------- */
  function searchPlaceholder() { return t('搜索餐厅 / 景点 / 地址 …'); }

  function renderSearch(s) {
    var mode = s.layout.mode, active = !!s.search.active;
    var nb = nearbyState(s);
    var key = [mode, active, s.layout.foldCover, s.layout.foldCover && s.layout.W < 450, s.fontScale, s.lang, s.account.signedIn, s.filters.region,
               s.search.placeFilter, !!nb.fix, !!nb.active, !!nb.planning,
               s.overlay.kind === 'regionPicker', s.overlay.kind === 'account'].join('|');
    if (sig.search !== key) {
      sig.search = key; sig.sugs = null;
      buildSearch(s);
      if (active) {
        var input = el('ov-sinput');
        if (input) {
          input.value = s.search.query || '';
          motion.afterGeometry(function () { try { input.focus({ preventScroll: true }); } catch (e) { input.focus(); } });
        }
      }
    }
    if (active) renderSuggestions(s);
    publishSearchInset(s);
  }

  function buildSearch(s) {
    var active = !!s.search.active, narrow = isNarrow(s);
    var fromRect = null;
    if (active) {
      var cap = R.search.querySelector('.ov-capsule, .ov-topfield');
      fromRect = cap ? cap.getBoundingClientRect() : capRectBefore;
    } else {
      var live = R.search.querySelector('.ov-shead');
      capRectBefore = live ? live.getBoundingClientRect() : null;
    }
    if (active) {
      var head =
        '<div class="ov-shead">' +
          '<button class="icon-btn" data-ov="search-cancel" aria-label="' + esc(t('取消搜索')) + '">' + ic('back') + '</button>' +
          '<div class="ov-sfield">' + ic('search') +
            '<input id="ov-sinput" class="ov-sinput" type="search" role="combobox" aria-expanded="true" ' +
              'aria-controls="ov-sugs" aria-autocomplete="list" autocomplete="off" autocapitalize="off" spellcheck="false" ' +
              'aria-label="' + esc(t('搜索')) + '" placeholder="' + esc(searchPlaceholder()) + '">' +
            '<button class="icon-btn ov-sclear" data-ov="search-clear" aria-label="' + esc(t('清空')) + '">' + ic('x') + '</button>' +
          '</div>' +
        '</div>';
      if (narrow) {
        R.search.innerHTML = '<div class="ov-search-full">' + head + '<div class="ov-sugs" id="ov-sugs" role="listbox" aria-label="' + esc(t('搜索建议')) + '"></div></div>';
      } else {
        R.search.innerHTML = head.replace('class="ov-shead"', 'class="ov-shead ov-shead--top"');
        var panel = document.createElement('div');
        panel.className = 'ov-search-panel';
        panel.id = 'ov-search-panel';
        panel.innerHTML = '<div class="ov-sugs" id="ov-sugs" role="listbox" aria-label="' + esc(t('搜索建议')) + '"></div>';
        R.pop.appendChild(panel);
        motion.afterGeometry(function () { placeSearchPanel(S()); });
      }
      bindSearchInput();
      if (fromRect && !motion.reduced) {
        var headEl = R.search.querySelector('.ov-shead');
        var sugsEl = R.search.querySelector('.ov-sugs') || el('ov-sugs');
        if (headEl) motion.flip(headEl, fromRect, { duration: motion.standard });
        if (sugsEl) { sugsEl.classList.remove('fade-in'); void sugsEl.offsetWidth; sugsEl.classList.add('fade-in'); }
      }
      return;
    }
    var oldPanel = el('ov-search-panel'); if (oldPanel) oldPanel.remove();
    var q = s.search.query ? '<span class="ov-cap-text is-query">' + esc(s.search.query) + '</span>'
                           : '<span class="ov-cap-text">' + esc(searchPlaceholder()) + '</span>';
    var flipBack = capRectBefore;
    if (narrow) {
      var nb = nearbyState(s);
      R.search.innerHTML =
        '<div class="ov-caprow">' +
          '<button class="ov-capsule glass" data-ov="search-activate" aria-label="' + esc(t('搜索')) + '">' + ic('search') + q + '</button>' +
          avatarHtml(s, 'glass') +
        '</div>' +
        '<div class="ov-chips">' +
          '<button class="chip chip-tall glass" data-ov="open" data-kind="regionPicker" aria-haspopup="dialog" aria-expanded="' + (s.overlay.kind === 'regionPicker') + '">' +
            '<span class="ov-chip-pin">' + emoji.img('📍', 16) + '</span>' + esc(regionLabel(s)) + ic('chevronDown', { cls: 'ic-sm' }) +
          '</button>' +
          '<button class="chip chip-tall glass' + (nb.active ? ' is-on' : '') + '" data-ov="nearby" aria-pressed="' + (!!nb.active) + '">' +
            ic('locate', { cls: 'ic-sm' }) + esc(t('附近')) + '</button>' +
          (nb.active && nb.planning ? '<button class="chip chip-tall glass" data-ov="restore-plan">' +
            ic('back', { cls: 'ic-sm' }) + esc(t('回到规划')) + '</button>' : '') +
        '</div>';
      if (s.layout.foldCover) {
        var row = R.search.querySelector('.ov-caprow'), chips = R.search.querySelector('.ov-chips');
        var region = chips.querySelector('[data-kind="regionPicker"]'), nearby = chips.querySelector('[data-ov="nearby"]');
        region.classList.add('ov-cover-region');
        region.setAttribute('aria-label', regionLabel(s));
        var label = document.createElement('span'); label.className = 'ov-cover-label';
        label.textContent = regionLabel(s);
        region.childNodes[1].replaceWith(label);
        nearby.setAttribute('aria-label', t('附近'));
        nearby.classList.add('ov-cover-nearby');
        if (s.fontScale >= 130 || s.layout.W < 450) nearby.classList.add('ov-cover-icon');
        var nearLabel = document.createElement('span'); nearLabel.className = 'ov-cover-near-label'; nearLabel.textContent = t('附近');
        nearby.lastChild.replaceWith(nearLabel);
        row.insertBefore(region, row.lastElementChild); row.insertBefore(nearby, row.lastElementChild);
        if (!chips.children.length) chips.remove();
      }
    } else {
      R.search.innerHTML = '<button class="ov-topfield data-ov="search-activate" aria-label="' + esc(t('搜索')) + '">' + ic('search') + q + '</button>';
    }
    if (flipBack && !motion.reduced) {
      var back = R.search.querySelector('.ov-capsule, .ov-topfield');
      if (back) motion.flip(back, flipBack, { duration: motion.standard });
    }
    capRectBefore = null;
  }

  function avatarHtml(s, cls) {
    var signedIn = !!s.account.signedIn;
    var label = signedIn ? t('账户与数据') : t('登录与数据');
    var initial = (s.account.email || s.account.name || '').trim().charAt(0).toUpperCase();
    var dot = (!signedIn || s.sync.dirty || s.sync.kind === 'err') ? '<span class="ov-dot" aria-hidden="true"></span>' : '';
    return '<button class="ov-avatar ' + (cls || '') + (signedIn ? ' is-in' : '') + '" data-ov="open" data-kind="account" ' +
      'aria-haspopup="dialog" aria-expanded="' + (s.overlay.kind === 'account') + '" aria-label="' + esc(label) + '">' +
      (signedIn && initial ? esc(initial) : ic('user')) + dot + '</button>';
  }

  function bindSearchInput() {
    var input = el('ov-sinput');
    if (!input) return;
    input.addEventListener('compositionstart', function () { composing = true; });
    input.addEventListener('compositionend', function () { composing = false; onQuery(input.value); });
    input.addEventListener('input', function () { if (!composing) onQuery(input.value); });
    input.addEventListener('keydown', function (e) {
      if (e.isComposing || e.keyCode === 229 || composing) return;   // IME owns the keys (I18N-02)
      if (e.key === 'ArrowDown') { e.preventDefault(); moveActive(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); moveActive(-1); }
      else if (e.key === 'Enter') {
        if (!options.length) return;
        e.preventDefault();
        var i = S().search.activeIndex;
        O.pick(i >= 0 ? i : 0);
      }
    });
  }

  function moveActive(d) {
    if (!options.length) return;
    var i = S().search.activeIndex;
    i = i < 0 ? (d > 0 ? 0 : options.length - 1) : (i + d + options.length) % options.length;
    App.set({ search: { activeIndex: i } });
  }

  /* ---- the real remote place lookup (was ssSearch / ssParseResult) ------
     Nominatim is CORS-open; the OSM usage policy is ≤1 req/s. M-055: an
     800 ms debounce plus a length floor keeps one typed query to roughly one
     request (a 300 ms autocomplete is exactly the pattern the policy
     forbids). M-082: a 9 s timeout, because Nominatim occasionally accepts a
     connection and then answers nothing. Stale responses are dropped by seq.
     -------------------------------------------------------------------- */
  var api = { items: null, pending: false, error: null, q: '' };
  var apiSeq = 0, apiDebounce = 0, apiAbort = null, apiAbortTimer = 0;
  var SS_TIMEOUT_MS = 9000, SS_DEBOUNCE_MS = 800;
  // Built from escapes on purpose: a literal class here would be scanned as
  // untranslated UI copy by the build's CJK-run pass.
  var SS_CJK_RE = new RegExp('[' + '\u3040-\u30FF\u3400-\u9FFF\uF900-\uFAFF\uAC00-\uD7AF' + ']');
  var SS_TYPE_ZOOM = {
    country: 8, state: 8, region: 8, province: 8, prefecture: 8,
    county: 10, island: 10,
    city: 12, municipality: 12, town: 13, village: 14,
    city_district: 14, district: 14, borough: 14, ward: 14,
    suburb: 14, quarter: 15, neighbourhood: 15, postcode: 14
  };
  function ssTargetZoom(it) {
    var k = it.addresstype || it.type || '';
    var z = SS_TYPE_ZOOM[k];
    if (z == null && it.category === 'boundary') z = 12;
    return z == null ? 16 : z;
  }
  function ssShouldQueryApi(v) {
    if (!v) return false;
    return SS_CJK_RE.test(v) ? v.length >= 2 : v.length >= 3;
  }
  function ssAcceptLanguage(lang) {
    if (lang === 'tw') return 'zh-TW,zh-Hant,zh,ja,en';
    if (lang === 'en') return 'en,ja';
    if (lang === 'ja') return 'ja,en';
    return 'zh-CN,zh,ja,en';
  }
  function ssParseResult(r) {
    var nd = r.namedetails || {};
    var name = nd['name:zh'] || nd['name:zh-Hans'] || nd['name:zh-Hant'] ||
               nd['name:ja'] || r.name || nd.name ||
               (r.display_name || '').split(',')[0].trim();
    var bb = null;
    if (Array.isArray(r.boundingbox) && r.boundingbox.length === 4) {
      var s1 = parseFloat(r.boundingbox[0]), n1 = parseFloat(r.boundingbox[1]),
          w1 = parseFloat(r.boundingbox[2]), e1 = parseFloat(r.boundingbox[3]);
      if (!isNaN(s1) && !isNaN(n1) && !isNaN(w1) && !isNaN(e1)) bb = [s1, n1, w1, e1];
    }
    return {
      id: 'osm:' + (r.osm_type || '') + (r.osm_id || '') + ':' + r.lat + ',' + r.lon,
      lat: parseFloat(r.lat), lon: parseFloat(r.lon),
      name: name || t('未命名地点'),
      address: r.display_name || '',
      bbox: bb,
      addresstype: r.addresstype || '', type: r.type || '',
      category: r.category || r.class || ''
    };
  }
  function abortApi() {
    if (apiAbort) { try { apiAbort.abort(); } catch (e) {} apiAbort = null; }
    if (apiAbortTimer) { clearTimeout(apiAbortTimer); apiAbortTimer = 0; }
  }
  function ssSearchPlaces(q, lang) {
    var seq = ++apiSeq;
    api = { items: null, pending: true, error: null, q: q };
    sig.sugs = null; App.requestRender('search');
    abortApi();
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    apiAbort = ctrl;
    var timedOut = false;
    apiAbortTimer = setTimeout(function () {
      apiAbortTimer = 0; timedOut = true;
      if (ctrl) { try { ctrl.abort(); } catch (e) {} }
    }, SS_TIMEOUT_MS);
    // Japan bbox: lon 122-154, lat 24-46. bounded=1 forbids matches outside it.
    var url = 'https://nominatim.openstreetmap.org/search?' +
      'format=jsonv2&limit=6&addressdetails=0&namedetails=1' +
      '&viewbox=122,46,154,24&bounded=1' +
      '&accept-language=' + ssAcceptLanguage(lang) +
      '&q=' + encodeURIComponent(q);
    fetch(url, { headers: { 'Accept': 'application/json' }, signal: ctrl && ctrl.signal })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (arr) {
        if (apiAbortTimer) { clearTimeout(apiAbortTimer); apiAbortTimer = 0; }
        if (seq !== apiSeq) return;                      // stale response
        var items = (Array.isArray(arr) ? arr : []).map(ssParseResult)
          .filter(function (x) { return !isNaN(x.lat) && !isNaN(x.lon); });
        api = { items: items, pending: false, error: null, q: q };
        sig.sugs = null; App.requestRender('search');
      })
      .catch(function (err) {
        if (apiAbortTimer) { clearTimeout(apiAbortTimer); apiAbortTimer = 0; }
        var aborted = err && err.name === 'AbortError';
        if (aborted && !timedOut) return;                // superseded: stay silent
        if (seq !== apiSeq) return;
        api = { items: null, pending: false, q: q,
                error: timedOut ? t('地点搜索超时') : t('地点搜索失败') };
        sig.sugs = null; App.requestRender('search');
      });
  }
  /** onQuery — local match paints this frame, the remote call is debounced. */
  function onQuery(v) {
    var q = (v || '').trim();
    App.set({ search: { query: v, activeIndex: -1, dropLoc: false } });
    clearTimeout(apiDebounce);
    if (!q) {
      apiSeq++; abortApi();
      api = { items: null, pending: false, error: null, q: '' };
      return;
    }
    if (!ssShouldQueryApi(q)) {
      apiSeq++; abortApi();
      api = { items: null, pending: false, error: null, q: q };
      return;
    }
    if (api.q !== q) api = { items: null, pending: true, error: null, q: q };
    apiDebounce = setTimeout(function () { ssSearchPlaces(q, S().lang); }, SS_DEBOUNCE_MS);
  }

  /** current map view for the 3.2.x viewport bias inside ssMatchLocal. */
  function searchView(s) {
    var b = (window.MapMod && MapMod.bounds) ? MapMod.bounds() : null;
    if (!b) return null;
    return { zoom: (s.mapView && s.mapView.zoom) || 0,
             bounds: { W: b.west, E: b.east, S: b.south, N: b.north } };
  }

  /** cuisine row count — the same filters with just this cuisine substituted. */
  var cuisineMemo = { key: '', map: null };
  function cuisineHitCount(cat, s, memoKey) {
    if (cuisineMemo.key !== memoKey) cuisineMemo = { key: memoKey, map: {} };
    if (cuisineMemo.map[cat] !== undefined) return cuisineMemo.map[cat];
    var foreign = Data.config.FOREIGN_SET.has(cat);
    var f2 = Object.assign({}, s.filters,
      foreign ? { hideForeign: false, cuisines: new Set([cat]) } : { cuisines: new Set([cat]) });
    var n = Data.applyFilters(f2).length;
    Data.applyFilters(s.filters);          // leave the shared cache where it was
    cuisineMemo.map[cat] = n;
    return n;
  }

  /** buildOptions — SEARCH-05 groups: cuisines, restaurants, places. */
  function buildOptions(s, memoKey) {
    var dropLoc = !!s.search.dropLoc;
    var res = Data.search(s.search.query, { view: searchView(s), dropLoc: dropLoc });
    var out = [];
    res.cuisines.forEach(function (c) {
      out.push({ group: '菜系', type: 'cuisine', value: c.cat, foreign: c.foreign,
        emoji: c.emoji, name: t(c.cat),
        sub: c.foreign ? t('显示这个菜系') : t('只看这个菜系'),
        count: cuisineHitCount(c.cat, s, memoKey) });
    });
    // M-025: the location constraint is opt-out, per query.
    var loc = res.local || {};
    if (loc.locToken && !dropLoc) {
      out.push({ group: '地点', type: 'clearPlace', icon: 'x',
        name: t('去掉地点约束'), sub: loc.locToken });
    }
    res.restaurants.forEach(function (r) {
      var bits = [priceShort(r)];
      if (r.st) bits.push(r.st);
      if (r.city) bits.push(r.city);
      out.push({ group: '餐厅', type: 'restaurant', value: r.id, emoji: Data.genreEmoji(r),
        name: r.name, ja: true, sub: bits.join(' · '), rating: r.rating });
    });
    (api.items || []).forEach(function (p) {
      out.push({ group: '地点', type: 'place', value: p.id, emoji: '📍', place: p,
        name: p.name, ja: true, sub: p.address || t('地点') });
    });
    return out;
  }

  function renderSuggestions(s) {
    var list = el('ov-sugs');
    if (!list) return;
    var memoKey = [s.filters.region, s.filters.ratingMin, s.filters.budgets.size, s.filters.cuisines.size,
      s.filters.awards.size, s.filters.bookableOnly, s.filters.favOnly, s.filters.hideBlack,
      s.filters.hideForeign, s.filters.gcalOnly, s.user.fav.size, s.user.black.size].join('|');
    var key = [s.search.query, s.lang, s.search.dropLoc, s.layout.mode, memoKey,
      api.pending, api.error, api.items ? api.items.length : -1, api.q].join('|');
    if (sig.sugs !== key) {
      sig.sugs = key;
      options = s.search.query.trim() ? buildOptions(s, memoKey) : [];
      list.innerHTML = optionsHtml(s);
      if (!isNarrow(s)) motion.afterGeometry(function () { placeSearchPanel(S()); });
    }
    updateActive(s.search.activeIndex);
  }

  function optionsHtml(s) {
    var q = (s.search.query || '').trim();
    if (!q) return '<div class="ov-sug-empty t-body">' + esc(t('输入店名、菜系、车站或地点')) + '</div>';
    var html = '', group = null;
    options.forEach(function (o, i) {
      if (o.group !== group) { group = o.group; html += '<div class="ov-sug-gt">' + esc(t(group)) + '</div>'; }
      var meta = '';
      if (o.count !== undefined) meta = '<span class="ov-sug-count num">' + esc(u.fmtCount(o.count, s.lang)) + '</span>';
      else if (o.rating !== undefined && o.rating !== null) meta = '<span class="rating num">' + ic('starFill', { fill: true }) + esc(u.fmtRating(o.rating)) + '</span>';
      html +=
        '<div class="ov-sug" role="option" id="ov-sg-' + i + '" aria-selected="false" data-ov="pick" data-i="' + i + '" tabindex="-1">' +
          '<span class="ov-sug-ic">' + (o.icon ? ic(o.icon) : emoji.img(o.emoji, 22)) + '</span>' +
          '<span class="ov-sug-main"><span class="ov-sug-name"' + (o.ja ? ' lang="ja"' : '') + '>' + esc(o.name) + '</span>' +
            (o.sub ? '<span class="ov-sug-sub"' + (o.ja ? ' lang="ja"' : '') + '>' + esc(o.sub) + '</span>' : '') + '</span>' +
          (meta ? '<span class="ov-sug-meta">' + meta + '</span>' : '') +
        '</div>';
    });
    // The remote group updates on its own and never hides the local hits.
    if (api.pending && ssShouldQueryApi(q)) {
      if (group !== '地点') html += '<div class="ov-sug-gt">' + esc(t('地点')) + '</div>';
      html += '<div class="ov-sug-empty t-secondary"><span class="ov-spin" aria-hidden="true"></span>' + esc(t('正在搜索地点…')) + '</div>';
    } else if (api.error) {
      if (group !== '地点') html += '<div class="ov-sug-gt">' + esc(t('地点')) + '</div>';
      html += '<div class="ov-sug-empty t-secondary">' + esc(api.error) +
        '<button class="btn btn-quiet" data-ov="place-retry">' + esc(t('重试')) + '</button></div>';
    }
    if (!options.length && !api.pending && !api.error) {
      html += '<div class="ov-sug-empty t-body">' + esc(t('没有找到相关结果')) + '</div>';
    }
    return html;
  }

  function updateActive(idx) {
    var list = el('ov-sugs'); if (!list) return;
    var rows = list.querySelectorAll('.ov-sug');
    Array.prototype.forEach.call(rows, function (row, i) {
      var on = i === idx;
      row.classList.toggle('is-active', on);
      row.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
    });
    var input = el('ov-sinput');
    if (input) {
      if (idx >= 0 && rows[idx]) input.setAttribute('aria-activedescendant', rows[idx].id);
      else input.removeAttribute('aria-activedescendant');
    }
  }

  /** SEARCH-04 — one submit path, per-type side effects. */
  O.pick = function (i) {
    var o = options[i];
    if (!o) return;
    var s = S();
    App.emit('search:pick', { type: o.type, id: o.value, cat: o.type === 'cuisine' ? o.value : undefined, place: o.place || null });
    if (o.type === 'clearPlace') {
      // Only the current query's location constraint; filters stay untouched.
      App.set({ search: { dropLoc: true, activeIndex: -1 } });
      sig.sugs = null;
      return;                                            // stays in search (SEARCH-03)
    }
    App.set({ search: { active: false, activeIndex: -1 } });   // query kept
    if (o.type === 'restaurant') {
      if (window.MapMod && MapMod.placeTempPin) MapMod.placeTempPin(null);
      App.set({ search: { placeFilter: null } });
      act.openDetail(o.value, 'search');
    } else if (o.type === 'cuisine') {
      if (o.foreign) act.applyFilters({ hideForeign: false });
      else act.applyFilters({ cuisines: new Set([o.value]) });
    } else if (o.type === 'place') {
      gotoPlace(o.place);
    }
  };
  O.options = function () { return options.slice(); };

  /** gotoPlace — the real bbox / type-zoom move plus the temporary 📍 (M-026). */
  function gotoPlace(p) {
    if (!p) return;
    var zoom = ssTargetZoom(p);
    App.set({ search: { placeFilter: p.id } });
    var handled = false;
    if (window.MapMod && MapMod.fitPlace) {
      try { handled = MapMod.fitPlace({ id: p.id, lat: p.lat, lon: p.lon, bbox: p.bbox, zoom: zoom, name: p.name }); }
      catch (e) { handled = false; }
    }
    if (!handled && window.MapMod && MapMod.flyTo) { try { MapMod.flyTo([p.lat, p.lon], zoom); } catch (e) {} }
    if (window.MapMod && MapMod.placeTempPin) {
      try { MapMod.placeTempPin({ lat: p.lat, lon: p.lon, label: p.name }); } catch (e) {}
    }
    // M-026: only POI-level results earn the "make a pin here" bubble; the
    // centroid of a whole ward is not a place you would bookmark.
    if (zoom < 16) return;
    lastTrigger = null;
    openOverlay('placeMenu', { lat: p.lat, lon: p.lon, place: { id: p.id, name: p.name } });
  }

  function onSearchLeft() {
    sig.search = null;
    motion.afterGeometry(function () {
      var cap = R.search.querySelector('[data-ov="search-activate"]');
      if (cap) { try { cap.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
    });
  }

  O.focusSearch = function () {
    rememberTrigger();
    App.set({ search: { active: true, activeIndex: -1 } });
  };

  /** placeSearchPanel — mid: the left region; wide: a dropdown under the field. */
  function placeSearchPanel(s) {
    var panel = el('ov-search-panel');
    if (!panel) return;
    var U = s.layout.U, cols = layout.columns(s), top = U.y + cols.topbarH + 8;
    if (s.layout.mode === 'mid') {
      var w = Math.min(Math.max(cols.fullLeft || 344, 344), U.w - 32);
      panel.style.left = (U.x) + 'px';
      panel.style.top = top + 'px';
      panel.style.width = w + 'px';
      panel.style.height = (U.y + U.h - top) + 'px';
      panel.style.borderRadius = '0';
    } else {
      var field = R.search.querySelector('.ov-sfield');
      var fr = field ? field.getBoundingClientRect() : null;
      var pw = fr && fr.width ? Math.max(fr.width, 360) : 480;
      panel.style.width = pw + 'px';
      panel.style.left = clamp(fr ? fr.left : U.x + 24, U.x + 16, U.x + U.w - 16 - pw) + 'px';
      panel.style.top = top + 'px';
      panel.style.height = '';
      panel.style.maxHeight = Math.min(560, U.y + U.h - top - 24) + 'px';
    }
  }

  /** publishSearchInset — the narrow search chrome obstructs the map (MAP-02 §6). */
  var lastInset = -1;
  function publishSearchInset(s) {
    var narrow = isNarrow(s), v = 0, noticeTop = '';
    if (narrow && !s.search.active) {
      var r = R.search.getBoundingClientRect();
      v = Math.round(Math.max(0, r.bottom - s.layout.U.y));
      noticeTop = v ? (v + s.layout.U.y + 8) + 'px' : '';
    } else if (narrow) {
      var head = R.search.querySelector('.ov-shead');
      if (head) noticeTop = Math.round(head.getBoundingClientRect().bottom + 8) + 'px';
    }
    if (R.notice && R.notice.style.top !== noticeTop) R.notice.style.top = noticeTop;
    if (Math.abs(v - lastInset) < 2) return;
    lastInset = v;
    layout.setInset('search', v ? { top: v } : null);
  }

  /* ----------------------------------------------------------------------
     2. mid/wide top-bar chips (#topbar-right)
     -------------------------------------------------------------------- */
  function renderChips(s) {
    if (!R.topRight) return;
    if (isNarrow(s)) { if (sig.chips !== 'narrow') { sig.chips = 'narrow'; R.topRight.innerHTML = ''; } return; }
    var n = badgeCount(s);
    var key = ['w', s.lang, s.filters.region, n, s.sheet.tab, s.account.signedIn, s.sync.dirty,
               s.overlay.kind, transient && transient.kind,
               !!(s.nearby && s.nearby.active), !!(s.nearby && s.nearby.planning)].join('|');
    if (sig.chips === key) return;
    sig.chips = key;
    var nbw = nearbyState(s);
    R.topRight.innerHTML =
      (nbw.active && nbw.planning ? '<button class="chip ov-chip" data-ov="restore-plan">' +
        ic('back', { cls: 'ic-sm' }) + esc(t('回到规划')) + '</button>' : '') +
      '<button class="chip ov-chip" data-ov="open" data-kind="regionPicker" aria-haspopup="dialog" aria-expanded="' + (s.overlay.kind === 'regionPicker') + '">' +
        '<span class="ov-chip-pin">' + emoji.img('📍', 16) + '</span>' + esc(regionLabel(s)) + ic('chevronDown', { cls: 'ic-sm' }) +
      '</button>' +
      '<button class="chip ov-chip' + (s.sheet.tab === 'filters' ? ' is-on' : '') + '" data-ov="tab" data-tab="filters" aria-pressed="' + (s.sheet.tab === 'filters') + '">' +
        ic('filter', { cls: 'ic-sm' }) + esc(t('筛选')) + (n ? '<span class="count-badge">' + esc(u.fmtCount(n, s.lang)) + '</span>' : '') +
      '</button>' +
      '<button class="chip ov-chip" data-ov="lang-menu" aria-haspopup="menu" aria-expanded="' + (!!transient && transient.kind === 'langMenu') + '">' +
        ic('globe', { cls: 'ic-sm' }) + esc(langLabel(s.lang)) + ic('chevronDown', { cls: 'ic-sm' }) +
      '</button>' +
      avatarHtml(s, '');
  }

  /* ----------------------------------------------------------------------
     3. Non-modal popovers (#overlay-root) — LAYER-01/02/03
     -------------------------------------------------------------------- */
  var POP_TITLE = {
    layers: '地图图层', more: '更多操作', memberPicker: '加入收藏', share: '分享',
    placeMenu: '这个地点', account: '账户与数据', regionPicker: '地区', sortMenu: '排序', help: '帮助'
  };

  function renderPopover(s) {
    var k = s.overlay.kind;
    var isPop = k && nav.SECONDARY_KINDS.indexOf(k) >= 0;
    var key = isPop ? [k, s.lang, s.layout.mode, s.layout.W, s.layout.H, popState(s), transient && transient.kind].join('|') : 'none';
    if (sig.pop === key) return;
    sig.pop = key;
    var panel = el('ov-search-panel');
    R.pop.innerHTML = '';
    if (panel) { R.pop.appendChild(panel); motion.afterGeometry(function () { placeSearchPanel(S()); }); }
    if (!isPop) return;
    var node = document.createElement('div');
    node.className = 'ov-pop scale-in';
    node.id = 'ov-pop';
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-label', t(POP_TITLE[k] || k));
    var bottom = isBottomSheetKind(s, k);
    node.innerHTML = (bottom ? '<div class="ov-grip handle-hit" aria-hidden="true"><div class="handle"></div></div>' : '') + popBody(s, k);
    R.pop.appendChild(node);
    if (bottom) bindSheetDrag(node);
    if (k === 'account') mountSignIn(s, node);
    // DEVICE PASS (§5.3): temporary-task popovers lock background focus and take it.
    // Without this the account sheet covered the phone screen while Tab still
    // walked the 11 controls behind it (measured on WebKit 402x874).
    if (MODAL_POPS.indexOf(k) >= 0) {
      node.setAttribute('aria-modal', 'true');
      trapFocus(node);
      if (!node.contains(document.activeElement)) motion.afterGeometry(function () { focusIn(node); });
    }
    placePopoverWhenStable();
  }

  var placeRaf = 0;
  function placePopoverWhenStable() {
    var frames = 0, last = null;
    if (placeRaf) { cancelAnimationFrame(placeRaf); placeRaf = 0; }
    function step() {
      placeRaf = 0;
      if (!el('ov-pop')) return;
      var s = S();
      var a = O.anchorRect(s.overlay.kind);
      var key = a ? Math.round(a.top) + ':' + Math.round(a.right) : 'none';
      if (key !== last) { last = key; placePopover(s); }
      if (++frames < 32) placeRaf = requestAnimationFrame(step);
    }
    motion.afterGeometry(step);
  }

  function popState(s) {
    var k = s.overlay.kind;
    if (k === 'layers') return [s.layers.long, s.layers.city, s.layers.landmarks, s.layers.pins, s.layers.hiddenLandmarks,
      s.layers.loading.long, s.layers.loading.city, s.layers.error.long, s.layers.error.city, s.user.bookmarks.length].join(',');
    if (k === 'account') return [s.account.signedIn, s.account.email, s.account.message, s.sync.text, s.sync.kind,
      s.sync.retryVisible, s.sync.storageInfo, s.user.fav.size, s.user.black.size, s.user.bookmarks.length,
      deleteArmedAt ? 1 : 0, signOutArmed ? 1 : 0, deleteMsg && deleteMsg.text, (s.install || {}).standalone, (s.install || {}).canPrompt].join(',');
    if (k === 'regionPicker') return [s.filters.region, listFilter].join(',');
    if (k === 'sortMenu') return s.sort + '|' + JSON.stringify(s.overlay.payload || {});
    if (k === 'memberPicker') return [s.user.bookmarks.length, s.user.fav.size, JSON.stringify(s.overlay.payload || {})].join(',');
    if (k === 'help') return JSON.stringify(s.overlay.payload || {}) + '|' + JSON.stringify(s.install || {}) + '|' + JSON.stringify(s.buildMeta || {});
    if (k === 'more' || k === 'share' || k === 'placeMenu') return JSON.stringify(s.overlay.payload || {});
    return '';
  }

  function popHead(title, opts) {
    return '<div class="ov-pop-head' + (opts && opts.plain ? ' is-plain' : '') + '">' +
      '<span class="t-group-title"' + (opts && opts.ja ? ' lang="ja"' : '') + '>' + esc(title) + '</span>' +
      '<button class="icon-btn" data-ov="close" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button></div>';
  }

  function popBody(s, k) {
    switch (k) {
      case 'layers': return layersBody(s);
      case 'account': return accountBody(s);
      case 'regionPicker': return regionBody(s);
      case 'sortMenu': return sortBody(s);
      case 'more': return moreBody(s);
      case 'share': return shareBody(s);
      case 'memberPicker': return memberBody(s);
      case 'placeMenu': return placeMenuBody(s);
      case 'help': return helpBody(s);
      default: return popHead(t(POP_TITLE[k] || k));
    }
  }

  /* ---- layers (LAYER-02 / §12.1) --------------------------------------- */
  function layerRow(opts) {
    var state = opts.loading ? '<span class="ov-spin" role="status" aria-label="' + esc(t('加载中…')) + '"></span>'
      : (opts.error ? '<span class="ov-layer-err">' + esc(t('加载失败')) + '</span><button class="btn btn-quiet" data-ov="layer-retry" data-layer="' + opts.key + '">' + esc(t('重试')) + '</button>' : '');
    return '<label class="ov-layer-row' + (opts.disabled ? ' is-disabled' : '') + '">' +
      ic(opts.icon) +
      '<span class="ov-layer-main"><span class="ov-layer-title">' + esc(opts.title) + '</span>' +
        (opts.sub ? '<span class="ov-layer-sub">' + esc(opts.sub) + '</span>' : '') + '</span>' +
      '<span class="ov-layer-state">' + state +
        '<input type="checkbox" class="switch" role="switch" data-ov="layer-toggle" data-layer="' + opts.key + '"' +
        (opts.on ? ' checked' : '') + (opts.disabled ? ' disabled' : '') +
        ' aria-checked="' + (opts.on ? 'true' : 'false') + '" aria-label="' + esc(opts.title) + '"></span>' +
      '</label>';
  }

  function layersBody(s) {
    var L = s.layers;
    var builtIn = (Data.landmarks || []).length;
    var hidden = Data.hiddenLandmarkIds().size;
    var own = (s.user.bookmarks || []).filter(function (b) { return b && b.category === 'attraction'; }).length;
    return popHead(t('地图图层')) +
      '<div class="ov-pop-body">' +
        layerRow({ key: 'long', icon: 'train', title: t('长途'), sub: t('新干线 / JR 特急'), on: L.long, loading: !!L.loading.long, error: !!L.error.long }) +
        layerRow({ key: 'city', icon: 'rail', title: t('市内'), sub: t('地铁 / 私铁 / 城市轨道'), on: L.city, loading: !!L.loading.city, error: !!L.error.city }) +
        layerRow({ key: 'landmarks', icon: 'landmark', title: t('景点'),
          sub: t('内置 {n} · 自建 {m}', { n: u.fmtCount(builtIn, s.lang), m: u.fmtCount(own, s.lang) }), on: L.landmarks }) +
        layerRow({ key: 'pins', icon: 'bookmark', title: t('书签标记'), sub: t('地图上的地点标记'), on: L.pins }) +
        '<label class="ov-layer-row' + (L.landmarks ? '' : ' is-disabled') + '">' +
          '<input type="checkbox" class="checkbox" data-ov="layer-hidden"' + (L.hiddenLandmarks ? ' checked' : '') +
            ' aria-checked="' + (L.hiddenLandmarks ? 'true' : 'false') + '">' +
          '<span class="ov-layer-main"><span class="ov-layer-title">' + esc(t('也显示已隐藏的景点')) + '</span>' +
            '<span class="ov-layer-sub">' + esc(t('已隐藏 {n}', { n: u.fmtCount(hidden, s.lang) })) + '</span></span>' +
        '</label>' +
      '</div>';
  }

  // The rail layers really are fetched (three LOD files on R2), so the row's
  // state is the request's state: map emits layers:load {kind, status}.
  function onLayersLoad(p) {
    if (!p || !p.kind) return;
    clearTimeout(layerTimers[p.kind]);
    var patch = { loading: {}, error: {} };
    patch.loading[p.kind] = p.status === 'loading';
    patch.error[p.kind] = p.status === 'error';
    App.set({ layers: patch });
  }

  var layerTimers = {};
  var LAYER_TIMEOUT_MS = 20000;
  function armLayerWait(kindKey) {
    clearTimeout(layerTimers[kindKey]);
    layerTimers[kindKey] = setTimeout(function () {
      var l = {}, e = {};
      l[kindKey] = false; e[kindKey] = true;
      if (App.state.layers.loading[kindKey]) App.set({ layers: { loading: l, error: e } });
    }, LAYER_TIMEOUT_MS);
  }
  /** retryLayer — a real second attempt: ask the map to refetch, and cycle the
   *  bucket across two frames so a module that only watches state also reloads. */
  function retryLayer(kindKey) {
    if (!kindKey) return;
    var ld = {}, er = {}, off = {}, on = {};
    ld[kindKey] = true; er[kindKey] = false; off[kindKey] = false; on[kindKey] = true;
    App.emit('layers:retry', { kind: kindKey });
    act.setLayers({ loading: ld, error: er });
    armLayerWait(kindKey);
    // The map handles layers:retry by refetching without touching the buckets.
    // Only when it cannot (no such method) do we fall back to cycling the
    // toggle — which writes the storage key twice and is what we want to avoid.
    if (window.MapMod && typeof window.MapMod.retryRail === 'function') return;
    act.setLayers(off);
    setTimeout(function () { act.setLayers(on); }, 0);
  }
  function toggleLayer(kindKey) {
    var s = S(), L = s.layers, on = !L[kindKey];
    var patch = {}; patch[kindKey] = on;
    if (kindKey === 'landmarks' && !on) patch.hiddenLandmarks = false;
    if (kindKey === 'long' || kindKey === 'city') {
      var loading = {}, error = {};
      loading[kindKey] = on; error[kindKey] = false;
      patch.loading = loading; patch.error = error;
      if (on) armLayerWait(kindKey); else clearTimeout(layerTimers[kindKey]);
    }
    act.setLayers(patch);
  }

  /* Content panels at narrow present as a bottom sheet (LAYER-01, 2026-09-12);
     anchored menus stay anchored. */
  var BOTTOM_SHEET_KINDS = ['account', 'regionPicker', 'help', 'memberPicker'];
  function isBottomSheetKind(s, k) {
    return isNarrow(s) && BOTTOM_SHEET_KINDS.indexOf(k) >= 0;
  }

  /** LAYER-02 placement, LAYER-03 fallback. */
  function placePopover(s) {
    var node = el('ov-pop');
    if (!node) return;
    var k = s.overlay.kind, U = s.layout.U, M = 16;
    var bottomSheet = isBottomSheetKind(s, k);
    node.classList.toggle('ov-pop--bottom', bottomSheet);
    node.classList.toggle('ov-pop--full', k === 'layers' && layersFallback.full);
    if (bottomSheet) {
      node.style.cssText = '';
      node.style.maxHeight = Math.round(U.h * 0.86) + 'px';
      return;
    }
    if (k === 'layers' && layersFallback.full) { node.style.cssText = ''; return; }

    var wPref = k === 'layers' ? 360 : (k === 'account' ? 360 : (k === 'regionPicker' ? 420 : (k === 'memberPicker' || k === 'share' || k === 'help' ? 340 : 248)));
    var w = Math.min(wPref, U.w - 2 * M);
    node.style.width = w + 'px';
    node.style.maxHeight = 'none';

    var a = O.anchorRect(k);
    if (!a) {
      node.style.left = Math.round(U.x + (U.w - w) / 2) + 'px';
      node.style.top = Math.round(U.y + Math.max(M, (U.h - node.offsetHeight) / 2)) + 'px';
      node.style.maxHeight = (U.h - 2 * M) + 'px';
      return;
    }
    var h = node.offsetHeight;
    var above = k === 'layers';                 // the FAB group opens upward first
    var spaceAbove = a.top - 8 - (U.y + M);
    var spaceBelow = (U.y + U.h - M) - (a.bottom + 8);
    var side;
    if (above) side = (h <= spaceAbove || spaceAbove >= spaceBelow) ? 'above' : 'below';
    else side = (h <= spaceBelow || spaceBelow >= spaceAbove) ? 'below' : 'above';
    var space = side === 'above' ? spaceAbove : spaceBelow;

    if (k === 'layers' && space < 100) { layersFallbackStep(s); return; }

    var used = Math.min(h, Math.max(100, space));
    node.style.maxHeight = used + 'px';
    var left = a.right - w;
    if (a.left < U.x + U.w / 2) left = a.left;
    left = clamp(left, U.x + M, U.x + U.w - M - w);
    var top = side === 'above' ? (a.top - 8 - used) : (a.bottom + 8);
    top = clamp(top, U.y + M, U.y + U.h - M - Math.min(used, h));
    node.style.left = Math.round(left) + 'px';
    node.style.top = Math.round(top) + 'px';
    node.style.transformOrigin = (side === 'above' ? 'right bottom' : 'right top');
  }

  /** LAYER-03: collapse the browse panel first, then become a full-U layer task. */
  function layersFallbackStep(s) {
    if (isNarrow(s) && s.sheet.state !== 'collapsed' && layersFallback.prevSheet === null) {
      layersFallback.prevSheet = s.sheet.state;
      if (window.Containers && Containers.temporarilyCollapse) Containers.temporarilyCollapse();
      else App.set({ sheet: { state: 'collapsed' } });
      sig.pop = null;
      return;
    }
    layersFallback.full = true;
    sig.pop = null;
    App.requestRender('overlay');
  }
  function restoreLayersFallback() {
    var prev = layersFallback.prevSheet;
    layersFallback = { prevSheet: null, full: false };
    if (!prev) return;
    if (window.Containers && Containers.restore) { if (Containers.restore()) return; }
    App.set({ sheet: { state: prev } });
  }

  O.anchorRect = function (kind) {
    var s = S(), U = s.layout.U;
    if (kind === 'layers') {
      var r = R.fab && R.fab.getBoundingClientRect();
      if (r && r.width) return r;
      return { left: U.x + U.w - 64, right: U.x + U.w - 16, top: U.y + U.h - 64, bottom: U.y + U.h - 16, width: 48, height: 48 };
    }
    if (kind === 'placeMenu') {
      var p = s.overlay.payload || {};
      var x = p.x || Math.round(U.x + U.w / 2), y = p.y || Math.round(U.y + U.h / 2);
      return { left: x, right: x, top: y, bottom: y, width: 0, height: 0 };
    }
    if (lastTrigger && document.contains(lastTrigger)) {
      var q = lastTrigger.getBoundingClientRect();
      if (q.width) return q;
    }
    var own = document.querySelector('[data-ov="open"][data-kind="' + kind + '"]');
    if (own && own.offsetParent !== null) {
      var o = own.getBoundingClientRect();
      if (o.width) return o;
    }
    return null;
  };

  /* ---- account & data (A08 / §13.1 / FEEDBACK-02) ---------------------- */
  function row(action, icon, label, opts) {
    opts = opts || {};
    return '<button class="menu-row' + (opts.danger ? ' is-danger' : '') + '" data-ov="' + action + '"' +
      (opts.data || '') + (opts.aria ? ' aria-label="' + esc(opts.aria) + '"' : '') + '>' +
      '<span class="ov-row-main">' + ic(icon) + '<span>' + esc(label) +
      (opts.sub ? '<span class="menu-sub">' + esc(opts.sub) + '</span>' : '') + '</span></span>' +
      (opts.chevron === false ? '' : ic('chevronRight', { cls: 'ic-sm' })) + '</button>';
  }

  function syncDotClass(s) {
    if (s.sync.kind === 'err') return ' is-err';
    if (s.sync.kind === 'busy') return ' is-busy';
    if (!s.account.signedIn) return ' is-local';
    return '';
  }

  function accountBody(s) {
    var lists = Data.lists().length;
    var pins = Data.pins(s.user.bookmarks).length;
    var stateLine = t('已收藏 {f} · 弃用 {b} · 书签 {p} · 收藏夹 {l}', {
      f: u.fmtCount(s.user.fav.size, s.lang), b: u.fmtCount(s.user.black.size, s.lang),
      p: u.fmtCount(pins, s.lang), l: u.fmtCount(lists, s.lang)
    }) + ' · ' + (s.account.signedIn ? t('已同步到你的 Google 账号') : t('未登录，仅保存在本设备'));
    var initial = (s.account.email || s.account.name || '').trim().charAt(0).toUpperCase();
    var syncLine = '<span class="ov-sync' + syncDotClass(s) + '"><span class="ov-sync-dot"></span>' +
      esc(s.sync.text || (s.account.signedIn ? t('已同步到你的 Google 账号') : t('未登录，仅保存在本设备'))) + '</span>' +
      (s.sync.retryVisible ? '<button class="btn btn-quiet" data-ov="retry-sync">' + esc(t('立即重试')) + '</button>' : '');
    var identity = s.account.signedIn
      ? '<div class="ov-acc-user">' +
          '<span class="ov-avatar is-in" aria-hidden="true">' + esc(initial || '·') + '</span>' +
          '<span class="ov-acc-mail"><b lang="en">' + esc(s.account.email || s.account.name) + '</b>' + syncLine + '</span>' +
          '<button class="btn btn-secondary" data-ov="signout" id="ov-signout">' + esc(t('退出')) + '</button></div>' +
          (signOutArmed
            ? '<div class="ov-signout-confirm" role="group" aria-label="' + esc(t('确认退出')) + '">' +
                '<p class="ov-danger-hint">' + esc(t('退出后这台设备上的收藏仍然保留，除非你选择一并清除。')) + '</p>' +
                '<div class="ov-signout-btns">' +
                  '<button class="btn btn-secondary" data-ov="signout-keep">' + esc(t('退出，保留本设备数据')) + '</button>' +
                  '<button class="btn btn-danger" data-ov="signout-clear">' + esc(t('退出并清除本设备数据')) + '</button>' +
                  '<button class="btn btn-quiet" data-ov="signout-cancel">' + esc(t('取消')) + '</button>' +
                '</div></div>'
            : '')
      : '<div class="ov-gsi" id="ov-gsi" aria-label="' + esc(t('使用 Google 账号登录')) + '"></div>' +
        '<ul class="ov-bullets"><li>' + esc(t('跨设备同步收藏、弃用、书签和子收藏夹')) + '</li>' +
        '<li>' + esc(t('数据托管于 Cloudflare，可导出到本地')) + '</li></ul>' +
        '<p class="ov-acc-note">' + esc(t('不登录也完全可用')) + '</p>' +
        '<p class="ov-state-line">' + syncLine + '</p>';
    var msg = s.account.message
      ? '<p class="ov-acc-msg"' + (s.account.messageColor ? ' style="color:' + esc(s.account.messageColor) + '"' : '') + ' role="status">' + esc(s.account.message) + '</p>' : '';
    var inst = installState(s);
    var installRow = (inst.standalone || inst.installed) ? '' :
      row('help', 'download', t('安装到主屏幕'), { data: ' data-topic="install"' });
    // Android shell only. Business publishes the record (and the label, in the system
    // language, out of the app's own string resources) when Native.openSettings exists;
    // in a browser state.nativeSettings stays null and this is nothing. It is the only
    // way into the shell's outbound-link policy, notification switch and — since 3.2.3
    // took page zoom away — its text-size setting, which is the accessibility way back.
    // The label is NOT run through t(): it is already localized by Android.
    var nativeRow = (s.nativeSettings && s.nativeSettings.label)
      ? row('native-settings', 'gear', s.nativeSettings.label) : '';

    return popHead(t('账户与数据')) +
      '<div class="ov-pop-body is-flush"><div class="ov-acc">' +
        '<div class="ov-acc-sec">' + identity + msg + '</div>' +
        '<div class="ov-acc-sec"><div class="ov-acc-h">' + esc(t('语言')) + '</div><div class="ov-langseg" role="group" aria-label="' + esc(t('语言')) + '">' +
          LANGS.map(function (l) {
            return '<button data-ov="lang" data-lang="' + l.key + '" aria-pressed="' + (s.lang === l.key) + '">' + esc(l.label) + '</button>';
          }).join('') + '</div></div>' +
        '<div class="ov-acc-sec"><div class="ov-acc-h">' + esc(t('偏好')) + '</div><div class="ov-card">' +
          row('reset-filters', 'reset', t('重置筛选'), { chevron: false }) + '</div></div>' +
        '<div class="ov-acc-sec"><div class="ov-acc-h">' + esc(t('备份与迁移')) + '</div><div class="ov-card">' +
          row('export', 'download', t('导出 favorites.json')) +
          row('import', 'upload', t('导入 favorites.json')) + '</div></div>' +
        '<div class="ov-acc-sec"><div class="ov-acc-h">' + esc(t('帮助与应用')) + '</div><div class="ov-card">' +
          row('help', 'legend', t('地图图例'), { data: ' data-topic="legend"' }) +
          row('help', 'info', t('关于本站'), { data: ' data-topic="about"' }) +
          row('help', 'keyboard', t('快捷键'), { data: ' data-topic="shortcuts"' }) +
          installRow + nativeRow + '</div></div>' +
        '<div class="ov-acc-sec"><div class="ov-acc-h">' + esc(t('隐私与数据')) + '</div><div class="ov-card">' +
          row('privacy', 'external', t('隐私政策')) +
          (s.account.signedIn
            ? row('delete-cloud', 'trash', deleteArmedAt ? t('再次点击以确认删除') : t('删除云端数据'), { danger: true, chevron: false })
            : '') +
        '</div>' +
        (deleteArmedAt ? '<p class="ov-danger-hint">' + esc(t('这会删除云端副本和本机的登录镜像，6 秒内再次点击才会执行。')) + '</p>' : '') +
        (deleteMsg ? '<p class="ov-danger-hint" role="status"' + (deleteMsg.err ? ' style="color:var(--danger)"' : '') + '>' + esc(deleteMsg.text) + '</p>' : '') +
        '</div>' +
        '<div class="ov-acc-sec"><div class="ov-acc-h">' + esc(t('本地状态')) + '</div>' +
          '<p class="ov-state-line">' + esc(stateLine) + '</p>' +
          (s.sync.storageInfo ? '<p class="ov-state-line">' + esc(s.sync.storageInfo) + '</p>' : '') +
          '<button class="btn btn-quiet" data-ov="refresh-storage">' + ic('refresh', { cls: 'ic-sm' }) + esc(t('刷新本地状态')) + '</button>' +
        '</div>' +
      '</div></div>';
  }

  /** the real GIS button (renderButton, never One Tap); 3 s of retries inside Business. */
  function mountSignIn(s, node) {
    if (s.account.signedIn) return;
    var host = node.querySelector('#ov-gsi');
    if (!host) return;
    motion.afterGeometry(function () {
      try { act.signIn(host); } catch (e) { console.warn('[overlays] sign-in button', e); }
    });
  }

  /* ---- region picker ---------------------------------------------------- */
  function regionBody(s) {
    var c = counts(s);
    var q = (listFilter || '').toLowerCase();
    var groups = Data.config.REGION_GROUPS.map(function (g) {
      var rows = '';
      for (var i = g.from; i <= g.to; i++) {
        var name = Data.regionName(i, s.lang);
        if (q && (name + ' ' + Data.config.REGIONS[i].en).toLowerCase().indexOf(q) < 0) continue;
        var n = c.region[i] || 0;
        rows += '<button class="ov-rg-row" role="radio" aria-checked="' + (s.filters.region === i) + '" data-ov="region" data-code="' + i + '">' +
          '<span class="ov-rg-name">' + esc(name) + '</span><span class="ov-rg-n num">' + esc(u.fmtCount(n, s.lang)) + '</span></button>';
      }
      return rows ? '<div class="ov-rg-head">' + esc(Data.regionGroupName(g)) + '</div>' + rows : '';
    }).join('');
    // counts.region is computed with the region condition cleared, so its sum
    // is what "all regions" would actually show — c.total is the *current*
    // region's count and would be wrong the moment one is picked.
    var allN = 0;
    Object.keys(c.region).forEach(function (k) { allN += c.region[k] || 0; });
    var all = (!q || t('全部地区').toLowerCase().indexOf(q) >= 0)
      ? '<button class="ov-rg-row" role="radio" aria-checked="' + (s.filters.region === null) + '" data-ov="region" data-code="">' +
        '<span class="ov-rg-name">' + esc(t('全部地区')) + '</span><span class="ov-rg-n num">' + esc(u.fmtCount(allN, s.lang)) + '</span></button>' : '';
    return popHead(t('地区')) +
      '<div class="ov-search-inline"><input class="input" id="ov-region-q" type="search" autocomplete="off" ' +
        'placeholder="' + esc(t('搜索地区')) + '" aria-label="' + esc(t('搜索地区')) + '" value="' + esc(listFilter) + '"></div>' +
      '<div class="ov-pop-body is-flush" role="radiogroup" aria-label="' + esc(t('地区')) + '">' + all + groups +
      (!all && !groups ? '<div class="ov-sug-empty t-body">' + esc(t('没有找到相关结果')) + '</div>' : '') + '</div>';
  }

  /* ---- sort menu -------------------------------------------------------- */
  function sortBody(s) {
    var items = null;
    if (window.ListMod && typeof ListMod.sortItems === 'function') { try { items = ListMod.sortItems(); } catch (e) { items = null; } }
    if (!items) items = Data.config.SORTS.map(function (o) { return { key: o.key, label: o.label, current: o.key === s.sort }; });
    return '<div class="ov-pop-body is-flush ov-menu" role="radiogroup" aria-label="' + esc(t('排序')) + '">' +
      items.map(function (so) {
        return '<button class="menu-row" role="radio" aria-checked="' + (!!so.current) + '" data-ov="sort" data-key="' + so.key + '">' +
          (so.current ? ic('check') : '<span class="ic" aria-hidden="true"></span>') + esc(t(so.label)) + '</button>';
      }).join('') + '</div>';
  }

  /* ---- restaurant "more" (content from Detail / List) -------------------- */
  function moreItemsFor(s) {
    var pl = s.overlay.payload || {};
    if (pl.context === 'savedList' && window.ListMod && typeof ListMod.listMenuItems === 'function') {
      try { return ListMod.listMenuItems(s, pl.listId) || []; } catch (e) { return []; }
    }
    if (pl.context === 'saved' && window.ListMod && typeof ListMod.rowMenuItems === 'function') {
      try { return ListMod.rowMenuItems(s, pl.id) || []; } catch (e) { return []; }
    }
    if (window.Detail && typeof Detail.moreItems === 'function') {
      try { return Detail.moreItems(s) || []; } catch (e) { return []; }
    }
    return [];
  }
  function moreBody(s) {
    var items = moreItemsFor(s), pl = s.overlay.payload || {};
    var r = Data.byId(pl.id);
    var listTitle = null;
    if (pl.context === 'savedList') {
      var l = (Data.lists() || []).filter(function (x) { return x.id === pl.listId; })[0];
      listTitle = l ? ((l.emoji ? l.emoji + ' ' : '') + l.name) : t('收藏夹');
    }
    var firstDanger = -1;
    items.forEach(function (it, i) { if (firstDanger < 0 && it.danger) firstDanger = i; });
    var body = items.map(function (it, i) {
      return ((it.sep || it.separatorBefore || (i === firstDanger && i > 0)) ? '<div class="menu-sep"></div>' : '') +
        '<button class="menu-row' + (it.danger ? ' is-danger' : '') + '" data-ov="more-run" data-i="' + i + '">' +
        ic(it.icon || 'info') + esc(t(it.label)) + '</button>';
    }).join('');
    if (!body) body = '<div class="ov-sug-empty t-secondary">' + esc(t('暂无可用操作')) + '</div>';
    return popHead(listTitle || (r ? r.name : t('更多操作')), { ja: !listTitle && !!r }) +
      '<div class="ov-pop-body is-flush ov-menu">' + body + '</div>';
  }

  /* ---- share ------------------------------------------------------------ */
  function shareBody(s) {
    var id = (s.overlay.payload || {}).id;
    var link = (id && act.shareUrl(id)) || location.href;
    var canNative = !!(navigator.share && id);
    return popHead(t('分享')) +
      '<div class="ov-pop-body"><div class="field"><label class="field-label" for="ov-share-in">' + esc(t('链接')) + '</label>' +
      '<input class="input" id="ov-share-in" readonly value="' + esc(link) + '" lang="en"></div>' +
      '<p class="ov-sub">' + esc(t('复制失败时可以手动选中上面的链接。')) + '</p>' +
      '<button class="btn btn-primary" data-ov="share-copy" style="width:100%;margin-top:8px">' + ic('copy') + esc(t('复制链接')) + '</button>' +
      (canNative ? '<button class="btn btn-secondary" data-ov="share-native" style="width:100%;margin-top:8px">' + ic('share') + esc(t('系统分享')) + '</button>' : '') +
      '</div>';
  }

  /* ---- member picker (DATA-03) ------------------------------------------ */
  function memberBody(s) {
    var id = (s.overlay.payload || {}).id;
    var saved = s.user.fav.has(id);
    var lists = Data.lists();
    var mine = new Set(Data.listsOf(s.user.bookmarks, id));
    var inDefault = saved && mine.size === 0;
    var defaultCount = 0;
    s.user.fav.forEach(function (ref) { if (Data.listsOf(s.user.bookmarks, ref).length === 0) defaultCount += 1; });

    var mrow = function (key, label, emo, count, on, isDefault, ja) {
      return '<button class="ov-mp-row" role="checkbox" aria-checked="' + on + '" data-ov="' +
        (isDefault ? 'member-default' : 'member-toggle') + '"' + (isDefault ? '' : ' data-list="' + esc(key) + '"') + '>' +
        '<input type="checkbox" class="checkbox" tabindex="-1" aria-hidden="true"' + (on ? ' checked' : '') + '>' +
        (emo ? emoji.img(emo, 20) : '') +
        '<span class="ov-mp-name"' + (ja ? ' lang="ja"' : '') + '>' + esc(label) + '</span>' +
        '<span class="ov-mp-n num">' + esc(u.fmtCount(count, s.lang)) + '</span></button>';
    };

    var rows = mrow('list:default', t('默认收藏夹'), '⭐', defaultCount, inDefault, true, false) +
      lists.map(function (l) {
        var n = Data.members(s.user.bookmarks, l.id).length;
        return mrow(l.id, listName(l), l.emoji, n, mine.has(l.id), false, false);
      }).join('');

    return popHead(t(saved ? '管理收藏' : '加入收藏')) +
      '<div class="ov-pop-body is-flush">' + rows + '</div>' +
      '<div class="ov-pop-foot"><button class="ov-inline-add" data-ov="new-list" data-from="memberPicker">' +
        ic('folderPlus', { cls: 'ic-sm' }) + esc(t('新建收藏夹')) + '</button>' +
        (saved ? '<button class="btn btn-quiet ov-mp-remove" data-ov="member-unsave">' + esc(t('取消收藏')) + '</button>' : '') +
      '</div>';
  }

  /** DATA-03: the default collection is "saved, in no user collection". */
  function toggleDefault(ref) {
    if (!ref) return;
    var s = S();
    var saved = s.user.fav.has(ref);
    var lists = Data.listsOf(s.user.bookmarks, ref);
    if (saved && !lists.length) { unsave(ref); return; }
    if (!saved) { act.toggleFav(ref); }
    else { lists.forEach(function (lid) { act.removeFromList(lid, ref); }); }
    sig.pop = null;
  }

  /** unsave(ref) — drop from Saved and from every collection at once. */
  function unsave(ref) {
    var s = S();
    Data.listsOf(s.user.bookmarks, ref).forEach(function (lid) { act.removeFromList(lid, ref); });
    if (S().user.fav.has(ref)) act.toggleFav(ref);
    sig.pop = null;
  }

  function toggleMember(listId, ref) {
    if (!listId || !ref) return;
    var s = S();
    var has = Data.listsOf(s.user.bookmarks, ref).indexOf(listId) >= 0;
    if (has) act.removeFromList(listId, ref);
    else {
      if (!s.user.fav.has(ref)) act.toggleFav(ref);   // DATA-03: joining a collection saves it
      act.addToList(listId, ref);
    }
    sig.pop = null;
  }

  /* ---- place menu (map contextmenu / picked place) ---------------------- */
  function placeMenuBody(s) {
    var p = s.overlay.payload || {};
    var coord = (Number(p.lat).toFixed(6)) + ', ' + (Number(p.lon).toFixed(6));
    var title = p.place && p.place.name ? p.place.name : t('这个地点');
    // MAP coreRequest #2: a tap on a landmark or a user pin carries
    // {kind, bm, id} as well as the coordinates, so the menu is about THAT
    // entry — 隐藏景点 for a built-in, 编辑 / 删除 for a pin — instead of the
    // generic "make something here" menu.
    var rows = '';
    if (p.kind === 'landmark' && p.bm) {
      var hiddenNow = Data.hiddenLandmarkIds().has(p.bm.id);
      rows = hiddenNow
        ? '<button class="menu-row" data-ov="lm-unhide">' + ic('landmark') + esc(t('恢复显示这个景点')) + '</button>'
        : '<button class="menu-row is-danger" data-ov="lm-hide">' + ic('hide') + esc(t('隐藏景点')) + '</button>';
    } else if (p.kind === 'pin' && p.bm) {
      rows = '<button class="menu-row" data-ov="pin-edit">' + ic('pin') + esc(t('编辑书签')) + '</button>' +
             '<button class="menu-row is-danger" data-ov="pin-delete">' + ic('trash') + esc(t('删除书签')) + '</button>';
    } else {
      rows = '<button class="menu-row" data-ov="place-new" data-cat="bookmark">' + ic('bookmark') + esc(t('新建书签')) + '</button>' +
             '<button class="menu-row" data-ov="place-new" data-cat="attraction">' + ic('landmark') + esc(t('新建景点')) + '</button>';
    }
    return popHead(title, { plain: true, ja: !!(p.place && p.place.name) }) +
      '<div class="ov-place-head"><span class="ov-sub num" lang="en">' + esc(coord) + '</span></div>' +
      '<div class="ov-pop-body is-flush ov-menu">' + rows +
        '<button class="menu-row" data-ov="place-copy">' + ic('copy') + esc(t('复制坐标')) + '</button>' +
        '<button class="menu-row" data-ov="place-gmaps">' + ic('external') + esc(t('在 Google Maps 打开')) + '</button>' +
      '</div>';
  }

  /* ---- help / legend / about / shortcuts / install ---------------------- */
  var HELP_TITLE = { legend: '地图图例', about: '关于本站', shortcuts: '快捷键', install: '安装到主屏幕', rating: '评分说明' };
  function helpTopic(s) {
    var p = s.overlay.payload || {};
    if (p.section === 'install') return 'install';
    return p.topic || 'about';
  }
  function installHelpBody(s) {
    var p = s.overlay.payload || {};
    var info = p.install || null;
    var inst = installState(s);
    if (inst.standalone || inst.installed) {
      return '<p class="t-long-body">' + esc(t('已经安装到主屏幕')) + '</p>';
    }
    var ios = info ? info.ios : !!inst.ios;
    var desktop = info ? info.desktop : false;
    var steps;
    if (ios) {
      steps = '<ol class="ov-steps"><li>' + esc(t('点底部的分享按钮')) + '</li><li>' +
        esc(t('选择添加到主屏幕')) + '</li><li>' + esc(t('再点添加')) + '</li></ol>';
    } else if (desktop) {
      steps = '<ol class="ov-steps"><li>' + esc(t('点地址栏右侧的安装图标')) + '</li><li>' +
        esc(t('或打开浏览器菜单选择安装')) + '</li></ol>';
    } else {
      steps = '<p class="t-long-body">' + esc(t('在浏览器菜单里找添加到主屏幕或安装')) + '</p>';
    }
    return (inst.canPrompt ? '<button class="btn btn-primary" data-ov="install-now" style="width:100%;margin-bottom:8px">' +
        ic('download') + esc(t('安装')) + '</button>' : '') +
      steps + '<p class="ov-sub">' + esc(t('装好之后离线也能看地图')) + '</p>';
  }
  function layoutDiagnosticText(s) {
    var doc = document.documentElement, sc = window.screen || {}, vv = window.visualViewport;
    var info = App.layout.foldCoverInfo(doc.clientWidth || window.innerWidth);
    function finite(v) { return typeof v === 'number' && Number.isFinite(v) ? v : null; }
    return JSON.stringify({
      appVersion: (s.buildMeta || {}).appVersion || '',
      screen: { width: info.screenWidth, height: info.screenHeight },
      availableScreen: { width: finite(sc.availWidth), height: finite(sc.availHeight) },
      layoutViewport: { width: info.layoutWidth, height: finite(doc.clientHeight) },
      innerViewport: { width: finite(window.innerWidth), height: finite(window.innerHeight) },
      visualViewport: vv ? { width: finite(vv.width), height: finite(vv.height) } : null,
      devicePixelRatio: info.dpr,
      physicalScreen: { width: info.physicalWidth, height: info.physicalHeight },
      touch: { detected: info.touch, maxTouchPoints: info.maxTouchPoints, coarsePointer: info.coarsePointer },
      foldCover: { matched: info.matched, enabled: !!s.layout.foldCover,
        attributeApplied: doc.hasAttribute('data-fold-cover'), reason: t(info.reason) }
    }, null, 2);
  }
  function helpBody(s) {
    var topic = helpTopic(s);
    var body = '';
    if (topic === 'legend') {
      body =
        '<div class="ov-help-h">' + esc(t('晚餐价格')) + '</div>' +
        Data.config.PRICE_BUCKETS.map(function (b) {
          return '<div class="ov-legend-row"><span class="price-dot price-' + b.key + '"></span>' +
            '<span class="ov-legend-label">' + esc(b.key === 'na' ? t('价格未知') : b.label) + '</span>' +
            '<span class="price-tag price-' + b.key + '">' + esc(b.key === 'na' ? t('未知') : b.short) + '</span></div>';
        }).join('') +
        '<p class="ov-sub">' + esc(t('按晚餐价位上限归档；没有晚餐价位时用午餐，两者都没有则归入价格未知。')) + '</p>' +
        '<div class="ov-help-h">' + esc(t('奖项')) + '</div><div class="badge-row">' +
        Data.config.AWARD_TAGS.map(function (a) {
          return '<span class="badge badge-' + a.slug + '">' + emoji.img(a.emoji, 14) + esc(Data.awardShort(a.slug)) + '</span>';
        }).join('') + '</div>' +
        '<div class="ov-help-h">' + esc(t('料理 emoji')) + '</div><div class="ov-legend-grid">' +
        Data.config.ALL_CUISINES.map(function (c) {
          return '<span class="ov-legend-emoji">' + emoji.img(Data.config.GENRE_EMOJI[c], 18) + esc(t(c)) + '</span>';
        }).join('') + '</div>' +
        '<div class="ov-help-h">' + esc(t('地图标记')) + '</div>' +
        '<p class="ov-sub">' + esc(t('蓝色圆环是当前选中的餐厅；蓝色圆点是聚合，数字为其中的餐厅数；景点用自身 emoji，书签用你选择的 emoji。')) + '</p>';
    } else if (topic === 'shortcuts') {
      body = [['/', t('聚焦搜索')], ['J / K', t('在结果中上下移动')], ['F', t('收藏当前餐厅')], ['X', t('弃用当前餐厅')],
        ['Esc', t('返回上一层')], ['?', t('打开本页帮助')]].map(function (k) {
          return '<div class="ov-kbd-row"><span class="ov-kbd">' + esc(k[0]) + '</span><span class="ov-kbd-desc">' + esc(k[1]) + '</span></div>';
        }).join('');
    } else if (topic === 'install') {
      body = installHelpBody(s);
    } else if (topic === 'rating') {
      body = '<p class="t-long-body">' + esc(t('评分来自 Tabelog 的公开分数，区间 3.4 – 4.5；没有分数的店不会因为评分条件被排除。')) + '</p>';
    } else {
      var bm = s.buildMeta || {};
      body = '<p class="t-long-body">' + esc(t('Japan Foodmap 是一份个人的日本美食与旅行地图。')) + '</p>' +
        '<div class="ov-kbd-row"><span class="ov-kbd-desc">' + esc(t('版本')) + '</span><b class="num" lang="en">' + esc(bm.appVersion || '') + '</b></div>' +
        '<div class="ov-kbd-row"><span class="ov-kbd-desc">' + esc(t('收录范围')) + '</span><b class="num">' + esc(t('{n} 家', { n: u.fmtCount(Data.restaurants.length, s.lang) })) + '</b></div>' +
        (bm.scrapedAt ? '<div class="ov-kbd-row"><span class="ov-kbd-desc">' + esc(t('数据采集于')) + '</span><b class="num" lang="en">' + esc(bm.scrapedAt) + '</b></div>' : '') +
        (bm.latestScrape && bm.latestScrape !== bm.scrapedAt ? '<div class="ov-kbd-row"><span class="ov-kbd-desc">' + esc(t('最近补充')) + '</span><b class="num" lang="en">' + esc(bm.latestScrape) + '</b></div>' : '') +
        '<p class="ov-sub">' + esc(t('数据来自 Tabelog 公开页面，仅作个人旅行参考。')) + '</p>' +
        '<details class="ov-layout-diagnostics"><summary data-ov="layout-diagnostics">' + esc(t('布局诊断')) + '</summary>' +
        '<p class="ov-sub">' + esc(t('仅在本地显示和复制屏幕信息，不含账号或收藏数据。')) + '</p>' +
        '<pre class="ov-layout-readings"></pre><button class="btn btn-secondary" data-ov="copy-layout-diagnostics">' +
        esc(t('复制诊断信息')) + '</button></details>';
    }
    return popHead(t(HELP_TITLE[topic] || '帮助')) + '<div class="ov-pop-body"><div class="ov-help-body">' + body + '</div></div>';
  }

  /* ----------------------------------------------------------------------
     4. Modals (#modal-root) — FORM-01 / FORM-02 / DATA-02 / lightbox
     -------------------------------------------------------------------- */
  function initDraft(kind, payload) {
    payload = payload || {};
    var key = kind + ':' + (payload.mode || 'create') + ':' + (payload.lat || '') + ':' + (payload.lon || '') + ':' + (payload.listId || payload.id || '');
    if (draftKey === key && draft) return;
    draftKey = key; draftErr = {}; saving = false;
    var d = payload.draft || {};
    if (kind === 'bookmarkForm') {
      draft = { name: d.name || '', emoji: d.emoji || Data.config.BOOKMARK_QUICK_EMOJI[0],
        category: d.category || 'bookmark', lat: payload.lat, lon: payload.lon,
        lists: (payload.lists || []).slice(), id: payload.id || null };
    } else {
      draft = { name: d.name || '', emoji: d.emoji || Data.config.LIST_QUICK_EMOJI[0],
        members: (payload.members || []).slice(),
        onSaved: payload.onSaved || null,
        id: payload.mode === 'edit' ? (payload.listId || payload.id || null) : null };
    }
  }

  function renderModal(s) {
    var k = s.overlay.kind;
    var isModal = k && nav.MODAL_KINDS.indexOf(k) >= 0;
    if (k === 'bookmarkForm' || k === 'listForm') initDraft(k, s.overlay.payload);
    var gate = !isModal && langGateActive(s);
    var key = isModal ? [k, s.lang, s.layout.mode, draftKey, transient && transient.kind,
                         importPreview ? importPreview.token : '', (s.overlay.payload || {}).index].join('|')
                      : (gate ? 'langgate|' + s.layout.mode
                              : (MODAL_POPS.indexOf(k) >= 0 ? 'popmodal|' + k : 'none'));
    if (sig.modal === key) return;
    sig.modal = key;
    R.modal.innerHTML = '';
    // MODAL_POPS (账户与数据) is not in MODAL_KINDS, so it renders through the
    // popover path; its containment rides along here so a modal opening on top
    // still wins (keepPop is false whenever a real modal or the gate is up).
    var popModal = !isModal && !gate && MODAL_POPS.indexOf(k) >= 0;
    setInert(!!isModal || gate || popModal, popModal);
    if (gate) { renderLangGate(s); return; }
    if (!isModal) return;
    if (k === 'lightbox') { renderLightbox(s); return; }
    var narrow = isNarrow(s);
    var node = document.createElement('div');
    node.className = 'ov-modal ' + (narrow ? 'ov-modal--sheet slide-up-in' : 'ov-modal--dialog scale-in');
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-modal', 'true');
    node.setAttribute('aria-labelledby', 'ov-modal-title');
    node.innerHTML = modalBody(s, k, narrow);
    R.modal.innerHTML = '<div class="ov-scrim" data-ov="scrim"></div>';
    R.modal.appendChild(node);
    if (narrow) bindSheetDrag(node);
    trapFocus(node);
    if (transient && transient.kind === 'emojiPicker') mountEmojiPicker(node);
    motion.afterGeometry(function () {
      var input = node.querySelector('.ov-modal-body input:not([readonly]):not([type=file]):not([type=checkbox])');
      if (input) { try { input.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
      else focusIn(node);
    });
  }

  function modalHead(title, narrow) {
    return (narrow ? '<div class="ov-grip handle-hit" aria-hidden="true"><div class="handle"></div></div>' : '') +
      '<div class="ov-modal-head"><h2 id="ov-modal-title" class="t-panel-title">' + esc(title) + '</h2>' +
      '<button class="icon-btn" data-ov="close" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button></div>';
  }

  function modalBody(s, k, narrow) {
    if (k === 'bookmarkForm') return bookmarkForm(s, narrow);
    if (k === 'listForm') return listForm(s, narrow);
    if (k === 'importDialog') return importDialog(s, narrow);
    return modalHead(t(POP_TITLE[k] || k), narrow);
  }

  /* ---- bookmark form (A07 / FORM-02) ------------------------------------ */
  function emojiChips(list, current, moreBtn) {
    return '<div class="ov-emoji-grid">' +
      list.map(function (e) {
        return '<button type="button" class="ov-emoji-chip" data-ov="emoji-pick" data-emoji="' + esc(e) + '" ' +
          'aria-pressed="' + (e === current) + '" aria-label="' + esc(e) + '">' + emoji.img(e, 26) + '</button>';
      }).join('') +
      (moreBtn ? '<button type="button" class="ov-emoji-more" data-ov="emoji-more">' + ic('plus', { cls: 'ic-sm' }) + esc(t('更多表情')) + '</button>' : '') +
      '</div>';
  }

  function bookmarkForm(s, narrow) {
    var d = draft || {};
    var lists = Data.lists();
    var coord = Number(d.lat).toFixed(6) + ', ' + Number(d.lon).toFixed(6);
    var body =
      '<div class="ov-form">' +
        '<div class="ov-coord"><span class="ov-coord-v num" id="ov-coord" lang="en">' + esc(coord) + '</span>' +
          '<button type="button" class="icon-btn" data-ov="copy-coord" aria-label="' + esc(t('复制坐标')) + '">' + ic('copy') + '</button></div>' +
        '<div class="ov-frow"><span class="field-label" id="ov-type-l">' + esc(t('类型')) + '</span>' +
          '<div class="ov-seg2" role="radiogroup" aria-labelledby="ov-type-l">' +
            '<button type="button" role="radio" data-ov="form-type" data-val="bookmark" aria-checked="' + (d.category !== 'attraction') + '">' + ic('star', { fill: d.category !== 'attraction' }) + esc(t('书签')) + '</button>' +
            '<button type="button" role="radio" data-ov="form-type" data-val="attraction" aria-checked="' + (d.category === 'attraction') + '">' + ic('image') + esc(t('景点')) + '</button>' +
          '</div></div>' +
        '<div class="ov-frow"><label class="field-label" for="ov-name">' + esc(t('名称')) + '</label>' +
          '<input class="input" id="ov-name" maxlength="40" autocomplete="off" value="' + esc(d.name || '') + '" placeholder="' + esc(t('给这个地点起个名字')) + '"></div>' +
        '<div class="ov-frow"><label class="field-label" for="ov-emoji">Emoji</label>' +
          '<span class="ov-emoji-field" id="ov-emoji-field">' +
            '<input class="ov-emoji-input" id="ov-emoji" maxlength="8" autocomplete="off" value="' + esc(d.emoji || '') + '" aria-describedby="ov-emoji-err">' +
            '<button type="button" class="icon-btn ov-sclear" data-ov="emoji-clear" aria-label="' + esc(t('清空')) + '">' + ic('x') + '</button></span></div>' +
        '<p class="field-error" id="ov-emoji-err" role="alert" hidden></p>' +
        '<div><div class="field-label" style="margin-bottom:6px">' + esc(t('常用')) + '</div>' +
          emojiChips(Data.config.BOOKMARK_QUICK_EMOJI, d.emoji, true) + '</div>' +
        (lists.length ? '<div><div class="field-label" style="margin-bottom:6px">' + esc(t('加入收藏夹')) + '</div>' +
          '<div class="ov-pick-lists">' + lists.map(function (l) {
            var on = (d.lists || []).indexOf(l.id) >= 0;
            return '<button type="button" class="ov-pick-list" data-ov="form-list" data-list="' + esc(l.id) + '" aria-pressed="' + on + '">' +
              (l.emoji ? emoji.img(l.emoji, 16) : '') + '<span>' + esc(listName(l)) + '</span></button>';
          }).join('') + '</div></div>' : '') +
        '<button type="button" class="ov-inline-add" data-ov="new-list" data-from="bookmarkForm">' +
          ic('folderPlus', { cls: 'ic-sm' }) + esc(t('新建子收藏夹')) + '</button>' +
      '</div>';
    return modalHead(d.category === 'attraction' ? t('新建景点') : t('新建书签'), narrow) +
      '<div class="ov-modal-body">' + body + '</div>' +
      '<div class="ov-modal-foot"><button class="btn btn-secondary" data-ov="close">' + esc(t('取消')) + '</button>' +
        '<button class="btn btn-primary" data-ov="save-bookmark">' + esc(t('保存')) + '</button></div>' +
      (transient && transient.kind === 'emojiPicker' ? emojiPickerHtml() : '');
  }

  /* The full emoji-picker-element module, self-hosted (M-006/M-011), loaded
     on first expand. The six quick chips keep working if it never arrives. */
  function emojiPickerHtml() {
    return '<div class="ov-sublayer" role="dialog" aria-label="' + esc(t('选择表情')) + '">' +
      '<div class="ov-modal-head"><h3 class="t-panel-title">' + esc(t('选择表情')) + '</h3>' +
        '<button class="icon-btn" data-ov="emoji-close" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button></div>' +
      '<div class="ov-modal-body"><div class="ov-picker-host" id="ov-picker-host">' +
        '<emoji-picker id="ov-emoji-picker" data-source="vendor/emoji-picker-element-data-1.8.0/en/emojibase/data.json"></emoji-picker>' +
      '</div>' + emojiChips(Data.config.BOOKMARK_QUICK_EMOJI, draft && draft.emoji, false) + '</div>' +
      '<div class="ov-modal-foot is-single"><button class="btn btn-secondary" data-ov="emoji-close">' + esc(t('返回')) + '</button></div></div>';
  }
  function ensurePickerModule() {
    if (!pickerLoaded) {
      // The leading './' matters: a bare specifier would need an import map.
      pickerLoaded = import('./vendor/emoji-picker-element-1.27.0/index.js')
        .catch(function (e) { pickerLoaded = null; console.warn('[overlays] emoji picker load failed:', e); });
    }
    return pickerLoaded;
  }
  function mountEmojiPicker(node) {
    var picker = node.querySelector('#ov-emoji-picker');
    if (!picker || picker._ovBound) return;
    picker._ovBound = true;
    ensurePickerModule();
    picker.addEventListener('emoji-click', function (ev) {
      var g = ev && ev.detail && ev.detail.unicode;
      if (!g || !draft) return;
      draft.emoji = g;
      var f = el('ov-emoji'); if (f) f.value = g;
      closeTransient();
      validateEmoji(); syncEmojiChips();
    });
  }

  /* ---- collection form --------------------------------------------------- */
  function listForm(s, narrow) {
    var p = s.overlay.payload || {};
    var d = draft || {};
    var body =
      '<div class="ov-form">' +
        '<div class="ov-frow ov-frow--stack"><label class="field-label" for="ov-list-name">' + esc(t('名称')) + '</label>' +
          '<input class="input" id="ov-list-name" maxlength="24" autocomplete="off" value="' + esc(d.name || '') + '" ' +
            'aria-describedby="ov-list-err" aria-invalid="' + (!!draftErr.name) + '" placeholder="' + esc(t('例如：东京美食周末')) + '"></div>' +
        '<p class="field-error" id="ov-list-err" role="alert"' + (draftErr.name ? '' : ' hidden') + '>' + esc(draftErr.name || '') + '</p>' +
        '<div><div class="field-label" style="margin-bottom:6px">' + esc(t('图标')) + '</div>' +
          emojiChips(Data.config.LIST_QUICK_EMOJI, d.emoji, false) + '</div>' +
        (d.members && d.members.length ? '<p class="ov-sub">' + esc(t('将加入 {n} 家餐厅', { n: d.members.length })) + '</p>' : '') +
      '</div>';
    return modalHead(p.mode === 'edit' ? t('重命名收藏夹') : t('新建子收藏夹'), narrow) +
      '<div class="ov-modal-body">' + body + '</div>' +
      '<div class="ov-modal-foot"><button class="btn btn-secondary" data-ov="close">' + esc(t('取消')) + '</button>' +
        '<button class="btn btn-primary" data-ov="save-list">' + esc(t('保存')) + '</button></div>';
  }

  /* ---- import (DATA-02) --------------------------------------------------- */
  function importDialog(s, narrow) {
    var p = importPreview || {};
    var c = p.counts || { contains: 0, add: 0, skip: 0 };
    var picks = p.picks || { fav: true, black: true, bm: true };
    var ok = !!p.norm;
    var pickRow = function (key, label, n) {
      return '<label class="ov-imp-pick"><input type="checkbox" class="checkbox" data-ov="imp-pick" data-key="' + key + '"' +
        (picks[key] ? ' checked' : '') + '><span>' + esc(label) + '</span><span class="num">' + esc(u.fmtCount(n, s.lang)) + '</span></label>';
    };
    return modalHead(t('导入 favorites.json'), narrow) +
      '<div class="ov-modal-body">' +
        '<p class="ov-sub" lang="en">' + esc(p.file || '') + '</p>' +
        (p.error ? '<p class="field-error" role="alert">' + esc(p.error) + '</p>' : '') +
        (ok ? '<div class="ov-import-stats">' +
          '<div class="ov-import-stat"><b class="num">' + esc(u.fmtCount(c.contains, s.lang)) + '</b><span>' + esc(t('文件含有')) + '</span></div>' +
          '<div class="ov-import-stat"><b class="num">' + esc(u.fmtCount(c.add, s.lang)) + '</b><span>' + esc(t('新增')) + '</span></div>' +
          '<div class="ov-import-stat"><b class="num">' + esc(u.fmtCount(c.skip, s.lang)) + '</b><span>' + esc(t('跳过')) + '</span></div>' +
        '</div>' +
        '<div class="ov-imp-picks"><div class="field-label">' + esc(t('要导入哪些内容')) + '</div>' +
          pickRow('fav', t('收藏'), (p.norm.favorites || []).length) +
          pickRow('black', t('弃用'), (p.norm.blacklist || []).length) +
          pickRow('bm', t('书签与收藏夹'), (p.norm.bookmarks || []).length) +
        '</div>' +
        '<p class="ov-sub">' + esc(t('导入按并集合并，同 ID 的条目保留当前版本；不提供导入撤销。')) + '</p>' : '') +
      '</div>' +
      '<div class="ov-modal-foot"><button class="btn btn-secondary" data-ov="close">' + esc(t('取消')) + '</button>' +
        '<button class="btn btn-primary" data-ov="import-confirm"' + (ok ? '' : ' disabled') + '>' + esc(t('确认导入')) + '</button></div>';
  }

  /* ---- lightbox ---------------------------------------------------------- */
  function renderLightbox(s) {
    var p = s.overlay.payload || {};
    var custom = null;
    if (window.Detail && typeof Detail.lightboxContent === 'function') {
      try { custom = Detail.lightboxContent(p); } catch (e) { custom = null; }
    }
    lbCustom = !!(custom && custom.nodeType === 1);
    R.modal.innerHTML = '<div class="ov-lb-back" data-ov="close"></div>';
    if (lbCustom) {
      R.modal.appendChild(custom);
      trapFocus(custom);
      motion.afterGeometry(function () {
        var im = custom.querySelector('img');
        if (im && lightboxFrom) { var from = lightboxFrom; lightboxFrom = null; motion.flip(im, from); }
        try { custom.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
      });
      return;
    }
    R.modal.insertAdjacentHTML('beforeend', wrapLightbox(s, typeof custom === 'string' ? custom : null));
    var shell = R.modal.querySelector('.ov-lb');
    trapFocus(shell);
    var img = el('ov-lb-img');
    if (img && lightboxFrom) {
      var from = lightboxFrom; lightboxFrom = null;
      var go = function () { motion.flip(img, from); };
      if (img.complete) go(); else img.addEventListener('load', go);
    }
    motion.afterGeometry(function () { focusIn(shell); });
    // photos live in the 12-slot popups payload, which is loaded lazily
    if (!custom && !lbPhotos.id) loadLightboxPhotos(p.id);
  }

  // Fallback shell only: Detail owns the real gallery. Photos come from
  // Data.detail(id) (slot 7) and arrive asynchronously.
  var lbPhotos = { id: null, urls: [] };
  function loadLightboxPhotos(id) {
    if (!id || lbPhotos.id === id) return;
    Data.detail(id).then(function (d) {
      lbPhotos = { id: id, urls: (d && d.photos) || [] };
      if (S().overlay.kind === 'lightbox') { sig.modal = null; App.requestRender('overlay'); }
    }).catch(function () {});
  }
  function wrapLightbox(s, inner) {
    var p = s.overlay.payload || {};
    var r = Data.byId(p.id);
    var photos = (lbPhotos.id === p.id ? lbPhotos.urls : []) || [];
    var i = clamp(p.index || 0, 0, Math.max(0, photos.length - 1));
    var src = photos[i] || null;
    var stage = inner || (src
      ? '<img id="ov-lb-img" src="' + esc(src) + '" alt="' + esc(r ? r.name : '') + '">'
      : '<p class="ov-lb-miss">' + esc(t('照片暂不可用')) + '</p>');
    return '<div class="ov-lb" role="dialog" aria-modal="true" aria-label="' + esc(t('照片')) + '">' +
      '<div class="ov-lb-head"><span lang="ja">' + esc(r ? r.name : '') + '</span>' +
        '<button class="icon-btn" data-ov="close" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button></div>' +
      '<div class="ov-lb-stage">' +
        (photos.length > 1 ? '<button class="ov-lb-nav ov-lb-prev" data-ov="lb-step" data-d="-1" aria-label="' + esc(t('上一张')) + '">' + ic('chevronLeft') + '</button>' : '') +
        stage +
        (photos.length > 1 ? '<button class="ov-lb-nav ov-lb-next" data-ov="lb-step" data-d="1" aria-label="' + esc(t('下一张')) + '">' + ic('chevronRight') + '</button>' : '') +
      '</div>' +
      '<div class="ov-lb-cap num">' + esc(photos.length ? (i + 1) + ' / ' + photos.length : '') + '</div></div>';
  }
  function stepLightbox(d) {
    var s = S(), p = s.overlay.payload || {};
    if (lbCustom) {
      if (window.Detail && typeof Detail.lightboxShow === 'function') Detail.lightboxShow((p.index || 0) + d);
      return;
    }
    var photos = (lbPhotos.id === p.id ? lbPhotos.urls : []) || [];
    if (!photos.length) return;
    var n = photos.length;
    App.set({ overlay: { payload: { id: p.id, index: ((p.index || 0) + d + n) % n } } });
    sig.modal = null;
  }
  O.lightboxOpen = function (payload) {
    rememberTrigger();
    lbPhotos = { id: null, urls: [] };
    if (window.Detail && typeof Detail.photoRect === 'function') {
      try { lightboxFrom = Detail.photoRect((payload && payload.index) || 0) || null; } catch (e) { lightboxFrom = null; }
    }
    openOverlay('lightbox', payload || { index: 0 });
  };

  /* ---- focus trap + drag-to-dismiss -------------------------------------- */
  function trapFocus(node) {
    if (!node || node._trapped) return;
    node._trapped = true;
    node.addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var f = node.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])');
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }
  function bindSheetDrag(node) {
    var grip = node.querySelector('.ov-grip');
    if (!grip) return;
    var start = null;
    grip.addEventListener('pointerdown', function (e) {
      start = e.clientY; try { grip.setPointerCapture(e.pointerId); } catch (_) {} node.style.transition = 'none';
    });
    grip.addEventListener('pointermove', function (e) {
      if (start === null) return;
      var dy = Math.max(0, e.clientY - start);
      node.style.transform = 'translateY(' + dy + 'px)';
    });
    function end(e) {
      if (start === null) return;
      var dy = Math.max(0, e.clientY - start); start = null;
      node.style.transition = ''; node.style.transform = '';
      if (dy > 96) act.closeOverlay('drag');
    }
    grip.addEventListener('pointerup', end);
    grip.addEventListener('pointercancel', function () { start = null; node.style.transition = ''; node.style.transform = ''; });
  }

  /* ----------------------------------------------------------------------
     5. Notices (FEEDBACK-02), first run (M-109), install offer (ENV-01)
     -------------------------------------------------------------------- */
  function langGateActive(s) {
    return !langGateDone && !s.notices.langChosen && isNarrow(s) && !s.overlay.kind;
  }
  function renderLangGate() {
    var node = document.createElement('div');
    node.className = 'ov-modal ov-modal--dialog ov-langgate scale-in';
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-modal', 'true');
    node.setAttribute('aria-labelledby', 'ov-lg-t');
    // Deliberately not localized: activeLang is still the default here.
    node.innerHTML =
      '<div class="ov-modal-head"><h2 id="ov-lg-t" class="t-panel-title">' + esc('Language · 语言 · 言語') + '</h2></div>' +
      '<div class="ov-modal-body"><div class="ov-lg-opts">' +
        '<button class="btn btn-secondary" data-ov="gate-lang" data-lang="zh">简体中文</button>' +
        '<button class="btn btn-secondary" data-ov="gate-lang" data-lang="tw" lang="zh-TW">繁體中文</button>' +
        '<button class="btn btn-secondary" data-ov="gate-lang" data-lang="en" lang="en">English</button>' +
        '<button class="btn btn-secondary" data-ov="gate-lang" data-lang="ja" lang="ja">日本語</button>' +
      '</div></div>';
    R.modal.innerHTML = '<div class="ov-scrim"></div>';
    R.modal.appendChild(node);
    trapFocus(node);
    motion.afterGeometry(function () { focusIn(node); });
  }

  function noticeRow(cls, icon, text, actions, role) {
    return '<div class="ov-notice' + (cls ? ' ' + cls : '') + '" role="' + (role || 'status') + '">' + ic(icon) +
      '<span class="ov-notice-text">' + text + '</span>' + (actions || '') + '</div>';
  }

  function renderNotices(s) {
    var inst = installState(s);
    // SEARCH-02 / LAYER-01: the suggestion body owns U while search is active,
    // and an open panel owns the area a floating row would land on. The
    // first-run row, the sign-in hint and the install offer are invitations,
    // not alerts, so they wait. Offline / new version / sync failure are
    // conditions, not invitations, and stay visible.
    var quiet = !!s.search.active || langGateActive(s) || !!s.overlay.kind || !!s.selected.id || s.sheet.state !== 'collapsed';
    var key = [s.notices.offline, s.notices.newVersion, s.notices.introSeen, s.lang, quiet,
      s.sync.kind, s.sync.retryVisible, s.sync.hintDismissed, s.account.signedIn,
      localNotices.geo, localNotices.geoDismissed, localNotices.updateDismissed,
      localNotices.introDismissed, localNotices.syncDismissed, localNotices.hintDismissed,
      snackOn, inst.standalone, inst.installed].join('|');
    if (sig.notice === key) return;
    sig.notice = key;
    var html = '';
    if (s.notices.offline) {
      html += noticeRow('ov-notice--warn', 'offline', esc(t('已离线 · 当前缓存可用')));
    }
    if (s.notices.newVersion && !localNotices.updateDismissed) {
      html += noticeRow('', 'refresh', esc(t('有新版本')),
        '<button class="btn btn-quiet" data-ov="reload">' + esc(t('重新载入')) + '</button>' +
        '<button class="btn btn-quiet" data-ov="notice-dismiss" data-notice="update">' + esc(t('稍后')) + '</button>');
    }
    // FEEDBACK-02: don't cry failure at a signed-out device. Business marks
    // the ordinary local-only-and-dirty state 'err' too ("本地模式（改动仅存浏览器）"),
    // and that already has its own, calmer surface below.
    if (s.sync.kind === 'err' && s.account.signedIn && !localNotices.syncDismissed) {
      html += noticeRow('ov-notice--danger', 'warning', esc(s.sync.text || t('同步失败')),
        (s.sync.retryVisible ? '<button class="btn btn-quiet" data-ov="retry-sync">' + esc(t('立即重试')) + '</button>' : '') +
        '<button class="icon-btn" data-ov="notice-dismiss" data-notice="sync" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button>', 'alert');
    }
    if (localNotices.geo && !localNotices.geoDismissed) {
      html += noticeRow('ov-notice--danger', 'warning',
        esc(localNotices.geoMessage || t('无法获取当前位置 · 请检查浏览器的定位权限')),
        '<button class="icon-btn" data-ov="notice-dismiss" data-notice="geo" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button>', 'alert');
    }
    // M-109 first-run row: one dismissible line, never a scrim.
    if (!quiet && !s.notices.introSeen && !localNotices.introDismissed && !s.notices.offline) {
      html += noticeRow('ov-notice--intro', 'info', esc(t('这是一张日本美食与景点地图')),
        '<button class="btn btn-quiet" data-ov="intro-region">' + esc(t('选一个地区开始')) + '</button>' +
        '<button class="btn btn-quiet" data-ov="intro-legend">' + esc(t('地图怎么看')) + '</button>' +
        '<button class="icon-btn" data-ov="intro-close" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button>');
    }
    // signed-out "this device only" hint
    if (!quiet && !s.account.signedIn && !s.sync.hintDismissed && !localNotices.hintDismissed && s.user.fav.size > 0) {
      html += noticeRow('', 'user', esc(t('收藏只保存在这台设备上')),
        '<button class="btn btn-quiet" data-ov="open-account">' + esc(t('登录以同步')) + '</button>' +
        '<button class="icon-btn" data-ov="hint-dismiss" aria-label="' + esc(t('关闭')) + '">' + ic('x') + '</button>');
    }
    // ENV-01 install offer: once per load, after the first Save.
    if (!quiet && snackOn && !localNotices.snackDismissed && !inst.standalone && !inst.installed) {
      html += noticeRow('ov-notice--offer', 'download',
        esc(t('把地图装到主屏幕')) + '<span class="ov-notice-sub">' + esc(t('下次一点就开')) + '</span>',
        '<button class="btn btn-quiet" data-ov="install-now">' + esc(t('安装')) + '</button>' +
        '<button class="btn btn-quiet" data-ov="install-later">' + esc(t('稍后')) + '</button>' +
        '<button class="btn btn-quiet" data-ov="install-never">' + esc(t('不再提示')) + '</button>');
    }
    R.notice.innerHTML = html;
  }

  function onLocateRequest() {
    localNotices.geo = false; localNotices.geoDismissed = false;
    sig.notice = null; App.requestRender('notice');
  }
  function onLocateResult(p) {
    p = p || {};
    if (p.status === 'ok') { localNotices.geo = false; sig.search = null; sig.notice = null; App.requestRender('geo'); return; }
    if (p.status === 'denied' || p.status === 'error') {
      localNotices.geo = true; localNotices.geoDismissed = false;
      localNotices.geoMessage = p.message || null;
      sig.search = null; sig.notice = null; App.requestRender('notice');
    }
  }
  O.setLocation = function () { sig.search = null; sig.notice = null; App.requestRender('geo'); };

  O.openTransient = function (kind, data) {
    transient = { kind: kind, data: data || null };
    sig.modal = null; sig.pop = null; sig.chips = null;
    nav.push(kind, function () { transient = null; sig.modal = null; sig.pop = null; sig.chips = null; App.requestRender('transient'); });
    App.requestRender('transient');
    if (kind === 'langMenu') motion.afterGeometry(function () { renderLangMenu(S()); });
  };
  function closeTransient() {
    if (!transient) return;
    var k = transient.kind;
    transient = null;
    nav.pop(k);
    var m = el('ov-lang-menu'); if (m) m.remove();
    sig.modal = null; sig.pop = null; sig.chips = null;
    App.requestRender('transient');
  }
  function renderLangMenu(s) {
    var old = el('ov-lang-menu'); if (old) old.remove();
    if (!transient || transient.kind !== 'langMenu') return;
    var node = document.createElement('div');
    node.className = 'ov-pop ov-menu scale-in';
    node.id = 'ov-lang-menu';
    node.setAttribute('role', 'menu');
    node.setAttribute('aria-label', t('语言'));
    node.innerHTML = '<div class="ov-pop-body is-flush">' + LANGS.map(function (l) {
      return '<button class="menu-row" role="menuitemradio" aria-checked="' + (s.lang === l.key) + '" data-ov="lang" data-lang="' + l.key + '">' +
        (s.lang === l.key ? ic('check') : '<span class="ic" aria-hidden="true"></span>') + esc(l.label) + '</button>';
    }).join('') + '</div>';
    R.pop.appendChild(node);
    var U = s.layout.U, a = lastTrigger && document.contains(lastTrigger) ? lastTrigger.getBoundingClientRect() : null;
    var w = 200;
    node.style.width = w + 'px';
    node.style.left = clamp(a ? a.right - w : U.x + U.w - 16 - w, U.x + 12, U.x + U.w - 12 - w) + 'px';
    node.style.top = (a ? a.bottom + 8 : U.y + 64) + 'px';
  }

  /* ----------------------------------------------------------------------
     6. Field wiring (no re-render under the caret) + actions
     -------------------------------------------------------------------- */
  function bindFields() {
    u.delegate(R.modal, 'input', 'input', function (e, node) {
      if (node.id === 'ov-name' || node.id === 'ov-list-name') { if (!draft) return; draft.name = node.value; clearFieldError(node); }
      else if (node.id === 'ov-emoji') { if (!draft) return; draft.emoji = node.value; validateEmoji(); syncEmojiChips(); }
    });
    u.delegate(R.pop, 'input', '#ov-region-q', function (e, node) {
      listFilter = node.value || '';
      filterRegionRows();
    });
    // Enter submits the open form (FORM-01: one commit path)
    u.delegate(R.modal, 'keydown', 'input', function (e, node) {
      if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;
      if (node.id === 'ov-name' || node.id === 'ov-emoji') { e.preventDefault(); saveBookmark(); }
      else if (node.id === 'ov-list-name') { e.preventDefault(); saveList(); }
    });
  }
  function clearFieldError(node) {
    draftErr.name = null;
    node.setAttribute('aria-invalid', 'false');
    var err = el('ov-list-err'); if (err) { err.textContent = ''; err.hidden = true; }
  }
  function validateEmoji() {
    var field = el('ov-emoji-field'), err = el('ov-emoji-err');
    if (!field || !err) return true;
    var v = (draft && draft.emoji) || '';
    var ok = !v || emoji.isPure(v);
    field.setAttribute('aria-invalid', ok ? 'false' : 'true');
    err.textContent = ok ? '' : t('只能填一个 emoji');
    err.hidden = ok;
    draftErr.emoji = ok ? null : t('只能填一个 emoji');
    return ok;
  }
  function syncEmojiChips() {
    var chips = R.modal.querySelectorAll('.ov-emoji-chip');
    Array.prototype.forEach.call(chips, function (c) {
      c.setAttribute('aria-pressed', String(c.dataset.emoji === (draft && draft.emoji)));
    });
  }
  function filterRegionRows() {
    var body = R.pop.querySelector('.ov-pop-body');
    if (!body) return;
    var q = (listFilter || '').toLowerCase();
    var visibleInGroup = 0, lastHead = null;
    Array.prototype.forEach.call(body.children, function (node) {
      if (node.classList.contains('ov-rg-head')) {
        if (lastHead) lastHead.hidden = visibleInGroup === 0;
        lastHead = node; visibleInGroup = 0; node.hidden = false; return;
      }
      if (!node.classList.contains('ov-rg-row')) return;
      var name = (node.textContent || '').toLowerCase();
      var code = node.dataset.code;
      var en = code === '' ? '' : (Data.config.REGIONS[Number(code)].en || '').toLowerCase();
      var hit = !q || name.indexOf(q) >= 0 || en.indexOf(q) >= 0;
      node.hidden = !hit;
      if (hit) visibleInGroup += 1;
    });
    if (lastHead) lastHead.hidden = visibleInGroup === 0;
  }

  /* ---- the one action table ------------------------------------------- */
  function runAction(name, node, e) {
    var s = S();
    switch (name) {
      case 'layout-diagnostics':
        node.parentElement.open = !node.parentElement.open;
        if (node.parentElement.open) node.parentElement.querySelector('pre').textContent = layoutDiagnosticText(s);
        break;
      case 'copy-layout-diagnostics': {
        var diagnostic = layoutDiagnosticText(s);
        node.parentElement.querySelector('pre').textContent = diagnostic;
        copyText(diagnostic);
        break;
      }
      case 'search-activate': rememberTrigger(); App.set({ search: { active: true, activeIndex: -1 } }); break;
      case 'search-cancel':
        App.emit('search:cancel', { reason: 'button' });
        App.set({ search: { active: false, activeIndex: -1 } });
        break;
      case 'search-clear': {                                   // SEARCH-03: keeps searchActive + focus
        apiSeq++; abortApi(); clearTimeout(apiDebounce);
        api = { items: null, pending: false, error: null, q: '' };
        App.set({ search: { query: '', activeIndex: -1, placeFilter: null, dropLoc: false } });
        if (window.MapMod && MapMod.placeTempPin) MapMod.placeTempPin(null);
        var inp = el('ov-sinput');
        if (inp) { inp.value = ''; inp.focus(); }
        break;
      }
      case 'pick': O.pick(Number(node.dataset.i)); break;
      case 'place-retry': if (api.q) ssSearchPlaces(api.q, s.lang); break;
      case 'nearby': {
        // "Find nearby" is a detour, not a new plan: remember where the plan
        // was (region / sort / centre / zoom) so 回到规划 can put it back. The
        // record rides in tabelog.listView through state.nearby, in the shape
        // 3.2.x wrote — sort is stored in the business spelling ('award').
        var nb = nearbyState(s);
        if (!nb.active) {
          var cc = s.mapView.center || [];
          App.set({ nearby: { active: true, pending: true, planning: {
            region: (s.filters.region === undefined) ? null : s.filters.region,
            sort: s.sort === 'awards' ? 'award' : s.sort,
            center: [cc[0], cc[1]], zoom: s.mapView.zoom } } });
        }
        App.emit('map:locate-request');
        break;
      }
      case 'restore-plan': {
        var pl = nearbyState(s).planning;
        App.set({ nearby: { active: false, pending: false } });
        if (!pl) break;
        act.applyFilters({ region: pl.region == null ? null : pl.region });
        App.set({ sort: pl.sort === 'award' ? 'awards' : pl.sort });
        if (window.MapMod && MapMod.restoreView) { try { MapMod.restoreView(pl.center, pl.zoom); } catch (err) {} }
        break;
      }
      case 'open': {
        var kind = node.dataset.kind;
        if (s.overlay.kind === kind) { act.closeOverlay('toggle'); break; }
        rememberTrigger(); openOverlay(kind, null); break;
      }
      case 'open-account': rememberTrigger(); openOverlay('account', null); break;
      case 'close': act.closeOverlay('cancel'); break;
      case 'scrim': act.closeOverlay('scrim'); break;
      case 'tab': act.setTab(node.dataset.tab); break;
      case 'lang-menu': rememberTrigger(); O.openTransient('langMenu'); break;
      case 'lang': act.setLanguage(node.dataset.lang); break;      // full navigation (tabelog.langSwitch)
      case 'gate-lang': {
        // Always go through setLanguage: it is what writes tabelog.lang, and
        // an unwritten key means the gate greets this visitor again tomorrow.
        langGateDone = true;
        act.setLanguage(node.dataset.lang);
        break;
      }

      /* layers */
      case 'layer-toggle': toggleLayer(node.dataset.layer); break;
      case 'layer-retry': retryLayer(node.dataset.layer); break;
      case 'layer-hidden': {
        var hOn = !s.layers.hiddenLandmarks;
        act.setLayers({ hiddenLandmarks: hOn, landmarks: hOn ? true : s.layers.landmarks });
        break;
      }

      /* account */
      // Two-step, because signing out on a shared device and signing out on
      // your own device want opposite things. act.signOut(clearLocal) passes an
      // options object to Business, which then deliberately SKIPS its own
      // native confirm() — the confirmation is ours to build. (adapter
      // coreRequest #5)
      case 'signout': signOutArmed = true; sig.pop = null; sig.modal = null; App.requestRender('overlay'); break;
      case 'signout-cancel': signOutArmed = false; sig.pop = null; sig.modal = null; App.requestRender('overlay'); break;
      case 'signout-keep': signOutArmed = false; act.signOut(false); break;
      case 'signout-clear': signOutArmed = false; act.signOut(true); break;
      case 'retry-sync': act.retrySync(); localNotices.syncDismissed = false; sig.notice = null; App.requestRender('notice'); break;
      case 'refresh-storage': act.refreshStorage(); break;
      case 'hint-dismiss': act.dismissSyncHint(); localNotices.hintDismissed = true; sig.notice = null; App.requestRender('notice'); break;
      case 'reset-filters': act.resetFilters(); act.closeOverlay('done'); break;
      case 'export': act.exportBackup(); break;
      case 'import': openFilePicker(); break;
      case 'imp-pick': {
        if (!importPreview) break;
        var pk = node.dataset.key;
        importPreview.picks[pk] = !!node.checked;
        break;
      }
      case 'import-confirm': confirmImport(); break;
      case 'privacy': window.open('privacy.html', '_blank', 'noopener'); break;
      case 'native-settings': act.openNativeSettings(); act.closeOverlay('done'); break;
      case 'delete-cloud': {
        if (!deleteArmedAt || Date.now() - deleteArmedAt > 6000) {
          deleteArmedAt = Date.now(); deleteMsg = null;
          clearTimeout(deleteTimer);
          deleteTimer = setTimeout(function () { deleteArmedAt = 0; sig.pop = null; App.requestRender('overlay'); }, 6000);
        } else {
          deleteArmedAt = 0; clearTimeout(deleteTimer);
          // DATA-04: Business clears the five keys on 200/204 only, and reloads.
          act.deleteCloud(function (ok, text, isErr) {
            deleteMsg = { text: text || '', err: !!isErr };
            sig.pop = null; App.requestRender('overlay');
          });
        }
        sig.pop = null; App.requestRender('overlay');
        break;
      }
      case 'help':
        rememberTrigger();
        returnChain = s.overlay.kind === 'account' ? { kind: 'account', payload: null } : null;
        openOverlay('help', { topic: node.dataset.topic || 'about' });
        break;
      case 'install-now': try { act.install(); } catch (err) {} localNotices.snackDismissed = true; sig.notice = null; App.requestRender('notice'); break;
      case 'install-later': act.snoozeInstall(); localNotices.snackDismissed = true; sig.notice = null; App.requestRender('notice'); break;
      case 'install-never': act.neverInstall(); localNotices.snackDismissed = true; sig.notice = null; App.requestRender('notice'); break;

      /* region / sort / more / share */
      case 'region': {
        var code = node.dataset.code === '' ? null : Number(node.dataset.code);
        act.applyFilters({ region: code });
        act.closeOverlay('done');
        break;
      }
      case 'sort': App.set({ sort: node.dataset.key }); act.closeOverlay('done'); break;
      case 'more-run': {
        var items = moreItemsFor(s);
        var it = items[Number(node.dataset.i)];
        if (it && typeof it.run === 'function') it.run();
        else act.closeOverlay('done');
        break;
      }
      case 'share-copy': {
        var sid = (s.overlay.payload || {}).id;
        copyText((sid && act.shareUrl(sid)) || location.href);
        break;
      }
      case 'share-native': {
        var nid = (s.overlay.payload || {}).id;
        if (nid) act.share(nid, e);
        break;
      }

      /* place menu */
      case 'place-new': {
        var p = s.overlay.payload || {};
        O.openBookmarkForm({ lat: p.lat, lon: p.lon, category: node.dataset.cat,
          name: (p.place && p.place.name) || '' });
        break;
      }
      case 'lm-hide': {
        var ph = s.overlay.payload || {};
        if (ph.bm) act.hideLandmark(ph.bm);
        act.closeOverlay('done');
        break;
      }
      case 'lm-unhide': {
        var pu = s.overlay.payload || {};
        if (pu.bm) act.unhideLandmark(pu.bm);
        act.closeOverlay('done');
        break;
      }
      case 'pin-edit': {
        var pe = s.overlay.payload || {};
        if (pe.bm) O.openBookmarkForm({ mode: 'edit', bm: pe.bm, lat: pe.bm.lat, lon: pe.bm.lon,
          category: pe.bm.category, emoji: pe.bm.emoji, name: Data.pinName(pe.bm, s.lang) });
        break;
      }
      case 'pin-delete': {
        var pd = s.overlay.payload || {};
        if (pd.bm) act.removePin(pd.bm);       // raises its own 9 s undo toast
        act.closeOverlay('done');
        break;
      }
      case 'place-copy': {
        var pc = s.overlay.payload || {};
        copyText(Number(pc.lat).toFixed(6) + ', ' + Number(pc.lon).toFixed(6));
        act.closeOverlay('done');
        break;
      }
      case 'place-gmaps': {
        var pg = s.overlay.payload || {};
        window.open('https://www.google.com/maps/search/?api=1&query=' + pg.lat + ',' + pg.lon, '_blank', 'noopener');
        break;
      }

      /* forms */
      case 'copy-coord': copyText(draft ? (Number(draft.lat).toFixed(6) + ', ' + Number(draft.lon).toFixed(6)) : ''); break;
      case 'form-type': {
        if (!draft) break;
        draft.category = node.dataset.val;
        var seg = R.modal.querySelectorAll('[data-ov="form-type"]');
        Array.prototype.forEach.call(seg, function (b) { b.setAttribute('aria-checked', String(b.dataset.val === draft.category)); });
        break;
      }
      case 'form-list': {
        if (!draft) break;
        var lid = node.dataset.list;
        draft.lists = draft.lists || [];
        var at = draft.lists.indexOf(lid);
        if (at >= 0) draft.lists.splice(at, 1); else draft.lists.push(lid);
        node.setAttribute('aria-pressed', String(at < 0));
        break;
      }
      case 'emoji-pick': {
        if (!draft) break;
        draft.emoji = node.dataset.emoji;
        var f = el('ov-emoji'); if (f) f.value = draft.emoji;
        validateEmoji(); syncEmojiChips();
        if (transient && transient.kind === 'emojiPicker') closeTransient();
        break;
      }
      case 'emoji-clear': {
        if (!draft) break;
        draft.emoji = '';
        var f2 = el('ov-emoji'); if (f2) { f2.value = ''; f2.focus(); }
        validateEmoji(); syncEmojiChips();
        break;
      }
      case 'emoji-more': ensurePickerModule(); O.openTransient('emojiPicker'); break;
      case 'emoji-close': closeTransient(); break;
      case 'new-list': {
        var from = node.dataset.from;
        returnChain = from === 'memberPicker'
          ? { kind: 'memberPicker', payload: s.overlay.payload }
          : (from === 'bookmarkForm' ? { kind: 'bookmarkForm', payload: bookmarkPayloadFromDraft() } : null);
        var members = from === 'memberPicker' && s.overlay.payload ? [s.overlay.payload.id] : [];
        draft = null; draftKey = null;
        openOverlay('listForm', { mode: 'create', draft: { name: '', emoji: Data.config.LIST_QUICK_EMOJI[0] }, members: members, returnTo: from });
        break;
      }
      case 'member-toggle': toggleMember(node.dataset.list, (s.overlay.payload || {}).id); break;
      case 'member-default': toggleDefault((s.overlay.payload || {}).id); break;
      case 'member-unsave': unsave((s.overlay.payload || {}).id); act.closeOverlay('done'); break;
      case 'save-bookmark': saveBookmark(); break;
      case 'save-list': saveList(); break;

      /* lightbox + notices */
      case 'lb-step': stepLightbox(Number(node.dataset.d)); break;
      case 'reload': act.acceptUpdate(); break;
      case 'notice-dismiss':
        if (node.dataset.notice === 'geo') localNotices.geoDismissed = true;
        else if (node.dataset.notice === 'sync') localNotices.syncDismissed = true;
        else { localNotices.updateDismissed = true; act.laterUpdate(); }
        sig.notice = null; App.requestRender('notice');
        break;
      case 'intro-close': act.markIntroSeen(); localNotices.introDismissed = true; sig.notice = null; App.requestRender('notice'); break;
      case 'intro-region': act.markIntroSeen(); localNotices.introDismissed = true; rememberTrigger(); openOverlay('regionPicker', null); break;
      case 'intro-legend': act.markIntroSeen(); localNotices.introDismissed = true; rememberTrigger(); openOverlay('help', { topic: 'legend' }); break;
      default: break;
    }
    if (e && e.preventDefault && node.tagName !== 'INPUT') e.preventDefault();
  }

  function bookmarkPayloadFromDraft() {
    if (!draft) return null;
    return { mode: 'create', lat: draft.lat, lon: draft.lon, lists: (draft.lists || []).slice(),
      draft: { name: draft.name, emoji: draft.emoji, category: draft.category } };
  }
  O.openBookmarkForm = function (opts) {
    rememberTrigger();
    returnChain = null;
    draft = null; draftKey = null;
    openOverlay('bookmarkForm', { mode: opts.mode === 'edit' && opts.bm ? 'edit' : 'create',
      bm: opts.bm || null, lat: opts.lat, lon: opts.lon, lists: opts.lists || [],
      draft: { name: opts.name || '', emoji: opts.emoji || (opts.category === 'attraction' ? '⛩️' : Data.config.BOOKMARK_QUICK_EMOJI[0]),
               category: opts.category || 'bookmark' } });
  };

  /* ---- writes ----------------------------------------------------------- */
  function saveBookmark() {
    if (saving || !draft) return;
    if (!validateEmoji()) { var f = el('ov-emoji'); if (f) f.focus(); return; }
    saving = true;
    var name = (draft.name || '').trim() || t('未命名地点');
    var pl = (App.state.overlay.payload || {});
    try {
      if (pl.mode === 'edit' && pl.bm && typeof act.editPin === 'function') {
        // The id is never rewritten: every category:'meta' member row
        // references the pin by it.
        act.editPin(pl.bm, { name: name, emoji: draft.emoji || '📍',
          category: draft.category === 'attraction' ? 'attraction' : 'bookmark' });
      } else {
        act.addPin({ name: name, emoji: draft.emoji || '📍', lat: draft.lat, lon: draft.lon,
          category: draft.category === 'attraction' ? 'attraction' : 'bookmark',
          lists: (draft.lists || []).slice() });
      }
    } catch (err) {
      saving = false;
      console.error('[overlays] addPin', err);
      var ee = el('ov-emoji-err');
      if (ee) { ee.textContent = t('保存失败，请重试'); ee.hidden = false; }
      return;                                   // FORM-01: a failed write never closes
    }
    returnChain = null;
    act.closeOverlay('saved');
  }

  function saveList() {
    if (saving || !draft) return;
    var name = (draft.name || '').trim();
    if (!name) { showListError(t('请填写收藏夹名称')); return; }
    var dup = Data.lists().some(function (l) { return l.name === name && l.id !== draft.id; });
    if (dup) { showListError(t('已经有同名的收藏夹')); return; }
    saving = true;
    var savedId = draft.id;
    try {
      if (draft.id) act.renameList(draft.id, name, draft.emoji);
      else {
        savedId = act.createList(name, draft.emoji);
        (draft.members || []).forEach(function (ref) {
          if (!ref) return;
          if (!S().user.fav.has(ref)) act.toggleFav(ref);   // DATA-03
          act.addToList(savedId, ref);
        });
      }
    } catch (err) {
      saving = false;
      console.error('[overlays] saveList', err);
      showListError(t('保存失败，请重试'));
      return;
    }
    if (typeof draft.onSaved === 'function') { try { draft.onSaved(savedId); } catch (e) {} }
    act.closeOverlay('saved');                  // returnChain (if any) reopens the caller
  }
  function showListError(msg) {
    draftErr.name = msg;
    var err = el('ov-list-err'), input = el('ov-list-name');
    if (err) { err.textContent = msg; err.hidden = false; }
    if (input) { input.setAttribute('aria-invalid', 'true'); input.focus(); }
  }

  /* ---- import (DATA-02) — Business owns the parse, merge and rollback ---- */
  // In the Android shell the file picker is NOT free to open: JsonFiles.choose() refuses
  // every onShowFileChooser that is not inside a 3 s window armed by a preceding
  // Native.prepareJsonImport() (STANDARDS §15, the 3.1.0 SAF bridge). Without this call the
  // input.click() below is swallowed silently, the system picker never appears, and JSON
  // import — the only way to get data back into a WebView whose localStorage is app-private
  // and erased on uninstall — is dead inside the app. The call must stay inside the click's
  // user activation: the façade's fileRequest() refuses without navigator.userActivation.
  function openFilePicker() {
    var input = ensureFileInput();
    var n = window.Native;
    if (location.origin === 'https://jpfoodmap.com' && n && typeof n.prepareJsonImport === 'function') {
      var refuse = function () { previewImport(null, t('读取文件失败'), ''); };
      try {
        Promise.resolve(n.prepareJsonImport()).then(function (allowed) {
          if (allowed) input.click(); else refuse();
        }, refuse);
      } catch (e) { refuse(); }
      return;
    }
    input.click();
  }
  function ensureFileInput() {
    var input = el('ov-file');
    if (!input) {
      input = document.createElement('input');
      input.type = 'file'; input.accept = 'application/json,.json';
      input.className = 'ov-file'; input.id = 'ov-file';
      input.addEventListener('change', function () {
        var file = input.files && input.files[0];
        input.value = '';
        if (!file) return;
        act.readImportFile(file, function (norm, err) { previewImport(norm, err, file.name); });
      });
      (R.modal.parentNode || document.body).appendChild(input);
    }
    return input;
  }
  function previewImport(norm, err, fileName) {
    var s = S();
    if (!norm) {
      importPreview = { token: u.uid('imp'), file: fileName, norm: null, error: err || t('无法读取这个文件'),
        counts: { contains: 0, add: 0, skip: 0 }, picks: { fav: true, black: true, bm: true } };
    } else {
      var have = new Set();
      (s.user.bookmarks || []).forEach(function (b) { if (b && b.id) have.add(b.id); });
      var addFav = (norm.favorites || []).filter(function (x) { return !s.user.fav.has(x); }).length;
      var addBlack = (norm.blacklist || []).filter(function (x) { return !s.user.black.has(x); }).length;
      var addBm = (norm.bookmarks || []).filter(function (b) { return b && b.id && !have.has(b.id); }).length;
      var skip = Number.isFinite(norm.skipped) ? norm.skipped : 0;
      var contains = (norm.favorites || []).length + (norm.blacklist || []).length + (norm.bookmarks || []).length + skip;
      importPreview = { token: u.uid('imp'), file: fileName, norm: norm, error: null,
        counts: { contains: contains, add: addFav + addBlack + addBm, skip: skip },
        picks: { fav: true, black: true, bm: true } };
    }
    rememberTrigger();
    sig.modal = null;
    openOverlay('importDialog', null);
  }
  function confirmImport() {
    if (!importPreview || !importPreview.norm) { act.closeOverlay('cancel'); return; }
    var r;
    try { r = act.applyImport(importPreview.norm, importPreview.picks); }
    catch (err) {
      // Business already rolled the in-memory state back; say so, keep the form.
      console.error('[overlays] import', err);
      importPreview.error = t('导入失败，已回滚');
      importPreview.token = u.uid('imp');
      sig.modal = null; App.requestRender('overlay');
      return;
    }
    if (r && r.error) {
      importPreview.error = r.error; importPreview.token = u.uid('imp');
      sig.modal = null; App.requestRender('overlay');
      return;
    }
    if (r && r.text) act.showToast({ kind: 'info', text: r.text });
    importPreview = null;
    act.closeOverlay('done');
  }

  function copyText(text) {
    if (!text) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
        act.showToast({ kind: 'info', text: t('已复制') });
        return;
      }
    } catch (e) { /* fall through */ }
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
      act.showToast({ kind: 'info', text: t('已复制') });
    } catch (e2) { act.showToast({ kind: 'info', text: t('复制失败，请手动复制') }); }
  }

  O.destroy = function () {
    R.search.innerHTML = ''; R.pop.innerHTML = ''; R.modal.innerHTML = ''; R.notice.innerHTML = '';
    if (R.topRight) R.topRight.innerHTML = '';
    setInert(false);
    sig = {};
  };

  window.App.registerModule('overlays', O);
})();
