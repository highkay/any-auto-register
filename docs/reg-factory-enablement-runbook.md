# reg-factory 集成启用清单

## 已实现能力

| 能力 | 入口 | Flag |
|------|------|------|
| FunCaptcha / PerimeterX / Composite auto | `core/base_captcha.py` + Settings 验证码 | `feature_capsolver`（CapSolver 节点） |
| Vision vote + page drivers | `services/vision_solver/` | `feature_vision_captcha` |
| Human mouse | `core/human_mouse.py` | 无（Outlook PX human 路径） |
| Magic-link mailbox | `core/mailbox_links.py` | 无 |
| GitHub 注册 | `platforms/github/` + Accounts | `feature_github_register` |
| Claude 注册 | `platforms/claude/` | `feature_claude_register`（默认搁置） |
| Outlook 邮箱生产 | `POST /api/mail-producers/outlook` | `feature_outlook_producer` |
| 多平台串行 | `POST /api/multi-tasks/register` | 各平台自身 flag |

## 推荐开启顺序

1. 配置 `yescaptcha_key`（及可选 `capsolver_key` / `ezcaptcha_key` / `vision_*`）
2. Settings → 实验功能：按需打开 flag
3. Outlook：`feature_outlook_producer=1` → `POST /api/mail-producers/outlook` → 本地 `outlook_accounts` → 其它平台 `mail_provider=microsoft`
4. GitHub：`feature_github_register=1` → Accounts 侧栏出现 GitHub → 注册（headed 推荐）
5. Claude：仅在需要时打开 `feature_claude_register`，邮箱用 magic-link 白名单 provider

## 闭环说明

```text
配置(CONFIG_KEYS)
  → flags fail-closed
  → platforms API 过滤
  → Accounts / RunningTasks UI
  → tasks / mail-producers / multi
  → captcha + vision + human_mouse
  → accounts / outlook_accounts 入库
```

## 已知限制

- Outlook / GitHub / Claude 浏览器流程受站点改版与风控影响，需 headed 人工兜底窗口
- Graph refresh_token 自动提取为可选增强，默认密码入库 + enabled 策略
- 多平台串行无 SLA
- clean-room vision/mouse：见 `docs/THIRD_PARTY_NOTICES.md`
