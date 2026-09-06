# 模拟器验收手册（VERIFY）

> 给检查员的 10 行版本：这份文件是「不看别的文件也能跑完 gate_emulator」的操作手册。两条命令
> 出全部证据：`tools/verify-geometry.sh` 量三种屏幕几何（外屏 / 内屏 / 60% 分屏）＋折叠代理，
> `tools/verify-flows.sh` 走九条行为流程（返回键、深链、外链、分享、断网、定位、深色、字号、
> 登录退化）。每条断言只有三种结果：**PASS**（对）、**FAIL**（错，要修）、**SKIP**（这个
> 版本还没这功能，或模拟器验不了）。**只有 FAIL 计入退出码**，所以一堆 SKIP 不代表失败——
> 骨架 APK 上本来就该是一堆 SKIP。证据全部落在
> `audit_outputs/android-2026-09-06/<who>/<avd>/`：`summary.txt` 是逐条结论，`*.png` 是截图，
> `*.json` 是那一刻的诊断读数。跑完先读 summary.txt 的最后一行（PASS/FAIL/SKIP 计数），
> 再看有没有 FAIL，最后翻截图。模拟器验不了的东西（真折叠、Google 登录、真实 inset、App
> Links 自动验证）见本文末尾「只能上真机」。

---

## 0. 前置

```bash
source ~/tools/android-env.sh          # JDK 17 + Android SDK + Gradle（脚本自己也会 source）
ls /dev/kvm                            # 必须存在且可写，否则模拟器慢到没法用
avdmanager list avd -c                 # 需要 foldcover / fold8inner / fold8inner60
free -g                                # 每台模拟器 2–3 GB；**同时最多 3 台**
```

- **同时最多 3 台模拟器**（`docs/PLAN.md` §10）。本机 19 GB，超过 3 台时 `system_server`
  会被内存压力打死，症状是 `adb install` 报 `cmd: Can't find service: package`、APP 启动后
  一秒退回桌面。脚本对这两种情况都会重试并在 `launch.log` 里记一行，但连续发生时
  **先关掉别人的模拟器再重跑**，不要把它当产品缺陷。
- 端口：本任务用 5554（读写，默认）。其它任务用 5556/5558/… 且必须
  `JPFM_EMU_READONLY=1`。
- APK：默认量 `android/apk/jpfoodmap.apk`（release，gate_emulator 要求的那个）。
  **debug APK 能多验一半东西**（见 §3 的「探针」），所以两个都跑一遍最省事：

```bash
JPFM_APK=$HOME/.cache/jpfoodmap-android/app/build/outputs/apk/debug/app-debug.apk \
  tools/verify-geometry.sh
```

---

## 1. 快速开始

```bash
cd /mnt/d/Dropbox/proj_2026/tabelog/android

tools/verify-geometry.sh                     # 三个 AVD，全套几何 + 折叠代理（约 8–12 分钟）
tools/verify-flows.sh                        # foldcover 上九条流程（约 6–10 分钟）
tools/verify-flows.sh back deeplink offline  # 只跑指定流程

JPFM_KEEP=1 tools/verify-geometry.sh foldcover   # 跑完不关模拟器，方便手动接着看
```

**跑验收请显式给 `JPFM_DIAG_SOURCE=native`。** 不给的话自动探测在装完 APK 几秒后就跑，
那时 APP 还没起来过，探测必然落到 `probe`（WebView DevTools，只有 debug 包有），而 probe
读不到 `imeMode` / `pageLoads` / `activityCreates` —— 恰好是几何、折叠、热深链三条最要紧的
断言要的键。

```bash
JPFM_WHO=gate/emulator JPFM_DIAG_SOURCE=native tools/verify-flows.sh deeplink
```

热深链那一条（`?r=` 到了、页面已经开着 → 只换卡不重载，STANDARDS §6.3）有两个见证：
壳自己数的 `pageLoads`（release 包也有，不需要页面配合）和页面自己的 `window.__jpfmBootId`
（要网站上线带 app-bridge 的版本之后才有）。网站上线前，`pageLoads` 会如实报 `1 -> 2`
并 FAIL —— 那是线上页面还没有 `__jpfmOpenShare` 钩子、壳按设计降级成 `loadUrl`，不是壳的
缺陷；push main 之后重跑同一条命令即应转绿（`pageLoads 1 -> 1`，bootId 那条从 SKIP 变 PASS）。

退出码 = FAIL 条数（0 = 没有 FAIL）。证据目录：

```
audit_outputs/android-2026-09-06/<who>/<avd>/
├── summary.txt              geometry 的逐条 PASS/FAIL/SKIP + 末尾计数
├── summary-flows.txt        flows 的同上（两个脚本各写各的，不互相覆盖）
├── <orient>-home.png        首页
├── <orient>-card.png        打开一张餐厅卡
├── <orient>-filter.png      打开筛选面板
├── <orient>-home.json       那一刻的诊断读数（几何、env()、bootId…）
├── fold-{a-inner,b-cover,c-inner}.json   折叠代理前 / 折中 / 折回
├── fold-{b-cover,c-inner}.png
├── launch.log  diag.log  png-stats.log    脚本自己的原始输出
└── app-links.txt  external-activities.txt （flows 才有）
```

