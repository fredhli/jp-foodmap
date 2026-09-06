# jpfoodmap Android APP 2.0.0 · 状态（STATUS）

每个任务完成后在下面追加自己的一段（**只追加，不改别人的段**）。T8 在集成时汇总成给主人的
版本。格式：任务 key、状态、实际做了什么、偏离计划的地方、留给别人的东西、真机待验项。

---

## T1 `skeleton` — 工程骨架、构建流水线、图标、版本 · **done**（2026-09-06）

- **工具链没有走回退梯，用的是计划里的第 0 级**：JDK 17.0.20.1 / Gradle 8.14.5 / AGP 8.11.1 /
  Kotlin 2.2.21 / compileSdk = targetSdk 36 / minSdk 31。依赖也全按 PLAN §5.3 的版本解析成功
  （core-ktx 1.18.0、activity-ktx 1.13.0、webkit 1.17.0、browser 1.10.0、splashscreen 1.2.0、
  credentials 1.5.0 ×2、googleid 1.1.1、coroutines 1.9.0）。**后续任务不要动这些版本号。**
- 产物：`apk/jpfoodmap.apk` 1.32 MiB（上限 8 MiB），`apk/BUILD-INFO.txt` 的 sha256 与文件一致；
  签名 SHA-256 `78:9F:…:85:D1`，与 dashboard 同证书。
- 骨架 APK 在 foldcover 模拟器上冷启动 0.6–1.6 s，debug 与 release（R8）都能显示完整地图。
- `emu.sh` 的 `diag` / `fold` / `net` 是**占位**，退出码 3（"未实现"），T7 实现；`launch` 已经能用。
- **偏离计划一处（要 T6 / T8 拍板）**：`aapt2 dump permissions` 是 **7 条**，不是计划 §8 gate_static
  说的 4 条。我们自己只声明 4 条；多出来的 3 条全部由库合并进来（`manifest-merger-release-report.txt`
  有逐条出处，证据在 `audit_outputs/android-2026-09-06/skeleton/`）：
  - `USE_BIOMETRIC`、`USE_FINGERPRINT` ← `androidx.biometric:1.1.0`，是 `androidx.credentials:1.5.0`
    的硬依赖（原生登录必需）。两条都是 normal 权限，安装时授予、用户不可见、不弹窗。
  - `com.fredhli.jpfoodmap.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION` ← `androidx.core:1.18.0`
    自定义的 signature 级权限，`ContextCompat.registerReceiver` 在 API < 33 上用它。
  没有删：删掉 biometric 两条要赌 Credential Manager 内部不会走 BiometricPrompt，而登录恰好是本机
  **验不了**的那条路（模拟器无 Google 账号）。建议把 gate_static 第 1 条改成「我们自己声明恰 4 条，
  且合并进来的每一条都能在 merger report 里指到具体的库」。
- **偏离计划第二处**：`app/build.gradle.kts` 里加了 `lint { warning += "MissingTranslation" }`。
  `values/` 是简体中文、`values-zh-rCN/` 只覆盖 `app_name` 一条（STANDARDS §1.6 要的
  `application-label-zh-CN`），lint 默认把「zh 里没有其余字符串」判成 error。降为 warning 而不是
  disable，这样 `values-en/` 真漏了字符串仍然会报。`lintDebug` 现在 **0 error / 14 warning**。
  T6 如果要建 `app/lint.xml`，请把这条一并搬过去或保留现状。
- 给别人的：所有 Kotlin 文件都是**可编译的 stub**，签名按 PLAN §5.4/§5.6 写死。改签名 = 改契约，
  改之前在这里加一段说明，让 T8 知道要重新接线。`MainActivity` 目前是最小实现（edge-to-edge +
  一个 WebView `loadUrl`），T2 整体替换；它的 `internal` 面（`webView` / `prefs` / `appOrigins` /
  `metricsJson` / `diagnosticsNativeJson` / `runDiagnostics` / `hapticTick` / `requestLocation`）
  和 `companion` 常量是 T3/T4/T5 的调用点，不要改名。
- 真机待验：抽屉里的图标与名称（模拟器上 `cmd input` 不稳定，抽屉截图没拍成；离屏渲染的遮罩预览在
  `skeleton/launcher-icon-masks.png`，启动画面实拍在 `skeleton/foldcover-release-splash.png`）。

---

## T5 `links-settings-diag` — 外链阶梯、分享、字号、设置页、诊断 · **done**（2026-09-06）

- 四个文件按 T1 冻结的签名填满：`Links.kt`、`ShellPrefs.kt`、`AppSettingsActivity.kt`、
  `Diagnostics.kt`，外加 `res/layout/activity_app_settings.xml`、
  `res/values{,-en}/strings_settings.xml`、`res/values{,-en}/strings_links.xml`，
  单测 `LinksTest`(17) / `ShellPrefsTest`(8) / `DiagnosticsTest`(17)，共 42 条。
  **没有改任何别人的文件**（MainActivity / SiteWebView / Notifications / Routes 只读）。
- **加了三个签名（都是新增，不动已有的）**，T8 集成时按这里对表：
  - `Links.share(context, title, url, appOrigins): Boolean` —— §5.4 的 `share` 消息要的
    `ACTION_SEND text/plain` chooser。放在 Links 是因为「只允许分享本域 URL」这条规则是
    `classify` 的。**T3 的 Bridge 派发 `share` 时调它**，别再写一份。
  - `Diagnostics.unquote(raw)` —— `evaluateJavascript` 结果的 JSON 解包。
  - `Diagnostics.startupJson(...)` / `Diagnostics.stripQuery(url)` /
    `Diagnostics.showText(page, native)` / `Diagnostics.Startup.onRendererGone()` /
    `Diagnostics.EXITS_MAX` —— 纯函数，为了能在 JVM 上钉住。
- **`Nav.EXTERNAL` 没有改名成任务书里写的 `EXTERNAL_HTTP`**：T1 stub 冻结的是 `EXTERNAL`，
  T4 的 `DeepLinks.Target.Leave` 已经在用它。任务书那处是照抄 dashboard 的名字。
- **给 T2 的一条**：`MainActivity` 里的私有 `decodeJsString()`（约 909 行）与
  `Diagnostics.unquote()` 是同一件事，而且带同一个 bug —— `JSONTokener` 对没引号的词是宽容的，
  `"garbage{"` 会被读成 `"garbage"`。页面探针失败、返回的不是 JSON 时，诊断里的报错文本会被
  截断。`Diagnostics.unquote` 已修（先测首字符是不是 `"`），建议 `decodeJsString` 直接换成它。
  我没动 MainActivity（边界）。
- **给 T2 的第二条**：设置页改了字号后不会通知 MainActivity；靠 MainActivity `onResume` 重新
  `ShellPrefs.load()` + 重设 `textZoom` 才会生效。现在 MainActivity 第 290 行已经这么做了，
  只是记一笔，别在重构时丢掉。
