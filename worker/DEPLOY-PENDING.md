# 待主人登录后执行的部署动作

本文件由实施 agent 追加，**每节只追加，不要覆盖别人写的内容**。

---

## M-069 / M-059 · Cloudflare 边缘 Cache Rule（fixpack-shell，2026-09-05）

`docs/_headers` 已经改好（`/data/*` → `immutable`、`/` 与 `/index.html` →
`max-age=60, stale-while-revalidate=86400`、新增 `/img/*`、`/emoji/*` 收紧成
`/emoji/*.png`）。但 **`Cache-Control` 只影响浏览器**：本 zone 上带 brotli 的响应
在 Cloudflare 边缘一律是 `cf-cache-status: DYNAMIC`（审计实测 HTML 与
`/data/restaurants.json?v=` 都不命中边缘，TTFB 0.87–1.08 s），必须在后台加一条
Cache Rule 才会真正 HIT。

需要在 **Cloudflare 后台**（无法用 wrangler 完成）操作：

1. Dashboard → 选择 `jpfoodmap.com` zone → Caching → Cache Rules → Create rule
2. 规则名：`jpfoodmap static payloads`
3. 匹配表达式（Edit expression）：

   ```
   (http.host eq "jpfoodmap.com" and starts_with(http.request.uri.path, "/data/"))
   or (http.host eq "jpfoodmap.com" and starts_with(http.request.uri.path, "/emoji/"))
   or (http.host eq "jpfoodmap.com" and starts_with(http.request.uri.path, "/img/"))
   ```

4. Then：
   - Cache eligibility → **Eligible for cache**
   - Edge TTL → **Use cache-control header if present**（回退 1 个月）
   - Browser TTL → **Respect origin**
   - Cache key → 保留 query string（`?v=` 就是版本号，**不要** ignore query string）

5. 可选第二条规则（HTML）：路径 `/` 与 `/index.html`，Edge TTL 设 60 s，
   Browser TTL Respect origin。**不要**给 `/sw.js` 设边缘缓存（它必须 no-cache）。

验证（部署后本地跑）：

```bash
curl -sI https://jpfoodmap.com/ | grep -i 'cf-cache-status\|cache-control'
curl -sI "https://jpfoodmap.com/data/restaurants.json?v=<当前hash>" | grep -i 'cf-cache-status\|cache-control'
curl -sI https://jpfoodmap.com/emoji/_manifest.json | grep -i 'cache-control'   # 应为 max-age=300
curl -sI https://jpfoodmap.com/img/default-avatar-v2.png | grep -i 'cache-control'
```

期望：第二次请求 `cf-cache-status: HIT`；`/emoji/_manifest.json` 不再是
`immutable`；`/img/*` 是 `max-age=604800`。

风险：无数据风险（纯缓存头）。回滚 = 删掉 Cache Rule 并把 `docs/_headers`
恢复到上一版。

---

## M1 · Worker 第 2 批 + 备份 + 隐私页（worker-api，2026-09-05）

`worker/src/index.js` 已改完并全部离线验证通过（`node tests/worker/run.mjs`
→ 93/93），**但我没有部署**：M1 的部署由集成员统一做，且线上一旦部署所有人
会重新登录一次。下面每条都注明「为什么」和「怎么验证」。

### 事实更正：wrangler 在本机**已登录**

任务书说未登录，实测不是：

```
$ cd worker && npx wrangler whoami
👋 You are logged in with an OAuth Token, associated with the email lihouze@gmail.com.
   Account: Lihouze@gmail.com's Account (1129e866494bb1ecbd2a0f6c715fa540)
   Scopes: workers (write), workers_kv (write), workers_routes (write), …
```

所以下面的命令**可以直接跑**，不需要先 `wrangler login`。（若换机器或 token
过期才需要 `cd worker && npx wrangler login`。）

### 0）先备份（已经跑过一次，read-only）

```bash
bash worker/scripts/backup-kv.sh          # 默认输出 ~/jpfoodmap-kv-backups/<UTC 时间戳>/
```

- 为什么：Cloudflare KV 没有时间点恢复，namespace 删除不可逆（M-005）。
  每次 Worker 部署前跑一次，出事能整包还原。
- 已执行：2026-09-05T15:49Z，3 个 key（`state:<sub>` ×3），落在
  `~/jpfoodmap-kv-backups/20260905T154918Z/`。**里面是真实个人数据
  （邮箱/姓名/收藏），不要放进仓库或 Dropbox 共享目录。**
- 顺带发现：3 个 blob 里有 2 个**没有 `v` 字段**（前版本化时代写入的），
  1 个 `v=5`；没有任何 blob 带 `prefs`/`favMeta`/`w`，所以 M-044 今天的
  实际损失确实是 0，它 gate 的是后面的新字段。
- 验证：`cat ~/jpfoodmap-kv-backups/<stamp>/MANIFEST.txt`，keys 数量应与
  `npx wrangler kv key list --remote --namespace-id 7d6e7c510aec41e6a309d060e40565a7 | grep -c name` 一致。

