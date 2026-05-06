# DeepSeek 注册链路调研记录

日期：2026-04-28

## 目标

参考当前仓库里 `cerebras` 的浏览器优先调研方式，验证 `https://chat.deepseek.com/sign_in` / `https://chat.deepseek.com/sign_up` 在以下条件下的真实注册链路：

1. 直连浏览器。
2. 使用仓库当前配置好的激活代理。
3. 在用户要求下，额外切到 `http://192.168.1.18:7890`。
4. 再补 `ja-JP` / `ja_JP` + `Asia/Tokyo` 的日文化验证。

目标不是猜测 DeepSeek 应该支持什么，而是确认今天真实可见的入口、风控边界、以及“邮箱激活 + 设置密码”这条链路是否真的能走通。

## 调研方法

### 1. 浏览器工具直连验证

使用浏览器工具直接访问：

- `https://chat.deepseek.com/sign_in`
- `https://chat.deepseek.com/sign_up`

读取真实 DOM、输入框、按钮、控制台和网络请求。

### 2. 本地代理 + `en-US` 浏览器探针

从本仓库 SQLite 配置库 `account_manager.db` 读取当前激活代理：

- `proxies.id = 8`
- `url = socks5://192.168.1.18:1080`
- `is_active = 1`

随后用本地 Playwright 探针起 `locale="en-US"`、`timezone_id="America/Los_Angeles"`、`Accept-Language: en-US,en;q=0.9` 的独立浏览器上下文，访问：

- `https://chat.deepseek.com/sign_in`
- `https://chat.deepseek.com/sign_up`
- `https://chat.deepseek.com/sign_in?locale=en-US`
- `https://chat.deepseek.com/sign_in?lng=en-US`

之后按用户要求，把同一个探针的代理改为：

- `http://192.168.1.18:7890`

并重跑同样四个入口。

随后又补了：

- `locale = ja-JP`
- `timezone_id = Asia/Tokyo`
- `localStorage.webLocalePreference = ja_JP`
- `localStorage.webLocale = ja_JP`

分别对以下代理重跑：

- `socks5://192.168.1.18:1080`
- `http://192.168.1.18:7890`

### 3. 官方登录 API 结构探针

对 `POST https://chat.deepseek.com/api/v0/users/login` 做最小合法请求探测，验证当前后端是否仍接受邮箱密码登录形态。

## 已验证事实

### 0. 用户提供的真实浏览器截图已经证明：`socks5://192.168.1.18:1080` 存在另一条日文密码登录分支

在本次调研后续，用户提供了一张真实浏览器截图，明确说明使用：

- `socks5://192.168.1.18:1080`
- JP 浏览器

可以看到 DeepSeek 的日文密码登录页，页面元素包括：

- `電話番号 / メールアドレス`
- `パスワード`
- `ログイン`
- `新規登録`
- Google / Apple 第三方登录按钮

这条用户侧证据非常重要，因为它推翻了一个过强结论：

- 不能再说 `socks5://192.168.1.18:1080 + JP 浏览器` 只会落到 `Human Verification`。

更准确的 owner 划分应当是：

1. 同一条 `socks5://192.168.1.18:1080`，真实浏览器会话可以到达日文密码登录页。
2. 但当前自动化探针在这条 socks5 上仍会落到 `Human Verification`。
3. 因此当前分叉 owner 已经明确偏向：
   - 浏览器指纹
   - 会话状态 / cookies / localStorage
   - 自动化痕迹
   - 或目标站针对自动化浏览器的差异化分流

这张截图只能证明：

- 该页面变体真实存在。

它还不能单独证明：

- `新規登録` 后一定是邮箱注册。
- 自动化路径已经具备复现条件。

后续已补做更接近真实用户的复现，结果见后文 `H`：在 `headed msedge + socks5://192.168.1.18:1080 + ja-JP` 下，`sign_in`/`sign_up` 的确能进入与该截图一致的日文密码分支，并且 `sign_up` 已成功走通邮箱注册。

### A. 直连 `sign_in` 不是邮箱激活页，而是手机号 + 短信验证码登录

浏览器工具直连 `https://chat.deepseek.com/sign_in` 的真实页面元素为：

