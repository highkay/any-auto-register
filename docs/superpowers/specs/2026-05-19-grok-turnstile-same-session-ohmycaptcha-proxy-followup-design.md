# Grok Turnstile Same-Session OhMyCaptcha Proxy Follow-up Design

## 背景

上一版同会话方案已经落地了 `TurnstileTaskSessionProxyless`，并完成了：

- `any-auto-register` 侧的 `sessionState / widgetHints / runtimeHints` 采集
- `ohmycaptcha` 侧的 session restore 分支
- 结构化错误透传

但当前 review 已确认一个关键边界仍未补齐：

**同会话任务恢复了 cookies / storage / UA / viewport，却没有恢复“同一出口代理”。**

对 Grok / x.ai 这种强依赖 Cloudflare / IP / 会话绑定的页面，这意味着：

- 浏览器状态可能恢复了
- 但网络身份没有恢复
- `ohmycaptcha` 看到的是“另一个客户端”
- 同会话任务很可能仍然失败

## 本文目标

把这个“任务级代理”缺口补成一份可直接实现的 follow-up spec，覆盖：

1. `ohmycaptcha` API 要加什么字段
2. `ohmycaptcha` solver 生命周期如何调整
3. `any-auto-register` 应该采集和传什么
4. 结果和错误怎么设计
5. 哪些边界属于 Phase 1.1，哪些属于后续 Phase 2

## 已确认的现状

### any-auto-register 侧

当前 Grok 在页面停滞时会触发同会话 solver：

- 采集 session state：cookies / storage / UA / viewport / locale / timezone
- 采集 widget hints：frameUrl / widgetBox / responseInputSelector
- 采集 runtime hints：pageBodyText / stepLabel / tokenMinLength

但**没有把当前注册代理传给 solver**。

### ohmycaptcha 侧

当前 `TurnstileTaskSessionProxyless` solver 会：

- 用 sessionState 重建上下文
- 重新访问 `websiteURL`
- 校验恢复后的 Turnstile 状态
- 尝试获得 token

但浏览器只会使用：

- 进程级全局 `browser_proxy`

不会使用：

- caller 当前任务的专属代理

### 风险结论

这意味着当前 Phase 1 实现更接近：

- “恢复页面状态”

而不是：

- “恢复页面状态 + 恢复同一网络身份”

对 Cloudflare / x.ai，这两者不是一回事。

## 结论

下一步应该做 **Phase 1.1：任务级代理透传**。

这是当前最优先、最合理的补丁点。

如果不先补这一层，就算 session restore 逻辑都对，也很可能永远达不到真实 Grok Step5 的可用率。

## 方案结论

推荐：

1. **保留现有 `TurnstileTaskSessionProxyless`**
2. **给它增加可选 `browserProxy` 字段**
3. **同会话任务改为“按任务启动浏览器”**
4. **任务级代理优先于服务级 `browser_proxy`**

不推荐现在就重命名任务类型，原因是：

- 现有实现已经合并
- 改字段比改任务类型影响更小
- 服务里原本就已经允许“Proxyless 任务 + 服务全局代理”的语义

但是文档必须明确写清楚：

> 这里的 `Proxyless` 是“caller 不需要自己代理转发 HTTP 请求到 solver” 的语义，  
> 不是 “solver 运行时绝对不能使用代理”。

## API 边界

### caller 负责

`any-auto-register` 负责：

1. 决定何时触发同会话 solver
2. 采集页面会话状态
3. 采集当前任务实际使用的代理
4. 调用 `ohmycaptcha`
5. 把返回 token 注回 caller 当前页面
6. 继续走平台注册的后续提交链路

### ohmycaptcha 负责

`ohmycaptcha` 负责：

1. 校验 payload
2. 用 caller 提供的代理启动 solver 浏览器
3. 恢复 sessionState
4. 校验恢复后的 widget 是否合理
5. 获取 token
6. 返回结构化结果或结构化错误

### 明确不做的事

`ohmycaptcha` 不负责：

- 直接提交 Grok 表单
- 提取最终 `sso` cookie
- 管理 caller 的邮箱、OTP、账号状态
- 复用 caller 当前真实 Playwright Page 对象

## API 变更

### 现有任务类型

保留：

- `TurnstileTaskSessionProxyless`

### 新增字段

在 `task` 根级新增：

```json
{
  "browserProxy": {
    "server": "socks5://192.168.1.18:1080",
    "username": "proxy-user",
    "password": "proxy-pass",
    "bypass": ".internal.example,localhost"
  }
}
```

### 字段位置

推荐放在 `task.browserProxy`，不要塞进：

- `sessionState`
- `options`
- `runtimeHints`

原因：

- `sessionState` 表达的是“页面/浏览器状态”
- `browserProxy` 表达的是“solver 执行环境”
- 二者职责不同

