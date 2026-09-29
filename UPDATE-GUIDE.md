# 餐厅更新与在线营业时间

本次代码只在本地实现与离线测试；没有启动正式采集、修改餐厅主库或部署网站/Worker。

## 统一主流程

完整规则和全部参数见 [SCRAPING-RULES.md](SCRAPING-RULES.md)。
普通main已经包含基础选集、五月的正餐补足和东京大阪特殊榜；不必另跑一次补足。

```bash
uv run python main.py aichi --dry-run
uv run python main.py aichi --no-build
uv run python main.py --all-regions --dry-run
```

普通模式会刷新旧店完整详情。经常性的分数更新请继续使用 `--bimonth-update`，
它调用相同选集，仅跳过有可靠榜单评分的旧店详情。

## 东京、大阪补爬

```bash
uv run python main.py --tokyo-osaka-append --dry-run
uv run python main.py --tokyo-osaka-append --no-build
```

新模式默认东京从原筛选榜第 10 页、大阪从提供的筛选榜第 8 页开始。
保留预算和正餐筛选规则；3.50 分包含在内，读到低于 3.50 的有效评分或第 60 页停止。
`score_floor` 和 `page_limit` 是两种不同结果；页数上限不表示已完整覆盖 3.50。
错误、异常空页、重复页和评分排序异常会报告未完成。
每次以给定起始页扫描并对已入库餐厅去重；不是根据混用的 `source_page` 自动续爬。
可使用 `--tokyo-list-url`、`--osaka-list-url`、`--tokyo-start-page`、`--osaka-start-page` 覆写。
URL 中的日期占位符在运行时替换，具体日期值保持不变。
旧 `--tokyo` 暂留为旧缺额模式；新的两地操作请用 `--tokyo-osaka-append`。

## 双月更新

```bash
uv run python main.py --bimonth-update --dry-run
uv run python main.py --bimonth-update osaka --no-build
uv run python main.py --bimonth-update --no-build
```

不指定地区时处理主库中的全部地区，指定多个地区则仅处理所列地区。
这是手动命令，没有创建定时任务。完整规则见 [SCRAPING-RULES.md](SCRAPING-RULES.md)。
默认基础选集参数为 `--top-pct 1 --hard-cap 500 --fine-dine-pct 0.1`，
随后按 `--main-meal-ratio 0.008 --main-meal-cap 300` 补足现行正餐；乌冬、荞麦、拉面不计入正餐补足。
数量阶段结束后，若地区餐厅集最低分仍高于3.50，会继续正餐补爬至包含3.50同分段或60页，
这层延伸可以超过300名额。最低分口径是整个保留餐厅集；原菜系和价格筛选不变。
任何榜单撞上60页都会打印 `[WARNING]` 和最后评分，并进入运行报告。
东京、大阪默认还会扫描已配置的特殊榜。基础榜达到配额后仍会利用页尾，并继续覆盖旧店评分。
所有榜单读到有效评分的旧店直接补分；地区末尾仅新候选及尚无有效评分的旧店访问详情。
日志会先打印详情访问计划，报告候选正餐数量、已提交数量和剩余缺额。
`--no-special-lists` 可明确跳过两地特殊榜；其他数值参数也可按需覆写。
确认低于 3.40、永久闭店或暂时休业的旧店退出活动 CSV；恰好 3.40 保留。
无评分、验证页、404 或网络失败不能证明停业，不触发删除。
退场完整记录保存在 `data/tabelog/bimonth_departures.jsonl`，运行记录在
`data/output/bimonth_runs/`。用户收藏和账号同步内容不改动。

先读运行报告，再在自己的环境里构建：

```bash
uv run python src/tabelog/scrape/map.py
uv run python scripts/verify_build.py
```

`main.py` 不加 `--no-build` 会在模式成功结束后构建地图；`--dry-run` 永不构建。
某地区失败后已确认的其他地区更新可能已经保存，但入口不会自动发布不完整结果。
只撞上60页上限（选集状态 `truncated`）不算失败，会列入运行结果的 `page_limited_regions`，照常自动构建。
请串行运行写入主库的脚本，不要同时开多个更新、补爬或地图构建任务。
正在运行的进程不会自动采用新代码，结束旧进程后再启动新命令；已经提交的地区数据会保留。

## 双月更新断点续跑

意外关机、重启或中断后，先查看恢复计划，再继续：

```powershell
uv run python main.py --bimonth-update --resume --dry-run
uv run python main.py --bimonth-update --resume --no-build
```