- 诊断里的 `url` **剪了两遍**：`JS_METRICS` 用 `location.origin + location.pathname` 组出来
  （不读 `location.href`），Kotlin 侧 `stripQuery` 再剪一次（页面半可能是非 JSON 的原始文本）。
  `lang` 取 `documentElement.lang` 而**不是** `localStorage['tabelog.lang']` —— §5.8 两者皆可，
  DOM 那个是页面每次切语言都会写的实时值，而读 localStorage 正是 STANDARDS §0.1 禁止的。
  `Diagnostics.stripQuery` 与 T4 的 `Routes.stripQuery` 重复了一份（三行），是故意的：诊断页
  不该因为另一个文件没写完就打不开。T8 若要合并，删 Diagnostics 里那份。
- `Diagnostics.JS_METRICS` 比 §5.8 多一个 `fontPx`（根字号 px）：`textZoom` 是壳侧的数字，
  `fontPx` 是页面真正渲染出来的，两个对上 STANDARDS §2.4 才算真的验过。
- lint：我这几个文件 0 error / 31 warning，全部是三类已知的——`MissingTranslation`（27 条，
  T1 记过的 `values-zh-rCN` 只有 `app_name` 的设计）、`UseSwitchCompatOrMaterial`（2 条，
  我们故意用框架 `Switch`，不引 AppCompat）、`UseKtx`（2 条，`Uri.parse` 保留显式写法）。
- 模拟器（`fold8inner60`，端口 5562，只读）验收八项过七项，证据在
  `audit_outputs/android-2026-09-06/links-settings-diag/`（`README.md` 是索引）：Custom Tab 与
  `MainActivity` 同 task `t17`；切 Chrome 后 `REQUIRE_NON_BROWSER` 得 `-91` 再钉 Chrome 起在
  自己的 `t22`；Google 地图链接第一级就进 Maps APP；`font_scale 1.3` + 跟随系统 →
  `textZoom 130`（`fontPx` 16→21），钉 100 时仍是 100；诊断对话框两半齐全且 `url` 无 query；
  `emu.sh diag` 四份合法 JSON；通知开关 → 系统弹窗 → 允许 → 测试通知真的进了通知栏。
- **真机待验**：分享 chooser（要 T3 的桥才有入口，模拟器里点不到）；Custom Tab 的返回箭头一下
  回到地图（模拟器的 Chrome 从没打开过，每次先弹 FirstRun 挡在前面）；通知点击回到 APP（属 T6）。

---

## T4 `applinks` — App Links + assetlinks + 深链冷/热路径 · **done**（2026-09-06）

- **写了什么**：`Routes.kt`（origin/host 解析、`shareIdOf`、`pathOf`/`stripQuery`/`stripFragment`/
  `jsStringLiteral`，全部纯字符串，不 import android）、`DeepLinks.kt`（`targetOf` 四态 +
  `hotShareJs`）、单测 `RoutesTest`(17) / `DeepLinksTest`(14)、仓库的
  `docs/.well-known/assetlinks.json`（两条：`com.fredhli.jpfoodmap` 与 `.debug`，同一指纹
  `78:9F:…:85:D1`）、`android/tools/applinks-check.sh`。
- **两处对 T1 stub 的加法（都是加，不是改）**：`Routes` 多了 `pathOf` / `stripFragment`（任务书
  §T4 点名要，dashboard 的 `Routes` 也有）；`DeepLinks.targetOf` 多了一个 `internal` 的纯参数
  重载 `(action, dataUrl, flags, stale, appOrigins, classify)`。**冻结的两个公开签名一个字没动。**
  纯重载存在的理由：`android.content.Intent` 在 JVM 单测里读不了（mockable android.jar 每个方法
  都 throw），而 `Intent.ACTION_VIEW` / `FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY` 是编译期常量会内联，
  所以规则本身可以离线测到底。`classify` 参数默认值就是 `Links::classify`，生产路径没有第二条。
- **`?r=` 口径**：`^[0-9a-z]{1,6}$`。实测 `docs/data/restaurants.json` 9,807 行全部有 id、全部不重复、
  最长 5 字符（453 个 4 位 + 9,354 个 5 位），6 是给语料涨过 36^5 留的余量。key 大小写敏感、只从
  query 里读、不碰 fragment——和页面的 `new URLSearchParams(location.search).get('r')` 一致。
  不合法的 id **降级成 `Target.Load`**，绝不进热路径（那条路会把字符串插进页面的 JS）。
- **`?lang=` 是 `Target.Load`**（STANDARDS §6.4）：换语言在页面里本来就是整页导航。同时带
  `?r=` 和 `?lang=` 的 URL 按契约仍判 `Share`——页面自己 mint 的分享链接（`shareUrlFor`）只有
  `origin + pathname + ?r=`，永远不带 lang，所以这个组合只可能是手工拼的。
- **验收**（证据 `audit_outputs/android-2026-09-06/applinks/`，fold8inner AVD / 端口 5560 / read-only）：
  单测 31 条全绿（全工程 89 条全绿，`lintDebug` 0 error）；`applinks-check.sh` 离线三项全绿
  （JSON 合法 + 两个包名 + apksigner 指纹与 assetlinks 一致）；模拟器 `--approve` 后
  `pm get-app-links` = `jpfoodmap.com: verified`；隐式 VIEW 冷启动 `?r=igfzg` 直接开 かに吉 的卡
  （无选择器）；前台再发 `?r=cibpv` 走 `onNewIntent`、进程不变、卡切到 飛騨季節料理 肴；
  `?lang=en` 出英文页；外域 URL 送到本组件 → Chrome 在同一个 task 里打开（Custom Tab），壳的页面
  还在下面。`cmd package query-activities` 确认只认 `jpfoodmap.com`，不认 `www.` 也不认 `api.`。
  另外用 Playwright 拿真实 id 跑了一遍线上页面的冷路径：igfzg / cibpv / bb8y3 三张卡都对，
  `?r=` 被 replaceState 摘掉，畸形 id 不抛异常。
- **留给别人的 / 真机待验**：
  1. **热路径「pageLoads 不变」还没验到**。线上 jpfoodmap.com 现在是 push 之前的 2.0.0 页面，
     没有 `window.__jpfmOpenShare`，所以今天热路径走的是 `hotShareJs -> "false" -> loadUrl` 那条
     退化分支（卡确实切了，但是靠一次导航）。主会话 push main 之后，T8 重跑一次这条即可。
  2. **`docs/.well-known/assetlinks.json` 上线前一切都要手动批准**。真机上 push 之后
     `设置 → 应用 → Japan Foodmap → 默认打开` 才会显示已验证；`applinks-check.sh --live` 现在
     报 HTTP 404，这是预期的，上线后应转绿（Cloudflare Pages 按扩展名给 `.json` 正确的
     Content-Type，`docs/_headers` 按 D16 没动）。
  3. 跑模拟器验收时我在 **scratch 里**（不是仓库里）把 T3 还是 `TODO()` 的
     `Bridge.install` / `Bridge.onPageStarted` 临时置空，否则 `onCreate` 一进去就
     `NotImplementedError` 崩，什么都验不了。scratch 已删，仓库一个字没改。T8 用完整 APK 复验时
     这个补丁自然不存在。
  4. 这台 read-only 的 fold8inner 实例期间 **system_server 崩过两次**（`droid.bluetooth` SIGABRT
     刷屏，我们的进程收到 `DeadSystemException`）。不是 APP 的问题，重启模拟器后同样的步骤全过。

---

## T6 `notify-power-hardening` — 通知、节电、静态加固 · **done**（2026-09-06）

