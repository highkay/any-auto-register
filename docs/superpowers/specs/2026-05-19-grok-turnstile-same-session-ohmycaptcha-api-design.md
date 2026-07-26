# Grok Turnstile Same-Session OhMyCaptcha API Design

## 背景

当前 `any-auto-register` 的 Grok 注册链路在 Step5 会进入 `accounts.x.ai/sign-up?redirect=grok-com` 下的同会话 Turnstile 页面。

已验证的事实是：

1. **当前页面内确实存在 Turnstile 组件**
   - 有 `input[name="cf-turnstile-response"]`
   - 有 `challenges.cloudflare.com` iframe URL
   - sitekey 可从 frame URL 中提取为 `0x4AAAAAAAhr9JGVDZbrZOo0`

2. **直接重新访问同一个 URL 不能复现 Step5**
   - 新开浏览器访问 `https://accounts.x.ai/sign-up?redirect=grok-com`
   - 看到的是 Step1 的 “使用邮箱注册” 页面
   - 没有 Turnstile iframe，没有 token 输入

3. **当前页面内点击链路没有推进状态**
   - `headless` 与 `headed` 两种模式都已实测
   - 点击前后 `cf-turnstile-response` token 长度始终为 `0`
   - Turnstile frame body/html 为空
   - 页面正文无变化

4. **当前 `ohmycaptcha` 的 `TurnstileTaskProxyless` API 本身没有坏**
   - 对公开 demo `https://react-turnstile.vercel.app/basic`
   - 同样的 `createTask/getTaskResult` 能正常返回 `XXXX.DUMMY.TOKEN.XXXX`

5. **当前 `ohmycaptcha` 的 Turnstile 模型不适合 Grok Step5**
   - 现有实现是：
     - 接收 `websiteURL + websiteKey`
     - 自己新建浏览器上下文
     - `goto(websiteURL)`
     - 在新页面里等 token
   - 这对“注册中间态 / 同会话后置页面”天然不成立

因此，问题不是现有 `TurnstileTaskProxyless` JSON 调错了，而是**任务模型边界不对**。

## 目标

- 为 `ohmycaptcha` 增加一种**同会话 Turnstile 求解**能力。
- 保持现有 `TurnstileTaskProxyless` 语义不变，不破坏已有使用方。
- 让 `any-auto-register` 可以在 Grok Step5 发生“页面状态停滞”时，把当前浏览器会话状态交给 `ohmycaptcha` 兜底。
- 输出足够可诊断的结果，避免再次出现“看起来一直卡住”的黑盒体验。

## 非目标

- 不修改现有 `TurnstileTaskProxyless` 的兼容语义。
- 不要求 `ohmycaptcha` 在第一版就支持任意远程附着到 caller 的真实 Playwright Page。
- 不要求第一版解决所有基于 JS 内存态的极端网站。
- 不在第一版把 Grok 特判硬编码进 `ohmycaptcha`。

## 结论

推荐走 **“修改 `ohmycaptcha` API，新增同会话任务类型”**，不推荐把当前 Grok 点击逻辑直接移植到 `any-auto-register` 或复制到 `ohmycaptcha` 里做站点特判。

原因：

1. `ohmycaptcha` 本来就是验证码求解边界，能力应该沉淀在那里。
2. `any-auto-register` 只负责平台主流程和状态采集，不应该长期承担 solver 特化演进。
3. 同类问题未来不止 Grok 会遇到，其他站点也可能需要“中间态页面 + 会话恢复”。
4. API 扩展可以保持旧调用方不受影响，演进风险最小。

## 方案对比

### 方案 A：新增 `TurnstileTaskSessionProxyless`（推荐）

思路：
- caller 把当前页面的**浏览器会话状态**发送给 `ohmycaptcha`
- `ohmycaptcha` 在自己的浏览器里恢复该状态
- 再用恢复后的上下文访问同一 URL 并求解 Turnstile

优点：
- 保持 `createTask / getTaskResult` 异步风格
- 不需要 caller 把自己的真实浏览器直接暴露给 `ohmycaptcha`
- API 边界清晰，适合服务化沉淀
- 对现有 `ohmycaptcha` 架构改动最自然

缺点：
- 如果目标站严重依赖当前 JS 运行时内存态，仅恢复 cookies/storage 仍可能不够

### 方案 B：新增“附着现有浏览器页面”的任务类型（备选 Phase 2）

思路：
- caller 暴露一个临时 browser attach 能力
- `ohmycaptcha` 直接操作 caller 当前页，而不是自己重建上下文

优点：
- 语义最强，最接近“真正的同页面求解”
- 对依赖强会话内存态的网站成功率可能更高

