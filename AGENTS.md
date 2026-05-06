# any-auto-register AGENTS Guide

本文件面向会在本仓库中工作的人类开发者与代理，目标是让后续工作先建立正确的系统地图，再动手修改代码。

## 1. 项目定位

`any-auto-register` 是一个多平台账号自动注册与管理系统，当前由以下几层组成：

- 后端：FastAPI 应用，负责任务编排、账号管理、配置管理、平台插件加载、邮件导入、外部服务管理、认证。
- 数据层：SQLite + SQLModel，保存账号、任务、代理、微软邮箱导入账号、配置项。
- 前端：React + TypeScript + Vite 单页应用，构建后由 FastAPI 直接托管。
- 桌面壳：Electron，生产模式下拉起打包后的后端并加载本地 Web UI。
- 自动化能力：Playwright / Camoufox / Patchright、协议请求执行器、验证码解题器、本地 Turnstile Solver。

它不是单一平台脚本集合，而是“统一任务编排层 + 可插拔平台实现 + 可插拔邮箱/导入/外部集成”的系统。

## 2. 最先阅读的文件

如果你第一次进入这个仓库，优先看这些入口文件：

- `main.py`
  - FastAPI 启动入口。
  - 生命周期内完成 `init_db()`、`load_all()`、`scheduler.start()`、`solver_manager.start_async()`。
  - 同时负责挂载 API 路由、认证中间件、前端静态资源托管。
- `core/registry.py`
  - 平台插件注册表。
  - 自动扫描 `platforms/*/plugin.py` 并通过 `@register` 注册。
- `core/base_platform.py`
  - 平台插件主接口定义。
  - `RegisterConfig`、`Account`、`BasePlatform` 都在这里。
- `api/tasks.py`
  - 核心注册任务编排入口。
  - 任务创建、持久化、并发执行、日志追加、停止/跳过控制都在这里。
- `frontend/src/pages/Accounts.tsx`
  - 当前默认 UI 下的平台管理页。
  - 同时承载按平台发起注册任务的弹窗，是 `/api/tasks/register` 的主要前端入口。
- `core/base_mailbox.py`
  - 邮箱抽象、验证码轮询、邮箱工厂 `create_mailbox()`。
- `core/db.py`
  - 核心表定义和数据库入口。
- `services/external_sync.py`
  - 注册成功后自动同步到外部系统的分发逻辑。
- `services/external_apps.py`
  - 外部应用安装、拉取、启动、停止的实际实现。
- `services/mail_imports/*`
  - 邮件导入策略接口与现有策略实现。
- `frontend/src/App.tsx`
  - 前端路由和主菜单结构。
- `frontend/src/pages/RegisterTaskPage.tsx`
  - 保留的独立注册表单实现。
  - 当前未在 `App.tsx` 路由和侧边栏中暴露，但仍可用于理解注册请求字段映射。
- `frontend/src/pages/Settings.tsx`
  - 全局配置页，覆盖邮箱、验证码、外部服务、认证、贡献系统等。
- `electron/main.js`
  - Electron 包装层入口。

## 3. 项目的主要功能

当前代码实际覆盖的主要能力如下：

- 多平台账号注册。
- 账号 CRUD、导入、导出、批量删除、批量有效性检测。
- 注册任务并发执行、实时日志、历史任务持久化、停止当前任务、跳过当前账号。
- 多种邮箱 provider 接入。
- 本地导入邮箱池并在运行时复用。
- 多种执行器模式：`protocol` / `headless` / `headed`。
- 多种验证码方案：`yescaptcha` / `local_solver` / `manual`。
- 代理池管理与代理轮询。
- 注册成功后自动回填外部系统。
- 外部服务的安装、拉起、停止与状态检测。
- 可选密码登录与 TOTP 二次验证。
- Contribution 额度/兑换码相关代理接口。

## 4. 核心运行流程

### 4.1 应用启动流程

入口在 `main.py`：

1. 输出当前 Python/Conda 运行环境信息。
2. `init_db()` 初始化 SQLite 表结构。
3. `load_all()` 自动导入所有平台插件。
4. 启动 `core.scheduler.scheduler`。
5. 启动本地 `services.solver_manager`。
6. 注册所有 API 路由。
7. 若 `static/` 存在，则挂载 SPA 静态资源并把非 API 路由回退到 `index.html`。

