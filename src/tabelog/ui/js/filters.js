/* ==========================================================================
   Japan Foodmap 4.0.0 — src/tabelog/ui/js/filters.js
   Owns: the full filter form (summary → region → rating → dinner price →
   cuisine frame → awards → other → reset) rendered into #filters-root, and
   the fixed "查看 N 家结果" / "正在更新结果…" button in #filters-foot.

   Ported from ui-captures-2026-09-10/redesign/demo-4.0/js/filters.js. What is
   different in production, and why:

   · **counts are computed off the main thread's critical path.** FILTER-02
     wants an option-level count for every control with the *other* conditions
     unchanged, which `Data.counts()` produces by running the verbatim
     `passesFilter` over all ~10k rows about forty times. On the demo's 300-row
     fixture that was free; here it is tens of milliseconds, and it would run
     on every checkbox. So the first pass is synchronous (the form must paint
     with real numbers), and every later one is scheduled on idle: the old
     numbers stay on screen, the footer says 正在更新结果, and one more render
     lands the new ones. `Data.counts` itself caches by filter signature.
   · the 47 prefectures come from `Data.config.REGIONS`, which is the build's
     own `PREFS` table (ja / sc / tc / en names, index-aligned with `row.pref`)
     grouped into the six regional blocks by `REGION_GROUPS`. Region counts are live
     (`counts.region[pref]`), not the build-time `n`.
   · award labels go through `Data.awardLabel` so GOLD/SILVER/BRONZE stay the
     Tabelog Award wording and 百名店 / 热门餐厅 2026 get translated.
   · the region picker is overlays' sheet. While overlays is still a stub the
     form falls back to an inline 47-prefecture panel rather than to a button
     that does nothing (see `openRegion`).

   Public API (CONTRACT.md §filters):
     Filters.counts()            → cached Data.counts for the current state
     Filters.badge()             → FILTER-02 group count for chip / tab badge
     Filters.scrollTo(section)   → 'top'|'region'|'rating'|'budget'|'cuisines'|'awards'|'other'
     Filters.summaryChips(state) → [{key, label, params, clear}]
   Additions:
     Filters.sectionIds()        → the fixed section order, for tests
     Filters.isUpdating()        → true while the footer shows 正在更新结果…
     Filters.countsReady()       → false while an idle recount is in flight
   Owner of this file and css/filters.css: the filters agent, nobody else.
   ========================================================================== */
