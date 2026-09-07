# jpfoodmap Android APP 2.3.0 · 标准（STANDARDS）

> 给主人的 10 行版本：这份文件规定「APP 做成什么样才算合格」。每一条都带**验收方法**——
> 要么是模拟器上能跑的命令，要么是真机上你自己按一下就能确认的动作。检查员按这里逐条打勾；
> 实施者按这里逐条自测。APP 就是把 jpfoodmap.com 装进一个 Kotlin 壳（WebView）里，网站本身
> 不重写；壳只负责：开合盖不重载、三种屏幕几何、返回键、外链去向、原生 Google 登录、分享、
> 定位授权、通知、节电、诊断、侧载升级。只为你的 Galaxy Z Fold 8 适配，其它机型以后再说。

范本：`/mnt/d/Dropbox/proj_2026/dashboard/android/`（只读）。凡本文件写「同 dashboard」，指
其 `docs/APP-SHELL-SPEC.md` 与 `app/src/main/kotlin/.../app/*.kt` 里已经在 Fold 8 真机跑过的做法。

术语：**cover** = 外屏（1248×1972 @420dpi = 475×751 dp）；**inner** = 内屏全屏
（2446×1848 @420 = 932×704 dp，横向为自然方向；竖向 704×932）；**split60** = 内屏 Samsung
60/40 分屏中 APP 占 60% 的窗口（横向 591×689 dp，竖向 438×917 dp）。这些数字来自 dashboard
在这台真机上的 Diagnostics 实测（`ANDROID-APP-PLAN.md` §4，2026-09-02），不是推算。
（网站自己的 smoke 视口 416×657 / 616×816 是另一套口径，见 §2.6。）

---

## 0. 总则

| # | 规则 | 验收 |
|---|---|---|
| 0.1 | **网站是主体，壳是配角。** 壳不得替页面做页面已经在做的事（语言、筛选、收藏、地图状态都由页面与其 localStorage/KV 管）。 | 静态审查：壳里没有任何读写 `localStorage`、KV、`tabelog.*` 键的代码。 |
| 0.2 | **红线不碰**：不改 localStorage 键、KV blob 结构、Worker API、内置景点 id（见仓库 `CLAUDE.md`）。 | `uv run python scripts/verify_build.py` 绿；`tests/compat/run.py` 绿；`node tests/worker/run.mjs` 绿。 |
| 0.3 | **页面对壳的每一处依赖都用 `window.Native` 特性检测守卫**（`typeof Native.x === 'function'`），且在浏览器里行为与今天完全一致。 | `tests/smoke_playwright.py` 五视口绿；人工：Chrome 里登录按钮、分享、安装引导与改前一致。 |
| 0.4 | **秘密不进仓库、不进日志**：APK 不含任何 token；日志/Toast 不打印 URL（`?lang`、`?r` 无害，但规则一律）。 | `grep -rn "SESSION_HMAC\|DASHBOARD_TOKEN" android/` 无命中；`Log.` 调用审查。 |
| 0.5 | **可复现构建**：从干净 scratch 一条命令出 APK。 | `rm -rf ~/.cache/jpfoodmap-android && android/build.sh` 成功。 |

---

## 1. UI / UX（壳的可见部分）