`--resume` 仅用于双月更新，自动识别最近可恢复的这一轮，跳过已完整提交的地区。
地区完成以报告的 `commit=complete`、且选集没有真正失败（状态不是 `partial`）为准；只因60页上限配额未满的 `truncated` 地区也算完成，旧报告里这类地区写的 `incomplete=true` 按新规则视为完成；报告内单独记录的待核实详情仍保留，不会仅因此重跑整个地区。
不加 `--resume` 仍是开始新一轮更新。恢复不会改变餐厅选择规则。
新版本在开始联网前记录地区顺序、选集参数和已经替换日期的特殊榜链接；
续跑沿用这些记录，明确指定的冲突参数会报错，避免把不同选集混在一轮中。
原运行的 `--no-build` 也会保留；本次旧版兼容恢复同样跳过定位和构建。最新一轮已完成时，`--resume` 不会退回更早的废弃运行。

新版本会将已通过校验的列表页和可用的详情结果逐项保存到本轮运行目录。
恢复时从本地结果重建选集，再访问尚未完成的页面/餐厅；看到选集计算从第一页重新输出，
不表示重新请求这些已缓存的页面。未完成地区尚未保存的请求可能需要重试。
地区主库提交与退场归档也有恢复检查，避免在断电后的提交窗口漏归档或重复归档。
恢复检查只比较爬取数据，不比较 `map.py` 写回主库的经纬度（`lat`/`lon`）：
中断后先构建地图不会阻止续跑；评分、地址等其他字段被别的任务改过时仍会拒绝恢复。

本次旧版本中断没有逐页/逐店断点：可以根据已有地区报告跳过完成地区，
但未完成地区只能重新扫描。旧报告未记录的参数不能凭空还原：兼容恢复采用当前库内全部地区和默认选集参数，特殊链接日期按原运行目录日期还原；恢复计划会明确提示。
这与本次原命令 `--bimonth-update --no-build` 的地区及选集设置一致。
`--dry-run` 只读取进度，不启动浏览器、不写断点、不更新主库。
网络失败等导致的未完成地区会进入重试；只撞上60页上限的地区不会重试，恢复功能也不会绕过60页限制。

## Chrome 调试端口无法连接

本次 `--resume` 在启动浏览器时停止，还未请求餐厅榜单，已保存的地区进度不会消失。
浏览器启动错误会写入 `data/output/browser_startup/`，并在异常末尾显示Chrome启动进程的退出状态和错误日志。
可在项目的Windows PowerShell里运行以下**只读**诊断：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\diagnose_chrome_cdp.ps1
```

脚本只查看项目Chrome进程、9223监听端口、CDP接口和AdGuard WFP驱动状态。
2026-09-25的诊断显示项目Chrome进程已带调试端口参数，但9223没有监听；同时此前三次蓝屏定位到的
AdGuard WFP驱动仍在运行。应先隔离故障驱动并重启，再关闭仅使用本项目`.chrome_profile`的Chrome窗口，
确认该项目Chrome进程退出后重试。不要删除Chrome配置目录或结束其他日常Chrome窗口。
`--resume --dry-run`可先只读核对剩余地区；它不会打开浏览器。本轮47地已经保存，
千叶、神奈川、埼玉和东京反复撞上60页限制，山形第8页曾超时；Chrome恢复后若只需重试网络失败地区，
用 `uv run python main.py --bimonth-update yamagata --no-build`，避免重复扫描四个页数受限地区。

## 意外重启诊断

在 Windows PowerShell 的项目目录执行以下只读脚本；如需定位具体蓝屏驱动，请使用管理员 PowerShell 并添加 `-CopyCrashDumps`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\collect_reboot_diagnostics.ps1
# 收集系统日志，并将近期小型蓝屏转储复制到本项目（需要管理员权限读取转储）：
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\collect_reboot_diagnostics.ps1 -CopyCrashDumps
```

结果保存在 `data/output/diagnostics/reboot-时间/reboot-diagnostics.json`。
脚本读取最近三天的关机/蓝屏、内存耗尽、硬件、显示驱动和 Windows 更新事件，
同时记录当前内存、分页文件、运行中的驱动及近期转储文件清单。`-CopyCrashDumps` 仅复制近期、每个不超过64MB的小型转储，保存到本次诊断目录的 `minidumps/`；不会上传文件。它不修改电源、驱动、更新或转储设置。
未获权限的读取会列入 `ReadErrors`；当前内存快照不能代替重启前的内存记录。
本机日志可能包含进程路径和账户信息，应先审阅再向外分享。

## Google 在线营业时间