`<who>` 默认 `emu-acceptance`，用 `JPFM_WHO=gate-emulator` 换成自己的目录，别覆盖别人的证据。

---

## 2. 期望值表（`docs/STANDARDS.md` §15 的实测口径）

| AVD | 方向 | 物理像素 | dp / CSS px | `innerWidth` | 备注 |
|---|---|---|---|---|---|
| `foldcover` | natural | 1248×1972 @420 | 475×751 | **475** | 外屏，默认 |
| `fold8inner` | landscape（自然） | 2446×1848 @420 | 932×704 | **932** | 内屏展开 |
| `fold8inner` | portrait | 1848×2446 | 704×932 | **704** | `emu.sh rotate portrait` |
| `fold8inner` | 折叠代理 cover | 1248×1972 | 475×751 | **475** | `emu.sh fold cover` |
| `fold8inner60` | portrait（自然） | 1552×1808 @420 | 591×689 dp | **591** | 60% 分屏窗口 |
| `fold8inner60` | landscape | 1808×1552 | 689×591 dp | **688** | `emu.sh rotate landscape`；见下方注 |

> 注：`fold8inner60` 横向是五个窗口里唯一 dp ≠ CSS px 的一个。1808 物理像素 / 2.625 =
> 688.76：窗口向上取整成 **689 dp**（诊断里的 `widthDp`、页面的 `outerWidth` / `screen.w`
> 都读 689），布局视口向下取整成 **688 CSS px**（`innerWidth`）。同一份 JSON 里两个数并存
> 是 Chromium 对的，不是壳少了一像素。`verify-geometry.sh` 断言的是 688。

其它三条到处一样：

| 键 | 期望 | 来源 |
|---|---|---|
| `dpr` | `2.625` | 420 dpi / 160 |
| `imeMode` | `WEBVIEW` | STANDARDS §2.5；壳没实现诊断时是 SKIP |
| `env.t` / `env.b` | 模拟器上 **24 / 24**；真机 42/15（外屏）、40/15（内屏）、40/0（分屏） | STANDARDS §1.2。**别拿 fold8inner60 的 inset 当真机数**：那是整块屏，系统条是模拟器自己的 |

折叠代理（`fold8inner`，STANDARDS §3.1–3.3）三份 JSON 必须满足：

| 键 | 期望 |
|---|---|
| `activityCreates` | 三份都是 `1`（Activity 没重建） |
| `pageLoads` | 三份相同（页面没重载） |
| `bootId` | 三份相同（同一个 document） |
| `pid` | 三份相同（进程没死） |
| 卡片 | 折前开着，折后还开着 |
| 截图 | `fold-b-cover.png` / `fold-c-inner.png` 里地图铺满，无灰块 |

---

## 3. 三个脚本各做什么

### `tools/emu.sh` —— 设备驱动

```bash
tools/emu.sh start|stop|status|wait
tools/emu.sh install [apk]         # 默认 scratch 里的 debug APK
tools/emu.sh launch [url]          # am start -W；带 url 就是 ACTION_VIEW（深链）
tools/emu.sh shot out.png
tools/emu.sh rotate landscape|portrait
tools/emu.sh diag out.json         # am start --ez diagnostics_log true → 抓 JpfmDiag 一行
tools/emu.sh fold cover|inner      # 只在 fold8inner 上；wm size 1248x1972 / wm size reset
tools/emu.sh net on|off            # svc wifi + svc data
```

`diag` 的退出码是有意义的：**0** 拿到合法 JSON、**3** 这个版本没有诊断日志（骨架 APK 就是，
按 SKIP 处理）、**1** 打了一行但不是合法 JSON（真缺陷）。

`shot` 在本机镜像上会先试 `screencap`，失败一次后就记住改走模拟器控制台截图
（`~/.android/jpfm-shot-<port>.mode`）。`screencap` 在 android-37.0 镜像上必然
abort（`hasReadColorBufferDma`），一次没事，一轮验收二十次就会把 `system_server` 拖死——
所以别把这个记忆文件删了。`JPFM_SHOT_MODE=console|screencap|auto` 可以强制。

### `tools/diag.sh` —— 读数与断言

```bash
tools/diag.sh capture out.json     # 有原生诊断就用原生，没有就用探针
tools/diag.sh get out.json innerWidth
tools/diag.sh check out.json innerWidth=475 dpr=2.625 imeMode=WEBVIEW
tools/diag.sh page 'document.title' # 直接问页面一句 JS（调试时很好用）
```

**两个数据源，同一套键名**：