### 4.2 注册任务主流程

真实主链路在 `api/tasks.py::_run_register()`：

1. 当前默认 UI 主要在 `frontend/src/pages/Accounts.tsx` 的平台注册弹窗中收集表单；`RegisterTaskPage.tsx` 仍保留为独立实现参考，但当前未挂到侧边栏菜单。
2. 请求 `POST /api/tasks/register`。
3. 后端先用 `_prepare_register_request()` 补齐和修正请求。
4. 创建 `RegisterTaskRecord`，并持久化到 `task_runs`。
5. 通过 `core.registry.get(req.platform)` 找到平台插件类。
6. 合并配置：`config_store.get_all()` + 本次请求的 `extra`。
7. 选择代理：
   - 固定代理优先。
   - 否则从代理池或预取代理中取。
8. 用 `create_mailbox()` 构造邮箱 provider。
9. 构造 `RegisterConfig`，实例化平台插件。
10. 绑定任务控制器和日志函数到平台与邮箱对象。
11. 调用平台插件的 `register(email, password)`。
12. 成功后写入 `accounts` 表，必要时做邮箱域名策略校验。
13. 后台线程触发 `services.external_sync.sync_account()` 做自动回填。
14. 持续把日志、进度、错误、控制状态同步到 `task_runs`。

### 4.3 任务控制流程

任务控制定义在 `core/task_runtime.py`：

- `RegisterTaskControl.request_stop()`：停止整个任务。
- `RegisterTaskControl.request_skip_current()`：跳过当前账号。
- `checkpoint()`：平台/邮箱等待逻辑在长轮询时必须持续检查这个控制器。

当前任务控制已经贯穿到邮箱等待逻辑，因此修改平台或邮箱等待流程时，不要破坏 `checkpoint()` 的调用链。

### 4.4 平台动作流程

平台动作接口定义在 `BasePlatform.get_platform_actions()` / `execute_action()`，API 层在 `api/actions.py`：

1. 前端读取 `/api/actions/{platform}` 获取动作定义。
2. 单个账号或批量账号请求动作执行接口。
3. API 层把数据库 `AccountModel` 转成 `core.base_platform.Account`。
4. 调用平台插件 `execute_action()`。
5. 若动作返回 token / sync 状态 / extra patch，则由 `api/actions.py` 负责回写账号状态。

这层是平台级“运维动作”接口，不是注册主流程本身。

### 4.5 邮件导入流程

这是一条独立扩展链：

1. 前端 `MailImportPanel` 请求 `/api/mail-imports/providers` 获取导入策略描述。
2. 用户提交导入内容后，请求 `/api/mail-imports`。
3. API 层通过 `mail_import_registry.get(type)` 找到导入策略。
4. 策略在 `services/mail_imports/providers.py` 中执行。
5. 当前两类导入落点不同：
   - `applemail`：写本地邮箱池文件。
   - `microsoft`：写 `outlook_accounts` 数据表。

### 4.6 外部服务管理流程

“插件管理”在这个项目里有两种含义，必须区分：

- 内部平台插件：
  - 指 `platforms/*/plugin.py` 这类注册插件。
  - 由 `core.registry` 自动加载。
- 外部服务管理：
  - 指 `services/external_apps.py` 管理的外部应用，如 `cliproxyapi`、`grok2api`、`kiro-manager`。
  - 通过 `api/integrations.py` 暴露安装、启动、停止、状态接口。

不要把这两类“插件”混为一谈。

## 5. 目录与模块结构

### 5.1 顶层目录

- `api/`
  - FastAPI 路由层。
  - 包含账号、任务、平台、代理、配置、动作、集成、认证、邮件导入、贡献系统等接口。
- `core/`
  - 系统核心抽象和基础设施。
  - 平台接口、邮箱接口、执行器、验证码、数据库、配置存储、代理工具、调度器都在这里。
- `platforms/`
  - 各平台插件实现。
  - 每个平台通常至少有 `plugin.py`，复杂平台还会有 `core.py`、token/upload/switch 等子模块。
- `services/`
  - 平台无关的系统服务层。
  - 包含外部同步、外部应用管理、邮件导入、solver 管理等。
