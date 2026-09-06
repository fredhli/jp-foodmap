# jpfoodmap Android APP 2.0.0 · 实施计划（PLAN）

> 给主人的 10 行版本：
> 1. 做法照抄 dashboard 的安卓壳（那套已经在你的 Fold 8 上跑过很多轮），换成 jpfoodmap.com，去掉小部件、字体、FCM、DataStore 这些用不上的东西。
> 2. 本机环境已验证可用：JDK 17 / SDK 36 / Gradle 8.14.5 / KVM；`foldcover` 模拟器 20 秒开机、47 秒内截到图（`audit_outputs/android-2026-09-06/plan/`）。
> 3. 登录是唯一需要你动手的地方：Google 不让 WebView 走网页登录，APP 要用原生 Google 登录，你得在 Google Cloud Console 给这个 APP 建一个「Android」类型的 OAuth client（包名 + 证书 SHA-1，见 §9）。没建之前 APP 也能用，只是登录按钮会提示去浏览器登录。
> 4. 网站只加一小段带守卫的「app bridge」代码，浏览器里行为不变；不碰任何存储键、KV、Worker。
> 5. 切成 8 个任务：骨架 → （壳 / 登录桥 / 深链 / 外链·设置·诊断 / 通知·节电·加固 / 验收脚本）并行 → 文档与集成。
> 6. 产物：`android/apk/jpfoodmap.apk`（Dropbox 同步到手机侧载）+ `BUILD-INFO.txt`；版本 2.0.0（versionCode 20000）。
> 7. 签名沿用 `~/.android/debug.keystore`，与 dashboard 同证书；App Links 指纹相同。
> 8. 四个检查员各有清单（§8）；三几何 + 折叠代理 + 返回键 + 深链 + 外链 + 断网都能在模拟器上脚本化验证；真机项列在 §9。
> 9. 需要你拍板的：包名、外链默认走 Custom Tab 还是 Chrome、APP 名称、是否要 www 域名。
> 10. 标准（每条带验收方法）在同目录 `STANDARDS.md`。

---

## 1. 目标与范围

- 把 jpfoodmap.com（2.0.0：Leaflet 静态站 + Cloudflare Worker 同步 + PWA）包成 Kotlin WebView 壳 APK，**只为 Galaxy Z Fold 8**：cover 475×751 dp、inner 932×704 dp、split60 591×689 dp 三几何 + 合盖↔开盖连贯。
- 网站与 APP 同步版本 2.0.0；之后每次网站发版 APP 同步 bump。
- 不做：小部件、FCM 推送（无服务端源）、深色模式（网站强制浅色）、生物识别、多机型适配。

可写范围（硬约束）：`android/**`；`docs/.well-known/assetlinks.json`；`src/tabelog/scrape/map.py` 里**一个**新的、`window.Native` / UA 守卫的 app-bridge 区块；`README.md` / `CHANGELOG.md` 只追加安卓段落。**不做 git 写操作**；不碰生产 KV/API 写；dashboard 目录只读。

---

## 2. 范本（dashboard/android）复用表

