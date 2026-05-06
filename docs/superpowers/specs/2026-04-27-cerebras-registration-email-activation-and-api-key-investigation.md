# Cerebras 注册、邮件激活与 API Key 流程调研记录

## 背景

本次调研目标是参考当前仓库里已经落地的 `NVIDIA` 浏览器注册链路，验证 `https://www.cerebras.ai/` 对应的开发者注册流程，并确认：

1. 注册入口与真实登录/注册页面。
2. 邮件验证是否为验证码还是激活链接。
3. 首次登录后的 onboarding/套餐页行为。
4. API key 的控制台落点与当前可否真实走通。

所有结论均在 2026-04-27 重新以真实页面、真实邮箱、真实网络请求验证。本文只记录已经证明的事实，不补业务实现。

## 与 NVIDIA 流程的关系

和 `platforms/nvidia` 一样，Cerebras 也必须按“浏览器优先 + 邮箱回调 + 最终进入控制台”来建模，不能先假设可用纯协议完成。

但它与 NVIDIA 的关键差异也已经明确：

- NVIDIA 是邮箱输入后进入注册页，再做 hCaptcha、密码、邮箱验证与 API key。
- Cerebras 不是 OTP，也不是先设密码。
- Cerebras 当前是 `email-first + magic-link + onboarding + plan selection`。
- 因此如果后续要做 `platforms/cerebras`，邮箱回调应优先提取激活 URL，而不是先围绕 6 位验证码设计。

## 真实入口与网络边界

### 1. 官网入口

从 `https://www.cerebras.ai/` 首页可确认：

- 开发者入口是 `https://cloud.cerebras.ai/`
- 聊天试玩入口是 `https://inference.cerebras.ai/`

首页按钮文本已验证为：

- `GET STARTED` -> `https://cloud.cerebras.ai/`
- `TRY CHAT` -> `https://inference.cerebras.ai/`

### 2. 当前出口直连会被地区封禁

当前环境下，以下站点直连都被 Cloudflare `Error 1009` 拒绝：

- `https://cloud.cerebras.ai/`
- `https://chat.cerebras.ai/`（`inference.cerebras.ai` 会跳这里）

页面原文可见：

- `The owner of this website ... has banned the country or region your IP address is in (CN)`

### 3. 已验证可用的代理边界

仓库当前环境里，`socks5://127.0.0.1:1080` 可以拿到 `cloud.cerebras.ai` 的真实页面；直连与 `http://127.0.0.1:7890` 都仍是 Cloudflare 拒绝页。

这意味着：

- 后续实现必须允许 Cerebras 平台走非 CN 出口。
- 只用本机默认出口无法完成真实注册链路。

## 已证实的注册链路

### 1. 登录/注册首屏

通过带代理的真实浏览器访问 `https://cloud.cerebras.ai/` 后，首屏是站内邮箱表单，而不是立即跳第三方 OAuth 页面。

已验证可见元素：

- 标题文案：`World's Fastest Inference`
- 区块文案：`Sign Up or Log In`
- 输入框：`Email`
- 主按钮：`CONTINUE WITH EMAIL`
- 第三方登录：`GOOGLE`、`GITHUB`

页面同时声明：

- `This site is protected by reCAPTCHA`

网络上也实测到了：

- `https://www.google.com/recaptcha/enterprise.js?...`
- `POST /api/auth/recaptcha`

结论：

- Cerebras 目前是浏览器表单 + 隐式 reCAPTCHA 保护，不是纯 API 表单。

### 2. 提交邮箱后的认证方式

输入新邮箱并点击 `CONTINUE WITH EMAIL` 后，页面会进入：

- `Check your email`
- `We've sent a sign-in link to <email>. Click the link to login or sign up.`

同步抓到的关键请求：

- `GET /api/auth/check-sso?email=...`
- `GET /api/auth/csrf`
- `POST /api/auth/signin/nodemailer`
- `GET /api/auth/providers`
- `GET /api/auth/session`

结论：

- 当前 Cerebras 不是邮箱 OTP。
- 当前 Cerebras 使用 `NextAuth + nodemailer` 风格的 magic-link 邮件登录。

