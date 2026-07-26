import unittest
from unittest import mock

from core.base_platform import RegisterConfig
from platforms.grok.plugin import GrokPlatform


class DummyMailboxAccount:
    def __init__(self, email: str):
        self.email = email
        self.account_id = email


class DummyMailbox:
    def __init__(self, emails: list[str]):
        self._emails = list(emails)
        self.blacklisted_domains: list[tuple[str, str]] = []

    def get_email(self):
        return DummyMailboxAccount(self._emails.pop(0))

    def get_current_ids(self, account):
        return set()

    def wait_for_code(self, *args, **kwargs):
        return "ABC123"

    def blacklist_domain(self, domain: str, *, reason: str = "") -> None:
        self.blacklisted_domains.append((domain, reason))


class GrokPluginTests(unittest.TestCase):
    def test_merge_protocol_extra_fills_from_config_store(self):
        platform = GrokPlatform(
            config=RegisterConfig(executor_type="protocol", extra={"grok_turnstile_mode": "offscreen"}),
        )
        with mock.patch("core.config_store.config_store.get") as get_mock:
            get_mock.side_effect = lambda key, default="": {
                "grok_clearance_mode": "never",
                "grok_cf_impersonate": "chrome131",
            }.get(key, default)
            merged = platform._merge_protocol_extra()
        self.assertEqual(merged["grok_turnstile_mode"], "offscreen")
        self.assertEqual(merged["grok_clearance_mode"], "never")
        self.assertEqual(merged["grok_cf_impersonate"], "chrome131")

    def test_register_uses_protocol_chain_for_protocol_executor(self):
        mailbox = DummyMailbox(["user@example.com"])
        platform = GrokPlatform(
            config=RegisterConfig(
                executor_type="protocol",
                captcha_solver="manual",
                extra={},
            ),
            mailbox=mailbox,
        )
        logs: list[str] = []
        platform._log_fn = logs.append

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch(
                "platforms.grok.protocol_register.GrokProtocolRegister"
            ) as protocol_cls:
                protocol_cls.return_value.register.return_value = {
                    "email": "user@example.com",
                    "password": "GrokPass!Aa1",
                    "given_name": "Demo",
                    "family_name": "User",
                    "sso": "sso-token",
                    "sso_rw": "sso-rw-token",
                    "register_mode": "protocol",
                }
                with mock.patch("platforms.grok.core.GrokRegister") as browser_cls:
                    account = platform.register(email="", password="GrokPass!Aa1")

        protocol_cls.assert_called_once()
        browser_cls.assert_not_called()
        self.assertIn(
            account.extra.get("register_mode"),
            {"protocol", "protocol+native_ui"},
        )
        self.assertTrue(
            any(
                ("协议清障" in entry)
                or ("有头浏览器" in entry)
                or ("协议注册" in entry)
                for entry in logs
            ),
        )

    def test_register_passes_task_control_to_grok_register(self):
        mailbox = DummyMailbox(["user@example.com"])
        platform = GrokPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None
        platform._task_control = mock.Mock()

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                register_cls.return_value.register.return_value = {
                    "email": "user@example.com",
                    "password": "GrokPass!Aa1",
                    "given_name": "Demo",
                    "family_name": "User",
                    "sso": "sso-token",
                    "sso_rw": "sso-rw-token",
                }
                platform.register(email="", password="GrokPass!Aa1")

        self.assertIs(
            register_cls.call_args.kwargs["task_control"],
            platform._task_control,
        )

    def test_register_persists_runtime_for_follow_up_device_oauth(self):
        mailbox = DummyMailbox(["user@example.com"])
        platform = GrokPlatform(
            config=RegisterConfig(
                executor_type="headed",
                proxy="socks5h://proxy:1080",
                extra={
                    "grok_flaresolverr_url": "http://solver:8191/v1",
                    "grok_flaresolverr_attempts": "4",
                    "yescaptcha_key": "not-saved-in-account-extra",
                },
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                register_cls.return_value.register.return_value = {
                    "email": "user@example.com",
                    "password": "GrokPass!Aa1",
                    "given_name": "Demo",
                    "family_name": "User",
                    "sso": "sso-token",
                    "sso_rw": "sso-rw-token",
                }
                account = platform.register(email="", password="GrokPass!Aa1")

        runtime = account.extra["grok_registration_runtime"]
        self.assertFalse(runtime["browser_headless"])
        self.assertEqual(runtime["grok_flaresolverr_url"], "http://solver:8191/v1")
        self.assertEqual(runtime["grok_flaresolverr_attempts"], "4")
        self.assertNotIn("yescaptcha_key", runtime)
        self.assertEqual(account.extra["registration_proxy"], "socks5h://proxy:1080")

    def test_register_persists_xai_session_cookies_for_follow_up_device_oauth(self):
        mailbox = DummyMailbox(["user@example.com"])
        platform = GrokPlatform(
            config=RegisterConfig(executor_type="headed", captcha_solver="manual", extra={}),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                register_cls.return_value.register.return_value = {
                    "email": "user@example.com",
                    "password": "GrokPass!Aa1",
                    "given_name": "Demo",
                    "family_name": "User",
                    "sso": "sso-token",
                    "sso_rw": "sso-rw-token",
                    "cookies": [
                        {
                            "name": "sso",
                            "value": "sso-token",
                            "domain": "accounts.x.ai",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                        },
                        {"name": "unrelated", "value": "skip", "domain": "accounts.x.ai"},
                    ],
                }
                account = platform.register(email="", password="GrokPass!Aa1")

        stored = account.extra["grok_session_cookies"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["name"], "sso")

    def test_register_persists_transient_submit_meta(self):
        mailbox = DummyMailbox(["user@example.com"])
        platform = GrokPlatform(
            config=RegisterConfig(executor_type="headed", captcha_solver="manual", extra={}),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                register_cls.return_value.register.return_value = {
                    "email": "user@example.com",
                    "password": "GrokPass!Aa1",
                    "given_name": "Demo",
                    "family_name": "User",
                    "sso": "sso-token",
                    "sso_rw": "sso-rw-token",
                    "register_submit_meta": {
                        "submit_attempts": 2,
                        "transient_error_retries": 1,
                        "saw_transient_error": True,
                        "email_transient_retries": 0,
                    },
                }
                account = platform.register(email="", password="GrokPass!Aa1")

        self.assertTrue(account.extra["register_had_transient_submit_error"])
        self.assertEqual(account.extra["register_submit_meta"]["transient_error_retries"], 1)

    def test_register_retries_new_mailbox_on_verification_failure(self):
        mailbox = DummyMailbox(["first@example.com", "second@example.com"])
        platform = GrokPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"grok_mailbox_attempts": 2},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        register_mock = mock.Mock()
        register_mock.side_effect = [
            TimeoutError("未收到验证码邮件"),
            {
                "email": "second@example.com",
                "password": "GrokPass!Aa1",
                "given_name": "Demo",
                "family_name": "User",
                "sso": "sso-token",
                "sso_rw": "sso-rw-token",
            },
        ]

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                register_cls.return_value.register = register_mock
                account = platform.register(email="", password="GrokPass!Aa1")

        self.assertEqual(account.email, "second@example.com")
        self.assertEqual(account.token, "sso-token")
        self.assertEqual(account.extra["sso_token"], "sso-token")
        self.assertEqual(register_mock.call_count, 2)
        first_call = register_mock.call_args_list[0]
        second_call = register_mock.call_args_list[1]
        self.assertEqual(first_call.kwargs["email"], "first@example.com")
        self.assertEqual(second_call.kwargs["email"], "second@example.com")

    def test_register_blacklists_rejected_domain_before_retry(self):
        mailbox = DummyMailbox(["first@blocked.example", "second@allowed.example"])
        platform = GrokPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"grok_mailbox_attempts": 2},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        register_mock = mock.Mock()
        register_mock.side_effect = [
            RuntimeError("邮箱域名被拒绝: disposable email is rejected"),
            {
                "email": "second@allowed.example",
                "password": "GrokPass!Aa1",
                "given_name": "Demo",
                "family_name": "User",
                "sso": "sso-token",
                "sso_rw": "sso-rw-token",
            },
        ]

        def config_get(key: str, default: str = "") -> str:
            return ""

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                with mock.patch(
                    "core.config_store.config_store.get",
                    side_effect=config_get,
                ):
                    with mock.patch("core.config_store.config_store.set") as set_mock:
                        register_cls.return_value.register = register_mock
                        account = platform.register(email="", password="GrokPass!Aa1")

        self.assertEqual(account.email, "second@allowed.example")
        self.assertEqual(
            mailbox.blacklisted_domains,
            [("blocked.example", "Grok 注册页拒绝该邮箱域名")],
        )
        set_mock.assert_called_once_with(
            "grok_blocked_email_domains",
            "blocked.example",
        )

    def test_register_retries_and_persists_domain_when_alt_rejection_copy_matches(self):
        mailbox = DummyMailbox(["first@blocked.example", "second@allowed.example"])
        platform = GrokPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"grok_mailbox_attempts": 2},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        register_mock = mock.Mock()
        register_mock.side_effect = [
            RuntimeError("邮箱提交失败: 请使用其他邮箱地址"),
            {
                "email": "second@allowed.example",
                "password": "GrokPass!Aa1",
                "given_name": "Demo",
                "family_name": "User",
                "sso": "sso-token",
                "sso_rw": "sso-rw-token",
            },
        ]

        def config_get(key: str, default: str = "") -> str:
            return ""

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.grok.core.GrokRegister") as register_cls:
                with mock.patch(
                    "core.config_store.config_store.get",
                    side_effect=config_get,
                ):
                    with mock.patch("core.config_store.config_store.set") as set_mock:
                        register_cls.return_value.register = register_mock
                        account = platform.register(email="", password="GrokPass!Aa1")

        self.assertEqual(account.email, "second@allowed.example")
        self.assertEqual(
            mailbox.blacklisted_domains,
            [("blocked.example", "Grok 注册页拒绝该邮箱域名")],
        )
        set_mock.assert_called_once_with(
            "grok_blocked_email_domains",
            "blocked.example",
        )


if __name__ == "__main__":
    unittest.main()