- `frontend/`
  - React 前端源码。
- `electron/`
  - Electron 桌面壳。
- `tests/`
  - 主要是 `unittest` 风格测试。
- `docs/`
  - 补充部署文档与设计文档。
- `static/`
  - 前端构建产物，由 FastAPI 直接托管。
- `mail/`
  - AppleMail 等本地邮箱池文件目录的默认落点之一。
- `_ext_targets/`
  - 外部服务源码/二进制落点，由 `services/external_apps.py` 管理。

### 5.2 API 模块

- `api/accounts.py`
  - 账号列表、详情、导入、导出、批量删除、检测。
- `api/tasks.py`
  - 注册任务创建、查询、SSE 日志、停止/跳过。
- `api/platforms.py`
  - 返回平台列表。
  - 注意当前故意过滤了 `cursor` 和 `tavily`。
- `api/actions.py`
  - 平台动作接口。
- `api/config.py`
  - 全局配置读写，`CONFIG_KEYS` 是白名单源头。
- `api/integrations.py`
  - 外部服务状态与启动/安装控制，以及回填任务。
- `api/mail_imports.py`
  - 通用邮件导入接口。
- `api/outlook.py`
  - 兼容用微软邮箱批量导入接口。
- `api/auth.py`
  - 密码登录、JWT、TOTP 2FA。
- `api/contribution.py`
  - Contribution 额度和兑换码接口代理。
- `api/proxies.py`
  - 代理池管理。

### 5.3 Core 模块

- `core/base_platform.py`
  - 平台插件主接口。
- `core/registry.py`
  - 平台插件扫描与注册。
- `core/base_mailbox.py`
  - 邮箱抽象、OTP 轮询、邮箱工厂。
- `core/base_executor.py`
  - 执行器抽象。
- `core/executors/protocol.py`
  - 纯协议执行器。
- `core/executors/playwright.py`
  - 浏览器执行器。
- `core/base_captcha.py`
  - 验证码接口与默认实现。
- `core/db.py`
  - 数据模型与数据库入口。
- `core/config_store.py`
  - 配置表读写，且会回退到环境变量和 `.env`。
- `core/task_runtime.py`
  - 任务控制与内存态任务存储。
- `core/scheduler.py`
  - 定时账号有效性检测与 CPA 维护调度。
- `core/proxy_utils.py`
  - 代理规范化、requests/playwright/mailbox 代理配置。

### 5.4 Services 模块

- `services/external_sync.py`
  - 注册成功后的外部回填分发。
- `services/external_apps.py`
  - 外部应用安装、更新、启动、停止、状态查询。
- `services/chatgpt_sync.py`
  - ChatGPT 与 CPA / Sub2API / CLIProxyAPI 状态回填。
- `services/cliproxyapi_sync.py`
  - CLIProxyAPI 状态同步。
- `services/grok2api_runtime.py`
  - Grok 相关外部运行时检查。
- `services/cpa_manager.py`
  - CPA 维护逻辑。
- `services/mail_imports/`
  - 邮件导入策略、描述模型、规则引擎。
- `services/solver_manager.py`
  - 本地 Turnstile Solver 进程管理。
- `services/turnstile_solver/`
  - Solver 实现。

### 5.5 Frontend 模块

- `frontend/src/App.tsx`
  - 路由与菜单。
- `frontend/src/pages/Dashboard.tsx`
  - 仪表盘。
- `frontend/src/pages/Accounts.tsx`
  - 账号列表与平台管理页。
  - 当前也承载各平台的注册弹窗与 `/api/tasks/register` 主入口。
- `frontend/src/pages/RegisterTaskPage.tsx`
  - 保留的独立注册页实现，当前未在 `App.tsx` 路由中暴露。
- `frontend/src/pages/RunningTasks.tsx`
  - 运行中任务页。
- `frontend/src/pages/TaskHistory.tsx`
  - 历史任务页。
- `frontend/src/pages/Proxies.tsx`
  - 代理管理。
- `frontend/src/pages/Settings.tsx`
  - 配置、导入、外部服务、认证。
- `frontend/src/components/settings/MailImportPanel.tsx`
  - 邮件导入 UI。
