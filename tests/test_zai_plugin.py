import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import RegisterConfig
from platforms.zai.plugin import (
    ZaiPlatform,
    _extract_zai_verify_link,
    _extract_zai_verify_link_from_message,
)


class DummyMailbox:
    def __init__(self, email: str):
        self._account = MailboxAccount(email=email, account_id="token")

    def get_email(self):
        return self._account

    def get_current_ids(self, account):
        return set()

    def _decode_raw_content(self, raw: str) -> str:
        return (
            raw.replace("=\r\n", "")
            .replace("=\n", "")
            .replace("=3D", "=")
            .replace("&amp;", "&")
        )

    def _get_mails(self, email: str):
        return [
            {
                "id": "1",
                "subject": "验证您的电子邮箱",
                "raw": (
                    "Click here:\n"
                    "https://chat.z.ai/auth/verify_email?token=3Dverify-demo-token&amp;email=3Duser%40example.com&amp;username=3Dzaiuser"
                ),
            }
        ]


class DummyClientMailbox:
    def __init__(self, email: str):
        self._account = MailboxAccount(email=email, account_id="token")

    def get_email(self):
        return self._account

    def get_current_ids(self, account):
        return set()

    def _get_client(self):
        client = mock.Mock()
        client.get_messages.return_value = [
            {
                "id": "1",
                "subject": "验证您的电子邮箱",
                "content": (
                    "Click here:\n"
                    "https://chat.z.ai/auth/verify_email?token=3Dverify-demo-token&amp;email=3Duser%40example.com&amp;username=3Dzaiuser"
                ),
            }
        ]
        return client


class DummyHtmlAnchorMailbox:
    def __init__(self, email: str):
        self._account = MailboxAccount(email=email, account_id="token")

    def get_email(self):
        return self._account

    def get_current_ids(self, account):
        return set()

    def _decode_raw_content(self, raw: str) -> str:
        return (
            raw.replace("=\r\n", "")
            .replace("=\n", "")
            .replace("=3D", "=")
            .replace("&amp;", "&")
        )

    def _message_search_text(self, message):
        return "请点击按钮验证邮箱"

    def _get_client(self):
        client = mock.Mock()
        client.get_messages.return_value = [
            {
                "id": "1",
                "subject": "验证您的电子邮箱",
                "content": (
                    '<p>请点击按钮</p>'
                    '<a href="https://chat.z.ai/auth/verify_email?token=3Dverify-demo-token&amp;email=3Duser%40example.com&amp;username=3Dzaiuser">'
                    "验证邮箱"
                    "</a>"
                ),
            }
        ]
        return client


class ZaiPluginTests(unittest.TestCase):
    def test_extract_zai_verify_link_decodes_quoted_printable_href(self):
        raw = (
            "https://chat.z.ai/auth/verify_email?token=3Dverify-demo-token&amp;email=3Duser%40example.com&amp;username=3Dzaiuser"
        )
        self.assertEqual(
            _extract_zai_verify_link(raw),
            "https://chat.z.ai/auth/verify_email?token=verify-demo-token&email=user%40example.com&username=zaiuser",
        )

    def test_extract_zai_verify_link_from_message_prefers_raw_html_href(self):
        mailbox = DummyHtmlAnchorMailbox("user@example.com")
        link = _extract_zai_verify_link_from_message(
            mailbox,
            {
                "id": "1",
                "subject": "验证您的电子邮箱",
                "content": (
                    '<p>请点击按钮</p>'
                    '<a href="https://chat.z.ai/auth/verify_email?token=3Dverify-demo-token&amp;email=3Duser%40example.com&amp;username=3Dzaiuser">'
                    "验证邮箱"
                    "</a>"
                ),
            },
        )

        self.assertEqual(
            link,
            "https://chat.z.ai/auth/verify_email?token=verify-demo-token&email=user%40example.com&username=zaiuser",
        )

    def test_register_uses_mailbox_verify_link_and_returns_bearer_account(self):
        mailbox = DummyMailbox("user@example.com")
        platform = ZaiPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"zai_mailbox_attempts": 1},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        def register_side_effect(**kwargs):
            link = kwargs["verification_link_callback"]()
            self.assertIn("chat.z.ai/auth/verify_email", link)
            self.assertEqual(kwargs["email"], "user@example.com")
            return {
                "email": "user@example.com",
                "password": "ZaiPass!Aa1",
                "token": "zai-bearer-token",
                "token_type": "Bearer",
                "user_id": "user-id-123",
                "username": "zaiuser",
                "profile_image_url": "/user.png",
                "verify_link": link,
            }

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.zai.plugin.ZaiRegister") as register_cls:
                register_cls.return_value.register.side_effect = register_side_effect
                account = platform.register(email="", password="ZaiPass!Aa1")

        self.assertEqual(account.platform, "zai")
        self.assertEqual(account.email, "user@example.com")
        self.assertEqual(account.token, "zai-bearer-token")
        self.assertEqual(account.user_id, "user-id-123")
        self.assertEqual(account.extra["username"], "zaiuser")
        self.assertEqual(account.extra["token_type"], "Bearer")

    def test_register_uses_client_messages_when_get_mails_absent(self):
        mailbox = DummyClientMailbox("user@example.com")
        platform = ZaiPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"zai_mailbox_attempts": 1},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        def register_side_effect(**kwargs):
            link = kwargs["verification_link_callback"]()
            self.assertIn("chat.z.ai/auth/verify_email", link)
            return {
                "email": "user@example.com",
                "password": "ZaiPass!Aa1",
                "token": "zai-bearer-token",
                "token_type": "Bearer",
                "user_id": "user-id-123",
                "username": "zaiuser",
                "profile_image_url": "/user.png",
                "verify_link": link,
            }

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.zai.plugin.ZaiRegister") as register_cls:
                register_cls.return_value.register.side_effect = register_side_effect
                account = platform.register(email="", password="ZaiPass!Aa1")

        self.assertEqual(account.email, "user@example.com")

    def test_register_uses_raw_html_href_when_mailbox_search_text_hides_link(self):
        mailbox = DummyHtmlAnchorMailbox("user@example.com")
        platform = ZaiPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"zai_mailbox_attempts": 1},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        def register_side_effect(**kwargs):
            link = kwargs["verification_link_callback"]()
            self.assertEqual(
                link,
                "https://chat.z.ai/auth/verify_email?token=verify-demo-token&email=user%40example.com&username=zaiuser",
            )
            return {
                "email": "user@example.com",
                "password": "ZaiPass!Aa1",
                "token": "zai-bearer-token",
                "token_type": "Bearer",
                "user_id": "user-id-123",
                "username": "zaiuser",
                "profile_image_url": "/user.png",
                "verify_link": link,
            }

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.zai.plugin.ZaiRegister") as register_cls:
                register_cls.return_value.register.side_effect = register_side_effect
                account = platform.register(email="", password="ZaiPass!Aa1")

        self.assertEqual(account.extra["verify_link"], "https://chat.z.ai/auth/verify_email?token=verify-demo-token&email=user%40example.com&username=zaiuser")

    def test_register_rejects_direct_email_mode(self):
        platform = ZaiPlatform(
            config=RegisterConfig(executor_type="headless", captcha_solver="manual"),
            mailbox=None,
        )

        with self.assertRaisesRegex(RuntimeError, "仅支持 mailbox provider"):
            platform.register(email="user@example.com", password="ZaiPass!Aa1")


if __name__ == "__main__":
    unittest.main()