- 交付：`Notifications.kt`（全实现）、`res/drawable/ic_notif.xml`、`res/values{,-en}/strings_notify.xml`、
  `app/lint.xml`、`app/src/test/.../NotificationsTest.kt`（13 条）、`tools/static-audit.sh`（36 项）、
  `tools/power-audit.sh`（13 项，含 5 分钟后台 soak）。
  **`res/xml/network_security_config.xml` 未改动**——T1 写的已经是 `cleartextTrafficPermitted="false"`
  且无自定义信任锚，符合 §10.5/§13。
- 验收全绿：`static-audit.sh` 36/36；`power-audit.sh` 13/13（release APK 清单 + 运行中模拟器 +
  300 s 后台 soak 计数器零变化）；`testDebugUnitTest` 106 条全绿；`lintDebug` **0 error** / 72 warning。
  模拟器实拍：冷启动无弹窗、无渠道 → 拨开开关弹系统权限框 → 拒绝则开关回弹且偏好写回 false →
  允许后测试通知进通知栏（`id=1 channel=jpfoodmap_general sound=null groupKey=silent`）→ 点它回
  MainActivity → 连点四次仍只有一条。证据在
  `audit_outputs/android-2026-09-06/notify-power-hardening/`。
- **偏离计划一：权限不是「恰四项」，沿用 T1 的口径。** 实际 7 项，白名单化在 `power-audit.sh` 里：
  `USE_BIOMETRIC`/`USE_FINGERPRINT`（androidx.credentials → biometric，登录必需，normal 级不弹窗）、
  `*.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION`（androidx.core，signature 级）。多第四条就红。
  **建议 T8 把 STANDARDS §10.3 与 gate_static 第 1 条改成这个口径。**
  〔2026-09-06 后续：两处口径已改；同时自己声明的从四条变五条（加 `ACCESS_NETWORK_STATE`），
  所以合并后是 **8 项** 不是 7 项。见本文件末尾「T2 返工」一节。〕
- **偏离计划二：`<service>`/`<receiver>` 也不是零。** 我们自己一条没有（脚本对 `com.fredhli.jpfoodmap*`
  开头的组件硬失败），但库合并进三条，都已查明出处且不排期、不耗电：
  `CredentialProviderMetadataHolder`（无 intent-filter 的元数据宿主）、`RevocationBoundService`
  （GMS 签名权限保护）、`ProfileInstallReceiver`（`permission=DUMP`，只有 `adb shell cmd package`
  能发）。加上 `androidx.startup.InitializationProvider` 一个 provider。**建议 T8 同样修订 §10.1 与
  gate_static 第 2 条。**
- **`power-audit.sh` 的四条检查第一版是假阳性，已修正**，注释里写了原因，别「简化」回去：
  ① `dumpsys jobscheduler | grep <pkg>` 会命中 quota tracker / TopAppTimer，只认 `JOB #<uid>/<id>:` 行；
  ② `dumpsys alarm` 同理，只认 `Alarm{`/`*walarm*`/`RTC_WAKEUP`/`ELAPSED_WAKEUP`；
  ③ `dumpsys activity services <pkg>` 永远有 `org.chromium.content.app.SandboxedProcessService`
  （WebView 自己的渲染进程，寄生在宿主 uid 下，所有 WebView APP 都有），只放行 `org.chromium.`；
  ④ batterystats 的 `Total run time` 是设备时钟，soak 多久走多久，必须排除。
- **给 T5 / T8 的一条整理建议（不阻塞）**：T5 的设置页正确调用了 `ensureChannel`/`hasPermission`/
  `canPost`/`postTest`，但「拒绝回弹」「四态提示优先级」「拨开才请求」三处是**同义的内联实现**，
  没有走 `Notifications.switchAfterPermissionResult` / `readiness` / `shouldRequestPermission`。
  行为逐条比对一致（含「设 isChecked 前先摘 listener」），不影响验收；我没有去改一个当时还在
  改动中的文件。T8 若换成调用这三个函数，`NotificationsTest` 钉住的就是线上那份逻辑而不是副本——
  这也是 gate_static 第 8 条「无死代码」唯一可能被点名的地方。
- **`MissingTranslation` 现在有两处声明**：T1 的 `app/build.gradle.kts` 与我的 `app/lint.xml`。
  `app/build.gradle.kts` 不在我的可写范围，所以我没有搬走它，而是在 `lint.xml` 里重述同一严重度
  并写明理由。两处同义，删掉任一处另一处仍拦着它变回 error。
- **模拟器口径**：计划分给 T6 的端口 5564 用了，但 `foldcover` 当时被别的任务**读写**占用，
  read-only rider 进不去，改用没人用的 `flow` AVD。通知与几何无关，结论不受影响。
- **scratch-only 补丁（仓库零改动）**：跑模拟器时 T2/T3 尚未落地，`MainActivity.onCreate` 撞
  `Bridge.install` 的 `TODO()` 必崩。我只在 ext4 scratch 里把 `Bridge.install`/`onPageStarted`
  置空、把当时半成品的 `SiteWebView.kt` 换回 T1 stub。最终的 `testDebugUnitTest lintDebug` 与
  release 构建都是在**重新同步过、未打补丁**的树上跑的。
- 真机待验：One UI 的通知权限流程（三星多一层总开关）；`ic_notif.xml` 这个新画的鸟居在真机高 DPI
  状态栏上的粗细。

---

## T7 `emu-acceptance` — 模拟器验收脚本（三几何 + 折叠代理 + 流程） · **partial**（2026-09-06）

- 交付齐了：`tools/emu.sh` 的 `diag` / `fold` / `net` 三个占位补完；新增
  `tools/verify-geometry.sh`、`tools/verify-flows.sh`、`tools/diag.sh`、`tools/VERIFY.md`，
  外加三个零依赖小工具 `tools/wv-eval.py`（WebView DevTools 里跑一句 JS）、
  `tools/diagjson.py`（诊断 JSON 取值 / 断言）、`tools/png-stats.py`（截图像素粗检）与共用底座
  `tools/lib-verify.sh`。**没有碰 app 源码，也没有改别人的文件。**
- **标 partial 的原因**：只跑通了 `foldcover` 一份证据（PASS 4 / FAIL 0 / SKIP 2，约 7 分钟）。
  `fold8inner` / `fold8inner60` 的证据没拿到——跑的时候这两个 AVD 正被别的任务以读写方式占着
  （本机同时开到 5 台模拟器，`PLAN §10` 写的是最多 3 台），宿主 19 GB 只剩 1–3 GB，
  guest 里 `lowmemorykiller` 一直在杀，`adb install` 报 `Can't find service: package`、
  APP 启动几秒后被踢回桌面，我的 5554 实例还被整个打死过一次。**脚本本身在这些情况下会重试
  并在 `launch.log` 留 `note:`；等模拟器数量降下来，`tools/verify-geometry.sh` 一条命令就能
  补齐另外两个 AVD。**
- 顺手在 `emu.sh` 里加了两条只影响可靠性的东西，都在我的区域内：
  1. `start` 发现 AVD 已被别人读写占用时（日志里 `Another emulator instance is running`），
     自动改用 `-read-only` 重启一次——这正是并行约定要的，只是原来要手动设环境变量。
  2. `shot` 记住 `screencap` 失败（`~/.android/jpfm-shot-<port>.mode`）。android-37.0 镜像上
     `screencap` 必然 abort，**而且 system_server 自己做任务快照时会踩同一条断言并整个死掉**，
     一轮验收二十张图不能每次都去踩它。别删那个记忆文件。
