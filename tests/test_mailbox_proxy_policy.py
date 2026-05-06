import unittest
from unittest.mock import patch

from core.base_mailbox import TempMailLolMailbox, create_mailbox
from core.proxy_utils import MAILBOX_PROXY_BYPASS_CONFIG
from core.proxy_utils import create_mailbox_requests_session


class MailboxProxyPolicyTests(unittest.TestCase):
    @staticmethod
    def _get_mailbox_proxy(mailbox):
        if hasattr(mailbox, "proxy"):
            return mailbox.proxy
        return getattr(mailbox, "_proxy", None)

    def test_factory_mailboxes_ignore_register_proxy(self):
        cases = [
            ("tempmail_lol", {}),
            (
                "skymail",
                {
                    "skymail_api_base": "https://api.skymail.ink",
                    "skymail_token": "token",
                    "skymail_domain": "mail.example",
                },
            ),
            (
                "duckmail",
                {
                    "duckmail_api_url": "https://www.duckmail.sbs",
                    "duckmail_provider_url": "https://api.duckmail.sbs",
                    "duckmail_bearer": "bearer",
                },
            ),
            (
                "cloudmail",
                {
                    "cloudmail_api_base": "https://cloudmail.example",
                    "cloudmail_admin_email": "admin@example.com",
                    "cloudmail_admin_password": "secret",
                    "cloudmail_domain": "mail.example",
                },
            ),
            (
                "freemail",
                {
                    "freemail_api_url": "https://example.invalid",
                    "freemail_admin_token": "admin-token",
                },
            ),
            ("moemail", {"moemail_api_url": "https://sall.cc"}),
            (
                "maliapi",
                {
                    "maliapi_base_url": "https://maliapi.215.im/v1",
                    "maliapi_api_key": "api-key",
                },
            ),
            (
                "gptmail",
                {
                    "gptmail_base_url": "https://mail.chatgpt.org.uk",
                    "gptmail_api_key": "gpt-test",
                },
            ),
            (
                "edumail",
                {
                    "edumail_base_url": "https://edumail.su",
                },
            ),
            (
                "imail",
                {
                    "imail_base_url": "https://imail.edu.vn",
                },
            ),
            (
                "edumaili",
                {
                    "edumaili_base_url": "https://edumaili.com",
                },
            ),
            (
                "boomlify",
                {
                    "boomlify_base_url": "https://boomlify.com/en/edu-temp-mail",
                    "boomlify_api_base": "https://v1.boomlify.com",
                },
            ),
            (
                "nullsto",
                {
                    "nullsto_base_url": "https://nullsto.edu.pl",
                },
            ),
            (
                "gptmail",
                {
                    "gptmail_base_url": "https://mail.chatgpt.org.uk",
                    "gptmail_mode": "automation",
                },
            ),
            (
                "applemail",
                {
                    "applemail_base_url": "https://www.appleemail.top",
                    "applemail_pool_dir": "mail",
                },
            ),
            (
                "opentrashmail",
                {
                    "opentrashmail_api_url": "https://trash.example",
                    "opentrashmail_domain": "trash.example",
                    "opentrashmail_password": "secret",
                },
            ),
            (
                "cfworker",
                {
                    "cfworker_api_url": "https://example.invalid",
                    "cfworker_admin_token": "admin-token",
                    "cfworker_domain": "mail.example",
                },
            ),
            (
                "cfrouting",
                {
                    "cfrouting_domain": "mail.example",
                    "cfrouting_imap_server": "imap.example",
                    "cfrouting_imap_port": 993,
                    "cfrouting_username": "demo@example.com",
                    "cfrouting_password": "secret",
                },
            ),
            (
                "outlook",
                {
                    "outlook_imap_server": "outlook.office365.com",
                    "outlook_token_endpoint": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                },
            ),
        ]

        for provider, extra in cases:
            with self.subTest(provider=provider):
                mailbox = create_mailbox(
                    provider,
                    extra=extra,
                    proxy="http://proxy.local:8080",
                )
                self.assertEqual(
                    self._get_mailbox_proxy(mailbox),
                    MAILBOX_PROXY_BYPASS_CONFIG,
                )
                self.assertIsNone(getattr(mailbox, "_proxy_url", None))

    def test_mailbox_session_ignores_environment_proxy(self):
        session = create_mailbox_requests_session(MAILBOX_PROXY_BYPASS_CONFIG)

        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, MAILBOX_PROXY_BYPASS_CONFIG)

    @patch.dict("os.environ", {"ALL_PROXY": "http://127.0.0.1:7899"}, clear=True)
    def test_mailbox_bypass_config_blocks_all_proxy_in_requests_merge(self):
        import requests

        merged = requests.Session().merge_environment_settings(
            "https://cftempmail.highkay.qzz.io/admin/new_address",
            dict(MAILBOX_PROXY_BYPASS_CONFIG),
            None,
            None,
            None,
        )

        self.assertEqual(
            merged["proxies"],
            {},
        )

    @patch("requests.post")
    def test_tempmail_direct_instantiation_bypasses_proxy(self, mock_post):
        mock_post.return_value.json.return_value = {
            "address": "demo@example.com",
            "token": "token-123",
        }

        mailbox = TempMailLolMailbox(proxy="http://proxy.local:8080")
        account = mailbox.get_email()

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.account_id, "token-123")
        self.assertEqual(mailbox.proxy, MAILBOX_PROXY_BYPASS_CONFIG)
        mock_post.assert_called_once_with(
            "https://api.tempmail.lol/v2/inbox/create",
            json={},
            proxies=MAILBOX_PROXY_BYPASS_CONFIG,
            timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
