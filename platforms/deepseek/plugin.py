"""DeepSeek 平台插件。"""

from __future__ import annotations

import os
from typing import Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register
from platforms.deepseek.core import (
    DEEPSEEK_DEFAULT_POW_WORKER_URL,
    DEEPSEEK_DEFAULT_REGION,
    DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
    DEEPSEEK_DEFAULT_UI_LOCALE,
    DEEPSEEK_HCAPTCHA_SITEKEY,
    DeepSeekClient,
    DeepSeekEmailDomainRejected,
    ensure_deepseek_email_sign_up_available_via_browser,
    random_password,
    register_deepseek_via_browser,
)


@register
class DeepSeekPlatform(BasePlatform):
    name = "deepseek"
    display_name = "DeepSeek"
    version = "1.0.0"
    supported_executors = ["headless", "headed"]

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def _build_client(self, log_fn) -> DeepSeekClient:
        from core.config_store import config_store

        extra = self.config.extra or {}
        return DeepSeekClient(
            proxy=self.config.proxy,
            log_fn=log_fn,
            ui_locale=str(
                extra.get("deepseek_ui_locale")
                or config_store.get(
                    "deepseek_ui_locale", DEEPSEEK_DEFAULT_UI_LOCALE
                )
            ).strip()
            or DEEPSEEK_DEFAULT_UI_LOCALE,
            region=str(
                extra.get("deepseek_region")
                or config_store.get("deepseek_region", DEEPSEEK_DEFAULT_REGION)
            ).strip()
            or DEEPSEEK_DEFAULT_REGION,
            tz_offset_seconds=str(
                extra.get("deepseek_tz_offset_seconds")
                or config_store.get(
                    "deepseek_tz_offset_seconds",
                    DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
                )
            ).strip()
            or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
            pow_worker_url=str(
                extra.get("deepseek_pow_worker_url")
                or config_store.get(
                    "deepseek_pow_worker_url",
                    DEEPSEEK_DEFAULT_POW_WORKER_URL,
                )
            ).strip()
            or DEEPSEEK_DEFAULT_POW_WORKER_URL,
        )

    def register(self, email: str, password: Optional[str] = None) -> Account:
        log = getattr(self, "_log_fn", print)
        if email:
            raise RuntimeError("DeepSeek 当前仅支持 mailbox provider 自动分配邮箱")
        if not self.mailbox:
            raise RuntimeError("DeepSeek 注册需要 mailbox provider 支持自动收码")
        extra = self.config.extra or {}
        browser_user_data_dir = (
            str(extra.get("deepseek_browser_user_data_dir") or "").strip() or None
        )
        flaresolverr_url = str(
            extra.get("deepseek_flaresolverr_url")
            or extra.get("flaresolverr_url")
            or ""
        ).strip() or None
        manual_send_code_handoff = (
            str(self.config.captcha_solver or "").strip().lower() == "manual"
        )
        if manual_send_code_handoff and self.config.executor_type != "headed":
            raise RuntimeError(
                "DeepSeek captcha_solver=manual 需要 executor_type=headed，"
                "以便人工接力完成 hCaptcha 发码"
            )
        client = self._build_client(log)
        try:
            ensure_deepseek_email_sign_up_available_via_browser(
                proxy=self.config.proxy,
                ui_locale=client.ui_locale,
                headless=self.config.executor_type != "headed",
                user_data_dir=browser_user_data_dir,
                flaresolverr_url=flaresolverr_url,
            )

            from core.config_store import config_store

            yescaptcha_key = str(
                extra.get("yescaptcha_key")
                or config_store.get("yescaptcha_key", "")
                or os.getenv("YESCAPTCHA_KEY")
                or ""
            ).strip()
            captcha_solver = None
            if not manual_send_code_handoff:
                captcha_solver = self._make_captcha(key=yescaptcha_key)
            hcaptcha_sitekey = str(
                extra.get("deepseek_hcaptcha_sitekey")
                or config_store.get("deepseek_hcaptcha_sitekey", DEEPSEEK_HCAPTCHA_SITEKEY)
                or DEEPSEEK_HCAPTCHA_SITEKEY
            ).strip() or DEEPSEEK_HCAPTCHA_SITEKEY
            try:
                mailbox_attempts = int(
                    str(
                        extra.get("deepseek_mailbox_attempts")
                        or config_store.get("deepseek_mailbox_attempts", "3")
                        or "3"
                    ).strip()
                )
            except (TypeError, ValueError):
                mailbox_attempts = 3
            mailbox_attempts = max(mailbox_attempts, 1)
            password_value = password or random_password()
            last_error: Exception | None = None

            for attempt in range(1, mailbox_attempts + 1):
                mail_acct = self.mailbox.get_email()
                current_email = mail_acct.email if mail_acct else ""
                if not current_email:
                    raise RuntimeError("DeepSeek 未获取到可用邮箱")

                before_ids = self.mailbox.get_current_ids(mail_acct)
                otp_timeout = self.get_mailbox_otp_timeout(default=180)
                log(f"[DeepSeek] 邮箱: {current_email}")
                try:
                    browser_state = register_deepseek_via_browser(
                        email=current_email,
                        password=password_value,
                        mailbox=self.mailbox,
                        mail_account=mail_acct,
                        before_ids=before_ids,
                        otp_timeout=otp_timeout,
                        proxy=self.config.proxy,
                        ui_locale=client.ui_locale,
                        headless=self.config.executor_type != "headed",
                        user_data_dir=browser_user_data_dir,
                        flaresolverr_url=flaresolverr_url,
                        captcha_solver=captcha_solver,
                        hcaptcha_sitekey=hcaptcha_sitekey,
                        task_control=getattr(self, "_task_control", None),
                        tz_offset_seconds=client.tz_offset_seconds,
                        pow_worker_url=client.pow_worker_url,
                        manual_send_code_handoff=manual_send_code_handoff,
                        log_fn=log,
                    )
                    break
                except DeepSeekEmailDomainRejected as exc:
                    last_error = exc
                    rejected_domain = exc.domain or current_email
                    if hasattr(self.mailbox, "blacklist_domain"):
                        self.mailbox.blacklist_domain(
                            rejected_domain,
                            reason="DeepSeek 发码接口拒绝该邮箱域名",
                        )
                    if attempt < mailbox_attempts:
                        log(
                            "DeepSeek 当前邮箱域名不支持，切换新邮箱重试 "
                            f"{attempt + 1}/{mailbox_attempts}: {exc}"
                        )
                        continue
                    raise
            else:
                raise last_error if last_error else RuntimeError("DeepSeek 注册失败")

            log(
                "[DeepSeek] 浏览器注册完成"
                f" final_url={browser_state.get('final_url') or ''}"
            )
            user = browser_state.get("register_user") or {}
            if not str(user.get("id") or "").strip() or not str(
                user.get("token") or ""
            ).strip():
                log("[DeepSeek] 浏览器注册响应缺少用户信息，回退到登录校验")
                login_data = client.login(
                    email=current_email,
                    password=password_value,
                )
                inner = login_data.get("data", {})
                if inner.get("biz_code") not in (0, "0"):
                    raise RuntimeError(f"DeepSeek 登录校验失败: {inner}")
                user = (inner.get("biz_data") or {}).get("user") or {}
            user_id = str(user.get("id") or "").strip()
            token = str(user.get("token") or "").strip()
            if not user_id or not token:
                raise RuntimeError(f"DeepSeek 注册返回缺少用户信息: {browser_state}")
            return Account(
                platform="deepseek",
                email=current_email,
                password=password_value,
                user_id=user_id,
                region=client.region,
                token=token,
                status=AccountStatus.REGISTERED,
                extra={
                    "username": str(user.get("email") or "").strip() or current_email,
                    "need_birthday": bool(user.get("need_birthday")),
                    "device_id": client.device_id,
                    "login_token": token,
                    "register_via": "browser",
                },
            )
        finally:
            client.close()

    def check_valid(self, account: Account) -> bool:
        client = self._build_client(getattr(self, "_log_fn", print))
        try:
            data = client.login(email=account.email, password=account.password)
        except Exception:
            return False
        finally:
            client.close()
        inner = data.get("data", {})
        return inner.get("biz_code") == 0