- 区号：`+86`
- 输入框：`请输入手机号`
- 输入框：`请输入验证码`
- 按钮：`发送验证码`
- 按钮：`登录`
- 文案：`未注册的手机号将自动注册`

这说明当前直连默认入口不是：

- 邮箱输入
- 激活邮件
- 设置密码

而是手机号验证码登录，未注册手机号会自动注册。

### B. 直连 `sign_up` 是手机号 + 密码 + 短信验证码注册

浏览器工具直连 `https://chat.deepseek.com/sign_up` 的真实页面元素为：

- 文案：`你所在地区仅支持手机号注册`
- 输入框：`请输入手机号`
- 输入框：`请输入密码`
- 输入框：`请再次输入密码`
- 输入框：`请输入验证码`
- 按钮：`发送验证码`
- 按钮：`注册`
- 按钮：`返回登录`

这条证据非常关键，因为它证明：

1. DeepSeek 当前确实有显式注册页。
2. 当前这台机器直连看到的注册方式是“手机号 + 设置密码 + 短信验证码”。
3. 当前直连看到的不是“邮箱激活链接 + 设密码”。

### C. 直连页面的地区/语言事实上是 `CN` / `zh-CN`

直连页面和网络请求里可以看到：

- `html_lang = zh-CN`
- `webLocale = zh_CN`
- `ds_region = CN`
- `ds_rawRegion = CN`

因此当前直连看到的手机号注册页并不是偶然 UI，而是和地区识别直接相关。

### D. 使用仓库当前激活代理 `socks5://192.168.1.18:1080` + `en-US` 时，入口先撞到风控页

用当前激活代理 `socks5://192.168.1.18:1080`，并把浏览器 locale / header 改成 `en-US` 后，访问 DeepSeek 候选入口得到的结果不是邮箱注册表单，而是风控页：

已记录到两类真实结果：

1. `Human Verification`
   - 标题：`Human Verification`
   - 文案：`Let's confirm you are human`
   - 按钮：`Begin`

2. CloudFront 拒绝页
   - 标题：`ERROR: The request could not be satisfied`
   - 正文：`403 ERROR The request could not be satisfied. Request blocked.`

对应的候选 URL 包括：

- `https://chat.deepseek.com/sign_in`
- `https://chat.deepseek.com/sign_up`
- `https://chat.deepseek.com/sign_in?locale=en-US`
- `https://chat.deepseek.com/sign_in?lng=en-US`

在这组代理 + `en-US` 探针里，没有抓到任何邮箱、密码、手机号、短信验证码字段；入口在到达真正注册表单之前就被拦住了。

### E. 使用 `http://192.168.1.18:7890` + `en-US` 后，可以进入英文真实表单，但仍然是手机号流

把代理切到 `http://192.168.1.18:7890` 并保持 `en-US` 浏览器设置后，四个候选入口都可以返回 `200` 且进入真实表单。

#### 1. `sign_in`

真实页面元素为：

- `Phone number`
- `Code`
- `Send code`
- `Log in`
- 文案：`New phone numbers will be automatically registered.`

没有看到：

- `Email`
- `Password`
- 激活邮件相关文案

#### 2. `sign_up`

真实页面元素为：

- 文案：`Only phone number registration is supported in your region.`
- `Phone number`
- `Password`
- `Confirm password`
- `Code`
- `Send code`
- `Sign up`
- `Log in`

这条证据比直连中文页更强，因为它证明：

1. 在用户指定的 `http://192.168.1.18:7890` 代理下，页面已经切成英文。
2. 即使是英文真实表单，注册方式仍然是“手机号 + 密码 + 短信验证码”。
3. 仍然没有观察到“邮箱激活链接 + 设置密码”的注册 UI。

### F. `ja-JP` 确实能触发日文 UI，但当前已验证链路里仍然没有邮箱注册

#### 1. `socks5://192.168.1.18:1080` 不是日本出口

通过 `https://ipinfo.io/json` 经该代理探测得到：

- `country = US`
- `region = California`
- `city = East Los Angeles`

也就是说，这条 socks5 不是 JP 出口。