详情页的固定营业时间区在评分/预算摘要之后、位置与座位之前。
需要登录现有 Google 账号，点击查看才申请额度并创建普通 Essentials UI Kit 组件。
不读取 Google REST 营业时间数据、不保存营业时间进 CSV，也不离线缓存 Google 返回内容。
补爬新增餐厅尚无 Google Place ID 时会显示 Tabelog 备用入口；本次不会自动运行原来的 Google 匹配脚本。
无 Google Place ID、获取失败、未登录、额度用完和未配置服务都有独立界面状态。
电脑、Fold 外屏/内屏和 iPhone 共用同一内容顺序，按各自详情栏宽度排版。
Google 自有内容的语言由 Maps JavaScript 首次加载时确定；切换本站语言会更新周围文案。

### 配置和启用（由你执行）

1. 在 Google Cloud 为本功能准备独立的浏览器密钥，启用 Maps JavaScript API 和 Places UI Kit（按 Google 官方启用页面操作）。
   设置 HTTP referrer 限制到正式域名；API 限制只允许本功能所需 API，不复用不受限服务端密钥。
2. 在本地 `.env` 配置 `GOOGLE_PLACES_UI_API_KEY`，然后构建前端。该密钥会公开在网页里，
   它不是服务器秘密；没有设置时网页只显示服务未配置状态。
3. `worker/wrangler.toml` 的 `PLACES_UI_ENABLED` 已设为 `true`，`wrangler deploy` 后生效；
   网页构建没有密钥时不会调用额度接口。部署 Worker 时会建立 SQLite Durable Object 绑定 `PLACES_QUOTA` 和迁移 `places-quota-v1`。
   现有 KV 用户收藏数据不迁移、不重置；额度放在独立存储。
4. `wrangler.toml` 设为每账号每日 `PLACES_DAILY_LIMIT=200`、全站每月 `PLACES_MONTHLY_LIMIT=20000`。
   变量缺失时代码回退为 20 / 9000；月上限超过 20000 会被拒绝，提高它需要改代码。
   日/月边界采用美国太平洋时间，界面把具体重置时刻显示为用户当地时间。
5. 先部署支持 `/api/places/permit` 的 Worker，再发布前端，并在真实设备上做小量在线验收。
   Worker 不可用或未启用时，前端不会绕过许可直接创建 Google 查询组件。

服务端用认证得到的 Google 账号身份计数，不信任浏览器传来的用户编号。
同一账号跨设备/标签共用每日额度，全站共用月额度；同一申请的网络重试幂等。
每次新查询先占一次额度，后续 Google 返回失败也不退还，以免失败重试突破预算。
额度对象在该月结束七天后删除计数及账号哈希。

**额度边界：** 以上限制的是本站正常交互发起的组件查询，不是 Google 按账号执行的计费硬上限。
UI Kit 直接从浏览器使用公开密钥，攻击者可能绕过页面；域名/API 限制、Google 控制台可用配额、
账单提醒及异常监控仍然需要设置。预算提醒本身不会强制停止计费。
网页端的 Place Details 组件按 Places UI Kit Query 计费：每月前 10,000 次免费，之后约每 1,000 次 1 美元，
与显示多少字段无关。月上限 20000 意味着正常使用下每月最多约 10 美元超额费用；
同一计费账号的其他项目也可能消耗这 10,000 次免费额度。
使用独立项目/受限密钥便于观察本功能用量，不会产生另一份免费额度。

### 本地验收

Python 新模式测试只用 fake session 和临时 CSV；Worker 测试替换网络和存储。
前端测试用模拟 Google 组件与模拟许可接口，不产生真实 Google 查询或费用。
真实 Tabelog 页面解析与 Google 配置/计费需要你在指定网络环境启用后小量核验。

离线测试命令：

```bash
uv run python -m unittest discover -s tests/pipeline -p 'test_*.py'
node --test tests/worker/places-quota.test.mjs
node tests/worker/run.mjs
uv run python tests/ux/hours_ui.py --output /tmp/hours-ui
# 在自己构建好页面后，使用实际页面作本地模拟验收：
uv run python tests/ux/hours_integration.py --preview docs --output /tmp/hours-integration
```

构建校验仍会检测异常缩库。若双月更新确实删除了较多已核实的低分/停业餐厅，
应先核对运行报告和退场档案，再决定是否用 `scripts/verify_build.py --update-baseline`
接受这次有意的数据规模变化；不要为了让检查通过而忽略未核实丢失。

参考：
- [Places UI Kit 启用步骤](https://developers.google.com/maps/documentation/javascript/places-ui-kit/get-started)
- [Places UI Kit 详情组件](https://developers.google.com/maps/documentation/javascript/places-ui-kit/place-details)
- [Google 价格表](https://developers.google.com/maps/billing-and-pricing/pricing)
- [Google 密钥安全建议](https://developers.google.com/maps/api-security-best-practices)
