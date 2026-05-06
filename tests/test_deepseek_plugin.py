import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import Account, RegisterConfig
from platforms.deepseek.plugin import DeepSeekPlatform


class DummyMailbox:
    def __init__(self, email: str):
        self._account = MailboxAccount(email=email, account_id="mail-token")
        self.get_email_calls = 0

    def get_email(self):
        self.get_email_calls += 1
        return self._account

    def get_current_ids(self, account):
        return {"msg-1"}

    def wait_for_code(self, account, keyword, timeout, before_ids, **kwargs):
        assert account.email == self._account.email
        assert keyword == "DeepSeek"
        assert before_ids == {"msg-1"}
        assert "otp_sent_at" in kwargs
        return "123456"


class DeepSeekPluginTests(unittest.TestCase):
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
                }
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

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
        self.assertTrue(register_via_browser.call_args.kwargs["headless"])
        client.login.assert_not_called()
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
            config=RegisterConfig(executor_type="headless"),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

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
