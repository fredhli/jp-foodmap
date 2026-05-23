# 同步教程

把收藏餐厅名单 / 弃用餐厅名单（不再显示在地图上） / 自定义地标（如景点、酒店等）进行云端同步，跨设备共用。

---

## 步骤

1. **注册 / 登录 GitHub**
   打开 <https://github.com> ，登录 / 注册一个账号。

   ![GitHub 首页](step1_github_register.png)

2. **新建一份 Gist**
   打开 <https://gist.github.com> ，filename 填 `favorites.json`，内容填 `[]`，右下角点 **Create secret gist**。

   ![新建 Gist](step2_create_gist.png)

   创建后地址栏里 `gist.github.com/<用户名>/` 后面那串十六进制就是 **Gist ID**，复制下来，等下要填进网站。

   ![地址栏里的 Gist ID](step2_create_gist_2.jpg)

3. **生成 PAT**
   打开 <https://github.com/settings/tokens?type=beta> ，右上角点 **Generate new token**。

   ![PAT 设置页](step3_PAT.png)

   - Token name：随便填
   - Expiration：建议 **No expiration**
   - 往下拉到 **Permissions** → **Add permissions** → 搜并勾 **Gists**

   ![No expiration + 选 Gists](step3_PAT_2.png)

   把 **Gists** 的 Access 改成 **Read and write**，拉到底点 **Generate token**。

   ![Gists 设为 Read and write](step3_PAT_3.png)

   **马上复制**生成的 `github_pat_...`（只显示一次，关掉就再也看不到）。

   ![生成的 PAT](step3_PAT_4.png)

4. **填进我的网站**
   回到地图 → 左下角 🎛 → 拉到底 → **⚙️ 同步设置** → 粘贴 Gist ID + PAT → **保存并测试**。

   ![同步设置面板](step4_fill.jpg)

看到 **✓ 配置成功** 就通了。之后每次 ⭐ / 🚫 都会自动同步。

---

## 其他设备 / 其他浏览器

在本网站的同步设置里重新填入已有的 Gist / PAT，就可以随时拉取你已经同步到云端的个人收藏 / 弃用 / 景点名单。

---

## 常见问题

- **左下角红色按钮还在闪？** 刷新页面；如果还闪，看同步设置底部的状态栏 —— *HTTP 403* 一般是 PAT 没勾 *Read and write*，重做步骤 3。
- **不填 Gist 怎么办？** 本地做的所有修改无法被同步，一旦换浏览器 / 换设备 / 清空浏览器缓存，就看不到了。
- **只填 Gist 不填 PAT 怎么办？** 可以看到收藏 / 弃用 / 景点名单，但是更改无法被同步到云端（只读但不可修改）。