- `frontend/src/lib/platformExecutorOptions.ts`
  - 各平台支持的执行器矩阵。

## 6. 技术栈

- Python 3.12+
- FastAPI
- SQLModel + SQLite
- React 19
- TypeScript 5
- Vite 8
- Ant Design 5
- Electron 33
- Playwright / Camoufox / Patchright
- curl_cffi / requests / httpx
- uv / pnpm

## 7. 数据模型与状态存储

主要模型定义在 `core/db.py`：

- `AccountModel`
  - 主账号表。
  - `extra_json` 承载平台特有字段。
- `TaskLog`
  - 单次账号注册日志记录。
- `TaskRunModel`
  - 任务运行快照持久化表。
- `OutlookAccountModel`
  - 微软邮箱导入账号表。
- `ProxyModel`
  - 代理池表。
- `ConfigItem`
  - 逻辑上定义在 `core/config_store.py`，实际使用同一数据库。

关键事实：

- 配置读取顺序是：数据库配置优先，空值时回退到环境变量和 `.env`。
- 前端菜单平台列表不是平台真实全集。
- 平台特有状态一般落在 `AccountModel.extra_json`，不要随意新建散落字段。

## 8. 平台插件与扩展接口

### 8.1 平台插件主接口

接口定义位置：

- `core/base_platform.py`
- `core/registry.py`

平台插件必须满足：

- 定义类属性：
  - `name`
  - `display_name`
  - `version`
  - 可选 `supported_executors`
- 实现方法：
  - `register(self, email: str, password: str | None) -> Account`
  - `check_valid(self, account: Account) -> bool`
- 可选扩展：
  - `get_trial_url()`
  - `get_platform_actions()`
  - `execute_action()`
  - `get_quota()`

注册方式：

- 在 `platforms/<name>/plugin.py` 的平台类上使用 `@register`。
- 应用启动时 `load_all()` 会自动导入 `platforms.*.plugin`。

当前平台实现位置：

- `platforms/chatgpt/plugin.py`
- `platforms/cursor/plugin.py`
- `platforms/grok/plugin.py`
- `platforms/kiro/plugin.py`
- `platforms/openblocklabs/plugin.py`
- `platforms/qwen/plugin.py`
- `platforms/tavily/plugin.py`
- `platforms/trae/plugin.py`

各平台主注册逻辑通常在：

- `platforms/chatgpt/*`
- `platforms/cursor/core.py`
- `platforms/grok/core.py`
- `platforms/kiro/core.py`
- `platforms/openblocklabs/core.py`
- `platforms/qwen/core.py`
- `platforms/tavily/core.py`
- `platforms/trae/core.py`

重要事实：

- `/api/platforms` 当前会过滤掉 `cursor` 和 `tavily`。
- `frontend/src/pages/RegisterTaskPage.tsx` 仍保留了历史表单实现，但当前默认 UI 的实际平台展示以 `App.tsx` + `api/platforms.py` + `Accounts.tsx` 为准。
- 不要仅根据侧边栏判断平台是否存在。

### 8.2 平台动作接口

接口定义在：

- `core/base_platform.py`
- `api/actions.py`

当前实现了动作接口的平台包括：

- `platforms/chatgpt/plugin.py`
- `platforms/cursor/plugin.py`
- `platforms/grok/plugin.py`
- `platforms/kiro/plugin.py`
- `platforms/qwen/plugin.py`
- `platforms/trae/plugin.py`

若新增动作，需要同时考虑：

1. 平台插件 `get_platform_actions()` 返回动作元数据。
2. 平台插件 `execute_action()` 执行动作。
3. 若动作会更新 token、sync 状态、extra 字段，需要检查 `api/actions.py::_apply_action_result()` 是否需要扩展。

### 8.3 邮箱 provider 接口

接口定义位置：

- `core/base_mailbox.py`
- `core/proxy_utils.py`

主接口：

- `BaseMailbox.get_email()`
- `BaseMailbox.wait_for_code()`
- `BaseMailbox.get_current_ids()`

当前构造入口：

- `create_mailbox(provider, extra, proxy)` in `core/base_mailbox.py`

关键约束：