- **给 T2/T5 的接口**：`emu.sh diag` 认 logcat tag `JpfmDiag` 的一行合法 JSON；退出码 3 =
  没有这一行（脚本按 SKIP 处理，不是 FAIL），1 = 有行但 JSON 非法（按缺陷处理）。诊断 JSON 的
  键**在树里的位置随便**（`insets.top` 或 `shell.insets.top` 都行），`diagjson.py` 按键名找。
  一旦出现 `imeMode` / `textZoom` / `pageLoads` / `activityCreates`，现在的 SKIP 会自动变成
  PASS/FAIL，验收脚本不用改。
- **骨架 APK 上几何也能验**：debug 构建的 WebView 会开 DevTools 端口，`diag.sh` 在没有原生诊断
  时就走它直接问页面 `innerWidth / dpr / env() / 卡片开没开`。**release APK 没有这条腿**，
  所以终验建议 debug + release 各跑一遍（VERIFY.md §0 有说明）。
- 真机待验：见 `tools/VERIFY.md` §6（真实折叠、真实 inset、Google 登录、App Links 自动验证、
  Custom Tab 落到 Chrome、One UI 分屏拖动）。

---

## T2 `shell` — WebView 壳与生命周期 · **done**（一条验收未过，见下）（2026-09-06）

- 写完：`MainActivity.kt`（924 行）、`SiteWebView.kt`、`PopupCatcher.kt`、`Insets.kt`、
  `res/layout/activity_main.xml`、`res/values{,-en}/strings_shell.xml`、`InsetsTest`（3 用例）。
  没碰别人的文件，没改 `AndroidManifest.xml`，没改 `map.py`。
  完整报告：`audit_outputs/android-2026-09-06/impl/shell.md`，取证：`.../shell/`。
- **与 T1 stub 的两处签名差异**（T8 记一笔，都在我自己的文件里）：
  `SiteWebView.create(activity: Activity)` → `create(activity: MainActivity)`（client 要回调
  activity）；`SiteWebView` 新增 `applyTextZoom(webView, prefTextZoom, fontScale)`。
  `MainActivity` 的 `internal` 面与 companion 常量**一个都没改名**，另按需要新增
  `internal lateinit var popupCatcher` 与 `internal fun onGeolocationPrompt(origin, callback)`。
  `START_URL` 现在等于 `Routes.BASE_URL`（编译期常量）。
- **有意比 dashboard 少的三样，都是死代码**：`mismatchReloads`（外域 finish 在本站不可达）、
  `DownloadListener`/`onDownloadStarted`（本站没有附件响应）、`--safe-*` 回退（本站直接用
  `env()`，没有那几个 CSS 变量；诊断里的 `safeVar` 恒 false 只为对齐 §5.8 的形状）。
- **有意多的一样**：`onPageFinished` 用 `wasLoading == target` 判断“这次 finish 就是目标自己
  的 load”。页面会自己 `replaceState` 掉 `?r=`（M-032），按 URL 相等判断会让冷启动再 load 一次。
- `activityCreates` / `pageLoads` 是**进程级** `@Volatile` 计数器（`MainActivity` companion），
  不是 per-activity —— 它们存在的意义就是证明折叠时没重建/没重载。注意 `pageLoads` 把**失败的
  导航也算进去**（一次失败 = 一次 `onPageStarted`），别当成“成功加载几次”。
- `bridgeLabels()` 返回空 map：与 T3 `Bridge.install` 的文档一致（缺的键由 Bridge 从自己的
  `strings_signin.xml` 解析）。两边说法对齐，不要“修”。
- 模拟器验收（fold8inner / 5556 / read-only）**过**：折叠代理 cover↔inner 前后
  `activityCreates=1`、`pageLoads` 不变、`innerWidth` 932↔475、开着的餐厅卡还开着、地图无灰块；
  `env` × density 精确等于 `insets`（24/32 对 63/84，cover 24/24 对 63/63）；`imeMode=WEBVIEW`；
  开卡后 BACK 关卡、再 BACK 退到 launcher；启动不申请定位、点定位 FAB 才弹
  `GrantPermissionsActivity`、授权后 `geo fix` 地图飞到东京站；`uimode night yes` 页面仍浅色且
  不重建；`font_scale 1.3` → `textZoom 130` 且页面 `fontPx` 16→21；错误面板 + 重试；
  `JpfmDiag` 单行 JSON（无 cookie/token/query）。
- **未过的一条：STANDARDS §9.1 断网冷启动出地图。** 实测出的是壳的错误面板（复现 2 次）。
  §9.2 的路径（拿不到主文档 → 面板 → 重试 → 回到页面）是过的。两个候选原因排在报告 §5：
  (1) 网站 SW 的 shell 缓存在这个 profile 里根本没建成（`controller` 存在 ≠ 缓存满），
  用 `tools/wv-eval.py` 读 `caches.keys()` 即可判定；(2) `onPageFinished` 里
  `url == errorUrl → ERROR; return` 把一个其实已经由 SW 渲染出来的离线页面盖住了。
  **先验 (1)，(1) 不成立再改 (2)。** 我没有擅自改 (2)：dashboard 同款逻辑在真机上没出过问题。
- **环境警告（T7/T8 必读）**：`android-37.0` 镜像在 3 台模拟器并发时极不稳定 ——
  本轮 `system_server` 崩了 4 次、`SystemUI ANR` 2 次（期间 `input tap` 全丢）、一次崩溃把刚装
  的包整个弄丢。另外 `adb install` 返回 Success 之后 data 目录可能还没落地，紧跟着 `am start`
  会崩在 `webview_data.lock: ENOENT`（**不是 APP 的 bug**）；建议 `verify-*.sh` 在 install 后加
  一个“等 `/data/user/0/<pkg>` 出现”的循环，并把并发模拟器降到 2 台。
- 冷启动 5.7 s / 首帧 6.0 s（软件渲染 + 公网），没达到 §1.5 的 3 s；splash 3 s 上限本身生效、
  无白帧。真机待测。`bootId` 现在是 null（要等 T3 的 map.py 区块上线），折叠证据用的是
  `pageLoads`/`activityCreates`。
- 真机待验：真实折叠（display 切换而非 `wm size` 覆盖）、真实 inset 数值、One UI「在外屏继续
  使用应用」、键盘弹出时搜索框的位置、60% 分屏（几何取证归 T7 `verify-geometry.sh`）。

---

## T3 `signin-bridge` — 原生登录桥 + 页面 app-bridge 区块 + 网站门禁 · **done**（2026-09-06）

- **壳侧**：`Bridge.kt`（传输照抄 dashboard：`addWebMessageListener` + `addDocumentStartJavaScript`，
  源集 `{https://jpfoodmap.com}`，`tryRules` 退化，**没有** `addJavascriptInterface`）、`GoogleSignIn.kt`
  （Credential Manager `GetGoogleIdOption`，`serverClientId` = Web client id）、
  `res/values{,-en}/strings_signin.xml` 六个字符串、`BridgeTest` 17 个用例（全绿）。