缺点：
- 边界更复杂
- 对浏览器启动方式、端口、生命周期、安全约束要求更高
- `any-auto-register` 和 `ohmycaptcha` 两边都要改较多

### 方案 C：把求解代码直接移植到 `any-auto-register`（不推荐）

优点：
- 短期最省跨仓库协调

缺点：
- 能力分叉
- solver 演进无法复用
- 未来其他项目还会重复实现
- 当前问题本质上是 captcha service 能力缺失，不是平台注册器职责

## 推荐方案细节

推荐先做 **方案 A：`TurnstileTaskSessionProxyless`**。

如果 Phase 1 证明“仅恢复 cookies/storage 仍不足以还原 Grok Step5”，再进入 Phase 2 增加“附着现有页面”的能力。

## API 设计

### 新任务类型

新增任务类型：

- `TurnstileTaskSessionProxyless`

命名原则：
- 保留 `TurnstileTask...` 族的一致性
- 明确它不是纯 URL 级任务，而是带 session restore 的任务

### createTask 请求

```json
{
  "clientKey": "sk-xxx",
  "task": {
    "type": "TurnstileTaskSessionProxyless",
    "websiteURL": "https://accounts.x.ai/sign-up?redirect=grok-com",
    "websiteKey": "0x4AAAAAAAhr9JGVDZbrZOo0",
    "sessionState": {
      "cookies": [
        {
          "name": "cookie_name",
          "value": "cookie_value",
          "domain": "accounts.x.ai",
          "path": "/",
          "httpOnly": true,
          "secure": true,
          "sameSite": "Lax",
          "expires": 1779999999
        }
      ],
      "origins": [
        {
          "origin": "https://accounts.x.ai",
          "localStorage": {
            "key1": "value1"
          },
          "sessionStorage": {
            "key2": "value2"
          }
        }
      ],
      "userAgent": "Mozilla/5.0 ...",
      "viewport": {
        "width": 1400,
        "height": 1200
      },
      "locale": "zh-CN",
      "timezoneId": "Asia/Shanghai"
    },
    "widgetHints": {
      "responseInputSelector": "input[name=\"cf-turnstile-response\"]",
      "frameUrl": "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/...",
      "widgetBox": {
        "x": 158,
        "y": 659,
        "width": 384,
        "height": 65
      }
    },
    "runtimeHints": {
      "pageBodyText": "您正在登录 完成注册 ...",
      "stepLabel": "grok_signup_step5",
      "tokenMinLength": 20
    },
    "options": {
      "pageLoadTimeoutMs": 30000,
      "solveTimeoutMs": 90000,
      "maxAttempts": 2,
      "captureDebugArtifacts": true
    }
  }
}
```

### 必填字段

- `type`
- `websiteURL`
- `websiteKey`
- `sessionState.cookies`
- `sessionState.userAgent`

### 强烈建议字段

- `sessionState.origins`
- `sessionState.viewport`
- `widgetHints.frameUrl`
- `widgetHints.widgetBox`

### 可选字段

- `runtimeHints.pageBodyText`
- `runtimeHints.stepLabel`
- `options.captureDebugArtifacts`

## getTaskResult 响应

### 成功

```json
{
  "errorId": 0,
  "status": "ready",
  "solution": {
    "token": "0.xxx",
    "solverMode": "session_restore",
    "tokenSource": "cf-turnstile-response",
    "finalURL": "https://accounts.x.ai/sign-up?redirect=grok-com",
    "attempts": 1,
    "restoredCookieCount": 14,
    "restoredOriginCount": 1
  }
}
```

### 处理中

兼容现有风格，仍返回：

```json
{
  "errorId": 0,
  "status": "processing",
  "solution": null
}
```

可选增强：

```json
{
  "errorId": 0,
  "status": "processing",
  "solution": null,
  "debug": {
    "phase": "restoring_session",
    "attempt": 1
  }
}
```

### 失败

```json
{
  "errorId": 1,
  "status": null,
  "solution": null,
  "errorCode": "ERROR_TURNSTILE_SESSION_UNSOLVABLE",
  "errorDescription": "Session restored, but Turnstile token was not obtained within timeout"
}
```

## 新错误码建议

- `ERROR_SESSION_STATE_REQUIRED`
  - 缺少 `sessionState`
- `ERROR_SESSION_RESTORE_FAILED`
  - cookies/storage 注入失败
- `ERROR_TURNSTILE_WIDGET_NOT_RESTORED`
  - 恢复后页面中没有出现 caller 提供的 widget 特征
- `ERROR_TURNSTILE_STATE_MISMATCH`
  - 恢复后的页面明显不是 caller 所在步骤