- 邮箱流量默认绕过注册代理和环境代理。
- 这条策略由 `core/proxy_utils.py::build_mailbox_proxy_config()` 和 `create_mailbox_requests_session()` 统一保证。
- 对应回归测试在 `tests/test_mailbox_proxy_policy.py`。

这意味着：

- 新增 mailbox provider 时，不要直接复用注册代理的 requests session。
- 新增 provider 后应补 mailbox proxy policy 测试。

### 8.4 邮件导入策略接口

接口定义位置：

- `services/mail_imports/base.py`
- `services/mail_imports/registry.py`
- `services/mail_imports/providers.py`
- `services/mail_imports/schemas.py`

主接口：

- `descriptor`
- `execute()`
- `get_snapshot()`
- `delete()`
- `batch_delete()`

当前实现：

- `AppleMailImportStrategy`
- `MicrosoftMailImportStrategy`

新增邮件导入策略时，至少要同步：

1. 新建策略类并实现接口。
2. 在 `services/mail_imports/registry.py` 注册。
3. 若要在前端显示，需要让 `frontend/src/components/settings/MailImportPanel.tsx` 识别它。
4. 若会影响运行时邮箱来源，需要检查 `api/config.py`、`frontend/src/pages/Settings.tsx`、`frontend/src/pages/Accounts.tsx`，以及仍保留的 `RegisterTaskPage.tsx`。

### 8.5 外部系统回填接口

入口在 `services/external_sync.py`，按平台分发：

- `chatgpt`
  - CPA
  - Sub2API
  - CodexProxy
  - Contribution
  - Team Manager
  - CLIProxyAPI 相关状态补齐
- `grok`
  - grok2api
- `kiro`
  - Kiro Manager
- `qwen`
  - Qwen CPA

平台特定上传实现通常位于对应平台目录，例如：

- `platforms/chatgpt/cpa_upload.py`
- `platforms/chatgpt/sub2api_upload.py`
- `platforms/grok/grok2api_upload.py`
- `platforms/kiro/account_manager_upload.py`
- `platforms/qwen/cpa_upload.py`

### 8.6 外部应用管理接口

实现位置：

- `services/external_apps.py`
- `api/integrations.py`

当前管理对象：

- `cliproxyapi`
- `grok2api`
- `kiro-manager`

如果新增外部服务，需要检查：

1. `_REMOTE_URLS`
2. `_SERVICE_META`
3. `_build_command()`
4. 运行时配置写入逻辑
5. 前端是否需要展示对应管理项

### 8.7 执行器与验证码接口

定义位置：

- `core/base_executor.py`
- `core/executors/protocol.py`
- `core/executors/playwright.py`
- `core/base_captcha.py`

主接口：

- `BaseExecutor`
  - `get/post/get_cookies/set_cookies/close`
- `BaseCaptcha`
  - `solve_turnstile`
  - `solve_image`

平台插件通常通过 `BasePlatform._make_executor()` 和 `_make_captcha()` 获取统一实现，不建议在平台中散落重复构造逻辑，除非平台确有特殊浏览器流程。

## 9. 前端与 Electron 结构

### 9.1 前端真实路由

`frontend/src/App.tsx` 当前路由包括：

- `/`
- `/accounts`
- `/accounts/:platform`
- `/running-tasks`
- `/history`
- `/proxies`
- `/settings`
- `/login`

补充说明：

- `frontend/src/pages/RegisterTaskPage.tsx` 文件仍存在，但当前默认 UI 已不再暴露独立 `/register` 菜单或路由。

### 9.2 平台与执行器前端约束

`frontend/src/lib/platformExecutorOptions.ts` 是前端平台执行器矩阵源头：

- `chatgpt` / `cursor` / `grok` / `kiro` / `tavily` / `trae` 支持 `protocol/headless/headed`
- `qwen` 仅支持 `headless/headed`
- `openblocklabs` 仅支持 `protocol`

新增平台后，若不更新这里，前端执行器选项会和后端能力不一致。

### 9.3 设置页职责

`frontend/src/pages/Settings.tsx` 是配置映射核心：

- 所有后端 `CONFIG_KEYS` 对应的前端输入项基本都在这里。
- 邮件导入面板嵌在这里。
- 外部服务与认证入口也集中在这里。

新增配置项时，至少检查：