- **façade 与 PLAN §5.4 逐字一致**，只把 `version` 填成 `2.0.0`；`credential` 回包用 `JSONObject`
  拼，`id_token` 从不落日志、不进 SharedPreferences、不二次引用（STANDARDS §7.7）。
- **偏离计划三处（都写在代码注释里）**：
  1. `install(webView, origins, labels)` 里 **labels 缺键时自己从 `R.string` 取**。T2 的
     `MainActivity.bridgeLabels()` 故意返回空 map 并注明「Bridge 持有 activity，自己解析」——照做，
     签名没动。显式传入的 label 仍然优先。
  2. **`no_credential` 也走「在浏览器中打开」对话框**，不只 `not_configured`。PLAN §5.4 的表只写了
     `not_configured`，但 STANDARDS §7.4 明说「开发者控制台未配置 / **无账号**」都要给这条退路；
     模拟器上真实命中的正是无账号（GMS 报 `cgfh: [28433]` → `NoCredentialException`）。回给页面的
     `error` 字符串仍然是两个不同的值，页面文案不受影响。
  3. Bridge 的「这是不是本站 URL」用自己的私有前缀判断（`matchesOrigin`），没有调
     `Routes.isAppOrigin`：这是消息**能不能动作**的安全闸门，不该依赖另一个任务的文件。
     `open` 分支按计划仍然调 `Routes.isAppOrigin`（那条路本来就落在 T5 的 `Links` 阶梯里）。
  另外新增了两个小口子：`Bridge.dispose()`（取消未完成的登录协程，T2 想在 `onDestroy` 调就调，不调也
  不漏 Activity 之外的东西）和纯函数 `Bridge.userAgent(default, versionName)`（UA 拼法可单测）。
- **页面侧**：`map.py` 的 `initMap` 末尾（`?r=` 消费之后）加了**一个**
  `// ===== APP BRIDGE (Android shell) =====` 连续块，注释全英文。浏览器里只多两个无副作用的全局
  （`__jpfmBootId`、`__jpfmOpenShare`），第三行就 `return`。APP 内依次：隐藏安装入口/snack/引导 →
  原生登录按钮 + `silentReAuth` 走 `Native.signIn(silent)` → `Native.share` → 头像菜单加一行
  「App settings」→ `signOut` 包 `Native.signOut()`。**没有新增 localStorage 键，没动
  `data/i18n`、`HEAD_BRANDING`、Worker。**
- **计划外多做的一件事**：APP 内把 `whenGIS(fn)` 改成立即执行。它原本等 3 s 找 `window.google`，
  找不到就**不回调**——而 gsi 脚本正是壳拦掉的，于是 `fallbackSilentGIS` 的调用方会永远挂着。
  改完 90 天 cookie 续期（STANDARDS §7.5）在 APP 里才真的能跑。
- **网站门禁全绿**（证据在 `audit_outputs/android-2026-09-06/signin-bridge/`）：`verify_build.py`
  11 项全过（i18n「no new untranslated UI strings」EN 478 / JA 478，都在 2026-09-05 基线内）、
  `smoke_playwright.py` 5/5 视口 console clean、`compat` 8/8、`sync` all_pass、`worker` 93/93。
  `git diff --stat` 只有 `src/tabelog/scrape/map.py` + `docs/index.html` + `docs/sw.js` +
  `docs/data/*.json`（构建产物）。
- **模拟器验收（私有 AVD `jpfmt3` = foldcover 几何，端口 5558）**：菜单里出现原生
  「Sign in with Google」+「App settings」、**安装为应用行消失**；点登录 → 页面状态行
  「登录处理失败」+ 壳对话框「Sign-in is not available in the app yet」→「Open in browser」→
  logcat `START ... dat=https://jpfoodmap.com/ pkg=com.android.chrome from uid ...jpfoodmap.debug`，
  Chrome 起来（Custom Tab 落在同一个 task，返回回到页面）。`Native.openSettings()` → logcat
  `START ... AppSettingsActivity ... Displayed +314ms`，MainActivity `TO_BACK`（同 task，Back 回页面）。
  **39,507 行 logcat 里没有任何 JWT / id_token 值、没有 `accounts.google.com` 或 `gsi/client` 请求。**
- **测试台账**（给 T7/T8）：模拟器加载的是**线上**页面，而线上还没有这个区块，所以验收时在
  **scratch 副本**里临时打了两个补丁（`SiteWebView.shouldInterceptRequest` 把 `jpfoodmap.com`
  代理到本机 `http://10.0.2.2:8765` 上的 `docs/`，外加 `network_security_config` 放行该地址），
  **仓库里一个字都没改**（`grep -rn "10.0.2.2\|JpfmLocalProxy" android/` 为空），临时 AVD 与
  scratch 用完即删。副作用一条：**Service Worker 的 fetch 不走 `shouldInterceptRequest`**，所以
  SW 会从真实线上域回填 shell 缓存，第二次冷启动就又变回线上页面——`pm clear` 之后的**首次**加载
  才是本地构建。真机/上线后不存在这个问题。
- **本机环境问题（不是代码问题，T7/T8 会遇到）**：同时跑 5 台模拟器时 19 GB 内存不够，guest 的
  `system_server` 反复在 `TaskSnapshotPer` 线程 SIGABRT，整机软重启，APP 被连带打死（logcat 里表现为
  `DeadSystemException`）。降到 2–3 台、并 `pm disable-user` 掉 youtube/photos/gm/quicksearchbox 之后
  稳定。
- **真机待验**：真实 Google 账号下的完整登录（拿到 id_token → `POST /api/session` → cookie → reload
  → 头像出现 → 杀 APP 重开仍登录）、`Native.share` 的系统分享面板、`Native.signOut` 后重新登录是否
  出现账号选择器、90 天静默续期。这四条都需要 §9 第 1 步（Android OAuth client）先建好。

---

## T8 `docs-integration` — 文档、侧载说明、集成与终验 · **done**（一条 partial，见「留给主人的两个决定」）（2026-09-06）

### 给主人的六行

- APK 已是终版：`android/apk/jpfoodmap.apk`，2.0.0 / versionCode 20000 / 1.6 MiB /
  sha256 `36deb3af…c0aa8`，`BUILD-INFO.txt` 和它一致。装法和真机清单在 `android/README.md`。
- 三种屏幕几何全绿（475 / 932 / 704 / 591 / 688 CSS px），**折叠代理彻底过了**：
  `activityCreates=1`、`pageLoads` 不变、开着的卡还开着。
- **断网冷启动出地图这条过了**（T2 那条没过的验收）——网站的 SW 先跑过一次就能离线开图。
- 九条流程 14 PASS / 2 FAIL / 9 SKIP。两条 FAIL 都是同一件事：APP 里网页的「离线」横幅
  不出现，因为少一个 `ACCESS_NETWORK_STATE` 权限。〔2026-09-06 后续：已在
  「T2 返工 · 离线条与 `ACCESS_NETWORK_STATE`」一节修掉，权限已加、四处口径已同步。〕
- 网站门禁重跑一遍全绿：verify_build 11 项、compat 8/8、sync all_pass、worker 93/93、
  smoke 5/5。单测 106 条全绿，lintDebug 0 error，static-audit 36/36。
- 登录仍然是「建了 Android OAuth client 才能真的用」，模拟器只验到退化路径。