| # | 规则 | 验收 |
|---|---|---|
| 1.1 | 壳**没有**地址栏、标题栏、底部栏、浮动按钮；屏幕上只有页面。 | 三几何截图：页面顶到状态栏、底到导航条，无壳 chrome。 |
| 1.2 | **Edge-to-edge**（targetSdk 36 强制）：状态栏/导航条透明；系统栏 inset **不由壳消费**，透传给 WebView，页面用 `env(safe-area-inset-*)` 自己留白（页面已有 `viewport-fit=cover` 与 10 处 `env()`）。 | 诊断 JSON：`env.t` ≈ `insets.top/density`、`env.b` ≈ `insets.bottom/density`（±1）。cover AVD 期望 `env.t=24`、`env.b=24`（模拟器的条）；真机期望 42/15（cover）、40/15（inner）、40/0（split）。 |
| 1.2a | **顶部 inset 的双保险**（2.1.0，bug A-1）。页面的固定顶栏按 `max(env(safe-area-inset-top), var(--app-inset-top))` 留白；壳在每次 inset 变化（折叠 / 旋转 / 分屏改尺寸）与每个文档首帧把 `statusBars∪cutout` 的顶边 inset 换算成 CSS px 写进 `--app-inset-top`。inset 仍然**不消费**、仍然透传，所以 `env()` 依旧是主来源，变量只在「WebView 报 0 而状态栏确实存在」时起作用；两者都对时相等，`max()` 只留白一次。 | 诊断 JSON：`safeVar=true` 且 `appInsetTop` == 页面半的 `env.t`。或 debug 包上 `tools/wv-eval.py "getComputedStyle(document.documentElement).getPropertyValue('--app-inset-top')"`。模拟器上开关 `cmd overlay enable com.android.internal.display.cutout.emulation.tall` 可以造出一次真实的 inset 变化（24 → 48）来验证重发。 |
| 1.3 | **强制浅色**：网站 `color-scheme: only light`、`theme-color #ffffff`。壳主题 `Theme.DeviceDefault.Light.NoActionBar`，状态栏/导航条图标始终深色（`windowLightStatusBar=true`），`setAlgorithmicDarkeningAllowed(false)`，`forceDarkAllowed=false`。系统切深色模式**不**改变页面。 | 模拟器 `adb shell cmd uimode night yes` 后截图：页面仍浅色、状态栏图标仍深色、Activity 未重建（`activityCreates` 不变）。 |
| 1.4 | **窗口底色**与页面首屏一致：`windowBackground` / WebView 背景 = `#fdf6e3`（manifest `background_color`），冷启动无白闪。 | 冷启动录屏或连续截图：splash → 页面之间无纯白帧。 |
| 1.5 | **启动画面**：`core-splashscreen`，图标 = 网站 maskable 图标（`docs/icons/icon-japan-emoji-v2-maskable-512.png`），底色 `#fdf6e3`；splash 保持到页面首帧（`onPageCommitVisible`），上限 3 s。 | `am start -W` 后立即截图：splash；≤3 s 后截图：页面。 |
| 1.6 | **应用图标**：自适应图标（前景 = 同上 PNG 按密度缩放，背景 `#fdf6e3`），抽屉名称 `Japan Foodmap`（zh-CN 资源：`日本美食地图`）。 | `aapt2 dump badging` 显示 `application-label` 与 `application-label-zh-CN`；launcher 截图。 |
| 1.7 | **错误面板**（页面主文档加载失败/SSL 失败/5xx）：覆盖在保留的 WebView 之上，只有「重试」「设置」两个按钮，文案中文，不显示 URL。 | 模拟器断网冷启动（无 SW 缓存时）→ 面板；联网点重试 → 页面。 |
| 1.7a | **面板不抢在 Service Worker 前面**（2026-09-06 加入）。断网时 Chromium 会先给主文档报一次 `ERR_INTERNET_DISCONNECTED`，网站的 SW 随后才用缓存把同一次导航接住——两者在 WebView API 里是同一个 URL 上的 `onReceivedError` + `onPageFinished`，**光看 URL 分不开**。所以：① `showError` 只是「布防」，面板延迟 `ERROR_PANEL_GRACE_MS`(1200 ms) 才真正显示，期间任何一次成功落地都会取消它；② `onPageFinished` 撞上 `url == errorUrl` 时先当 ERROR，再用 `resolveRescuedPage` 问文档一句 `navigator.serviceWorker.controller` ——**这次导航确实 commit 了文档**（`committedUrl == url`，把「SSL 被取消、屏幕上还是上一页」排除掉）**且该文档是 SW 送出来的**，才撤下面板转 READY。Chromium 自带的错误页是内部文档、没有 controller，所以 §9.2 的路径一步没变。 | §9.1 的模拟器流程：断网冷启动 → 地图 + 离线条（`shell-rework/foldcover/offline-cold.png`）；§9.2 的流程：`pm clear` 后断网冷启动 → 面板 → 联网重试 → 页面（`shell-rework/offline-92/`）。 |
| 1.8 | **文字**：壳自己的字符串默认简体中文（`values/`），英文在 `values-en/`；页面文字由网站语言开关管。 | `aapt2 dump resources` 有两套 `strings`。 |

---

## 2. 屏幕适配（三几何 + 多窗口 + 字号）

