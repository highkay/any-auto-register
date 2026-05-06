"""Cerebras 平台插件。"""

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


_CEREBRAS_MAGIC_LINK_RE = re.compile(
    r"https://cloud\.cerebras\.ai/auth/magic-link\?callbackUrl=[^\s\"'>]+",
    re.IGNORECASE,
)


def _random_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    base = "".join(random.choices(alphabet, k=max(length, 10)))
    return f"{base}!Aa1"


def _extract_cerebras_magic_link(*texts: str) -> str:
    for text in texts:
        normalized = html.unescape(str(text or ""))
        normalized = normalized.replace("=\r\n", "").replace("=\n", "")
        normalized = normalized.replace("=3D", "=").replace("&amp;", "&")
        match = _CEREBRAS_MAGIC_LINK_RE.search(normalized)
        if match:
            return html.unescape(match.group(0)).replace("&amp;", "&")
    return ""


@register
class CerebrasPlatform(BasePlatform):
    name = "cerebras"
    display_name = "Cerebras"
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
        from platforms.cerebras.core import CerebrasRegister

        log = getattr(self, "_log_fn", print)
        yescaptcha_key = self.config.extra.get("yescaptcha_key") or config_store.get(
            "yescaptcha_key", ""
        )
        captcha_solver = self._make_captcha(key=yescaptcha_key)
        password_value = password or _random_password()
        requested_headless = (self.config.executor_type or "headed") != "headed"
        mailbox_attempts = 1 if email else int(self.config.extra.get("cerebras_mailbox_attempts", 3) or 3)
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
            used_links: set[str] = set()

            def verify_link_cb() -> str:
                if not self.mailbox or not mail_acct:
                    return ""
                fetch_mails = getattr(self.mailbox, "_get_mails", None)
                decode_raw = getattr(self.mailbox, "_decode_raw_content", None)
                if not callable(fetch_mails):
                    return ""
                deadline = time.time() + otp_timeout
                before_id_texts = {str(item) for item in before_ids or set()}
                while time.time() < deadline:
                    try:
                        mails = fetch_mails(mail_acct.email)
                    except Exception:
                        mails = []
                    for mail in sorted(mails, key=lambda item: item.get("id", 0), reverse=True):
                        mid = str(mail.get("id", "") or mail.get("message_id", "") or "")
                        if mid and mid in before_id_texts:
                            continue
                        raw = str(mail.get("raw", "") or "")
                        subject = str(mail.get("subject", "") or "")
                        decoded = decode_raw(raw) if callable(decode_raw) else raw
                        link = _extract_cerebras_magic_link(decoded, raw, subject)
                        if not link or link in used_links:
                            continue
                        used_links.add(link)
                        log("发现 Cerebras 邮件激活链接")
                        return link
                    time.sleep(3)
                return ""

            reg = CerebrasRegister(
                proxy=self.config.proxy,
                captcha_solver=captcha_solver,
                log_fn=log,
                headless=requested_headless,
                task_control=getattr(self, "_task_control", None),
            )

            try:
                result = reg.register(
                    email=current_email,
                    password=password_value,
                    full_name=str(
                        self.config.extra.get("cerebras_full_name")
                        or config_store.get("cerebras_full_name", "")
                        or ""
                    ).strip(),
                    use_case=str(
                        self.config.extra.get("cerebras_use_case")
                        or config_store.get("cerebras_use_case", "hobbyist")
                        or "hobbyist"
                    ).strip(),
                    verification_link_callback=verify_link_cb if self.mailbox else None,
                )
                break
            except Exception as exc:
                last_error = exc
                msg = str(exc)
                should_retry = (
                    attempt < mailbox_attempts
                    and not email
                    and (
                        "邮箱域名被封禁" in msg
                        or "组织状态异常" in msg
                        or "当前账号没有可用组织" in msg
                        or "This organization does not exist" in msg
                    )
                )
                if should_retry:
                    log(f"Cerebras 当前邮箱不可用，切换新邮箱重试 {attempt + 1}/{mailbox_attempts}")
                    continue
                raise
        else:
            raise last_error if last_error else RuntimeError("Cerebras 注册失败")

        return Account(
            platform="cerebras",
            email=result["email"],
            password=result["password"],
            token=result["api_key"],
            status=AccountStatus.REGISTERED,
            extra={
                "api_key": result["api_key"],
                "base_url": result["base_url"],
                "organization_id": result.get("organization_id", ""),
                "organization_name": result.get("organization_name", ""),
                "project_id": result.get("project_id", ""),
                "project_name": result.get("project_name", ""),
                "api_key_id": result.get("api_key_id", ""),
                "api_key_name": result.get("api_key_name", ""),
                "api_key_created_at": result.get("api_key_created_at", ""),
                "api_key_last_used_at": result.get("api_key_last_used_at", ""),
                "region_id": result.get("region_id", ""),
                "region_name": result.get("region_name", ""),
            },
        )

    def check_valid(self, account: Account) -> bool:
        import requests

        extra = account.extra or {}
        api_key = str(extra.get("api_key") or account.token or "").strip()
        if not api_key:
            return False

        base_url = str(extra.get("base_url") or "https://api.cerebras.ai").rstrip("/")
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