在此基础上，即使把 locale 切到 `ja-JP`、timezone 切到 `Asia/Tokyo`，DeepSeek 仍然返回：

- `Human Verification`
- `Let's confirm you are human`

没有进入任何日文注册表单。

#### 2. `http://192.168.1.18:7890` 在 `ja-JP` 下可以触发日文真实 UI

通过 `https://ipinfo.io/json` 经该代理探测得到：

- `country = US`
- `region = New York`
- `city = Buffalo`

这条代理同样不是 JP 出口，但它在 `ja-JP` 下可以稳定触发日文 UI：

- 页面标题：`DeepSeek - 未知への旅`
- `html_lang = ja`
- `navigator_language = ja-JP`

#### 3. 但日文 UI 仍然是手机号流，不是邮箱流

`sign_in` 日文化后真实元素为：

- `電話番号`
- `コード`
- `コードを送信`
- `ログイン`
- 文案：`新しい電話番号は自動的に登録されます。`

`sign_up` 日文化后真实元素为：

- 文案：`お住まいの地域では電話番号での登録のみ対応しています。`
- `電話番号`
- `パスワード`
- `パスワード確認`
- `コード`
- `コードを送信`
- `新規登録`

因此，本次验证可以确认：

1. `ja-JP` locale 的确会把 UI 切成日文。
2. 但当前已走通的日文 UI 仍然是手机号注册，不是邮箱注册。
3. 还没有证据证明“日文版本 = 邮箱注册”。

### G. 登录 API 今天仍然接受邮箱密码登录形态

对 `POST https://chat.deepseek.com/api/v0/users/login` 的结构探针结果如下：

1. 只发 `email + password` 时：
   - `422`
   - 缺少字段：`device_id`、`os`

2. 补齐：
   - `email`
   - `password`
   - `device_id`
   - `os = web`

后，无论直连、数据库里激活的 `socks5://192.168.1.18:1080`，还是用户指定的 `http://192.168.1.18:7890`，都得到：

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 2,
    "biz_msg": "PASSWORD_OR_USER_NAME_IS_WRONG",
    "biz_data": null
  }
}
```

这说明当前后端并没有把邮箱字段当成非法输入直接拒绝；它接受邮箱密码登录结构，只是凭据错误。

这条证据只能证明：

- “邮箱密码登录能力”当前仍存在。

它不能证明：

- “新用户邮箱激活注册”当前一定开放。
- “代理 + `en-US` 下真实注册页就是邮箱注册”。

### H. `headed msedge + socks5://192.168.1.18:1080 + ja-JP` 已成功复现截图分支，并完成邮箱注册

为压缩 owner，又补了一条更接近真实用户的浏览器路径：

- 浏览器：`msedge`
- 模式：`headed`
- 代理：`socks5://192.168.1.18:1080`
- locale：`ja-JP`
- timezone：`Asia/Tokyo`
- localStorage:
  - `webLocalePreference = ja_JP`
  - `webLocale = ja_JP`

在这条路径下：

#### 1. `sign_in?locale=ja-JP` 进入了用户截图同类页面

真实表单元素为：

- `電話番号 / メールアドレス`
- `パスワード`
- `パスワードをお忘れですか？`
- `新規登録`
- `ログイン`

这与用户截图已经对上，说明之前同 socks5 下卡在 `Human Verification` 的 owner 主要是自动化模式差异，而不是代理或 locale 本身。

#### 2. `sign_up?locale=ja-JP` 明确是邮箱注册，不是手机号注册

真实页面文案为：

- `お住まいの地域ではメール登録のみ対応しています。`

真实表单字段为：

- `メールアドレス`
- `パスワード`
- `パスワード確認`
- `コード`

也就是说，`sign_up` 这条路在该浏览器模式下已经切到邮箱注册。

#### 3. 已成功完成一次端到端注册

本次实测链路：

1. 从 `cfworker` 生成新邮箱。
2. 打开 `https://chat.deepseek.com/sign_up?locale=ja-JP`。
3. 填入邮箱、密码、确认密码。
4. 点击 `コードを送信`。
5. 收到 DeepSeek 邮箱验证码。
6. 填入验证码并点击 `新規登録`。
7. 页面跳转到 `https://chat.deepseek.com/`，进入已登录状态。