## createTask 请求 spec

### 完整示例

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
          "name": "cf_clearance",
          "value": "cookie-value",
          "domain": "accounts.x.ai",
          "path": "/",
          "httpOnly": true,
          "secure": true,
          "sameSite": "Lax"
        }
      ],
      "origins": [
        {
          "origin": "https://accounts.x.ai",
          "localStorage": {
            "signup_flow": "grok"
          },
          "sessionStorage": {
            "step": "5"
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
    "browserProxy": {
      "server": "socks5://192.168.1.18:1080"
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

### 字段定义

#### 必填

- `type`
- `websiteURL`
- `websiteKey`
- `sessionState.cookies`
- `sessionState.userAgent`

#### 强烈建议

- `sessionState.origins`
- `sessionState.viewport`
- `widgetHints.responseInputSelector`
- `widgetHints.frameUrl`
- `browserProxy.server`

#### 可选

- `browserProxy.username`
- `browserProxy.password`
- `browserProxy.bypass`
- `options.captureDebugArtifacts`

## browserProxy 字段 spec

### 结构

```json
{
  "server": "socks5://host:port",
  "username": "optional-user",
  "password": "optional-pass",
  "bypass": ".internal.example,localhost"
}
```

### 约束

- `server`:
  - 必须是非空字符串
  - 由 caller 预先规范化到 Playwright 可接受格式
  - 推荐直接传 `build_playwright_proxy_config(...)` 的结果等价结构

- `username`:
  - 可选
  - 若提供必须为非空字符串

- `password`:
  - 可选
  - 若提供必须为字符串

- `bypass`:
  - 可选
  - 若提供必须为字符串

### 优先级

浏览器代理优先级：

1. `task.browserProxy`
2. `ohmycaptcha` 服务级 `browser_proxy`
3. 无代理直连

## API 校验规则

新增校验：

### browserProxy 校验

- `browserProxy` 存在时，`browserProxy.server` 必须存在
- `browserProxy.server` 必须为非空字符串
- `browserProxy.username` 如果存在，必须为非空字符串
- `browserProxy.password` 如果存在，必须为字符串
- `browserProxy.bypass` 如果存在，必须为字符串

### sessionState 与 browserProxy 的关系

不强制要求一定带 `browserProxy`，因为：

- 某些调用方本身就是直连
- 某些调用方可能只依赖服务级 `browser_proxy`

但对于像 Grok 这种 caller 已知使用任务级代理的场景，业务侧应主动传。

## getTaskResult 响应 spec

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
    "restoredOriginCount": 1,
    "proxyMode": "task",
    "proxyServer": "socks5://192.168.1.18:1080"
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
  "errorDescription": "Session restored with caller proxy, but Turnstile token was not obtained within timeout"
}
```

## 新增返回元数据

建议在 `solution` 中新增：

- `proxyMode`
  - `task`
  - `service_default`
  - `none`

- `proxyServer`
  - 最终生效的代理地址
  - 不回传用户名密码

## 错误码

在上一版错误码基础上，建议新增：

- `ERROR_BROWSER_PROXY_INVALID`
  - `browserProxy` 结构非法

- `ERROR_BROWSER_PROXY_LAUNCH_FAILED`
  - 带任务级代理启动浏览器失败

- `ERROR_BROWSER_PROXY_REQUIRED_BY_CALLER`
  - caller 明确标记需要同代理，但请求里没带代理
  - 这项可选，不是第一版必须

## solver 生命周期设计

### 关键事实

当前 `TurnstileSolver` 是单例 `_browser`：

- 启动一次
- 复用同一个 browser
- 代理来自全局配置

这与“任务级代理”天然冲突，因为 Chromium 的代理通常是 **launch-time** 级别，而不是 context 级别。

### 结论

对 `TurnstileTaskSessionProxyless`：

**不要复用全局 `_browser`。**

### 推荐实现

对 session task 采用：

- **按任务启动独立浏览器**
- 任务结束立即关闭

理由：

- 正确性优先
- 任务级代理实现简单
- 避免不同代理任务共用一个 browser 造成污染

### 具体逻辑

#### 普通 Turnstile 任务

仍可继续复用现有 `_browser`：

- `TurnstileTaskProxyless`
- `TurnstileTaskProxylessM1`

#### 同会话任务

`TurnstileTaskSessionProxyless` 改为：

1. 根据 `task.browserProxy` 生成 launch kwargs
2. 启动临时 browser
3. 在该 browser 里恢复 session
4. 求解后关闭临时 browser

### 伪代码

```python
async def _solve_session_once(...):
    browser = await self._launch_browser_for_session_task(browser_proxy)
    try:
        context = await browser.new_context(...)
        await self._restore_session_state(context, session_state)
        page = await context.new_page()
        await goto_solver_page(page, website_url, timeout)
        await self._validate_restored_turnstile_state(...)
        token = await self._obtain_turnstile_token(...)
        return solution
    finally:
        await browser.close()
```

## any-auto-register 对接逻辑

### 代理来源

`any-auto-register` 不应重新推导代理，而应直接复用当前 Grok 注册器已经在用的代理：

- `self.proxy`

### 采集逻辑

新增一个明确的采集函数：

```python
def _collect_turnstile_solver_proxy(self) -> dict[str, Any] | None:
    if not self.proxy:
        return None
    return build_playwright_proxy_config(self.proxy)
```

注意：

- 这里要用和当前注册浏览器一致的规范化逻辑
- 不要直接把未经处理的原始代理字符串传给 `ohmycaptcha`

### 调用逻辑

当前：

- 页面状态停滞 -> 调 `solve_turnstile_session(...)`

应改为：

- 页面状态停滞
- 采集 `sessionState`
- 采集 `widgetHints`
- 采集 `runtimeHints`
- 采集 `browserProxy`
- 调 `solve_turnstile_session(...)`

### 业务规则

建议：

- 若当前 Grok 任务本身使用了代理，则同会话 solver 调用时必须带 `browserProxy`
- 若当前任务直连，则不传 `browserProxy`

## OhMyCaptcha 内部逻辑

### 1. 新增模型

在 `TaskObject` 上新增：

```python
class BrowserProxy(BaseModel):
    server: str
    username: str | None = None
    password: str | None = None
    bypass: str | None = None
```

并加到：

```python
browserProxy: BrowserProxy | None = None
```

### 2. 路由校验

`_validate_turnstile_session_task(...)` 增加：

- `browserProxy.server` 非空校验
- 字段类型校验

### 3. session task 专用浏览器

新增：

```python
async def _launch_browser_for_session_task(self, browser_proxy: dict[str, Any] | None):
    launch_kwargs = {
        "headless": self._config.browser_headless,
        "args": [...],
    }
    effective_proxy = browser_proxy or self._service_default_proxy()
    if effective_proxy:
        launch_kwargs["proxy"] = effective_proxy
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(**launch_kwargs)
    return playwright, browser
```

### 4. 结果元数据

成功时记录：

- `proxyMode`
- `proxyServer`

失败日志中记录：

- 是否使用任务级代理
- 生效的 proxy server

## 调试与日志

建议 session task 日志至少包含：

- `proxyMode=task/service_default/none`
- `proxyServer=...`
- `restoredCookieCount`
- `restoredOriginCount`
- `sitekeyMatched=true/false`
- `responseInputPresent=true/false`
- `frameHostMatched=true/false`

这样未来出现失败时，可以立即区分：

1. 会话恢复问题
2. 代理不一致问题
3. widget 不存在问题
4. token 轮询问题

## 验证方案

### 单元测试

#### ohmycaptcha

应新增：

1. `browserProxy` 校验测试
2. session task 使用 `task.browserProxy` 启动浏览器测试
3. `proxyMode` / `proxyServer` 返回值测试
4. 无 `browserProxy` 时回退全局 `browser_proxy` 测试

#### any-auto-register

应新增：

1. Grok 同会话 solver payload 带上 `browserProxy`
2. 代理字符串被规范化后再传输
3. 无代理任务不传 `browserProxy`

### 集成验证

至少做两条：

1. **正控**
   - 公开 Turnstile demo
   - 带 task-level proxy 的 session task 仍能正常返回 token

2. **真实 Grok Step5**
   - 当前任务使用 `socks5h://...`
   - 页面状态停滞后触发同会话 solver
   - `ohmycaptcha` 日志中应能看到 `proxyMode=task`
   - 最终确认是否拿到 token

## 边界说明

### Phase 1.1 解决的问题

- 会话状态恢复不带同代理的问题
- 任务级代理与服务级代理混淆的问题
- `ohmycaptcha` session task 共享全局 browser 的结构问题

### Phase 1.1 不解决的问题

- caller 当前 JS heap / React 内存态无法复用
- Patchright / Chromium 指纹仍可能不同
- Cloudflare 可能仍基于更深层行为特征阻断

### 如果 Phase 1.1 后仍不够

才进入 Phase 2：

- `TurnstileTaskAttachedSession`
- 直接附着 caller 当前页

## 最终建议

当前 follow-up 的优先级是：

1. **先补任务级代理透传**
2. **把 session task 改成按任务独立启动浏览器**
3. **补成功返回里的代理元数据**
4. **做一次真实 Grok Step5 端到端验证**

在这四步完成前，不建议把当前 Phase 1 实现视为“已解决 Grok 同会话 Turnstile”。