| # | 规则 | 验收 |
|---|---|---|
| 2.1 | **CSS 宽度 = dp 宽度**：`useWideViewPort=true`、`loadWithOverviewMode=false`、尊重页面 `<meta viewport>`（`width=device-width, initial-scale=1, viewport-fit=cover`）。 | 诊断 JSON：cover `innerWidth=475`；inner 横 `932`、竖 `704`；split60 竖 `591`、横 `688`（`emu.sh rotate landscape`；窗口 689 dp，布局视口 688 CSS px——1808/2.625=688.76，两个数并存是对的）。`dpr=2.625`。 |
| 2.2 | 三几何各自的页面布局由**网站断点**（480/750/1100 + 容器查询；750 起 2.3.0）决定；壳不注入任何布局 CSS。`fold8inner60`（591/688 CSS px）自 2.3.0 起落在 phone 档，筛选并入左栏。 | 三几何截图各一张（首页、打开一张餐厅卡、打开筛选面板），与同宽度 Chrome 截图目测一致。 |
| 2.3 | **多窗口**：`resizeableActivity=true`、不锁方向、`configChanges` 覆盖 `orientation|screenSize|smallestScreenSize|screenLayout|density|uiMode|keyboard|keyboardHidden|fontScale|locale|layoutDirection`；分屏拖动过程中页面持续重排，不重载。 | fold8inner60 AVD：`emu.sh rotate landscape` ↔ `portrait` 各一次，诊断 `pageLoads` 不变、`bootId` 不变、`activityCreates=1`。 |
| 2.4 | **字号**：`textZoom = 系统 fontScale × 100`（「跟随系统」，默认），或固定 90/95/100/115/130（设置页）。WebView 默认**不**跟随系统字号，壳必须乘进去。 | `adb shell settings put system font_scale 1.3` → 诊断 `textZoom=130`；设置页选 100 → `textZoom=100`；恢复 `font_scale 1.0`。 |
| 2.5 | **键盘**：WebView Chromium ≥144 自己收缩 visual viewport（`imeMode=WEBVIEW`）；旧版走壳的原生 padding。 | 诊断 `imeMode=WEBVIEW`（模拟器 WebView 145、真机 151）；搜索框获焦时截图，输入框在键盘之上。 |
| 2.6 | 网站 smoke 视口（416×657 / 616×816 / 816×616）与本机实测（475×751 / 932×704 / 704×932）**不一致**，APP 验收以实测为准；是否把 smoke 视口改成实测值是网站侧决定（不在本次范围）。 | 记录在 `docs/PLAN.md` §决策；不阻塞。 |
| 2.7 | **页面缩放**：与 Chrome 一致——地图上的捏合由 Leaflet 消费，页面其它区域允许捏合（网站已放开 `user-scalable`）；不显示 ± 缩放按钮。 | `builtInZoomControls=true`、`displayZoomControls=false`；真机手测。 |

---

## 3. 合盖 ↔ 开盖连贯性

| # | 规则 | 验收 |
|---|---|---|
| 3.1 | **Activity 不重建、WebView 不重载**：`singleTask` + 上述 `configChanges`；折叠/展开只触发 `onConfigurationChanged` → WebView 重新布局 → 页面 `resize` 事件 → Leaflet `trackResize` 自动 `invalidateSize()`。 | fold8inner AVD 折叠代理：`adb shell wm size 1248x1972`（= cover 几何）→ `wm size reset`（= inner），前后诊断：`activityCreates=1`、`pageLoads` 不变、`bootId` 不变、`innerWidth` 475→932。 |
| 3.2 | **页面状态全部保住**：地图中心/缩放、打开的餐厅卡（bottom sheet）、筛选面板、搜索框输入与焦点、滚动位置。 | 脚本：深链打开一张卡 → 折叠代理 → 展开代理 → `evaluateJavascript` 检查卡仍打开（T7 脚本用页面 DOM 判断）；截图对比。 |
| 3.3 | **地图无灰块/错位**：几何切换后 500 ms 内地图瓦片铺满新尺寸。 | 切换后 1 s 截图：无灰色空白区。 |
| 3.4 | **进程被杀后的恢复**：`onSaveInstanceState` 保存最后 URL（去 query）；重开回到同一 URL（页面自行从 localStorage 恢复视图）。 | `adb shell am kill com.fredhli.jpfoodmap` 后从最近任务重开：页面正常。 |
| 3.5 | One UI「在外屏继续使用应用」需按 APP 开启，否则合盖回锁屏——这是系统开关，写进侧载说明。 | 文档项（`android/README.md`）。 |

---

## 4. 返回键语义

