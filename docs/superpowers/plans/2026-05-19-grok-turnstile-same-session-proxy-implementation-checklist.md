# Grok Turnstile Same-Session Proxy Implementation Checklist

## 目标

把 `TurnstileTaskSessionProxyless` 从“仅恢复 sessionState”推进到“恢复 sessionState + 恢复同一任务代理”。

本清单是执行层文档，默认上一份设计稿已经确认：

- [2026-05-19-grok-turnstile-same-session-ohmycaptcha-api-design.md](</F:/git/any-auto-register/docs/superpowers/specs/2026-05-19-grok-turnstile-same-session-ohmycaptcha-api-design.md:1>)
- [2026-05-19-grok-turnstile-same-session-ohmycaptcha-proxy-followup-design.md](</F:/git/any-auto-register/docs/superpowers/specs/2026-05-19-grok-turnstile-same-session-ohmycaptcha-proxy-followup-design.md:1>)

## 结论先行

这一轮真正要做的，不是再改 Grok 点击策略，而是补齐这条链：

1. `any-auto-register`
   - 采集当前 Grok 任务实际使用的代理
   - 把代理和 session state 一起发给 `ohmycaptcha`

2. `ohmycaptcha`
   - 接受 `task.browserProxy`
   - session task 改成按任务独立启动 browser
   - 优先使用任务级代理而不是服务全局代理

3. 验证
   - 不是只跑单测
   - 必须做一次真实 Grok Step5 端到端验证

---

## Part A: ohmycaptcha 改动清单

### A1. 数据模型

文件：

- `F:\git\ohmycaptcha\src\models\task.py`

要做的事：

1. 新增 `BrowserProxy` model

建议结构：

```python
class BrowserProxy(BaseModel):
    model_config = ConfigDict(extra="allow")

    server: str
    username: str | None = None
    password: str | None = None
    bypass: str | None = None
```

2. 在 `TaskObject` 上增加：

```python
browserProxy: BrowserProxy | None = None
```

注意：

- 不要把代理字段放进 `sessionState`
- 不要把代理字段塞进 `options`
- 它是 solver 执行环境，不是页面状态

### A2. API 校验

文件：

- `F:\git\ohmycaptcha\src\api\routes.py`

要做的事：

在 `_validate_turnstile_session_task(...)` 里增加 `browserProxy` 校验：

1. `browserProxy` 存在时：
   - `server` 必须存在
   - `server` 必须是非空字符串

2. `username` 如果存在：
   - 必须是非空字符串

3. `password` 如果存在：
   - 必须是字符串

4. `bypass` 如果存在：
   - 必须是字符串

建议错误码：

- `ERROR_BROWSER_PROXY_INVALID`

建议错误描述：

- `browserProxy.server is required when browserProxy is provided`
- `browserProxy.username must be a non-empty string when provided`

### A3. Session Task 浏览器生命周期重构

文件：

- `F:\git\ohmycaptcha\src\services\turnstile.py`

当前问题：

- `_browser` 是服务级单例
- 代理来自 `self._config.browser_proxy`
- 这不适合任务级代理

要做的事：

1. 保留普通任务：
   - `TurnstileTaskProxyless`
   - `TurnstileTaskProxylessM1`
   - 仍可复用全局 `_browser`

2. 对 `TurnstileTaskSessionProxyless`：
   - 不复用 `_browser`
   - 按任务启动独立 `browser`
   - 任务结束立即关闭

需要新增的函数建议：

```python
async def _launch_browser_for_session_task(
    self,
    browser_proxy: dict[str, Any] | None,
) -> tuple[Playwright, Browser, str, str | None]:
    ...
```

建议返回：

- `playwright`
- `browser`
- `proxy_mode`
- `proxy_server`

`proxy_mode` 取值：

- `task`
- `service_default`
- `none`

优先级：

1. `task.browserProxy`
2. `self._config.browser_proxy`
3. none

### A4. 把 browserProxy 传进 session task

文件：

- `F:\git\ohmycaptcha\src\services\turnstile.py`

具体改动点：

1. `_solve_session_task(...)`
   - 从 `params` 读取 `browserProxy`

2. `_solve_session_once(...)`
   - 不再使用全局 `_browser`
   - 改为使用 `_launch_browser_for_session_task(...)`

3. 返回结果里增加：

```python
{
    "proxyMode": proxy_mode,
    "proxyServer": proxy_server,
}
```

注意：

- `proxyServer` 可以返回 host/port 形式
- 不要回传用户名密码

### A5. 日志

文件：

- `F:\git\ohmycaptcha\src\services\turnstile.py`

建议新增日志：

1. session task 启动时：

```text
Turnstile session task launching browser (proxy_mode=task, proxy_server=socks5://...)
```

2. session restore 成功时：

```text
Turnstile session restored (cookies=14, origins=1, proxy_mode=task)
```

3. widget 校验失败时：

```text
Turnstile session widget restore failed (proxy_mode=task, reason=...)
```

### A6. ohmycaptcha 测试补充

文件：

- `F:\git\ohmycaptcha\tests\test_api.py`
- `F:\git\ohmycaptcha\tests\test_turnstile.py`

必须补的测试：

1. `browserProxy` 校验通过
2. `browserProxy.server` 缺失时报错
3. session task 使用 `task.browserProxy` 启动浏览器
4. 无 `task.browserProxy` 时回退 `self._config.browser_proxy`
5. 返回 `solution.proxyMode`
6. 返回 `solution.proxyServer`

