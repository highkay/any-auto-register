"""Qwen (chat.qwen.ai) platform plugin for Any Auto Register.

Registration flow (aligned with qwen2api):
    - Browser form submit (Name + Email + Password + Confirm + Terms)
    - Solve Aliyun WAF slider in-session when needed
    - Capture browser cookies (JWT may arrive only after activation)
    - Wait for activation email and open the link with the same cookie jar
    - OAuth device-code flow via Cookie authorize API to get access/refresh tokens
    - Optional: upload email+password to OpenGate / CPA
"""

import json

from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registry import register


def _normalize_key(key: str) -> str:
    return str(key or "").strip().lower().replace("-", "").replace("_", "")


def _parse_json_like(raw: str):
    text = str(raw or "").strip()
    if not text:
        return None
    if not ((text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _find_value_by_keys(obj, key_candidates: set[str], *, depth: int = 0, max_depth: int = 5) -> str:
    if depth > max_depth:
        return ""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if _normalize_key(key) in key_candidates:
                if isinstance(value, str) and value.strip():
                    return value.strip()
            if isinstance(value, (dict, list)):
                found = _find_value_by_keys(value, key_candidates, depth=depth + 1, max_depth=max_depth)
                if found:
                    return found
            elif isinstance(value, str):
                parsed = _parse_json_like(value)
                if parsed is not None:
                    found = _find_value_by_keys(parsed, key_candidates, depth=depth + 1, max_depth=max_depth)
                    if found:
                        return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_value_by_keys(item, key_candidates, depth=depth + 1, max_depth=max_depth)
            if found:
                return found
    elif isinstance(obj, str):
        parsed = _parse_json_like(obj)
        if parsed is not None:
            return _find_value_by_keys(parsed, key_candidates, depth=depth + 1, max_depth=max_depth)
    return ""


def _extract_qwen_oauth_fields(raw_tokens) -> tuple[str, str, str]:
    oauth_access_keys = {
        "oauthaccesstoken",
        "qwenoauthaccesstoken",
        "oauth_token",
        "oauthaccess",
    }
    refresh_keys = {
        "refreshtoken",
        "refreshtokenvalue",
        "qwenrefreshtoken",
        "oauthrefreshtoken",
    }
    resource_keys = {
        "resourceurl",
        "qwenresourceurl",
        "oauthresourceurl",
        "baseurl",
        "apiurl",
    }
    oauth_access_token = (
        _find_value_by_keys(raw_tokens, oauth_access_keys)
        if isinstance(raw_tokens, (dict, list, str))
        else ""
    )
    refresh_token = _find_value_by_keys(raw_tokens, refresh_keys) if isinstance(raw_tokens, (dict, list, str)) else ""
    resource_url = _find_value_by_keys(raw_tokens, resource_keys) if isinstance(raw_tokens, (dict, list, str)) else ""
    return oauth_access_token, refresh_token, resource_url


@register
class QwenPlatform(BasePlatform):
    name = "qwen"
    display_name = "Qwen"
    version = "1.0.0"
    # Qwen requires browser automation (form fill + submit)
    supported_executors = ["headless", "headed"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        from platforms.qwen.core import (
            QwenRegister,
            call_activation_api,
            disable_qwen_memory_features,
            obtain_qwen_oauth_tokens_with_cookies,
            obtain_qwen_oauth_tokens_with_login,
            wait_for_activation_link,
        )
        from core.config_store import config_store

        log = getattr(self, "_log_fn", print)
        # qwen2api default: discard captcha attempts — do NOT use ohmycaptcha/yescaptcha.
        # solve = legacy in-session slide solver; manual = headed human solve.
        captcha_mode = str(
            (self.config.extra or {}).get("qwen_captcha_mode")
            or config_store.get("qwen_captcha_mode", "")
            or "discard"
        ).strip().lower() or "discard"
        captcha_solver = None
        if captcha_mode == "solve":
            yescaptcha_key = self.config.extra.get("yescaptcha_key") or config_store.get(
                "yescaptcha_key", ""
            )
            captcha_solver = self._make_captcha(key=yescaptcha_key)
            log("[Qwen] captcha_mode=solve（遗留路径，将调用验证码求解器）")
        elif captcha_mode == "manual":
            log("[Qwen] captcha_mode=manual（headed：请在浏览器中手动完成阿里云验证码）")
        else:
            captcha_mode = "discard"
            log("[Qwen] captcha_mode=discard（对齐 qwen2api：遇验证码丢弃重试，不调用求解器）")

        mail_acct = None
        before_ids: set = set()
        if self.mailbox:
            # Prefer the email passed in by the task layer; only mint a new mailbox
            # address when the caller did not provide one (avoid burning two addresses).
            if email:
                class _BoundMail:
                    def __init__(self, addr: str):
                        self.email = addr
                        self.account_id = addr

                mail_acct = _BoundMail(str(email))
                try:
                    before_ids = self.mailbox.get_current_ids(mail_acct) or set()
                except Exception:
                    before_ids = set()
            else:
                mail_acct = self.mailbox.get_email()
                email = mail_acct.email if mail_acct else None
                try:
                    before_ids = self.mailbox.get_current_ids(mail_acct) if mail_acct else set()
                except Exception:
                    before_ids = set()
        if not email:
            raise RuntimeError("Qwen registration requires an email address")

        log("[Qwen] 开始注册流程（对齐 qwen2api: 表单 → 激活邮件 → Cookie OAuth）")
        log(f"[Qwen] 邮箱: {email}")
        otp_timeout = self.get_mailbox_otp_timeout()

        with self._make_executor() as ex:
            reg = QwenRegister(
                executor=ex,
                log_fn=log,
                captcha_solver=captcha_solver,
                captcha_mode=captcha_mode,
                task_control=getattr(self, "_task_control", None),
            )
            result = reg.register(email=email, password=password)

        if result.get("status") != "success":
            reason = str(result.get("error") or "signup failed")
            log(f"[Qwen] 注册失败: {reason}")
            raise RuntimeError(f"Qwen registration failed: {reason}")

        tokens = result.get("tokens", {}) if isinstance(result.get("tokens"), dict) else {}
        cookies = result.get("cookies", {}) if isinstance(result.get("cookies"), dict) else {}
        access_token = str(
            tokens.get("token")
            or tokens.get("cookie:token")
            or tokens.get("access_token")
            or cookies.get("token")
            or ""
        ).strip()
        pending_activation = bool(result.get("pending_activation")) or not access_token
        log(
            f"[Qwen] 表单阶段完成: token={'有' if access_token else '无'}, "
            f"cookies={len(cookies)}, pending_activation={pending_activation}"
        )

        activated = False
        if self.mailbox and mail_acct:
            log("[Qwen] 等待激活邮件...")
            activation_link = wait_for_activation_link(
                self.mailbox,
                mail_acct=mail_acct,
                account_email=email,
                timeout=max(30, min(180, int(otp_timeout or 120))),
                before_ids=before_ids,
                log_fn=log,
            )

            if activation_link:
                log("[Qwen] 找到激活链接，使用浏览器 Cookie 会话激活...")
                act_result = call_activation_api(activation_link, cookies=cookies)
                activated = bool(act_result.get("ok"))
                act_cookies = act_result.get("cookies") if isinstance(act_result.get("cookies"), dict) else {}
                if act_cookies:
                    cookies = {**cookies, **act_cookies}
                token_after = str(act_result.get("token") or cookies.get("token") or "").strip()
                if token_after:
                    access_token = token_after
                    tokens = {**tokens, "token": token_after, "cookie:token": token_after}
                log(f"[Qwen] 激活结果: {'成功' if activated else '失败'} status={act_result.get('status_code')}")
            else:
                log("[Qwen] 未找到激活链接，跳过激活")
        else:
            log("[Qwen] 无邮箱服务，跳过激活")

        raw_tokens = tokens if isinstance(tokens, dict) else {}
        oauth_access_token, refresh_token, resource_url = _extract_qwen_oauth_fields(raw_tokens)

        if not refresh_token:
            # Primary path (qwen2api): authorize device code with Cookie header.
            if cookies:
                log("[Qwen] 尝试 Cookie 会话 OAuth device flow...")
                oauth_data = obtain_qwen_oauth_tokens_with_cookies(
                    cookies,
                    email=str(result.get("email") or email),
                    log_fn=log,
                    poll_timeout_seconds=60,
                )
            else:
                oauth_data = {}

            if not oauth_data:
                log("[Qwen] Cookie OAuth 未拿到 refresh_token，回退密码登录 OAuth...")
                with self._make_executor() as ex:
                    oauth_data = obtain_qwen_oauth_tokens_with_login(
                        ex.page,
                        email=str(result.get("email") or email),
                        password=str(result.get("password") or password or ""),
                        log_fn=log,
                        poll_timeout_seconds=30,
                    )

            if oauth_data:
                got_refresh = str(oauth_data.get("refresh_token") or "").strip()
                if got_refresh:
                    refresh_token = got_refresh
                    oauth_access_token = str(
                        oauth_data.get("oauth_access_token")
                        or oauth_data.get("access_token")
                        or oauth_access_token
                        or ""
                    ).strip()
                    resource_url = str(
                        oauth_data.get("resource_url")
                        or resource_url
                        or "portal.qwen.ai"
                    ).strip()
                    if oauth_access_token:
                        access_token = access_token or oauth_access_token
                    log("[Qwen] 成功获取 OAuth tokens")
                    log(f"[Qwen] refresh_token: {refresh_token[:20]}...")
                    log(f"[Qwen] resource_url: {resource_url}")
                else:
                    log("[Qwen] OAuth 完成但未获取到 refresh_token")
            else:
                log("[Qwen] OAuth 失败，无法获取 refresh_token")
        else:
            log(f"[Qwen] 已获取 refresh_token: {refresh_token[:20]}...")

        if not access_token and oauth_access_token:
            access_token = oauth_access_token
        if not access_token:
            log("[Qwen] 注册失败: 激活/OAuth 后仍无 access token")
            raise RuntimeError(
                "Qwen registration failed: no auth token after activation/oauth"
            )

        # Best-effort: mirror qwen2api post-register settings tweak.
        if disable_qwen_memory_features(access_token):
            log("[Qwen] 已关闭新账号记忆功能")

        extra = {
            "activated": activated,
            "full_name": result.get("full_name", ""),
            "raw_tokens": raw_tokens,
            "pending_activation": pending_activation and not activated,
        }
        if cookies:
            extra["cookies"] = cookies
        if oauth_access_token:
            extra["oauth_access_token"] = oauth_access_token
            extra["qwen_oauth_access_token"] = oauth_access_token
        if refresh_token:
            extra["refresh_token"] = refresh_token
            extra["qwen_refresh_token"] = refresh_token
        if resource_url:
            extra["resource_url"] = resource_url
            extra["qwen_resource_url"] = resource_url

        log(f"[Qwen] 注册流程完成，激活状态: {'已激活' if activated else '未激活'}")
        log(f"[Qwen] refresh_token 状态: {'已获取' if refresh_token else '未获取'}")

        return Account(
            platform="qwen",
            email=result["email"],
            password=result["password"],
            token=access_token,
            status=AccountStatus.REGISTERED,
            extra=extra,
        )

    def check_valid(self, account: Account) -> bool:
        """Check if Qwen account token is still valid."""
        from curl_cffi import requests as curl_req

        token = account.token
        if not token:
            return False

        try:
            r = curl_req.get(
                "https://chat.qwen.ai/api/v1/chats",
                headers={
                    "Authorization": f"Bearer {token}",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                impersonate="chrome124",
                timeout=15,
            )
            return r.status_code == 200
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        """Return platform-specific actions."""
        return [
            {"id": "activate_account", "label": "激活账号", "params": []},
            {"id": "get_user_info", "label": "获取用户信息", "params": []},
            {
                "id": "upload_cpa",
                "label": "导入 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_opengate",
                "label": "导入 OpenGate",
                "params": [
                    {
                        "key": "api_url",
                        "label": "OpenGate URL",
                        "type": "text",
                        "placeholder": "http://192.168.1.18:7860",
                    },
                    {"key": "api_key", "label": "OpenGate API Key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """Execute platform-specific actions."""
        from curl_cffi import requests as curl_req
        from platforms.qwen.core import call_activation_api, wait_for_activation_link

        if action_id == "activate_account":
            # Try to find activation link from mailbox and activate
            if not self.mailbox:
                try:
                    from core.base_mailbox import create_mailbox

                    cfg_extra = (self.config.extra or {}) if self.config else {}
                    provider = str(cfg_extra.get("mail_provider", "luckmail") or "luckmail")
                    self.mailbox = create_mailbox(
                        provider=provider,
                        extra=cfg_extra,
                        proxy=self.config.proxy if self.config else None,
                        platform=self.name,
                    )
                except Exception as e:
                    return {"ok": False, "error": f"未配置可用邮箱，无法激活: {e}"}

            try:
                default_wait_seconds = self.get_mailbox_otp_timeout(default=60)
                wait_seconds = int(params.get("wait_seconds") or default_wait_seconds)
                wait_seconds = max(5, min(120, wait_seconds))
                mail_acct = None
                before_ids = None
                supports_direct_email_lookup = hasattr(self.mailbox, "_get_mails")
                if (
                    not supports_direct_email_lookup
                    and hasattr(self.mailbox, "get_current_ids")
                    and hasattr(self.mailbox, "get_email")
                ):
                    try:
                        mail_acct = self.mailbox.get_email()
                        if mail_acct:
                            before_ids = self.mailbox.get_current_ids(mail_acct)
                    except Exception:
                        mail_acct = None
                        before_ids = None

                activation_link = wait_for_activation_link(
                    self.mailbox,
                    mail_acct=mail_acct,
                    account_email=account.email or "",
                    timeout=wait_seconds,
                    before_ids=before_ids,
                    log_fn=getattr(self, "_log_fn", print),
                )

                if activation_link:
                    act_result = call_activation_api(activation_link)
                    if act_result.get("ok"):
                        return {"ok": True, "message": "账号激活成功"}
                    return {"ok": False, "error": f"激活请求失败: {act_result}"}
                return {"ok": False, "error": f"在 {wait_seconds}s 内未找到激活邮件"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        if action_id == "get_user_info":
            token = account.token
            if not token:
                return {"ok": False, "error": "账号缺少 token"}

            def _decode_jwt_payload(raw_token: str) -> dict:
                import base64
                import json as _json

                parts = str(raw_token or "").split(".")
                if len(parts) < 2:
                    return {}
                payload = parts[1]
                padded = payload + "=" * ((4 - len(payload) % 4) % 4)
                try:
                    decoded = base64.urlsafe_b64decode(padded.encode("utf-8")).decode(
                        "utf-8", errors="ignore"
                    )
                    obj = _json.loads(decoded)
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}

            headers = {
                "Authorization": f"Bearer {token}",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            candidates = [
                ("users_me", "https://chat.qwen.ai/api/v1/users/me"),
                ("users_profile", "https://chat.qwen.ai/api/v1/users/profile"),
                ("chats", "https://chat.qwen.ai/api/v1/chats"),
            ]
            source_labels = {
                "users_me": "用户信息接口(me)",
                "users_profile": "用户资料接口(profile)",
                "chats": "会话列表接口",
            }
            attempts = []
            try:
                for source, url in candidates:
                    r = curl_req.get(
                        url,
                        headers=headers,
                        impersonate="chrome124",
                        timeout=15,
                    )
                    attempts.append(
                        {
                            "source": source,
                            "url": url,
                            "status_code": r.status_code,
                        }
                    )
                    if r.status_code != 200:
                        continue

                    data = None
                    try:
                        data = r.json()
                    except Exception:
                        data = {"raw": (r.text or "")[:1000]}

                    token_payload = _decode_jwt_payload(token)
                    payload_summary = {
                        "id": token_payload.get("id"),
                        "exp": token_payload.get("exp"),
                        "last_password_change": token_payload.get("last_password_change"),
                    }

                    attempts_cn = [
                        {
                            "接口": source_labels.get(item["source"], item["source"]),
                            "地址": item["url"],
                            "状态码": item["status_code"],
                        }
                        for item in attempts
                    ]

                    result = {
                        "来源": source_labels.get(source, source),
                        "接口地址": url,
                        "HTTP状态": r.status_code,
                        "用户ID": payload_summary.get("id"),
                        "Token过期时间": payload_summary.get("exp"),
                        "最近改密时间": payload_summary.get("last_password_change"),
                        "尝试记录": attempts_cn,
                        "原始数据": data,
                    }
                    if source == "chats" and isinstance(data, list):
                        result["会话数量"] = len(data)
                    return {"ok": True, "data": result}

                return {
                    "ok": False,
                    "error": (
                        "查询用户信息失败: "
                        + ", ".join(
                            f"{item['source']}={item['status_code']}" for item in attempts
                        )
                    ),
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

        if action_id == "upload_cpa":
            from platforms.qwen.cpa_upload import generate_token_json, upload_to_cpa
            from platforms.qwen.core import obtain_qwen_oauth_tokens_with_login

            class _A:
                pass

            extra = account.extra if isinstance(account.extra, dict) else {}
            raw_tokens = extra.get("raw_tokens") if isinstance(extra.get("raw_tokens"), dict) else {}
            raw_oa, raw_rt, raw_ru = _extract_qwen_oauth_fields(raw_tokens)
            oauth_access_token = str(
                extra.get("oauth_access_token")
                or extra.get("qwen_oauth_access_token")
                or raw_oa
                or ""
            ).strip()
            refresh_token = str(
                extra.get("refresh_token")
                or extra.get("qwen_refresh_token")
                or raw_rt
                or ""
            ).strip()
            resource_url = str(
                extra.get("resource_url")
                or extra.get("qwen_resource_url")
                or raw_ru
                or ""
            ).strip()

            account_extra_patch = {}
            if not refresh_token and account.email and account.password:
                with self._make_executor() as ex:
                    oauth_data = obtain_qwen_oauth_tokens_with_login(
                        ex.page,
                        email=str(account.email or ""),
                        password=str(account.password or ""),
                        log_fn=getattr(self, "_log_fn", print),
                        poll_timeout_seconds=20,
                    )
                got_refresh = str(oauth_data.get("refresh_token") or "").strip()
                if got_refresh:
                    oauth_access_token = str(
                        oauth_data.get("oauth_access_token")
                        or oauth_data.get("access_token")
                        or oauth_access_token
                        or ""
                    ).strip()
                    refresh_token = got_refresh
                    resource_url = str(
                        oauth_data.get("resource_url")
                        or resource_url
                        or "portal.qwen.ai"
                    ).strip()
                    account_extra_patch = {
                        "oauth_access_token": oauth_access_token,
                        "qwen_oauth_access_token": oauth_access_token,
                        "refresh_token": refresh_token,
                        "qwen_refresh_token": refresh_token,
                        "resource_url": resource_url,
                        "qwen_resource_url": resource_url,
                    }

            a = _A()
            a.email = account.email
            a.access_token = oauth_access_token or account.token or ""
            a.refresh_token = refresh_token
            a.resource_url = resource_url
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            resp = {"ok": ok, "data": msg}
            if account_extra_patch:
                resp["account_extra_patch"] = account_extra_patch
            return resp

        if action_id == "upload_opengate":
            from platforms.qwen.opengate_upload import (
                persist_opengate_sync_result,
                upload_to_opengate,
            )

            ok, msg, detail = upload_to_opengate(
                account,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            try:
                persist_opengate_sync_result(account, ok=ok, msg=msg, detail=detail)
            except Exception:
                pass
            return {"ok": ok, "data": msg, "detail": detail}

        raise NotImplementedError(f"Unknown action: {action_id}")