1. **原生**（`JpfmDiag` logcat 行）—— 唯一对 release APK 有效的来源，是真正要验的东西。
2. **探针**（`tools/wv-eval.py`，走 WebView 的 DevTools 端口）—— **只有 debug APK 有**
   （`setWebContentsDebuggingEnabled(BuildConfig.DEBUG)`）。它能问页面 DOM：卡片开没开、
   筛选面板开没开、离线条出没出、`navigator.onLine`、`bootId`。壳还没实现诊断的阶段，
   几何断言全靠它，所以**骨架 APK 上也能验出 475/932/704/591/688**。

探针不写 `localStorage`、不发网络请求；`bootId` 优先用页面的 `window.__jpfmBootId`（T3 的
app-bridge 区块上线后才有），没有就用探针自己种的一次性标记——两者都能证明「document 没被
重载」。用 `JPFM_DIAG_SOURCE=native|probe` 可以固定来源（脚本自己会先探一次再固定，
否则每次 capture 都要白等一轮 logcat）。

### `tools/verify-geometry.sh` / `tools/verify-flows.sh`

见 §1。两个脚本共用 `tools/lib-verify.sh`（PASS/FAIL/SKIP、冷启动重试、截图、点击）。
`tools/png-stats.py` 做「地图有没有铺满」的像素粗检：一块没画出来的 Leaflet 区域是纯
`#ddd`，脚本卡的是 `#ddd` 占比 >15%、单色占比 >90%、或者整块只有 <200 种颜色。

---

## 4. 读结果：什么算真的坏了

| summary.txt 里的行 | 含义 | 该怎么办 |
|---|---|---|
| `FAIL innerWidth expected 932, got 933` | 壳没把 dp 宽度如实交给页面（或 AVD 被改过） | 真缺陷，查 `useWideViewPort` / viewport meta |
| `FAIL bootId changed across the fold` | 折叠让页面重载了 | 真缺陷，查 `configChanges` 是否完整 |
| `FAIL activityCreates moved` | Activity 被重建 | 真缺陷，同上 |
| `FAIL pageLoads unchanged across the hot deep link expected 1, got 2` | 第二条 `?r=` 深链把页面整个重载了 | 先看线上页面有没有 `window.__jpfmOpenShare`：没有就是网站还没 push，壳的降级分支是对的；有还这样才是缺陷（查 `MainActivity.pushTarget`） |
| `SKIP pageLoads across the hot deep link (the activity was recreated…)` | 两次读数之间进程/Activity 重启了，计数器全部归零 | 环境问题（本机常见），重跑 |
| `FAIL … flat region, looks unpainted` | 截图里地图是灰的 | 先看那张 PNG；确实灰就是 §3.3 缺陷 |
| `SKIP … (not reported by this build)` | 这个版本的诊断没这个键 | 正常，等对应任务实现 |
| `SKIP … (no deep-link path in this build)` | 壳还没接深链 | 正常（骨架/未接线阶段） |
| `SKIP … no browser on this image` | 镜像里没有 Chrome | 模拟器验不了，留给真机 |
| `install attempt 1 failed` / `after launch 1 the app is alive=false` | `system_server` 又死了 | 环境问题，见 §0；连着三次就先关别的模拟器 |

---

## 5. 手动补几刀（脚本没覆盖或想自己看时）

```bash
# 现在页面是什么状态
tools/diag.sh page 'JSON.stringify({w:innerWidth,dpr:devicePixelRatio,card:document.getElementById("bs-sheet").classList.contains("bs-open")})'

# 顶层窗口是谁（判断 Custom Tab / chooser / 权限弹窗）
adb -s emulator-5554 shell dumpsys window | grep mCurrentFocus

# 权限清单（STANDARDS §10.3 要求我们自己声明恰好五项 + 库合并进来的三项 = 8 条；
#   第五项 ACCESS_NETWORK_STATE 是离线条的前提，理由与回退办法见 STANDARDS §10.3a）
aapt2 dump permissions apk/jpfoodmap.apk

# App Links 状态
adb -s emulator-5554 shell pm get-app-links com.fredhli.jpfoodmap

# 字号
adb -s emulator-5554 shell settings put system font_scale 1.3   # 记得改回 1.0
```

---

## 6. 只能上真机（模拟器给不出结论，别在这里纠结）

- 真实折叠：显示屏切换、One UI「在外屏继续使用应用」开关。模拟器只有 `wm size` 代理。
- 真实 inset 数值（42/15、40/15、40/0）。模拟器是 24/24。
- Google 登录全流程、90 天静默续期：模拟器无 Google 账号，只能验退化路径。
- App Links 自动验证：要网站上线 `assetlinks.json` 之后，在手机「默认打开」里看。
- Custom Tab 真正落到 Chrome：镜像里没有 Chrome。
- One UI 分屏拖动过程中的连续重排。
- 抽屉里的图标与名称。
