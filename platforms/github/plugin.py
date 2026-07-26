"""GitHub 平台插件"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.flags import FEATURE_GITHUB_REGISTER, require_flag
from core.registry import register


@register
class GitHubPlatform(BasePlatform):
    name = "github"
    display_name = "GitHub"
    version = "1.0.0"
    supported_executors = ["headed", "headless"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _checkpoint(self):
        control = getattr(self, "_task_control", None)
        if control is not None and hasattr(control, "checkpoint"):
            control.checkpoint()

    def register(self, email: str = None, password: str = None) -> Account:
        require_flag(FEATURE_GITHUB_REGISTER)
        from platforms.github.core import register_github, rand_password

        log = getattr(self, "_log_fn", print)
        extra = self.config.extra or {}

        mail_acct = self.mailbox.get_email() if self.mailbox else None
        email = email or (mail_acct.email if mail_acct else None)
        if not email:
            raise RuntimeError("GitHub 注册需要邮箱")
        password = password or rand_password()
        log(f"[GitHub] 邮箱: {email}")
        before_ids = self.mailbox.get_current_ids(mail_acct) if mail_acct and self.mailbox else set()
        otp_timeout = self.get_mailbox_otp_timeout(default=180)

        captcha = None
        try:
            captcha = self._make_captcha()
        except Exception as exc:
            log(f"[GitHub] captcha 初始化: {exc}")

        skip_raw = str(extra.get("github_skip_captcha_variants") or "character")
        skip_variants = tuple(x.strip() for x in skip_raw.split(",") if x.strip()) or ("character",)

        if self.config.executor_type not in ("headed", "headless"):
            self.config.executor_type = "headed"

        with self._make_executor() as ex:
            page = ex.page
            result = register_github(
                page,
                email=email,
                password=password,
                username=extra.get("github_username") or None,
                captcha=captcha,
                mailbox=self.mailbox,
                mail_account=mail_acct,
                before_ids=before_ids,
                otp_timeout=otp_timeout,
                log_fn=log,
                control=getattr(self, "_task_control", None),
                skip_variants=skip_variants,
            )

        return Account(
            platform="github",
            email=result["email"],
            password=result["password"],
            user_id=result.get("username") or "",
            token="",
            status=AccountStatus.REGISTERED,
            extra={
                "username": result.get("username") or "",
                "cookies": result.get("cookies") or {},
                "signup_url": result.get("url") or "",
            },
        )

    def check_valid(self, account: Account) -> bool:
        cookies = (account.extra or {}).get("cookies") or {}
        return bool(cookies.get("user_session") or cookies.get("logged_in") or account.email)