| # | 规则 | 验收 |
|---|---|---|
| 4.1 | 网站用 `history.pushState` 浮层栈（每个浮层一条 state-only 记录）。壳：`OnBackPressedCallback`，**仅当 `webView.canGoBack()` 为真时启用**，回调调 `webView.goBack()` → `popstate` → 页面关掉最上层浮层。栈空时回调禁用，系统预测式返回退出 APP。**绝不**重写 `onBackPressed`，也**绝不**改用 `copyBackForwardList().currentIndex > 0` 当判据（2.2.0 实测，见 4.1a）。 | 脚本：`verify-flows.sh back` 三段，判据只读 `dumpsys window` 与诊断 JSON 的 `back` 块，release 包上照样能红。读不到 `back` 块时先强制一次 `JPFM_DIAG_SOURCE=native` 再判 SKIP：跑法自己的 source 探测在 APP 第一次 READY 之前就跑完（预算 8 s），会把冷启动慢的 2.2.0 包误判成 probe，而 probe 半边（页面视角）根本没有 `back` 块 —— 2026-09-07 复核抓到这条曾让整段在默认跑法下回落成 SKIP。 |
| 4.1a | **例外，不修，与 Chrome 一致**：页面在**没有用户手势**时 `pushState`（`?r=` 深链冷启动开卡、热深链 `__jpfmOpenShare` 开卡）会触发 Chromium 的 history-manipulation intervention —— 下面那条历史被标成 `skip_on_back_forward_ui`，`canGoBack()` 因此答 false，第一次返回直接退出 APP，卡片不先关。2026-09-07 实测：`canGoBackOrForward(-1)` 同样是 false，强行 `goBackOrForward(-1)` 一动不动**并把这次返回吞掉**，所以壳侧没有任何 API 能绕过；在 Chrome 里新标签页打开同一 URL 行为相同。 | 诊断 JSON：`back.index > 0` 而 `back.canGoBack=false`。`verify-flows.sh` 的第三段断言的是**不变式**（要么退出、要么弹掉一层，绝不能什么都不发生）。 |
| 4.2 | 已知：语言切换是整页导航（`?lang=`），返回会回到上一语言页面，与 Chrome 一致，不修。 | 记录。 |

---

## 5. 外链去向

| # | 规则 | 验收 |
|---|---|---|
| 5.1 | **分类**（同 dashboard `Links.classify`）：`https://jpfoodmap.com/*` = IN_APP（含 `privacy.html`、`404.html`）；其它 http(s) = EXTERNAL；`intent://` 走 `openIntentUri`；`mailto/tel/geo/market` 走系统；其余（`javascript:` `file:` `data:` `blob:`）静默丢弃。 | JVM 单测 `LinksTest` 覆盖矩阵。 |
| 5.2 | EXTERNAL 的阶梯：① `FLAG_ACTIVITY_REQUIRE_NON_BROWSER`（**Google Maps 链接直接打开 Maps APP**，Tabelog 若有 APP 也是）→ ② 用户策略：**Chrome Custom Tab（默认）** / Chrome / 系统默认浏览器 → ③ 兜底 `ACTION_VIEW`。 | 模拟器：卡片「Tabelog ↗」→ Custom Tab 覆盖在 APP 任务上（`dumpsys activity` 同一 task，无第二张最近任务卡），返回箭头回到页面；「Google Maps」→ 若装了 Maps 则 Maps，否则按策略。 |
| 5.3 | `target=_blank` / `window.open` 走 `onCreateWindow` → `PopupCatcher` 抓 URL → 5.1 分类；主 WebView 永不载入外域文档。 | 模拟器：点外链后 `webView.url` 仍是 jpfoodmap.com。 |
| 5.4 | **设置页**可切换策略；`Native.openExternal(url)` 保证离开 APP（对本域 URL 也钉住浏览器包）。 | 设置切到 Chrome 后再点外链 → Chrome。 |

---

## 6. 深链 / App Links

| # | 规则 | 验收 |
|---|---|---|
| 6.1 | `intent-filter autoVerify=true`：`https://jpfoodmap.com` 全部路径；`docs/.well-known/assetlinks.json` 列 `com.fredhli.jpfoodmap` 与 `.debug`，指纹 `78:9F:…:85:D1`（= `~/.android/debug.keystore`）。 | `apksigner verify --print-certs` 的 SHA-256 与 assetlinks 一致；上线后 `curl -sI https://jpfoodmap.com/.well-known/assetlinks.json` → 200 `application/json`；真机 `设置→应用→Japan Foodmap→默认打开` 显示已验证。 |
| 6.2 | **冷启动深链** `https://jpfoodmap.com/?r=<base36>`：直接 `loadUrl`，页面自行消费 `?r=`。 | 模拟器 `am start -a VIEW -d 'https://jpfoodmap.com/?r=<id>'`（先 `pm set-app-links … 1 jpfoodmap.com` 强制批准）→ 该餐厅卡打开。 |
| 6.3 | **热路径**（APP 已开）：`onNewIntent` → 若页面 READY 且同源 → `evaluateJavascript(window.__jpfmOpenShare(id))`；页面返回 false 或钩子不存在 → 退化为 `loadUrl`。 | APP 在前台时再发同一 intent → 卡切换、`pageLoads` 不变。 |
| 6.4 | `?lang=` 深链走 `loadUrl`（整页导航本就是页面语义）。 | 同 6.2。 |
| 6.5 | 长按图标快捷方式：仅「APP 设置」一项（网站没有可路由的页面）。 | 长按图标可见。 |

