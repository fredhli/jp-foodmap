# Japan Foodmap — the Android app

给主人的 10 行版本：

1. 这是把 [jpfoodmap.com](https://jpfoodmap.com) 包起来的安卓 APP，装在 Fold 8 上，图标叫
   **Japan Foodmap**（系统语言是中文时显示「日本美食地图」）。里面就是那个网站，收藏、
   筛选、登录同步全都还是网站那一套，数据一个字节都没有搬家。
2. **装法**：等 Dropbox 同步完 → 手机上打开 Dropbox APP → `proj_2026 / tabelog / android /
   apk / jpfoodmap.apk` → 点它 → 第一次会让你给 Dropbox 开「允许安装未知应用」→ 装上去。
   以后出新版就照样再装一次，直接覆盖，数据不丢（详见下面「装到手机上」）。
3. 比浏览器多的东西：没有地址栏、合上/展开手机页面不重载、别处点到 jpfoodmap 的链接直接
   进 APP、外部链接（Tabelog、Google 地图）走应用内浏览器再退回来、分享走系统面板。
4. **登录暂时不能用，要你先去 Google Cloud Console 建一个「Android」类型的 OAuth 客户端**
   （包名和证书指纹在下面「真机清单」第 4 条，照抄即可）。建好之前 APP 一切照常，只是点
   登录会提示「登录处理失败」并给一个「在浏览器中打开」的按钮。
5. 装完还有三件事要你在手机上点一下：One UI 的「在外屏继续使用应用」、卸载旧的 jpfoodmap
   PWA、确认「默认打开」里 jpfoodmap.com 已验证。同样在「真机清单」。
6. 通知：默认关着，本版本**没有**服务器推送，打开也只有设置页里那条测试通知。
7. 权限：网络、定位（只有你点地图上的定位按钮时才申请）、通知（只有你在设置页拨开关时才
   申请）。启动时什么都不问。
8. 出新版：`android/build.sh`，产物就是 `apk/jpfoodmap.apk`，Dropbox 自己同步过去；
   `apk/BUILD-INFO.txt` 里有版本号、大小和 sha256，可以拿来核对手机上下到的是不是新的。
9. 签名用的是和 dashboard 同一个 `~/.android/debug.keystore`。**那个文件丢了，这个 APP 和
   dashboard 就都升不了级了**（见「签名」）。
10. 出问题看最后一节「出问题怎么办」。所有验收证据在
    `audit_outputs/android-2026-09-06/`。

---

## 这是什么（多两句）

一个 Kotlin 写的 WebView 壳：一个 Activity、一个 WebView、里面是
`https://jpfoodmap.com/`。壳**不碰**页面的任何数据 —— 不读 localStorage、不管收藏、不管
语言、不管地图位置。收藏 / 弃用 / 书签 / 子收藏夹依旧由页面和 Cloudflare Worker 负责，
和你在电脑浏览器里用的是同一份云端状态。

壳提供浏览器给不了的五件事：

| | 浏览器里 | APP 里 |
|---|---|---|
| 折叠 / 展开 | Chrome 会重排，有时整页重载 | Activity 不重建、页面不重载，开着的餐厅卡还开着 |
| 别处的 jpfoodmap 链接 | 落在 Chrome 的某个标签页 | App Links 直接进 APP，落在已经开着的地图上 |
| 登录 | 网页 Google 登录 | Google **不允许**在 WebView 里跑网页登录，所以改成系统级的 Google 登录（Credential Manager），拿到的凭据交给页面，页面照旧换它自己的 90 天 cookie |
| 外链 | 新标签页，回不来 | 应用内浏览器（Custom Tab）盖在上面，返回箭头一下回到地图 |
| 分享 | 复制链接 | 系统分享面板 |

版本与网站同步：**2.2.0**（`versionCode 20200`）。网站发版，APP 跟着发。

---

## 装到手机上（不用数据线，不用 adb）

1. **等 Dropbox 同步完** `apk/jpfoodmap.apk`。在手机的 Dropbox APP 里，文件还在下载时会
   转圈；文件大小应该和 `apk/BUILD-INFO.txt` 里写的一致。
2. 手机上打开 **Dropbox APP** → `proj_2026` → `tabelog` → `android` → `apk` → 点
   **jpfoodmap.apk**。
3. 安卓会说*「出于安全考虑，你的手机不允许安装来自此来源的未知应用」*。点**设置** →
   给 **Dropbox** 打开**允许来自此来源** → 返回。这是一次性授权，而且你授权的是 Dropbox，
   不是这个 APP。
4. 点**安装** → **打开**。第一次进去是完整的地图，和网站一模一样（第一次要联网加载）。
5. 以后出新版：同样的路径再装一遍，直接**覆盖安装**，收藏、登录状态、设置全都在。

**装不上，说「应用未安装 / App not installed」怎么办**

- 最常见：**Dropbox 还没下载完**。回 Dropbox 里看文件大小对不对得上 `BUILD-INFO.txt`，
  等它下完再点。
- 其次：**上一版是用另一把签名钥匙签的**。安卓不允许换证书升级。先「设置 → 应用 →
  Japan Foodmap → 卸载」，再装新的（这会清掉 APP 本地的三个设置项；收藏在云端和页面
  自己的存储里，重新登录就回来了）。
- 如果你同时装过 **debug 版**（`com.fredhli.jpfoodmap.debug`，图标一样但是是另一个 APP），
  它和正式版互不干扰，也不会互相覆盖 —— 想清爽就把 debug 版卸了。

---

## 真机清单（装好之后照着点一遍）

1. **One UI「在外屏继续使用应用」** —— `设置 → 显示 → 在外屏继续使用应用`，把
   **Japan Foodmap** 打开。不开的话合上手机会回到锁屏，而不是把地图接到外屏。
2. **卸载旧的 jpfoodmap PWA** —— 如果以前在 Chrome 里点过「安装应用」，桌面上那个
   jpfoodmap 图标是 PWA，会和 APP 抢 `jpfoodmap.com` 的链接。长按它 → 卸载。
   （网站本身照常能在 Chrome 里打开，这只是去掉那个「已安装的网页应用」身份。）
3. **默认打开** —— 网站上线带 `assetlinks.json` 的版本之后（主会话 push main 那一次），
   到 `设置 → 应用 → Japan Foodmap → 默认打开`，应该看到 **jpfoodmap.com** 已验证。
   没有的话点「添加链接」手动勾上，或者断网重连再等几分钟（安卓的验证是后台跑的）。
4. **Google Cloud Console 建 Android OAuth 客户端**（登录能用的前提，只需做一次）：
   - 打开 Google Cloud Console，进**和网站 Web client 同一个项目**
     （Web client id `536198170238-me7dpu2og75tseuekl3pu8rjjgo2ig2p...`）。
   - 凭据 → 创建凭据 → OAuth 客户端 ID → 应用类型 **Android**。
   - **包名**：`com.fredhli.jpfoodmap`
   - **SHA-1 证书指纹**：`9F:60:4D:C5:02:C4:10:94:ED:CF:D6:0F:53:E1:8B:50:0B:03:F0:10`
   - 想让 debug 版也能登录，再建一个同样 SHA-1、包名 `com.fredhli.jpfoodmap.debug` 的。
   - **不需要改 Web client，不需要改 Worker。** APP 拿到的 id_token 的 `aud` 仍然是那个
     Web client id，服务端一个字都不用动。
   - 建好之前 APP 完全可用（本地模式）：点登录会显示「登录处理失败」，并弹一个
     「在浏览器中打开」的按钮，用浏览器登录一次也照样能同步。
5. **登录**（第 4 步生效后）：头像菜单 → Google 登录 → 选账号 → 头像出现 → 杀掉 APP
   再开，应该仍然是登录状态（90 天）。
6. **折叠连贯**：开一张餐厅卡 → 合上手机 → 外屏上卡还在、地图没有变灰重画 → 再展开 →
   还是同一张卡、同一个地图位置。（这是这个 APP 存在的主要理由，一定要试。）
7. **60% 分屏**：从最近任务里把 Japan Foodmap 拖成分屏，占 60% 左右 → 页面不该出现横向
   滚动条，右下角的 FAB 按钮组要还看得见。
8. **深链**：给自己发一条 `https://jpfoodmap.com/?r=igfzg` 这样的分享链接（页面里每张卡的
   分享按钮生成的就是这种），点它 → 应该直接在 APP 里打开那家店的卡片，而不是 Chrome。
   APP 已经开着的时候再点另一条 → 卡片切换，地图不重载。
9. **外链**：餐厅卡里的「Tabelog ↗」→ 应用内浏览器盖上来 → 左上角返回箭头 → 回到地图，
   最近任务里**不**应该多出第二张卡片。「Google 地图」链接如果手机装了 Google 地图 APP，
   会直接进那个 APP。
10. **定位**：启动时**不该**有任何权限弹窗；点地图上的定位按钮才弹，允许之后地图飞到你
    的位置。
11. **通知**（可选）：设置页拨开「允许通知」→ 系统弹窗 → 允许 → 「发送测试通知」→
    通知栏出现一条 → 点它回到地图。不想要就别开，默认是关的。

打不开设置页：长按桌面图标 → **设置**；或者在 APP 里点头像菜单里的 **App settings**。

---

## APP 里有什么

- **地图本身** —— 就是网站。搜索、筛选、收藏、子收藏夹、语言切换全都在页面里，和电脑上
  一样。
- **返回键**：**你自己点开的**浮层（餐厅卡、大图、抽屉、筛选）返回一层关一层，栈空了才
  退出 APP。（页面用 `history.pushState` 管浮层，壳只是把系统返回键接过去。）
  一个例外，和 Chrome 一样：从别处点开一条 `?r=` 分享链接时，卡片是**页面自己**弹出来的，
  不是你点出来的 —— Chromium 会把这种「没有用户手势的 pushState」下面那条历史标成不可回退
  （history-manipulation intervention），所以这时第一次返回是直接退出 APP，卡片不会先关。
  在 Chrome 里新开一个标签页打开同一条链接，返回键也是同样的表现。
- **设置页**（长按图标 → 设置，或头像菜单 → App settings）：
  - **外部链接的打开方式** —— 应用内浏览器（Custom Tab，默认）/ Chrome / 系统默认浏览器。
    只影响离开本站的链接；手机上装了 Google 地图或 Tabelog APP 的话，对应链接仍会直接
    交给它们。
  - **文字大小** —— 跟随系统 / 90 / 95 / 100 / 115 / 130 %。只放大文字，版面重排。
    「跟随系统」用的是手机「显示 → 字体大小」，网页默认不跟随它，这里由壳乘进去。
  - **通知** —— 开关 + 「发送测试通知」。默认关。
  - **在浏览器中打开本站** —— 需要网页版登录、或者想用 Chrome 的功能时的后路。
  - **诊断…** —— 一屏数字：窗口宽高、`env()` 安全区、字号、WebView 版本、页面加载次数、
    Activity 创建次数、上次退出原因。可以一键复制。出问题时把它复制给我。
  - **关于** —— 版本号 + 系统 WebView 版本。

---

## 权限与隐私

APP 自己声明五个权限，**没有一个会向你弹窗要「同意」，除了定位和通知那两条，而那两条只在
你亲手点的时候才问**：

| 权限 | 什么时候要 | 不给会怎样 |
|---|---|---|
| `INTERNET` | 一直 | 装不上也没意义 |
| `ACCESS_NETWORK_STATE` | 一直（安装时自动给，不弹窗） | 断网时网页不知道自己断网了，顶上那条「当前离线」横幅不出现 |
| `ACCESS_COARSE_LOCATION` / `ACCESS_FINE_LOCATION` | 只有你点地图上的定位按钮时 | 定位按钮不工作，别的照常 |
| `POST_NOTIFICATIONS` | 只有你在设置页拨开通知开关时 | 通知发不出，别的照常 |

`ACCESS_NETWORK_STATE` 值得单独说一句，因为它是唯一一条 **APP 自己一行代码都不用** 的权限：
它是给 WebView 里的 Chromium 用的。少了它，Chromium 不去登记「网络变了」的回调，网页问
`navigator.onLine` 永远得到「在线」，于是网站自带的离线提示条永远不出现。它是 `normal` 级
权限——安装时自动授予、不弹窗、在系统的「权限」页里根本不列出来，能读到的只有「有没有网、
是 Wi-Fi 还是流量」，读不到你去过哪里、也读不到任何流量内容。
（不想要它也行：删掉 manifest 里那一行即可，代价就是断网时页面不提示，办法写在
`docs/STANDARDS.md` §10.3a。）

`aapt2 dump permissions` 在这五条之外还会多列三条，是依赖库合并进来的，不是我们声明的，
也都不会向你弹窗：

- `USE_BIOMETRIC` / `USE_FINGERPRINT` ← `androidx.credentials` 依赖的 `androidx.biometric`
  （原生 Google 登录必需）。两条都是 normal 级，安装时授予，系统的权限页里看不到。
- `com.fredhli.jpfoodmap.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION` ← `androidx.core`
  自定义的 signature 级权限，只有本 APP 自己能用。

其它：

- 壳**不读写**页面的任何存储：没有 localStorage 访问、没有收藏、没有 token 落盘。
  登录拿到的 id_token 从 Credential Manager 直接进一条桥消息交给页面，不记日志、不存盘、
  不二次引用。壳往页面里写的东西只有一个 CSS 变量 `--app-inset-top`（2.1.0 起，就是状态栏
  高度换算成 CSS px），页面拿它和自己的 `env(safe-area-inset-top)` 取大的那个来留白，
  免得顶栏被状态栏压住。
- 壳自己只存三个偏好（外链策略、文字大小、通知开关），在 SharedPreferences 里，
  `allowBackup=false`，不进云备份。
- `cleartextTrafficPermitted="false"`，没有自定义信任锚；WebView 关掉了文件访问、
  内容访问和自动开窗，混合内容一律拒绝；页面与壳之间只有一条 `addWebMessageListener`
  通道，且只对 `https://jpfoodmap.com` 生效（**没有** `addJavascriptInterface`）。
- 没有后台服务、没有定时任务、没有 wakelock、没有 FCM。APP 关掉就是真的不跑了。
- 诊断输出里的 URL 会去掉 query，不含 cookie、不含 token。

---

## 构建

```bash
cd /mnt/d/Dropbox/proj_2026/tabelog/android
./build.sh                      # assembleRelease + 发布 apk/jpfoodmap.apk + 写 BUILD-INFO.txt
./build.sh assembleDebug        # 不压缩的调试版，给模拟器用，不发布
./build.sh testDebugUnitTest lintDebug   # 任意 Gradle 任务，都在 scratch 里跑
```

`build.sh` 会先把源码 rsync 到 ext4 的 scratch（`$HOME/.cache/jpfoodmap-android`）再让
Gradle 在那里跑，只把成品 APK 拷回来 —— **Gradle 从不在 `/mnt/d` 上跑**（9p 太慢、文件
监听不可靠，而且 Dropbox 会同步每一个中间产物）。改代码在这边，构建在那边。

几个人同时构建时各给一份 scratch，并把发布串起来：

```bash
JPFM_ANDROID_BUILD=$HOME/.cache/jpfoodmap-android-x ./build.sh assembleDebug
flock /tmp/jpfoodmap-android-build.lock ./build.sh
```

工具链（本机 `~/tools/android-env.sh` 里已经装好）：JDK 17 / Gradle 8.14.5 / AGP 8.11.1 /
Kotlin 2.2.21 / compileSdk = targetSdk 36 / minSdk 31。**这些版本号不要动** —— 它们是
一起验过的一组。

模拟器验收（三种屏幕几何 + 九条行为流程）：见 `tools/VERIFY.md`，两条命令
（`tools/verify-geometry.sh`、`tools/verify-flows.sh`）出全部证据。

### 签名

release 版用 `~/.android/debug.keystore`（alias `androiddebugkey`，storepass `android`）
签名 —— 和 dashboard 那个 APP 是**同一把钥匙**。这是故意的：只侧载不上架，证书不变才能
覆盖安装，而且 App Links 的 `assetlinks.json` 用的就是这个指纹。

```
SHA-1    9F:60:4D:C5:02:C4:10:94:ED:CF:D6:0F:53:E1:8B:50:0B:03:F0:10
SHA-256  78:9F:E3:5F:02:40:43:2A:CF:C7:E1:71:50:1B:94:1C:29:B9:91:55:D3:58:CF:33:9C:78:AE:C2:10:16:85:D1
```

**这个文件丢了会静默出事**：Gradle 找不到它会自己新生成一把，构建照样成功、APK 照样发布、
Dropbox 照样同步，然后手机说「应用未安装」，而且哪里都没有报错可看。同时 dashboard 也
一起废掉，两个 APP 的 `assetlinks.json` 都会变成假话。备份方法见
`proj_2026/dashboard/deploy/SIGNING-KEY.md`（主人自己做，agent 不碰这个文件）。

---

## 目录

```
android/
├── README.md                  这份
├── CHANGELOG-ANDROID.md       APK 的版本记录
├── build.sh                   rsync 到 ext4 scratch → gradle → 发布 apk/ + BUILD-INFO
├── apk/
│   ├── jpfoodmap.apk          构建产物（.gitignore 排除；Dropbox 带到手机）
│   └── BUILD-INFO.txt         版本 / 大小 / sha256 的戳，**这个进 git**
├── docs/
│   ├── PLAN.md                实施计划（任务划分、逐字契约、决策记录）
│   ├── STANDARDS.md           每条标准 + 它的验收方法
│   └── STATUS.md              每个任务的实际状态、偏离、真机待验项
├── tools/
│   ├── env.sh                 JPFM_ANDROID_SRC / JPFM_ANDROID_BUILD + 机器工具链
│   ├── emu.sh                 模拟器驱动：start/stop/install/launch/shot/rotate/diag/fold/net
│   ├── verify-geometry.sh     三种几何 + 折叠代理的验收
│   ├── verify-flows.sh        九条行为流程的验收
│   ├── diag.sh  diagjson.py  wv-eval.py  png-stats.py   读数与断言
│   ├── lib-verify.sh          两个 verify-*.sh 的共用底座（PASS/FAIL/SKIP、重试、截图）
│   ├── applinks-check.sh      指纹 vs assetlinks.json，pm get-app-links
│   ├── static-audit.sh        36 项静态加固检查
│   ├── power-audit.sh         13 项耗电检查（含 5 分钟后台 soak）
│   ├── apk-contents.py        APK 体积构成
│   ├── gen-launcher-icon.py   从网站图标生成各密度 mipmap
│   └── VERIFY.md              检查员手册（不看别的文件就能跑完验收）
└── app/src/main/kotlin/com/fredhli/jpfoodmap/
    ├── MainActivity.kt        生命周期、页面状态机、insets、返回键、错误面板、定位、诊断
    ├── SiteWebView.kt         WebView 工厂 + WebViewClient / ChromeClient
    ├── Bridge.kt              页面 ↔ 壳的唯一通道（window.Native）
    ├── GoogleSignIn.kt        Credential Manager 封装
    ├── Routes.kt DeepLinks.kt 本域判定、?r= 解析、深链冷/热路径
    ├── Links.kt               外链阶梯（Custom Tab / Chrome / 系统）+ 分享
    ├── ShellPrefs.kt          三个偏好
    ├── AppSettingsActivity.kt 设置页
    ├── Diagnostics.kt         诊断的两半
    ├── Notifications.kt       一个渠道 + 一条测试通知
    ├── Insets.kt PopupCatcher.kt
    └── ../../test/…           JVM 单测（106 条）
```

仓库里和 APP 相关、但不在 `android/` 下的只有两处：

- `docs/.well-known/assetlinks.json` —— App Links 的验证文件，跟着网站一起部署。
- `src/tabelog/scrape/map.py` 里一个 `// ===== APP BRIDGE (Android shell) =====` 区块 ——
  页面在 APP 内的行为（隐藏 PWA 安装入口、改用原生登录按钮、分享走系统面板、菜单里多一行
  App settings）。浏览器里这段第三行就 return，行为零变化。

---

## 出问题怎么办

**打开是白屏 / 一直转圈。** 先看有没有网。壳有 10 秒看门狗，超时会显示自己的错误面板，
上面有「重试」。连着白屏就到设置页 → 诊断，把内容复制出来。

**显示「无法加载」的错误面板。** 面板上的「重试」重新加载；「设置」进设置页。**从没联网
用过就断网冷启动**，一定会看到它 —— 网站的离线缓存要联网跑过一次才建得起来。联过一次网
之后再断网冷启动，地图会照常出来（模拟器上已验，见 `docs/STATUS.md` T8 段）。

**断网了，但网页顶上那条「离线」横幅不出现。** 2026-09-06 已修：加了 `ACCESS_NETWORK_STATE`
权限，见上面「权限与隐私」。如果你装的是更早的 APK，那时的表现是「地图照常出来、只是不提示
断网」，升级即可。

**合上手机回到锁屏，没接到外屏。** One UI 的「在外屏继续使用应用」没给这个 APP 开，
见真机清单第 1 条。

**jpfoodmap 的链接还是在 Chrome 里开。** 三种可能：网站还没发布带 `assetlinks.json` 的
版本；旧的 PWA 还装着（真机清单第 2 条）；「默认打开」里没验证（第 3 条）。

**点登录说「登录处理失败」。** 正常 —— Google Cloud Console 的 Android OAuth 客户端还
没建（真机清单第 4 条）。同一个弹窗里的「在浏览器中打开」可以先用浏览器登录。
建好之后如果还失败，多半是包名或 SHA-1 抄错了一个字符。

**手机上的收藏和电脑上不一样。** 那是网站的同步，不是 APP 的问题 —— 两边登的是不是同一个
Google 账号？没登录的话状态只在本机。参见仓库根的 `CLAUDE.md`「Sync」。

**文字太小 / 太大。** 设置页 → 文字大小。「跟随系统」跟的是手机的字体大小设置。

**想确认手机上装的是不是最新的。** 设置页最下面一行有版本号和 WebView 版本；
`apk/BUILD-INFO.txt` 里有同一个版本号、文件大小和 sha256。

**要给我看现场。** 设置页 → 诊断… → 复制，粘给我。里面没有 cookie、没有 token、URL 的
query 也剪掉了。