## 邮件激活链接

### 1. 邮件主题

用仓库现有 `duckmail` provider 实测收到的认证邮件主题为：

- `Sign in to Cerebras Link`

### 2. 邮件正文

邮件正文明确写了：

- `Click the button below to securely sign in`
- `This link will expire in 15 minutes for security reasons`

### 3. 激活链接形态

正文里真实可提取到的目标链接模式是：

```text
https://cloud.cerebras.ai/auth/magic-link?callbackUrl=https%3A%2F%2Fcloud.cerebras.ai%2F&token=<redacted>&email=<redacted>
```

这就是后续实现应该提取的主链接，不应误取正文里品牌站点 `https://cerebras.ai/` 或邮件追踪/退订链接。

### 4. 链接点击后的下一步

打开 magic link 后，不是直接完成登录，而是先进入一个确认页：

- 页面路径：`/auth/magic-link?...`
- 页面主文案：`Complete Sign-in`
- 需要再次点击：`Continue`

然后才会打到：

- `GET /api/auth/callback/nodemailer?...`

结论：

- Cerebras 的邮箱验证链路至少有两步：
  1. 收到邮件中的 magic link
  2. 打开后再点一次 `Continue`

## 首次登录后的 onboarding

### 1. 第一步：填写资料

点击 `Continue` 后，首次新账号会进入：

- `/platform/org_<id>/onboarding`

已验证字段：

- `Full Name *`
- `What best describes your use case? *`

可选 use case：

- `Hobbyist`
- `Student`
- `Startup`
- `Enterprise`

### 2. 第二步：选择套餐

提交基本资料后，同一路由会变为套餐选择视图：

- `Build with Cerebras`
- `Free $0`
- `Pay as You Go $10+`
- `Cerebras Code $50+ /month`
- `Subscriptions $1500+ /month`

已验证交互：

- `Skip →`
- `Get Started`（Free）

## 邮箱域差异：`duckmail.sbs` 被 ban，但 `cfworker` 自有域可成功进入控制台

这一段是本次调研最重要的 owner 边界。

### 1. 负样本：`duckmail.sbs` 会被 ban，组织被置为 disabled

使用仓库当前可自动化收信的 `duckmail.sbs` 邮箱时，无论：

- 选择 `Skip`
- 还是选择 `Free $0 -> Get Started`

最终都会落到：

- `/platform/org_<id>/get-started?onboarding=true`

页面报错：

- `This organization does not exist. Please check your URL.`

这不是“页面没刷新”级别的假象，后台状态已被请求证据证明为不可用。

抓到的真实请求与响应：

```json
POST /api/emailban
{"email":"<redacted>@duckmail.sbs"}

200
{"isBanned":true,"emailDomain":"duckmail.sbs"}
```

抓到的 GraphQL 关键字段：

```json
{
  "id": "org_<id>",
  "name": "Personal",
  "organizationType": "PERSONAL",
  "state": "DISABLED",
  "isProjectsEnabled": false
}
```

同时还能看到：

- `GetMyRole -> "DISABLED"`
- `GetMyPagesAccess -> dashboard/settings/billing/... 全部 "DISABLED"`
- `ListMyProjects -> []`

因此这里的真实 owner 是：

- disposable 邮箱域命中 ban 规则
- 组织状态被置为 `DISABLED`
- 项目列表为空
- 页面权限整体不可用

结论：

- `duckmail.sbs` 这类临时邮箱当前无法走通到可用控制台。

### 2. 正样本：使用你配置好的 `cfworker/cftempmail` 可以成功拿到可用账号

本次继续调试时，直接使用仓库当前配置的 `cfworker`：

- `mail_provider = cfworker`
- `cfworker_api_url = https://tempmail.highkay.qzz.io`
- 已配置可用域池，实测成功落到了：
  - `highkay.com`
  - `highlu.de`

实测生成邮箱示例形态：

- `tmp******@highkay.com`
- `tmp******@highlu.de`

### 3. `cfworker` 邮件提取的实现注意点

`cfworker` 收到的 Cerebras 邮件是带 `quoted-printable` 折行的原始 MIME。