---

## Part B: any-auto-register 改动清单

### B1. 代理采集函数

文件：

- `F:\git\any-auto-register\platforms\grok\core.py`

新增函数建议：

```python
def _collect_turnstile_solver_proxy(self) -> dict[str, Any] | None:
    if not self.proxy:
        return None
    return build_playwright_proxy_config(self.proxy)
```

要求：

- 必须和当前注册浏览器使用的代理规范化逻辑一致
- 不要直接传原始代理字符串

### B2. 同会话 solver 调用点

文件：

- `F:\git\any-auto-register\platforms\grok\core.py`

函数：

- `_solve_turnstile_by_same_session_solver(...)`

当前：

- 传了 `session_state`
- 传了 `widget_hints`
- 传了 `runtime_hints`
- 没传 `browserProxy`

应改为：

```python
browser_proxy = self._collect_turnstile_solver_proxy()

solution = solve_turnstile_session(
    ...,
    session_state=session_state,
    widget_hints=widget_hints,
    runtime_hints=runtime_hints,
    browser_proxy=browser_proxy,
    options=...,
)
```

### B3. YesCaptcha 客户端签名扩展

文件：

- `F:\git\any-auto-register\core\base_captcha.py`

函数：

- `BaseCaptcha.solve_turnstile_session(...)`
- `YesCaptcha.solve_turnstile_session(...)`

要做的事：

新增参数：

```python
browser_proxy=None
```

并在 task payload 中透传：

```python
if browser_proxy:
    task["browserProxy"] = browser_proxy
```

### B4. 日志

文件：

- `F:\git\any-auto-register\platforms\grok\core.py`

建议新增日志：

1. 当同会话 solver 被触发时：

```text
页面状态停滞，调用同会话 Turnstile solver 兜底 (sitekey=0x4AAAAA..., proxy=task)
```

2. 当无代理时：

```text
页面状态停滞，调用同会话 Turnstile solver 兜底 (sitekey=0x4AAAAA..., proxy=none)
```

3. solver 返回后：

```text
同会话 solver 返回 token (mode=session_restore, attempts=1, proxyMode=task, finalURL=...)
```

### B5. any-auto-register 测试补充

文件：

- `F:\git\any-auto-register\tests\test_yescaptcha_api_base.py`
- `F:\git\any-auto-register\tests\test_grok_core.py`

必须补的测试：

1. `solve_turnstile_session(...)` payload 带 `browserProxy`
2. Grok 当前有代理时，`_collect_turnstile_solver_proxy()` 返回规范化结构
3. Grok 当前无代理时，不传 `browserProxy`
4. Grok 日志里能看到 `proxyMode`

---

## Part C: 端到端验证清单

### C1. 本地 API 正控

验证项：

1. `ohmycaptcha` 启动后，新任务 schema 可被接受
2. `browserProxy` 字段能通过校验
3. `getTaskResult` 成功时带 `proxyMode/proxyServer`

### C2. 会话恢复验证

验证项：

1. 使用真实 Grok Step5 采集到的 payload
2. 让 `ohmycaptcha` 记录：
   - `proxyMode`
   - `proxyServer`
   - `restoredCookieCount`
   - `restoredOriginCount`
3. 确认 restore 后页面至少出现：
   - 正确 `sitekey`
   - `responseInputSelector`
   - 对应 frame host

### C3. 真实 Grok 端到端

必须做：

1. 用与 caller 当前任务相同的代理
2. 跑到 Step5 停滞
3. 触发 same-session solver
4. 验证：
   - `ohmycaptcha` 日志显示 `proxyMode=task`
   - `any-auto-register` 日志收到 token
   - token 注入当前页后，提交流程是否前进

### C4. 失败分类

若仍失败，需要先分层：

1. `ERROR_BROWSER_PROXY_LAUNCH_FAILED`
   - 代理启动失败

2. `ERROR_TURNSTILE_WIDGET_NOT_RESTORED`
   - 会话恢复后 widget 不存在

3. `ERROR_TURNSTILE_STATE_MISMATCH`
   - 会话恢复后页面不是 Step5

4. `ERROR_TURNSTILE_SESSION_UNSOLVABLE`
   - 会话、代理都恢复了，但 token 仍拿不到

只有第 4 类才说明需要进入 Phase 2。

---

## Phase 2 触发条件

只有满足以下条件，才值得继续做 `TurnstileTaskAttachedSession`：

1. `task.browserProxy` 已补齐
2. session task 已按任务独立 browser 运行
3. 真实 Grok Step5 已确认 restore 后页面状态正确
4. 仍然持续 `ERROR_TURNSTILE_SESSION_UNSOLVABLE`

否则，不应跳过 Phase 1.1 直接做附着式方案。

---

## 最终执行顺序

建议严格按下面顺序推进：

1. `ohmycaptcha`:
   - 加 `browserProxy` model
   - 加 API 校验
   - session task 改为按任务启动 browser
   - 返回 `proxyMode/proxyServer`

2. `any-auto-register`:
   - 采集当前 Grok 代理
   - 透传 `browserProxy`
   - 补日志和单测

3. 验证：
   - 单测
   - API 正控
   - 真实 Grok Step5

4. 只有 Phase 1.1 证明确实还不够，再做 Phase 2