| dashboard 文件 | 结论 | 说明 |
|---|---|---|
| `build.sh` | **改写复用** | 去掉 token 绊线（我们没有秘密）；变量改 `JPFM_ANDROID_SRC/BUILD`，scratch `$HOME/.cache/jpfoodmap-android`，产物 `apk/jpfoodmap.apk`；原子拷回 + BUILD-INFO 原样保留。 |
| `tools/env.sh`、`tools/android-env.sh` | **改写复用** | 前缀改 `JPFM_`；`~/tools/android-env.sh` 直接 source。 |
| `tools/emu.sh` | **改写复用** | 保留 start/stop/status/wait/shot/rotate/install；删 pin/widget；加 `launch`、`diag`、`fold`、`net`、只读并发（§10）。AVD 口径与「AN AVD IS A DISPLAY」注释原样保留。 |
| `tools/apk-contents.py` | **原样复用** | 大小审查。 |
| `tools/gen-launcher-icon.py` | **改写** | 源是 PNG（网站 maskable 图标），不是 SVG：用 Pillow 出各密度 mipmap。 |
| `build.gradle.kts`（根）钉子 AGP 8.11.1 / Kotlin 2.2.21 / Gradle 8.14.5 / JDK 17 | **原样复用** | 去掉 compose 与 google-services 插件。 |
| `app/build.gradle.kts` | **改写** | 去 glance/datastore/work/firebase；留 core-ktx 1.18.0 / activity-ktx 1.13.0 / webkit 1.17.0 / browser 1.10.0 / splashscreen 1.2.0；加 credentials 1.5.0 ×2 + googleid 1.1.1 + coroutines 1.9.0；release 用 debug 签名 + R8 `-dontobfuscate`。 |
| `gradle.properties`、`gradle/wrapper/*`、`gradlew` | **原样复用** | wrapper 带 sha256。 |
| `AndroidManifest.xml` | **改写** | 见 §5.2 全文；权限从 2 项变 4 项（加定位×2）。 |
| `app/MainActivity.kt`（1164 行） | **大幅裁剪复用** | 保留：状态机 NONE/LOADING/READY/ERROR、pendingUrl 冷/温/热、splash 条件、insets 监听、back 回调、renderer 崩溃重建、watchdog、错误面板、`onConfigurationChanged` 处理、诊断入口。删除：FlowStore/DataStore 观察、cookie seeding、Foreground/WidgetSync/Push/Files/LinkSheet、flowTheme、themeColor。新增：定位权限桥、`?r=` 热路径、`JpfmDiag` 日志。 |
| `app/DashboardWebView.kt` | **改写复用** | 同一套 WebSettings；`setGeolocationEnabled(true)`；`shouldInterceptRequest` 拦 gsi/client；`onGeolocationPermissionsShowPrompt`。 |
| `app/Bridge.kt` | **改写复用** | 传输层（`addWebMessageListener` + `addDocumentStartJavaScript` + tryRules）原样；消息集换成 §5.4；加 replyProxy 保存用于异步回 credential。 |
| `app/Links.kt`、`app/Routes.kt` | **近原样复用** | 主机集合改 `{jpfoodmap.com}`；去 `APP_DOCUMENT`（我们没有 `/api/` 文档）；`LinkPolicy` 默认改 `CUSTOM_TAB`。JVM 单测 `LinksTest`/`RoutesTest` 跟着搬。 |
| `app/PopupCatcher.kt` | **原样复用** | `target=_blank` 抓 URL。 |
| `app/Insets.kt` | **原样复用** | IME 模式判定 + 单测。 |
| `app/ShellPrefs.kt` | **改写复用** | 三个偏好：linkPolicy / textZoom / notifyEnabled；存 SharedPreferences（不用 DataStore）。 |
| `app/AppSettingsActivity.kt` + `activity_app_settings.xml` | **改写复用** | 去 Server&token、flowTheme；加「发送测试通知」「在浏览器中打开」「诊断」「关于」。 |
| `app/Diagnostics.kt` | **改写复用** | JS_METRICS 里的 `data-desk/data-theme` 换成 `bootId/lang`；Startup/exits 原样。 |
| `app/Notifications.kt` | **大幅裁剪** | 一个渠道、一个测试通知、权限判断；去掉所有 Flow 语义。 |
| `app/Files.kt`、`LinkSheet.kt`、`Events*.kt`、`Push.kt`、`RunNotice.kt`、`BatchNotice.kt`、`WidgetSync.kt`、`Foreground.kt`、所有 widget/*、glass 资源、字体 | **不要** | 与本项目无关。 |
| `res/values/themes.xml` | **改写** | 强制浅色；splash 用 PNG 图标；去 Sheet/Trampoline。 |
| `res/xml/data_extraction_rules.xml` | **原样复用** | 全排除。 |
| `res/xml/network_security_config.xml` | **改写** | `cleartextTrafficPermitted=false`（dashboard 为 mock server 开了 true）。 |
| `docs/APP-SHELL-SPEC.md` §3/§5/§6 | **规范复用** | 桥、insets、冷/温/热路由，本文件 §5 逐条对应。 |
| `docs/MERGE-NOTES.md` 教训 | **吸收** | ① 图标/名称第一轮真机就会被打回 → 首版就用网站图标+正名；② `versionCode` 必须严格递增；③ 真机 App Links 需先卸载同域 PWA；④ One UI「外屏续用」开关；⑤ 秘密不进 argv。 |
| `tools/VERIFY.md` §1-3、§Which AVD | **规范复用** | gate_build / gate_static 的命令直接搬。 |
| `deploy/SIGNING-KEY.md` | **引用** | 同一 keystore、同一风险。 |

---

## 3. 环境验证（2026-09-06，本机）

| 项 | 结果 |
|---|---|
| `source ~/tools/android-env.sh` | OK；`JAVA_HOME=~/tools/jdk-17`，Temurin 17.0.20.1 |
| `gradle` / `adb` / `sdkmanager` / `avdmanager` / `apksigner` / `aapt2` | 全在 PATH；`emulator` 不在 PATH（emu.sh 用 `$ANDROID_HOME/emulator/emulator`，存在） |
| `sdkmanager --list_installed` | build-tools 34/35、emulator 37.1.11、platform-tools 37.0.1、platforms 35+36、system-images android-35 & android-37.0 google_apis x86_64 |
| AVD | `foldcover`、`fold8inner`、`fold8inner60`、`flow` 都在 `~/.android/avd` |
| KVM | `/dev/kvm` 可写（group kvm） |
| `foldcover` 实跑 | `emu.sh start` **20 s 开机**（含等待 29 s wall），`wm size` 1248×1972 @420，框架 `sw475dp w475dp h751dp`，Android 17（SDK 37），WebView **145.0.7632.218**，装有 `com.android.chrome`、`com.google.android.gms`、`com.android.vending`；`emu.sh shot` 47 s 内出图（`audit_outputs/android-2026-09-06/plan/foldcover-boot.png`，日志 `emu-start.log`）。已 `emu.sh stop`。 |
| 签名 | `~/.android/debug.keystore` 0600，SHA-1 `9F:60:4D:C5:02:C4:10:94:ED:CF:D6:0F:53:E1:8B:50:0B:03:F0:10`，SHA-256 `78:9F:E3:5F:02:40:43:2A:CF:C7:E1:71:50:1B:94:1C:29:B9:91:55:D3:58:CF:33:9C:78:AE:C2:10:16:85:D1`（= dashboard assetlinks 指纹） |
| 网站工具 | `uv`、`node v24.20.0` 可用；门禁命令见 `tests/README.md` |
| 资源 | 32 核、19 GB 内存、ext4 `$HOME` 895 GB 空闲 |

限制：模拟器无 Google 账号 → 原生登录只能验到「Credential Manager 被调用 + 优雅失败」；真实登录在真机。

---

## 4. 决策记录

| # | 决策 | 理由 |
|---|---|---|
| D1 | `applicationId = com.fredhli.jpfoodmap`，label `Japan Foodmap`（zh-CN `日本美食地图`），`versionName 2.0.0`，`versionCode 20000`（major×10000+minor×100+patch） | 与 dashboard 同命名空间风格；versionCode 可从版本号推出，永远递增。**待主人确认包名/名称**。 |
| D2 | 签名 = `~/.android/debug.keystore`（与 dashboard 同证书） | 侧载、覆盖安装、assetlinks 指纹一致；主人已知风险。 |
| D3 | 工具链钉子照抄 dashboard：JDK 17 / Gradle 8.14.5 / AGP 8.11.1 / Kotlin 2.2.21 / compileSdk=targetSdk 36 / minSdk 31 | 本机已验证能编；targetSdk 36 拿到预测式返回与 edge-to-edge。 |
| D4 | **登录 = 原生 Credential Manager 拿 id_token → 交给页面 → 页面自己 `POST /api/session`**（方案 A），不由壳注入 cookie | 复用页面已验证的 `exchangeForSession`/`saveSessionProfile`/reload 链路；Worker 零改动；cookie 天然落在 WebView jar；壳不碰任何 token。 |
| D5 | `setAcceptThirdPartyCookies(webView, true)` | api.jpfoodmap.com 对 jpfoodmap.com 是 same-site，理论上第三方开关无关，但 Chromium WebView 的判定不值得赌；cookie 本身 HttpOnly+Secure+host-only，无额外风险。 |
| D6 | APP 内拦截 `accounts.google.com/gsi/client` | GIS 在 WebView 里必失败（disallowed_useragent），拦掉省 100 KB/次；页面在 APP 内用原生按钮。 |
| D7 | 外链默认 **Chrome Custom Tab**（可切 Chrome / 系统默认） | 看 Tabelog 是「绕一下再回来」，Custom Tab 的返回箭头最顺；Google Maps 链接先试 `REQUIRE_NON_BROWSER` 直达 Maps APP。**待主人确认**（dashboard 选的是 Chrome）。 |
| D8 | App Links 只绑 `jpfoodmap.com`（不绑 www） | www 不解析；少一个验证失败点。 |
| D9 | 强制浅色（WebView 窗口与设置页都 `Theme.DeviceDefault.Light`） | 网站 `color-scheme: only light`；少一套 night 资源；状态栏图标恒深色。 |
| D10 | 通知：一个渠道 + 开关（默认关）+ 测试通知；不接 FCM | 无服务端推送源；dashboard 的 FCM 需要 Firebase 项目 + 服务端，不是零成本。 |
| D11 | 折叠连贯靠 `configChanges` + `singleTask` + `resizeableActivity`（同 dashboard，真机验证过），模拟器用 `wm size 1248x1972 ↔ reset` 做折叠代理 | 单个 AVD 无法切换显示屏；尺寸覆盖触发的正是 fold 产生的 config change（两屏同 420dpi）。 |
| D12 | 页面钩子命名 `window.__jpfm*`；UA 后缀 ` JpFoodMapApp/2.0.0`；`Native.app === "jpfoodmap"` | 与 dashboard 的 `DashboardApp/` 区分；页面判 `isApp = !!window.Native || /\bJpFoodMapApp\//.test(UA)`。 |
| D13 | app-bridge 区块里的中文 UI 文案**只能用 `data/i18n/*.json` 已有的键**（如「登录」「分享」「登录处理失败」「登录中」「重试」），新文案由壳用 Android 资源提供、经 façade 的 `Native.labels` 注入；区块注释一律英文 | `scripts/verify_build.py` 会数 index.html 里没有翻译的 CJK 串，`data/i18n` 不在可写范围。 |
| D14 | 壳的三个偏好用 `SharedPreferences`（不引 DataStore） | 少一个依赖；无并发写。 |
| D15 | 网站 smoke 视口（416/616/816）与真机实测（475/932/704）不一致：APP 按实测验收；是否改网站 smoke 视口留给主会话 | `tests/` 不在可写范围。 |
| D16 | `docs/_headers` 不改：Cloudflare Pages 按扩展名给 `.json` 正确 Content-Type | `_headers` 不在可写范围。 |
| D17 | 错误面板文案/设置页文案：`values/` 简体中文，`values-en/` 英文 | 主人读中文；壳字符串不经网站 i18n。 |

---

## 5. 架构与逐字契约（builders 之间的接口）

### 5.1 目录

```
android/
├── README.md                     侧载、真机清单、构建、目录说明（T8）
├── CHANGELOG-ANDROID.md          APK 版本记录（T8；仓库根 CHANGELOG.md 只追加一段指过来）
├── build.sh  tools/env.sh  tools/emu.sh  tools/apk-contents.py  tools/gen-launcher-icon.py
├── tools/verify-geometry.sh  tools/verify-flows.sh  tools/diag.sh  (T7)
├── tools/VERIFY.md               检查员手册（T7 + T8）
├── apk/BUILD-INFO.txt            （*.apk 在 .gitignore）
├── docs/PLAN.md  docs/STANDARDS.md  docs/STATUS.md（T8）
├── settings.gradle.kts  build.gradle.kts  gradle.properties  gradle/  gradlew  .gitignore
└── app/
    ├── build.gradle.kts  proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── kotlin/com/fredhli/jpfoodmap/
        │   ├── MainActivity.kt        (T2)   生命周期、状态机、insets、back、错误面板、定位权限、诊断入口
        │   ├── SiteWebView.kt         (T2)   WebView 工厂 + WebViewClient/ChromeClient
        │   ├── PopupCatcher.kt        (T2)
        │   ├── Insets.kt              (T2)
        │   ├── Bridge.kt              (T3)   façade JS + 消息解析/分发
        │   ├── GoogleSignIn.kt        (T3)   Credential Manager 封装
        │   ├── Routes.kt              (T4)   纯字符串：origin/host/?r= 解析、jsStringLiteral
        │   ├── DeepLinks.kt           (T4)   Intent → Target；热路径 JS
        │   ├── Links.kt               (T5)   classify + 外链阶梯
        │   ├── ShellPrefs.kt          (T5)
        │   ├── AppSettingsActivity.kt (T5)
        │   ├── Diagnostics.kt         (T5)
        │   └── Notifications.kt       (T6)
        ├── res/ …（见各任务）
        └── src/test/kotlin/…          各自的 JVM 单测
```

### 5.2 `AndroidManifest.xml`（T1 一次写全，之后**只有 T1/T8 可改**）

```xml
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
    <uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
    <queries>
        <package android:name="com.android.chrome" />
        <intent><action android:name="android.support.customtabs.action.CustomTabsService" /></intent>
        <intent>
            <action android:name="android.intent.action.VIEW" />
            <category android:name="android.intent.category.BROWSABLE" />
            <data android:scheme="https" />
        </intent>
        <intent><action android:name="android.intent.action.SEND" /><data android:mimeType="text/plain" /></intent>
    </queries>
    <application
        android:label="@string/app_name"
        android:icon="@mipmap/ic_launcher"
        android:roundIcon="@mipmap/ic_launcher_round"
        android:allowBackup="false"
        android:dataExtractionRules="@xml/data_extraction_rules"
        android:networkSecurityConfig="@xml/network_security_config"
        android:supportsRtl="false"
        android:enableOnBackInvokedCallback="true">
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:launchMode="singleTask"
            android:theme="@style/Theme.Jpfm.Splash"
            android:configChanges="orientation|screenSize|smallestScreenSize|screenLayout|density|uiMode|keyboard|keyboardHidden|fontScale|locale|layoutDirection"
            android:resizeableActivity="true"
            android:windowSoftInputMode="adjustResize">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
            <intent-filter android:autoVerify="true">
                <action android:name="android.intent.action.VIEW" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data android:scheme="https" />
                <data android:host="jpfoodmap.com" />
            </intent-filter>
            <meta-data android:name="android.app.shortcuts" android:resource="@xml/shortcuts" />
        </activity>
        <activity
            android:name=".AppSettingsActivity"
            android:exported="true"
            android:label="@string/settings_title"
            android:theme="@style/Theme.Jpfm.Settings">
            <intent-filter>
                <action android:name="android.intent.action.APPLICATION_PREFERENCES" />
                <category android:name="android.intent.category.DEFAULT" />
            </intent-filter>
        </activity>
    </application>
</manifest>
```

`res/xml/shortcuts.xml`：一项 `settings` → `AppSettingsActivity`（`targetPackage` 写死 `com.fredhli.jpfoodmap`；debug 变体在 `src/debug/res/xml/shortcuts.xml` 写 `.debug`）。

### 5.3 Gradle（T1）

根 `build.gradle.kts`：`com.android.application 8.11.1`、`org.jetbrains.kotlin.android 2.2.21`（都 `apply false`）。
`app/build.gradle.kts` 要点：`namespace/applicationId = com.fredhli.jpfoodmap`，`compileSdk = targetSdk = 36`，`minSdk = 31`，`versionCode = 20000`，`versionName = "2.0.0"`，`buildFeatures.buildConfig = true`，debug `applicationIdSuffix = ".debug"`，release `isMinifyEnabled = true; isShrinkResources = true; signingConfig = signingConfigs.getByName("debug")`，JVM 17。依赖：

```kotlin
implementation("androidx.core:core-ktx:1.18.0")
implementation("androidx.activity:activity-ktx:1.13.0")
implementation("androidx.webkit:webkit:1.17.0")
implementation("androidx.browser:browser:1.10.0")
implementation("androidx.core:core-splashscreen:1.2.0")
implementation("androidx.credentials:credentials:1.5.0")
implementation("androidx.credentials:credentials-play-services-auth:1.5.0")
implementation("com.google.android.libraries.identity.googleid:googleid:1.1.1")
implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
testImplementation("junit:junit:4.13.2")
testImplementation("org.json:json:20240303")
```

回退梯（同 dashboard §8）：① AGP 8.10.1 + Kotlin 2.1.0；② credentials 1.3.0 + googleid 1.1.1；③ compileSdk 35 + core-ktx 1.16.0 / activity 1.10.1 / webkit 1.14.0 / browser 1.8.0 / splash 1.0.1。走到哪一级记进 `docs/STATUS.md`。

`proguard-rules.pro`：`-dontobfuscate`、`-keep class com.fredhli.jpfoodmap.** { *; }`。

### 5.4 桥（T3 拥有；T2/T4/T5 按此调用）

- 传输：`WebViewCompat.addWebMessageListener(webView, "NativeBridge", setOf("https://jpfoodmap.com"), listener)`；façade 经 `addDocumentStartJavaScript` 注入同一源集，无 `DOCUMENT_START_SCRIPT` 时在 `onPageStarted` 评估。**无 `addJavascriptInterface`。**
- UA：`WebSettings.getDefaultUserAgent(ctx) + " JpFoodMapApp/" + BuildConfig.VERSION_NAME`。
- façade（`Bridge.FACADE_JS`，`__SIGN_IN_LABEL__`/`__SETTINGS_LABEL__`/`__BROWSER_LABEL__` 由壳用当前 locale 的 `strings.xml` 值替换后注入）：

```js
(function () {
  if (window.Native || !window.NativeBridge) return;
  var B = window.NativeBridge, waiting = [];
  function send(o) { try { B.postMessage(JSON.stringify(o)); } catch (e) {} }
  B.onmessage = function (ev) {
    var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (!m) return;
    if (m.t === "metrics") { var r = waiting.shift(); if (r) r(m); return; }
    if (m.t === "credential" && typeof window.__jpfmNativeCredential === "function") {
      try { window.__jpfmNativeCredential(String(m.req || ""), m.idToken || null, m.error || null); } catch (e) {}
    }
  };
  window.Native = {
    version: "2.0.0",
    app: "jpfoodmap",
    labels: { signIn: "__SIGN_IN_LABEL__", settings: "__SETTINGS_LABEL__", browser: "__BROWSER_LABEL__" },
    signIn: function (req, silent) { send({ t: "signin", req: String(req || ""), silent: !!silent }); },
    signOut: function () { send({ t: "signout" }); },
    share: function (title, url) { send({ t: "share", title: String(title || ""), url: String(url || "") }); },
    openExternal: function (url) { send({ t: "open", url: String(url || "") }); },
    openSettings: function () { send({ t: "settings" }); },
    haptic: function () { send({ t: "haptic" }); },
    metrics: function () { return new Promise(function (res) { waiting.push(res); send({ t: "metrics" }); }); }
  };
})();
```

- 页面 → 壳消息与动作：

| `t` | 字段 | 壳动作 |
|---|---|---|
| `signin` | `req`, `silent` | `GoogleSignIn.request(activity, silent)` → 成功：`reply.postMessage({t:"credential", req, idToken})`；失败：`{t:"credential", req, error: "no_credential" \| "cancelled" \| "not_configured" \| "error"}`，且非 silent 时 Toast `R.string.signin_failed_hint` 并在 `error=="not_configured"` 时附「在浏览器中打开」动作（Snackbar/对话框） |
| `signout` | — | `CredentialManager.clearCredentialState()` |
| `share` | `title`, `url` | 仅 `https://jpfoodmap.com/*` URL：`ACTION_SEND text/plain` chooser（EXTRA_TEXT = url，EXTRA_SUBJECT = title）；其它丢弃 |
| `open` | `url` | http(s)：`Links.openExternal(activity, url, prefs.linkPolicy, allowSelf = !Routes.isAppOrigin(url))`；其它：`Links.leave` |
| `settings` | — | `startActivity(Intent(this, AppSettingsActivity::class.java))`（无 NEW_TASK） |
| `haptic` | — | `webView.performHapticFeedback(CONFIRM)` |
| `metrics` | — | `reply.postMessage(metricsJson)`，键集：`t,webview,webviewPackage,package,version,insets{top,bottom,left,right},ime,imeMode,fontScale,textZoom,density,widthDp,heightDp,pageLoads,activityCreates` |

- 壳 → 页面（`evaluateJavascript`，热路径深链）：`(function(id){try{return !!(window.__jpfmOpenShare&&window.__jpfmOpenShare(id));}catch(e){return false;}})(<jsStringLiteral(id)>)` → 结果 `"false"` 则 `loadUrl(url)`。

### 5.5 页面钩子（map.py app-bridge 区块提供，T3）

| 名字 | 语义 |
|---|---|
| `window.__jpfmBootId` | 页面每次载入随机字符串；诊断用来证明「没重载」 |
| `window.__jpfmNativeCredential(req, idToken, error)` | 壳回 credential；页面按 `req` 找回调 |
| `window.__jpfmOpenShare(id)` | 打开 `?r=<id>` 对应餐厅卡；返回 boolean |

### 5.6 `MainActivity` 对外（T2 拥有；T4/T5 调用）

```kotlin
companion object {
    const val EXTRA_DIAGNOSTICS = "com.fredhli.jpfoodmap.EXTRA_DIAGNOSTICS"       // Boolean: READY 后弹诊断对话框
    const val EXTRA_DIAGNOSTICS_LOG = "diagnostics_log"                          // Boolean: READY 后单行打 logcat tag JpfmDiag
    fun diagnosticsIntent(ctx: Context): Intent
}
internal val webView: WebView?; internal val prefs: ShellPrefs; internal val appOrigins: Set<String>
internal fun metricsJson(): JSONObject; internal fun diagnosticsNativeJson(): JSONObject
internal fun runDiagnostics(); internal fun hapticTick()
internal fun requestLocation(onResult: (Boolean) -> Unit)   // ActivityResultLauncher；已授权直接 true
```

`DeepLinks`（T4）：`fun targetOf(intent: Intent?, stale: Boolean, appOrigins: Set<String>): Target`，`sealed class Target { object None; data class Load(val url: String); data class Share(val id: String, val url: String); data class Leave(val url: String, val nav: Links.Nav) }`；`fun hotShareJs(id: String): String`。MainActivity 在 `handleIntent` 里调用。

### 5.7 `tools/emu.sh` 接口（T1 建立，T7 扩展）

```
JPFM_AVD=foldcover|fold8inner|fold8inner60|flow   JPFM_EMU_PORT=5554   JPFM_EMU_READONLY=1
emu.sh start|stop|status|wait|shot <png>|rotate landscape|portrait|install [apk]
emu.sh launch [url]          # am start -W；有 url 则 ACTION_VIEW
emu.sh diag <json-out>       # am start --ez diagnostics_log true → 抓 logcat JpfmDiag 一行
emu.sh fold cover|inner      # 仅 fold8inner：wm size 1248x1972 / wm size reset
emu.sh net on|off            # svc wifi/data
```

### 5.8 诊断 JSON（页面半，`Diagnostics.JS_METRICS`，T5）

`innerWidth, innerHeight, outerWidth, outerHeight, dpr, screen{w,h,aw,ah}, vv{w,h,ot,s}, env{t,b,l,r}, bootId, lang(localStorage tabelog.lang 或 html lang), url(去 query), ua, native(!!window.Native), sw(!!navigator.serviceWorker.controller), online(navigator.onLine)`。壳半 = 5.4 metrics + `pageState, safeVar(false), webViewFeatures, startup{...}, exits[...], config{orientation}`。

---

## 6. 网站 app-bridge 区块规范（map.py，T3）

位置：`FILTER_JS_TEMPLATE` 内、`initMap` 作用域末尾（M-032 `?r=` 消费之后），一个以 `// ===== APP BRIDGE (Android shell) =====` 起、`// ===== /APP BRIDGE =====` 止的连续块。**注释全部英文**（D13）。

```js
// ===== APP BRIDGE (Android shell) =====
(function () {
  var N = window.Native;
  var isApp = !!(N && N.app === 'jpfoodmap') || /\bJpFoodMapApp\//.test(navigator.userAgent || '');
  window.__jpfmBootId = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  window.__jpfmOpenShare = function (id) {            // hot deep link
    var row = shareRowById(String(id || ''));
    if (!row) return false;
    try { if (typeof row.lat === 'number') map.setView([row.lat, row.lon], Math.max(map.getZoom(), 16), {animate: false}); } catch (_) {}
    openSheet(row);
    return true;
  };
  if (!isApp) return;                                  // browser: nothing below runs
  // 1. install UI: the shell IS the install
  obMaybeSnack = function () {};
  obRefreshInstallUi = function () { if (obInstallRow) obInstallRow.hidden = true; obHideSnack(); if (obInstall.isOpen()) obInstall.close(); };
  obOnInstallState = obRefreshInstallUi; obRefreshInstallUi();
  // 2. sign-in: native button -> Native.signIn -> __jpfmNativeCredential -> onGoogleCredential
  var pending = {};
  window.__jpfmNativeCredential = function (req, idToken, err) { var cb = pending[req]; delete pending[req]; if (cb) cb(idToken, err); };
  function nativeSignIn(silent, cb) { var req = 'r' + Date.now().toString(36); pending[req] = cb; N.signIn(req, silent); }
  if (N && typeof N.signIn === 'function') {
    renderSignInButton = function () {
      if (signinBtnContainer.querySelector('.jpfm-native-signin')) return;
      var b = document.createElement('button'); b.type = 'button'; b.className = 'ssm-row jpfm-native-signin';
      b.textContent = (N.labels && N.labels.signIn) || ('Google ' + localizeText('登录'));
      b.addEventListener('click', function () {
        cfgMsg.style.color = ''; cfgMsg.textContent = localizeText('登录中') + '…';
        nativeSignIn(false, function (tok, err) {
          if (tok) onGoogleCredential({credential: tok});
          else { cfgMsg.style.color = '#dc2626'; cfgMsg.textContent = localizeText(err === 'cancelled' ? '登录被取消' : '登录处理失败'); }
        });
      });
      signinBtnContainer.textContent = ''; signinBtnContainer.appendChild(b);
    };
    silentReAuth = function (cb) {                     // 90-day renewal without GIS
      cb = cb || function () {};
      nativeSignIn(true, function (tok) {
        if (!tok) { cb(false); return; }
        exchangeForSession(tok, function (ok, p) { if (ok && p) { saveSessionProfile(p); cb(true); } else cb(false); });
      });
    };
    refreshAuthUI();
  }
  // 3. share: system sheet instead of clipboard
  if (N && typeof N.share === 'function') {
    var origShare = shareRestaurant;
    shareRestaurant = function (d, ev) { if (ev && ev.preventDefault) ev.preventDefault(); var u = shareUrlFor(d); if (!u) return; N.share(d.name || '', u); };
    window.__shareRestaurant = shareRestaurant;
  }
  // 4. app settings row in the avatar menu
  if (N && typeof N.openSettings === 'function') { /* append an .ssm-row after #ssm-install with text N.labels.settings -> N.openSettings() */ }
  // 5. sign-out also clears the native credential state
  if (N && typeof N.signOut === 'function') { /* wrap signOut(): call N.signOut() first, then the original */ }
})();
// ===== /APP BRIDGE =====
```

约束：① `renderSignInButton` / `silentReAuth` / `shareRestaurant` / `obRefreshInstallUi` / `obMaybeSnack` 是 `initMap` 内的函数声明，重新赋值即可覆盖（页面已有 `toggleFav = function…` 先例）；T3 需核对 `window.__shareRestaurant` 的所有赋值点（9316/9649/18074 行附近）保证卡片按钮拿到包装后的版本；② 不新增 localStorage 键；③ 不改 HEAD_BRANDING（GIS `<script>` 仍在，由壳拦截）；④ 改后必须 `flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py` 重建并全绿：`verify_build.py`、`tests/smoke_playwright.py`、`tests/compat/run.py`、`tests/sync/run_all.py`、`node tests/worker/run.mjs`；⑤ 浏览器里（无 Native、UA 无标记）除 `__jpfmBootId`/`__jpfmOpenShare` 两个无副作用的全局外，行为零变化。

---

## 7. 任务（≤8，全部 Opus；`after` 为真实依赖）

并行运行约定见 §10。每个任务：自测通过后把证据（截图/日志/命令输出）放到 `audit_outputs/android-2026-09-06/<key>/`，并在 `android/docs/STATUS.md` 追加一段（T8 汇总）。做不完标 `partial`，不留半截：至少让 `android/build.sh assembleDebug` 绿。

### T1 `skeleton` — 工程骨架、构建流水线、图标、版本（after: 无）

- 读：本文件 §5.1–5.3、§5.7、STANDARDS §1.4–1.6、§14；dashboard `build.sh`、`tools/env.sh`、`tools/emu.sh`、根/应用 `build.gradle.kts`、`gradle.properties`、`gradle/wrapper/*`、`res/values/themes.xml`、`mipmap-anydpi-v26/*`、`res/xml/data_extraction_rules.xml`。
- 写：`android/{settings.gradle.kts,build.gradle.kts,gradle.properties,gradlew,gradlew.bat,gradle/,.gitignore,build.sh}`；`android/tools/{env.sh,android-env.sh(参考副本),emu.sh(裁剪版+launch/diag/fold/net 占位),apk-contents.py,gen-launcher-icon.py}`；`app/build.gradle.kts`、`app/proguard-rules.pro`、`app/src/main/AndroidManifest.xml`（§5.2 全文）、`src/debug/res/xml/shortcuts.xml`；`res/values/{themes.xml,colors.xml,strings.xml}` + `res/values-en/strings.xml` + `res/values-zh-rCN/strings.xml`（`app_name`、`settings_title` 等骨架字符串）、`res/xml/{shortcuts.xml,data_extraction_rules.xml,network_security_config.xml}`、`res/mipmap-*/ic_launcher*.png` + `mipmap-anydpi-v26/*.xml` + `res/drawable/ic_splash.xml`；**所有 Kotlin 文件的可编译占位**：`MainActivity.kt`（最小：edge-to-edge + 一个 WebView `loadUrl("https://jpfoodmap.com/")`）、`AppSettingsActivity.kt`（空 Activity）、其余文件（`SiteWebView, PopupCatcher, Insets, Bridge, GoogleSignIn, Routes, DeepLinks, Links, ShellPrefs, Diagnostics, Notifications`）各一个含 §5.4/5.6 签名的 stub（`TODO()` 或空实现），保证 T2–T6 从同一份签名起步。
- 图标：`gen-launcher-icon.py` 用 Pillow 从 `docs/icons/icon-japan-emoji-v2-maskable-512.png` 生成 mdpi..xxxhdpi 前景 PNG（108dp 画布），背景色 `#fdf6e3`；splash 用同一前景。
- 验收：`android/build.sh assembleDebug` 与 `android/build.sh`（release）都 `BUILD SUCCESSFUL`；`apk/jpfoodmap.apk` + `BUILD-INFO.txt` 生成；`apksigner verify --print-certs` SHA-256 = `78:9F:…:85:D1`；`aapt2 dump badging` 显示 `package: name='com.fredhli.jpfoodmap' versionCode='20000' versionName='2.0.0'`、`sdkVersion:'31'`、`targetSdkVersion:'36'`、两个 label；权限恰四项；`emu.sh start && emu.sh install && emu.sh launch` 后截图能看到地图；`find android -name build -o -name .gradle` 为空；`git check-ignore android/apk/jpfoodmap.apk` 命中。
- 自测命令：`cd android && ./build.sh assembleDebug && ./build.sh && source tools/env.sh && apksigner verify --print-certs apk/jpfoodmap.apk && aapt2 dump badging apk/jpfoodmap.apk | head -20 && aapt2 dump permissions apk/jpfoodmap.apk`。
- 边界：不实现任何行为；stub 的签名一旦写下即冻结（后续任务需要改签名 → 在 STATUS.md 记录并通知 T8 集成）。

### T2 `shell` — WebView 壳与生命周期（after: T1）

- 读：STANDARDS §1–§4、§8.2–8.3、§9、§10；dashboard `MainActivity.kt`、`DashboardWebView.kt`、`PopupCatcher.kt`、`Insets.kt`、`APP-SHELL-SPEC.md` §5/§6；map.py 的 M-015 浮层栈（7885–7950 行）、`pageshow`/`invalidateSize` 用法。
- 写：`MainActivity.kt`、`SiteWebView.kt`、`PopupCatcher.kt`、`Insets.kt`、`res/layout/activity_main.xml`、`res/values*/strings_shell.xml`；单测 `InsetsTest`。
- 实现：状态机与 pendingUrl 冷/温/热（§5.6 契约）；`installSplashScreen` + 3 s 上限；`enableEdgeToEdge`，insets 透传（WEBVIEW/NATIVE 两种 IME 模式）；`OnBackPressedCallback` 随 `canGoBack()` 启停；`onConfigurationChanged` 只重设 textZoom（不重建）；`onPause/onResume` 的 WebView 与 timers + `CookieManager.flush()`；`onRenderProcessGone` 重建；10 s watchdog；错误面板；`onGeolocationPermissionsShowPrompt` 仅本域 + `requestLocation` 运行时申请；`shouldInterceptRequest` 拦 `accounts.google.com/gsi/client`；`setAcceptThirdPartyCookies(true)`；`pageLoads` 计数；`EXTRA_DIAGNOSTICS_LOG` → logcat `JpfmDiag`（调用 T5 的 `Diagnostics`，未就绪时打壳半即可）。调用 T3 `Bridge.install()`、T4 `DeepLinks.targetOf()`、T5 `Links`/`Diagnostics` 的 stub 签名。
- 验收（模拟器，foldcover + fold8inner）：冷启动 ≤3 s 出页面、无白闪；`emu.sh fold cover/inner` 前后 `activityCreates=1`、`pageLoads` 不变、`bootId` 不变、`innerWidth` 475↔932；深链开卡后 BACK 关卡、再 BACK 退出；断网（联网跑过一次后）冷启动出地图；定位 FAB 触发系统权限弹窗且启动时无弹窗；`am kill` 后重开恢复。
- 自测命令：`JPFM_AVD=fold8inner JPFM_EMU_PORT=5556 JPFM_EMU_READONLY=1 tools/emu.sh start && tools/emu.sh install && tools/emu.sh launch && tools/emu.sh diag /tmp/a.json && tools/emu.sh fold cover && tools/emu.sh diag /tmp/b.json && tools/emu.sh fold inner && tools/emu.sh diag /tmp/c.json`（比较三份 JSON）。
- 边界：不写 façade JS、不写 Links 阶梯、不写设置页；诊断 JS 由 T5 提供，T2 只接线。

### T3 `signin-bridge` — 原生登录桥 + 页面 app-bridge 区块 + 网站门禁（after: T1）

- 读：STANDARDS §7、§8.1、§0.3、§0.2；本文件 §5.4、§5.5、§6、D4–D6、D13；dashboard `Bridge.kt` + `BridgeTest`；map.py 7450–7800（auth 层）、16700–16960（GIS 挂载、`onGoogleCredential`、`silentReAuth`、`renderSignInButton`、`refreshAuthUI`）、17735–17775（菜单行）、17895–17920（`obRefreshInstallUi`）、18000–18110（`?r=` 与 `shareRestaurant`）、`window.__shareRestaurant` 全部赋值点；`worker/src/index.js`（只读，确认 `/api/session` 契约）；`tests/README.md`。
- 写：`Bridge.kt`、`GoogleSignIn.kt`、`res/values*/strings_signin.xml`（`signin_label`、`settings_label`、`open_in_browser`、`signin_failed_hint`、`signin_not_configured`）；单测 `BridgeTest`（parse 全矩阵、façade 替换）；**map.py 的 app-bridge 区块**（§6）；重建 `docs/index.html`、`docs/sw.js`、`docs/data/*`（构建产物一并算入本任务）。
- `GoogleSignIn.request(activity, silent)`: `CredentialManager.create(ctx).getCredential(activity, GetCredentialRequest(listOf(GetGoogleIdOption.Builder().setServerClientId(WEB_CLIENT_ID).setFilterByAuthorizedAccounts(silent).setAutoSelectEnabled(silent).build())))` → `GoogleIdTokenCredential.createFrom(cred.data).idToken`；`WEB_CLIENT_ID` = `536198170238-me7dpu2og75tseuekl3pu8rjjgo2ig2p.apps.googleusercontent.com`（与 map.py `GOOGLE_CLIENT_ID` 一致，是 Worker 校验的 `aud`）；异常映射：`GetCredentialCancellationException→cancelled`、`NoCredentialException→no_credential`、消息含 "Developer console"/code 10/16 → `not_configured`、其它 `error`。`signOut` → `clearCredentialState(ClearCredentialStateRequest())`。
- 验收：模拟器（无账号）点「使用 Google 登录」→ 状态行「登录处理失败」+ Toast/对话框提供「在浏览器中打开」→ Chrome 开站；不崩溃；`adb logcat` 无 `id_token`/`credential` 字样、无 gsi 请求；浏览器（Playwright smoke）五视口绿；`verify_build.py`「no new untranslated UI strings」绿；compat/sync/worker 绿；`git diff --stat` 里除 map.py 与 docs 构建产物外无其它文件。
- 自测命令：`flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py && uv run python scripts/verify_build.py && uv run python tests/smoke_playwright.py && uv run python tests/compat/run.py && node tests/worker/run.mjs && uv run python tests/sync/run_all.py`；壳侧 `cd android && ./build.sh assembleDebug && ./gradlew testDebugUnitTest`（在 scratch 里跑）。
- 边界：不改 Worker、不改 `data/i18n`、不改 HEAD_BRANDING、不新增 localStorage 键；壳侧只写 Bridge/GoogleSignIn；MainActivity 的接线由 T2（T3 提供的 `Bridge.install(webView, origins, labels)` 与 `Bridge.onPageStarted()` 签名已在 T1 stub）。

### T4 `applinks` — App Links + assetlinks + 深链冷/热路径（after: T1）

- 读：STANDARDS §6；本文件 §5.6 `DeepLinks`、D8；dashboard `Routes.kt` + `RoutesTest`、`MainActivity.handleIntent/applyPending/pushRoute`、`APP-SHELL-SPEC.md` §6/§7.1；map.py 17995–18105（`?r=` 语义、`shareIdOf` base36 规则）。
- 写：`Routes.kt`、`DeepLinks.kt`、单测 `RoutesTest`/`DeepLinksTest`；`docs/.well-known/assetlinks.json`（两条：`com.fredhli.jpfoodmap`、`com.fredhli.jpfoodmap.debug`，指纹 `78:9F:…:85:D1`）；`android/tools/applinks-check.sh`（`apksigner` 指纹 vs assetlinks；`pm get-app-links`；`pm set-app-links … 1 jpfoodmap.com` 强制批准用于模拟器）。
- 实现：`Target` 判定（`?r=` 有值 → `Share(id, url)`；本域其它 → `Load`；外域 → `Leave`；`FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY`/stale → `None`）；`hotShareJs(id)`；base36 id 校验 `^[0-9a-z]{1,6}$`。
- 验收：JVM 单测绿；模拟器：`pm set-app-links` 后 `am start -a android.intent.action.VIEW -d 'https://jpfoodmap.com/?r=<真实 id>'` 冷启动开对应卡；APP 在前台再发另一个 id → 卡切换、`pageLoads` 不变；`?lang=en` 深链 → 英文页；外域 URL 送到组件 → 交给浏览器、壳仍显示页面；`adb shell pm get-app-links com.fredhli.jpfoodmap` 显示 `jpfoodmap.com`；`applinks-check.sh` 指纹一致。真实 id 取法：`python3 -c "import json;d=json.load(open('docs/data/restaurants.json'));..."`（detail_url 末段十进制→base36）。
- 边界：不改 manifest（T1 已写 filter）；MainActivity 的调用点由 T2 按 §5.6 签名接；不碰 `docs/_headers`。

### T5 `links-settings-diag` — 外链阶梯、分享、字号、设置页、诊断（after: T1）

- 读：STANDARDS §2.4、§5、§8.1、§12、§13；dashboard `Links.kt`+`LinksTest`、`ShellPrefs.kt`+`ShellPrefsTest`、`AppSettingsActivity.kt`+`activity_app_settings.xml`+`strings_settings.xml`、`Diagnostics.kt`+`DiagnosticsTest`；map.py 9295–9312（Tabelog/Google Maps 链接形态）、`SEARCH_BOX_HTML` 里的 `ss-menu`。
- 写：`Links.kt`、`ShellPrefs.kt`、`AppSettingsActivity.kt`、`Diagnostics.kt`、`res/layout/activity_app_settings.xml`、`res/values*/strings_settings.xml`、`res/values*/strings_links.xml`；单测 `LinksTest`、`ShellPrefsTest`、`DiagnosticsTest`。
- 实现：`Links.classify/leave/openExternal/openIntentUri/openOtherScheme`（去 APP_DOCUMENT；`LinkPolicy` 默认 `CUSTOM_TAB`）；`ShellPrefs`（SharedPreferences `jpfm_shell`：`link_policy`、`text_zoom`、`notify_enabled`；`effectiveTextZoom`）；设置页：外链策略三选一、字号六档、通知开关 + 「发送测试通知」（调 T6 `Notifications` stub）、「在浏览器中打开」、「诊断…」、「关于」（`versionName` + WebView 版本）；`Diagnostics`：JS_METRICS（§5.8）、`show()` 对话框、`Startup`、`exitsJson`、`logLine()` 供 T2 打 logcat。
- 验收：单测绿；模拟器：卡片「Tabelog ↗」→ Custom Tab（同 task）；切 Chrome → Chrome；「Google Maps」→ 无 Maps 时按策略；分享 → chooser；`settings put system font_scale 1.3` → `textZoom=130`，设置页选 100 → 100；诊断对话框有页面半+壳半、`url` 无 query、可复制；`emu.sh diag` 得到合法 JSON。
- 边界：不碰 MainActivity（`metricsJson`/`requestLocation` 由 T2 实现）；通知本体属 T6，T5 只放按钮并调 stub。

### T6 `notify-power-hardening` — 通知、节电、静态加固（after: T1）

- 读：STANDARDS §10、§11、§13、§0.4；dashboard `Notifications.kt`（只取 ensureChannel/canPost/hasPermission/show 的骨架）、`AppSettingsActivity` 的权限申请段、`network_security_config.xml`、`data_extraction_rules.xml`、`VERIFY.md` §3；Android 13+ `POST_NOTIFICATIONS` 流程。
- 写：`Notifications.kt`（`CHANNEL_ID="jpfoodmap_general"`、`ensureChannel`、`hasPermission`、`canPost`、`postTest(ctx)`：静音、固定 id、点击 → `MainActivity`）；`res/drawable/ic_notif.xml`（单色小图标）；`res/values*/strings_notify.xml`；`res/xml/network_security_config.xml`（cleartext 关）；`app/lint.xml` + `lintDebug` 零错误；`android/tools/power-audit.sh`（对已安装包跑 `dumpsys jobscheduler|alarm|power|batterystats`、`aapt2 dump permissions/xmltree` 断言无 service/receiver/wakelock/多余权限）；`android/tools/static-audit.sh`（grep `addJavascriptInterface`、`setWebContentsDebuggingEnabled(true)`、`cleartextTrafficPermitted="true"`、`allowFileAccess`、日志里的 URL/token；`aapt2` 权限恰四项）；单测 `NotificationsTest`（纯规则：权限/开关/渠道三态）。
- 与 T5 的接口：T5 设置页在开关打开时调 `Notifications.ensureChannel()` + 请求权限（T5 写 UI 与 `requestPermissions`，T6 提供 `hasPermission/canPost` 与「拒绝后回弹」的规则函数）。
- 验收：`power-audit.sh` 全绿（无 job/alarm/wakelock/service）；`static-audit.sh` 全绿；模拟器：冷启动无权限弹窗；设置页开开关 → 系统弹窗 → 允许 → 测试通知出现 → 点击回 APP；拒绝 → 开关回弹关；`dumpsys notification --noredact | grep jpfoodmap_general` 有渠道；后台 5 分钟 `dumpsys batterystats --charged com.fredhli.jpfoodmap` 无网络/唤醒计数增长。
- 边界：不接 FCM；不加权限；不改 manifest（POST_NOTIFICATIONS 已在 T1）。

### T7 `emu-acceptance` — 模拟器验收脚本（三几何 + 折叠代理 + 流程）（after: T1）

- 读：STANDARDS 全部「验收」列、§15；本文件 §5.7、§5.8、D11；dashboard `tools/emu.sh`（rotate/shot/AVD 注释）、`tools/VERIFY.md` §8b 的取证组织方式、`screenshots/avd1-r5/*`（几何证据格式）；map.py `BOTTOM_SHEET_HTML` / 筛选面板 / `#ss-box` 的 DOM id（用于「卡仍打开」判定）。
- 写：`android/tools/emu.sh`（完成 `launch/diag/fold/net`、`JPFM_EMU_READONLY`）；`android/tools/verify-geometry.sh`（对 foldcover / fold8inner(横+竖+折叠代理) / fold8inner60(横+竖) 各：安装 → 启动 → diag JSON → 首页/开卡/筛选面板三张截图 → 断言 `innerWidth/dpr/imeMode/activityCreates/pageLoads/bootId`）；`android/tools/verify-flows.sh`（返回键两步、深链冷/热、外链 Custom Tab、分享 chooser、断网冷启动、定位权限弹窗、`uimode night` 不重建、`font_scale` 跟随）；`android/tools/diag.sh`（logcat 抓取 + JSON 校验 + 与期望表比对）；`android/tools/VERIFY.md`（检查员手册：怎么跑、期望值表、证据目录结构 `audit_outputs/android-2026-09-06/<who>/<avd>/…`）。脚本对「无该功能的旧 APK」要能报 SKIP 而非崩。
- 验收：对 T1 骨架 APK 跑通（几何断言绿、流程断言大多 SKIP/FAIL 但脚本本身健壮）；对最终 APK（T8 阶段）全绿；每个 AVD 一份 `summary.txt` + 截图；总耗时 ≤ 15 分钟。
- 边界：不改 app 源码；需要页面 DOM 判定时只读 map.py 找 id。

### T8 `docs-integration` — 文档、侧载说明、集成与终验（after: T2 T3 T4 T5 T6 T7）

- 读：全部任务的 STATUS 段与 `audit_outputs/android-2026-09-06/*`；dashboard `README.md`「Install on the phone」「Rebuilding」「When something is wrong」、`deploy/SIGNING-KEY.md`、`docs/STATUS.md` 格式；仓库 `README.md`、`CHANGELOG.md`。
- 写：`android/README.md`（人话开头 ≤10 行；侧载步骤；One UI「外屏续用」、卸载同域 PWA、默认打开 App Links、Google Console 步骤；构建；目录；出问题怎么办；真机清单）；`android/CHANGELOG-ANDROID.md`；`android/docs/STATUS.md`（回退梯走到哪、partial 项、真机待验）；仓库 `README.md` 追加「## Android APP」一段、`CHANGELOG.md` `[Unreleased]` 追加安卓条目（只追加）；`apk/BUILD-INFO.txt`（终版构建）。
- 集成：解决 T2–T6 的签名漂移（以 §5 契约为准），全量 `./build.sh assembleDebug && ./build.sh`、`./gradlew testDebugUnitTest lintDebug`（scratch 内）、跑 T7 全套三几何 + 流程于终版 release APK、跑网站门禁一次（确认 map.py 改动仍绿）。
- 验收：四个门禁清单（§8）自查全绿或诚实标注；`BUILD-INFO.txt` 的 sha256 = `apk/jpfoodmap.apk`；三处版本号一致；`git status` 只含允许范围内的改动。

---

## 8. 四个检查员清单

### gate_build
1. `rm -rf ~/.cache/jpfoodmap-android && cd android && ./build.sh assembleDebug && ./build.sh` 干净构建绿；`find android -name build -o -name .gradle` 为空。
2. `apksigner verify --print-certs android/apk/jpfoodmap.apk`：SHA-256 = `78:9F:E3:5F:02:40:43:2A:CF:C7:E1:71:50:1B:94:1C:29:B9:91:55:D3:58:CF:33:9C:78:AE:C2:10:16:85:D1`；`docs/.well-known/assetlinks.json` 两条指纹相同、包名正确、JSON 合法。
3. `aapt2 dump badging`：`com.fredhli.jpfoodmap`、`versionCode 20000`、`versionName 2.0.0`、`sdkVersion 31`、`targetSdkVersion 36`、`launchable-activity` = MainActivity、两个 label。
4. APK ≤ 8 MiB；`tools/apk-contents.py` 前 15 条无字体/Compose/Firebase；`BUILD-INFO.txt` 的 size/sha256 与文件一致。
5. `./gradlew testDebugUnitTest` 全绿，`lintDebug` 0 error（在 scratch 内跑）。
6. 网站门禁（`verify_build.py`、smoke、compat、sync、worker）绿；`docs/index.html` 重建产物与 map.py 一致（再跑一次 map.py，`git diff --stat docs/index.html` 为空或仅版本戳）。
7. `git status`：改动仅在 `android/**`、`docs/.well-known/assetlinks.json`、`src/tabelog/scrape/map.py`、`docs/` 构建产物、`README.md`、`CHANGELOG.md`。

### gate_emulator（用 release APK）
1. `tools/verify-geometry.sh` 三 AVD 全绿；证据：每几何首页/开卡/筛选三张截图 + `summary.txt`（`innerWidth` 475/932/704/591/688（split60 横向窗口是 689 dp、布局视口 688 CSS px，见 `tools/VERIFY.md` §2 注）、`dpr 2.625`、`imeMode WEBVIEW`）。
2. 折叠代理（fold8inner）：cover→inner→cover 三份 diag，`activityCreates=1`、`pageLoads` 相同、`bootId` 相同、开着的餐厅卡仍在、地图无灰块。
3. 返回键：开卡 → BACK 关卡不退出 → BACK 退出到 launcher。
4. 深链：`pm set-app-links` 后冷启动 `?r=` 开卡；热路径切卡不重载；`pm get-app-links` 列出域名。
5. 外链：Tabelog → Custom Tab 同 task；分享 → chooser；策略切 Chrome 生效。
6. 60% 分屏：fold8inner60 横/竖各一张截图，页面布局合理（无横向滚动、FAB 可见）。
7. 断网冷启动出地图 + 离线条（离线条依赖 `ACCESS_NETWORK_STATE`，见 gate_static 第 1 条与 STANDARDS §10.3a）；错误面板 + 重试路径。
8. 定位：启动无弹窗，FAB 触发弹窗，`geo fix` 后地图飞到坐标。
9. 登录退化：点登录 → 「登录处理失败」+「在浏览器中打开」，无崩溃，logcat 无 token。
10. `uimode night yes` 页面仍浅色且不重建；`font_scale 1.3` → `textZoom 130`。

### gate_static
1. **我们自己在 `AndroidManifest.xml` 里声明的恰好五条**：INTERNET、ACCESS_NETWORK_STATE、ACCESS_COARSE_LOCATION、ACCESS_FINE_LOCATION、POST_NOTIFICATIONS；`aapt2 dump permissions`（合并后）多出来的每一条都要在 `manifest-merger-*-report.txt` 里指到具体的库并已进 `tools/power-audit.sh` 白名单。2.0.0 实测合并后 8 条，多的三条是 `USE_BIOMETRIC` / `USE_FINGERPRINT`（androidx.credentials → biometric，登录必需，normal 级）与 `*.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION`（androidx.core，signature 级）。第四条即失败。〔2026-09-06 口径修订之一：原文写「`aapt2 dump permissions` 恰好四条」，在有原生 Google 登录的前提下永远不可能成立；T1/T6/T8 均提出，gate_docs 检查员按实现口径改写。〕〔2026-09-06 口径修订之二：自己声明的从四条改为五条，加入 `ACCESS_NETWORK_STATE`。它是 normal 级、安装时授予、不弹窗、系统权限页不列出，我们自己的代码一行都不用它——加它只为让 Chromium 的 `NetworkChangeNotifierAutoDetect` 注册连通性回调，否则 WebView 里 `navigator.onLine` 恒为 true、网站的 `#net-offline` 离线条永不出现（gate_emulator 因此判 §9.1 半 FAIL）。理由与回退办法见 `STANDARDS.md` §10.3a。〕
2. `aapt2 dump xmltree`：**本包 `<service>` / `<receiver>` 为零**（库合并进来的三条 —— `CredentialProviderMetadataHolder`、`RevocationBoundService`、`ProfileInstallReceiver` —— 已逐条查明出处、不排期、不耗电，见 `tools/power-audit.sh` 白名单；出现第四条即失败）、`allowBackup=false`、`enableOnBackInvokedCallback=true`、`resizeableActivity=true`、`configChanges` 完整、App Links filter `autoVerify=true` 仅 `jpfoodmap.com`。
3. `network_security_config`：`cleartextTrafficPermitted="false"`；无自定义信任锚。
4. WebSettings：`mixedContentMode=NEVER_ALLOW`、`allowFileAccess=false`、`allowContentAccess=false`、`javaScriptCanOpenWindowsAutomatically=false`、`setWebContentsDebuggingEnabled(BuildConfig.DEBUG)`；`grep addJavascriptInterface` 无；桥源集合仅 `https://jpfoodmap.com`。
5. 节电：无 WorkManager/AlarmManager/JobScheduler/Service/wakelock 引用；`onPause` 有 `onPause()+pauseTimers()+flush()`；定位无 `watch`。
6. 通知：一个渠道、静音、固定 id；`POST_NOTIFICATIONS` 只在设置页申请；拒绝回弹。
7. 日志/Toast 无 URL、无 token；`Diagnostics` 去 query；`strings` 无硬编码密钥。
8. 代码质量：`lintDebug` 0 error；单测覆盖 Routes/Links/Bridge.parse/ShellPrefs/Insets/Notifications 规则；每个文件头有「为什么」注释；无死代码（Files/LinkSheet/Flow 残留）。
9. map.py app-bridge 区块：单一连续块、英文注释、全部 `Native` 用法特性检测、无新 localStorage 键、浏览器路径零行为变化（对照 `git diff`）。

### gate_docs
1. `android/README.md`：开头 ≤10 行人话；侧载步骤（Dropbox 路径、允许来源、覆盖安装、"App not installed" 处理）；真机清单（One UI 外屏续用、卸载 PWA、默认打开、Google Console 步骤含包名与 SHA-1）；构建与目录；权限与隐私清单。
2. `android/docs/STATUS.md`：每任务状态、回退梯、partial 项、真机待验；`android/docs/PLAN.md`/`STANDARDS.md` 与实现一致（抽查 5 条）。
3. `android/CHANGELOG-ANDROID.md` 2.0.0 条目；仓库 `CHANGELOG.md` `[Unreleased]` 有安卓条目；仓库 `README.md` 有「Android APP」段（只追加）。
4. 版本三处一致：map.py `APP_VERSION`、`app/build.gradle.kts versionName`、CHANGELOG。
5. `apk/BUILD-INFO.txt` 存在且内容与 APK 一致；`*.apk` 被 .gitignore；`tools/VERIFY.md` 能让检查员不看别的文件就跑完 gate_emulator。

---

## 9. 主人需要手动做的事 & 待拍板

### 手动步骤（登录能用的前提）
1. **Google Cloud Console → 与 Web client `536198170238-…` 同一个项目 → 凭据 → 创建 OAuth 客户端 ID → 类型 Android**：
   - 包名 `com.fredhli.jpfoodmap`；SHA-1 `9F:60:4D:C5:02:C4:10:94:ED:CF:D6:0F:53:E1:8B:50:0B:03:F0:10`（= `keytool -list -v -keystore ~/.android/debug.keystore -storepass android -alias androiddebugkey | grep SHA1`）。
   - 再建一个给调试包 `com.fredhli.jpfoodmap.debug`（同 SHA-1）——可选，只为模拟器/调试。
   - 不需要改 Web client，不需要改 Worker：APP 拿到的 id_token 的 `aud` 仍是 Web client id。
   - 生效前 APP 的登录按钮会显示「登录处理失败」并提供「在浏览器中打开」；APP 其它功能全部可用（本地模式）。
2. 手机：`设置 → 显示 → 在外屏继续使用应用` 里打开 Japan Foodmap（否则合盖回锁屏）。
3. 手机：若装过 jpfoodmap 的 PWA（Chrome「安装应用」），先卸载它，App Links 才不会打架。
4. 网站上线（主会话 push main）后：`设置 → 应用 → Japan Foodmap → 默认打开` 应显示 jpfoodmap.com 已验证。
5. 备份 `~/.android/debug.keystore`（见 `dashboard/deploy/SIGNING-KEY.md`）——它现在同时是两个 APP 的身份。

### 待拍板（默认值已写进计划，改一个字就行）
1. 包名 `com.fredhli.jpfoodmap`（永久，装上后不能改）？
2. APP 名称：`Japan Foodmap`（系统中文时显示 `日本美食地图`）？
3. 外链默认：Custom Tab（推荐）还是 Chrome？
4. 只绑 `jpfoodmap.com`，不绑 `www`？
5. 网站 smoke 视口是否改成真机实测（416→475、616×816→704×932/932×704）？——网站侧决定，不在本次。
6. 通知开关默认关（现在没有推送源）——同意？

---

## 10. 并行运行约定（所有任务必读）

- **Gradle scratch 各自一份**：`JPFM_ANDROID_BUILD=$HOME/.cache/jpfoodmap-android-<key> ./build.sh …`（`tools/env.sh` 尊重已设值）。共享 `~/.gradle` 缓存是安全的。`./build.sh` 默认不发布 debug 产物；**只有 T1 与 T8 允许更新 `android/apk/`**。
- **模拟器端口**：T2 `5556`、T3 `5558`、T4 `5560`、T5 `5562`、T6 `5564`、T7 `5554`、T8 `5554`；非 5554 一律 `JPFM_EMU_READONLY=1`（`-read-only`，可与他人共用同一 AVD）。每台 2–3 GB 内存，**同时最多 3 台**；用完 `emu.sh stop`。
- **网站构建锁**：只有 T3（与 T8 复验）跑 `flock /tmp/tabelog-build.lock uv run python src/tabelog/scrape/map.py`；其它任务不得重建网站。
- **文件归属**见 §5.1；跨归属需要改动 → 不改，写进自己的 STATUS 段，由 T8 集成。
- **不做 git 写操作**；不碰 dashboard 目录；不向 `api.jpfoodmap.com` 发写请求（登录测试在无账号模拟器上只会失败，不会写 KV）。
- 证据目录：`audit_outputs/android-2026-09-06/<key>/`（截图 PNG、`*.json`、`*.log`、`summary.txt`）。