### 1）跑离线测试台（不需要网络）

```bash
node tests/worker/run.mjs        # 期望 93/93；非零退出即有回归
```

- 为什么：这一批改动跨 4 个端点，且新旧 Worker × 新旧客户端四种组合会同时
  在线上跑一段时间，测试台把四格都断言了。

### 2）部署 Worker

```bash
cd worker && npx wrangler deploy
```

- 为什么：本批 M-044 / M-043 / M-042 / M-127 / M-129 / M-132 / M-040 /
  M-008 / M-037 / M-126 / M-013 / M-053 全部在 Worker 侧，不部署等于没做。
- **代价：所有已登录用户会重新登录一次**（cookie 从 `Domain=jpfoodmap.com`
  改成 host-only，同一次响应会清掉旧的那条）。**数据不受影响**：KV 按
  Google `sub` 索引，重新登录后原样拉回来。
- **顺序提示**：前端的「删除我的云端数据」按钮（i18n-ux 任务）依赖
  `DELETE /api/state`。**Worker 部署之前，那个按钮会收到 405，属预期**；
  按钮的错误提示要能接住 405。可以先部署 Worker 再 push 页面，反过来也行，
  只是中间那一小时按钮不可用。
- 验证（部署后，全部只读）：

  ```bash
  # 405 带 Allow（新增了 DELETE）
  curl -si -X PATCH https://api.jpfoodmap.com/api/state | grep -i '^allow\|HTTP/'
  # 401 带 CORS + no-store（未登录）
  curl -si -H 'Origin: https://jpfoodmap.com' https://api.jpfoodmap.com/api/state \
    | grep -i 'HTTP/\|access-control-allow-origin\|cache-control'
  # 垃圾 cookie 要 401 而不是 500
  curl -si -H 'Origin: https://jpfoodmap.com' -H 'Cookie: tabelog_session=a.b.%%%' \
    https://api.jpfoodmap.com/api/me | grep -i 'HTTP/\|set-cookie'
  ```

  期望：405 带 `Allow: GET, PUT, DELETE, OPTIONS`；401 带
  `Access-Control-Allow-Origin: https://jpfoodmap.com` 与
  `Cache-Control: private, no-store`；垃圾 cookie 得 401 + `Max-Age=0`。
  再用浏览器登录一次，确认设置面板显示邮箱（走 `profile:<sub>` KV 键）。
- 回滚：`cd worker && npx wrangler rollback`（或 `wrangler deployments list`
  找到上一个版本 id 再 rollback）。回滚只影响代码，KV 数据不动。

### 3）（可选，仅在轮换密钥时）SESSION_HMAC_PREV

```bash
cd worker
npx wrangler secret put SESSION_HMAC_PREV   # 粘贴当前的 SESSION_HMAC 值
npx wrangler deploy
npx wrangler secret put SESSION_HMAC        # 粘贴新生成的随机值
npx wrangler deploy
# 90 天后（一个 cookie 生命周期）：
npx wrangler secret delete SESSION_HMAC_PREV
```

- 为什么：M-126。以前只有一个密钥，换掉就是所有人同时掉线。
- 不轮换就**什么都不用做**：`SESSION_HMAC_PREV` 未设置时代码走单密钥路径。
- 验证：轮换第二步之后，老 cookie 仍能 `GET /api/me` 200。

### 4）Cloudflare 后台（wrangler 做不了）

1. **Rate Limiting**（M-040）：Security → WAF → Rate limiting rules → Create
   - 表达式：`(http.host eq "api.jpfoodmap.com")`
   - 规则：同 IP **每分钟 60 次**，超出 → Block 10 秒（免费版含 1 条规则）
   - 为什么：`POST /api/session` 每次会外呼 Google tokeninfo，刷它能烧掉
     Workers 请求数与 subrequest 配额，超了整站同步停摆（Error 1027）。
   - 验证：`for i in $(seq 1 70); do curl -s -o /dev/null -w '%{http_code} ' https://api.jpfoodmap.com/api/me; done`
     最后应出现 429。**跑完记得等 1 分钟再用站点。**
2. **Bot Fight Mode**：Security → Bots → 打开（免费版）。同上，减少无谓请求。
3. 不需要给 `api.jpfoodmap.com` 加任何 Cache Rule——响应已经是
   `private, no-store`，加缓存反而危险（会串号）。

### 5）Google Cloud Console（隐私政策，M-008）

OAuth 同意屏幕 → 编辑应用 → 「隐私权政策链接」填
`https://jpfoodmap.com/privacy.html`（页面已随 `docs/` 发布）。

- 为什么：Google OAuth API Services 政策要求；一旦触发验证流程而没有这个
  链接，整站登录会被卡住。
- 验证：打开 `https://jpfoodmap.com/privacy.html` 应 200；同意屏幕保存后
  在授权页底部能看到链接。
