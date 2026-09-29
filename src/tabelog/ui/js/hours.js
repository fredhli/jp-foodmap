/* On-demand Google opening hours. Google owns the rendered content and attribution. */
(function () {
  'use strict';

  var Hours = {};
  var ctx = null;
  var entries = Object.create(null);
  var current = null;
  var parking = null;
  var mapsPromise = null;
  var mapsLanguage = null;

  function tr(key, args) { return ctx.t(key, args); }
  function esc(value) { return ctx.util.esc(value); }
  function locale(lang) { return { zh: 'zh-CN', tw: 'zh-TW', en: 'en', ja: 'ja' }[lang] || 'zh-CN'; }
  function signedIn(state) { return !!(state && state.account && state.account.signedIn); }
  function accountKey(state) { return signedIn(state) ? (state.account.email || 'signed-in') : 'guest'; }
  function config() { return window.PLACES_UI_CONFIG || {}; }
  function placeId(row) { return row && typeof row.gpid === 'string' ? row.gpid.trim() : ''; }

  Hours.key = function (row, lang, state) {
    return [placeId(row), accountKey(state)].join('|');
  };

  Hours.init = function (context) {
    ctx = context;
    parking = document.createElement('div');
    parking.className = 'dt-hours-parking';
    parking.hidden = true;
    parking.setAttribute('aria-hidden', 'true');
    document.body.appendChild(parking);
  };

  Hours.html = function (state, row) {
    var key = Hours.key(row, state.lang, state);
    return '<section class="dt-sec dt-hours" data-section="hours" data-hours-key="' + esc(key) + '">' +
      '<h2 class="t-group-title">' + esc(tr('营业时间')) + '</h2>' +
      '<div class="dt-hours-state" aria-live="polite"></div>' +
      '<div class="dt-hours-widget"></div>' +
      '</section>';
  };

  function entryFor(key) {
    return entries[key] || (entries[key] = {
      status: 'idle', widget: null, promise: null, remainingDaily: null,
      error: '', resetAt: null, monthlyResetAt: null, timer: null
    });
  }

  function sourceLink(row) {
    return '<a class="dt-link" href="' + esc(row.id) + '" target="_blank" rel="noopener">' +
      esc(tr('在 Tabelog 打开')) + '</a>';
  }

  function googleLink(row) {
    return 'https://www.google.com/maps/search/?api=1&query=' +
      encodeURIComponent((row.lat || '') + ',' + (row.lon || '')) +
      '&query_place_id=' + encodeURIComponent(placeId(row));
  }

  function resetLabel(value, lang) {
    if (!value) return '';
    var date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '';
    return new Intl.DateTimeFormat(locale(lang), {
      year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    }).format(date);
  }

  function fallbackLinks(row) {
    return '<p class="dt-hours-fallback t-secondary">' + sourceLink(row) +
      ' · <a class="dt-link" href="' + esc(googleLink(row)) + '" target="_blank" rel="noopener">' +
      esc(tr('在 Google Maps 打开')) + '</a></p>';
  }

  function limitExpired(entry) {
    var at = entry.resetAt || entry.monthlyResetAt;
    return !!at && Number.isFinite(new Date(at).getTime()) && new Date(at).getTime() <= Date.now();
  }

  function scheduleReset(entry) {
    clearTimeout(entry.resetTimer);
    var at = entry.resetAt || entry.monthlyResetAt;
    var due = at && new Date(at).getTime();
    if (!Number.isFinite(due)) return;
    var delay = due - Date.now();
    if (delay <= 0) { entry.status = 'idle'; paint(); return; }
    entry.resetTimer = setTimeout(function () { scheduleReset(entry); }, Math.min(delay, 2147483647));
  }

  function button(label, action) {
    return '<button type="button" class="btn btn-secondary dt-hours-button t-control" data-act="' + action + '">' +
      esc(tr(label)) + '</button>';
  }

  function statusHtml(entry, state, row) {
    if (!placeId(row)) {
      return '<p class="dt-hours-message t-secondary">' + esc(tr('未匹配到 Google 地点，可到 Tabelog 查看营业信息。')) +
        ' ' + sourceLink(row) + '</p>';
    }
    if (!signedIn(state)) {
      return '<p class="dt-hours-message t-secondary">' + esc(tr('登录 Google 账号后可查看营业时间。')) + '</p>' +
        button('使用 Google 账号登录', 'hours-login');
    }
    if (!config().apiKey || !config().permitUrl) {
      return '<p class="dt-hours-message t-secondary">' + esc(tr('营业时间服务暂不可用，请稍后再试。')) + '</p>' + fallbackLinks(row);
    }
    if (entry.status === 'loading') {
      return '<p class="dt-hours-message t-secondary" role="status">' + esc(tr('正在加载营业时间…')) + '</p>';
    }
    if (entry.status === 'loaded') {
      var count = Number.isFinite(entry.remainingDaily)
        ? '<span class="dt-hours-balance">' + esc(tr('今日还可查看 {n} 次', { n: entry.remainingDaily })) + '</span>' : '';
      return '<p class="dt-hours-source t-secondary">' +
        '<a href="' + esc(googleLink(row)) + '" target="_blank" rel="noopener">' + esc(tr('营业时间由 Google 提供')) + '</a>' +
        ' · ' + esc(tr('营业时间可能临时变更，请到店前确认。')) + count +
        (mapsLanguage && mapsLanguage !== locale(state.lang) ?
          '<span class="dt-hours-language">' + esc(tr('Google 内容沿用首次查看时的语言。')) + '</span>' : '') +
        '<span class="dt-hours-language">' + esc(tr('若未显示时段，请到 Google Maps 查看。')) + '</span></p>';
    }
    if (entry.status === 'daily_limit' || entry.status === 'monthly_limit') {
      var daily = entry.status === 'daily_limit';
      var when = resetLabel(entry.resetAt || entry.monthlyResetAt, state.lang);
      var message = daily ? '今天的查看次数已用完。' : '本月的营业时间查看额度已用完。';
      return '<p class="dt-hours-message t-secondary" role="status">' + esc(tr(message)) +
        (when ? ' ' + esc(tr('{time} 后可再试。', { time: when })) : '') + '</p>' + fallbackLinks(row);
    }
    if (entry.status === 'error') {
      return '<p class="dt-hours-message t-secondary" role="status">' + esc(tr('营业时间加载失败。再次尝试会使用一次查看额度。')) + '</p>' +
        button('重试营业时间', 'hours-retry') + fallbackLinks(row);
    }
    if (entry.status === 'unavailable') {
      return '<p class="dt-hours-message t-secondary" role="status">' + esc(tr('营业时间服务暂不可用，请稍后再试。')) + '</p>' +
        button('重试营业时间', 'hours-retry') + fallbackLinks(row);
    }
    return '<p class="dt-hours-message t-secondary">' +
      esc(tr('每次查看会使用一次额度；每日及每月额度用完后会显示恢复时间。')) + '</p>' +
      button('查看营业时间', 'hours-open');
  }

  function paint() {
    if (!current || !current.section.isConnected) return;
    var state = ctx.App.state;
    if (Hours.key(current.row, current.lang, state) !== current.key) return;
    var entry = entryFor(current.key);
    if ((entry.status === 'daily_limit' || entry.status === 'monthly_limit') && limitExpired(entry)) entry.status = 'idle';
    var heading = current.section.querySelector('h2');
    if (heading) heading.textContent = tr('营业时间');
    var host = current.section.querySelector('.dt-hours-state');
    var slot = current.section.querySelector('.dt-hours-widget');
    if (!host || !slot) return;
    host.innerHTML = statusHtml(entry, state, current.row);
    slot.hidden = entry.status !== 'loading' && entry.status !== 'loaded';
    if (entry.widget && entry.widget.parentNode !== slot) slot.appendChild(entry.widget);
  }

  Hours.park = function () {
    if (!current) return;
    var slot = current.section.querySelector('.dt-hours-widget');
    if (slot && parking) {
      while (slot.firstChild) parking.appendChild(slot.firstChild);
    }
    current = null;
  };

  Hours.attach = function (root, row, lang, state) {
    var section = root.querySelector('[data-section="hours"]');
    if (!section) return;
    var key = Hours.key(row, lang, state);
    if (current && current.section !== section) Hours.park();
    current = { section: section, row: row, lang: lang, key: key };
    section.setAttribute('data-hours-key', key);
    paint();
  };

  function withTimeout(promise, ms) {
    var timer;
    return Promise.race([
      Promise.resolve(promise),
      new Promise(function (_, reject) {
        timer = setTimeout(function () { reject(new Error('timeout')); }, ms);
      })
    ]).finally(function () { clearTimeout(timer); });
  }

  function loadMaps(lang) {
    if (mapsPromise) return mapsPromise;
    var cfg = config();
    if (!cfg.apiKey) return Promise.reject(new Error('missing key'));
    var wantedLanguage = locale(lang);
    var script = null;
    var callback = '__jpfoodmapPlacesReady';
    var loading;
    if (window.google && window.google.maps && window.google.maps.importLibrary) {
      loading = Promise.resolve();
    } else {
      loading = new Promise(function (resolve, reject) {
        script = document.createElement('script');
        window[callback] = resolve;
        script.onerror = function () { reject(new Error('Maps JavaScript failed')); };
        script.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(cfg.apiKey) +
          '&v=weekly&loading=async&libraries=places&language=' + encodeURIComponent(wantedLanguage) +
          '&callback=' + callback;
        document.head.appendChild(script);
      });
    }
    mapsPromise = withTimeout(loading, 12000)
      .then(function () {
        if (!window.google || !window.google.maps || !window.google.maps.importLibrary) throw new Error('Maps unavailable');
        return withTimeout(Promise.all([
          window.google.maps.importLibrary('places'),
          customElements.whenDefined('gmp-place-details')
        ]), 12000);
      })
      .then(function () { mapsLanguage = wantedLanguage; delete window[callback]; })
      .catch(function (error) {
        mapsPromise = null;
        delete window[callback];
        if (script) script.remove();
        throw error;
      });
    return mapsPromise;
  }

  async function permit(place, requestId) {
    var cfg = config();
    var options = {
      method: 'POST', credentials: 'include',
      headers: Object.assign({}, ctx.Data && ctx.Data.authHeaders ? ctx.Data.authHeaders() : {}, { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ placeId: place, requestId: requestId })
    };
    var response;
    try {
      response = await withTimeout(fetch(cfg.permitUrl, options), 10000);
    } catch (error) {
      // A lost reply may have consumed the permit. The same ID makes retry idempotent.
      response = await withTimeout(fetch(cfg.permitUrl, options), 10000);
    }
    var data = {};
    try { data = await withTimeout(response.json(), 5000); } catch (error) { /* invalid reply */ }
    if (response.ok && data.allowed === true && data.requestId === requestId) return data;
    var failure = new Error(data.error || 'unavailable');
    failure.code = data.error || (response.status === 401 ? 'unauthorized' : 'unavailable');
    failure.resetAt = data.resetAt || null;
    failure.monthlyResetAt = data.monthlyResetAt || null;
    throw failure;
  }

  function createWidget(row, entry) {
    var widget = document.createElement('gmp-place-details');
    var request = document.createElement('gmp-place-details-place-request');
    request.setAttribute('place', placeId(row));
    var contents = document.createElement('gmp-place-content-config');
    contents.appendChild(document.createElement('gmp-place-opening-hours'));
    contents.appendChild(document.createElement('gmp-place-attribution'));
    widget.appendChild(request);
    widget.appendChild(contents);
    widget.addEventListener('gmp-load', function () {
      clearTimeout(entry.timer);
      if (entry.widget !== widget) return;
      entry.status = 'loaded';
      paint();
    });
    widget.addEventListener('gmp-error', function () {
      clearTimeout(entry.timer);
      if (entry.widget !== widget) return;
      entry.status = 'error';
      widget.remove();
      entry.widget = null;
      paint();
    });
    entry.widget = widget;
    entry.timer = setTimeout(function () {
      if (entry.widget !== widget || entry.status !== 'loading') return;
      entry.status = 'error';
      widget.remove();
      entry.widget = null;
      paint();
    }, 20000);
    if (current && current.key === Hours.key(row, current.lang, ctx.App.state)) paint();
    else parking.appendChild(widget);
  }

  Hours.activate = function (row, lang) {
    var state = ctx.App.state;
    if (!placeId(row)) return Promise.resolve();
    if (!signedIn(state)) {
      ctx.act.openOverlay('account', null);
      return Promise.resolve();
    }
    var key = Hours.key(row, lang, state);
    var entry = entryFor(key);
    if ((entry.status === 'daily_limit' || entry.status === 'monthly_limit') && limitExpired(entry)) entry.status = 'idle';
    if (entry.status === 'loading' || entry.status === 'loaded' ||
        entry.status === 'daily_limit' || entry.status === 'monthly_limit') return entry.promise || Promise.resolve();
    if (!config().apiKey || !config().permitUrl) { entry.status = 'unavailable'; paint(); return Promise.resolve(); }
    entry.status = 'loading';
    paint();
    var requestedAccount = accountKey(state);
    entry.promise = (async function () {
      try {
        var result = await permit(placeId(row), crypto.randomUUID());
        if (accountKey(ctx.App.state) !== requestedAccount) { entry.status = 'idle'; return; }
        await loadMaps(lang);
        if (accountKey(ctx.App.state) !== requestedAccount) { entry.status = 'idle'; return; }
        entry.remainingDaily = Number.isFinite(Number(result.remainingDaily)) ? Number(result.remainingDaily) : null;
        createWidget(row, entry);
      } catch (error) {
        entry.status = error.code === 'daily_limit' || error.code === 'monthly_limit' ? error.code :
          (error.code === 'unauthorized' ? 'idle' : 'unavailable');
        entry.resetAt = error.resetAt || null;
        entry.monthlyResetAt = error.monthlyResetAt || null;
        if (entry.status === 'daily_limit' || entry.status === 'monthly_limit') scheduleReset(entry);
        if (error.code === 'unauthorized') {
          ctx.act.invalidateSession();
          ctx.act.openOverlay('account', null);
        }
        paint();
      } finally {
        entry.promise = null;
      }
    })();
    return entry.promise;
  };

  window.Hours = Hours;
})();