---

## 7. 登录（原生 Google → Worker 会话 cookie）

| # | 规则 | 验收 |
|---|---|---|
| 7.1 | **WebView 里不能走 GIS/OAuth**（Google `disallowed_useragent`）。APP 内：页面把 `#ssm-signin-btn` 换成一个原生风格按钮「使用 Google 登录」→ `Native.signIn(req, false)` → 壳用 **Credential Manager**（`GetGoogleIdOption`，`serverClientId` = map.py 的 `GOOGLE_CLIENT_ID`）拿 `id_token` → 通过 `JavaScriptReplyProxy` 回 `{t:"credential", req, idToken}` → 页面调既有 `onGoogleCredential({credential})` → `POST /api/session`（`credentials:'include'`）→ cookie 落在 WebView 自己的 cookie jar → reload。**Worker、API、cookie 规则零改动。** | 真机：登录 → 头像出现 → 杀 APP 重开仍登录态 → 在网站（Chrome）看到同一份收藏。 |
| 7.2 | 壳：`CookieManager.setAcceptCookie(true)`、`setAcceptThirdPartyCookies(webView, true)`（api. 子域对页面是 same-site，但 WebView 的第三方判定不赌）、`onPause` 时 `CookieManager.flush()`。 | 静态审查 + 7.1 真机。 |
| 7.3 | `accounts.google.com/gsi/client` 在 APP 内用 `shouldInterceptRequest` 拦成空脚本（省 ~100 KB/次，反正不能用）。 | `adb logcat` 无 gsi 请求；页面登录区显示原生按钮而非 Google iframe。 |
| 7.4 | **未建 Android OAuth client 之前的退化**：Credential Manager 报 `GetCredentialException`（开发者控制台未配置 / 无账号）→ 页面状态行显示既有文案「登录处理失败」+ 壳 Toast「请在浏览器中登录后再回来」并提供「在浏览器中打开」（Chrome 打开 jpfoodmap.com）。**只本地模式也完全可用**（网站本就如此）。 | 模拟器（无 Google 账号）：点登录 → 该退化路径；不崩溃。 |
| 7.5 | 90 天后 cookie 过期：页面 `silentReAuth` 在 APP 内改走 `Native.signIn(req, true)`（`autoSelect` + `filterByAuthorizedAccounts`），成功则静默换新 cookie。 | 真机长期项；模拟器只验「静默失败不弹 UI、不崩」。 |
| 7.6 | 退出登录：页面既有 `DELETE /api/session` + 本地清理；APP 内额外 `Native.signOut()` → `clearCredentialState()`，下次登录重新选账号。 | 真机：退出后再登录出现账号选择。 |
| 7.7 | 壳**从不**读写 `tabelog.auth`、cookie 值；`id_token` 只经 replyProxy 交给页面一次，不落日志。 | 静态审查。 |

---

## 8. 分享、定位、震动

| # | 规则 | 验收 |
|---|---|---|
| 8.1 | WebView 无 `navigator.share`。页面 `shareRestaurant()` 在 APP 内优先 `Native.share(title, url)` → `ACTION_SEND text/plain` chooser；无 Native 时维持现有「复制链接」退化。 | 模拟器：卡片分享 → 系统分享面板（`dumpsys activity` 顶层 ChooserActivity）。 |
| 8.2 | **定位**：`setGeolocationEnabled(true)`；`WebChromeClient.onGeolocationPermissionsShowPrompt` 只对 `https://jpfoodmap.com` 放行；首次由页面的定位 FAB 触发 → 壳按需申请 `ACCESS_FINE_LOCATION`（可降级 COARSE）→ `callback.invoke(origin, granted, retain=false)`。**启动时绝不申请**。 | 模拟器：冷启动无权限弹窗；点 FAB → 系统权限弹窗；`adb emu geo fix 139.76 35.68` 后地图飞到东京；`dumpsys package` 权限仅 INTERNET/LOCATION×2/POST_NOTIFICATIONS。 |
| 8.3 | 定位插件 `watch:false`、`maximumAge 10min` 是页面既有设置；壳不开 `watchPosition`，无前台定位服务。 | 静态审查。 |
| 8.4 | `Native.haptic()`：`performHapticFeedback(CONFIRM)`，不需要 VIBRATE 权限；页面 2.0.0 不调用，接口保留。 | 无。 |