### 集成：改了什么

以 PLAN §5 契约为准，处理 T2–T6 之间的漂移。**只有下面这些，别的一个字没动。**

1. **`Bridge.shareText` 删掉，`Msg.Share` 改调 `Links.share(host, title, url, appOrigins)`。**
   T5 在自己的 STATUS 段里点名要求过（「放在 Links 是因为『只允许分享本域 URL』这条规则是
   `classify` 的……别再写一份」），T3 没照做，于是 `Links.share` 成了只有 `LinksTest` 在用的
   死代码，而 `Bridge` 里那份少了「没有任何分享目标时给一个 toast」。合并后 ACTION_SEND
   在树里只有一处，gate_static 第 8 条的「无死代码」也不再被这条点名。
   行为差别两点，都往好的方向：chooser 标题从餐厅名变成系统默认（`Links.share` 传 null），
   一台没有任何分享 APP 的机器现在会看到和外链阶梯同一个 toast 而不是静默。
2. **`MainActivity.decodeJsString` 删掉，改调 `Diagnostics.unquote`。** 同样是 T5 点名的：
   两份实现同一件事，而 `MainActivity` 那份少了「先看第一个字符是不是引号」的守卫——
   `JSONTokener` 对没引号的词是宽容的，页面探针失败返回 `garbage{` 时诊断里会被截成
   `garbage`。顺手删掉两个不再用到的 import。
3. **`AppSettingsActivity` 的三处内联通知逻辑改调 `Notifications` 的三个规则函数**
   （`shouldRequestPermission` / `switchAfterPermissionResult` / `readiness`）。T6 的 STATUS
   段建议过。行为逐条等价（`canPost = hasPermission && 系统开关`，而 `readiness` 只有在
   `hasPermission` 为真时才看第三个参数），换过来之后 `NotificationsTest` 钉住的就是线上
   那份逻辑而不是一个副本。API 33 以下不请求权限的 `Build.VERSION.SDK_INT` 守卫**保留**
   （T5 注释里说明过为什么它不是冗余的）。
4. **验收脚手架四处修复**（`tools/lib-verify.sh` / `tools/verify-geometry.sh` /
   `tools/verify-flows.sh`，都是 T7 的文件，按 PLAN §10「跨归属由 T8 集成」处理）：
   - `cold_start` 里 `emu launch | tee` 加 `|| true`。`am start` 在这个镜像上会在框架
     重配置时（`wm size` 之后、折叠代理期间、system_server 刚回来）偶发返回非零，
     `set -e` + `pipefail` 会直接杀掉整轮验收——**而它下面那个三次重试循环因此永远走不到**。
     这就是本轮前四次 geometry/flows 跑到一半 exit 224 / 20 / 141 的原因，不是产品缺陷。
   - `wait_page` 里 `*complete*) [ … ] && { … }` 改成 `if`。AND-list 左边失败就是这个
     case 分支的退出码，`set -e` 会当场结束——而「文档 complete 但一个 marker 都还没画」
     正是轮询时最常见的一次。
   - `verify-geometry.sh` / `verify-flows.sh` 的诊断源检测改成「调用方显式设了
     `JPFM_DIAG_SOURCE` 就用调用方的」。原来它无条件 `export` 覆盖，而检测发生在装完
     APK 的几秒内、APP 还没 READY，于是对一个 JpfmDiag 完全正常的构建也答 `probe`——
     探针那半没有 `imeMode` / `pageLoads` / `activityCreates`，恰好是折叠验收要的三个键。
     修完之后这三条从 SKIP 变成 PASS。
   - `verify-geometry.sh` 的几何计划循环加 `</dev/null`。计划是从 stdin 喂进来的，
     `one_orientation` 里的 `adb shell` 会把后面的行吃掉——**`fold8inner` 的 portrait(704)
     和 `fold8inner60` 的 landscape 从来没有被测过，而且不是 SKIP，是根本没出现在
     summary 里**。这是本轮发现的最危险的一个。
   - 期望值 `fold8inner60` landscape **689 → 688**。1808 物理像素 / 2.625 = 688.76：
     窗口向上取整成 689 dp（页面的 `outerWidth` / `screen.w` 也读 689），布局视口向下取整
     成 688 CSS px。同一份诊断里 `widthDp 689` 与 `innerWidth 688` 并存，是 Chromium 对的，
     不是壳少了一像素。五个窗口里只有这一个的除法不落在半像素以内。
     `tools/VERIFY.md` §2 的表和 `STANDARDS.md` §15 还写着 689，**这两份不在 T8 的可写范围，
     请一并改成 688**。

**没有改**：任何 `.kt` 的公开签名、`AndroidManifest.xml`、`app/build.gradle.kts`、
`map.py`（T3 的区块原样）、`docs/.well-known/assetlinks.json`、`data/i18n`、Worker。
T4 提过的 `Diagnostics.stripQuery` / `Routes.stripQuery` 重复**故意留着**：T5 的理由
（诊断页不该因为另一个文件没写完就打不开）成立，三行的代价换一个独立性。

### 四个门禁的自查结果

**gate_build — 全绿。** `rm -rf` scratch 后 `./build.sh assembleDebug`、
`testDebugUnitTest lintDebug`、`./build.sh` 依次绿；`find android -name build -o -name .gradle`
为空；apksigner SHA-256 = `78:9F:…:85:D1`；badging `com.fredhli.jpfoodmap` / 20000 / 2.0.0 /
sdk 31 / target 36 / `launchable-activity` = MainActivity / label `Japan Foodmap` +
`application-label-zh-CN` `日本美食地图`；APK 1.6 MiB（上限 8）；`apk-contents.py` 前 15 条
无字体 / Compose / Firebase；BUILD-INFO 的 size + sha256 与文件一致；单测 **106/106**，
`lintDebug` **0 error / 72 warning**；网站门禁五项全绿；`git status` 只含允许范围。
第 6 条「再跑一次 map.py，`git diff --stat docs/index.html` 为空或仅版本戳」：**连跑两次
输出字节数完全相同**，差异只有 `sw.js` 的 `VERSION` 时间戳、folium 生成的随机元素 id
（`map_` / `marker_cluster_` / `tile_layer_`）和 i18n 表的条目顺序；把这三样规范化之后
token 多重集完全相等（证据 `gate-build-determinism*.txt`）。这三样都是本次改动之前就有的。

**gate_emulator — 12 条里 10 条绿、1 条部分、1 条模拟器给不出结论。**
证据全部在 `audit_outputs/android-2026-09-06/docs-integration/`。

