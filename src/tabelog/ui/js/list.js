/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/list.js
   Owns: result / saved rows (narrow photo-fade rows, column emoji directory),
   grouping, the sort trigger, multi-select + bulk bar, direct save / hide, the
   single undo toast (FEEDBACK-01) rendered into #toast-root, the empty result
   card, the out-of-view card and the "其中 N 家可网订" footer.

   Ported from ui-captures-2026-09-10/redesign/demo-4.0/js/list.js. What
   changed for production:
     · the rows are the real corpus (10,250 rows, up to ~8k after the default
       filters), so the list is WINDOWED: only the items intersecting the
       scroller viewport (plus an overscan band) are in the DOM, and the two
       pad rows carry the rest of the height. Row heights are MEASURED at
       runtime per item — the 3.2.x list assumed a constant 60px row and the
       scrollbar drifted as soon as a name wrapped to two lines;
     · photos come from the popups payload (slot 7, up to 3 URLs) which is a
       multi-MB lazy fetch — the first one is kicked off from idle time after
       the list has painted, never on the boot path, and a row keeps its exact
       height whether the photo arrives, fails or never loads;
     · the Saved tab lists every starred ref, not only restaurants: pins and
       landmarks (M-106's four names) get their own row shape, and a ref whose
       row has left the corpus is shown rather than silently dropped;
     · every mutation goes through App.act (toggleFav / setBlack / batch /
       addToList…), so the sync engine, the KV blob and the undo semantics are
       the production ones;
     · copy lives in ui/i18n/ui-strings.json — no inline I18N.register here.

   Public API (CONTRACT.md §list):
     ListMod.rowsFor(state)   → L  (ordered refs for the current tab)
     ListMod.groupsFor(state) → [{key, label, emoji, ids, items, list}]
     ListMod.anchor()         → {id, offset, scrollTop} of the first visible row
     ListMod.restore(anchor)  → restore scroll + focus after geometry settles
     ListMod.visibleIds()     → V (rows intersecting the scroller viewport)
   Additions:
     ListMod.rowMenuItems(state, ref) → [{key, icon, label, danger, run}]
     ListMod.sortItems()      → [{key, label, current}]
     ListMod.undoState()      → {toastId, canUndo, rev}
     ListMod.listText(state)  → plain-text dump of the Saved tab
     ListMod.metrics()        → {items, rendered, avg} (virtualiser, for tests)
   Events: 'list:restored' {reason:'anchor'|'fallback'|'empty', anchor}
   ========================================================================== */
(function () {
  'use strict';

  var Lm = window.ListMod = {};
  var ctx, App, D, t, u, root, foot, toastRoot;

  /* module-local bookkeeping -------------------------------------------- */
  var rev = 0;                 // MULTI-03 business-data revision (bumped on user:changed)
  var lastSig = '';            // render signature (idempotent render)
  var lastIds = '';            // row-id signature, to know when scrollTop may be kept
  var lastMultiActive = false;
  var multiEnterTimer = 0;
  var pendingRestore = null;   // NAV-02 anchor waiting for geometry to settle
  var focusAfter = null;
  var anchorOrder = [];        // L at the time the anchor was taken (NAV-02 fallback)
  var oovShownThisLoad = false;
  var lastMVCount = null;
  var status = { text: null, params: null, timer: null };
  var toastTimer = null, toastTrack = { id: null, rev: -1, remaining: 0, paused: false };

  /* virtualiser state ---------------------------------------------------- */
  var items = [];              // flat render list: {t:'g'|'r', key, …}
  var indexOf = Object.create(null);   // ref → item index
  var itemH = Object.create(null);     // geometry key → measured px
  var avgH = Object.create(null);      // estimate bucket → {sum, n}
  var vp = null;               // the <ul> the window paints into
  var curScroller = null, offScroll = null;
  var winStart = -1, winEnd = -1;
  var scrollRaf = false;
  var OVERSCAN = 700;          // px of extra rows kept above and below the viewport

  /* photos: popups slot 7, lazily fetched ------------------------------- */
  var photoCache = Object.create(null);   // ref → url | '' (known-missing)
  var popupsKicked = false, popupsReady = false;

  /* ======================================================================
     init
     ==================================================================== */
  Lm.init = function (c) {
    ctx = c; App = c.App; D = c.Data; t = c.t; u = c.util;
    root = c.roots.listRoot; foot = c.roots.listFoot; toastRoot = c.roots.toastRoot;
    root.className = 'ls-root';
    foot.className = 'ls-foot-root';
    toastRoot.className = 'ls-toast-root';

    bind();

    App.on('user:changed', function () { rev += 1; });
    App.on('nav:restore', function (prev) {
      if (!prev || (prev.origin !== 'results' && prev.origin !== 'saved')) return;
      pendingRestore = { anchor: prev.anchor, id: prev.id };
      App.requestRender('list');
    });
    // NAV-01: an explicit x is not a navigation - it does not re-select the
    // restaurant, and at narrow it drops the sheet to the map - but the rule keeps
    // the source id "for position restore only", so the reader keeps their place in
    // the list. Back/Esc came through nav:restore and was already covered; x only
    // emitted detail:close, which nobody consumed, so every width reset to the top.
    App.on('detail:close', function (p) {
      // reason 'back' already emits nav:restore right after this, and that one
      // carries the trigger id so NAV-02 can also pull the row it came from into
      // view. Taking both would re-restore without a trigger and undo that.
      if (!p || p.reason !== 'x') return;
      var prev = p.selected;
      if (!prev || !prev.anchor || (prev.origin !== 'results' && prev.origin !== 'saved')) return;
      // Park it unconditionally: at narrow the panel is collapsing right now and
      // the list only comes back when the reader reopens it, which restoreAfterPark
      // picks up on the unlaid → laid transition.
      closeAnchor = prev.anchor;
      wantRestore = true;              // consumed as soon as the list is laid out again
      if (listIsLaidOut()) {
        pendingRestore = { anchor: prev.anchor, id: prev.id };
        wantRestore = false; closeAnchor = null;
        App.requestRender('list');
      } else {
        // At mid the left column is mid-animation here and NOTHING re-renders the
        // list once it finishes, so the intent would sit unconsumed. Pump it for a
        // bounded number of frames; if it runs out (narrow, where the reader reopens
        // the panel later) the flag is left standing for the next render.
        pumpParkedRestore(0);
      }
    });
    App.on('map:moveend', onMapMoved);
    App.on('map:mv', onMapMoved);
    App.on('toast:dismissed', function () { stopToastTimer(); toastTrack.id = null; });
    // LAY-02: at mid, opening a detail auto-collapses the left column, so the
    // list's viewport goes to zero width and comes back rebuilt. Try to put
    // the reader back where they were. (Known gap: at mid this still lands at
    // the top of the list — see audit_outputs/4.0.0-impl/INTEGRATION.md.)
    App.on('layout:settled', function () { paintWindow(true); restoreAfterPark(); });
    App.on('containers:restored', function () { paintWindow(true); restoreAfterPark(); });
    bindShortcuts();
  };

  /* ----------------------------------------------------------------------
     J / K / F / X — the four row shortcuts the 快捷键 help panel documents.
     J / K walk the current ordered list (results or Saved) and open the row;
     F stars the selected restaurant, X puts it on the Hidden list. All four
     are no-ops while typing, while an overlay is open, and for a row that is
     not a restaurant (a pin or a landmark has no fav / hidden state).
     ------------------------------------------------------------------- */
  function bindShortcuts() {
    document.addEventListener('keydown', function (e) {
      if (e.isComposing || e.keyCode === 229 || e.metaKey || e.ctrlKey || e.altKey) return;
      var tg = e.target, tag = (tg && tg.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || (tg && tg.isContentEditable)) return;
      var s = App.state;
      if (s.overlay.kind || s.search.active) return;
      var k = e.key;
      if (k === 'j' || k === 'J' || k === 'k' || k === 'K') {
        var ids = Lm.rowsFor(s);
        if (!ids.length) return;
        var cur = ids.indexOf(s.selected.id);
        var next = (k === 'j' || k === 'J')
          ? (cur < 0 ? 0 : Math.min(ids.length - 1, cur + 1))
          : (cur < 0 ? 0 : Math.max(0, cur - 1));
        e.preventDefault();
        ctx.act.openDetail(ids[next], s.sheet.tab === 'saved' ? 'saved' : 'results');
        return;
      }
      if ((k === 'f' || k === 'F' || k === 'x' || k === 'X') && s.selected.id && D.byId(s.selected.id)) {
        e.preventDefault();
        var id = s.selected.id;
        if (k === 'f' || k === 'F') {
          var added = ctx.act.toggleFav(id);
          announce((added ? t('已收藏') : t('已取消收藏')) + ' ' + (D.byId(id).name || ''));
        } else {
          var on = !s.user.black.has(id);
          if (ctx.act.setBlack(id, on)) announce((on ? t('已弃用') : t('已恢复显示')) + ' ' + (D.byId(id).name || ''));
        }
      }
    }, false);
  }
  function announce(text) {
    var el = document.getElementById('sr-live');
    if (el) { el.textContent = ''; setTimeout(function () { el.textContent = text; }, 30); }
  }

  function onMapMoved() {
    var s = App.state;
    var n = computeMV(D.M(s));
    if (n !== lastMVCount) { lastMVCount = n; App.requestRender('list'); }
  }

  /* MV is recomputed on every map move over the whole match set, so it is
     memoised on (bounds, filter identity) — the array identity from the
     adapter's applyFilters cache is enough to know the set did not change. */
  var _mv = { ids: null, key: '', n: null };
  function computeMV(ids) {
    try {
      if (!window.MapMod || typeof window.MapMod.bounds !== 'function') return null;
      var b = window.MapMod.bounds();
      if (!b || typeof b.south !== 'number') return null;
      var key = b.south.toFixed(4) + ',' + b.west.toFixed(4) + ',' + b.north.toFixed(4) + ',' + b.east.toFixed(4);
      if (_mv.ids === ids && _mv.key === key) return _mv.n;
      var n = D.MV(ids, b).length;
      _mv = { ids: ids, key: key, n: n };
      return n;
    } catch (e) { return null; }
  }

  function sortCtx(s) {
    var fix = s.nearby && s.nearby.fix;
    return { from: (fix && typeof fix.lat === 'number') ? [fix.lat, fix.lon] : null };
  }

  /* Sorting 8k rows is not free, and groupsFor() is called from render, from
     rowsFor() and from the containers pager. Memoise on the match set's
     identity (the adapter hands back the same array while the filter key is
     unchanged) plus the sort key and origin. */
  var _ord = { M: null, key: '', ids: null };
  var _mEpoch = 0, _mRef = null;
  function orderedResults(s) {
    var M = D.M(s);
    if (M !== _mRef) { _mRef = M; _mEpoch += 1; }
    var c = sortCtx(s);
    var key = s.sort + '|' + (c.from ? c.from[0].toFixed(3) + ',' + c.from[1].toFixed(3) : '');
    if (_ord.M === M && _ord.key === key && _ord.ids) return _ord.ids;
    var ids = D.sort(M, s.sort, c);
    _ord = { M: M, key: key, ids: ids };
    return ids;
  }

  /* ======================================================================
     public API
     ==================================================================== */
  /**
   * groupsFor(state) — results: one unlabelled group of restaurants; saved:
   * the production city / collection groups, which carry pins and landmarks
   * alongside restaurants (favBuildGroups). `items` keeps the kind so a row
   * can be drawn for each of them; `ids` stays the flat ref list.
   */
  Lm.groupsFor = function (s) {
    if (s.sheet.tab === 'saved') {
      var groups = [];
      try { groups = D.savedGroups(s) || []; } catch (e) { groups = []; }
      return groups.map(function (g) {
        var list = (g.items || []).map(function (it) {
          return { kind: it.kind, ref: it.ref, d: it.d || null, bm: it.bm || null };
        });
        return {
          key: g.key, label: g.label, emoji: g.emoji || null, ja: !!g.ja,
          listId: g.listId || null, unfiled: !!g.unfiled, list: g.list || null,
          ids: list.map(function (it) { return it.ref; }), items: list
        };
      });
    }
    var ordered = orderedResults(s);
    return [{
      key: 'results', label: null, emoji: null, ja: false, listId: null, unfiled: false, list: null,
      ids: ordered,
      items: ordered.map(function (id) { return { kind: 'rst', ref: id, d: D.byId(id), bm: null }; })
    }];
  };

  /** rowsFor(state) → L, the flat ordered ref list of the current tab. */
  Lm.rowsFor = function (s) {
    var out = [];
    Lm.groupsFor(s).forEach(function (g) { g.ids.forEach(function (id) { out.push(id); }); });
    return out;
  };

  function scroller() {
    return (window.Containers && Containers.scroller) ? Containers.scroller('list') : null;
  }

  /** anchor() — first rendered row whose bottom is inside the viewport (NAV-02). */
  Lm.anchor = function () {
    var sc = scroller();
    var rows = root.querySelectorAll('.ls-row[data-id]');
    if (!sc || !rows.length) return null;
    var vr = sc.getBoundingClientRect();
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i].getBoundingClientRect();
      if (r.bottom > vr.top + 0.5) {
        return { id: rows[i].getAttribute('data-id'), offset: r.top - vr.top, scrollTop: sc.scrollTop };
      }
    }
    return { id: rows[rows.length - 1].getAttribute('data-id'), offset: 0, scrollTop: sc.scrollTop };
  };

  /** visibleIds() — V: rows with a positive-area intersection with the viewport (MULTI-01). */
  Lm.visibleIds = function () {
    var sc = scroller(); if (!sc) return [];
    var vr = sc.getBoundingClientRect(), out = [];
    Array.prototype.forEach.call(root.querySelectorAll('.ls-row[data-id]'), function (el) {
      var r = el.getBoundingClientRect();
      if (Math.min(r.bottom, vr.bottom) - Math.max(r.top, vr.top) > 0) out.push(el.getAttribute('data-id'));
    });
    return out;
  };

  /**
   * restore(anchor, triggerId) — NAV-02. Runs after geometry settles. The
   * anchored row may be outside the painted window or gone from the list
   * entirely: the offset comes from the virtualiser's measured heights, and a
   * missing row falls back to the first successor in the old order, then the
   * nearest predecessor, then the top.
   */
  Lm.restore = function (anchor, triggerId) {
    ctx.motion.afterGeometry(function () { restoreNow(anchor, triggerId, 0); });
  };
  /** The panel can still be mid-transition when afterGeometry fires on a short
   *  screen: the viewport has no width yet, paintWindow() correctly refuses to
   *  measure, no row exists to focus, and the keyboard caret is left on <body>.
   *  Wait for the list to actually be laid out — bounded, so a genuinely empty
   *  list still ends the restore and emits list:restored. */
  /** holdFocus — keep the NAV-02 caret on its row for ~20 frames after the
   *  restore, against a late repaint. Gives up the moment the user moves
   *  focus somewhere real of their own accord. */
  function holdFocus(triggerId, n) {
    if (n > 20) return;
    ctx.util.raf(function () {
      var a = document.activeElement;
      if (a && a !== document.body && !root.contains(a)) return;   // the user moved on
      var row = triggerId ? rowEl(triggerId) : null;
      var btn = row && row.querySelector('.ls-open');
      if (btn && document.activeElement !== btn) {
        try { btn.focus({ preventScroll: true }); } catch (e) { /* detached */ }
      }
      holdFocus(triggerId, n + 1);
    });
  }

  /* --- keeping the place across a park (LAY-02 auto-collapse) ----------- */
  var parkedAnchor = null, wasLaidOut = false, wantRestore = false;
  // Separate from parkedAnchor on purpose: noteAnchor() rewrites parkedAnchor on
  // every scroll, and the list scrolls to the top while it is being rebuilt — that
  // clobbered the close anchor with a top-of-list one before it could be consumed.
  var closeAnchor = null;
  function noteAnchor() {
    if (!vp || !vp.isConnected || !vp.clientWidth) return;
    var a = null;
    try { a = Lm.anchor(); } catch (e) { a = null; }
    if (a && a.id) parkedAnchor = a;
  }
  function listIsLaidOut() {
    return !!(vp && vp.isConnected && vp.clientWidth >= 120 && scroller());
  }
  function restoreAfterPark() {
    var laid = listIsLaidOut();
    // Two ways in. The unlaid → laid EDGE covers the plain LAY-02 auto-collapse.
    // wantRestore is the explicit one, set when a detail closed: at mid the column
    // can be measured as laid-out on both sides of the close, so the edge never
    // fires and the reader lost their place — hold the intent until the list is
    // actually there instead of depending on catching a transition.
    var byEdge = laid && !wasLaidOut;
    if (laid && (byEdge || wantRestore) && parkedAnchor && App.state.sheet.tab !== 'filters') {
      var a = parkedAnchor;
      wantRestore = false;
      // the column is still animating open; restore once it has settled
      ctx.motion.afterGeometry(function () { Lm.restore(a, null); });
    }
    wasLaidOut = laid;
  }


  var _restoreH = -1;
  function restoreNow(anchor, triggerId, tries) {
    var sc = scroller();
    var laid = sc && vp && vp.isConnected && vp.clientWidth >= 120 && sc.clientHeight > 40;
    // The panel's HEIGHT is still animating when afterGeometry fires on a
    // detail -> browse return: the sheet shrinks from the detail stop to the
    // browse stop. Restoring against a viewport that is about to change puts
    // the anchor hundreds of px out (475x751 measured -410 against a wanted
    // -79). Wait for two consecutive frames at the same height.
    if (laid && sc.clientHeight !== _restoreH) {
      _restoreH = sc.clientHeight; laid = false;
    }
    if (!laid && tries < 60) { ctx.util.raf(function () { restoreNow(anchor, triggerId, tries + 1); }); return; }
    _restoreH = -1;
    (function () {
      if (!sc) return;
      var idx = (anchor && anchor.id) ? idxOf(anchor.id) : -1;
      var reason = 'anchor';
      if (idx < 0 && anchor && anchor.id) {
        var i = anchorOrder.indexOf(anchor.id), j;
        for (j = i + 1; j < anchorOrder.length && idx < 0; j++) idx = idxOf(anchorOrder[j]);
        for (j = i - 1; j >= 0 && idx < 0; j--) idx = idxOf(anchorOrder[j]);
        reason = idx >= 0 ? 'fallback' : 'empty';
      }
      if (idx >= 0) {
        var want = (anchor && anchor.offset) || 0;
        scrollToIndex(sc, idx, want);
        scrollToIndex(sc, idx, want);   // second pass: measured heights
        // Converge on the REAL position. Each paintWindow() measures the band
        // it just painted and can move the estimated offsets under us, so two
        // open-loop passes could still land hundreds of px out (475x751 was
        // 331px off). Close the loop against the row's actual rectangle.
        var id2 = (anchor && anchor.id) || (items[idx] && (items[idx].ref || items[idx].id));
        for (var pass = 0; pass < 6; pass++) {
          var el2 = id2 ? rowEl(id2) : null;
          if (!el2) {
            // The row is outside the painted band, which means the estimated
            // offsets are still off. Re-target from FRESH offsets and repaint
            // rather than giving up — breaking here is what left the restore
            // hundreds of px out once heights had been re-measured.
            var offs3 = offsets();
            sc.scrollTop = u.clamp(vpOffset(sc) + offs3[idx] - want, 0,
                                   Math.max(0, sc.scrollHeight - sc.clientHeight));
            paintWindow(true);
            continue;
          }
          var got = el2.getBoundingClientRect().top - sc.getBoundingClientRect().top;
          var off = got - want;
          if (Math.abs(off) <= 1) break;
          sc.scrollTop = u.clamp(sc.scrollTop + off, 0, Math.max(0, sc.scrollHeight - sc.clientHeight));
          paintWindow(true);
        }
      } else if (anchor && typeof anchor.scrollTop === 'number') {
        sc.scrollTop = u.clamp(anchor.scrollTop, 0, Math.max(0, sc.scrollHeight - sc.clientHeight));
        paintWindow(true);
      }
      // Safety net: whatever the anchor maths did, the row the user came FROM
      // must be on screen. At mid the left column is parked while the detail
      // column is open, so the list is rebuilt and re-measured on the way back
      // and the anchor can land far from where it was.
      if (triggerId) {
        var ti = idxOf(triggerId);
        if (ti >= 0) {
          var te = rowEl(triggerId);
          var vr0 = sc.getBoundingClientRect();
          var visible = te && te.getBoundingClientRect().bottom > vr0.top + 1
                           && te.getBoundingClientRect().top < vr0.bottom - 1;
          if (!visible) {
            scrollToIndex(sc, ti, Math.max(0, (sc.clientHeight - 120) / 3));
            scrollToIndex(sc, ti, Math.max(0, (sc.clientHeight - 120) / 3));
          }
        }
      }
      // focus the triggering row, else the list toolbar (NAV-02)
      var trig = triggerId ? rowEl(triggerId) : null;
      var btn = trig ? trig.querySelector('.ls-open') : null;
      if (btn) {
        btn.focus({ preventScroll: true });
        var br = btn.getBoundingClientRect(), vr2 = sc.getBoundingClientRect();
        if (br.bottom <= vr2.top || br.top >= vr2.bottom) sc.scrollTop += (br.top - vr2.top) - 12;
        // The panel is still finishing its height transition here, and anything
        // that re-inserts or repaints the row afterwards sends the caret to
        // <body>. Both known paths now carry focus across the move, but a slow
        // device can still land a late repaint: check once per frame for a
        // short window and put the caret back on the row the user came from.
        holdFocus(triggerId, 0);
      } else {
        var tools = root.querySelector('.ls-tools button, .ls-tools select');
        if (tools) tools.focus({ preventScroll: true });
      }
      App.emit('list:restored', { reason: reason, anchor: anchor });
    })();
  }

  function idxOf(ref) { var i = indexOf[ref]; return i === undefined ? -1 : i; }

  /**
   * scrollToIndex — paint first, then scroll. The pads only carry the list's
   * full height once a window has been painted, and setting scrollTop before
   * that clamps against a nearly empty scroller: coming back from a detail on
   * the phone landed at the top of the list every time.
   */
  function scrollToIndex(sc, idx, offset) {
    paintWindow(true);
    var offs = offsets();
    var want = vpOffset(sc) + offs[idx] - (offset || 0);
    sc.scrollTop = u.clamp(want, 0, Math.max(0, sc.scrollHeight - sc.clientHeight));
    paintWindow(true);
  }

  function rowEl(id) {
    if (!id) return null;
    return root.querySelector('.ls-row[data-id="' + cssEscape(id) + '"]');
  }
  function cssEscape(s) { return String(s).replace(/["\\]/g, '\\$&'); }

  /** rowMenuItems(state, ref) — the saved-row ⋯ menu (§9.3 wording is never merged). */
  Lm.rowMenuItems = function (s, ref) {
    var it = itemFor(ref);
    var kind = it ? it.kind : 'rst';
    var out = [];
    out.push({ key: 'addToList', icon: 'folderPlus', label: '加入收藏夹', danger: false,
      run: function () { ctx.act.openOverlay('memberPicker', { id: ref }); } });
    var lists = [];
    try { lists = D.listsOf(s.user.bookmarks, ref) || []; } catch (e) { lists = []; }
    if (lists.length) out.push({ key: 'removeFromList', icon: 'folder', label: '从此收藏夹移除', danger: false,
      run: function () { removeFromLists(ref, lists); } });
    out.push({ key: 'unsave', icon: 'heart', label: '取消收藏', danger: false, run: function () { unsave(ref); } });
    if (kind === 'rst') {
      out.push({ key: 'hide', icon: 'hide', label: '弃用这间店', danger: true, run: function () { hideOne(ref); } });
    } else if (kind === 'pin' && it && it.bm) {
      out.push({ key: 'removePin', icon: 'trash', label: '删除书签', danger: true,
        run: function () { ctx.act.removePin(it.bm); } });
    } else if (kind === 'sight' && it && it.bm) {
      out.push({ key: 'hideLandmark', icon: 'hide', label: '隐藏景点', danger: true,
        run: function () { ctx.act.hideLandmark(it.bm); } });
    }
    return out;
  };

  /** listMenuItems(state, listId) — the collection ⋯ menu on a Saved group
   *  head. Deleting a collection keeps its restaurants Saved (E2). */
  Lm.listMenuItems = function (s, listId) {
    var list = (D.lists() || []).filter(function (l) { return l.id === listId; })[0];
    if (!list) return [];
    return [
      { key: 'rename', icon: 'folder', label: '重命名收藏夹', danger: false,
        run: function () {
          ctx.act.openOverlay('listForm', { mode: 'edit', listId: listId,
            draft: { name: list.name || '', emoji: list.emoji || D.config.LIST_QUICK_EMOJI[0] } });
        } },
      { key: 'copy', icon: 'copy', label: '复制清单文本', danger: false,
        run: function () { copyList(listId); } },
      { key: 'delete', icon: 'trash', label: '删除收藏夹', danger: true,
        run: function () { ctx.act.deleteList(listId); } }
    ];
  };

  function itemFor(ref) {
    var i = idxOf(ref);
    if (i >= 0 && items[i] && items[i].t === 'r') return items[i];
    var d = D.byId(ref);
    if (d) return { kind: 'rst', ref: ref, d: d, bm: null };
    var lm = D.landmarkById(ref);
    if (lm) return { kind: 'sight', ref: ref, d: null, bm: lm };
    var bms = (App.state.user.bookmarks || []);
    for (var k = 0; k < bms.length; k++) {
      if (bms[k] && bms[k].id === ref && bms[k].category !== 'meta' && bms[k].category !== 'hidden') {
        return { kind: 'pin', ref: ref, d: null, bm: bms[k] };
      }
    }
    return null;
  }

  /** sortItems() — the five production sorts, current one flagged (LIST-02). */
  Lm.sortItems = function () {
    var cur = App.state.sort;
    return D.config.SORTS.map(function (o) { return { key: o.key, label: o.label, current: o.key === cur }; });
  };

  Lm.undoState = function () { return { toastId: toastTrack.id, canUndo: canUndo(App.state), rev: rev }; };

  Lm.metrics = function () {
    var n = 0, avg = {};
    for (var k in avgH) { n += avgH[k].n; avg[k] = Math.round(avgH[k].avg * 10) / 10; }
    offsets();
    return { items: items.length, rendered: root.querySelectorAll('.ls-row[data-id]').length,
             window: [winStart, winEnd], measured: n, avg: avg, total: totalH };
  };

  /** listText(state) — plain text dump of the Saved tab (copy button). */
  Lm.listText = function (s, listId) {
    var lines = [];
    Lm.groupsFor(s).forEach(function (g) {
      if (listId && g.listId !== listId) return;
      if (g.label) lines.push('# ' + g.label + ' (' + g.ids.length + ')');
      g.items.forEach(function (it) {
        if (it.kind === 'rst' && it.d) {
          var r = it.d, bits = [r.name];
          if (r.rating !== null && r.rating !== undefined) bits.push('★ ' + u.fmtRating(r.rating));
          bits.push(u.fmtPrice(r.bucket, { short: true }));
          if (r.st) bits.push(r.st);
          lines.push('- ' + bits.join(' · '));
          lines.push('  ' + r.id);
        } else if (it.bm) {
          lines.push('- ' + D.pinName(it.bm));
          if (typeof it.bm.lat === 'number') lines.push('  ' + it.bm.lat + ',' + it.bm.lon);
        } else {
          lines.push('- ' + it.ref);
        }
      });
      lines.push('');
    });
    return lines.join('\n').trim();
  };

  /* ======================================================================
     events
     ==================================================================== */
  function bind() {
    var del = u.delegate;
    del(root, 'click', '[data-open]', function (e, el) { onRowActivate(el.getAttribute('data-open')); });
    // The row heart opens the collection picker rather than saving straight away
    // (owner decision, 2026-09-12): every user has a 默认收藏夹 and their own
    // collections sit beside it, so choosing where it goes is the save action.
    // The picker itself is where an already-saved place is un-saved.
    del(root, 'click', '[data-fav]', function (e, el) {
      e.stopPropagation();
      ctx.act.openOverlay('memberPicker', { id: el.getAttribute('data-fav') });
    });
    del(root, 'click', '[data-hide]', function (e, el) { e.stopPropagation(); hideOne(el.getAttribute('data-hide'), el); });
    del(root, 'click', '[data-menu]', function (e, el) { e.stopPropagation(); ctx.act.openOverlay('more', { id: el.getAttribute('data-menu'), context: 'saved' }); });
    del(root, 'change', '[data-check]', function (e, el) { toggleCheck(el.getAttribute('data-check'), el.checked); });
    del(root, 'click', '[data-group]', function (e, el) { toggleGroup(el.getAttribute('data-group')); });
    del(root, 'click', '[data-focus-list]', function (e, el) { e.stopPropagation(); focusList(el.getAttribute('data-focus-list')); });
    del(root, 'click', '[data-group-menu]', function (e, el) {
      e.stopPropagation();
      ctx.act.openOverlay('more', { context: 'savedList', listId: el.getAttribute('data-group-menu') });
    });
    del(root, 'click', '[data-act]', function (e, el) { headAction(el.getAttribute('data-act'), el); });
    del(root, 'change', '[data-groupby]', function (e, el) { App.set({ saved: { groupBy: el.value, openGroups: null } }); });
    del(root, 'error', 'img.ls-img', function (e, el) {
      var box = el.parentElement; if (!box) return;
      box.classList.remove('skeleton');
      box.classList.add('is-empty'); el.remove();
      // Keep the source URL so a later visit can retry.
      box.innerHTML = (box.getAttribute('data-fallback') || '') + '<span class="ls-fade"></span>';
    }, true);
    // MOTION M14: stop the shimmer the moment the photo has decoded
    del(root, 'load', 'img.ls-img', function (e, el) {
      if (el.parentElement) el.parentElement.classList.remove('skeleton');
    }, true);
    del(foot, 'click', '[data-act]', function (e, el) { headAction(el.getAttribute('data-act'), el); });
    del(toastRoot, 'click', '[data-act]', function (e, el) { headAction(el.getAttribute('data-act'), el); });
    // FEEDBACK-01: hover / focus inside the toast pauses the countdown
    u.on(toastRoot, 'pointerenter', function () { toastTrack.paused = true; }, true);
    u.on(toastRoot, 'pointerleave', function () { toastTrack.paused = false; }, true);
    u.on(toastRoot, 'focusin', function () { toastTrack.paused = true; });
    u.on(toastRoot, 'focusout', function () { toastTrack.paused = false; });
  }

  function headAction(act, el) {
    var s = App.state;
    switch (act) {
      case 'sort': ctx.act.openOverlay('sortMenu', { from: 'list' }); break;
      case 'multi': App.set({ multi: { active: true, ids: new Set(), scope: s.sheet.tab === 'saved' ? 'saved' : 'results' } }); focusAfter = '[data-act="select-visible"]'; break;
      case 'cancel-multi': App.emit('multi:cancel', { reason: 'button' }); App.set({ multi: { active: false, ids: new Set() } }); focusAfter = '[data-act="multi"]'; break;
      case 'select-visible': App.set({ multi: { ids: unionVisible(s) } }); break;
      case 'clear-selection': App.set({ multi: { ids: new Set() } }); break;
      case 'bulk-fav': bulkFav(); break;
      case 'bulk-hide': bulkHide(); break;
      case 'new-list': ctx.act.openOverlay('listForm', { mode: 'create', draft: { name: '', emoji: D.config.LIST_QUICK_EMOJI[0] }, members: [] }); break;
      case 'copy-list': copyList(); break;
      case 'clear-focus': focusList(null); break;
      case 'to-filters': ctx.act.setTab('filters'); break;
      case 'reset-filters': ctx.act.resetFilters(); break;
      case 'bookable-only': ctx.act.applyFilters({ bookableOnly: true }); break;
      case 'bookable-all': ctx.act.applyFilters({ bookableOnly: false }); break;
      case 'oov-show': showAllResults(); break;
      case 'oov-close': App.set({ notices: { oov: { visible: false } } }); break;
      case 'oov-never': ctx.act.dismissOovForever(); App.set({ notices: { oov: { visible: false } } }); break;
      case 'undo': runUndo(); break;
      case 'toast-action': runToastAction(); break;
      case 'toast-dismiss': ctx.act.dismissToast(App.state.toast ? App.state.toast.id : null); break;
      default: break;
    }
  }

  function onRowActivate(id) {
    var s = App.state;
    if (s.multi.active) { toggleCheck(id, !s.multi.ids.has(id)); return; }   // LIST-01: multi selects, never opens
    anchorOrder = Lm.rowsFor(s);
    var a = Lm.anchor();
    var it = itemFor(id);
    if (it && it.kind !== 'rst') {           // a pin / landmark row centres the map on it
      App.set({ selected: { id: null, origin: null, anchor: null } }, { silent: true });
      try {
        if (it.bm && typeof it.bm.lat === 'number' && window.MapMod && window.MapMod.flyTo) {
          window.MapMod.flyTo([it.bm.lat, it.bm.lon], Math.max(15, s.mapView.zoom || 15));
        }
      } catch (e) { /* map not ready */ }
      if (s.layout.mode === 'narrow') ctx.act.setSheet('collapsed');
      return;
    }
    if (!it) return;
    ctx.act.openDetail(id, s.sheet.tab === 'saved' ? 'saved' : 'results', a);
  }

  function toggleCheck(id, on) {
    var s = App.state;
    App.set({ multi: { ids: on ? u.setWith(s.multi.ids, id) : u.setWithout(s.multi.ids, id) } });
  }

  function unionVisible(s) {
    var next = new Set(s.multi.ids);
    Lm.visibleIds().forEach(function (id) { next.add(id); });   // C := C ∪ V, never a toggle
    return next;
  }

  function toggleGroup(key) {
    var s = App.state;
    var keys = Lm.groupsFor(s).map(function (g) { return g.key; });
    var open = s.saved.openGroups ? new Set(s.saved.openGroups) : new Set(keys);
    if (open.has(key)) open.delete(key); else open.add(key);
    App.set({ saved: { openGroups: open } });
  }

  /* ---- data actions ---------------------------------------------------- */
  function toggleFav(id) {
    var added = ctx.act.toggleFav(id);
    if (!added && App.state.sheet.tab === 'saved') announce('已取消收藏 {n} 家', { n: 1 });
  }
  function unsave(id) { if (App.state.user.fav.has(id)) toggleFav(id); }

  function hideOne(id, el) {
    var changed = ctx.act.setBlack(id, true);
    if (!changed) return;
    // FEEDBACK-01: don't take focus — move it to a stable neighbour instead
    if (el) {
      var row = el.closest('.ls-row');
      var next = row && row.nextElementSibling && row.nextElementSibling.classList.contains('ls-row') ? row.nextElementSibling : null;
      focusAfter = next ? '.ls-row[data-id="' + cssEscape(next.getAttribute('data-id')) + '"] .ls-open' : '[data-act="multi"]';
    }
    offerUndo([id], 0, '已弃用 1 家，这家餐厅不会再显示在筛选结果和地图上');
  }

  /** the ⋯ menu's "remove from this collection" — one act per list (E2 members). */
  function removeFromLists(ref, listIds) {
    (listIds || []).forEach(function (lid) { ctx.act.removeFromList(lid, ref); });
    announce('已从收藏夹移除', null);
  }

  /** focusList(id) — 只看这个收藏夹 / 显示全部收藏 (production __favFocus). */
  function focusList(listId) {
    var cur = App.state.saved.onlyList;
    ctx.act.setListFocus(cur === listId ? null : (listId || null));
  }

  function bulkFav() {
    var s = App.state; if (!s.multi.ids.size) return;
    var add = Array.from(s.multi.ids).filter(function (id) { return !s.user.fav.has(id); });
    if (add.length) ctx.act.batch(function (tFav) { add.forEach(function (id) { tFav(id); }); });
    App.set({ multi: { active: false, ids: new Set() } });        // MULTI-02: finish → exit, clear C
    focusAfter = '[data-act="multi"]';
    announce('已收藏 {n} 家', { n: add.length });                  // the real diff, not |C|
  }

  function bulkHide() {
    var s = App.state; if (!s.multi.ids.size) return;
    var done = [], skipped = 0;
    Array.from(s.multi.ids).forEach(function (id) {
      if (!D.byId(id)) { skipped += 1; return; }                  // pins / landmarks don't take the hide action
      if (s.user.black.has(id)) { skipped += 1; return; }
      done.push(id);
    });
    if (done.length) ctx.act.batch(function (tFav, tBlack) { done.forEach(function (id) { tBlack(id); }); });
    App.set({ multi: { active: false, ids: new Set() } });
    focusAfter = '[data-act="multi"]';
    if (!done.length && skipped) { announce('已弃用 {n} 家 · 跳过 {k} 家', { n: 0, k: skipped }); return; }
    offerUndo(done, skipped, done.length === 1 ? '已弃用 1 家，这家餐厅不会再显示在筛选结果和地图上' : '已弃用 {n} 家，这些餐厅不会再显示在筛选结果和地图上');
  }

  /** offerUndo(ids, skipped, key) — MULTI-03: record the exact diff + the revision it was taken at. */
  function offerUndo(ids, skipped, key) {
    var text = skipped > 0 ? '已弃用 {n} 家 · 跳过 {k} 家' : key;
    var id = ctx.act.showToast({ kind: 'undo', text: text, count: ids.length, undo: { kind: 'black', ids: ids, skipped: skipped } });
    toastTrack.id = id; toastTrack.rev = rev; toastTrack.paused = false;
    toastTrack.remaining = ctx.motion.toastMs || 9000;
    startToastTimer();
  }

  function canUndo(s) {
    if (!s.toast || !s.toast.undo) return false;
    if (toastTrack.id !== s.toast.id) return true;     // a toast we adopted: treat as fresh
    return toastTrack.rev === rev;
  }

  function runUndo() {
    var s = App.state; if (!s.toast || !s.toast.undo) return;
    if (!canUndo(s)) {
      App.emit('toast:undo', { id: s.toast.id, undo: s.toast.undo, result: 'refused' });
      App.set({ toast: { undo: null, text: '数据已更新，无法撤销此操作', count: 0 } });
      return;
    }
    var un = s.toast.undo;
    if (un.kind === 'black') {
      var back = (un.ids || []).filter(function (id) { return App.state.user.black.has(id); });
      if (back.length) ctx.act.batch(function (tFav, tBlack) { back.forEach(function (id) { tBlack(id); }); });
    }
    App.emit('toast:undo', { id: s.toast.id, undo: un, result: 'done' });
    ctx.act.dismissToast(s.toast.id);
    focusAfter = '[data-act="multi"]';
  }

  /** a business toast (pin deleted, import applied…) carries its own callback. */
  function runToastAction() {
    var tst = App.state.toast;
    if (!tst || !tst.action || typeof tst.action.run !== 'function') return;
    try { tst.action.run(); } catch (e) { console.error(e); }
    ctx.act.dismissToast(tst.id);
  }

  /** copyList(listId?) — the whole Saved tab, or just one collection. */
  function copyList(listId) {
    var text, n;
    if (listId) {
      var g = Lm.groupsFor(App.state).filter(function (x) { return x.listId === listId; })[0];
      if (!g) return;
      text = Lm.listText(App.state, listId);
      n = g.ids.length;
    } else {
      text = Lm.listText(App.state);
      n = Lm.rowsFor(App.state).length;
    }
    var ok = function () { announce('已复制 {n} 家到剪贴板', { n: n }); };
    var fail = function () { announce('复制失败，请手动复制', null); };
    function fallback() {
      try {
        var ta = document.createElement('textarea');
        ta.value = text; ta.setAttribute('readonly', ''); ta.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
        document.body.appendChild(ta); ta.select();
        var done = document.execCommand('copy');
        ta.remove(); if (done) ok(); else fail();
      } catch (e2) { fail(); }
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(ok, fallback);
      else fallback();
    } catch (e) { fallback(); }
  }

  /** showAllResults() — the out-of-view card's primary action: fit M inside mapRect. */
  function showAllResults() {
    var s = App.state;
    var ids = D.M(s);
    App.set({ notices: { oov: { visible: false } }, userDraggedMap: false });
    try {
      var map = window.MapMod && window.MapMod.map;
      if (!map || !window.L || !ids.length) return;
      var pts = [];
      for (var i = 0; i < ids.length; i++) {
        var r = D.byId(ids[i]);
        if (r && typeof r.lat === 'number' && typeof r.lon === 'number') pts.push([r.lat, r.lon]);
      }
      if (!pts.length) return;
      var U = s.layout.U, R = s.layout.mapRect;
      var tl = [Math.max(0, R.x - U.x) + 24, Math.max(0, R.y - U.y) + 24];
      var br = [Math.max(0, (U.x + U.w) - (R.x + R.w)) + 24, Math.max(0, (U.y + U.h) - (R.y + R.h)) + 24];
      map.fitBounds(window.L.latLngBounds(pts), { paddingTopLeft: tl, paddingBottomRight: br, maxZoom: 16, animate: !ctx.motion.reduced });
    } catch (e) { /* map not ready */ }
  }

  /** announce(key, params) — FEEDBACK-01: never preempt a live undo toast. */
  function announce(key, params) {
    var s = App.state;
    if (s.toast && s.toast.kind === 'undo' && s.toast.undo) {
      status = { text: key, params: params, timer: status.timer };
      clearTimeout(status.timer);
      status.timer = setTimeout(function () { status.text = null; App.requestRender('list'); }, 4000);
      App.requestRender('list');
      return;
    }
    ctx.act.showToast({ kind: 'info', text: key, count: (params && params.n) || 0 });
    toastTrack.id = App.state.toast ? App.state.toast.id : null;
    toastTrack.rev = rev; toastTrack.paused = false; toastTrack.remaining = ctx.motion.toastMs || 9000;
    startToastTimer();
  }

  /* ---- toast countdown -------------------------------------------------- */
  function startToastTimer() {
    stopToastTimer();
    var step = 200;
    toastTimer = setInterval(function () {
      var sp = App.state.toast && App.state.toast.paused;
      if (toastTrack.paused || sp) return;
      toastTrack.remaining -= step;
      var total = ctx.motion.toastMs || 9000;
      var pct = Math.max(0, Math.min(100, toastTrack.remaining / total * 100));
      var bar = toastRoot.querySelector('.toast-progress');
      if (bar) bar.style.setProperty('--toast-pct', pct.toFixed(1) + '%');
      if (toastTrack.remaining <= 0) {
        stopToastTimer();
        var cur = App.state.toast;
        if (cur && cur.id === toastTrack.id) ctx.act.dismissToast(cur.id);
      }
    }, step);
  }
  function stopToastTimer() { if (toastTimer) { clearInterval(toastTimer); toastTimer = null; } }

  /* ======================================================================
     render
     ==================================================================== */
  Lm.render = function (s, changed) {
    if (!ctx) return;
    changed = changed || new Set(['*']);
    // LAY-02: at mid, opening a detail auto-collapses the left column, so the
    // list's viewport goes to zero width and comes back rebuilt from nothing.
    // Watching the width here catches it whichever way the column moved.
    var groups = Lm.groupsFor(s);
    var L = []; groups.forEach(function (g) { g.ids.forEach(function (id) { L.push(id); }); });
    var M = D.M(s);
    var mvCount = computeMV(M); lastMVCount = mvCount;

    // out-of-view notice (FEEDBACK-02: at most once per load, honour "never again")
    if (s.sheet.tab === 'results' && !s.selected.id && M.length > 0 && mvCount === 0 &&
        !s.notices.oov.dismissedForever && !s.notices.oov.shown && !oovShownThisLoad) {
      oovShownThisLoad = true;
      App.set({ notices: { oov: { shown: true, visible: true } } }, { silent: true });
      s = App.state;
    }

    var idsSig = L.length + '#' + _mEpoch + '|' + (L.length ? L[0] + '|' + L[L.length - 1] : '') +
      '|' + s.sort + '|' + s.sheet.tab + '|' + s.saved.groupBy;
    var sig = [
      s.layout.mode, s.sheet.tab, s.selected.id || '', idsSig, M.length, mvCount,
      s.multi.active ? '1' : '0', Array.from(s.multi.ids).sort().join(','), s.multi.scope,
      s.sort, s.saved.groupBy, s.saved.openGroups ? Array.from(s.saved.openGroups).sort().join(',') : '*',
      s.saved.onlyList || '', rev, s.lang, s.fontScale,
      s.notices.oov.visible ? '1' : '0', s.notices.oov.dismissedForever ? '1' : '0',
      s.filters.bookableOnly ? '1' : '0',
      s.toast ? s.toast.id + '|' + s.toast.text + '|' + s.toast.count + '|' + (s.toast.undo ? '1' : '0') : '',
      status.text || '', canUndo(s) ? '1' : '0'
    ].join('\u0002');
    if (sig === lastSig && !changed.has('*') && root.firstChild) { paintWindow(); runPending(); return; }

    var sc = scroller();
    var keepScroll = (idsSig === lastIds && sc) ? sc.scrollTop : null;
    lastSig = sig; lastIds = idsSig;

    buildItems(s, groups);
    renderBody(s, groups, L, M, mvCount);
    renderFoot(s, L, M);
    renderToast(s);

    bindScroller();
    if (keepScroll !== null && sc) sc.scrollTop = keepScroll;
    paintWindow(true);
    kickPopups(s);
    runPending();
  };

  /* The list itself is the only thing that reliably knows it is back on screen:
     neither layout:settled nor containers:restored fires on the detail-close path
     (traced — the close event lands, the events never do). So the parked intent is
     consumed here, at the end of every render, once there is a real scroller. */
  function pumpParkedRestore(tries) {
    if (!wantRestore || !closeAnchor) return;                 // already consumed
    if (listIsLaidOut() && App.state.sheet.tab !== 'filters') { consumeParkedRestore(); return; }
    if (tries < 90) ctx.util.raf(function () { pumpParkedRestore(tries + 1); });
    // on timeout the flag stays set on purpose — runPending() picks it up later
  }

  function consumeParkedRestore() {
    if (!wantRestore || !closeAnchor) return;
    if (!listIsLaidOut()) return;
    if (App.state.sheet.tab === 'filters') return;
    var a = closeAnchor;
    wantRestore = false; closeAnchor = null;
    ctx.motion.afterGeometry(function () { Lm.restore(a, null); });
  }

  function runPending() {
    consumeParkedRestore();
    if (pendingRestore) {
      var p = pendingRestore; pendingRestore = null;
      Lm.restore(p.anchor, p.id);
    }
    if (focusAfter) {
      var sel = focusAfter; focusAfter = null;
      ctx.motion.afterGeometry(function () {
        var el = root.querySelector(sel) || foot.querySelector(sel);
        if (el && el.focus) el.focus({ preventScroll: true });
      });
    }
  }

  /* ---- the flat item list the virtualiser walks ------------------------- */
  function buildItems(s, groups) {
    items = []; indexOf = Object.create(null);
    var open = s.saved.openGroups;
    var narrow = s.layout.mode === 'narrow';
    groups.forEach(function (g) {
      var isOpen = !open || open.has(g.key);
      if (g.label !== null) {
        var first = items.length === 0;
        var afterRows = !first && items[items.length - 1].t === 'r';
        var hasRows = isOpen && g.items.length > 0;
        var geometry = (first ? 'first' : afterRows ? 'after-rows' : 'after-heading') + (hasRows ? ':rows' : ':empty');
        items.push({ t: 'g', key: 'g:' + g.key, heightKey: 'g:' + g.key + '|' + geometry,
          geometry: geometry, first: first, afterRows: afterRows, hasRows: hasRows, g: g, open: isOpen });
      }
      if (g.label !== null && !isOpen) return;
      g.items.forEach(function (it) {
        if (it.kind === 'rst' && !it.d) it.d = D.byId(it.ref);
        var key = 'r:' + it.ref;
        indexOf[it.ref] = items.length;
        items.push({ t: 'r', key: key, kind: it.kind, ref: it.ref, d: it.d, bm: it.bm, narrow: narrow, group: g });
      });
    });
    itemSignature = items.map(heightKeyOf).join('\u0003');
    offsDirty = true;
  }

  /* ---- height bookkeeping ---------------------------------------------- */
  var itemSignature = '';
  function heightKeyOf(it) { return it.heightKey || it.key; }
  function bucketOf(it) {
    if (it.t === 'g') return 'g:' + it.geometry;
    return (it.narrow ? 'n:' : 'c:') + it.kind;
  }
  function defaultH(it) {
    if (it.t === 'g') return 44 + (it.first ? 8 : it.afterRows ? 24 : 0) + (it.hasRows ? 12 : 0);
    return it.narrow ? 97 : 77;
  }
  function estH(it) {
    var b = avgH[bucketOf(it)];
    return b && b.n ? b.avg : defaultH(it);
  }
  function hOf(it) { var h = itemH[heightKeyOf(it)]; return h === undefined ? estH(it) : h; }
  /**
   * noteH — a measured height. The mean is an EMA over the last ~20 samples,
   * not a cumulative average: a row measured while its column was still
   * animating open (or before the panel had a width at all) is wrong by a
   * factor of several, and a cumulative mean would carry that error for the
   * whole session — the scrollbar then claims 1.9M px for an 800k px list.
   * measureW below throws those samples away outright; the EMA is the second
   * line of defence.
   */
  function noteH(it, h) {
    if (!(h > 0)) return false;
    var prev = itemH[heightKeyOf(it)];
    if (prev !== undefined && Math.abs(prev - h) < 0.5) return false;
    itemH[heightKeyOf(it)] = h;
    var k = bucketOf(it), b = avgH[k] || (avgH[k] = { avg: h, n: 0 });
    b.n += 1;
    b.avg += (h - b.avg) / Math.min(b.n, 20);
    return true;
  }
  /**
   * Every measured height is a function of the column width, so the cache is
   * kept per width instead of thrown away: opening a detail at Fold-inner
   * collapses the left column to the rail and coming back restores it, and a
   * wiped cache would send NAV-02's anchor back to pure estimates — the list
   * landed ~3 rows off. Returning to a width we have measured is exact.
   */
  var measureW = -1;
  var heightsByW = Object.create(null), avgByW = Object.create(null);
  function widthChanged(w) {
    if (w === measureW) return false;
    measureW = w;
    itemH = heightsByW[w] || (heightsByW[w] = Object.create(null));
    avgH = avgByW[w] || (avgByW[w] = Object.create(null));
    offsDirty = true;
    return true;
  }

  var offsCache = null, offsDirty = true, totalH = 0;
  function offsets() {
    if (!offsDirty && offsCache && offsCache.length === items.length + 1) return offsCache;
    var out = new Array(items.length + 1); out[0] = 0;
    for (var i = 0; i < items.length; i++) out[i + 1] = out[i] + hOf(items[i]);
    totalH = out[items.length];
    offsCache = out; offsDirty = false;
    return out;
  }
  function lowerBound(offs, y) {
    var lo = 0, hi = items.length;
    while (lo < hi) { var mid = (lo + hi) >> 1; if (offs[mid + 1] <= y) lo = mid + 1; else hi = mid; }
    return Math.min(lo, Math.max(0, items.length - 1));
  }

  /* ---- body -------------------------------------------------------------- */
  function renderBody(s, groups, L, M, mvCount) {
    var html = head(s, L, M, mvCount);
    var listOnly = false;
    if (s.sheet.tab === 'results' && M.length === 0) {
      html += emptyResults(s);
    } else if (s.sheet.tab === 'saved' && L.length === 0) {
      html += emptySaved();
    } else {
      if (s.sheet.tab === 'results' && s.notices.oov.visible && !s.notices.oov.dismissedForever && mvCount === 0) html += oovCard(M.length);
      listOnly = true;
    }
    if (s.sheet.tab === 'saved' && s.saved.onlyList) html += focusBar(s);
    html += '<ul class="ls-list ls-vp" aria-label="' + esc(t(s.sheet.tab === 'saved' ? '收藏列表' : '结果列表')) + '"></ul>';
    root.innerHTML = html;
    vp = root.querySelector('.ls-vp');
    if (!listOnly) { items = []; indexOf = Object.create(null); offsDirty = true; }
    winStart = winEnd = -1;

    // MOTION M8 plays when multi-select is ENTERED, not on every tick.
    if (s.multi.active && !lastMultiActive) {
      root.classList.add('multi-enter');
      if (multiEnterTimer) clearTimeout(multiEnterTimer);
      multiEnterTimer = setTimeout(function () {
        multiEnterTimer = 0;
        if (root) root.classList.remove('multi-enter');
      }, 260);
    } else if (!s.multi.active && root.classList.contains('multi-enter')) {
      root.classList.remove('multi-enter');
    }
    lastMultiActive = s.multi.active;
  }

  /* ---- the window ------------------------------------------------------- */
  function bindScroller() {
    var sc = scroller();
    if (sc === curScroller) return;
    if (offScroll) { offScroll(); offScroll = null; }
    curScroller = sc;
    if (!sc) return;
    offScroll = u.on(sc, 'scroll', function () {
      if (scrollRaf) return;
      scrollRaf = true;
      u.raf(function () { scrollRaf = false; paintWindow(); noteAnchor(); });
    }, { passive: true });
  }

  function vpOffset(sc) {
    if (!vp || !sc) return 0;
    return vp.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
  }

  /** focusMark() → {id, sel} when the keyboard caret is inside a painted row. */
  function focusMark() {
    var a = document.activeElement;
    if (!a || !vp.contains(a)) return null;
    var row = a.closest && a.closest('.ls-row[data-id]');
    if (!row) return null;
    var cls = (a.className || '').split(/\s+/).filter(Boolean)[0];
    return { id: row.getAttribute('data-id'), cls: cls || null };
  }
  function restoreFocus(mark) {
    if (!mark) return;
    var row = rowEl(mark.id);
    if (!row) return;
    // the class came off our own markup, but only ever trust a plain token
    var safe = mark.cls && /^[A-Za-z][\w-]*$/.test(mark.cls) ? mark.cls : null;
    var el = (safe && row.querySelector('.' + safe)) ||
             row.querySelector('.ls-open') || row;
    try { el.focus({ preventScroll: true }); } catch (e) { /* detached */ }
  }

  function paintWindow(force, depth) {
    if (!vp || !vp.isConnected) return;
    if (!vp.clientWidth) return;          // parked, or the panel is collapsed: nothing to lay out
    var sc = scroller();
    if (!sc) return;
    if (!items.length) {
      if (vp.firstChild) vp.innerHTML = '';
      winStart = winEnd = -1;
      return;
    }
    var vpW = vp.clientWidth;
    // the measurement key: anything that changes a row's rendered height
    var mKey = vpW + ':' + App.state.fontScale + ':' + (App.state.multi.active ? 'm' : '') + ':' + App.state.sheet.tab;
    if (vpW >= 120 && widthChanged(mKey)) force = true;
    var offs = offsets();
    var vpTop = vpOffset(sc);
    var viewH = sc.clientHeight || App.state.layout.H || 480;
    var from = sc.scrollTop - vpTop - OVERSCAN;
    var to = sc.scrollTop - vpTop + viewH + OVERSCAN;
    var start = lowerBound(offs, Math.max(0, from));
    var end = start;
    while (end < items.length && offs[end] < to) end += 1;
    if (end <= start) end = Math.min(items.length, start + 1);

    // Hysteresis: rebuilding the band on every frame that crosses a row
    // boundary is ~20 rows of innerHTML per frame, which WebKit feels. The
    // painted band already reaches OVERSCAN past the viewport, so only rebuild
    // once the viewport comes within KEEP px of its edge.
    var needFrom = sc.scrollTop - vpTop, needTo = needFrom + viewH;
    var KEEP = 220;
    if (!force && winStart >= 0 &&
        (winStart === 0 || offs[winStart] + KEEP <= needFrom) &&
        (winEnd >= items.length || offs[winEnd] - KEEP >= needTo)) return;
    if (!force && start === winStart && end === winEnd) return;

    var topPad = offs[start];
    var s = App.state;
    var buf = '<li class="ls-pad" aria-hidden="true" style="height:' + Math.round(topPad) + 'px"></li>';
    for (var i = start; i < end; i++) {
      var it = items[i];
      buf += it.t === 'g' ? groupHead(it) : rowHtml(it, s, i === 0);
    }
    buf += '<li class="ls-pad" aria-hidden="true" style="height:' + Math.round(Math.max(0, totalH - offs[end])) + 'px"></li>';
    // A window repaint replaces every row node, which sends the keyboard
    // caret to <body> if it was on one of them. That is not only a NAV-02
    // problem — any scroll that crosses the hysteresis band while a row
    // button has focus used to lose it. Remember where focus was and put it
    // back on the same row+control if that row is still painted.
    var keep = focusMark();
    vp.innerHTML = buf;
    restoreFocus(keep);
    winStart = start; winEnd = end;

    // measure what we just painted, then keep the scroll position stable.
    // A column narrower than any real panel means the geometry has not landed
    // yet (boot, a column mid-transition): rendering is fine, measuring is not.
    var changedH = false;
    var nodes = vp.children;
    if (vpW >= 120) {
      for (var k = 1; k < nodes.length - 1; k++) {
        var idx = start + k - 1, node = nodes[k];
        if (idx >= items.length) break;
        if (noteH(items[idx], node.offsetHeight)) changedH = true;
      }
    }
    if (changedH) {
      offsDirty = true;
      var offs2 = offsets();
      var delta = offs2[start] - topPad;
      nodes[0].style.height = Math.round(offs2[start]) + 'px';
      nodes[nodes.length - 1].style.height = Math.round(Math.max(0, totalH - offs2[end])) + 'px';
      if (Math.abs(delta) > 0.5) sc.scrollTop = Math.max(0, sc.scrollTop + delta);
      if ((depth || 0) < 2) paintWindow(true, (depth || 0) + 1);
    }
    if (vpW >= 120) calibrate(s);
    paintPhotos();
  }

  /**
   * calibrate — measure ~24 rows sampled across the WHOLE list, once per
   * measurement key, in a hidden probe of the same width.
   *
   * Without it the only rows ever measured are the ones near the viewport,
   * and the top of a rating-sorted list is the tallest part of it (two-line
   * names, two award badges). The estimated total then overshoots by ~6%, and
   * dragging the scrollbar to the bottom lands short of the last row and has
   * to be repeated. The sample makes the estimate representative and gives
   * those particular rows their exact height as a bonus.
   */
  var calibKey = null;
  function calibrate(s) {
    if (!vp || items.length < 60) return;
    var key = measureW + '|' + itemSignature;
    if (calibKey === key) return;
    calibKey = key;
    var N = 48, picks = [], seen = Object.create(null);
    for (var i = 0; i < N; i++) {
      var idx = Math.floor(i * items.length / N);
      if (seen[idx] || itemH[heightKeyOf(items[idx])] !== undefined) continue;
      seen[idx] = 1; picks.push(idx);
    }
    if (!picks.length) return;
    var probe = document.createElement('ul');
    probe.className = 'ls-list ls-probe';
    probe.setAttribute('aria-hidden', 'true');
    var buf = '';
    picks.forEach(function (idx) {
      var it = items[idx];
      buf += it.t === 'g' ? groupHead(it) : rowHtml(it, s, false);
    });
    probe.innerHTML = buf;
    root.appendChild(probe);
    var nodes = probe.children;
    for (var k = 0; k < nodes.length && k < picks.length; k++) noteH(items[picks[k]], nodes[k].offsetHeight);
    probe.remove();
    offsDirty = true;
    var offs2 = offsets();
    if (vp.firstChild && vp.lastChild !== vp.firstChild) {
      vp.firstChild.style.height = Math.round(offs2[winStart]) + 'px';
      vp.lastChild.style.height = Math.round(Math.max(0, totalH - offs2[winEnd])) + 'px';
    }
  }

  /* ---- photos: popups slot 7 (lazy, never on the boot path) ------------- */
  function kickPopups(s) {
    if (popupsKicked || s.layout.mode !== 'narrow' || !items.length) return;
    var seed = null;
    for (var i = 0; i < items.length && !seed; i++) if (items[i].t === 'r' && items[i].kind === 'rst') seed = items[i].ref;
    if (!seed) return;
    popupsKicked = true;
    var run = function () {
      D.detail(seed).then(function () { popupsReady = true; paintPhotos(); }, function () { popupsReady = false; });
    };
    if (window.requestIdleCallback) window.requestIdleCallback(run, { timeout: 2500 });
    else setTimeout(run, 1500);
  }

  function paintPhotos() {
    if (!popupsReady || !vp) return;
    Array.prototype.forEach.call(vp.querySelectorAll('.ls-photo[data-ref][data-pending]'), function (box) {
      var ref = box.getAttribute('data-ref');
      var known = photoCache[ref];
      if (known === null) return;                                   // in flight; its resolve paints
      if (known !== undefined) { applyPhoto(box, known); return; }
      photoCache[ref] = null;      // pending
      D.detail(ref).then(function (det) {
        var url = (det && det.photos && det.photos.length) ? det.photos[0] : '';
        photoCache[ref] = url || '';
        Array.prototype.forEach.call(root.querySelectorAll('.ls-photo[data-ref="' + cssEscape(ref) + '"][data-pending]'), function (b) {
          applyPhoto(b, photoCache[ref]);
        });
      }, function () { photoCache[ref] = ''; });
    });
  }

  function applyPhoto(box, url) {
    box.removeAttribute('data-pending');
    if (!url) return;
    box.classList.remove('is-empty');
    box.classList.add('skeleton');
    box.innerHTML = '<img class="ls-img" src="' + esc(PhotoUrls.url(url, 320)) + '" alt="" loading="lazy" decoding="async">' +
      '<span class="ls-fade"></span>';
  }

  function esc(v) { return u.esc(v); }

  /* ---- head -------------------------------------------------------------- */
  function head(s, L, M, mvCount) {
    var saved = s.sheet.tab === 'saved';
    var sub, tools;
    if (s.multi.active) {
      var C = s.multi.ids, k = 0;
      C.forEach(function (id) { if (idxOf(id) < 0) k += 1; });
      sub = t('已选 {n} 家', { n: u.fmtCount(C.size) }) +
        (k > 0 ? ' · ' + t(saved ? '其中 {k} 家不在当前列表' : '其中 {k} 家不在当前结果', { k: u.fmtCount(k) }) : '');
      tools =
        '<button class="btn btn-quiet ls-tool" data-act="select-visible">' + t('全选可见') + '</button>' +
        '<button class="btn btn-quiet ls-tool" data-act="clear-selection"' + (C.size ? '' : ' disabled') + '>' + t('清空选择') + '</button>' +
        '<button class="btn btn-secondary ls-tool" data-act="cancel-multi">' + t('取消') + '</button>';
    } else if (saved) {
      sub = t('收藏 {n} 家 · 不受筛选影响', { n: u.fmtCount(L.length) });
      tools =
        '<span class="select-wrap ls-groupby"><select class="select" data-groupby aria-label="' + esc(t('分组方式')) + '">' +
          '<option value="city"' + (s.saved.groupBy === 'city' ? ' selected' : '') + '>' + t('按城市') + '</option>' +
          '<option value="list"' + (s.saved.groupBy === 'list' ? ' selected' : '') + '>' + t('按收藏夹') + '</option>' +
        '</select></span>' +
        '<button class="icon-btn ls-tool-icon" data-act="new-list" aria-label="' + esc(t('新建收藏夹')) + '">' + ctx.icon('plus') + '</button>' +
        '<button class="icon-btn ls-tool-icon" data-act="copy-list" aria-label="' + esc(t('复制清单文本')) + '"' + (L.length ? '' : ' disabled') + '>' + ctx.icon('copy') + '</button>' +
        multiBtn(L.length);
    } else {
      sub = mvCount === null
        ? t('符合筛选 {m} 家', { m: u.fmtCount(M.length) })
        : t('符合筛选 {m} 家 · 屏幕内 {v} 家', { m: u.fmtCount(M.length), v: u.fmtCount(mvCount) });
      var cur = D.config.SORTS.filter(function (o) { return o.key === s.sort; })[0] || D.config.SORTS[0];
      tools =
        '<button class="select-btn ls-sort" data-act="sort" aria-haspopup="menu" aria-expanded="' + (s.overlay.kind === 'sortMenu') + '" aria-label="' + esc(t('排序方式')) + '">' +
          '<span class="ls-sort-label">' + t(cur.label) + '</span>' + ctx.icon('chevronDown', { cls: 'ic-sm' }) + '</button>' +
        multiBtn(L.length);
    }
    return '<div class="ls-head">' +
      '<p class="ls-sub t-secondary">' + sub + '</p>' +
      '<div class="ls-tools">' + tools + '</div>' +
      '<p class="ls-status t-secondary" role="status">' + (status.text ? t(status.text, status.params) : '') + '</p>' +
      '</div>';
  }

  function multiBtn(n) {
    return '<button class="btn btn-secondary ls-tool ls-multi" data-act="multi"' + (n ? '' : ' disabled') + '>' + ctx.icon('select', { cls: 'ic-sm' }) + '<span>' + t('多选') + '</span></button>';
  }

  function focusBar(s) {
    var name = '';
    try {
      var g = (D.savedGroups(s) || []).filter(function (x) { return x.listId === s.saved.onlyList; })[0];
      name = g ? g.label : '';
    } catch (e) { name = ''; }
    return '<div class="ls-focus" role="status">' +
      '<span class="t-control">' + esc(name) + '</span>' +
      '<button class="link-btn" data-act="clear-focus">' + t('显示全部收藏') + '</button></div>';
  }

  function groupHead(it) {
    var g = it.g, isOpen = it.open;
    var focusBtn = '';
    if (g.listId) {
      var on = App.state.saved.onlyList === g.listId;
      // 只看这个收藏夹 + the collection's own ⋯ (rename / copy / delete).
      // Without the second one 4.0 had no UI path to act.renameList or
      // act.deleteList at all, so a collection could be created but never
      // renamed or removed (DESIGN §15.2, the collection-management row).
      focusBtn = '<span class="ls-group-focus"><button class="icon-btn icon-btn-secondary" data-focus-list="' + esc(g.listId) + '" ' +
        'aria-pressed="' + on + '" aria-label="' + esc(t('只看这个收藏夹')) + '">' + ctx.icon('filter') + '</button>' +
        '<button class="icon-btn icon-btn-secondary ls-group-more" data-group-menu="' + esc(g.listId) + '" ' +
        'aria-haspopup="menu" aria-label="' + esc(t('收藏夹操作')) + '">' + ctx.icon('more') + '</button></span>';
    }
    return '<li class="ls-group' + (it.first ? ' is-first' : '') +
      (it.afterRows ? ' is-after-rows' : '') + (it.hasRows ? ' has-rows' : '') + '">' +
      '<button class="ls-group-btn" data-group="' + esc(g.key) + '" aria-expanded="' + isOpen + '">' +
        ctx.icon(isOpen ? 'chevronDown' : 'chevronRight', { cls: 'ic-sm' }) +
        (g.emoji ? ctx.emoji.img(g.emoji, 18) : '') +
        '<span class="t-group-title ls-group-name"' + (g.ja ? ' lang="ja"' : '') + '>' + esc(g.label) + '</span>' +
        '<span class="ls-group-n t-secondary num">' + u.fmtCount(g.ids.length) + '</span>' +
      '</button>' + focusBtn + '</li>';
  }

  /* ---- rows -------------------------------------------------------------- */
  function rateHtml(r) {
    return (!r || r.rating === null || r.rating === undefined)
      ? '<span class="ls-rate rating-missing t-secondary">' + t('评分暂无') + '</span>'
      : '<span class="ls-rate rating num">' + ctx.icon('starFill', { cls: 'ic-sm', fill: true }) + u.fmtRating(r.rating) + '</span>';
  }

  function nameLine(text, cls, noRate, r) {
    return '<span class="ls-line"><span class="ls-name ' + cls + ' clamp-2" lang="ja">' + esc(text) + '</span>' +
      (noRate ? '' : rateHtml(r)) + '</span>';
  }

  function metaLine(r) {
    var bits = ['<span class="price-text">' + esc(u.fmtPrice(r.bucket, { short: true })) + '</span>'];
    if (r.st) bits.push('<span lang="ja">' + esc(r.st) + '</span>');
    // 距离最近 without a distance is a list you cannot check. When a fix is
    // known, every row says how far it is — the same affordance 3.2.x showed
    // as .ux-distance after 附近.
    var d = distanceFrom(r);
    if (d !== null) bits.push('<span class="ls-dist num">' + esc(u.fmtDistance(d)) + '</span>');
    return '<span class="ls-meta t-secondary">' + bits.join('<span class="ls-sep" aria-hidden="true">|</span>') + '</span>';
  }
  /** metres from the live/persisted fix to a row, or null when there is none.
   *  Only shown while the list is actually ordered by distance, so an ordinary
   *  browse is not cluttered with a number nobody asked for. */
  function distanceFrom(r) {
    var s = App.state;
    if (s.sort !== 'distance') return null;
    var fix = (window.Adapter && Adapter.liveFix) || (s.nearby && s.nearby.fix);
    if (!fix || typeof fix.lat !== 'number' || !r || typeof r.lat !== 'number') return null;
    var lon = (fix.lng != null ? fix.lng : fix.lon);
    if (typeof lon !== 'number') return null;
    var R = 6371000, toRad = Math.PI / 180;
    var dLat = (r.lat - fix.lat) * toRad, dLon = (r.lon - lon) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(fix.lat * toRad) * Math.cos(r.lat * toRad) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function awardsLine(r) {
    var aw = D.awardSlugs(r); if (!aw.length) return '';
    var seen = {}, out = '';
    aw.forEach(function (slug) {
      if (seen[slug]) return; seen[slug] = 1;
      if (!D.config.AWARD_BY_SLUG[slug]) return;
      out += '<span class="badge badge-' + esc(slug) + '">' + esc(D.awardShort(slug)) + '</span>';
    });
    return out ? '<span class="badge-row ls-awards">' + out + '</span>' : '';
  }

  function hiddenMark(ref, s) {
    return s.user.black.has(ref) ? '<span class="ls-hidden-mark t-badge">' + ctx.icon('hide', { cls: 'ic-sm' }) + t('已弃用') + '</span>' : '';
  }

  function checkbox(ref, label, s) {
    if (!s.multi.active) return '';
    var on = s.multi.ids.has(ref);
    return '<label class="ls-check"><input type="checkbox" class="checkbox" data-check="' + esc(ref) + '"' + (on ? ' checked' : '') +
      ' aria-label="' + esc(label) + '"></label>';
  }

  function actions(ref, label, s, kind) {
    if (s.multi.active) return '';
    var fav = s.user.fav.has(ref);
    var heart = '<button class="icon-btn is-fav" data-fav="' + esc(ref) + '" aria-pressed="' + fav +
      '" aria-haspopup="dialog" aria-label="' +
      esc(t(fav ? '管理收藏' : '加入收藏') + ' ' + label) + '">' + ctx.icon('heart', { fill: fav }) + '</button>';
    if (s.sheet.tab === 'saved') {
      return '<span class="ls-actions">' + heart +
        '<button class="icon-btn icon-btn-secondary" data-menu="' + esc(ref) + '" aria-haspopup="menu" aria-label="' + esc(t('更多操作') + ' ' + label) + '">' + ctx.icon('more') + '</button>' +
        '</span>';
    }
    if (kind !== 'rst') return '<span class="ls-actions">' + heart + '</span>';
    return '<span class="ls-actions">' + heart +
      '<button class="icon-btn is-danger" data-hide="' + esc(ref) + '" aria-label="' + esc(t('弃用这间店') + ' ' + label) + '">' + ctx.icon('hide') + '</button>' +
      '</span>';
  }

  function openAttrs(ref, label, s) {
    var pressed = s.multi.active ? ' aria-pressed="' + s.multi.ids.has(ref) + '"' : '';
    return ' data-open="' + esc(ref) + '" aria-label="' + esc(label) + '"' + pressed;
  }

  function rowClass(ref, s, extra) {
    var c = ['ls-row', extra];
    if (s.selected.id === ref) c.push('is-selected');
    if (s.multi.active) c.push('is-multi');
    if (s.multi.active && s.multi.ids.has(ref)) c.push('is-checked');
    if (s.user.black.has(ref)) c.push('is-hidden');
    return c.join(' ');
  }

  function rowHtml(it, s, first) {
    if (it.kind === 'rst' && it.d) return it.narrow ? rowNarrow(it, s, first) : rowColumn(it, s, first);
    return rowOther(it, s, first);
  }

  function photoBox(it) {
    var ref = it.ref;
    var fallback = '<span class="ls-photo-ph">' + ctx.emoji.img(D.genreEmoji(it.d), 20) + '</span>';
    var known = photoCache[ref];
    if (known) {
      return '<span class="ls-photo skeleton" data-ref="' + esc(ref) + '" aria-hidden="true" data-fallback="' + esc(fallback) + '">' +
        '<img class="ls-img" src="' + esc(PhotoUrls.url(known, 320)) + '" alt="" loading="lazy" decoding="async"><span class="ls-fade"></span></span>';
    }
    var pending = (known === undefined || known === null) ? ' data-pending' : '';
    return '<span class="ls-photo is-empty" data-ref="' + esc(ref) + '"' + pending + ' aria-hidden="true" data-fallback="' + esc(fallback) + '">' +
      fallback + '<span class="ls-fade"></span></span>';
  }

  function rowNarrow(it, s, first) {
    var r = it.d;
    var label = r.name + (r.rating ? ' · ' + t('评分') + ' ' + u.fmtRating(r.rating) : '');
    return '<li class="' + rowClass(it.ref, s, 'ls-row-n') + (first ? ' is-first' : '') + '" data-id="' + esc(it.ref) + '">' +
      checkbox(it.ref, r.name, s) +
      '<button class="ls-open"' + openAttrs(it.ref, label, s) + '>' + photoBox(it) +
        '<span class="ls-text">' + nameLine(r.name, 't-list-name-narrow', true) + metaLine(r) + awardsLine(r) + hiddenMark(it.ref, s) + '</span>' +
      '</button>' +
      '<span class="ls-rail">' + rateHtml(r) + actions(it.ref, r.name, s, 'rst') + '</span></li>';
  }

  function rowColumn(it, s, first) {
    var r = it.d;
    var label = r.name + (r.rating ? ' · ' + t('评分') + ' ' + u.fmtRating(r.rating) : '');
    return '<li class="' + rowClass(it.ref, s, 'ls-row-c') + (first ? ' is-first' : '') + '" data-id="' + esc(it.ref) + '">' +
      checkbox(it.ref, r.name, s) +
      '<button class="ls-open"' + openAttrs(it.ref, label, s) + '>' +
        '<span class="ls-emoji" aria-hidden="true">' + ctx.emoji.img(D.genreEmoji(r), 20) + '</span>' +
        '<span class="ls-text">' + nameLine(r.name, 't-list-name-column', false, r) + metaLine(r) + awardsLine(r) + hiddenMark(it.ref, s) + '</span>' +
      '</button>' + actions(it.ref, r.name, s, 'rst') + '</li>';
  }

  /** a saved pin / landmark / vanished restaurant — the other three of M-106's four names. */
  function rowOther(it, s, first) {
    var name, emoji = null, kindLabel;
    if (it.kind === 'pin') { name = D.pinName(it.bm); emoji = (it.bm && it.bm.emoji) || '📍'; kindLabel = t('书签'); }
    else if (it.kind === 'sight') { name = D.landmarkName(it.bm); emoji = (it.bm && it.bm.emoji) || '📍'; kindLabel = t('景点'); }
    // a ref whose restaurant has left the corpus: shown, never silently dropped
    else { name = it.ref; kindLabel = t('不在当前数据里'); }
    var glyph = emoji ? ctx.emoji.img(emoji, 20) : ctx.icon('help');
    return '<li class="' + rowClass(it.ref, s, 'ls-row-c ls-row-other') + (first ? ' is-first' : '') + '" data-id="' + esc(it.ref) + '">' +
      checkbox(it.ref, name, s) +
      '<button class="ls-open"' + openAttrs(it.ref, name, s) + '>' +
        '<span class="ls-emoji" aria-hidden="true">' + glyph + '</span>' +
        '<span class="ls-text">' +
          '<span class="ls-line"><span class="ls-name t-list-name-column clamp-2"' + (D.placeNameLanguage(it.bm, name) ? ' lang="' + D.placeNameLanguage(it.bm, name) + '"' : '') + '>' + esc(name) + '</span></span>' +
          '<span class="ls-meta t-secondary">' + esc(kindLabel) + '</span>' +
        '</span>' +
      '</button>' + actions(it.ref, name, s, it.kind) + '</li>';
  }

  /* ---- cards ------------------------------------------------------------- */
  function emptyResults(s) {
    return '<div class="ls-card ls-empty" role="status">' +
      '<p class="t-group-title">' + t('没有符合条件的餐厅') + '</p>' +
      '<p class="t-secondary">' + t('放宽或重置筛选条件后再试') + '</p>' +
      '<div class="ls-card-actions">' +
        '<button class="btn btn-primary" data-act="to-filters">' + t('修改筛选') + '</button>' +
        '<button class="btn btn-secondary" data-act="reset-filters">' + t('重置筛选') + '</button>' +
      '</div></div>';
  }

  function emptySaved() {
    return '<div class="ls-card ls-empty" role="status">' +
      '<p class="t-group-title">' + t('还没有收藏') + '</p>' +
      '<p class="t-secondary">' + t('在结果里点心形，把餐厅加入收藏') + '</p></div>';
  }

  function oovCard(n) {
    return '<div class="ls-card ls-oov" role="status">' +
      '<p class="t-group-title">' + t('{n} 家不在当前视野', { n: u.fmtCount(n) }) + '</p>' +
      '<p class="t-secondary">' + t('列表仍可浏览；也可以把它们显示在地图上') + '</p>' +
      '<div class="ls-card-actions">' +
        '<button class="btn btn-primary" data-act="oov-show">' + t('返回全部结果') + '</button>' +
        '<button class="btn btn-secondary" data-act="oov-close">' + t('关闭') + '</button>' +
        '<button class="btn btn-quiet" data-act="oov-never">' + t('不再提示') + '</button>' +
      '</div></div>';
  }

  /* ---- footer ------------------------------------------------------------ */
  function renderFoot(s, L, M) {
    if (s.multi.active) {
      var n = s.multi.ids.size, off = n ? '' : ' disabled';
      foot.innerHTML = '<div class="ls-bulk">' +
        '<button class="btn btn-primary btn-fixed" data-act="bulk-fav"' + off + '>' + t('批量收藏') + (n ? ' (' + u.fmtCount(n) + ')' : '') + '</button>' +
        '<button class="btn btn-danger btn-fixed" data-act="bulk-hide"' + off + '>' + t('批量弃用') + (n ? ' (' + u.fmtCount(n) + ')' : '') + '</button>' +
        '</div>';
      return;
    }
    if (s.sheet.tab !== 'results' || !M.length) { foot.innerHTML = ''; return; }
    if (s.filters.bookableOnly) {
      foot.innerHTML = '<div class="ls-bookable">' +
        '<span class="t-control">' + t('正在只看可网订 · {n} 家', { n: u.fmtCount(M.length) }) + '</span>' +
        '<button class="link-btn" data-act="bookable-all">' + t('显示全部结果') + ctx.icon('chevronRight', { cls: 'ic-sm' }) + '</button></div>';
      return;
    }
    var bookable = 0;
    for (var i = 0; i < M.length; i++) { var r = D.byId(M[i]); if (r && r.bookable) bookable += 1; }
    foot.innerHTML = '<div class="ls-bookable">' +
      '<span class="t-control">' + t('其中 {n} 家可网订', { n: u.fmtCount(bookable) }) + '</span>' +
      (bookable ? '<button class="link-btn" data-act="bookable-only">' + t('只看这 {n} 家', { n: u.fmtCount(bookable) }) + ctx.icon('chevronRight', { cls: 'ic-sm' }) + '</button>' : '') +
      '</div>';
  }

  /* ---- toast ------------------------------------------------------------- */
  function renderToast(s) {
    if (!s.toast) { toastRoot.innerHTML = ''; stopToastTimer(); toastTrack.id = null; return; }
    if (toastTrack.id !== s.toast.id) {          // adopt a toast we did not raise (a business toast)
      toastTrack.id = s.toast.id; toastTrack.rev = rev;
      toastTrack.paused = false;
      toastTrack.remaining = typeof s.toast.remainingMs === 'number' ? s.toast.remainingMs : (ctx.motion.toastMs || 9000);
      startToastTimer();
    }
    var total = ctx.motion.toastMs || 9000;
    var pct = Math.max(0, Math.min(100, toastTrack.remaining / total * 100));
    var undoable = s.toast.undo && canUndo(s);
    var action = s.toast.action && typeof s.toast.action.run === 'function' ? s.toast.action : null;
    toastRoot.innerHTML = '<div class="ls-toast toast rise-in" role="status">' +
      '<span class="toast-text">' + t(s.toast.text, { n: u.fmtCount(s.toast.count), k: u.fmtCount((s.toast.undo && s.toast.undo.skipped) || 0) }) + '</span>' +
      (s.toast.undo
        ? '<button class="btn btn-quiet ls-undo" data-act="undo"' + (undoable ? '' : ' disabled') + '>' + ctx.icon('undo', { cls: 'ic-sm' }) + t('撤销') + '</button>'
        : '') +
      (action ? '<button class="btn btn-quiet ls-undo" data-act="toast-action">' + ctx.icon('undo', { cls: 'ic-sm' }) + esc(action.label || t('撤销')) + '</button>' : '') +
      '<button class="icon-btn" data-act="toast-dismiss" aria-label="' + esc(t('关闭')) + '">' + ctx.icon('x') + '</button>' +
      (s.toast.undo && !undoable ? '<span class="ls-undo-note t-secondary">' + t('数据已更新，无法撤销此操作') + '</span>' : '') +
      '<span class="toast-progress" style="--toast-pct:' + pct.toFixed(1) + '%"></span>' +
      '</div>';
  }

  Lm.destroy = function () {
    stopToastTimer();
    if (offScroll) { offScroll(); offScroll = null; }
    curScroller = null;
    root.innerHTML = ''; foot.innerHTML = ''; toastRoot.innerHTML = '';
    lastSig = ''; lastIds = ''; items = []; indexOf = Object.create(null);
  };

  window.App.registerModule('list', Lm);
})();
