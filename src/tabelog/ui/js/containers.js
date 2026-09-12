/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/containers.js
   Owns: the narrow bottom sheet (state machine, 1:1 drag + snap, three header
   variants, the single scroll area and the footer slot), the mid/wide top bar,
   left column, 56px rail, detail column and candidate host, all of LAY-01..05,
   and *where every other module's root lives right now*. Other modules always
   render into their own persistent root; containers only moves that root
   between hosts, so listeners and scroll positions live.

   Ported from ui-captures-2026-09-10/redesign/demo-4.0/js/containers.js. What
   changed for production:
     · counts come from Data.M(state) / state.user.fav / Filters.badge() — the
       real corpus, never a fixture;
     · the detail pager walks the real result set (Data.sort over Data.M);
     · copy lives in ui/i18n/ui-strings.json, so there is no inline
       I18N.register here (a Chinese literal outside t() would be scanned as an
       untranslated run by the build's second i18n pass).

   Public API (CONTRACT.md §containers):
     Containers.slot(name)                  → host element of a root
     Containers.scroller(name)              → the single scrolling element
     Containers.snapTo(sheetState)          → animate to a semantic stop
     Containers.measureSheet(state, s)      → {F, C} for LAY-04
     Containers.headerVariant(state)        → 'entry' | 'tabs' | 'nav'
   Additions:
     Containers.host(module) / scrollEl(module)
     Containers.setHeader(variant, data)
     Containers.afterSettle(fn)
     Containers.temporarilyCollapse() / restore()   LAYER-03 fallback 1
     Containers.snapCandidates(state)
     Containers.detailTools(state)                  the ⋯ / expand / × cluster
     Containers.mapBand(state)
   Events emitted: 'sheet:snapped' {state, reason:'drag'}, 'columns:changed'.
   ========================================================================== */
(function () {
  'use strict';

  var C = window.Containers = {};
  var ctx, App, util, t, D, roots, els = {}, CFG;
  var _html = {};                 // element id → last written innerHTML (idempotent render)
  var _drag = null;               // live pointer drag
  var _dragPending = null;        // sheet state a drag just snapped to
  var _headerOverride = null;     // setHeader()
  var _tempCollapsed = null;      // LAYER-03 saved semantic stop
  var _lastSheetH = null;
  var _lastCols = null;

  var SHEET_MIN = 80;

  /* ======================================================================
     init
     ==================================================================== */
  C.init = function (c) {
    ctx = c; App = c.App; util = c.util; t = c.t; D = c.Data; roots = c.roots;
    CFG = c.layout.CFG; SHEET_MIN = CFG.sheet.collapsed;

    ['app', 'narrow-top', 'sheet', 'sheet-handle', 'sheet-head', 'sheet-body', 'sheet-toast', 'sheet-foot',
      'topbar', 'topbar-brand', 'topbar-search', 'topbar-right',
      'col-left', 'col-left-rail', 'col-left-head', 'col-left-body', 'col-left-toast', 'col-left-foot',
      'col-detail', 'col-detail-head', 'col-detail-body', 'col-detail-toast', 'col-detail-foot',
      'candidates-host', 'parking'].forEach(function (id) { els[id] = document.getElementById(id); });

    els.sheet.setAttribute('role', 'region');
    els['sheet-handle'].setAttribute('role', 'button');
    els['sheet-handle'].setAttribute('tabindex', '0');
    els['sheet-handle'].innerHTML = '<span class="handle"></span>';

    c.layout.registerSheetMeasure(C.measureSheet);
    bindClicks();
    bindDrag();

    // NAV-02 / LAY-05: geometry first, then whoever restores an anchor.
    App.on('nav:restore', function (prev) {
      C.afterSettle(function () { App.emit('containers:restored', prev); });
    });
    // a cancelled search puts the panel back exactly where it was (NAV-04)
    App.on('search:cancel', function () { App.requestRender('search'); });
    // NAV-01/NAV-02: after a card closes the caret must be somewhere the
    // reader can see. The list returns it to the source row when there was
    // one; otherwise we place it. The flag survives the renders that follow,
    // because a header rewrite can drop the caret again a frame later.
    App.on('detail:close', function (p) {
      // A card opened from the result list or from Saved has a source row, and
      // the list module puts the caret back on it (NAV-02). Do not compete:
      // the header fallback is only for a card opened from search, a marker or
      // a deep link, where there is no row to go back to.
      var origin = p && p.selected && p.selected.origin;
      _rescueRef = (origin === 'results' || origin === 'saved') ? (p.selected.id || null) : null;
      _rescue = true;
      _rescueAt = Date.now();
      clearTimeout(_rescueTimer);
      // Bounded window: after this the reader owns the caret again, and a
      // deliberate click on the map must be allowed to blur.
      _rescueTimer = setTimeout(function () { _rescue = false; }, 2000);
      rescueFocus();
    });
    // Whatever hands the caret to <body> inside that window — a header
    // rewrite, an overlay releasing a trigger that has since been destroyed,
    // a node going inert — put it back. Ordering-proof, which the render-time
    // check alone was not.
    document.addEventListener('focusout', function () {
      if (!_rescue) return;
      util.raf(function () { if (_rescue) rescueFocus(3); });
    }, true);
    // Any deliberate press ends the window — the reader owns the caret again.
    document.addEventListener('pointerdown', function () { _rescue = false; }, true);
  };

  /* ======================================================================
     event wiring — every control is a real button with an accessible name
     ==================================================================== */
  function bindClicks() {
    var hosts = [els.sheet, els['col-left'], els['col-detail']];
    hosts.forEach(function (host) {
      util.delegate(host, 'click', '[data-ct]', function (e, el) {
        var a = el.dataset.ct;
        if (a === 'tab') { ctx.act.setTab(el.dataset.tab); return; }
        if (a === 'sheet') { C.snapTo(el.dataset.sheet); return; }
        if (a === 'back') { ctx.nav.back(); return; }
        if (a === 'close-detail') { ctx.act.closeDetail(); return; }
        if (a === 'collapse-left') { App.set({ columns: { userLeftPreference: 'closed' } }); return; }
        if (a === 'expand-left') { App.set({ columns: { userLeftPreference: 'open' } }); return; }
        if (a === 'account') { ctx.act.openOverlay('account', null); return; }
        if (a === 'step') { stepDetail(Number(el.dataset.step)); return; }
      });
    });
  }

  /** detail column pager (wide): move inside the same result set, origin kept. */
  function stepDetail(dir) {
    var s = App.state, ids = candidateIds(s);
    var i = ids.indexOf(s.selected.id);
    if (i < 0) return;
    var n = i + dir;
    if (n < 0 || n >= ids.length) return;
    ctx.act.openDetail(ids[n], 'candidates');
  }

  /** the ordered result set the pager walks — the list module's if it is up. */
  function candidateIds(s) {
    try {
      if (window.Detail && typeof window.Detail.candidates === 'function') {
        var ids = window.Detail.candidates(s);
        if (ids && ids.length) return ids;
      }
    } catch (e) { /* detail not ready */ }
    try {
      if (window.ListMod && typeof window.ListMod.rowsFor === 'function' && s.sheet.tab !== 'filters') {
        var L = window.ListMod.rowsFor(s);
        if (L && L.length) return L;
      }
    } catch (e2) { /* list not ready */ }
    return D.sort(D.M(s), s.sort, { from: null });
  }

  /* ======================================================================
     drag: 1:1 with the finger, snap by velocity + position (MOTION M1)
     ==================================================================== */
  function bindDrag() {
    var h = els['sheet-handle'];
    h.addEventListener('pointerdown', onDown);
    h.addEventListener('pointermove', onMove);
    h.addEventListener('pointerup', onUp);
    h.addEventListener('pointercancel', onUp);
    h.addEventListener('keydown', onKey);
  }

  function sheetPx() { return els.sheet.getBoundingClientRect().height; }

  function onDown(e) {
    if (App.state.layout.mode !== 'narrow') return;
    if (e.button !== undefined && e.button > 0) return;
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    _drag = { id: e.pointerId, y0: e.clientY, h0: sheetPx(), h: sheetPx(), moved: false, v: 0, lastY: e.clientY, lastT: now };
    try { els['sheet-handle'].setPointerCapture(e.pointerId); } catch (err) { /* older engines */ }
    els.sheet.setAttribute('data-dragging', '');
    App.set({ sheet: { dragging: true } }, { silent: true });
    e.preventDefault();
  }

  function onMove(e) {
    if (!_drag || e.pointerId !== _drag.id) return;
    var s = App.state;
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    var dy = _drag.y0 - e.clientY;
    if (Math.abs(dy) > 4) _drag.moved = true;
    var h = util.clamp(_drag.h0 + dy, SHEET_MIN, s.layout.H);
    var dt = now - _drag.lastT;
    if (dt > 0) _drag.v = ((_drag.lastY - e.clientY) / dt) * 0.5 + _drag.v * 0.5;   // px/ms, up = +
    _drag.lastY = e.clientY; _drag.lastT = now;
    _drag.h = h;
    document.documentElement.style.setProperty('--sheet-h', Math.round(h) + 'px');
  }

  function onUp(e) {
    if (!_drag || (e && e.pointerId !== undefined && e.pointerId !== _drag.id)) return;
    var s = App.state, d = _drag;
    _drag = null;
    els.sheet.removeAttribute('data-dragging');
    App.set({ sheet: { dragging: false } }, { silent: true });

    var cands = C.snapCandidates(s);
    var target;
    if (!d.moved) {
      target = tapTarget(s, cands);
    } else {
      var projected = util.clamp(d.h + d.v * 120, SHEET_MIN, s.layout.H);
      var best = cands[0], bd = Infinity;
      cands.forEach(function (k) {
        var th = ctx.layout.sheetTarget(k, s).h;
        var dist = Math.abs(th - projected);
        if (dist < bd) { bd = dist; best = k; }
      });
      target = best;
    }
    applySnap(target, 'drag');
  }

  function tapTarget(s, cands) {
    var cur = s.sheet.state;
    if (cur === 'expanded' || cur === 'full') return cands[1];
    if (cur === 'collapsed') return cands[1];
    return 'expanded';
  }

  function onKey(e) {
    var s = App.state;
    if (s.layout.mode !== 'narrow') return;
    var cands = C.snapCandidates(s);
    var i = cands.indexOf(s.sheet.state === 'full' ? 'expanded' : s.sheet.state);
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault(); applySnap(tapTarget(s, cands), 'key'); return;
    }
    if (e.key === 'ArrowUp') { e.preventDefault(); applySnap(cands[Math.min(cands.length - 1, (i < 0 ? 1 : i) + 1)], 'key'); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); applySnap(cands[Math.max(0, (i < 0 ? 1 : i) - 1)], 'key'); return; }
  }

  /** the semantic stops reachable from the current task (LAY-03). */
  C.snapCandidates = function (s) {
    s = s || App.state;
    if (s.selected.id) return ['collapsed', 'detail', 'expanded'];
    if (s.sheet.tab === 'filters') return ['collapsed', 'filter', 'expanded'];
    return ['collapsed', 'browse', 'expanded'];
  };

  function applySnap(target, reason) {
    var s = App.state;
    // paint the target height now so the transition starts from the dragged height
    document.documentElement.style.setProperty('--sheet-h', ctx.layout.sheetTarget(target, s).h + 'px');
    _dragPending = reason === 'drag' ? target : null;
    if (target === 'collapsed' && s.selected.id) { ctx.act.closeDetail(); return; }
    if (target === s.sheet.state) { App.requestRender('sheet'); return; }
    ctx.act.setSheet(target, reason === 'drag' ? 'drag' : undefined);
  }

  /** snapTo(state) — the public, animated way to change the semantic stop. */
  C.snapTo = function (st) {
    var s = App.state;
    if (st === 'collapsed' && s.selected.id) { ctx.act.closeDetail(); return; }
    // repaint the variable even when the semantic stop does not change: a drag
    // that ended on the same stop left a hand-painted height behind, and
    // App.set would report no change and never re-derive it.
    if (s.layout.mode === 'narrow') {
      document.documentElement.style.setProperty('--sheet-h', ctx.layout.sheetTarget(st, s).h + 'px');
    }
    ctx.act.setSheet(st);
  };

  /* ======================================================================
     LAY-04 measurement: real fixed chrome F, minimum body C
     ==================================================================== */
  C.measureSheet = function (sheetState, state) {
    var F = 0;
    if (App.state.layout.mode === 'narrow') {
      ['sheet-handle', 'sheet-head', 'sheet-toast', 'sheet-foot'].forEach(function (id) {
        var el = els[id];
        if (!el) return;
        if (id !== 'sheet-handle' && id !== 'sheet-head' && sheetState === 'collapsed') return;
        F += el.offsetHeight || 0;
      });
    }
    var S = CFG.sheet, Cmin;
    if (sheetState === 'collapsed') Cmin = 0;
    else if (state && state.search && state.search.active) Cmin = S.minSuggestion;
    else if (sheetState === 'filter') Cmin = S.minControl;
    else if (sheetState === 'detail') Cmin = S.minText;
    else if (state && state.selected && state.selected.id) Cmin = S.minText;
    else Cmin = S.minControl;
    return { F: Math.round(F), C: Cmin };
  };

  /* ======================================================================
     slots — CONTRACT §4. Only containers moves a root.
     ==================================================================== */
  var ROOT_OF = {
    search: 'searchRoot', list: 'listRoot', listFoot: 'listFoot', detail: 'detailRoot',
    detailFoot: 'detailFoot', filters: 'filtersRoot', filtersFoot: 'filtersFoot',
    candidates: 'candidatesRoot', toast: 'toastRoot'
  };
  C.slot = function (name) { var r = roots[ROOT_OF[name]]; return r ? r.parentElement : null; };
  C.host = C.slot;
  C.scroller = function (name) {
    var host = C.slot(name);
    if (!host) return null;
    if (host.id === 'sheet-body' || host.id === 'col-left-body' || host.id === 'col-detail-body') return host;
    return host.closest ? (host.closest('#sheet-body,#col-left-body,#col-detail-body') || null) : null;
  };
  C.scrollEl = C.scroller;
  /** afterSettle(fn) — LAY-05: never touch scrollTop / focus mid-transition. */
  C.afterSettle = function (fn) { ctx.motion.afterGeometry(fn); };

  /** Move a module root to a new host. appendChild() re-inserts the subtree,
   *  and a re-insert sends the keyboard caret to <body> if it was inside —
   *  which silently undid the list's NAV-02 focus restore whenever a render
   *  re-parented #list-root after it. Carry focus across the move. */
  function place(root, host) {
    if (!root || !host || root.parentElement === host) return;
    var a = document.activeElement;
    var keep = (a && a !== document.body && root.contains(a)) ? a : null;
    host.appendChild(root);
    if (keep && keep.isConnected && document.activeElement !== keep) {
      try { keep.focus({ preventScroll: true }); } catch (e) { /* detached */ }
    }
  }
  function park(root) { place(root, els.parking); }

  function placeRoots(s) {
    var mode = s.layout.mode;
    if (mode === 'narrow') {
      place(roots.searchRoot, els['narrow-top']);
      place(roots.toastRoot, els['sheet-toast']);
      park(roots.candidatesRoot);
      if (s.selected.id) {
        place(roots.detailRoot, els['sheet-body']); place(roots.detailFoot, els['sheet-foot']);
        park(roots.listRoot); park(roots.listFoot); park(roots.filtersRoot); park(roots.filtersFoot);
      } else if (s.sheet.tab === 'filters') {
        place(roots.filtersRoot, els['sheet-body']); place(roots.filtersFoot, els['sheet-foot']);
        park(roots.listRoot); park(roots.listFoot); park(roots.detailRoot); park(roots.detailFoot);
      } else {
        place(roots.listRoot, els['sheet-body']); place(roots.listFoot, els['sheet-foot']);
        park(roots.filtersRoot); park(roots.filtersFoot); park(roots.detailRoot); park(roots.detailFoot);
      }
      return;
    }
    var cols = ctx.layout.columns(s);
    place(roots.searchRoot, els['topbar-search']);
    if (s.sheet.tab === 'filters') {
      place(roots.filtersRoot, els['col-left-body']); place(roots.filtersFoot, els['col-left-foot']);
      park(roots.listRoot); park(roots.listFoot);
    } else {
      place(roots.listRoot, els['col-left-body']); place(roots.listFoot, els['col-left-foot']);
      park(roots.filtersRoot); park(roots.filtersFoot);
    }
    if (cols.detailW > 0) {
      place(roots.detailRoot, els['col-detail-body']); place(roots.detailFoot, els['col-detail-foot']);
      place(roots.toastRoot, els['col-detail-toast']);
    } else {
      park(roots.detailRoot); park(roots.detailFoot);
      place(roots.toastRoot, els['col-left-toast']);
    }
    // §10.4: the strip stays hosted while a detail is open at mid even when the user
    // collapsed it — detail renders a one-line "show again" rail in that case.
    if (mode === 'mid' && cols.detailW > 0) place(roots.candidatesRoot, els['candidates-host']);
    else park(roots.candidatesRoot);
  }

  /* ======================================================================
     render
     ==================================================================== */
  /** Write a chrome host's markup. An innerHTML replacement drops the
   *  keyboard caret to <body> if it was inside — the header variant changes
   *  the moment a detail closes, which is exactly when the caret has just been
   *  put on it. Carry focus across by [data-ct]+[data-tab]/[data-sheet]. */
  function html(id, markup) {
    if (_html[id] === markup) return;
    _html[id] = markup;
    var host = els[id];
    var a = document.activeElement;
    var mark = null;
    if (a && a !== document.body && host.contains(a) && a.dataset && a.dataset.ct) {
      mark = { ct: a.dataset.ct, tab: a.dataset.tab || '', sheet: a.dataset.sheet || '' };
    }
    host.innerHTML = markup;
    if (!mark) return;
    var sel = '[data-ct="' + mark.ct + '"]' +
      (mark.tab ? '[data-tab="' + mark.tab + '"]' : '') +
      (mark.sheet ? '[data-sheet="' + mark.sheet + '"]' : '');
    var back = host.querySelector(sel) || host.querySelector('[data-ct="' + mark.ct + '"]') ||
               host.querySelector('button:not([disabled])');
    if (back && back.getClientRects().length) {
      try { back.focus({ preventScroll: true }); } catch (e) { /* detached */ }
    }
  }

  C.headerVariant = function (s) {
    s = s || App.state;
    if (_headerOverride) return _headerOverride.variant;
    if (s.selected.id) return 'nav';
    if (s.sheet.state === 'collapsed') return 'entry';
    return 'tabs';
  };
  /** setHeader(variant, data) — force a header variant; setHeader(null) clears. */
  C.setHeader = function (variant, data) {
    _headerOverride = variant ? { variant: variant, data: data || null } : null;
    App.requestRender('containers');
  };

  /** the three live counts in every header variant: M, Saved, filter badge. */
  function counts(s) {
    var M = 0;
    try { M = D.M(s).length; } catch (e) { M = 0; }
    // Saved is every starred ref — restaurants, pins and landmarks alike
    // (M-106): the same number __favCount() reports and the Saved tab lists.
    var fav = s.user && s.user.fav ? s.user.fav.size : 0;
    var badge = 0;
    try { badge = (window.Filters && window.Filters.badge) ? window.Filters.badge() : D.summaryCount(s.filters, null); }
    catch (e) { try { badge = D.summaryCount(s.filters, null); } catch (e2) { badge = 0; } }
    return { M: M, fav: fav, badge: badge };
  }

  C.render = function (s, changed) {
    var mode = s.layout.mode;
    var narrow = mode === 'narrow';

    placeRoots(s);

    // --- visibility / inertness of the whole panel layer ------------------
    els.topbar.hidden = narrow;
    var sheetHidden = !narrow || !!s.search.active;
    setInert(els.sheet, sheetHidden);
    setInert(els['narrow-top'], !narrow);
    setInert(els.topbar, narrow);
    setInert(els['col-left'], narrow);
    setInert(els['col-detail'], narrow || !s.columns.detailOpen || s.columns.detailPaused);

    els.sheet.setAttribute('aria-label', t('面板'));
    els['col-left'].setAttribute('aria-label', t('结果与筛选'));
    els['col-detail'].setAttribute('aria-label', t('餐厅详情'));
    els['sheet-handle'].setAttribute('aria-label', t('拖动调整面板高度'));

    var n = counts(s);

    if (narrow) renderSheet(s, n);
    else renderColumns(s, n);

    if (_rescue) rescueFocus(3);
  };

  function setInert(el, on) {
    if (!el) return;
    if ('inert' in el) el.inert = !!on;
    else if (on) el.setAttribute('inert', ''); else el.removeAttribute('inert');
  }

  /* ---------------------------------------------------------------- narrow */
  function renderSheet(s, n) {
    var variant = C.headerVariant(s);
    els.sheet.toggleAttribute('data-compact', !!s.layout.severe);

    var st = ctx.layout.sheetTarget(s.sheet.state, s);
    els.sheet.toggleAttribute('data-full', !!st.full);

    if (variant === 'entry') html('sheet-head', headEntry(s, n));
    else if (variant === 'tabs') html('sheet-head', headTabs(s, n));
    else html('sheet-head', headNav(s));

    if (variant === 'tabs') fitTabs(els['sheet-head']);
    else if (variant === 'entry') fitEntry(els['sheet-head']);

    // M1 / M1b: hold scrollTop + focus restores until the height transition ends
    var h = s.sheet.height;
    if (_lastSheetH !== null && _lastSheetH !== h && !s.sheet.dragging) {
      ctx.motion.geometryBusy(ctx.motion.afterTransition(els.sheet, { prop: 'height', timeout: ctx.motion.sheet + 60 })
        .then(function () {
          if (_dragPending) { App.emit('sheet:snapped', { state: _dragPending, reason: 'drag' }); _dragPending = null; }
        }));
    } else if (_dragPending) { _dragPending = null; }
    _lastSheetH = h;
    _lastCols = null;
  }

  function segment(key, label, count, iconName, badgeStyle, always) {
    // A zero Saved list or a zero filter badge has nothing to say, but 结果 0 is the
    // answer to the question the user just asked — show it.
    var showN = always || !!count;
    var inner = (iconName ? ctx.icon(iconName, { cls: 'ic-sm' }) : '') +
      '<span class="ct-seg-label">' + util.esc(label) + '</span>' +
      (showN ? (badgeStyle === 'badge'
        ? '<span class="count-badge num">' + util.fmtCount(count || 0) + '</span>'
        : '<span class="count-text num">' + util.fmtCount(count || 0) + '</span>') : '');
    return '<button class="ct-seg" data-ct="tab" data-tab="' + key + '" aria-label="' +
      util.esc(t('{label}，共 {n}', { label: label, n: util.fmtCount(count || 0) })) + '">' + inner + '</button>';
  }

  /* variant A — collapsed entry bar: 结果 N | ♡ 收藏 | 筛选 n | ^ */
  function headEntry(s, n) {
    return '<div class="ct-entry"><div class="ct-segs">' +
      segment('results', t('结果'), n.M, null, 'text', true) +
      '<span class="ct-sep" aria-hidden="true"></span>' +
      segment('saved', t('收藏'), n.fav, 'heart', 'text') +
      '<span class="ct-sep" aria-hidden="true"></span>' +
      segment('filters', t('筛选'), n.badge, 'sliders', 'badge') +
      '</div>' +
      '<button class="ct-expand" data-ct="sheet" data-sheet="' + (s.sheet.tab === 'filters' ? 'filter' : 'browse') + '" ' +
      'aria-label="' + util.esc(t('展开面板')) + '" aria-expanded="false">' + ctx.icon('up') + '</button>' +
      '</div>';
  }

  /* variant B — browse / filter tabs */
  function headTabs(s, n) {
    var expanded = s.sheet.state === 'expanded' || s.sheet.state === 'full';
    var restore = s.sheet.tab === 'filters' ? 'filter' : 'browse';
    return '<div class="ct-tabs">' + tabsMarkup(s, n) +
      '<div class="ct-head-right">' +
      '<button class="icon-btn icon-btn-secondary" data-ct="sheet" data-sheet="' + (expanded ? restore : 'expanded') + '" ' +
      'aria-label="' + util.esc(t(expanded ? '收起面板' : '展开面板')) + '" aria-expanded="' + expanded + '">' +
      ctx.icon(expanded ? 'collapse' : 'expand') + '</button>' +
      '<button class="ct-pill" data-ct="sheet" data-sheet="collapsed" aria-label="' + util.esc(t('收起到地图')) + '">' +
      ctx.icon('chevronDown', { cls: 'ic-sm' }) + '<span class="ct-pill-text">' + util.esc(t('收起')) + '</span></button>' +
      '</div></div>';
  }

  /* variant C — detail navigation row (VIS-03 quiet row)
     The detail tool buttons (⋯ / expand / close) ride the restaurant NAME row that
     detail.js renders, not a strip of their own. What is left here is the
     source-return link, and with no source there is no row at all. The buttons
     still carry data-ct, so this host's delegation keeps handling them. */
  function headNav(s) {
    var label = null;
    try { label = (window.Detail && window.Detail.backLabel) ? window.Detail.backLabel(s) : null; } catch (e) { label = null; }
    if (!label) return '';
    return '<div class="ct-nav">' +
      '<button class="ct-back" data-ct="back">' + ctx.icon('back', { cls: 'ic-sm' }) + '<span>' + util.esc(t(label)) + '</span></button>' +
      '</div>';
  }

  /** detailTools(state) — the ⋯ / expand / close cluster detail.js puts in its title row. */
  C.detailTools = function (s) {
    s = s || App.state;
    var expanded = s.sheet.state === 'expanded' || s.sheet.state === 'full';
    return '<button class="icon-btn icon-btn-secondary" data-ct="sheet" data-sheet="' + (expanded ? 'detail' : 'expanded') + '" ' +
      'aria-label="' + util.esc(t(expanded ? '收起面板' : '展开面板')) + '" aria-expanded="' + expanded + '">' +
      ctx.icon(expanded ? 'collapse' : 'expand') + '</button>' +
      '<button class="icon-btn" data-ct="close-detail" aria-label="' + util.esc(t('关闭')) + '">' + ctx.icon('x') + '</button>';
  };

  function tabsMarkup(s, n) {
    var tab = s.sheet.tab;
    var one = function (key, label, count, badge, always) {
      var sel = tab === key;
      var showN = always || !!count;
      return '<button class="tab" role="tab" data-ct="tab" data-tab="' + key + '" aria-selected="' + sel + '">' +
        '<span class="ct-tab-label">' + util.esc(label) + '</span>' +
        (showN ? (badge ? '<span class="count-badge num">' + util.fmtCount(count || 0) + '</span>'
          : '<span class="count-text num">' + util.fmtCount(count || 0) + '</span>') : '') + '</button>';
    };
    return '<div class="tabs" role="tablist" aria-label="' + util.esc(t('结果与筛选')) + '">' +
      one('results', t('结果'), n.M, false, true) +
      one('saved', t('收藏'), n.fav, false) +
      one('filters', t('筛选'), n.badge, false) +
      '<span class="tab-ink" aria-hidden="true"></span></div>';
  }

  /**
   * fitRow — §14: a tab label is never cut in half. First tighten the row
   * (smaller paddings, the collapse pill drops its word but keeps its name);
   * if the strip still does not fit, it scrolls sideways with the selected
   * tab kept in view. Never shrinks the type.
   */
  function fitRow(wrap, strip) {
    if (!wrap || !strip) return false;
    if (wrap.hasAttribute('data-tight')) wrap.removeAttribute('data-tight');
    if (strip.scrollWidth > strip.clientWidth + 1) wrap.setAttribute('data-tight', '');
    var scrolls = strip.scrollWidth > strip.clientWidth + 1;
    wrap.toggleAttribute('data-scroll', scrolls);
    return scrolls;
  }

  /** the collapsed entry bar drops its decorative icons before it scrolls. */
  function fitEntry(scope) {
    var wrap = scope.querySelector('.ct-entry');
    if (wrap) fitRow(wrap, wrap.querySelector('.ct-segs'));
  }

  /**
   * fitTopbar() — LAY-04 order for the mid/wide bar: the brand gives way before
   * the search field, and the bar scrolls sideways before anything is crushed.
   * Measured, not breakpointed, because the pressure comes from the text scale
   * (200% / JA) as much as from the width.
   */
  function fitTopbar() {
    var bar = els.topbar;
    if (!bar || bar.hidden) return;
    var field = bar.querySelector('.ov-topfield');
    var brand = els['topbar-brand'];
    function over() {
      if (bar.scrollWidth > bar.clientWidth + 1) return true;
      // the brand clips its own text before the bar overflows (it is
      // overflow: hidden), so "Japan Foodm…" has to count as pressure too
      if (brand && brand.scrollWidth > brand.clientWidth + 1) return true;
      if (!field) return false;
      var w = field.getBoundingClientRect().width;
      // 14em is the CSS floor; under it the placeholder starts setting vertically
      return w > 0 && w < parseFloat(getComputedStyle(bar).fontSize) * 14 - 1;
    }
    bar.removeAttribute('data-tight'); bar.removeAttribute('data-tight2');
    if (over()) bar.setAttribute('data-tight', '');
    if (over()) bar.setAttribute('data-tight2', '');
    bar.toggleAttribute('data-scroll', bar.scrollWidth > bar.clientWidth + 1);
    // LAY-04: the row grows for the text rather than clipping it. Measure the
    // tallest child (at 200% the search field alone is ~94px) and hand it to
    // core, which feeds --topbar-h, the column tops and the map rect.
    var need = 0;
    Array.prototype.forEach.call(bar.children, function (c) {
      var h = c.scrollHeight || c.getBoundingClientRect().height;
      if (h > need) need = h;
    });
    ctx.layout.setTopbarH(need > 0 ? need + 12 : 0);
  }

  function fitTabs(scope) {
    var wrap = scope.querySelector('.ct-tabs, .ct-col-head');
    if (!wrap) return;
    var strip = wrap.querySelector('.tabs');
    if (!strip) return;
    var scrolls = fitRow(wrap, strip);
    var sel = strip.querySelector('.tab[aria-selected="true"]');
    if (sel && scrolls) {
      var l = sel.offsetLeft, r = l + sel.offsetWidth;
      if (l < strip.scrollLeft) strip.scrollLeft = l;
      else if (r > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = r - strip.clientWidth;
    }
    moveInk(scope);
  }

  /** M11: slide the underline to the selected tab. */
  function moveInk(scope) {
    var ink = scope.querySelector('.tab-ink');
    var sel = scope.querySelector('.tab[aria-selected="true"]');
    if (!ink) return;
    if (!sel) { ink.style.width = '0px'; return; }
    ink.style.width = sel.offsetWidth + 'px';
    ink.style.transform = 'translateX(' + sel.offsetLeft + 'px)';
  }

  /* ------------------------------------------------------------ mid / wide */
  function renderColumns(s, n) {
    var cols = ctx.layout.columns(s);
    var mode = s.layout.mode;

    els['col-left'].toggleAttribute('data-rail', !cols.leftVisible);
    els['col-detail'].toggleAttribute('data-closed', cols.detailW === 0);

    html('topbar-brand', brandMarkup(mode));
    html('col-left-head', cols.leftVisible ? colHead(s, n) : '');
    html('col-left-rail', cols.leftVisible ? '' : railMarkup(s, n));
    html('col-detail-head', cols.detailW > 0 ? detailHead(s) : '');
    if (cols.leftVisible) fitTabs(els['col-left-head']);
    fitTopbar();

    var key = cols.leftW + ':' + cols.detailW + ':' + mode;
    if (_lastCols !== null && _lastCols !== key) {
      ctx.motion.geometryBusy(ctx.motion.afterTransition(els['col-detail'], { prop: 'width', timeout: ctx.motion.standard + 60 }));
      App.emit('columns:changed', {
        mode: mode, leftW: cols.leftW, detailW: cols.detailW,
        rail: !cols.leftVisible, autoCollapsed: cols.autoCollapsed, detailPaused: cols.detailPaused
      });
    }
    _lastCols = key;
    _lastSheetH = null;
  }

  function brandMarkup(mode) {
    return '<span class="ct-logo" aria-hidden="true">' + ctx.emoji.img('🍜', 22) + '</span>' +
      '<span class="ct-brand-text"><span class="ct-brand-name">' + util.esc(t('Japan Foodmap')) + '</span>' +
      (mode === 'wide' ? '<span class="ct-brand-tag">' + util.esc(t('探索日本的美食地图')) + '</span>' : '') + '</span>';
  }

  function colHead(s, n) {
    return '<div class="ct-col-head">' + tabsMarkup(s, n) +
      '<button class="icon-btn icon-btn-secondary" data-ct="collapse-left" aria-label="' + util.esc(t('收起左栏')) + '">' +
      ctx.icon('chevronLeft') + '</button></div>';
  }

  function railMarkup(s, n) {
    var item = function (key, iconName, label, count) {
      return '<button class="ct-rail-btn" role="tab" data-ct="tab" data-tab="' + key + '" ' +
        'aria-selected="' + (s.sheet.tab === key) + '" aria-label="' +
        util.esc(t('{label}，共 {n}', { label: label, n: util.fmtCount(count || 0) })) + '">' +
        ctx.icon(iconName) + '<span>' + util.esc(label) + '</span>' +
        (count ? '<span class="ct-rail-count num">' + util.fmtCount(count) + '</span>' : '') + '</button>';
    };
    return '<div class="ct-rail-tabs" role="tablist" aria-label="' + util.esc(t('结果与筛选')) + '">' +
      item('results', 'list', t('结果'), n.M) +
      item('saved', 'heart', t('收藏'), n.fav) +
      item('filters', 'sliders', t('筛选'), n.badge) +
      '</div><span class="ct-rail-spacer"></span>' +
      '<button class="ct-rail-btn ct-rail-expand" data-ct="expand-left" aria-label="' + util.esc(t('展开左栏')) + '">' +
      ctx.icon('chevronRight') + '<span>' + util.esc(t('展开')) + '</span></button>' +
      '<button class="ct-rail-btn" data-ct="account" aria-label="' + util.esc(t('账户与数据')) + '" aria-haspopup="dialog" aria-expanded="' + (s.overlay.kind === 'account') + '">' +
      ctx.icon('gear') + '<span>' + util.esc(t('设置')) + '</span></button>';
  }

  function detailHead(s) {
    var mode = s.layout.mode;
    var label = null;
    try { label = (window.Detail && window.Detail.backLabel) ? window.Detail.backLabel(s) : null; } catch (e) { label = null; }
    var back = (mode === 'mid' && label)
      ? '<button class="ct-back" data-ct="back">' + ctx.icon('back', { cls: 'ic-sm' }) + '<span>' + util.esc(t(label)) + '</span></button>'
      : '';
    var pager = '';
    if (mode === 'wide') {
      var ids = candidateIds(s), i = ids.indexOf(s.selected.id);
      if (i >= 0 && ids.length > 1) {
        pager = '<span class="ct-pager">' +
          '<button class="icon-btn icon-btn-secondary" data-ct="step" data-step="-1" aria-label="' + util.esc(t('上一家')) + '"' + (i === 0 ? ' disabled' : '') + '>' + ctx.icon('chevronLeft') + '</button>' +
          '<span class="ct-pager-label">' + util.esc(t('第 {k} / {n} 家', { k: util.fmtCount(i + 1), n: util.fmtCount(ids.length) })) + '</span>' +
          '<button class="icon-btn icon-btn-secondary" data-ct="step" data-step="1" aria-label="' + util.esc(t('下一家')) + '"' + (i === ids.length - 1 ? ' disabled' : '') + '>' + ctx.icon('chevronRight') + '</button>' +
          '</span>';
      }
    }
    return '<div class="ct-detail-head">' + back + pager +
      '<button class="icon-btn ct-close" data-ct="close-detail" aria-label="' + util.esc(t('关闭')) + '">' + ctx.icon('x') + '</button></div>';
  }

  /* ======================================================================
     insets — the narrow search chrome obstructs the map (MAP-02 / LAY-01).
     overlays owns that rect, so containers deliberately publishes none: two
     owners would be max()-ed together and neither could cap the other. What
     containers owns is the panel side of the same budget.
     ==================================================================== */
  C.mapBand = function (s) {
    s = s || App.state;
    if (s.layout.mode !== 'narrow') return s.layout.mapRect.h;
    return Math.max(0, s.layout.H - (s.sheet.height || 0));
  };

  /* ======================================================================
     LAYER-03 fallback 1 — borrow the panel's height for an anchored layer.
     Does not touch the user's preference and keeps the semantic stop.
     ==================================================================== */
  /** After a detail closes, the keyboard caret must land on something the
   *  reader can see. The list module returns it to the source row when there
   *  was one; a card opened from search or from a marker has no source row, and
   *  the caret was left on <body>. Put it on the panel's own header. */
  var _rescue = false, _rescueTimer = null, _rescueAt = 0, _rescueRef = null;
  function rescueFocus(tries) {
    tries = tries || 0;
    var run = function () {
      var a = document.activeElement;
      var lost = !a || a === document.body ||
                 (a.closest && (a.closest('#detail-root') || a.closest('#detail-foot') ||
                                a.closest('#parking') || a.closest('[inert]')));
      // Deliberately do NOT clear _rescue here: the caret can be knocked to
      // <body> again a few frames later (a header rewrite, a node going
      // inert). The window is closed by its timer, not by one success.
      if (!lost) return;
      // The list module returns the caret to the source row, and it waits for
      // the panel geometry to settle first. Stepping in before it has had its
      // chance would put the caret on the header instead of on the row the
      // reader came from, which is worse than doing nothing. Hold off for a
      // grace period measured from the close, then act only if it is STILL
      // lost — which is the case for a card opened from search or a marker,
      // where there is no source row to go back to.
      if (Date.now() - _rescueAt < 420) { setTimeout(function () { rescueFocus(3); }, 60); return; }
      // Still waiting on the list to return the caret to its source row? Give
      // it the grace, then put the caret on that row ourselves — the row is
      // where NAV-02 promises it, and a caret on <body> is never acceptable.
      if (_rescueRef) {
        var srcRow = document.querySelector('#list-root .ls-row[data-id="' +
          _rescueRef.replace(/["\\]/g, '\\$&') + '"]');
        if (srcRow) {
          if (Date.now() - _rescueAt < 900) { setTimeout(function () { rescueFocus(3); }, 80); return; }
          var open = srcRow.querySelector('.ls-open') || srcRow;
          if (open.getClientRects().length) {
            try { open.focus({ preventScroll: true }); } catch (e) { /* detached */ }
            if (document.activeElement === open) return;
          }
        }
      }
      var hosts = App.state.layout.mode === 'narrow'
        ? ['sheet-head', 'sheet-body', 'narrow-top']
        : ['col-left-head', 'col-left-rail', 'col-left-body', 'topbar-right', 'topbar-search', 'topbar-brand'];
      var FOCUSABLE = 'button:not([disabled]), a[href], select, input:not([type="hidden"]), [tabindex]:not([tabindex="-1"])';
      for (var i = 0; i < hosts.length; i++) {
        var host = els[hosts[i]];
        if (!host || !host.getClientRects().length) continue;
        var cands = host.querySelectorAll(FOCUSABLE);
        for (var j = 0; j < cands.length; j++) {
          if (!cands[j].getClientRects().length || cands[j].closest('[inert]')) continue;
          try { cands[j].focus({ preventScroll: true }); } catch (e) { continue; }
          if (document.activeElement === cands[j]) return;
        }
      }
      if (tries < 20) util.raf(function () { rescueFocus(tries + 1); });
    };
    if (tries === 0) ctx.motion.afterGeometry(run); else run();
  }

  C.temporarilyCollapse = function () {
    var s = App.state;
    if (s.layout.mode !== 'narrow' || s.sheet.state === 'collapsed') return false;
    _tempCollapsed = s.sheet.state;
    App.set({ sheet: { state: 'collapsed' } });
    return true;
  };
  C.restore = function () {
    if (!_tempCollapsed) return false;
    var st = _tempCollapsed; _tempCollapsed = null;
    if (App.state.layout.mode !== 'narrow') return false;
    App.set({ sheet: { state: st } });
    return true;
  };

  C.destroy = function () {
    ['sheet-head', 'col-left-head', 'col-left-rail', 'col-detail-head', 'topbar-brand'].forEach(function (id) {
      if (els[id]) els[id].innerHTML = '';
    });
    _html = {};
  };

  window.App.registerModule('containers', C);
})();