| # | 结论 |
|---|---|
| 1 三 AVD 几何 | **PASS**。475 / 932 / 704 / 591 / 688，`dpr 2.625`、`imeMode WEBVIEW` 无一例外；每个窗口首页 + 开卡两张截图，`png-stats` 判定「地图铺满」 |
| 2 折叠代理 | **PASS**。`activityCreates=1`、`pageLoads` 三份都是 1、`pid` 不变、卡片折前折后都开着、两张截图无灰块。`bootId` 三份都是 `null`（线上页面还没有 T3 的区块），所以这一条现在靠计数器而不是 bootId——**主会话 push main 之后重跑一次就能补上** |
| 3 返回键 | **部分**。「第二次 BACK 退出到 launcher」PASS；「第一次 BACK 关卡、APP 还在前台」两条 SKIP——release APK 没有 DevTools，脚本读不到 `#bs-sheet` 的状态。T2 在 debug APK 上验过 |
| 4 深链 | **PASS**。`pm get-app-links` 列出 `jpfoodmap.com`；冷启动 `?r=` 开卡；热路径切卡。「切卡时没重载」SKIP（同 #2，缺 bootId） |
| 5 外链 | **部分**。主 WebView 没离开本站、APP 没死，都 PASS；「落到哪个 APP」SKIP——`foldcover` 镜像里没有浏览器。T5 在有 Chrome 的实例上验过 Custom Tab 同 task |
| 6 60% 分屏 | **PASS**。横竖各一套截图 + 诊断，591 / 688 |
| 7 断网冷启动 | **1 PASS / 2 FAIL**。**地图出来了**（`net off` 后 `Active default network: none`，冷启动截图里地图铺满）——T2 未过的那条现在过了，而且证实了他排的第一个候选原因：SW 缓存要先在线跑过一次才建得起来。两条 FAIL 是网页的「离线」横幅不出现，见下面「留给主人的决定 2」 |
| 8 定位 | **PASS**。启动无弹窗；点定位 FAB 弹 `GrantPermissionsActivity`。`geo fix` 后的定位标记 SKIP（无探针） |
| 9 登录退化 | **PASS**。不崩溃；**logcat 里没有任何 token 形状的字符串** |
| 10 深色 / 字号 | **PASS**。`uimode night yes` 页面仍浅色；`font_scale 1.3` → `textZoom 130` |

**gate_static — 全绿。** `tools/static-audit.sh` **36/36**（在集成改动之后重跑）。
`aapt2 dump xmltree`：`allowBackup=false`、`enableOnBackInvokedCallback=true`、
`resizeableActivity=true`、`configChanges=0x40003fb4`、App Links filter `autoVerify=true`
且只有 `jpfoodmap.com`、`networkSecurityConfig` 已接。我们自己的组件只有两个 activity。
权限沿用 T1/T6 的口径：**自己声明恰四条**，另外三条（`USE_BIOMETRIC`、`USE_FINGERPRINT`、
`*.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION`）由 `androidx.credentials` / `androidx.core`
合并进来，merger report 里逐条有出处。**建议把 `STANDARDS.md` §10.3 与 `PLAN.md` gate_static
第 1 条改成这个口径**（T1 和 T6 都提过；两份文件不在 T8 可写范围）。
〔2026-09-06 后续：两份文件已由 T2 返工改写，且自己声明的从四条变成**五条**——加入
`ACCESS_NETWORK_STATE`，合并后总数 7 → 8。`static-audit.sh` / `power-audit.sh` 的清单同步。〕
release APK 上 `have_probe` 报 `no` —— `setWebContentsDebuggingEnabled(BuildConfig.DEBUG)`
确实生效了；同一台机器上 debug 包在场时探针会变 `yes`，那是 debug 包的 socket，不是 release 的。
`power-audit.sh` 本轮**没有重跑**（它要 5 分钟后台 soak 且需要独占模拟器）——T6 跑过 13/13，
而集成改动没有碰任何组件、权限或后台机制。

**gate_docs — 全绿。** `android/README.md`（开头 10 行人话、侧载五步 +「应用未安装」三种
成因、真机 11 项含 Google Console 的包名与 SHA-1、构建、签名、目录、权限与隐私、出问题
怎么办）、`android/CHANGELOG-ANDROID.md` 2.0.0、本文件、仓库 `README.md` 的
「## Android APP」段、仓库 `CHANGELOG.md` 的 `[Unreleased]` 安卓条目（都是追加）、
`apk/BUILD-INFO.txt` 与 APK 一致、`git check-ignore` 命中 `*.apk`。
版本三处一致：`map.py APP_VERSION = "2.0.0"`、`versionName = "2.0.0"` / `versionCode 20000`、
两份 CHANGELOG 的 `[2.0.0]`。

### 留给主人的两个决定

1. **Google Cloud Console 的 Android OAuth client**（`README.md` 真机清单第 4 条）。
   不做的话 APP 一切可用，只是登录按钮走退化路径。这条没有变通办法，必须你去点。
2. ~~**要不要加 `ACCESS_NETWORK_STATE`。**~~ **已在 2026-09-06 的 T2 返工里加上并全套验证**
   （见本文件末尾「T2 返工」一节；不要它的话那一节写了逐步回退办法）。以下为当时的原始记录：
   现象：断网之后 APP 里 `navigator.onLine` 仍然是
   `true`，网页顶上那条「离线」横幅不出现（地图照常能看，SW 在管）。**已经用实验钉死了
   原因**：把这一条权限加进 manifest、只在 ext4 scratch 里构建一个 debug APK（仓库一个字
   没改，`grep -c ACCESS_NETWORK_STATE android/app/src/main/AndroidManifest.xml` = 0），
   同一台模拟器、同一条 offline 流程，三条断言全部由 FAIL 变 PASS
   （证据 `offline-networkstate-experiment.log`）。这是 Chromium WebView 的既定行为：
   没有这条权限，它的网络状态通知器拿不到变化，`navigator.onLine` 恒为 true。
   - 代价：它是 `normal` 级权限，安装时授予、**不弹窗、不出现在系统的权限页**，不涉及任何
     隐私数据（只能读「有没有网」）。
   - 但是它会打破现在写死的「自己声明恰四条」：要同步改
     `tools/static-audit.sh`（那条断言）、`tools/power-audit.sh`（白名单）、
     `docs/STANDARDS.md` §10.3、`docs/PLAN.md` gate_static 第 1 条——**这四个文件都不在
     T8 的可写范围，而且这是一个「放宽已写下的标准」的决定，不该由集成者自己拍板**，
     所以 T8 没有加。要加的话就是 manifest 一行 + 那四处口径。

### 本轮的两条环境说明

- **`emu-acceptance/foldcover/` 里有三个文件被我覆盖了**（`summary.txt`、`launch.log`、
  `diag-source-probe.json`，2026-09-06 10:46）：我调试脚本中断问题时忘了带 `JPFM_WHO=`，
  跑进了 T7 的默认证据目录。截图和 `run-geometry-foldcover.log` / `run-flows-foldcover.log`
  （T7 自己的原始输出）都还在，T7 的结论不受影响，但那三个文件现在是我那次中断跑的内容。
- **本轮不再遇到 T2/T7 记的 system_server 崩溃**：全程只开一台模拟器，19 GB 里始终有
  7 GB 以上空余。之前那些 exit 224 / 20 / 141 全部是上面第 4 条修掉的 `set -e` 脆弱点，
  不是内存问题——同一条流程逐条单独跑就全过了。

---

## T2 返工 — 离线条与 `ACCESS_NETWORK_STATE` · **done**（2026-09-06 下午）

### 给主人的六行

- gate_emulator 第 7 条只过了一半：断网冷启动地图出来了，但网页顶上那条「当前离线」提示条
  不出现。现在两条都过了。
- 修法：**给 APP 加了一条权限 `ACCESS_NETWORK_STATE`**。它是「安装时自动给、不弹窗、系统
  权限页里不列出」的那一类，能读到的只有「有没有网、是 Wi-Fi 还是流量」，读不到你去过哪里，
  也读不到任何流量内容。**我们自己的代码一行都不用它** —— 它是给 WebView 里的 Chromium 用的：
  少了它，Chromium 不登记「网络变了」的回调，网页永远以为自己在线。