- `ERROR_TURNSTILE_SESSION_UNSOLVABLE`
  - 会话恢复成功，但仍未拿到 token
- `ERROR_TURNSTILE_UNSUPPORTED_HINTS`
  - 请求字段语义合法，但当前 solver 版本还不支持某些 hint

## OhMyCaptcha 侧实现要求

### 1. 保持旧接口不变

`TurnstileTaskProxyless` 与 `TurnstileTaskProxylessM1` 原语义完全不变。

### 2. 新增 session restore 流程

在 `TurnstileTaskSessionProxyless` 中：

1. 启动浏览器上下文
2. 预注入 cookies
3. 对每个 origin 注入 localStorage / sessionStorage
4. 使用 caller 提供的 `userAgent / viewport / locale / timezoneId`
5. 再访问 `websiteURL`
6. 验证恢复后的页面是否出现 caller 的 widget 特征
7. 执行 Turnstile 求解
8. 从 `cf-turnstile-response` 或 `turnstile.getResponse()` 提取 token

### 3. 恢复校验

为了避免“恢复失败但还在盲解”，必须先做恢复后校验。至少校验：

- 页面 URL 是否仍在预期 origin
- `websiteKey` 是否仍能从 DOM / iframe URL 中读到
- `responseInputSelector` 是否存在
- 若 caller 提供了 `frameUrl`，frame host 是否一致

若这些前置校验不成立，应直接失败，不要进入长时间轮询。

### 4. 调试输出

建议 `ohmycaptcha` 内部记录：

- 恢复的 cookie 数量
- 恢复的 origin 数量
- 是否检测到 widget
- token 轮询次数
- 每次 attempt 的最终状态

必要时输出到服务日志，而不是全部塞进 API 响应。

## any-auto-register 侧对接设计

### 1. 触发条件

只在以下场景调用新 API：

- 平台为 `grok`
- 当前已进入 Step5
- 本地页面内点击链路判断为“状态停滞”
- 当前 solver 为 `yescaptcha` 兼容服务，且配置指向支持该新任务类型的 `ohmycaptcha`

### 2. sessionState 采集

caller 需从当前 Playwright/Patchright 上下文采集：

- `context.cookies()`
- 当前 origin 的 `localStorage`
- 当前 origin 的 `sessionStorage`
- `navigator.userAgent`
- viewport
- locale
- timezone

### 3. widgetHints 采集

caller 需补充：

- `frameUrl`
- `widgetBox`
- `responseInputSelector`
- token 最小长度

### 4. 成功后的处理

拿到 token 后仍由 `any-auto-register` 注入当前页面：

- 设置 `cf-turnstile-response`
- 触发 `input/change`
- 再走当前的提交按钮链路

不要让 `ohmycaptcha` 直接负责 Grok 注册提交。

## 安全与隐私

### 默认不发送的内容

以下内容默认不要发送给 `ohmycaptcha`：

- 完整页面 HTML
- 用户输入的密码明文
- 邮箱正文

### 允许发送的最小状态

优先发送：

- cookies
- storage
- userAgent / viewport / locale
- widget hints

### 调试开关

若需要排障，再通过 `options.captureDebugArtifacts=true` 打开更高诊断级别。

## Phase 2 预留：附着现有页面

如果 Phase 1 验证后发现：

- 恢复 cookies/storage 仍不能稳定回到 Step5
- Grok/x.ai 强依赖当前 JS 内存态

则再新增第二种任务模型：

- `TurnstileTaskAttachedSession`

其核心不是 `session restore`，而是让 `ohmycaptcha` 直接操作 caller 当前页。

这一版需要单独设计：

- attach 认证
- 生命周期
- 安全边界
- 浏览器兼容

不建议和 Phase 1 一起上。

## 验证方式

实现后至少做三类验证：

1. **正控**
   - 公开 Turnstile demo 继续通过
   - 证明旧 API 未被破坏

2. **会话恢复能力**
   - 使用 Grok Step5 的真实 `sessionState`
   - 检查恢复后页面是否出现 caller 提供的 widget 特征

3. **真实端到端**
   - `any-auto-register` 在 Step5 状态停滞后调用新任务类型
   - 返回 token 后完成当前页注入与提交

## 结论

本次推荐方案是：

1. **在 `ohmycaptcha` 新增 API，不修改旧 API 语义**
2. **第一版做 `TurnstileTaskSessionProxyless`**
3. **由 caller 负责采集当前浏览器会话状态**
4. **由 `ohmycaptcha` 负责恢复会话并求解 token**
5. **由 caller 负责把 token 注回当前页面并继续业务提交**

这条边界最清晰，也最符合两个仓库各自的职责。
