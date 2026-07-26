"""Z.ai 平台插件。"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Iterable
from typing import Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register
from platforms.zai.core import ZaiRegister, random_password, verify_zai_token


_ZAI_VERIFY_LINK_RE = re.compile(
    r"https://chat\.z\.ai/auth/verify_email\?[^\s\"'>]+",
    re.IGNORECASE,
)


def _extract_zai_verify_link(*texts: str) -> str:
    for text in texts:
        normalized = html.unescape(str(text or ""))
        normalized = normalized.replace("=\r\n", "").replace("=\n", "")
        normalized = normalized.replace("=3D", "=").replace("&amp;", "&")
        match = _ZAI_VERIFY_LINK_RE.search(normalized)
        if match:
            return html.unescape(match.group(0)).replace("&amp;", "&")
    return ""


def _mail_message_id(mailbox: BaseMailbox, message: dict) -> str:
    resolve_message_id = getattr(mailbox, "_resolve_message_id", None)
    if callable(resolve_message_id):
        try:
            value = str(resolve_message_id(message) or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    message_id_value = getattr(mailbox, "_message_id_value", None)
    if callable(message_id_value):
        try:
            value = str(message_id_value(message) or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    for key in ("id", "message_id", "uid", "mail_id", "_id"):
        value = str(message.get(key) or "").strip()
        if value:
            return value
    return json.dumps(message, ensure_ascii=False, sort_keys=True)


def _mail_search_text(mailbox: BaseMailbox, message: dict) -> str:
    message_search_text = getattr(mailbox, "_message_search_text", None)
    if callable(message_search_text):
        try:
            text = str(message_search_text(message) or "").strip()
        except Exception:
            text = ""
        if text:
            return text
    parts = [
        message.get("subject"),
        message.get("content"),
        message.get("html"),
        message.get("text"),
        message.get("body"),
        message.get("raw"),
    ]
    return "\n".join(str(item or "") for item in parts if str(item or "").strip()).strip()


def _iter_message_strings(value, *, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        priority_keys = ("html", "raw", "content", "body", "text", "message", "payload")
        for key in priority_keys:
            if key in value:
                yield from _iter_message_strings(value.get(key), depth=depth + 1)
        for key, item in value.items():
            if key in priority_keys:
                continue
            yield from _iter_message_strings(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_message_strings(item, depth=depth + 1)
        return


def _extract_zai_verify_link_from_message(mailbox: BaseMailbox, message: dict) -> str:
    decode_raw = getattr(mailbox, "_decode_raw_content", None)
    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(text: str) -> None:
        value = str(text or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for text in _iter_message_strings(message):
        add_candidate(text)
        if callable(decode_raw):
            try:
                add_candidate(decode_raw(text))
            except Exception:
                pass

    add_candidate(_mail_search_text(mailbox, message))
    return _extract_zai_verify_link(*candidates)


@register
class ZaiPlatform(BasePlatform):
    name = "zai"
    display_name = "Z.ai"
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

        log = getattr(self, "_log_fn", print)
        if email:
            raise RuntimeError("Z.ai 当前仅支持 mailbox provider 自动分配邮箱")
        if not self.mailbox:
            raise RuntimeError("Z.ai 注册需要 mailbox provider 支持自动收信")

        yescaptcha_key = self.config.extra.get("yescaptcha_key") or config_store.get(
            "yescaptcha_key", ""
        )
        captcha_solver = self._make_captcha(key=yescaptcha_key)
        password_value = password or random_password()
        requested_headless = (self.config.executor_type or "headed") != "headed"
        mailbox_attempts = int(
            self.config.extra.get("zai_mailbox_attempts")
            or config_store.get("zai_mailbox_attempts", "3")
            or 3
        )
        last_error = None

        for attempt in range(1, mailbox_attempts + 1):
            mail_acct = self.mailbox.get_email()
            current_email = mail_acct.email if mail_acct else ""
            if not current_email:
                raise RuntimeError("未获取到可用邮箱")

            before_ids = self.mailbox.get_current_ids(mail_acct) if mail_acct else set()
            otp_timeout = self.get_mailbox_otp_timeout(default=180)
            used_links: set[str] = set()

            def verify_link_cb() -> str:
                if not self.mailbox or not mail_acct:
                    return ""
                fetch_mails = getattr(self.mailbox, "_get_mails", None)
                client = getattr(self.mailbox, "_get_client", None)
                deadline = time.time() + otp_timeout
                before_id_texts = {str(item) for item in before_ids or set()}
                while time.time() < deadline:
                    self._checkpoint_mailbox()
                    try:
                        if callable(fetch_mails):
                            mails = fetch_mails(mail_acct.email)
                        elif callable(client):
                            mails = client().get_messages(mail_acct.email)
                        else:
                            return ""
                    except Exception:
                        mails = []
                    for mail in sorted(
                        mails,
                        key=lambda item: _mail_message_id(self.mailbox, item),
                        reverse=True,
                    ):
                        mid = _mail_message_id(self.mailbox, mail)
                        if mid and mid in before_id_texts:
                            continue
                        link = _extract_zai_verify_link_from_message(self.mailbox, mail)
                        if not link or link in used_links:
                            continue
                        used_links.add(link)
                        log("发现 Z.ai 邮件验证链接")
                        return link
                    time.sleep(3)
                return ""

            reg = ZaiRegister(
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
                    verification_link_callback=verify_link_cb,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt < mailbox_attempts:
                    log(f"Z.ai 当前邮箱注册失败，切换新邮箱重试 {attempt + 1}/{mailbox_attempts}: {exc}")
                    continue
                raise
        else:
            raise last_error if last_error else RuntimeError("Z.ai 注册失败")

        return Account(
            platform="zai",
            email=result["email"],
            password=result["password"],
            user_id=result.get("user_id", ""),
            token=result["token"],
            status=AccountStatus.REGISTERED,
            extra={
                "username": result.get("username", ""),
                "token_type": result.get("token_type", "Bearer"),
                "profile_image_url": result.get("profile_image_url", ""),
                "captcha_verify_param": result.get("captcha_verify_param"),
                "verify_link": result.get("verify_link", ""),
                "register_via": "browser",
            },
        )

    def check_valid(self, account: Account) -> bool:
        return verify_zai_token(account.token, proxy=self.config.proxy)

    def _checkpoint_mailbox(self) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint()