(function () {
  'use strict';

  var F = { name: 'filters' };
  var ctx, App, D, U, t, root, foot;

  var _counts = null, _countsKey = '', _pendingCountsKey = null;
  var _mv = null;                    // last MV reported by the map module
  var _pendingUntil = 0, _pendingTimer = null;
  var _domLang = null;               // language the current DOM was built for
  var _help = { rating: false, budget: false };
  var _regionFallback = false, _regionOpen = false;
  // Saved / Hidden change the 只看已收藏 count and can change M; a size pair is
  // not enough (one star added and another removed leaves both sizes equal), so
  // the recount is keyed off a revision the adapter's user:changed bumps.
  var _userRev = 0;
  var SECTIONS = ['top', 'region', 'rating', 'budget', 'cuisines', 'awards', 'other'];

  var idle = window.requestIdleCallback
    ? function (fn) { window.requestIdleCallback(fn, { timeout: 400 }); }
    : function (fn) { setTimeout(fn, 0); };

  /* ---------------------------------------------------------------- utils */
  function cfg() { return D.config; }
  function budgetKeys() { return cfg().PRICE_BUCKETS.map(function (b) { return b.key; }); }
  /** empty Set means "no restriction" (Data.filterMatch); show it as everything ticked. */
  // Literal sets (see adapter Prefs.readFilterState): what the set says is what
  // is ticked, and an empty one really is "nothing ticked" — no results.
  function effBudgets(f) { return new Set(f.budgets || []); }
  function effCuisines(f) { return new Set(f.cuisines || []); }
  function budgetsUnrestricted(f) { return !!f.budgets && f.budgets.size === cfg().PRICE_BUCKETS.length; }
  function cuisinesUnrestricted(f) { return !!f.cuisines && f.cuisines.size === cfg().ALL_CUISINES.length; }
  function r2(v) { return Math.round(Number(v) * 100) / 100; }
  function num(n) { return U.fmtCount(n); }
  function esc(s) { return U.esc(s); }
  function ic(name) { return ctx.icon(name); }

  var SECTION_ICON = { region: 'pin', rating: 'star', budget: 'yen', cuisines: 'cutlery', awards: 'trophy', other: 'sliders' };

  function stateKey(s) {
    var f = s.filters;
    return [s.lang, f.region, f.ratingMin,
      Array.from(f.budgets).sort().join(','), Array.from(f.cuisines).sort().join(','), Array.from(f.awards).sort().join(','),
      f.bookableOnly ? 1 : 0, f.favOnly ? 1 : 0, f.hideBlack ? 1 : 0, f.hideForeign ? 1 : 0, f.gcalOnly ? 1 : 0,
      _userRev, D.restaurants.length].join('~');
  }

  /* ------------------------------------------------------------ public API */
  /**
   * counts() — FILTER-02 numbers. Synchronous the first time (nothing to show
   * otherwise), then always from cache: a changed signature schedules one idle
   * recount and the previous numbers stay up until it lands.
   */
  F.counts = function () {
    var s = App.state, k = stateKey(s);
    if (k === _countsKey && _counts) return _counts;
    if (!_counts) { _counts = D.counts(s.filters); _countsKey = k; return _counts; }
    if (_pendingCountsKey !== k) {
      _pendingCountsKey = k;
      markPending();
      idle(function () {
        if (_pendingCountsKey !== k) return;          // superseded by a newer edit
        _pendingCountsKey = null;
        try { _counts = D.counts(App.state.filters); _countsKey = k; }
        catch (e) { console.error('[filters] counts', e); }
        App.requestRender('filters:counts');
      });
    }
    return _counts;
  };
  F.countsReady = function () { return _pendingCountsKey === null; };
  F.badge = function () { return D.summaryCount(App.state.filters, F.counts()); };
  F.summaryChips = function (state) { return D.summaryGroups((state || App.state).filters, F.counts()); };
  F.sectionIds = function () { return SECTIONS.slice(); };
  F.isUpdating = function () { return _pendingUntil > Date.now() || !F.countsReady(); };
  F.scrollTo = function (sec) {
    var sc = window.Containers && window.Containers.scroller ? window.Containers.scroller('filters') : null;
    if (!sc) return;
    ctx.motion.afterGeometry(function () {
      var target = root.querySelector('[data-section="' + sec + '"]');
      if (!target || sec === 'top') { sc.scrollTop = 0; return; }
      var a = target.getBoundingClientRect(), b = sc.getBoundingClientRect();
      sc.scrollTop = Math.max(0, sc.scrollTop + (a.top - b.top) - 8);
    });
  };

  /* -------------------------------------------------------------- markup */
  function headHtml(section, titleKey, rightHtml, extraHtml) {
    return '<div class="ft-head">' +
      '<h3 class="t-group-title ft-title">' + ic(SECTION_ICON[section]) + '<span>' + esc(t(titleKey)) + '</span></h3>' +
      (extraHtml || '') + (rightHtml || '') + '</div>';
  }
  function helpBtn(kind, labelKey) {
    return '<button type="button" class="ft-help-btn" data-help="' + kind + '" aria-expanded="false" aria-controls="ft-help-' + kind + '" aria-label="' + esc(t(labelKey)) + '">?</button>';
  }
  function bulkLinks(prefix, allKey, noneKey) {
    return '<span class="ft-head-actions">' +
      '<button type="button" class="ft-link" data-bulk="' + prefix + '-all">' + esc(t(allKey)) + '</button>' +
      '<span class="ft-link-sep" aria-hidden="true">|</span>' +
      '<button type="button" class="ft-link" data-bulk="' + prefix + '-none">' + esc(t(noneKey)) + '</button>' +
      '</span>';
  }

  /** the inline 47-prefecture panel — only used while overlays has no picker. */
  function regionPanelHtml() {
    var C = cfg();
    var out = '<div class="ft-region-panel" id="ft-region-panel" hidden>' +
      '<button type="button" class="ft-region-opt is-all" data-region="">' +
      '<span class="ft-region-opt-n">' + esc(t('全部地区')) + '</span>' +
      '<span class="opt-count num" data-region-count="all"></span></button>';
    C.REGION_GROUPS.forEach(function (g) {
      var opts = '';
      for (var i = g.from; i <= g.to; i++) {
        var p = C.REGIONS[i];
        if (!p) continue;
        opts += '<button type="button" class="ft-region-opt" data-region="' + i + '">' +
          '<span class="ft-region-opt-n"></span>' +
          '<span class="opt-count num" data-region-count="' + i + '"></span></button>';
      }
      out += '<div class="ft-region-grp"><h4 class="ft-region-grp-t t-secondary">' + esc(D.regionGroupName(g)) + '</h4>' +
        '<div class="ft-region-opts">' + opts + '</div></div>';
    });
    return out + '</div>';
  }

  function buildHtml() {
    var C = cfg(), h = [];

    h.push('<div class="ft-form" role="group" aria-label="' + esc(t('筛选条件')) + '">');

    /* ---- summary ---- */
    h.push('<section class="ft-sec ft-sec-top" data-section="top">' +
      '<p class="ft-summary t-body" id="ft-summary" aria-live="polite"></p>' +
      '<div class="ft-chips" id="ft-chips"></div>' +
      '</section>');

    /* ---- region ---- */
    h.push('<section class="ft-sec" data-section="region">' +
      headHtml('region', '地区') +
      '<button type="button" class="select-btn ft-region" data-act="region" aria-haspopup="dialog" aria-expanded="false">' +
      '<span class="ft-region-val">' + ic('pin') + '<span class="ft-region-name"></span><span class="ft-region-count num"></span></span>' +
      ic('chevronDown') + '</button>' +
      regionPanelHtml() +
      '</section>');

    /* ---- rating ---- */
    var quick = '<div class="ft-quick" role="group" aria-label="' + esc(t('评分快捷选择')) + '">' +
      '<button type="button" class="chip chip-tall" data-quick="' + C.RATING_MIN.toFixed(2) + '" aria-pressed="false">' + esc(t('全部')) + '</button>' +
      C.RATING_QUICK.map(function (q) {
        return '<button type="button" class="chip chip-tall num" data-quick="' + q.toFixed(2) + '" aria-pressed="false">' + esc(t('≥ {n}', { n: q.toFixed(1) })) + '</button>';
      }).join('') + '</div>';
    h.push('<section class="ft-sec" data-section="rating">' +
      headHtml('rating', '评分', '<span class="ft-head-value num" id="ft-rating-val"></span>', helpBtn('rating', '关于评分下限')) +
      '<p class="ft-help t-secondary" id="ft-help-rating" hidden>' + esc(t('3.4 是当前收录范围下限，不是全站最低分')) + '</p>' +
      '<div class="ft-slider-wrap">' +
      '<input type="range" class="slider" id="ft-rating" min="' + C.RATING_MIN + '" max="' + C.RATING_MAX + '" step="0.05" aria-label="' + esc(t('最低评分')) + '">' +
      '<div class="ft-scale t-secondary num"><span>' + C.RATING_MIN.toFixed(1) + '</span><span>' + C.RATING_MAX.toFixed(1) + '</span></div>' +
      '</div>' + quick +
      '</section>');

    /* ---- dinner price ---- */
    var rows = C.PRICE_BUCKETS.map(function (b) {
      return '<label class="opt-row ft-budget" data-budget="' + b.key + '">' +
        '<input type="checkbox" class="checkbox" data-budget-cb="' + b.key + '">' +
        '<span class="price-dot price-' + b.key + '" aria-hidden="true"></span>' +
        '<span class="opt-label">' + esc(t(b.label)) + '</span>' +
        '<span class="opt-count num" data-count="budget:' + b.key + '"></span>' +
        '</label>';
    }).join('');
    h.push('<section class="ft-sec" data-section="budget">' +
      headHtml('budget', '晚餐价格', bulkLinks('budget', '全选', '全清'), helpBtn('budget', '关于价格分档')) +
      '<p class="ft-help t-secondary" id="ft-help-budget" hidden>' + esc(t('按晚餐价位上限归档；没有晚餐价位时用午餐，两者都没有则归入价格未知。')) + '</p>' +
      '<div class="ft-rows">' + rows + '</div>' +
      '</section>');

    /* ---- cuisines (one frame for every parent and child) ---- */
    var groups = C.MEAL_GROUPS.map(function (g, gi) {
      var children = g.buckets.map(function (cat) {
        return '<label class="opt-row opt-row-child ft-cui" data-cuisine="' + esc(cat) + '">' +
          '<input type="checkbox" class="checkbox" data-cui-cb="' + esc(cat) + '">' +
          '<span class="ft-emj" aria-hidden="true">' + ctx.emoji.img(C.GENRE_EMOJI[cat] || '🍽️', 16) + '</span>' +
          '<span class="opt-label">' + esc(D.cuisineLabel(cat)) + '</span>' +
          '<span class="opt-count num" data-count="cuisine:' + esc(cat) + '"></span>' +
          '</label>';
      }).join('');
      return '<div class="ft-grp" data-group="' + esc(g.name) + '" data-open="true">' +
        '<div class="ft-grp-head">' +
        '<label class="opt-row ft-grp-row">' +
        '<input type="checkbox" class="checkbox" data-grp-cb="' + esc(g.name) + '">' +
        '<span class="opt-label">' + esc(t(g.name)) + '</span>' +
        '<span class="opt-count num" data-count="group:' + esc(g.name) + '"></span>' +
        '</label>' +
        '<button type="button" class="icon-btn ft-grp-arrow" data-grp-toggle="' + esc(g.name) + '" aria-expanded="true" aria-controls="ft-grp-' + gi + '" aria-label="' + esc(t('展开或收起 {name}', { name: t(g.name) })) + '">' + ic('chevronDown') + '</button>' +
        '</div>' +
        '<div class="ft-children" id="ft-grp-' + gi + '"><div class="ft-children-in">' + children + '</div></div>' +
        '</div>';
    }).join('');
    h.push('<section class="ft-sec" data-section="cuisines">' +
      headHtml('cuisines', '菜系',
        '<button type="button" class="icon-btn ft-collapse" data-cuisines-toggle aria-expanded="true" aria-controls="ft-cui-box" aria-label="' + esc(t('展开或收起菜系')) + '">' + ic('chevronUp') + '</button>',
        '<span class="ft-head-sum t-secondary" id="ft-cui-sum"></span>') +
      '<div class="ft-box" id="ft-cui-box">' +
      '<div class="ft-box-bulk">' +
      '<label class="opt-row ft-all-row"><input type="checkbox" class="checkbox" data-bulk-cb="cuisine"><span class="opt-label">' + esc(t('全选')) + '</span></label>' +
      '<button type="button" class="ft-link" data-bulk="cuisine-none">' + esc(t('全清')) + '</button>' +
      '</div>' + groups +
      '<p class="ft-note t-secondary">' + esc(t('非日本料理的十类不在这棵树里，由下方「隐藏非日本料理」控制')) + '</p>' +
      '</div></section>');

    /* ---- awards ---- */
    var aw = C.AWARD_TAGS.map(function (a) {
      return '<label class="opt-row ft-award" data-award="' + a.slug + '">' +
        '<input type="checkbox" class="checkbox cb-award" data-award-cb="' + a.slug + '" style="--cb-color:var(--award-' + a.slug + '-text)">' +
        '<span class="ft-emj" aria-hidden="true">' + ctx.emoji.img(a.emoji, 16) + '</span>' +
        '<span class="opt-label">' + esc(D.awardLabel(a)) + '</span>' +
        '<span class="opt-count num" data-count="award:' + a.slug + '"></span>' +
        '</label>';
    }).join('');
    h.push('<section class="ft-sec" data-section="awards">' +
      headHtml('awards', '获奖', '<span class="ft-head-actions"><button type="button" class="ft-link" data-bulk="award-none">' + esc(t('全清')) + '</button></span>') +
      '<p class="ft-note t-secondary">' + esc(t('勾选后只看对应获奖店 · 多选取并集 · 不勾则不限制')) + '</p>' +
      '<div class="ft-rows ft-awards">' + aw + '</div>' +
      '</section>');

    /* ---- other ---- */
    function sw(field, labelKey, subKey, countName) {
      return '<label class="ft-sw-row" data-sw-row="' + field + '">' +
        '<span class="ft-sw-text">' +
        '<span class="ft-sw-label">' + esc(t(labelKey)) + '</span>' +
        (subKey ? '<span class="ft-sw-sub t-secondary">' + esc(t(subKey)) + '</span>' : '') +
        '</span>' +
        (countName ? '<span class="ft-sw-count t-secondary num" data-count="' + countName + '"></span>' : '') +
        '<input type="checkbox" class="switch" role="switch" aria-checked="false" data-sw="' + field + '">' +
        '</label>';
    }
    h.push('<section class="ft-sec" data-section="other">' +
      headHtml('other', '其它') +
      '<div class="ft-rows ft-switches">' +
      sw('bookableOnly', '只看可网订的店', '有 Tabelog 网上预订入口，不表示有余位', 'bookable') +
      sw('favOnly', '只看已收藏', '仅匹配收藏集合', 'fav') +
      '<div class="ft-sw-group">' + sw('hideBlack', '隐藏弃用名单', '研究后排除的餐厅不在结果和地图显示') +
      '<p class="ft-sw-extra t-secondary" id="ft-black-extra"></p></div>' +
      '<div class="ft-sw-group">' + sw('hideForeign', '隐藏非日本料理', '中餐、韩餐、西餐、南亚、中东等十类，独立于上面的菜系树') +
      '<p class="ft-sw-extra t-secondary" id="ft-foreign-extra"><span id="ft-foreign-text"></span> ' +
      '<button type="button" class="ft-link" data-act="show-foreign">' + esc(t('一起显示')) + '</button></p></div>' +
      sw('gcalOnly', '只看谷歌地图校准过坐标的餐厅', '只描述坐标来源，不代表谷歌认证餐厅品质', 'gcal') +
      '</div></section>');

    /* ---- reset ---- */
    h.push('<div class="ft-reset"><button type="button" class="btn btn-quiet ft-reset-btn" data-act="reset">' + ic('reset') + '<span>' + esc(t('重置筛选')) + '</span></button></div>');
    h.push('<div class="ft-tail" aria-hidden="true"></div>');
    h.push('</div>');
    return h.join('');
  }

  function buildFootHtml() {
    return '<div class="ft-foot"><button type="button" class="btn btn-primary btn-fixed ft-see" data-see-results></button></div>';
  }

  /* ---------------------------------------------------------------- sync */
  function setChecked(input, on, indeterminate) {
    if (!input) return;
    if (input.checked !== !!on) input.checked = !!on;
    input.indeterminate = !!indeterminate;
    if (input.getAttribute('role') === 'switch') input.setAttribute('aria-checked', on ? 'true' : 'false');
  }
  function setText(node, s) { if (node && node.textContent !== s) node.textContent = s; }

  function currentMV(mIds) {
    if (window.MapMod && typeof window.MapMod.MV === 'function' && window.MapMod.map) {
      try { return window.MapMod.MV(mIds).length; } catch (e) { /* map not ready */ }
    }
    return _mv === null ? mIds.length : _mv;
  }

  function sync(s) {
    var C = cfg(), f = s.filters, c = F.counts();
    var mIds = D.applyFilters(f);
    var M = mIds.length, MV = currentMV(mIds), TOTAL = D.restaurants.length;

    /* summary line + chips */
    var sum = root.querySelector('#ft-summary');
    if (sum) {
      sum.innerHTML = t('筛选后 {m} 家餐厅符合标准 · 其中屏幕内 {mv} 家 / {total}', {
        m: '<b class="num">' + num(M) + '</b>', mv: '<span class="num">' + num(MV) + '</span>', total: '<span class="num">' + num(TOTAL) + '</span>'
      });
    }
    var chipHost = root.querySelector('#ft-chips');
    if (chipHost) {
      var groups = D.summaryGroups(f, c);
      chipHost.innerHTML = groups.map(function (g, i) {
        var label = t(g.label, g.params);
        return '<span class="chip is-on ft-chip"><span class="ft-chip-t">' + esc(label) + '</span>' +
          '<button type="button" class="chip-x" data-chip="' + i + '" aria-label="' + esc(t('移除筛选：{name}', { name: label })) + '">' + ctx.icon('x', { cls: 'ic-sm' }) + '</button></span>';
      }).join('');
      chipHost.hidden = groups.length === 0;
      F._chipGroups = groups;
    }

    /* region */
    var regionBtn = root.querySelector('.ft-region');
    if (regionBtn) regionBtn.setAttribute('aria-expanded', (s.overlay.kind === 'regionPicker' || _regionOpen) ? 'true' : 'false');
    var regionTotal = 0; Object.keys(c.region).forEach(function (k) { regionTotal += c.region[k]; });
    setText(root.querySelector('.ft-region-name'), f.region === null || f.region === undefined ? t('全部地区') : D.regionName(f.region, s.lang));
    setText(root.querySelector('.ft-region-count'), '(' + num(f.region === null || f.region === undefined ? regionTotal : (c.region[f.region] || 0)) + ')');
    syncRegionPanel(s, c, regionTotal);

    /* rating */
    setText(root.querySelector('#ft-rating-val'), '≥ ' + f.ratingMin.toFixed(2));
    var sl = root.querySelector('#ft-rating');
    if (sl) {
      if (document.activeElement !== sl || Number(sl.value) !== f.ratingMin) sl.value = String(f.ratingMin);
      sl.setAttribute('aria-valuetext', '≥ ' + f.ratingMin.toFixed(2));
      var pct = ((f.ratingMin - C.RATING_MIN) / (C.RATING_MAX - C.RATING_MIN)) * 100;
      sl.style.setProperty('--slider-pct', pct.toFixed(1) + '%');
    }
    Array.prototype.forEach.call(root.querySelectorAll('[data-quick]'), function (b) {
      b.setAttribute('aria-pressed', Math.abs(Number(b.dataset.quick) - f.ratingMin) < 1e-9 ? 'true' : 'false');
    });
    var hr = root.querySelector('#ft-help-rating'); if (hr) hr.hidden = !_help.rating;
    var hb = root.querySelector('#ft-help-budget'); if (hb) hb.hidden = !_help.budget;
    Array.prototype.forEach.call(root.querySelectorAll('[data-help]'), function (b) {
      b.setAttribute('aria-expanded', _help[b.dataset.help] ? 'true' : 'false');
    });

    /* budgets */
    var eb = effBudgets(f);
    C.PRICE_BUCKETS.forEach(function (b) {
      setChecked(root.querySelector('[data-budget-cb="' + b.key + '"]'), eb.has(b.key), false);
      setText(root.querySelector('[data-count="budget:' + b.key + '"]'), num(c.budgets[b.key] || 0));
      var row = root.querySelector('.ft-budget[data-budget="' + b.key + '"]');
      if (row) row.classList.toggle('is-on', !budgetsUnrestricted(f) && eb.has(b.key));
    });
    var bAll = root.querySelector('[data-bulk="budget-all"]'), bNone = root.querySelector('[data-bulk="budget-none"]');
    if (bAll) bAll.disabled = budgetsUnrestricted(f);
    if (bNone) bNone.disabled = budgetsUnrestricted(f);

    /* cuisines */
    var ecu = effCuisines(f), restricted = !cuisinesUnrestricted(f);
    C.ALL_CUISINES.forEach(function (cat) {
      setChecked(root.querySelector('[data-cui-cb="' + cssq(cat) + '"]'), ecu.has(cat), false);
      setText(root.querySelector('[data-count="cuisine:' + cssq(cat) + '"]'), num(c.cuisines[cat] || 0));
      var crow = root.querySelector('.ft-cui[data-cuisine="' + cssq(cat) + '"]');
      if (crow) crow.classList.toggle('is-on', restricted && ecu.has(cat));
    });
    C.MEAL_GROUPS.forEach(function (g) {
      var on = g.buckets.filter(function (b) { return ecu.has(b); }).length;
      setChecked(root.querySelector('[data-grp-cb="' + cssq(g.name) + '"]'), on === g.buckets.length, on > 0 && on < g.buckets.length);
      setText(root.querySelector('[data-count="group:' + cssq(g.name) + '"]'), num(c.groups[g.name] || 0));
      var box = root.querySelector('.ft-grp[data-group="' + cssq(g.name) + '"]');
      if (box) {
        var open = !!(s.filtersUi.openGroups && s.filtersUi.openGroups.has(g.name));
        box.setAttribute('data-open', open ? 'true' : 'false');
        box.classList.toggle('is-on', restricted && on > 0);
        var arrow = box.querySelector('[data-grp-toggle]');
        if (arrow) arrow.setAttribute('aria-expanded', open ? 'true' : 'false');
        var kids = box.querySelector('.ft-children');
        if (kids) kids.inert = !open;
      }
    });
    var selN = C.ALL_CUISINES.filter(function (x) { return ecu.has(x); }).length;
    setText(root.querySelector('#ft-cui-sum'), restricted ? t('已选择 {n} 个菜系', { n: num(selN) }) : t('全部菜系'));
    setChecked(root.querySelector('[data-bulk-cb="cuisine"]'), !restricted || selN === C.ALL_CUISINES.length, restricted && selN > 0 && selN < C.ALL_CUISINES.length);
    var cNone = root.querySelector('[data-bulk="cuisine-none"]'); if (cNone) cNone.disabled = !restricted;
    var cbox = root.querySelector('#ft-cui-box'), ctog = root.querySelector('[data-cuisines-toggle]');
    if (cbox) { cbox.hidden = !s.filtersUi.cuisinesOpen; }
    if (ctog) {
      ctog.setAttribute('aria-expanded', s.filtersUi.cuisinesOpen ? 'true' : 'false');
      ctog.innerHTML = ic(s.filtersUi.cuisinesOpen ? 'chevronUp' : 'chevronDown');
    }

    /* awards */
    C.AWARD_TAGS.forEach(function (a) {
      var on = f.awards.has(a.slug);
      setChecked(root.querySelector('[data-award-cb="' + a.slug + '"]'), on, false);
      setText(root.querySelector('[data-count="award:' + a.slug + '"]'), num(c.awards[a.slug] || 0));
      var row = root.querySelector('.ft-award[data-award="' + a.slug + '"]');
      if (row) row.classList.toggle('is-on', on);
    });
    var aNone = root.querySelector('[data-bulk="award-none"]'); if (aNone) aNone.disabled = f.awards.size === 0;

    /* other */
    ['bookableOnly', 'favOnly', 'hideBlack', 'hideForeign', 'gcalOnly'].forEach(function (k) {
      setChecked(root.querySelector('[data-sw="' + k + '"]'), !!f[k], false);
    });
    setText(root.querySelector('[data-count="bookable"]'), num(c.bookable));
    setText(root.querySelector('[data-count="fav"]'), num(c.fav));
    setText(root.querySelector('[data-count="gcal"]'), num(c.gcal));
    var be = root.querySelector('#ft-black-extra');
    if (be) { be.textContent = t('当前已弃用 {n} 家', { n: num(s.user.black.size) }); be.hidden = s.user.black.size === 0; }
    var fe = root.querySelector('#ft-foreign-extra'), ftx = root.querySelector('#ft-foreign-text');
    if (fe) {
      var blocked = c.foreignBlocked || 0;
      fe.hidden = !(f.hideForeign && blocked > 0);
      if (ftx) ftx.textContent = t('这次筛选里有 {n} 家因此被挡住', { n: num(blocked) });
    }

    /* footer */
    var see = foot.querySelector('.ft-see');
    if (see) {
      var updating = F.isUpdating();
      setText(see, updating ? t('正在更新结果…') : t('查看 {n} 家结果', { n: num(M) }));
      see.setAttribute('aria-busy', updating ? 'true' : 'false');
      see.classList.toggle('is-updating', updating);
    }
  }

  function syncRegionPanel(s, c, regionTotal) {
    var panel = root.querySelector('#ft-region-panel');
    if (!panel) return;
    var show = _regionFallback && _regionOpen;
    panel.hidden = !show;
    if (!show) return;
    var C = cfg(), f = s.filters;
    Array.prototype.forEach.call(panel.querySelectorAll('.ft-region-opt'), function (b) {
      var raw = b.getAttribute('data-region');
      var code = raw === '' ? null : Number(raw);
      var name = code === null ? t('全部地区') : D.regionName(code, s.lang);
      setText(b.querySelector('.ft-region-opt-n'), name);
      setText(b.querySelector('.opt-count'), num(code === null ? regionTotal : (c.region[code] || 0)));
      var on = (code === null && (f.region === null || f.region === undefined)) || code === f.region;
      b.classList.toggle('is-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  /** escape a value used inside an attribute selector (cuisine names carry ·) */
  function cssq(v) { return String(v).replace(/["\\]/g, '\\$&'); }

  /* ------------------------------------------------------------- actions */
  function apply(patch) { ctx.act.applyFilters(patch); }

  function toggleBudget(key) {
    var f = App.state.filters, cur = effBudgets(f);
    if (cur.has(key)) cur.delete(key); else cur.add(key);
    if (cur.size === 0) cur = new Set();             // nothing ticked = no restriction (Data.filterMatch)
    apply({ budgets: cur });
  }
  function toggleCuisine(cat) {
    var f = App.state.filters, cur = effCuisines(f);
    if (cur.has(cat)) cur.delete(cat); else cur.add(cat);
    apply({ cuisines: cur });
  }
  function toggleGroup(name) {
    var f = App.state.filters, cur = effCuisines(f);
    var g = cfg().MEAL_GROUPS.filter(function (x) { return x.name === name; })[0];
    if (!g) return;
    var all = g.buckets.every(function (b) { return cur.has(b); });
    g.buckets.forEach(function (b) { if (all) cur.delete(b); else cur.add(b); });
    apply({ cuisines: cur });
  }
  function markPending() {
    _pendingUntil = Date.now() + 300;
    if (_pendingTimer) clearTimeout(_pendingTimer);
    _pendingTimer = setTimeout(function () {
      _pendingTimer = null; _pendingUntil = 0;
      App.requestRender('filters:settled');
    }, 300);
  }

  /**
   * openRegion() — overlays owns the 47-prefecture picker. While it is still a
   * stub nothing lands in #overlay-root, and a dead control is worse than an
   * inline list, so the form takes the job back for the rest of the session.
   */
  function openRegion() {
    if (_regionFallback) {
      _regionOpen = !_regionOpen;
      App.requestRender('filters:region');
      return;
    }
    ctx.act.openOverlay('regionPicker', null);
    ctx.util.raf(function () {
      if (App.state.overlay.kind !== 'regionPicker') return;
      if (overlayRendered()) return;
      ctx.act.closeOverlay('fallback');
      _regionFallback = true; _regionOpen = true;
      App.requestRender('filters:region');
    });
  }
  function overlayRendered() {
    return realChild(document.getElementById('overlay-root')) || realChild(document.getElementById('modal-root'));
  }
  function realChild(host) {
    if (!host) return false;
    for (var i = 0; i < host.children.length; i++) if (!host.children[i].hasAttribute('data-stub')) return true;
    return false;
  }

  function bind() {
    var u = U;
    u.delegate(root, 'click', '[data-act="region"]', openRegion);
    u.delegate(root, 'click', '[data-region]', function (e, b) {
      var raw = b.getAttribute('data-region');
      apply({ region: raw === '' ? null : Number(raw) });
      _regionOpen = false;
      App.requestRender('filters:region');
    });
    u.delegate(root, 'click', '[data-quick]', function (e, b) { apply({ ratingMin: r2(b.dataset.quick) }); });
    u.delegate(root, 'click', '[data-help]', function (e, b) { _help[b.dataset.help] = !_help[b.dataset.help]; App.requestRender('filters:help'); });
    u.delegate(root, 'input', '#ft-rating', function (e, sl) { apply({ ratingMin: r2(sl.value) }); });
    u.delegate(root, 'change', '#ft-rating', function (e, sl) { apply({ ratingMin: r2(sl.value) }); });

    u.delegate(root, 'change', '[data-budget-cb]', function (e, cb) { toggleBudget(cb.dataset.budgetCb); });
    u.delegate(root, 'change', '[data-cui-cb]', function (e, cb) { toggleCuisine(cb.dataset.cuiCb); });
    u.delegate(root, 'change', '[data-grp-cb]', function (e, cb) { toggleGroup(cb.dataset.grpCb); });
    u.delegate(root, 'change', '[data-award-cb]', function (e, cb) { apply({ awards: U.setToggle(App.state.filters.awards, cb.dataset.awardCb) }); });
    u.delegate(root, 'change', '[data-sw]', function (e, cb) { var p = {}; p[cb.dataset.sw] = cb.checked; apply(p); });
    u.delegate(root, 'change', '[data-bulk-cb]', function (e, cb) {
      apply({ cuisines: cb.checked ? new Set(cfg().ALL_CUISINES) : new Set() });
    });

    u.delegate(root, 'click', '[data-bulk]', function (e, b) {
      switch (b.dataset.bulk) {
        case 'budget-all': apply({ budgets: new Set(budgetKeys()) }); break;
        case 'budget-none': apply({ budgets: new Set() }); break;
        case 'cuisine-all': apply({ cuisines: new Set(cfg().ALL_CUISINES) }); break;
        case 'cuisine-none': apply({ cuisines: new Set() }); break;
        case 'award-none': apply({ awards: new Set() }); break;
      }
    });
    /* the arrow only opens / closes — it never changes the selection (§8.2) */
    u.delegate(root, 'click', '[data-grp-toggle]', function (e, b) {
      e.preventDefault(); e.stopPropagation();
      var name = b.dataset.grpToggle, open = App.state.filtersUi.openGroups || new Set();
      App.set({ filtersUi: { openGroups: U.setToggle(open, name) } });
    });
    u.delegate(root, 'click', '[data-cuisines-toggle]', function () {
      App.set({ filtersUi: { cuisinesOpen: !App.state.filtersUi.cuisinesOpen } });
    });
    u.delegate(root, 'click', '[data-chip]', function (e, b) {
      e.preventDefault();
      var g = (F._chipGroups || [])[Number(b.dataset.chip)];
      if (g && g.clear) apply(g.clear);
    });
    u.delegate(root, 'click', '[data-act="show-foreign"]', function (e) { e.preventDefault(); apply({ hideForeign: false }); });
    u.delegate(root, 'click', '[data-act="reset"]', function () { _help.rating = false; _help.budget = false; ctx.act.resetFilters(); });
    u.delegate(foot, 'click', '[data-see-results]', function () { ctx.act.setTab('results'); });
  }

  /* ------------------------------------------------------------ lifecycle */
  F.init = function (c) {
    ctx = c; App = c.App; D = c.Data; U = c.util; t = c.t;
    root = c.roots.filtersRoot; foot = c.roots.filtersFoot;
    bind();
    App.on('map:mv', function (p) {
      if (!p || p.MV === _mv) return;
      _mv = p.MV;
      App.requestRender('filters:mv');
    });
    App.on('user:changed', function () { _userRev++; });
    App.on('filters:changed', markPending);
    App.on('filters:reset', function () { _regionOpen = false; markPending(); });
  };

  F.render = function (s, changed) {
    if (!App.changedAny(changed, ['*', 'filters', 'filtersUi', 'user', 'lang', 'layout', 'sheet', 'fontScale', 'mapView', 'columns',
      'overlay', 'filters:help', 'filters:mv', 'filters:settled', 'filters:counts', 'filters:region'])) return;
    if (_domLang !== s.lang || !root.firstChild) {
      root.innerHTML = buildHtml();
      foot.innerHTML = buildFootHtml();
      _domLang = s.lang;
    }
    sync(s);
    if (s.filtersUi.scrollTo) {
      var target = s.filtersUi.scrollTo;
      App.set({ filtersUi: { scrollTo: null } }, { silent: true });
      F.scrollTo(target);
    }
  };

  F.destroy = function () {
    if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
    root.innerHTML = ''; foot.innerHTML = ''; _domLang = null;
  };

  window.Filters = F;
  window.App.registerModule('filters', F);
})();