直接在 `raw` 上跑 URL 正则会得到被截断的伪链接，例如：

- `https://cloud.cerebras.ai/auth/magic-link?callbackUrl=`

而先走仓库现成的：

- `BaseMailbox._decode_raw_content(raw)`

再提取 URL，就能拿到完整 magic link：

```text
https://cloud.cerebras.ai/auth/magic-link?callbackUrl=https%3A%2F%2Fcloud.cerebras.ai%2F&token=<redacted>&email=<redacted>
```

这点对后续 `platforms/cerebras` 很关键：

- `cfworker` 路径下必须先解码 raw 邮件正文，再提取激活链接。

### 4. `cfworker` 域不会触发 ban，组织状态是 active

使用 `highkay.com` 域重跑整条链路后，成功走通：

- 邮箱提交
- magic-link 邮件收取
- `Complete Sign-in -> Continue`
- onboarding
- `Free $0 -> Get Started`

抓到的真实请求与响应：

```json
POST /api/emailban
{"email":"<redacted>@highkay.com"}

200
{"isBanned":false,"emailDomain":"highkay.com"}
```

同时 `ListMyProjects` / `GetOrganization` 已证明组织处于可用状态：

```json
{
  "id": "org_<id>",
  "name": "Personal",
  "state": "ACTIVE",
  "organizationType": "PERSONAL",
  "isEnabledProjects": false
}
```

并且：

- `ListMyProjects` 返回了当前 org
- 自动生成了 `Default Project`
- `role = ADMIN`

这里还有一个重要边界：

- `GetMyPagesAccess` 仍返回很多 `DISABLED`
- 但这并不阻止个人账号进入 `get-started`、`playground`、`apikeys` 等真实页面

因此 `GetMyPagesAccess` 不是当前个人免费流是否可用的 owner。

## API Key 页面与控制台落点

本次已用 `cfworker/highkay.com` 真实进入可用控制台，因此这里不再只是文档推断，而是 live 页面 + live GraphQL 双重证据。

### 1. 官方文档证明

官方文档明确写明：

- API key 需要在 Cloud Console 创建和管理。
- Quickstart 指引用户进入控制台后到左侧导航的 `API Keys`。

官方文档中的直接表述包括：

- `Create and manage API keys from our Inference Cloud Console.`
- `Please visit this link and navigate to “API Keys” on the left nav bar.`

此外，项目文档还证明：

- `API Keys` 是控制台的一等页面，与 `Playground`、`Limits`、`Analytics`、`Members`、`Audit Logs`、`Settings` 同级。
- 每个 API key 归属于恰好一个 project。

### 2. 管理 API key 的文档证明

Management API 文档进一步给出了：

- `Management API keys` 位于控制台 `API keys` 页面
- 需要 `org_name`
- 管理 API 走：

```text
https://api.cerebras.ai/management/v1/orgs/{org_name}/...
```

这说明控制台上的 `API keys` 页面不仅管理普通 inference key，还承载 management key 入口。

### 3. 前端 bundle 证明的路由

在本次抓到的 `onboarding` / `apikeys` 客户端 bundle 中，已看到路径切换逻辑会把 personal org 的 `get-started` / `playground` 导向：

- `apikeys`

结合当前控制台 URL 结构，可以合理推断正常用户的 API key 页面路由是：

```text
/platform/{organizationId}/apikeys
```

这条结论现在已经由 live 页面直接证实，而不只是 bundle 推断。

## 当前已证实的 API key 结论

### 1. 新账号在成功完成 `Free` 路径后会自动拿到默认 key

使用 `highkay.com` 成功链路后，落地页是：

```text
/platform/{organizationId}/get-started
```

页面可见：

- `API key`
- `COPY API KEY`
- `VIEW ALL API KEYS`

也就是说，对新成功账号而言，平台会自动准备一个默认可用 key，不需要用户先手动创建第一把 key。

### 2. `/apikeys` 页面已实测可打开

真实页面路径：

```text
/platform/{organizationId}/apikeys
```

页面文案已验证为：

