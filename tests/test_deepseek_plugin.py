import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import Account, RegisterConfig
from platforms.deepseek.core import DeepSeekEmailDomainRejected
from platforms.deepseek.plugin import DeepSeekPlatform


class DummyMailbox:
    def __init__(self, email: str | list[str]):
        emails = [email] if isinstance(email, str) else list(email)
        self._accounts = [
            MailboxAccount(email=item, account_id=f"mail-token-{index}")
            for index, item in enumerate(emails, 1)
        ]
        self._account = self._accounts[0]
        self.get_email_calls = 0
        self.blacklisted_domains: list[tuple[str, str]] = []

    def get_email(self):
        self.get_email_calls += 1
        if self.get_email_calls <= len(self._accounts):
            self._account = self._accounts[self.get_email_calls - 1]
        return self._account

    def get_current_ids(self, account):
        return {"msg-1"}

    def wait_for_code(self, account, keyword, timeout, before_ids, **kwargs):
        assert account.email == self._account.email
        assert keyword == "DeepSeek"
        assert before_ids == {"msg-1"}
        assert "otp_sent_at" in kwargs
        return "123456"

    def blacklist_domain(self, domain: str, *, reason: str = "") -> None:
        self.blacklisted_domains.append((domain, reason))


class DeepSeekPluginTests(unittest.TestCase):
    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    @mock.patch.dict(
        "os.environ",
        {"DEEPSEEK_FLARESOLVERR_URL": "http://127.0.0.1:8191/v1"},
        clear=False,
    )
    def test_register_does_not_enable_flaresolverr_from_env_by_default(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox("user@example.com")
        client = client_cls.return_value
        client.ui_locale = "en-US"
        client.region = "US"
        client.device_id = "deepseek-device-env"
        client.tz_offset_seconds = "-25200"
        client.pow_worker_url = "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        ensure_email_sign_up_available.return_value = {
            "classification": "email_form",
        }
        register_via_browser.return_value = {
            "code": "123456",
            "final_url": "https://chat.deepseek.com/",
            "register_user": {
                "id": "deepseek-user-env",
                "token": "deepseek-token-env",
                "email": "user@example.com",
                "need_birthday": False,
            },
        }

        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headless",
                extra={"yescaptcha_key": "client-key"},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            account = platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(account.user_id, "deepseek-user-env")
        ensure_email_sign_up_available.assert_called_once_with(
            proxy=None,
            ui_locale="en-US",
            headless=True,
            user_data_dir=None,
            flaresolverr_url=None,
        )
        self.assertIsNone(register_via_browser.call_args.kwargs["flaresolverr_url"])
        client.close.assert_called_once()

    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_register_uses_browser_register_payload_without_login(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox("user@example.com")
        client = client_cls.return_value
        client.ui_locale = "ja-JP"
        client.region = "US"
        client.device_id = "deepseek-device-1"
        client.tz_offset_seconds = "32400"
        client.pow_worker_url = "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        ensure_email_sign_up_available.return_value = {
            "classification": "email_form",
        }
        register_via_browser.return_value = {
            "code": "123456",
            "final_url": "https://chat.deepseek.com/",
            "register_user": {
                "id": "deepseek-user-1",
                "token": "deepseek-token",
                "email": "user@example.com",
                "need_birthday": True,
            },
        }

        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headless",
                extra={
                    "deepseek_ui_locale": "ja-JP",
                    "deepseek_region": "US",
                    "deepseek_tz_offset_seconds": "32400",
                    "deepseek_pow_worker_url": "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js",
                    "deepseek_flaresolverr_url": "http://127.0.0.1:8191/v1",
                    "deepseek_hcaptcha_sitekey": "352e5376-f2cc-43fe-a744-e51640449610",
                    "deepseek_browser_user_data_dir": "F:/tmp/deepseek-profile",
                    "yescaptcha_key": "client-key",
                }
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        captcha_solver = mock.Mock()
        with mock.patch.object(platform, "_make_captcha", return_value=captcha_solver):
            account = platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(account.platform, "deepseek")
        self.assertEqual(account.email, "user@example.com")
        self.assertEqual(account.password, "Aa1!demoPass")
        self.assertEqual(account.user_id, "deepseek-user-1")
        self.assertEqual(account.token, "deepseek-token")
        self.assertEqual(account.extra["username"], "user@example.com")
        self.assertTrue(account.extra["need_birthday"])
        self.assertEqual(account.extra["device_id"], "deepseek-device-1")
        self.assertEqual(account.extra["register_via"], "browser")

        register_via_browser.assert_called_once()
        ensure_email_sign_up_available.assert_called_once_with(
            proxy=None,
            ui_locale="ja-JP",
            headless=True,
            user_data_dir="F:/tmp/deepseek-profile",
            flaresolverr_url="http://127.0.0.1:8191/v1",
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["email"],
            "user@example.com",
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["password"],
            "Aa1!demoPass",
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["ui_locale"],
            "ja-JP",
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["flaresolverr_url"],
            "http://127.0.0.1:8191/v1",
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["user_data_dir"],
            "F:/tmp/deepseek-profile",
        )
        self.assertIs(
            register_via_browser.call_args.kwargs["captcha_solver"],
            captcha_solver,
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["hcaptcha_sitekey"],
            "352e5376-f2cc-43fe-a744-e51640449610",
        )
        self.assertEqual(
            register_via_browser.call_args.kwargs["tz_offset_seconds"],
            "32400",
        )
        self.assertTrue(register_via_browser.call_args.kwargs["headless"])
        client.login.assert_not_called()
        client.close.assert_called_once()

    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_register_manual_handoff_uses_headed_browser_without_captcha_solver(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox("user@example.com")
        client = client_cls.return_value
        client.ui_locale = "en-US"
        client.region = "US"
        client.device_id = "deepseek-device-manual"
        client.tz_offset_seconds = "-25200"
        client.pow_worker_url = "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        ensure_email_sign_up_available.return_value = {
            "classification": "email_form",
        }
        register_via_browser.return_value = {
            "code": "123456",
            "final_url": "https://chat.deepseek.com/",
            "register_user": {
                "id": "deepseek-user-manual",
                "token": "deepseek-token-manual",
                "email": "user@example.com",
                "need_birthday": False,
            },
        }

        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headed",
                captcha_solver="manual",
                extra={
                    "deepseek_hcaptcha_sitekey": "352e5376-f2cc-43fe-a744-e51640449610",
                },
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha") as make_captcha:
            account = platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(account.user_id, "deepseek-user-manual")
        make_captcha.assert_not_called()
        ensure_email_sign_up_available.assert_called_once_with(
            proxy=None,
            ui_locale="en-US",
            headless=False,
            user_data_dir=None,
            flaresolverr_url=None,
        )
        self.assertFalse(register_via_browser.call_args.kwargs["headless"])
        self.assertTrue(register_via_browser.call_args.kwargs["manual_send_code_handoff"])
        self.assertIsNone(register_via_browser.call_args.kwargs["captcha_solver"])
        client.login.assert_not_called()
        client.close.assert_called_once()

    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_register_manual_handoff_requires_headed_executor(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox("user@example.com")
        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha") as make_captcha:
            with self.assertRaisesRegex(RuntimeError, "executor_type=headed"):
                platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(mailbox.get_email_calls, 0)
        make_captcha.assert_not_called()
        client_cls.assert_not_called()
        ensure_email_sign_up_available.assert_not_called()
        register_via_browser.assert_not_called()

    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_register_blacklists_unsupported_email_domain_and_retries(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox(
            ["first@mail.highkay.qzz.io", "second@example.com"]
        )
        client = client_cls.return_value
        client.ui_locale = "en-US"
        client.region = "US"
        client.device_id = "deepseek-device-retry"
        client.tz_offset_seconds = "-25200"
        client.pow_worker_url = "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        ensure_email_sign_up_available.return_value = {
            "classification": "email_form",
        }
        register_via_browser.side_effect = [
            DeepSeekEmailDomainRejected(
                "first@mail.highkay.qzz.io",
                {"biz_code": 4, "biz_msg": "EMAIL_DOMAIN_NOT_SUPPORTED"},
            ),
            {
                "code": "123456",
                "final_url": "https://chat.deepseek.com/",
                "register_user": {
                    "id": "deepseek-user-retry",
                    "token": "deepseek-token-retry",
                    "email": "second@example.com",
                    "need_birthday": False,
                },
            },
        ]

        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headless",
                extra={
                    "deepseek_mailbox_attempts": 2,
                    "yescaptcha_key": "client-key",
                },
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            account = platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(account.email, "second@example.com")
        self.assertEqual(mailbox.blacklisted_domains[0][0], "mail.highkay.qzz.io")
        self.assertIn("DeepSeek 发码接口拒绝", mailbox.blacklisted_domains[0][1])
        self.assertEqual(register_via_browser.call_count, 2)
        self.assertEqual(
            register_via_browser.call_args_list[0].kwargs["email"],
            "first@mail.highkay.qzz.io",
        )
        self.assertEqual(
            register_via_browser.call_args_list[1].kwargs["email"],
            "second@example.com",
        )
        client.close.assert_called_once()

    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_register_falls_back_to_login_when_browser_state_lacks_user(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox("user@example.com")
        client = client_cls.return_value
        client.ui_locale = "ja-JP"
        client.region = "US"
        client.device_id = "deepseek-device-2"
        client.tz_offset_seconds = "32400"
        client.pow_worker_url = "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        ensure_email_sign_up_available.return_value = {
            "classification": "email_form",
        }
        register_via_browser.return_value = {
            "code": "123456",
            "final_url": "https://chat.deepseek.com/",
        }
        client.login.return_value = {
            "data": {
                "biz_code": 0,
                "biz_data": {
                    "user": {
                        "id": "deepseek-user-2",
                        "token": "deepseek-token-2",
                        "email": "user@example.com",
                        "need_birthday": False,
                    }
                },
            }
        }

        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headless",
                extra={
                    "deepseek_hcaptcha_sitekey": "352e5376-f2cc-43fe-a744-e51640449610",
                    "yescaptcha_key": "client-key",
                },
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            account = platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(account.user_id, "deepseek-user-2")
        self.assertEqual(account.token, "deepseek-token-2")
        self.assertFalse(account.extra["need_birthday"])
        client.login.assert_called_once_with(
            email="user@example.com",
            password="Aa1!demoPass",
        )
        client.close.assert_called_once()

    @mock.patch("platforms.deepseek.plugin.ensure_deepseek_email_sign_up_available_via_browser")
    @mock.patch("platforms.deepseek.plugin.register_deepseek_via_browser")
    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_register_fails_before_allocating_mailbox_when_sign_up_entry_is_not_email_form(
        self,
        client_cls,
        register_via_browser,
        ensure_email_sign_up_available,
    ):
        mailbox = DummyMailbox("user@example.com")
        client = client_cls.return_value
        client.ui_locale = "ja-JP"
        client.region = "US"
        client.device_id = "deepseek-device-3"
        client.tz_offset_seconds = "32400"
        client.pow_worker_url = "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        ensure_email_sign_up_available.side_effect = RuntimeError(
            "DeepSeek 当前出口命中手机号注册页，不支持邮箱注册: classification=phone_only"
        )

        platform = DeepSeekPlatform(
            config=RegisterConfig(
                executor_type="headless",
                proxy="socks5h://192.168.1.18:1080",
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        with self.assertRaisesRegex(RuntimeError, "DeepSeek 当前出口命中手机号注册页"):
            platform.register(email="", password="Aa1!demoPass")

        self.assertEqual(mailbox.get_email_calls, 0)
        register_via_browser.assert_not_called()
        ensure_email_sign_up_available.assert_called_once_with(
            proxy="socks5h://192.168.1.18:1080",
            ui_locale="ja-JP",
            headless=True,
            user_data_dir=None,
            flaresolverr_url=None,
        )
        client.close.assert_called_once()

    @mock.patch("platforms.deepseek.plugin.DeepSeekClient")
    def test_check_valid_returns_true_only_for_biz_code_zero(self, client_cls):
        client = client_cls.return_value
        client.login.return_value = {"data": {"biz_code": 0}}
        platform = DeepSeekPlatform(config=RegisterConfig(executor_type="headless"))

        self.assertTrue(
            platform.check_valid(
                Account(platform="deepseek", email="user@example.com", password="Aa1!demoPass")
            )
        )

        client.login.return_value = {"data": {"biz_code": 2}}
        self.assertFalse(
            platform.check_valid(
                Account(platform="deepseek", email="user@example.com", password="Aa1!demoPass")
            )
        )


if __name__ == "__main__":
    unittest.main()