---

## 9. 离线与更新

| # | 规则 | 验收 |
|---|---|---|
| 9.1 | 网站的 Service Worker 在 WebView 里照常工作：`domStorageEnabled=true`、`cacheMode=LOAD_DEFAULT`、不拦截同源请求。联网跑过一次后断网冷启动仍出地图（页面自己的离线条 `#net-offline` 可见）。**离线条依赖 `ACCESS_NETWORK_STATE`**——那条权限不在清单里时 `navigator.onLine` 恒为 true，地图照出但条不出（见 §10.3a）。 | 模拟器：联网启动等 30 s → `svc wifi disable; svc data disable` → 杀进程重开 → 地图出现 + 离线条；`svc … enable` 恢复。 |
| 9.2 | 主文档拿不到且无 SW 缓存 → 错误面板（1.7）；重试后回到同一 URL。 | 清 APP 数据后断网启动 → 面板；联网重试 → 页面。 |
| 9.3 | 网站更新由 SW 的 `reg.waiting → 有新版本` 提示处理，壳不参与。APK 更新 = Dropbox 覆盖安装；设置页显示 APP 版本 + WebView 版本；`BUILD-INFO.txt` 记 sha256。 | 设置页截图；BUILD-INFO 内容。 |

---

## 10. 节电与能耗

| # | 规则 | 验收 |
|---|---|---|
| 10.1 | 无 WorkManager、无 Service、无 AlarmManager、无 JobScheduler、无 wakelock、无 FCM、无后台网络；进程退后台只有 WebView 自己（已 `onPause()+pauseTimers()`）。 | `aapt2 dump xmltree`：**我们自己写的 `<service>` / `<receiver>` 为零**（任何 `com.fredhli.jpfoodmap*` 开头的组件出现即失败）；库合并进来的必须逐个白名单化并指到出处——2.0.0 的三条是 `androidx.credentials.playservices.CredentialProviderMetadataHolder`、`com.google.android.gms.auth.api.signin.RevocationBoundService`、`androidx.profileinstaller.ProfileInstallReceiver`，外加 provider `androidx.startup.InitializationProvider`，都不排期、不耗电。`dumpsys jobscheduler`/`alarm`/`power` 中本包无条目；`dumpsys activity services` 只允许 `org.chromium.` 的 WebView 渲染进程。实现见 `tools/power-audit.sh` §1。 |
| 10.2 | `onPause`：`webView.onPause(); pauseTimers(); CookieManager.flush()`；`onResume` 反之。 | 静态审查；后台 5 分钟 `dumpsys batterystats --charged <pkg>` 无 CPU/网络计数增长（模拟器粗验）。 |
| 10.3 | 权限最小集：`INTERNET`、`ACCESS_NETWORK_STATE`、`ACCESS_COARSE_LOCATION`、`ACCESS_FINE_LOCATION`、`POST_NOTIFICATIONS`。硬性禁止：任何存储、相机、麦克风、电话、通讯录、`QUERY_ALL_PACKAGES`、`REQUEST_INSTALL_PACKAGES`、`SYSTEM_ALERT_WINDOW`、`WAKE_LOCK`、`RECEIVE_BOOT_COMPLETED`、`FOREGROUND_SERVICE*`。 | **`AndroidManifest.xml` 里我们自己声明的恰好这五项**（`tools/static-audit.sh` §4）。`aapt2 dump permissions` 读的是合并后的清单，2.0.0 上是 **8 项**：多出的三项必须逐条在 `manifest-merger-*-report.txt` 里指到具体的库并写进 `tools/power-audit.sh` 的白名单——`USE_BIOMETRIC` / `USE_FINGERPRINT`（`androidx.credentials` → `androidx.biometric`，原生登录必需，normal 级、安装时授予、系统权限页里看不到）与 `com.fredhli.jpfoodmap.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION`（`androidx.core`，signature 级，只有本 APP 能用）。**出现第四条合并权限即失败。** |
| 10.3a | `ACCESS_NETWORK_STATE` 的例外说明（2026-09-06 加入，原文只有四项）。**我们自己的 Kotlin 一行都不碰 `ConnectivityManager`**；它存在只为 Chromium：`NetworkChangeNotifierAutoDetect` 拿不到这条权限就不注册连通性回调，WebView 里的 `navigator.onLine` 于是恒为 `true`，网站自己的离线条 `#net-offline` 永远不出现，断网的用户得不到任何提示（§9.1）。protection level 是 `normal`：安装时授予、**不弹窗**、系统权限页里根本不列出，能读到的只有「有没有网、是什么网」——不涉及位置、身份、流量内容。耗电：回调是系统框架自己的，Chromium 在应用退到后台时会注销。 | 同一台模拟器同一条 offline 流程做过两次对照：无此权限 → `navigator.onLine` 期望 false 得 true、离线条不出现；有此权限 → 两条均 PASS（`gate/emulator` 与 `impl/shell-rework.md`）。**若主人不要这条权限**：删掉 manifest 那一行、把 `tools/static-audit.sh` §4 与 `tools/power-audit.sh` §1 的清单改回四项、把 §9.1 的括号句改成「APP 内不显示离线条（已知取舍）」，其余不动。 |
| 10.4 | 不启用 `WebView.setWebContentsDebuggingEnabled` 于 release；`allowFileAccess=false`、`allowContentAccess=false`、`mixedContentMode=NEVER_ALLOW`、`javaScriptCanOpenWindowsAutomatically=false`、`mediaPlaybackRequiresUserGesture=true`。 | 静态审查。 |
| 10.5 | 网络安全配置：**cleartext 关闭**（`cleartextTrafficPermitted=false`），无自定义 CA。 | `res/xml/network_security_config.xml` 审查；`http://` 请求在 logcat 报错。 |
| 10.6 | 桥只经 `addWebMessageListener`（源限定 `https://jpfoodmap.com`），**禁用** `addJavascriptInterface`。 | `grep -rn addJavascriptInterface android/app/src` 无命中。 |