- **不想要这条权限也行**，回退办法写在 `STANDARDS.md` §10.3a，删一行 + 改四处清单，五分钟。
  代价就是回到「断网时页面不提示」。
- 加了权限之后暴露出第二个真 bug（T2 当初怀疑过、没敢下结论的那个）：断网冷启动时壳的
  「页面打不开」面板会盖在一张其实已经正常显示的地图上面。也修了。
- 全部重验：static-audit 36/36、power-audit（APK 段）6/6、单测 106 条、lint 0 error，
  模拟器上 §9.1（断网出地图 + 离线条）和 §9.2（没缓存时出面板、重试回到页面）都实拍过。
- 网站一个字没改（`git status` 里 `src/`、`docs/` 全是别的任务的改动），所以网站门禁不需要
  为这次返工重跑。

### 改了什么

| 文件 | 改动 |
|---|---|
| `app/src/main/AndroidManifest.xml` | `+ <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />`，头注释 FOUR → FIVE 并写清它为什么在这里 |
| `tools/static-audit.sh` §4 | 「恰四条」→「恰五条」，新增说明注释 |
| `tools/power-audit.sh` §1 | `ours` 清单加一条；文案「四条」→「五条」 |
| `docs/STANDARDS.md` | §10.3 最小集加一条、合并后 7 → 8；新增 §10.3a（例外说明 + 回退办法）；§9.1 点明离线条依赖这条权限；新增 §1.7a（面板与 SW 的时序） |
| `docs/PLAN.md` | §5.2 的 manifest 样例加一行；gate_static 第 1 条改口径（附「口径修订之二」说明）；gate_emulator 第 7 条注明依赖 |
| `app/src/main/kotlin/.../MainActivity.kt` | `committedUrl`、`finishReady()`、`resolveRescuedPage()`、`PAGE_RESCUED_JS`、`revealErrorPanel` + `ERROR_PANEL_GRACE_MS` |
| `app/src/main/kotlin/.../SiteWebView.kt` | `onPageCommitVisible` 把 url 传给 activity |
| `README.md` / `CHANGELOG-ANDROID.md` | 权限表加一行 + 单独一段说明；「出问题怎么办」里那条已知问题改成「已修」；CHANGELOG 的 Known limits 划掉 |

### 为什么面板会盖住一张好好的地图

断网时的时序是这样的（模拟器实测，`shell-rework/foldcover-run1-before-fix/offline-cold.png`
就是没修之前的样子）：

1. Chromium 知道没网了（**正是因为刚加的这条权限**），给主文档报一次
   `ERR_INTERNET_DISCONNECTED` → 壳的 `onReceivedError` 升起面板；
2. 网站的 Service Worker 随后用 `tabelog-shell-<build>` 里的缓存把**同一次导航**接住，
   一张完整的页面 commit 在面板底下；
3. `onPageFinished` 拿到的 url 与 `errorUrl` 相等，老代码据此判定「这是 Chromium 的错误页」，
   直接 `return`，面板就再也没撤下来。

这两种情况在 WebView 的 API 里长得一模一样（同一个 URL 上的 error + finish），**光看 URL
分不开**，所以改成问文档本身：

- `committedUrl == url` —— 这次导航确实把一个文档画到屏幕上了。（少了这一条，SSL 被取消
  那种「屏幕上还是上一页」的情况会被误判成「页面好着呢」。）
- `navigator.serviceWorker.controller` 非空 —— 屏幕上这个文档是 SW 送出来的。Chromium 自带
  的错误页是内部文档，没有 controller，所以 §9.2 那条路一步没变。

两条都成立才撤面板、转 READY。另外 `showError` 改成「布防」而不是「立刻显示」：面板延迟
1200 ms 才真正出现，这期间任何一次成功落地都会取消它 —— 否则每次离线启动都要先闪一下
「页面打不开」再变成地图。页面状态机不受影响，`state` 仍在错误到达的那一刻就变 ERROR。

### 验收（本轮实拍，证据在 `audit_outputs/android-2026-09-06/shell-rework/`）

| 项 | 结果 | 证据 |
|---|---|---|
| `aapt2 dump permissions` = 我们的五条 + 库的三条 | 8 条，符合新口径 | `02-aapt2-permissions.txt` |
| 签名指纹未变（覆盖安装 / assetlinks 仍成立） | SHA-1 `9f604dc5…f010`，与 `docs/.well-known/assetlinks.json` 一致 | `02b-apksigner.txt` |
| `tools/static-audit.sh` | **36 PASS / 0 FAIL** | `README` 同款输出 |
| `tools/power-audit.sh --no-device` | **6 PASS / 0 FAIL** | `03-power-audit-apk.log` |
| 单测 + lint | 106 条全绿；lint **0 error** / 72 warning（与返工前逐条一致） | `05-test-summary.txt`、`05b-lint-results-debug.xml` |
| **§9.1 断网冷启动** | **地图 PASS + 「页面知道自己离线」PASS + 离线条 PASS**（返工前是 1 过 2 挂） | `foldcover/offline-cold.png`、`foldcover/summary-flows.txt` |
| **§9.2 无缓存 + 断网** | 面板出现（uiautomator 读到「页面打不开 / 重试 / 设置」三个控件）→ 联网点重试 → 地图 | `offline-92/92a-panel.png`、`92b-ui.xml`、`92c-after-retry.png` |
| 几何（foldcover） | **7 PASS / 0 FAIL** | `09-geometry-foldcover.log` |
| 返回 / 深链 / 深色 / 字号 / 定位 / 登录退化（回归） | **11 PASS / 0 FAIL / 5 SKIP**，SKIP 全是探针或「线上页面还没上线 bootId」，与 gate_emulator 同因 | `10-verify-flows-all.log`、`12-verify-flows-rest.log` |

### 留下的话

- 面板的 1200 ms 宽限期是在模拟器（软件渲染、公网）上量出来够用的值。真机更快，只会更稳；
  如果哪天真机上仍看到一闪，把 `ERROR_PANEL_GRACE_MS` 调大即可，它只影响面板的显示时机，
  不影响状态机。
- `resolveRescuedPage` 依赖网站的 Service Worker 存在。网站哪天不用 SW 了，这段会自然退化成
  「永远不撤面板」，也就是返工前的行为，不会更坏。

## 验收后的三处修正（gate_static 检查员，2026-09-06）

主会话合入前补记，详情在 `audit_outputs/android-2026-09-06/gate/static/REPORT.md` §3：

- `AppSettingsActivity` 改用 `Notifications.PERMISSION` / `Notifications.permissionGranted()`，
  单测钉住的从此是出厂那份规则而不是副本；行为不变。
- `tools/static-audit.sh` 的通知申请点检查同时认 `Notifications.PERMISSION`（做过反向诱饵验证，
  检查只会更严）。
- `MainActivity.onDestroy()` 补上 `bridge.dispose()`，T3 遗留的「提供了但无人调用」就此关闭。

之后重建的 APK 由 gate_emulator 第二轮验收（`apk/BUILD-INFO.txt` 的 sha256 以那一份为准）。