- `API keys`
- `These API keys allow you programmatic access to the Cerebras platform.`
- `GENERATE API KEY`

列表里已看到默认记录：

- `Default Key`
- `ACTIVE`

### 3. 页面加载时就会拉取完整 key 列表

抓到的真实 GraphQL：

```json
ListOrganizationApiKeys(organizationId=<org_id>)
```

返回字段已验证包含：

- `id`
- `name`
- `secretKey`
- `projectId`
- `projectName`
- `state`
- `createdAt`
- `lastUsedAt`

已验证当前成功账号的首个 key 形态是：

```json
{
  "name": "Default Key",
  "secretKey": "<redacted>",
  "projectName": "Default Project",
  "state": "ACTIVE"
}
```

这说明：

- 平台默认 project 是 `Default Project`
- 默认 inference key 在 API 页面查询时会直接返回完整 `secretKey`
- `get-started` 页面上的 `COPY API KEY` 不是伪按钮，它背后对应的是这把真实 key

### 4. 手动创建 key 的弹窗流程已验证，但本次未点最终创建

点击 `/apikeys` 页的 `GENERATE API KEY` 后，真实弹窗为：

- `Create new Cerebras API key`
- `This API key will be tied to your organization, and will share quota with other API keys in this organization.`
- 字段：`Key name`
- 按钮：`CANCEL` / `CREATE`

因此手动创建的最小 UI 流程已经清楚：

1. 打开 `/platform/{organizationId}/apikeys`
2. 点击 `GENERATE API KEY`
3. 填 `Key name`
4. 点击 `CREATE`

本次没有点最后的 `CREATE`，因为当前账号已经自动带有一把 `Default Key`，继续生成第二把 key 会造成不必要的账户污染。

## 这次仍然没有拿到的东西

以下内容本次仍未主观补全：

- 未执行手动 `CREATE` 按钮，因此没有抓到“新增第二把 key”的 mutation 名称和请求体。
- 未验证 `Management API keys` 是否与普通 inference key 共用同一弹窗，还是另有单独入口。

## 对后续 `platforms/cerebras` 的实现建议

如果后续要正式接入本仓库，当前最合理的实现顺序是：

1. 浏览器优先，不做 protocol-first。
2. 强制支持非 CN 出口代理。
3. 邮箱回调实现按 magic-link 提取，不按 OTP。
4. 页面步骤按以下状态机建模：
   - cloud landing
   - email submit
   - wait for sign-in mail
   - open magic link
   - complete sign-in
   - enter details
   - choose plan
   - open `apikeys`
   - create key
5. `cfworker` 路径下的 magic-link 提取必须先对 `raw` 做 `_decode_raw_content(...)`，否则会把链接截断成无效 `callbackUrl=`.
6. 邮箱域质量必须纳入 owner 判断：
   - `duckmail.sbs` 当前会直接触发 `emailban`
   - `highkay.com` / `highlu.de` 这类自有域可真实进入控制台
7. 新账号成功后优先先读默认 `Default Key`，再决定是否需要额外点击 `GENERATE API KEY`。

## 下一步验证建议

如果目标是把 `Cerebras` 真正做成可注册平台，当前优先级如下：

1. 固定走非 banned 邮箱域，例如当前已验证可用的 `cfworker/highkay.com` 类域名。
2. 保持 `socks5://127.0.0.1:1080` 或其他非 CN 出口。
3. 重新走完整条链路，重点补齐：
   - 手动点击 `CREATE` 时的实际 GraphQL / fetch 请求体
   - 第二把 key 的返回结构与页面呈现
   - 是否还存在 `Management API keys` 的单独生成按钮/弹窗

当前最准确的结论是：

- Cerebras 的注册链路已被证明是 magic-link 型；
- `duckmail.sbs` 会被 ban，但你配置好的 `cfworker` 自有域可以成功走通；
- 成功新账号在 `Free` 路径后会自动拿到 `Default Project` 和 `Default Key`；
- `/platform/{organizationId}/apikeys` 页面与 `GENERATE API KEY` 弹窗都已被真实验证；
- 当前唯一还没抓到的只是“手动新增第二把 key”那一下的最终创建请求。
