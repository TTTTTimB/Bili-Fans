# 怎么拿到自己的 SESSDATA

SESSDATA 是 B 站登录态的凭据，**本质等同于你的账号密码**（拿到它就能以你的身份发言、改资料）。
所以只需要、也只能拿**你自己账号**的。整个过程不需要把密码交给任何第三方工具。

> 下面所有操作都在你自己的浏览器里完成，Cookie 不会离开你的电脑，只会写进本地数据库和你自己的环境变量。

## 方法 0：`bilifans login`（推荐）

```bash
./bin/bilifans login
```

会弹出一个**独立 profile** 的 Chrome 窗口，直接打开 B 站登录页：

```
正在启动浏览器…
  浏览器已启动，正在打开登录页…
  登录页加载完成，等待扫码…
  等待扫码…还剩 175 秒
✓ 已取回 Cookie（17 个字段，用时 23.4 秒）
  SESSDATA=027df2…IIEC; DedeUserID=…
  已保存到 /Users/you/.bilifans_cookie（权限 600）
✓ 已登记账号：你的昵称（UID 12345678）
```

- 窗口里扫码登录一次即可，**登录状态记在这个 profile 里**，下次通常不用再扫
- Cookie 存到 `~/.bilifans_cookie`，之后所有命令自动使用
- 登录的是哪个号，程序会自动读出来并登记成监控账号，不需要 `bilifans add`

### 兜底：手动粘贴

登录页怎么都渲染不出来时，看板「采集」区有个 **「手动粘贴 Cookie」** 按钮：
把从开发者工具复制的 `SESSDATA=...`（或整段 Cookie）粘进去，效果和扫码登录一样。

### 在网页看板里登录

```bash
bilifans serve
```

点 **「登录 B 站」**，效果完全一样：弹出浏览器窗口，等待扫码的进度实时打在按钮下方的日志框里。

### 窗口一直转圈怎么办

新版已经处理了常见的几个原因：

- **直接打开登录页**（`passport.bilibili.com/login`），不再先开首页再找入口
- **窗口自动提到最前**，并关掉 Chrome 的后台/遮挡节流
  （`--disable-background-timer-throttling --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows --disable-features=…CalculateNativeWinOcclusion`）：
  窗口一旦失去焦点，macOS 可能判定「被遮挡」并暂停渲染，表现就是**只有蓝色顶栏、登录框永远不出来**
- 关掉了首启后台任务：`--no-service-autorun --disable-sync --disable-component-update
  --disable-extensions --use-mock-keychain`
- 导航后会等 `document.readyState === 'complete'` 再开始读 Cookie，并打印「登录页加载完成」

如果还是慢，多半是网络到 `passport.bilibili.com` 的问题，可以：
`bilifans login --timeout 300`（多等一会儿），或按下面方法 A/B 手动复制。

---

## ⚠️ 先排除一个坑：控制台里 `document.cookie` 拿不到 SESSDATA

网上很多教程会让你在控制台输入 `document.cookie` 或 `document.cookie.match(/SESSDATA=/)`，
**这在 B 站上行不通**——SESSDATA 被 B 站设成了 **HttpOnly**，浏览器的 JavaScript 引擎
根本读不到它（这是 B 站的安全设计，否则任何一个 XSS 漏洞都能盗号）。

所以你会看到 `undefined` 或空字符串，这是正常的，换个方法即可。
**HttpOnly 只拦 JavaScript，不拦开发者工具面板**——面板里照样能看到和复制。

---

## 方法 A：储存空间 / Application 面板（手动）

1. 先打开 <https://www.bilibili.com>（**不要用 space.bilibili.com**，见下面第 5 点），
   确认右上角是你的头像。
2. 按 `F12` 打开开发者工具。
   - Chrome / Edge 中文版：点顶部 **「应用」** 或 **「储存空间」** 标签
   - 英文版：**Application**
3. 左侧栏找到 **Cookies**，展开后点 **`https://www.bilibili.com`**。
4. 右侧出现一张表，列是 `名称 / 值 / 域 / 路径 / 过期时间 / 大小 / HttpOnly`。
   找到**名称 = `SESSDATA`** 的那一行，**双击「值」这一格**，全选复制。
5. 如果在 `space.bilibili.com` 下面找不到 SESSDATA，是因为它是属于 `.bilibili.com`
   这个域名的 Cookie，必须先切到 `https://www.bilibili.com` 再看。

复制出来大概长这样（`%2C` 是逗号被 URL 编码了，**原样保留，不要解码**）：

```
a1b2c3d4%2C1760000000%2C9f8e7d%2A32
```

> 面板里 `HttpOnly` 那一列会打个勾，就是前面说的那个坑。

## 方法 B：网络面板复制整段 Cookie（手动，最可靠）

面板找不着就用这个，100% 能看到（包括 HttpOnly 的 Cookie）：