成功后的页面证据包括：

- 左侧出现新会话 UI：`新規チャット`
- 账号信息显示为新邮箱的遮罩形式
- 已进入聊天主页
- 额外出现生日确认弹窗：`あなたはいつ生まれましたか？`

#### 4. 当前最准确的 owner 划分

现在可以确认：

1. 不是所有 `ja-JP` 探针都会得到同一个结果。
2. `headless` / 更强自动化痕迹会把同 socks5 流量导向 `Human Verification`。
3. `headed msedge` 的真实浏览器路径可以进入：
   - `sign_in`: `電話番号 / メールアドレス + パスワード`
   - `sign_up`: `メール登録のみ対応`
4. 因此“是否能触发邮箱注册”目前更依赖浏览器模式/指纹，而不是只依赖代理协议名或 locale 值。

## 结论

### 1. 真实可用的新用户邮箱注册路径已经证明存在，但稳定 owner 仍是浏览器，不是纯协议

截至 2026-04-28，本次调查已经证明：

1. `headed msedge + socks5://192.168.1.18:1080 + ja-JP + Asia/Tokyo` 可以稳定进入：
   - `sign_in`: `電話番号 / メールアドレス + パスワード`
   - `sign_up`: `お住まいの地域ではメール登録のみ対応しています。`
2. 在该路径下，真实邮箱验证码注册已经成功完成至少一次，页面最终跳转到 `https://chat.deepseek.com/`。
3. 同一 socks5 在更强自动化/`headless` 路径下，仍可能落到 `Human Verification`。

所以“是否能触发邮箱注册”当前更依赖浏览器模式和指纹面，而不是只依赖代理协议名或 locale 值。

### 2. 忘记密码链路已经完整协议化并验证通过

已验证可用的忘记密码 / 登录相关协议端点为：

- `POST /api/v0/users/create_guest_challenge`
- `POST /api/v0/users/create_email_verification_code`
- `POST /api/v0/users/check_email_code`
- `POST /api/v0/users/email_reset_password`
- `POST /api/v0/users/login`

这说明以下动作已经可以稳定由 `curl_cffi` / 协议客户端替代页面：

- 发邮箱验证码
- 校验邮箱验证码
- 提交忘记密码的新密码
- 邮箱密码登录校验

### 3. `/register` 的最终契约和 PoW owner 也已经证明，但纯协议新注册仍会撞设备风控

当前已经确认：

1. 最终注册接口是：
   - `POST https://chat.deepseek.com/api/v0/users/register`
2. 其关键 payload 结构为：

```json
{
  "locale": "ja",
  "region": "US",
  "payload": {
    "email": "...",
    "email_verification_code": "...",
    "password": "..."
  },
  "device_id": "...",
  "os": "web"
}
```

3. 注册前需要额外获取一次作用于 `/register` 的 guest challenge。
4. `x-ds-guest-pow-response` 实际只回传：

```json
{"salt":"...","answer":12345}
```

5. DeepSeek 前端 PoW worker 的 owner 已确认；必须在 `chat.deepseek.com` 同源页面里创建 worker 才能复用站点真实算法。

但即使已经改成：

- 海外代理
- 真实邮箱验证码
- 真实 worker 计算出的 PoW answer

纯协议 `POST /register` 仍然命中过：

- `biz_code = 11`
- `biz_msg = "RISK_DEVICE_DETECTED"`

因此本次最强结论是：

- `curl_cffi` 已经可以覆盖发码 / 校码 / 重置密码 / 登录；
- `curl_cffi` 也已经能触达真实 `/register` 契约；
- 但“纯协议创建全新账号”截至今天仍不如浏览器注册稳定，稳定 owner 仍是浏览器。

### 4. 当前仓库实现决策

基于以上 owner 边界，仓库内的 `DeepSeek` 平台实现采用：

1. 注册：
   - 走浏览器注册路径
   - 支持执行器：`headless` / `headed`
2. 协议层继续承担：
   - 登录校验
   - 忘记密码
   - 发码/校码探针
   - `/register` 契约探测