---

## 11. 通知（现阶段范围）

| # | 规则 | 验收 |
|---|---|---|
| 11.1 | 没有服务端推送源，本版只做**标准 + 最小可验证能力**：一个渠道 `jpfoodmap_general`（IMPORTANCE_DEFAULT，无角标），一个「通知」开关（默认 **关**），一个「发送测试通知」按钮，通知点击打开 APP。 | 设置页：开关打开 → 系统权限弹窗（API 33+）→ 点测试 → 通知栏出现 → 点它 → APP 前台。 |
| 11.2 | `POST_NOTIFICATIONS` **只在设置页打开开关时**申请，绝不在启动时；拒绝一次后开关回弹为关并说明去系统设置开。 | 冷启动无权限弹窗；`dumpsys notification` 无本包渠道直到开关打开。 |
| 11.3 | 通知内容策略（为将来）：静音、可替换（固定 id）、不叠加、不带 URL 参数；未来推送走 FCM data-only + 客户端决定文案（同 dashboard 2.8.0），需要 Firebase 项目——**不在本版**。 | 文档项。 |

---

## 12. 诊断

| # | 规则 | 验收 |
|---|---|---|
| 12.1 | 设置页「诊断…」→ 对话框：页面半（`innerWidth/Height`、`dpr`、`visualViewport`、`env()` 四边、`bootId`、`lang`、`url` 去 query、`navigator.userAgent`、`!!window.Native`）+ 壳半（WebView 版本、insets px、`imeMode`、`textZoom`、`fontScale`、`density`、`widthDp/heightDp`、`pageState`、`pageLoads`、`activityCreates`、`back`（2.2.0：`enabled` / `canGoBack` / `index` / `size`，返回键验收在 release 包上唯一的见证）、启动耗时、最近 5 次进程退出原因）。可「复制」。 | 对话框截图；复制后粘贴出完整 JSON。 |
| 12.2 | 脚本化入口：`am start -n <pkg>/.MainActivity --ez diagnostics_log true` → READY 后把同一 JSON 单行打到 logcat tag `JpfmDiag`（release 也有；无秘密）。**`<pkg>` 必须是真正装着的那个**：debug 包是 `com.fredhli.jpfoodmap.debug`，发布包没有后缀，发到没装的那个 id 上 `am` 只回一句谁也不看的 `result code=-92`，看起来和「这个构建没有诊断」一模一样（2.0.0 审计就是这样误判成「R8 剥掉了日志」；合并后的 R8 配置里从来没有 `android.util.Log` 的 `-assumenosideeffects`）。`tools/emu.sh` 与 `tools/diag.sh` 现在向设备问一次。 | `adb logcat -d -s JpfmDiag` 取到一行合法 JSON。 |
| 12.3 | 诊断里**没有** cookie、token、query。 | 静态审查 + 输出审查。 |