1. `F12` → **网络 / Network** 标签
2. **刷新页面**（`Command + R`），请求列表会刷出一堆
3. 点其中任意一个发往 `api.bilibili.com` 或 `www.bilibili.com` 的请求
4. 右侧 → **标头 / Headers** → 往下找 **请求标头 / Request Headers** → **`Cookie`**
5. 整段复制，形如：

```
SESSDATA=a1b2c3d4%2C176...; bili_jct=xxx; buvid3=xxx; ...
```

程序两种都认：**只给 SESSDATA 的值**，或者**整段 Cookie**。整段更保险。

## 方法 C：Safari

1. 菜单栏 **Safari → 设置 → 高级**，勾选「显示网页开发者功能」
2. 回到页面按 `Option + Command + I`
3. 切到 **存储 → Cookie → bilibili.com**，找 `SESSDATA`

## 方法 D：手机上的账号怎么办？

最省事的姿势：**在电脑上扫码登录一次**。

手机 App 里拿 Cookie 需要抓包 + 装证书，麻烦且有风险，不建议。电脑扫码登录后，
Cookie 和手机上登录的是同一个账号，功能完全一样。

---

## 验证拿到的对不对

拿到的值先别急着用，一条命令就能验证：

```bash
export BILI_COOKIE='SESSDATA=你复制的值'
./bin/bilifans whoami
```

正常会输出：

```
✓ Cookie 有效
  登录账号  你的昵称（UID 12345678）
  创作中心  可用，近 30 天里有 30 天数据（2026-10-14 ~ 2026-11-12）
  监听列表  已添加：我的号（UID 12345678）
```

如果输出「✗ Cookie 校验失败：code=-101」，说明复制不完整或已失效，重新走一遍上面的步骤。

---

## 怎么交给程序

### 方式 1：临时（当前终端窗口有效）

```bash
export BILI_COOKIE='SESSDATA=...'
./bin/bilifans sync --all
```

### 方式 2：长期（写进 shell 配置）

```bash
echo "export BILI_COOKIE='SESSDATA=...'" >> ~/.zshrc
```

注意 `~/.zshrc` 是明文，机器有别人用的话不要这么做。

### 方式 3：文件 + 权限（推荐）

```bash
echo 'SESSDATA=...' > ~/.bilifans_cookie
chmod 600 ~/.bilifans_cookie
export BILI_COOKIE="$(cat ~/.bilifans_cookie)"
```

`.gitignore` 里已经忽略了 `cookie.txt`，别把 Cookie 提交到任何仓库。

### 方式 4：launchd 定时任务

模板 `scripts/com.bilifans.plist.template` 里的 `EnvironmentVariables` 就是干这个的，
把 `SESSDATA=你的值` 填进去即可。

---

## 安全须知

1. **SESSDATA = 账号密码**。别人拿到它可以直接用你的号发弹幕、评论、改签名、看你的创作中心数据。
2. 不要发到群里、不要贴进截图、不要提交 git、不要填进任何「在线检测工具」。
3. 只在**你自己的机器**上使用。这个工具只会把 Cookie 发往 `api.bilibili.com` 和
   `member.bilibili.com`，没有第三方的请求。
4. 只拿自己账号的。拿别人的 Cookie 属于盗号，不是「技术问题」。

## 有效期与失效

- 一般能撑**几个月**，B 站会在你长期不活跃或改密码、退出所有设备时让它失效。
- 失效的表现：`backfill` 报 `code=-101 账号未登录`，`whoami` 显示无效。
- 处理方式：重新走一遍上面的步骤，覆盖掉环境变量里的旧值即可。
- 失效期间**不影响**总粉丝数采集（`poll`），只是新增/掉粉会退回采样估算。

## 常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 控制台输出 `undefined` / 空 | **SESSDATA 是 HttpOnly，JS 读不到** | 用「储存空间」面板或「网络」面板，别用 `document.cookie` |
| `应用` 面板里找不到 SESSDATA | 当前页是 `space.bilibili.com`，Cookie 属于 `.bilibili.com` | 先切到 `https://www.bilibili.com` 再打开面板 |
| `code=-101` | 值复制少了 / 已过期 / 在错误域名下复制 | 重新在 `www.bilibili.com` 下复制 |
| `code=-400` | Cookie 不完整，少了 `bili_jct` 之类的配套字段 | 用方法 B 复制整段 Cookie |
| `code=-352` | 触发了风控，常见于机房 IP、频繁请求 | 换个网络（家用宽带 / 手机热点）再试，别在服务器上跑 |
| `whoami` 显示的不是自己的号 | 浏览器里登录的是另一个账号 | 换账号重新登录后再取 |
| 复制出来的值带引号/换行 | 从某些编辑器里复制的 | 程序会自动去掉首尾引号，但换行会截断，重新复制一次 |