3. 前端已补齐：
   - `DeepSeek` 平台入口
   - DeepSeek 配置页
   - 注册页 DeepSeek 专属配置
   - 账号页 `用户名 / 密码` 列
   - 详情页 `用户名 / Need Birthday / Device ID`

### 5. 账号与凭据结果

本次调查期内已经成功创建并验证过 DeepSeek 账号，且后续忘记密码链路也已走通。

出于避免把真实凭据直接写入长期文档的考虑，这里只保留“链路已走通”的结论，不再把账号邮箱、密码或本地探针输出文件名当作长期文档内容维护。

### 6. 本次产物

临时探针、证据和实现相关输出曾覆盖以下几类信息：

- 入口页 DOM / 网络抓包
- 风控页与手机号页截图
- 浏览器注册链路探针
- 忘记密码链路探针
- DeepSeek 前端 bundle / PoW worker 分析
- 对应的 JSON 结果快照

这些产物现在不仅可用于重放调查，也已经直接支撑仓库内的 DeepSeek 平台接入实现。

## 2026-04-28 晚间复核补充

### 1. 中午那条 `等待验证码超时` 失败，不能再直接归因为 CFWorker

对 `task_1777377652384` 的严格复核表明：

1. 任务日志里确实出现了多次：
   - `CFWorker /admin/mails ... Read timed out`
2. 但当时的浏览器实现只是：
   - 点击 `コードを送信`
   - 固定等待 `2.5s`
   - 立即记录 `"[DeepSeek] 浏览器已发送注册验证码"`
3. 它没有等待并校验真实的 `POST /api/v0/users/create_email_verification_code` 响应。

因此该任务最多只能证明：

- 收码阶段看到了 CFWorker 超时现象；

它不能证明：

- DeepSeek 页面当时一定已经成功接受了发码请求；
- `CFWorker` 就是那次失败的主根因。

### 2. 严格对照验证：协议发码 + CFWorker 收码链路是通的

使用同一代理、同一域名、同一邮箱后端做纯协议验证：

- 代理：`socks5h://192.168.1.18:1080`
- 区域参数：`ja-JP + US`
- 域名：`mail.highkay.com`

结果：

1. `POST /api/v0/users/create_email_verification_code` 返回：

```json
{
  "code": 0,
  "msg": "",
  "data": {
    "biz_code": 0,
    "biz_msg": "",
    "biz_data": {
      "send_window_secs": 60
    }
  }
}
```

2. `CFWorkerMailbox.wait_for_code()` 成功收到了 DeepSeek 验证码。
3. 原始 `/admin/mails` 轮询也返回 `200`，并出现新邮件。
4. 对同一个邮箱连续额外压测 15 次 `/admin/mails`，全部为 `200`。

这说明：

- 当前 `CFWorker` 不是“必现坏掉”；
- 当前 `wait_for_code()` 逻辑也不是天然抓不到 DeepSeek 验证码；
- `CFWorker` 更准确的 owner 级别应是“有间歇性超时风险”，而不是“已经被证明是主因”。

### 3. 浏览器页面 owner 被重新证明为当前主链问题

晚间的浏览器 DOM 探针表明：

1. `sign_up?locale=ja-JP` 当前仍然能渲染邮箱注册表单。
2. 表单真实 DOM 节点存在于主文档，不在 iframe，也不在 open shadow root：
   - `input.ds-input__input[type="text"]` -> `メールアドレス`
   - `input.ds-input__input[type="password"]` -> `パスワード`
   - `input.ds-input__input[type="password"]` -> `パスワード確認`
   - `button.ds-verify-code-input-countdown` -> `コードを送信`
3. 旧实现因为：
   - 依赖日文 placeholder 的即时可用性；
   - 不等待表单真正 hydrated；
   - 不等待发码接口真实响应；
   所以会把“点击成功”误记成“发码成功”。

### 4. 修复后已完成双重成功验证

修复点：

1. 浏览器注册现在会先等待表单节点真实出现。
2. 填表后会等待并校验：
   - `POST /api/v0/users/create_email_verification_code`
3. 只有在响应 `biz_code == 0` 后，才进入邮箱收码阶段。

