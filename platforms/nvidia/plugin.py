"""NVIDIA 平台插件。"""

from __future__ import annotations

import html
import random
import re
import string
import time
from typing import Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register


def _random_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    base = "".join(random.choices(alphabet, k=max(length, 10)))
    return f"{base}!Aa1"


_NVIDIA_VERIFY_LINK_RE = re.compile(
    r"https://login\.nvgs\.nvidia\.cn(?:/[a-z-]+)?/profile-management/verify-email\?[^\s\"'>]+",
    re.IGNORECASE,
)


def _extract_nvidia_verify_link(*texts: str) -> str:
    for text in texts:
        normalized = html.unescape(str(text or ""))
        normalized = normalized.replace("=\r\n", "").replace("=\n", "")
        normalized = normalized.replace("=3D", "=").replace("&amp;", "&")
        match = _NVIDIA_VERIFY_LINK_RE.search(normalized)
        if match:
            return html.unescape(match.group(0)).replace("&amp;", "&")
    return ""


@register
class NvidiaPlatform(BasePlatform):
    name = "nvidia"
    display_name = "NVIDIA"
    version = "1.0.0"
    supported_executors = ["headless", "headed"]

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def register(self, email: str, password: Optional[str] = None) -> Account:
        from core.config_store import config_store
        from platforms.nvidia.core import NvidiaRegister

        log = getattr(self, "_log_fn", print)
        yescaptcha_key = self.config.extra.get("yescaptcha_key") or config_store.get(
            "yescaptcha_key", ""
        )
        captcha_solver = self._make_captcha(key=yescaptcha_key)
        requested_headless = (self.config.executor_type or "headed") != "headed"

        password_value = password or _random_password()
        mailbox_attempts = 1 if email else int(self.config.extra.get("nvidia_mailbox_attempts", 12))
        last_error = None

        for attempt in range(1, mailbox_attempts + 1):
            mail_acct = None
            current_email = email
            if self.mailbox and not current_email:
                mail_acct = self.mailbox.get_email()
                current_email = mail_acct.email if mail_acct else None
            if not current_email:
                raise RuntimeError("未获取到可用邮箱")

            before_ids = (
                self.mailbox.get_current_ids(mail_acct)
                if self.mailbox and mail_acct
                else set()
            )
            otp_timeout = self.get_mailbox_otp_timeout(default=180)
            used_codes: set[str] = set()
            used_links: set[str] = set()

            def otp_cb(*, otp_sent_at=None, exclude_codes=None) -> str:
                log("等待 NVIDIA 验证邮件...")
                if not self.mailbox or not mail_acct:
                    return ""
                excluded = {
                    str(code).strip()
                    for code in (exclude_codes or set())
                    if str(code).strip()
                }
                excluded.update(used_codes)
                code = self.mailbox.wait_for_code(
                    mail_acct,
                    keyword="",
                    timeout=otp_timeout,
                    before_ids=before_ids,
                    code_pattern=r"(?<![A-Za-z0-9])(\d{3}(?:[-\s]?\d{3}))(?![A-Za-z0-9])",
                    otp_sent_at=otp_sent_at,
                    exclude_codes=excluded,
                )
                if code:
                    raw_code = str(code).strip()
                    normalized_code = re.sub(r"\D+", "", raw_code)
                    used_codes.add(raw_code)
                    if normalized_code:
                        used_codes.add(normalized_code)
                    log(f"NVIDIA 验证码: {code}")
                return code

            def verify_link_cb() -> str:
                if not self.mailbox or not mail_acct:
                    return ""
                fetch_mails = getattr(self.mailbox, "_get_mails", None)
                decode_raw = getattr(self.mailbox, "_decode_raw_content", None)
                if not callable(fetch_mails):
                    return ""
                deadline = time.time() + otp_timeout
                while time.time() < deadline:
                    try:
                        mails = fetch_mails(mail_acct.email)
                    except Exception:
                        mails = []
                    for mail in sorted(mails, key=lambda item: item.get("id", 0), reverse=True):
                        mid = str(mail.get("id", "") or "")
                        if mid and mid in {str(item) for item in before_ids or set()}:
                            continue
                        raw = str(mail.get("raw", "") or "")
                        subject = str(mail.get("subject", "") or "")
                        decoded = decode_raw(raw) if callable(decode_raw) else raw
                        link = _extract_nvidia_verify_link(decoded, raw, subject)
                        if not link:
                            continue
                        if link in used_links:
                            continue
                        used_links.add(link)
                        log("发现 NVIDIA 邮件验证链接")
                        return link
                    time.sleep(3)
                return ""

            reg = NvidiaRegister(
                captcha_solver=captcha_solver,
                proxy=self.config.proxy,
                log_fn=log,
                headless=requested_headless,
                task_control=getattr(self, "_task_control", None),
            )

            try:
                result = reg.register(
                    email=current_email,
                    password=password_value,
                    otp_callback=otp_cb if self.mailbox else None,
                    verification_link_callback=verify_link_cb if self.mailbox else None,
                )
                break
            except Exception as exc:
                last_error = exc
                msg = str(exc)
                login_branch_blocked = (
                    attempt < mailbox_attempts
                    and not email
                    and "未进入 NVIDIA Create Account 页面" in msg
                    and "/v1/login" in msg
                )
                if login_branch_blocked:
                    log(
                        f"NVIDIA 当前邮箱落到登录页，切换新邮箱重试 {attempt + 1}/{mailbox_attempts}"
                    )
                    continue
                raise
        else:
            raise last_error if last_error else RuntimeError("NVIDIA 注册失败")

        return Account(
            platform="nvidia",
            email=result["email"],
            password=result["password"],
            token=result["api_key"],
            status=AccountStatus.REGISTERED,
            extra={
                "api_key": result["api_key"],
                "base_url": result["base_url"],
                "org_name": result.get("org_name", ""),
                "key_id": result.get("key_id", ""),
                "key_expiry": result.get("key_expiry", ""),
                "user_verified": result.get("user_verified"),
                "user_blocked": result.get("user_blocked"),
            },
        )

    def check_valid(self, account: Account) -> bool:
        import requests

        extra = account.extra or {}
        api_key = str(extra.get("api_key") or account.token or "").strip()
        if not api_key:
            return False

        base_url = str(
            extra.get("base_url") or "https://integrate.api.nvidia.com"
        ).rstrip("/")
        try:
            resp = requests.get(
                f"{base_url}/v1/models",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                timeout=15,
            )
            return resp.status_code < 400
        except Exception:
            return False