---

## 13. 隐私与权限清单（写进 README）

- 权限：见 10.3；每项一句话说明用途与何时申请。
- 数据：壳自身只存两类偏好（外链策略、字号、通知开关）在 `SharedPreferences`；`allowBackup=false`、`dataExtractionRules` 全排除（cookie jar 不上云备份）。
- 网络：只连网站与 Worker 本就连接的域（页面决定）；壳自身零请求（Credential Manager 由 Google Play services 处理）。

---

## 14. 版本与发布流程

| # | 规则 | 验收 |
|---|---|---|
| 14.1 | `versionName = "2.3.0"`，`versionCode = 20300`（= major×10000 + minor×100 + patch；与网站 `APP_VERSION` 同步更新；2.0.0 = 20000）。 | `aapt2 dump badging` |
| 14.2 | `applicationId = com.fredhli.jpfoodmap`（debug 加 `.debug`）；**永久身份**，不改。 | badging |
| 14.3 | 签名：release 用 `~/.android/debug.keystore`（alias `androiddebugkey`，storepass `android`），与 dashboard 同一证书（升级路径 + assetlinks 都钉在它上；备份责任在主人，见 `dashboard/deploy/SIGNING-KEY.md`）。 | `apksigner verify --print-certs android/apk/jpfoodmap.apk` SHA-256 = `78:9F:E3:5F:02:40:43:2A:CF:C7:E1:71:50:1B:94:1C:29:B9:91:55:D3:58:CF:33:9C:78:AE:C2:10:16:85:D1` |
| 14.4 | 构建：`android/build.sh`（rsync → `$HOME/.cache/jpfoodmap-android` → `./gradlew assembleRelease`，R8 `-dontobfuscate` + shrinkResources → 原子拷回 `android/apk/jpfoodmap.apk` + `BUILD-INFO.txt`）。Gradle **从不**在 `/mnt/d` 运行。 | `find android -name build -o -name .gradle` 为空；`BUILD-INFO.txt` 有 built/published/size/sha256。 |
| 14.5 | 工具链：JDK 17、Gradle 8.14.5、AGP 8.11.1、Kotlin 2.2.21、compileSdk=targetSdk=36、minSdk 31（与 dashboard 同一组已验证的钉子）。 | `gradle/wrapper/gradle-wrapper.properties` 带 sha256；根 `build.gradle.kts`。 |
| 14.6 | APK 大小 ≤ 8 MiB（Credential Manager 拉进 Play services auth；无字体、无 Compose）。 | `BUILD-INFO.txt`；`tools/apk-contents.py` 前 15 条无意外大件。 |
| 14.7 | 侧载：Dropbox → 手机 Dropbox APP → `proj_2026/tabelog/android/apk/jpfoodmap.apk` → 允许来源一次 → 安装/覆盖安装（同证书）。`.gitignore` 排除 `*.apk`，只提交 `BUILD-INFO.txt`。 | README 步骤；`git check-ignore android/apk/jpfoodmap.apk` 命中。 |
| 14.8 | 每次发版：网站 `APP_VERSION`、APP `versionName`、`CHANGELOG.md` 三处同一版本号。 | grep 三处。 |

---

## 15. 验收几何速查

| AVD | 物理 | dp / CSS px | 用途 |
|---|---|---|---|
| `foldcover` | 1248×1972 @420 | 475×751 | 外屏；默认 |
| `fold8inner` | 2446×1848 @420 | 932×704（横，自然）；`rotate portrait` → 704×932 | 内屏；折叠代理 = `wm size 1248x1972` ↔ `wm size reset` |
| `fold8inner60` | 1552×1808 @420 | 591×689 dp；`rotate landscape` → 689×591 dp。**CSS px：竖 591、横 688**（1808/2.625=688.76，窗口进位 689 dp、布局视口舍去 688 px，两个数并存是 Chromium 对的） | 60% 分屏窗口（系统条是模拟器的，别读它的 inset） |
| `flow` | 1080×2400 @420 | 411×914 | 「普通手机还能用」，非必需 |

模拟器上**验不了**的（真机清单，见 `android/README.md`）：真实折叠（display 切换）、One UI 外屏续用开关、Google 账号登录全流程、90 天静默续期、App Links 自动验证（需网站已上线 assetlinks）、真实 inset 数值、One UI 分屏拖动。