验证结果：

#### A. 插件级直连验证成功

`DeepSeekPlatform.register()` 已成功完成：

- 浏览器发码
- CFWorker 收码
- 表单提交
- 跳转到 `https://chat.deepseek.com/`
- 协议登录校验

成功样本之一已经完成整条浏览器注册链路验证；出于长期文档安全性考虑，这里不再记录真实邮箱或密码。

#### B. 应用级 `/api/tasks/register` 真任务验证成功

新的真实任务：

- `task_id = task_1777380505520`
- 状态：`done`
- `success = 1`

关键日志：

- `[DeepSeek] 浏览器已发送注册验证码`
- `[CFWorker] 命中新验证码 ... code=595950`
- `[DeepSeek] 浏览器注册完成 final_url=https://chat.deepseek.com/`
- `[OK] 注册成功: <redacted>`

该账号已经写入本地 `accounts` 表，密码也已落库。

### 5. 当前最新可用账号

本节只保留结论，不再记录真实邮箱或密码。后续如需做账号级验证，应直接查本地数据库或任务日志，不把临时注册凭据写入长期文档。

落库字段示例：

- `platform = deepseek`
- `register_via = browser`

### 6. 调查期原始产物

本次调研曾生成大量本地 `tmp_*` 探针脚本、截图与 JSON 输出。这些原始产物仅用于当时的即时验证，不属于长期项目文档的一部分，后续已按本地调试残留处理，不再把具体文件名当作正式交付物维护。

如果需要重做同类验证，建议直接参考仓库内当前的：

- `platforms/deepseek/`
- `tests/test_deepseek_core.py`
- `tests/test_deepseek_plugin.py`

## 2026-05-06 复核补充

### 1. 旧的 HTTP 预检不能再当作 DeepSeek 注册入口真相

在当前 Windows 主机上，进程环境里存在：

- `http_proxy=http://127.0.0.1:7890`
- `https_proxy=http://127.0.0.1:7890`

因此基于 `curl_cffi` / `HTTPClient(proxy=None)` 的“直连”预检，会被环境变量代理污染；它看到的出口不一定等于 Playwright 浏览器真实出口。

本轮复核已经确认：

1. `HTTP` 探针的“直连”与显式 `http://127.0.0.1:7890`，都可能落到同一个代理出口。
2. Playwright 在“未显式传 proxy”时，这台机器的真实浏览器出口则可能是本机联通 IPv6 直连。

因此当前仓库里的 `DeepSeek` 前置检查已经改为浏览器级检查，而不是只看 HTTP 预检。

### 2. 当前更稳定的 owner 是页面分支，而不是 locale

2026-05-06 又对以下 locale 做了真实浏览器矩阵：

- `ja-JP`
- `en-US`
- `zh-CN`
- `zh-TW`
- `ko-KR`
- `de-DE`
- `fr-FR`
- `es-ES`
- `pt-BR`
- `ru-RU`
- `it-IT`
- 无 `locale` 参数

并分别在：

- `direct`
- `socks5h://highkay_1:1844@gate.rola.vip:2000`

下复核。结果 `24/24` 全部落到同一个分支：

- `phone_only`

也就是说：

1. `locale` 只会改变页面语言。
2. 不会把手机号注册页切回邮箱注册页。
3. 当前 owner 更接近 DeepSeek 服务端按出口/风险策略分配注册页分支。

### 3. Rola 刷新国家后，IP 会变，但页面分支仍然是 `phone_only`

本轮还对 `refresh.rola.vip` 做了国家级刷新验证，`country` 至少测试了：

- `us`
- `jp`
- `de`
- `gb`
- `sg`
- `ca`
- `au`
- `br`

出口 IP 已确认会变化，但这些国家下的浏览器结果仍全部为：

- `DeepSeek 当前出口命中手机号注册页，不支持邮箱注册`

因此截至 2026-05-06，这条结论已经足够稳定：

- “只靠换 locale”不能恢复邮箱注册页。
- “只靠 refresh country”也不能保证恢复邮箱注册页。
- 如果要继续提升成功率，应优先换线路类型/供应商，或者直接转手机号注册链路。
