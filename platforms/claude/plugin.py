"""Claude 平台插件（feature_claude_register 门禁，默认关）"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.flags import FEATURE_CLAUDE_REGISTER, FEATURE_VISION_CAPTCHA, flag_enabled, require_flag
from core.registry import register


@register
class ClaudePlatform(BasePlatform):
    name = "claude"
    display_name = "Claude"
    version = "1.0.0"
    supported_executors = ["headed", "headless"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str = None, password: str = None) -> Account:
        require_flag(FEATURE_CLAUDE_REGISTER)
        from platforms.claude.core import register_claude

        log = getattr(self, "_log_fn", print)
        extra = self.config.extra or {}
        mail_acct = self.mailbox.get_email() if self.mailbox else None
        email = email or (mail_acct.email if mail_acct else None)
        if not email:
            raise RuntimeError("Claude 注册需要邮箱")
        log(f"[Claude] 邮箱: {email}")

        captcha = None
        try:
            captcha = self._make_captcha()
        except Exception as exc:
            log(f"[Claude] captcha init: {exc}")

        use_vision = flag_enabled(FEATURE_VISION_CAPTCHA) or str(
            extra.get("claude_use_vision", "")
        ).lower() in {"1", "true", "yes", "on"}

        if self.config.executor_type not in ("headed", "headless"):
            self.config.executor_type = "headed"

        with self._make_executor() as ex:
            result = register_claude(
                ex.page,
                email=email,
                mailbox=self.mailbox,
                mail_account=mail_acct,
                captcha=captcha,
                proxy=self.config.proxy,
                log_fn=log,
                control=getattr(self, "_task_control", None),
                otp_timeout=self.get_mailbox_otp_timeout(default=180),
                use_vision=use_vision,
            )

        return Account(
            platform="claude",
            email=result["email"],
            password=password or "",
            token=result.get("session_key") or "",
            status=AccountStatus.REGISTERED,
            extra={
                "session_key": result.get("session_key") or "",
                "cookies": result.get("cookies") or {},
                "url": result.get("url") or "",
            },
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token or (account.extra or {}).get("session_key") or (account.extra or {}).get("cookies"))
