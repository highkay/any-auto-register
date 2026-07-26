"""Grok (x.ai) 平台插件"""

from typing import Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register


@register
class GrokPlatform(BasePlatform):
    name = "grok"
    display_name = "Grok"
    version = "1.1.0"
    DOMAIN_REJECTION_REASON = "Grok 注册页拒绝该邮箱域名"
    # protocol = TLS + gRPC + offscreen Turnstile; browser modes keep full UI chain
    supported_executors = ["protocol", "headless", "headed"]

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    @staticmethod
    def _is_rejected_email_domain_error(message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        lowered = text.lower()
        return (
            "邮箱域名被拒绝" in text
            or "其他邮箱地址" in text
            or "please use another email" in lowered
            or "disposable email" in lowered
            or ("email domain" in lowered and "reject" in lowered)
        )

    def _persist_blocked_email_domain(self, domain: str) -> bool:
        from core.config_store import config_store
        from core.platform_email_domains import (
            extract_email_domain,
            is_email_domain_blocked,
            parse_email_domain_list,
            platform_blocked_email_domains_key,
            resolve_platform_blocked_email_domains,
        )

        normalized_domain = extract_email_domain(domain)
        if not normalized_domain:
            return False

        key = platform_blocked_email_domains_key(self.name)
        if not key:
            return False

        existing_raw = config_store.get(key, "")
        existing_domains = resolve_platform_blocked_email_domains(
            self.name,
            {key: existing_raw},
        )
        if is_email_domain_blocked(normalized_domain, existing_domains):
            return False

        configured_domains = parse_email_domain_list(existing_raw)
        configured_domains.append(normalized_domain)
        serialized = ",".join(configured_domains)
        config_store.set(key, serialized)
        if isinstance(self.config.extra, dict):
            self.config.extra[key] = serialized
        return True

    _PROTOCOL_EXTRA_KEYS = (
        "grok_register_mode",
        "grok_browser_fallback",
        "grok_browser_mode",
        "grok_clearance_mode",
        "grok_flaresolverr_url",
        "flaresolverr_url",
        "grok_flaresolverr_attempts",
        "grok_turnstile_mode",
        "grok_turnstile_timeout",
        "grok_manual_turnstile",
        "grok_manual_turnstile_timeout",
        "grok_force_visible_browser",
        "grok_signup_attempts",
        "grok_cf_impersonate",
        "grok_cf_impersonate_fallback",
        "grok_mailbox_attempts",
    )

    def _merge_protocol_extra(self) -> dict:
        """Task extra wins; fill missing keys from global config_store."""
        from core.config_store import config_store

        extra = dict(self.config.extra or {}) if isinstance(self.config.extra, dict) else {}
        for key in self._PROTOCOL_EXTRA_KEYS:
            if str(extra.get(key) or "").strip():
                continue
            value = config_store.get(key, "")
            if str(value or "").strip():
                extra[key] = value
        return extra

    def _resolve_register_mode(self, executor_type: str, extra: dict, log) -> str:
        """protocol | browser.

        - protocol: FlareSolverr 清障 + 有头同页注册（默认）
        - headed/browser: 纯浏览器链（也有头）
        - headless: 浏览器无头链
        """
        forced = str(
            extra.get("grok_register_mode")
            or extra.get("register_mode")
            or ""
        ).strip().lower()
        if forced in {"protocol", "browser", "ui"}:
            return "browser" if forced in {"browser", "ui"} else "protocol"
        # Explicit headed/headless keep pure browser chain for max fidelity.
        if executor_type in {"headed", "browser", "ui"}:
            return "browser"
        if executor_type == "headless":
            return "browser"
        return "protocol"

    def register(self, email: str, password: Optional[str] = None) -> Account:
        from core.config_store import config_store
        from core.platform_email_domains import extract_email_domain
        from platforms.grok.core import GrokRegister
        from platforms.grok.protocol_register import GrokProtocolRegister

        log = getattr(self, "_log_fn", print)
        extra = self._merge_protocol_extra()

        # 优先从任务配置读取，兜底从全局配置读取
        yescaptcha_key = extra.get("yescaptcha_key") or config_store.get(
            "yescaptcha_key", ""
        )
        captcha_solver = self._make_captcha(key=yescaptcha_key)
        executor_type = str(self.config.executor_type or "protocol").strip().lower()
        register_mode = self._resolve_register_mode(executor_type, extra, log)
        requested_headless = executor_type == "headless"
        browser_fallback = str(extra.get("grok_browser_fallback", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        if register_mode == "protocol":
            # Protocol = FlareSolverr 清障 + 有头浏览器同页完成（不强制 YesCaptcha）
            if "grok_browser_mode" not in extra:
                # protocol 默认有头；仅当任务执行器显式 headless 时才无头
                extra["grok_browser_mode"] = (
                    "headless" if requested_headless else "headed"
                )
            extra.setdefault("executor_type", executor_type or "protocol")
            log(
                "Grok 使用协议清障 + 有头浏览器同页注册"
                f"（browser_mode={extra.get('grok_browser_mode')}，无第三方打码）"
            )
            reg = GrokProtocolRegister(
                captcha_solver=captcha_solver,
                yescaptcha_key=yescaptcha_key,
                proxy=self.config.proxy,
                log_fn=log,
                task_control=getattr(self, "_task_control", None),
                extra=extra,
            )
        else:
            log(
                f"Grok 使用浏览器注册链（{'headless' if requested_headless else 'headed'}）"
            )
            reg = GrokRegister(
                captcha_solver=captcha_solver,
                yescaptcha_key=yescaptcha_key,
                proxy=self.config.proxy,
                log_fn=log,
                headless=requested_headless,
                task_control=getattr(self, "_task_control", None),
                extra=extra,
            )
        mailbox_attempts = (
            1 if email else int(extra.get("grok_mailbox_attempts", 8) or 8)
        )
        otp_timeout = self.get_mailbox_otp_timeout(default=180)
        last_error = None
        result = None

        for attempt in range(1, mailbox_attempts + 1):
            mail_acct = None
            current_email = email
            if self.mailbox and not current_email:
                mail_acct = self.mailbox.get_email()
                current_email = mail_acct.email if mail_acct else None
            log(f"邮箱: {current_email}")
            before_ids = (
                self.mailbox.get_current_ids(mail_acct)
                if (self.mailbox and mail_acct)
                else set()
            )
            seen_ids = set(before_ids)

            def otp_cb():
                log("等待验证码...")
                if not self.mailbox or not mail_acct:
                    return ""
                code = self.mailbox.wait_for_code(
                    mail_acct,
                    keyword="",
                    timeout=otp_timeout,
                    before_ids=seen_ids,
                    code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
                )
                if code:
                    try:
                        seen_ids.clear()
                        seen_ids.update(self.mailbox.get_current_ids(mail_acct))
                    except Exception:
                        pass
                    code = code.replace("-", "").replace(" ", "")
                    log(f"验证码: {code}")
                return code

            try:
                if not current_email:
                    raise RuntimeError("未获取到可用邮箱")
                result = reg.register(
                    email=current_email,
                    password=password,
                    otp_callback=otp_cb if self.mailbox else None,
                )
                break
            except Exception as e:
                last_error = e
                msg = str(e)
                is_rejected_domain = self._is_rejected_email_domain_error(msg)
                if (
                    is_rejected_domain
                    and not email
                    and self.mailbox
                    and hasattr(self.mailbox, "blacklist_domain")
                ):
                    rejected_domain = extract_email_domain(current_email)
                    if rejected_domain:
                        self.mailbox.blacklist_domain(
                            rejected_domain,
                            reason=self.DOMAIN_REJECTION_REASON,
                        )
                        if self._persist_blocked_email_domain(rejected_domain):
                            log(f"Grok 邮箱后缀已追加到黑名单配置: {rejected_domain}")
                should_retry_mailbox = (
                    attempt < mailbox_attempts
                    and not email
                    and (
                        isinstance(e, TimeoutError)
                        or is_rejected_domain
                        or "验证码" in msg
                        or "发码失败" in msg
                    )
                )
                if should_retry_mailbox:
                    log(
                        f"Grok 验证阶段失败，切换新邮箱重试 {attempt + 1}/{mailbox_attempts}: {msg}"
                    )
                    continue
                # Protocol path optional fallback to browser UI on hard failures
                if (
                    register_mode == "protocol"
                    and browser_fallback
                    and not is_rejected_domain
                    and not (
                        "验证码" in msg
                        or isinstance(e, TimeoutError)
                    )
                ):
                    log(f"协议注册失败，回退浏览器链: {msg}")
                    browser_reg = GrokRegister(
                        captcha_solver=captcha_solver,
                        yescaptcha_key=yescaptcha_key,
                        proxy=self.config.proxy,
                        log_fn=log,
                        headless=requested_headless,
                        task_control=getattr(self, "_task_control", None),
                        extra=extra,
                    )
                    try:
                        result = browser_reg.register(
                            email=current_email,
                            password=password,
                            otp_callback=otp_cb if self.mailbox else None,
                        )
                        break
                    except Exception as browser_exc:
                        last_error = browser_exc
                        raise
                raise
        else:
            raise last_error if last_error else RuntimeError("Grok 注册失败")

        if not result:
            raise last_error if last_error else RuntimeError("Grok 注册失败")

        account_extra = {
            "sso": result["sso"],
            "sso_token": result["sso"],
            "sso_rw": result["sso_rw"],
            "given_name": result["given_name"],
            "family_name": result["family_name"],
            "register_mode": result.get("register_mode") or register_mode,
        }
        submit_meta = result.get("register_submit_meta")
        if isinstance(submit_meta, dict) and submit_meta:
            account_extra["register_submit_meta"] = submit_meta
            if submit_meta.get("saw_transient_error"):
                # Observability only — not a hard block. Helps correlate later
                # Device OAuth invalid_grant with flaky first-click submit.
                account_extra["register_had_transient_submit_error"] = True
        from platforms.grok.cpa_xai import (
            GROK_SESSION_COOKIES_EXTRA_KEY,
            REGISTRATION_RUNTIME_EXTRA_KEY,
            build_registration_runtime,
            select_grok_session_cookies,
        )

        session_cookies = select_grok_session_cookies(result.get("cookies"))
        if session_cookies:
            account_extra[GROK_SESSION_COOKIES_EXTRA_KEY] = session_cookies
        account_extra[REGISTRATION_RUNTIME_EXTRA_KEY] = build_registration_runtime(
            extra,
            headless=requested_headless,
        )
        if self.config.proxy:
            # Device OAuth must use the same egress path as the successful signup.
            account_extra["registration_proxy"] = self.config.proxy

        return Account(
            platform="grok",
            email=result["email"],
            password=result["password"],
            token=result["sso"],
            status=AccountStatus.REGISTERED,
            extra=account_extra,
        )

    def check_valid(self, account: Account) -> bool:
        return bool((account.extra or {}).get("sso"))

    def get_platform_actions(self) -> list:
        return [
            {"id": "upload_grok2api", "label": "导入 grok2api", "params": []},
            {"id": "upload_xai_cpa", "label": "生成并导入 xAI OAuth 到 CPA", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "upload_grok2api":
            from platforms.grok.grok2api_upload import upload_to_grok2api

            ok, msg = upload_to_grok2api(account)
            return {"ok": ok, "data": {"message": msg}}
        if action_id == "upload_xai_cpa":
            from platforms.grok.cpa_xai import mint_and_upload_xai_cpa

            ok, msg, metadata = mint_and_upload_xai_cpa(
                account,
                captcha_solver=self._make_captcha(),
                task_control=getattr(self, "_task_control", None),
                log=getattr(self, "_log_fn", print),
            )
            result = {"ok": ok, "data": {"message": msg}}
            if metadata:
                result["account_extra_patch"] = {"grok_cpa": metadata}
            return result
        raise NotImplementedError(f"未知操作: {action_id}")