1. `api/config.py::CONFIG_KEYS`
2. `frontend/src/pages/Settings.tsx`
3. `frontend/src/pages/RegisterTaskPage.tsx`
4. 若与导入有关，再检查 `MailImportPanel.tsx`

### 9.4 Electron 行为

`electron/main.js` 的真实行为：

- 开发模式不自动起后端，要求先在仓库根目录手动启动后端。
- 生产模式会拉起打包后的 Python 后端。
- Electron 只是一层壳，业务逻辑仍以后端和 Web 前端为主。

## 10. 仓库内的重要一致性约束

这些不是泛泛而谈，而是当前代码已经形成的真实约束：

- 平台列表有三份视图：
  - 插件真实全集：`platforms/*`
  - API 暴露列表：`api/platforms.py`
  - 前端侧边栏 / 平台管理页 / 历史独立注册页：`frontend/src/App.tsx`、`Accounts.tsx`、`RegisterTaskPage.tsx`
  - 修改平台可见性时要同时检查三处。
- 配置项有三份映射：
  - 后端白名单：`api/config.py::CONFIG_KEYS`
  - 设置页：`frontend/src/pages/Settings.tsx`
  - 当前平台注册入口：`frontend/src/pages/Accounts.tsx`
  - 历史独立注册页：`frontend/src/pages/RegisterTaskPage.tsx`
- 邮箱 provider 不是单纯后端逻辑：
  - 还会影响设置页、平台注册弹窗、导入页、历史独立注册页、测试。
- mailbox 请求默认绕过代理：
  - 这是当前仓库的强约束，不要随手改掉。
- 任务停止/跳过能力依赖 checkpoint：
  - 平台和邮箱中的长等待循环必须继续调用它。
- 平台特有状态优先落在 `Account.extra` / `extra_json`：
  - 不要为单个平台轻易新增全局列。

## 11. 常见开发落点清单

### 11.1 新增一个平台插件

至少检查这些位置：

1. `platforms/<name>/plugin.py`
2. `platforms/<name>/core.py` 或等价实现文件
3. `frontend/src/pages/Accounts.tsx`
4. `frontend/src/lib/platformExecutorOptions.ts`
5. `api/platforms.py`
6. 如果需要平台动作，再检查 `api/actions.py`
7. 添加对应测试

### 11.2 新增一个邮箱 provider

至少检查这些位置：

1. `core/base_mailbox.py`
2. `core/proxy_utils.py`
3. `api/config.py`
4. `frontend/src/pages/Settings.tsx`
5. `frontend/src/pages/Accounts.tsx`
6. `frontend/src/pages/RegisterTaskPage.tsx`
7. `tests/test_mailbox_proxy_policy.py`

### 11.3 新增一个邮件导入策略

至少检查这些位置：

1. `services/mail_imports/base.py`
2. `services/mail_imports/providers.py`
3. `services/mail_imports/registry.py`
4. `frontend/src/components/settings/MailImportPanel.tsx`
5. `tests/test_mail_imports_service.py`

### 11.4 新增一个外部服务管理对象

至少检查这些位置：

1. `services/external_apps.py`
2. `api/integrations.py`
3. 相关配置项与设置页
4. 若涉及自动回填，再检查 `services/external_sync.py`

## 12. 启动与构建

常用入口：

- 后端推荐启动：
  - `.\start_backend.ps1`
- 手动后端启动：
  - `uv run python main.py`
- 前端构建：
  - `pnpm --dir frontend build`
- Docker：
  - `docker-compose.yml`

补充说明：

- `start_backend.ps1` 会优先使用项目 `.venv`，否则回退到 conda 环境。
- `main.py` 会把前端构建产物从 `static/` 托管出来，因此生产访问默认是 `8000`。
- 测试主要位于 `tests/`，以 `unittest` 风格为主。

## 13. 结论

这个仓库最重要的不是某一个平台脚本，而是下面这条主线：

`平台插件` + `邮箱来源` + `任务编排` + `外部回填` + `前端配置映射`

任何修改只改其中一层都很容易留下断点。进入具体任务前，先确认你改动的是哪一段链路，以及这段链路在前端、API、core、platforms、services、tests 中分别落在哪些文件。
