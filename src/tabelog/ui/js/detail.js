/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/detail.js
   Owns: the detail body (identity / awards / gallery / summary / reading
   sections / collections / status row) in #detail-root, the fixed action bar
   in #detail-foot, the ⋯ menu item list (rendered by overlays), the lightbox
   content (shell by overlays), and the mid-width candidate strip in
   #candidates-root.

   Ported from ui-captures-2026-09-10/redesign/demo-4.0/js/detail.js. What is
   different in production, and why:

   · **the payload is async and positional.** A row (restaurants.json) only
     carries name / rating / bucket / awards / lat-lon / gpid / gcal /
     bookable / st. Everything else — the Japanese genre string, the two price
     ceilings, seats, station, address, the policy blurb, the photos, the
     award ribbons with their years, the policy struct, the closing days and
     the metres to the station — lives in popups*.json as a **12-slot
     positional array** (CLAUDE.md). `Data.detail(id)` resolves it for the
     active language and length-checks slots 9–11, because a service-worker
     cache written before 2.0.0 still serves 9-slot arrays. So the card paints
     twice: identity + actions from the row immediately, the rest when the
     ~7 MB popups file has landed (skeletons in between, a retry on failure).
   · **原文 / 译文 is two files, not two fields.** Slot 6 is *one* string in
     the language of the file it came from, so the Japanese original of a
     Chinese/English page lives in `data/popups-ja.json`. That file is only
     fetched when the reader actually asks for the original, once per session
     (see `loadJa()`); in a Japanese page the active file already *is* the
     original, so the switch is not drawn at all rather than lying. The
     structured policy rows (slot 9) follow the same switch.
   · **award years come from the ribbons HTML** (slot 8), which is the only
     place the year is recorded; `r.awards` is a bare slug array. Both are
     rendered — the slug badges give the colour family and the localized
     name, the ribbons give the year.
   · Japanese source runs (name, genre, seats, station, address, the original
     policy) are marked `lang="ja"`; the ones a reader plausibly cannot read
     carry the production 翻译 button, which is `act.translateJa` — the same
     Google endpoint 3.2.x used.
   · the main Save action opens the collection picker (overlays' memberPicker)
     and falls back to a plain toggle while overlays has not landed yet.

   Public API (CONTRACT.md §detail):
     Detail.backLabel(state)        → zh key for the origin, or null
     Detail.moreItems(state)        → [{key, icon, label, danger, separatorBefore, run()}]
     Detail.photoRect(index)        → DOMRect of a gallery thumbnail (lightbox FLIP origin)
     Detail.scrollTo(section)       → 'top'|'photos'|'summary'|'address'|'policy'
     Detail.candidates(state)       → ids of the current result set
     Detail.lightboxContent(payload)→ Element for the overlays lightbox shell
   Additions:
     Detail.index(state)            → {k, n, prev, next} for the wide header
     Detail.photoCount(id)          → number of real photos (0 until loaded)
     Detail.actionsStacked()        → narrow action bar fell back to one column
     Detail.shareIsInline()         → 分享 currently sits in the action row
     Detail.detailFor(id)           → the resolved 12-slot payload or null
   Owner of this file and css/detail.css: the detail agent, nobody else.
   ========================================================================== */
(function () {
  'use strict';

  var Dm = { name: 'detail' };
  var ctx, root, foot, cand;
  var bodySig = null, footSig = null, candSig = null;
  var stacked = false, shareInline = true;
  var lbEl = null, lbIndex = 0;
  var lastPhotoIndex = 0;

  /* popups payload cache: 'id|lang' → {st:'loading'|'ok'|'err', det} */
  var detCache = Object.create(null);
  /* the Japanese originals, fetched at most once per session */
  var jaMap = null, jaPromise = null, jaErr = false;

  /* ---------------------------------------------------------------- utils */
  function t(k, p) { return ctx.t(k, p); }
  function esc(s) { return ctx.util.esc(s); }
  function ic(n, o) { return ctx.icon(n, o); }
  function isNarrow(s) { return s.layout.mode === 'narrow'; }
  function modeOf(s) { return s.layout.mode; }
  function matches(s, r) { try { return ctx.Data.filterMatch(r, s.filters); } catch (e) { return true; } }

  /** DETAIL-02: metres → minutes at 80 m/min; no metres means no fake walk time. */
  function walkMin(m) { return Math.max(1, Math.round(Number(m) / 80)); }

  function mapsUrl(r) {
    return 'https://www.google.com/maps/search/?api=1&query=' +
      encodeURIComponent(r.lat + ',' + r.lon) +
      (r.gpid ? '&query_place_id=' + encodeURIComponent(r.gpid) : '');
  }

  /** popups slot 7 is a plain array of URL strings (3.2.x shape). */
  function photosOf(det) {
    if (!det || !Array.isArray(det.photos)) return [];
    return det.photos.filter(function (p) { return typeof p === 'string' && p; });
  }
  function thumb(url) {
    return PhotoUrls.url(url, ((window.devicePixelRatio || 1) > 1.2 || window.innerWidth >= 700) ? 640 : 320);
  }

  /** a yen ceiling from the popups (slot 1 / 2) → the short price-bucket label. */
  function priceOf(v) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return t('未提供');
    return ctx.util.fmtPrice(ctx.Data.priceBucketOf(Number(v)), { short: true });
  }
  function priceTitle(v) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '';
    return ' title="' + esc(ctx.util.fmtYen(Number(v))) + '"';
  }

  /* ------------------------------------------------- async 12-slot payload */

  function cacheKey(id, lang) { return id + '|' + lang; }

  /** ensure(id) — kick the popups fetch once per id+language; repaint on landing. */
  function ensureDetail(id, lang) {
    var k = cacheKey(id, lang);
    var hit = detCache[k];
    if (hit) return hit;
    detCache[k] = { st: 'loading', det: null };
    ctx.Data.detail(id).then(function (d) {
      detCache[k] = { st: 'ok', det: d };
      bodySig = null;
      ctx.App.requestRender('detail:data');
    }, function () {
      detCache[k] = { st: 'err', det: null };
      bodySig = null;
      ctx.App.requestRender('detail:data');
    });
    return detCache[k];
  }
  Dm.detailFor = function (id) {
    var e = detCache[cacheKey(id, ctx.App.state.lang)];
    return e && e.st === 'ok' ? e.det : null;
  };

  /**
   * loadJa() — the Japanese originals. popups-ja.json is the only place the
   * untranslated policy exists, and it is ~8 MB, so it is never part of the
   * first paint: this runs the first time a reader presses 原文 and the result
   * is reused for the rest of the session.
   */
  function loadJa() {
    if (jaMap) return Promise.resolve(jaMap);
    if (jaPromise) return jaPromise;
    jaPromise = fetch('data/popups-ja.json', { cache: 'force-cache' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (j) { jaMap = j; jaErr = false; bodySig = null; ctx.App.requestRender('detail:ja'); return j; })
      .catch(function (e) {
        jaPromise = null; jaErr = true; bodySig = null;
        ctx.App.requestRender('detail:ja');
        throw e;
      });
    return jaPromise;
  }
  /** the ja 12-slot array for one id, or null while it is still loading. */
  function jaSlots(id) {
    var arr = jaMap && jaMap[id];
    return Array.isArray(arr) ? arr : null;
  }

  /* ------------------------------------------------------------ public API */

  Dm.backLabel = function (s) {
    return { results: '返回结果', saved: '返回收藏', search: '返回搜索', filters: '返回结果' }[s.selected.origin] || null;
  };

  Dm.moreItems = function (s) {
    var id = s.selected.id;
    var r = id ? ctx.Data.byId(id) : null;
    if (!r) return [];
    var hidden = s.user.black.has(id);
    var items = [];
    if (!shareInline || s.layout.mode !== 'wide') {
      items.push({ key: 'share', icon: 'share', label: '分享这家店', run: function () { doShare(id, null); } });
    }
    items.push({
      key: 'lists', icon: 'folderPlus', label: '加入收藏夹',
      run: function () { ctx.act.closeOverlay('done'); openMemberPicker(id); }
    });
    items.push({
      key: 'source', icon: 'external', label: '在 Tabelog 打开',
      run: function () { window.open(id, '_blank', 'noopener'); ctx.act.closeOverlay('done'); }
    });
    items.push({
      key: 'hide', icon: 'hide', separatorBefore: true,
      label: hidden ? '恢复显示这间店' : '弃用这间店', danger: !hidden,
      run: function () { setHidden(id, !hidden); ctx.act.closeOverlay('done'); }
    });
    return items;
  };

  Dm.photoRect = function (i) {
    var el = root.querySelector('[data-photo="' + i + '"]');
    return el ? el.getBoundingClientRect() : null;
  };
  Dm.photoCount = function (id) { return photosOf(Dm.detailFor(id)).length; };
  Dm.actionsStacked = function () { return stacked; };
  Dm.shareIsInline = function () { return shareInline; };

  /** LAY-05: never touch scrollTop while geometry is still moving. */
  Dm.scrollTo = function (sec) {
    var name = (sec === 0 || sec === 'top') ? 'top' : sec;
    ctx.motion.afterGeometry(function () {
      var sc = window.Containers && window.Containers.scroller && window.Containers.scroller('detail');
      if (!sc) return;
      if (name === 'top') { sc.scrollTop = 0; return; }
      var el = root.querySelector('[data-section="' + name + '"]');
      if (!el) return;
      var top = sc.scrollTop + el.getBoundingClientRect().top - sc.getBoundingClientRect().top - 8;
      sc.scrollTop = Math.max(0, top);
    });
  };

  /**
   * §10.4: the candidate strip browses the current result set, nothing new.
   * Memoised — containers calls this on every render for its pager, and the
   * production set is ~8k ids, so re-sorting it each frame is not free.
   * `Data.applyFilters` hands back the *same array* while the filters are
   * unchanged, which makes reference identity a sound cache key.
   */
  var _cand = { ref: null, sort: null, from: '', ids: null };
  Dm.candidates = function (s) {
    var D = ctx.Data;
    var base = D.applyFilters(s.filters);
    var from = nearFrom(s), fk = from ? from.join(',') : '';
    if (_cand.ids && _cand.ref === base && _cand.sort === s.sort && _cand.from === fk) return _cand.ids;
    var ids = D.sort(base, s.sort, { from: from });
    _cand = { ref: base, sort: s.sort, from: fk, ids: ids };
    return ids;
  };
  function nearFrom(s) {
    var f = s.nearby && s.nearby.fix;
    if (f && typeof f.lat === 'number' && typeof f.lon === 'number') return [f.lat, f.lon];
    return null;
  }

  Dm.index = function (s) {
    var ids = Dm.candidates(s), i = ids.indexOf(s.selected.id);
    if (i < 0) return { k: 0, n: ids.length, prev: null, next: null };
    return { k: i + 1, n: ids.length, prev: i > 0 ? ids[i - 1] : null, next: i < ids.length - 1 ? ids[i + 1] : null };
  };

  /* ------------------------------------------------------------- intents */
  function setHidden(id, on) {
    ctx.act.setBlack(id, on);
    ctx.App.emit('detail:hide', { id: id, on: !!on });
  }
  function doShare(id, ev) {
    if (ctx.act.share) { ctx.act.share(id, ev || null); return; }
    ctx.act.openOverlay('share', { id: id });
  }

  /**
   * openMemberPicker(id) — the Save button asks *which* collection
   * (DESIGN §9.3). overlays owns that sheet; while it is still a stub the
   * overlay root stays empty, in which case this degrades to the plain
   * default-collection toggle rather than doing nothing at all.
   */
  function openMemberPicker(id) {
    ctx.act.openOverlay('memberPicker', { id: id });
    ctx.util.raf(function () {
      if (ctx.App.state.overlay.kind !== 'memberPicker') return;
      if (overlayRendered()) return;
      ctx.act.closeOverlay('fallback');
      var added = ctx.act.toggleFav(id);
      ctx.App.emit('detail:fav', { id: id, added: added });
    });
  }
  function overlayRendered() {
    var host = document.getElementById('overlay-root');
    var modal = document.getElementById('modal-root');
    return !!(realChild(host) || realChild(modal));
  }
  function realChild(host) {
    if (!host) return false;
    for (var i = 0; i < host.children.length; i++) {
      if (!host.children[i].hasAttribute('data-stub')) return true;
    }
    return false;
  }

  /* -------------------------------------------------------------- sections */

  /**
   * a Japanese source run plus the production 翻译 button (act.translateJa).
   * A Japanese page is already reading the original, so it gets the run and no
   * button — translating ja into ja would be a round trip to nowhere.
   */
  function jaRun(text, cls) {
    if (!text) return '';
    var run = '<span class="dt-ja' + (cls ? ' ' + cls : '') + '" lang="ja">' + esc(text) + '</span>';
    if (ctx.App.state.lang === 'ja') return run;
    return '<span class="dt-ja-wrap">' + run +
      '<button type="button" class="dt-tx-btn t-secondary" data-act="tx" aria-label="' + esc(t('翻译这段日文')) + '">' +
      ic('translate', { cls: 'ic-sm' }) + '<span class="dt-tx-label">' + esc(t('翻译')) + '</span></button></span>';
  }

  function identityHtml(s, r, det) {
    var titleCls = s.layout.mode === 'wide' ? 't-detail-title-wide' : 't-detail-title';
    var meta = [];
    meta.push(r.rating === null || r.rating === undefined
      ? '<span class="rating-missing">' + t('评分暂无') + '</span>'
      : '<span class="rating num">' + ic('starFill', { fill: true, cls: 'ic-sm' }) + ctx.util.fmtRating(r.rating) + '</span>');
    var bucketName = (r.categories && r.categories[0]) ? t(r.categories[0]) : '';
    if (bucketName) meta.push('<span class="dt-genre">' + esc(bucketName) + '</span>');
    meta.push('<span class="price-text num">' + ctx.util.fmtPrice(r.bucket, { short: true }) + '</span>');

    var ribbonHost = document.createElement('div');
    ribbonHost.innerHTML = (det && det.ribbons) || '';
    var dated = Object.create(null);
    function family(slug) { return /gold|silver|bronze/.test(slug) ? 'award' : (/hyaku/.test(slug) ? 'hyaku' : slug); }
    Array.prototype.forEach.call(ribbonHost.querySelectorAll('.rst-ribbon'), function (el) {
      var m = el.className.match(/rst-ribbon-([\w-]+)/);
      if (m && /20\d{2}/.test(el.textContent)) dated[family(m[1])] = true;
    });
    var awards = ctx.Data.awardObjs(r).filter(function (a) { return !dated[family(a.slug)]; }).map(function (a) {
      return '<span class="badge badge-lg badge-' + esc(a.slug) + '">' + esc(ctx.Data.awardLabel(a)) + '</span>';
    }).join('');
    var ribbons = ribbonHost.innerHTML ? '<div class="dt-ribbons">' + ribbonHost.innerHTML + '</div>' : '';

    var tools = '';
    if (s.layout.mode !== 'wide') {
      tools = '<button class="icon-btn dt-more" data-act="more" data-ov="open" data-kind="more" aria-label="' + t('更多操作') + '" aria-haspopup="menu" aria-expanded="' +
        (s.overlay.kind === 'more') + '">' + ic('more') + '</button>' +
        ((window.Containers && window.Containers.detailTools) ? window.Containers.detailTools(s) : '');
    }
    var more = tools ? '<span class="dt-tools">' + tools + '</span>' : '';

    // the raw Tabelog genre string is kana-heavy — it gets the 翻译 button.
    var genreLine = (det && det.genreText)
      ? '<div class="dt-genre-ja t-secondary">' + jaRun(det.genreText) + '</div>' : '';

    return '<header class="dt-identity">' +
      '<div class="dt-title-row"><h1 class="dt-title ' + titleCls + '" data-section="top" lang="ja">' + esc(r.name) + '</h1>' + more + '</div>' +
      '<div class="dt-meta t-body">' + meta.join('<span class="dt-sep" aria-hidden="true"></span>') + '</div>' +
      genreLine +
      (awards ? '<div class="dt-awards badge-row badge-row-lg">' + awards + '</div>' : '') +
      ribbons +
      '</header>';
  }

  /** DETAIL-03 gallery: narrow = snapping strip, W≥750 = one large + two small. */
  function galleryHtml(s, det, loading) {
    var narrow = isNarrow(s);
    if (loading) {
      return '<div class="dt-gallery is-empty" data-section="photos" aria-busy="true">' +
        '<div class="photo-placeholder dt-photo-empty skeleton"></div></div>';
    }
    var ph = photosOf(det), n = ph.length;
    if (!n) {
      return '<div class="dt-gallery is-empty" data-section="photos">' +
        '<div class="photo-placeholder dt-photo-empty">' + ic('image') + '<span>' + t('这家店暂无照片') + '</span></div></div>';
    }
    var cls = narrow ? 'is-strip' : ('is-grid n' + Math.min(n, 3));
    var cells = ph.map(function (p, i) {
      return '<button type="button" class="dt-photo" data-photo="' + i + '" aria-label="' + t('查看照片 {i} / {n}', { i: i + 1, n: n }) + '">' +
        '<img class="dt-photo-img" src="' + esc(thumb(p)) + '" alt="" loading="lazy" decoding="async"' +
        '' + '></button>';
    }).join('');
    var all = (!narrow && n > 1)
      ? '<button type="button" class="dt-gallery-all" data-act="lightbox" data-index="0">' + ic('images', { cls: 'ic-sm' }) + '<span>' + t('查看全部照片') + '</span></button>'
      : '';
    return '<div class="dt-gallery ' + cls + '" data-section="photos" role="group" aria-label="' + t('照片') + '">' + cells + all + '</div>';
  }

  /** VIS-03 summary: dinner + lunch share one subtle surface, booking its own. */
  function summaryHtml(s, r, det, loading) {
    function cell(icon, label, value, extraAttr) {
      return '<div class="dt-sum-cell"' + (extraAttr || '') + '>' + ic(icon, { cls: 'ic-sm' }) +
        '<div class="dt-sum-text"><div class="dt-sum-label t-secondary">' + t(label) + '</div>' +
        '<div class="dt-sum-val num">' + esc(value) + '</div></div></div>';
    }
    var dinner = loading ? '…' : priceOf(det && det.dinnerUpper);
    var lunch = loading ? '…' : priceOf(det && det.lunchUpper);
    return '<div class="dt-summary" data-section="summary">' +
      '<div class="dt-sum-budget card-subtle">' +
      cell('moon', '人均晚餐', dinner, loading ? '' : priceTitle(det && det.dinnerUpper)) +
      cell('sun', '人均午餐', lunch, loading ? '' : priceTitle(det && det.lunchUpper)) +
      '</div>' +
      '<div class="dt-sum-book">' +
      cell('calendar', 'Tabelog 预订', t(r.bookable ? '有 Tabelog 预订入口' : '无 Tabelog 预订入口')) +
      '</div></div>';
  }

  function infoRow(icon, label, valueHtml, opts) {
    return '<div class="dt-row' + (opts && opts.cls ? ' ' + opts.cls : '') + '">' +
      ic(icon, { cls: 'ic-sm' }) +
      '<div class="dt-row-text t-long-body">' +
      (label ? '<span class="dt-row-label t-secondary">' + t(label) + '</span>' : '') +
      '<span class="dt-row-val">' + valueHtml + '</span>' +
      '</div></div>';
  }
  function quiet(text) { return '<span class="dt-missing t-secondary">' + esc(text) + '</span>'; }

  /** DETAIL-02 getting there: station (+ walk), seats, full address, closing days. */
  function placeHtml(s, r, det, loading) {
    var station = (det && det.station) || r.st || '';
    var stationHtml;
    if (!station) stationHtml = quiet(t('未提供'));
    else {
      stationHtml = jaRun(station);
      if (det && det.stationM !== null && det.stationM !== undefined && !isNaN(Number(det.stationM))) {
        stationHtml += '<span class="dt-walk t-secondary num"> · ' + esc(t('步行 {n} 分', { n: walkMin(det.stationM) })) +
          ' · ' + esc(ctx.util.fmtDistance(Number(det.stationM))) + '</span>';
      }
    }
    var seat = det && det.seat ? jaRun(det.seat) : quiet(loading ? t('正在载入…') : t('未提供'));
    var addr = det && det.address
      ? jaRun(det.address) + '<button type="button" class="link-btn dt-copy t-control" data-act="copy-addr">' + ic('copy', { cls: 'ic-sm' }) + esc(t('复制')) + '</button>'
      : quiet(loading ? t('正在载入…') : t('未提供'));
    var rows = infoRow('pin', null, stationHtml) +
      infoRow('seat', '座位', seat) +
      infoRow('map', null, addr);
    if (det && det.holiday) rows += infoRow('clock', '休息日', jaRun(det.holiday));
    return '<section class="dt-sec" data-section="address">' +
      '<h2 class="t-group-title">' + t('位置与座位') + '</h2>' +
      '<div class="dt-sec-body">' + rows + '</div></section>';
  }

  /**
   * DETAIL-02 预约信息 + the 原文 / 译文 switch.
   * `det` is the active language's payload; `ja` the Japanese original, which
   * is only present after the reader asked for it (loadJa).
   */
  function policyHtml(s, r, det, loading) {
    var uiJa = s.lang === 'ja';
    var wantJa = !uiJa && s.detail.translation === 'ja';
    var ja = wantJa ? jaSlots(r.id) : null;

    // structured rows follow the switch (slot 9, length-checked by Data.detail)
    var pol = det ? det.policy : null;
    var polJa = ja && ja.length > 9 ? (ja[9] || null) : null;
    var shown = wantJa ? polJa : pol;
    var shownLang = wantJa ? 'ja' : null;

    var rows = infoRow('calendar', '能否预订', esc(ctx.Data.policyLabel(wantJa ? { policy: polJa } : det)));
    function ln(key, v) { return v ? infoRow('info', key, wrapLang(v, shownLang)) : ''; }
    if (shown) {
      rows += ln('提前预订', shown.lead);
      rows += ln('取消政策', shown.cancel);
      rows += ln('说明', shown.note);
    }
    rows += infoRow('external', 'Tabelog 预订',
      '<a class="dt-link" href="' + esc(r.id) + '" target="_blank" rel="noopener">' +
      esc(t(r.bookable ? '有 Tabelog 预订入口' : '无 Tabelog 预订入口')) + '</a>');

    // the long blurb
    var body, bodyLang = null, missingKey = null;
    if (loading) { body = null; missingKey = '正在载入…'; }
    else if (wantJa) {
      if (jaErr) { body = null; missingKey = '原文加载失败'; }
      else if (!jaMap) { body = null; missingKey = '正在载入原文…'; }
      else { body = (ja && ja[6]) || null; bodyLang = 'ja'; if (!body) missingKey = '未提供原文'; }
    } else {
      body = (det && det.policyText) || null;
      bodyLang = uiJa ? 'ja' : null;
      if (!body) missingKey = uiJa ? '未提供原文' : '未提供译文';
    }
    var textHtml = body
      ? '<p class="dt-policy-text t-long-body"' + (bodyLang ? ' lang="ja"' : '') + '>' + esc(body) + '</p>'
      : '<p class="dt-policy-text t-long-body is-missing">' + esc(t(missingKey || '未提供译文')) + '</p>';

    var sw = uiJa ? '' :
      '<div class="dt-trans segmented segmented-joined" role="group" aria-label="' + t('原文与译文') + '">' +
      '<button type="button" class="t-control" data-act="trans" data-trans="zh" aria-pressed="' + (!wantJa) + '">' + t('译文') + '</button>' +
      '<button type="button" class="t-control" data-act="trans" data-trans="ja" aria-pressed="' + wantJa + '">' + t('原文') + '</button></div>';
    var tag = '<span class="dt-lang-tag t-secondary">' +
      esc(uiJa ? t('日文原文') : (wantJa ? t('日文原文') : t('机器翻译'))) + '</span>';

    return '<section class="dt-sec" data-section="policy">' +
      '<div class="dt-sec-head"><h2 class="t-group-title">' + t('预约信息') + '</h2>' + sw + '</div>' +
      '<div class="dt-sec-body">' + rows + tag + textHtml +
      '<p class="dt-note t-secondary">' + t('网订入口 ≠ 余位，请到 Tabelog 确认') + '</p>' +
      '</div></section>';
  }
  function wrapLang(v, lang) {
    return lang ? '<span lang="ja">' + esc(v) + '</span>' : esc(v);
  }

  /** 收藏夹: which collections this restaurant is in, and the way to change it. */
  function listsHtml(s, r) {
    var lists = [];
    try { lists = ctx.Data.listsOf(s.user.bookmarks, r.id) || []; } catch (e) { lists = []; }
    var fav = s.user.fav.has(r.id);
    var chips = lists.map(function (l) {
      return '<span class="chip is-on dt-list-chip">' + ctx.emoji.img(l.emoji || '📁', 14) +
        '<span class="dt-list-name">' + esc(l.name || '') + '</span></span>';
    }).join('');
    var note = lists.length ? '' :
      '<span class="t-secondary dt-list-none">' + esc(t(fav ? '在默认收藏夹' : '尚未加入任何收藏夹')) + '</span>';
    return '<section class="dt-sec dt-lists" data-section="lists">' +
      '<div class="dt-sec-head"><h2 class="t-group-title">' + t('收藏夹') + '</h2>' +
      '<button type="button" class="link-btn t-control" data-act="member">' + ic('folderPlus', { cls: 'ic-sm' }) + esc(t('管理收藏夹')) + '</button></div>' +
      '<div class="dt-sec-body"><div class="dt-list-chips">' + chips + note + '</div></div></section>';
  }

  /** Status row: calibration is a status line, not a button (DETAIL-02). */
  function statusHtml(s, r) {
    var items = [];
    if (r.gcal === 1) {
      items.push('<span class="dt-status-item is-ok">' + ic('check', { cls: 'ic-sm' }) + t('Google 坐标已校准') + '</span>');
    }
    items.push('<a class="dt-status-item is-link" href="' + esc(r.id) + '" target="_blank" rel="noopener">' + ic('external', { cls: 'ic-sm' }) + t('查看原文') + '</a>');
    return '<div class="dt-status">' + items.join('<span class="dt-sep" aria-hidden="true"></span>') + '</div>';
  }

  /** mid/wide "第 k / N 家" stepper. containers owns the column header; while it
   *  does not draw one, the body carries the stepper so D4 stays reachable. */
  function indexHtml(s) {
    if (isNarrow(s)) return '';
    // containers draws 第 k / N 家 in #col-detail-head as soon as the real
    // module is up (detailTools is the signal); two pagers would be a bug.
    if (window.Containers && typeof window.Containers.detailTools === 'function') return '';
    var ix = Dm.index(s);
    if (!ix.n) return '';
    return '<div class="dt-index">' +
      '<button type="button" class="icon-btn dt-index-btn" data-act="prev" ' + (ix.prev ? '' : 'disabled ') +
      'aria-label="' + t('上一家') + '">' + ic('chevronUp') + '</button>' +
      '<span class="dt-index-pos t-secondary num">' + esc(t('第 {k} / {n} 家', { k: ix.k, n: ctx.util.fmtCount(ix.n) })) + '</span>' +
      '<button type="button" class="icon-btn dt-index-btn" data-act="next" ' + (ix.next ? '' : 'disabled ') +
      'aria-label="' + t('下一家') + '">' + ic('chevronDown') + '</button>' +
      '</div>';
  }

  function bodyHtml(s, r, entry) {
    var det = entry.st === 'ok' ? entry.det : null;
    var loading = entry.st === 'loading';
    var notes = '';
    var hidden = s.user.black.has(r.id);
    if (!matches(s, r) && !hidden) {
      notes += '<p class="dt-quiet-note t-secondary" role="status">' + t('这家店不在当前筛选结果中') + '</p>';
    }
    if (hidden) {
      notes += '<div class="dt-hidden-note t-secondary">' + ic('hide', { cls: 'ic-sm' }) +
        '<span>' + t('已弃用：不出现在结果里') + '</span>' +
        '<button type="button" class="link-btn t-control" data-act="unhide">' + t('恢复显示') + '</button></div>';
    }
    if (entry.st === 'err') {
      notes += '<div class="dt-hidden-note t-secondary">' + ic('warning', { cls: 'ic-sm' }) +
        '<span>' + t('详细信息加载失败') + '</span>' +
        '<button type="button" class="link-btn t-control" data-act="retry-detail">' + t('重试') + '</button></div>';
    }
    return '<article class="dt">' + indexHtml(s) + notes +
      identityHtml(s, r, det) + galleryHtml(s, det, loading) + summaryHtml(s, r, det, loading) +
      placeHtml(s, r, det, loading) + policyHtml(s, r, det, loading) + listsHtml(s, r) + statusHtml(s, r) +
      '</article>';
  }

  /* ------------------------------------------------------ fixed action bar */

  function favLabel(s, r) { return t(s.user.fav.has(r.id) ? '已收藏' : '加入收藏'); }

  function actionsHtml(s, r) {
    var narrow = s.layout.mode !== 'wide';
    var maps = mapsUrl(r);
    var fav = s.user.fav.has(r.id);
    if (narrow) {
      return '<div class="dt-actions' + (stacked ? ' is-stack' : '') + '">' +
        '<button type="button" class="btn btn-secondary btn-fixed btn-stack dt-act dt-act-side dt-act-fav' + (fav ? ' is-on' : '') + '" ' +
        'data-act="fav" aria-pressed="' + fav + '">' + ic('heart', fav ? { fill: true } : null) +
        '<span class="dt-act-label t-control">' + favLabel(s, r) + '</span></button>' +
        '<a class="btn btn-primary btn-fixed dt-act dt-act-main" href="' + esc(maps) + '" target="_blank" rel="noopener" ' +
        'aria-label="' + t('在Google Map 打开') + '">' +
        '<img class="dt-gmaps" src="img/google-maps-v2.png" alt="" width="22" height="22">' +
        '<span class="dt-act-label dt-act-label-wrap t-body">Google Maps</span></a>' +
        '<a class="btn btn-secondary btn-fixed btn-stack dt-act dt-act-side" href="' + esc(r.id) + '" target="_blank" rel="noopener" ' +
        'aria-label="' + t('在 Tabelog 打开') + '">' + '<span class="dt-tabelog" aria-hidden="true"><img src="img/tabelog-logo.webp" alt=""></span>' +
        '<span class="dt-act-label t-control">Tabelog</span></a>' +
        '</div>';
    }
    var parts = [];
    parts.push('<button type="button" class="btn btn-primary btn-fixed dt-act-row dt-act-save" data-act="fav" aria-pressed="' + fav + '">' +
      ic('heart', fav ? { fill: true } : null) + '<span class="dt-act-label">' + favLabel(s, r) + '</span></button>');
    parts.push('<a class="btn btn-secondary btn-fixed dt-act-row" href="' + esc(maps) + '" target="_blank" rel="noopener" aria-label="' + t('在Google Map 打开') + '">' +
      '<img class="dt-gmaps" src="img/google-maps-v2.png" alt="" width="20" height="20"><span class="dt-act-label">Google Maps</span></a>');
    if (shareInline && s.layout.mode === 'wide') {
      parts.push('<button type="button" class="btn btn-secondary btn-fixed dt-act-row dt-act-share" data-act="share">' +
        ic('share') + '<span class="dt-act-label">' + t('分享') + '</span></button>');
    }
    parts.push('<a class="btn btn-secondary btn-fixed dt-act-row" href="' + esc(r.id) + '" target="_blank" rel="noopener" aria-label="' + t('在 Tabelog 打开') + '">' +
      '<span class="dt-tabelog" aria-hidden="true"><img src="img/tabelog-logo.webp" alt=""></span>' + '<span class="dt-act-label">Tabelog</span></a>');
    parts.push('<button type="button" class="btn btn-secondary btn-fixed dt-act-row dt-act-more" data-act="more" data-ov="open" data-kind="more" aria-label="' + t('更多操作') + '" ' +
      'aria-haspopup="menu" aria-expanded="' + (s.overlay.kind === 'more') + '">' + ic('more') + '</button>');
    return '<div class="dt-actions is-row">' + parts.join('') + '</div>';
  }

  /* ----------------------------------------------------- candidate strip */

  function candidateHtml(s) {
    var ids = Dm.candidates(s), D = ctx.Data;
    if (!ids.length) return '';
    var show = ids.slice(0, 60);          // the track scrolls; 10k cards do not
    var cards = show.map(function (id) {
      var r = D.byId(id); if (!r) return '';
      var media = '<span class="dt-cand-img is-empty">' + ctx.emoji.img(D.genreEmoji(r), 22) + '</span>';
      var on = id === s.selected.id;
      return '<button type="button" class="dt-cand-card' + (on ? ' is-on' : '') + '" data-cand="' + esc(id) + '"' +
        (on ? ' aria-current="true"' : '') + '>' + media +
        '<span class="dt-cand-name t-list-name-column clamp-1" lang="ja">' + esc(r.name) + '</span>' +
        '<span class="dt-cand-meta"><span class="rating num">' + ic('starFill', { fill: true, cls: 'ic-sm' }) +
        (r.rating === null || r.rating === undefined ? t('评分暂无') : ctx.util.fmtRating(r.rating)) + '</span>' +
        '<span class="price-text num">' + ctx.util.fmtPrice(r.bucket, { short: true }) + '</span></span></button>';
    }).join('');
    return '<section class="dt-cand" aria-label="' + t('符合筛选的餐厅') + '">' +
      '<div class="dt-cand-head">' +
      '<span class="dt-cand-title t-control">' + ic('cutlery', { cls: 'ic-sm' }) + t('符合筛选的餐厅') + '</span>' +
      '<span class="dt-cand-tools">' +
      '<button type="button" class="link-btn t-control" data-act="cand-all">' + t('查看全部（{n}）', { n: ctx.util.fmtCount(ids.length) }) + '</button>' +
      '<span class="dt-sep" aria-hidden="true"></span>' +
      '<button type="button" class="link-btn t-control" data-act="cand-close">' + t('收起') + ic('chevronRight', { cls: 'ic-sm' }) + '</button>' +
      '</span></div>' +
      '<div class="dt-cand-row">' +
      '<button type="button" class="icon-btn dt-cand-nav" data-act="cand-prev" aria-label="' + t('上一批') + '">' + ic('chevronLeft') + '</button>' +
      '<div class="dt-cand-track">' + cards + '</div>' +
      '<button type="button" class="icon-btn dt-cand-nav" data-act="cand-next" aria-label="' + t('下一批') + '">' + ic('chevronRight') + '</button>' +
      '</div></section>';
  }

  /* ------------------------------------------------------------- lightbox */

  Dm.lightboxContent = function (payload) {
    var s = ctx.App.state;
    var id = (payload && payload.id) || s.selected.id;
    var r = ctx.Data.byId(id);
    if (!r) return null;
    var ph = photosOf(Dm.detailFor(id));
    if (!ph.length) return null;
    lbIndex = ctx.util.clamp((payload && payload.index) || 0, 0, ph.length - 1);

    var el = ctx.util.h('div.dt-lb', { tabindex: '-1', role: 'group', 'aria-label': r.name });
    el.innerHTML =
      '<button type="button" class="icon-btn dt-lb-btn dt-lb-close" data-lb="close" aria-label="' + t('关闭') + '">' + ic('x') + '</button>' +
      '<button type="button" class="icon-btn dt-lb-btn dt-lb-prev" data-lb="prev" aria-label="' + t('上一张') + '">' + ic('chevronLeft') + '</button>' +
      '<figure class="dt-lb-fig"><img class="dt-lb-img" alt="" src="' + esc(PhotoUrls.url(ph[lbIndex], 640)) + '">' +
      '<figcaption class="dt-lb-cap t-control"><span class="dt-lb-name" lang="ja">' + esc(r.name) + '</span>' +
      '<span class="dt-lb-count num" data-lb-count>' + t('照片 {i} / {n}', { i: lbIndex + 1, n: ph.length }) + '</span></figcaption></figure>' +
      '<button type="button" class="icon-btn dt-lb-btn dt-lb-next" data-lb="next" aria-label="' + t('下一张') + '">' + ic('chevronRight') + '</button>';

    var img = el.querySelector('.dt-lb-img');
    var count = el.querySelector('[data-lb-count]');
    function show(i) {
      lbIndex = (i + ph.length) % ph.length;
      var failed = el.querySelector('.dt-photo-failed');
      if (failed) failed.remove();
      img._photoAttempt = null; img.style.visibility = '';
      img.src = PhotoUrls.url(ph[lbIndex], 640);
      count.textContent = t('照片 {i} / {n}', { i: lbIndex + 1, n: ph.length });
      lastPhotoIndex = lbIndex;
      ctx.App.set({ overlay: { payload: { id: id, index: lbIndex } } }, { silent: true });
    }
    Dm.lightboxShow = show;
    el.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-lb]') : null;
      if (!b) return;
      var k = b.dataset.lb;
      if (k === 'close') ctx.act.closeOverlay('close');
      else if (k === 'prev') show(lbIndex - 1);
      else if (k === 'next') show(lbIndex + 1);
      else if (k === 'retry') show(lbIndex);
    });
    el.addEventListener('keydown', function (e) {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === 'ArrowLeft') { e.preventDefault(); show(lbIndex - 1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); show(lbIndex + 1); }
    });
    var sx = null;
    el.addEventListener('pointerdown', function (e) { sx = e.clientX; });
    el.addEventListener('pointerup', function (e) {
      if (sx === null) return;
      var dx = e.clientX - sx; sx = null;
      if (Math.abs(dx) > 40) show(lbIndex + (dx < 0 ? 1 : -1));
    });
    if (ph.length < 2) {
      el.querySelector('.dt-lb-prev').hidden = true;
      el.querySelector('.dt-lb-next').hidden = true;
    }
    bindPhotoFallbacks(el);
    lbEl = el;
    setTimeout(function () { try { el.focus(); } catch (e2) { /* ignore */ } }, 0);
    return el;
  };

  /* --------------------------------------------------------- measurements */

  function measureActions(s, pass) {
    pass = pass || 0;
    var row = foot.querySelector('.dt-actions');
    if (!row || !row.clientWidth) return;
    if (s.layout.mode !== 'wide') {
      if (stacked) return;
      var overflow = false;
      Array.prototype.forEach.call(row.querySelectorAll('.dt-act-label'), function (l) {
        if (l.scrollWidth > l.clientWidth + 1) overflow = true;
        if (l.classList.contains('dt-act-label-wrap') && l.scrollHeight > l.clientHeight + 1) overflow = true;
      });
      if (overflow) { stacked = true; footSig = null; paintFoot(s); }
      return;
    }
    row.classList.remove('is-two-row'); row.classList.remove('is-col');
    // DEVICE PASS (DETAIL-01): the mid/wide row only ever looked at whether it
    // WRAPPED. .dt-act-label is overflow:hidden, so at 130% / 200% the labels
    // were clipped ("Google Map…") while every button still reported its basis
    // width — nothing wrapped, no fallback fired, and the flex row itself ran
    // 12px past the 419px detail column. Truncation is pressure, same as a wrap.
    if (!isWrapped(row) && !clippedLabel(row)) return;
    if (shareInline && s.layout.mode === 'wide' && pass < 2) {
      shareInline = false; footSig = null; paintFoot(s);
      ctx.util.raf(function () { measureActions(ctx.App.state, pass + 1); });
      return;
    }
    row.classList.add('is-two-row');
    // two rows still cut the label (a 419px column at 200%) → one per row.
    // DETAIL-01 forbids shrinking the type, so the column is what gives.
    if (clippedLabel(row)) row.classList.add('is-col');
  }
  function clippedLabel(row) {
    var labels = row.querySelectorAll('.dt-act-label'), clipped = false;
    for (var i = 0; i < labels.length; i++) {
      if (labels[i].scrollWidth > labels[i].clientWidth + 1) clipped = true;
    }
    return clipped;
  }
  function isWrapped(row) {
    var kids = row.children, top = null;
    for (var i = 0; i < kids.length; i++) {
      var o = kids[i].offsetTop;
      if (top === null) top = o;
      else if (Math.abs(o - top) > 2) return true;
    }
    return false;
  }

  function measureSummary() {
    var sum = root.querySelector('.dt-summary');
    if (!sum || !sum.clientWidth) return;
    sum.classList.remove('is-stacked', 'is-stacked-budget');
    if (!overflowsSummary(sum)) return;
    sum.classList.add('is-stacked');
    if (overflowsSummary(sum)) sum.classList.add('is-stacked-budget');
  }
  function overflowsSummary(sum) {
    var over = false;
    Array.prototype.forEach.call(sum.querySelectorAll('.dt-sum-label, .dt-sum-val'), function (el) {
      if (el.scrollWidth > el.clientWidth + 1) over = true;
    });
    return over;
  }

  function afterPaintMeasure() {
    ctx.util.raf(function () {
      if (!ctx.App.state.selected.id) return;
      measureActions(ctx.App.state);
      measureSummary();
    });
  }

  /* -------------------------------------------------------------- painting */

  function bindPhotoFallbacks(host) {
    Array.prototype.forEach.call(host.querySelectorAll('img.dt-photo-img, img.dt-cand-img, img.dt-lb-img'), function (img) {
      var frame = img.parentElement;
      if (frame && !img.complete) {
        frame.classList.add('skeleton');
        img.addEventListener('load', function () { frame.classList.remove('skeleton'); }, { once: true });
      }
      img.addEventListener('error', function () {
        if (!frame) return;
        frame.classList.remove('skeleton');
        if (!frame.querySelector('.dt-photo-failed')) {
          var note = document.createElement(img.classList.contains('dt-lb-img') ? 'button' : 'span');
          if (note.tagName === 'BUTTON') { note.type = 'button'; note.setAttribute('data-lb', 'retry'); }
          note.className = 'dt-photo-failed t-secondary';
          note.textContent = t('照片暂不可用') + (note.tagName === 'BUTTON' ? ' · ' + t('重试') : '');
          frame.appendChild(note);
        }
      });
    });
  }

  /** only the wide body's own stepper depends on the index; skip the work
   *  entirely once containers draws the pager in the column header. */
  function indexSig(s) {
    if (window.Containers && typeof window.Containers.detailTools === 'function') return '';
    var ix = Dm.index(s);
    return ix.k + '/' + ix.n;
  }

  function paintBody(s, r) {
    var entry = ensureDetail(r.id, s.lang);
    var sig = [r.id, s.lang, s.fontScale, modeOf(s), s.detail.translation,
      s.user.black.has(r.id) ? 1 : 0, matches(s, r) ? 1 : 0, s.overlay.kind === 'more' ? 1 : 0,
      entry.st, jaMap ? 1 : 0, jaErr ? 1 : 0, s.user.fav.has(r.id) ? 1 : 0,
      (s.user.bookmarks || []).length,
      isNarrow(s) ? s.sheet.state : '',
      isNarrow(s) ? '' : indexSig(s)].join('|');
    if (sig === bodySig) return false;
    var sc = window.Containers && window.Containers.scroller && window.Containers.scroller('detail');
    var keepTop = (bodySig && bodySig.split('|')[0] === r.id && sc) ? sc.scrollTop : null;
    var idChangedHere = !bodySig || bodySig.split('|')[0] !== r.id;
    bodySig = sig;
    root.innerHTML = bodyHtml(s, r, entry);
    if (idChangedHere && !ctx.motion.reduced) {
      root.classList.remove('rise-in');
      void root.offsetWidth;
      root.classList.add('rise-in');
    }
    bindPhotoFallbacks(root);
    if (keepTop !== null) ctx.motion.afterGeometry(function () { if (sc) sc.scrollTop = keepTop; });
    return true;
  }

  function paintFoot(s) {
    var r = ctx.Data.byId(s.selected.id);
    if (!r) { foot.innerHTML = ''; footSig = null; return; }
    var sig = [r.id, s.lang, s.fontScale, modeOf(s), s.user.fav.has(r.id) ? 1 : 0,
      stacked ? 1 : 0, shareInline ? 1 : 0, s.overlay.kind === 'more' ? 1 : 0].join('|');
    if (sig === footSig) return;
    footSig = sig;
    foot.innerHTML = actionsHtml(s, r);
  }

  var candidateObserver = null;
  function loadCandidates() {
    if (candidateObserver) candidateObserver.disconnect();
    var track = cand.querySelector('.dt-cand-track');
    if (!track) return;
    candidateObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var card = entry.target;
        candidateObserver.unobserve(card);
        var id = card.getAttribute('data-cand');
        ctx.Data.detail(id).then(function (det) {
          if (!card.isConnected) return;
          var ph = photosOf(det); if (!ph.length) return;
          var img = document.createElement('img');
          img.className = 'dt-cand-img'; img.alt = ''; img.decoding = 'async';
          img.src = PhotoUrls.url(ph[0], 320);
          card.replaceChild(img, card.querySelector('.dt-cand-img'));
          bindPhotoFallbacks(card);
        }).catch(function () {});
      });
    }, { root: track, rootMargin: '0px 180px', threshold: 0 });
    Array.prototype.forEach.call(track.children, function (card) { candidateObserver.observe(card); });
  }
  function paintCandidates(s) {
    var host = s.layout.mode === 'mid' && s.selected.id;
    if (!host) { if (candSig !== '') { cand.innerHTML = ''; candSig = ''; } return; }
    if (!s.detail.candidatesOpen) {
      var csig = 'collapsed|' + s.lang + '|' + s.fontScale;
      if (candSig === csig) return;
      candSig = csig;
      cand.innerHTML = '<div class="dt-cand dt-cand--collapsed"><button class="dt-cand-reopen" type="button" data-act="cand-open">' +
        ic('cutlery', { cls: 'ic-sm' }) + '<span>' + esc(t('符合筛选的餐厅')) + '</span>' +
        '<span class="dt-cand-reopen-n num">' + ctx.util.fmtCount(Dm.candidates(s).length) + '</span>' +
        ic('chevronUp', { cls: 'ic-sm' }) + '</button></div>';
      return;
    }
    var ids = Dm.candidates(s);
    var sig = [s.lang, s.fontScale, s.selected.id, ids.length, ids.slice(0, 60).join(',')].join('|');
    if (sig === candSig) return;
    candSig = sig;
    cand.innerHTML = candidateHtml(s);
    loadCandidates();
    var on = cand.querySelector('.dt-cand-card.is-on');
    if (on) ctx.motion.afterGeometry(function () { try { on.scrollIntoView({ block: 'nearest', inline: 'center' }); } catch (e) { /* ignore */ } });
  }

  /* ------------------------------------------------------------ lifecycle */

  Dm.init = function (c) {
    ctx = c;
    root = c.roots.detailRoot;
    foot = c.roots.detailFoot;
    cand = c.roots.candidatesRoot;
    var u = c.util;

    function onAct(e, el) {
      var s = ctx.App.state, id = s.selected.id;
      switch (el.getAttribute('data-act')) {
        case 'fav': openMemberPicker(id); break;
        case 'member': openMemberPicker(id); break;
        case 'more': ctx.act.openOverlay('more', { id: id }); break;
        case 'share': doShare(id, e); break;
        case 'unhide': setHidden(id, false); break;
        case 'trans': onTrans(el.getAttribute('data-trans')); break;
        case 'tx': translateRun(el); break;
        case 'copy-addr': copyAddress(); break;
        case 'retry-detail': retryDetail(id); break;
        case 'lightbox': openLightbox(Number(el.getAttribute('data-index')) || 0); break;
        case 'prev': step(-1); break;
        case 'next': step(1); break;
        case 'cand-all': ctx.act.setTab('results'); ctx.App.set({ columns: { userLeftPreference: 'open' } }); break;
        case 'cand-close': ctx.App.set({ detail: { candidatesOpen: false } }); break;
        case 'cand-open': ctx.App.set({ detail: { candidatesOpen: true } }); break;
        case 'cand-prev': scrollCand(-1); break;
        case 'cand-next': scrollCand(1); break;
      }
    }
    u.delegate(root, 'click', '[data-act]', onAct);
    u.delegate(foot, 'click', '[data-act]', onAct);
    u.delegate(cand, 'click', '[data-act]', onAct);
    u.delegate(root, 'click', '[data-photo]', function (e, el) { openLightbox(Number(el.getAttribute('data-photo')) || 0); });
    u.delegate(cand, 'click', '[data-cand]', function (e, el) {
      ctx.act.openDetail(el.getAttribute('data-cand'), 'candidates');
    });

    ctx.App.on('overlay:closed', function (p) {
      if (!p || p.kind !== 'lightbox') return;
      lbEl = null;
      ctx.motion.afterGeometry(function () {
        var el = root.querySelector('[data-photo="' + lastPhotoIndex + '"]');
        if (el) { try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); } }
      });
    });
    ctx.App.on('layout:settled', function () {
      var s = ctx.App.state;
      if (!s.selected.id) return;
      if (stacked || !shareInline) { stacked = false; shareInline = true; footSig = null; paintFoot(s); }
      ctx.util.raf(function () { measureActions(ctx.App.state); measureSummary(); });
    });

    function onTrans(which) {
      if (which === 'ja' && !jaMap) loadJa().catch(function () { /* surfaced in the card */ });
      ctx.App.set({ detail: { translation: which } });
    }
    function step(dir) {
      var ix = Dm.index(ctx.App.state);
      var target = dir < 0 ? ix.prev : ix.next;
      if (target) ctx.act.openDetail(target, 'candidates');
    }
    function retryDetail(id) {
      delete detCache[cacheKey(id, ctx.App.state.lang)];
      bodySig = null;
      ctx.App.requestRender('detail:data');
    }
    function copyAddress() {
      var el = root.querySelector('[data-section="address"] .dt-row:last-of-type .dt-ja, [data-section="address"] .dt-ja');
      var det = Dm.detailFor(ctx.App.state.selected.id);
      var text = (det && det.address) || (el ? el.textContent : '');
      if (!text) return;
      var done = function () { ctx.act.showToast({ kind: 'info', text: ctx.t('已复制地址') }); };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, done);
        else done();
      } catch (e) { done(); }
    }
    /** the production 翻译 button: ja → the active UI language, on demand. */
    function translateRun(btn) {
      var wrap = btn.closest('.dt-ja-wrap');
      var target = wrap && wrap.querySelector('.dt-ja');
      if (!target) return;
      var label = btn.querySelector('.dt-tx-label');
      if (btn.dataset.state === 'translated') {
        target.textContent = btn.dataset.orig || target.textContent;
        target.setAttribute('lang', 'ja');
        btn.dataset.state = '';
        if (label) label.textContent = ctx.t('翻译');
        return;
      }
      var text = (target.textContent || '').trim();
      if (!text) return;
      btn.disabled = true;
      if (label) label.textContent = '…';
      ctx.act.translateJa(text).then(function (out) {
        if (!out) throw new Error('empty');
        btn.dataset.orig = text;
        btn.dataset.state = 'translated';
        target.textContent = out;
        target.removeAttribute('lang');
        if (label) label.textContent = ctx.t('原文');
      }).catch(function () {
        if (label) label.textContent = ctx.t('翻译失败');
      }).then(function () { btn.disabled = false; });
    }
    function scrollCand(dir) {
      var track = cand.querySelector('.dt-cand-track');
      if (!track) return;
      track.scrollBy({ left: dir * Math.max(160, track.clientWidth * 0.8), behavior: ctx.motion.reduced ? 'auto' : 'smooth' });
    }
    function openLightbox(i) {
      lastPhotoIndex = i;
      if (window.Overlays && typeof window.Overlays.lightboxOpen === 'function') window.Overlays.lightboxOpen({ id: ctx.App.state.selected.id, index: i });
      else ctx.act.openOverlay('lightbox', { id: ctx.App.state.selected.id, index: i });
    }
  };

  Dm.render = function (s, changed) {
    if (!ctx.App.changedAny(changed, ['selected', 'user', 'detail', 'lang', 'fontScale', 'layout', 'filters', 'sort',
      'overlay', 'columns', 'sheet', 'detail:data', 'detail:ja', 'nearby'])) return;
    var r = s.selected.id ? ctx.Data.byId(s.selected.id) : null;
    if (!r) {
      if (bodySig !== null) { root.innerHTML = ''; foot.innerHTML = ''; bodySig = null; footSig = null; }
      paintCandidates(s);
      return;
    }
    var idChanged = !bodySig || bodySig.split('|')[0] !== r.id;
    var modeChanged = !bodySig || bodySig.split('|')[3] !== modeOf(s);
    if (idChanged || modeChanged || (bodySig && bodySig.split('|')[1] !== s.lang) ||
        (bodySig && bodySig.split('|')[2] !== String(s.fontScale))) {
      stacked = false; shareInline = true;
    }
    var repainted = paintBody(s, r);
    paintFoot(s);
    paintCandidates(s);
    if (repainted || ctx.App.changedAny(changed, ['layout', 'lang', 'fontScale'])) afterPaintMeasure();
    if (s.detail.scrollTo !== null && s.detail.scrollTo !== undefined) {
      var sec = s.detail.scrollTo;
      ctx.App.set({ detail: { scrollTo: null } }, { silent: true });
      Dm.scrollTo(sec);
    }
  };

  Dm.destroy = function () {
    root.innerHTML = ''; foot.innerHTML = ''; cand.innerHTML = '';
    bodySig = null; footSig = null; candSig = null;
  };

  window.Detail = Dm;
  window.App.registerModule('detail', Dm);
})();
