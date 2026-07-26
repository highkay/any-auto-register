import unittest
from unittest import mock

from platforms.grok.protocol_client import SignupConfig
from platforms.grok.protocol_register import GrokProtocolRegister


class GrokProtocolRegisterTests(unittest.TestCase):
    def test_register_uses_native_ui_complete_path(self):
        reg = GrokProtocolRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            extra={
                "grok_clearance_mode": "never",
                # Force SPA path for this test (protocol_http is preferred by default).
                "grok_register_strategy": "native_ui",
            },
        )
        client = mock.Mock()
        cfg = SignupConfig(
            site_key="0x4AAAAAAAhr9JGVDZbrZOo0",
            action_id="action123",
            state_tree="%5B%22%22%5D",
            source="test",
        )
        client.user_agent = "UA"

        with mock.patch.object(reg, "_prepare_client", return_value=(client, cfg)):
            with mock.patch.object(
                reg,
                "_native_ui_register",
                return_value={
                    "email": "user@example.com",
                    "password": "Pass123,,,aA1",
                    "given_name": "Ada",
                    "family_name": "Lovelace",
                    "sso": "sso-token-value-xxxxx",
                    "sso_rw": "sso-rw",
                    "cookies": [{"name": "sso", "value": "sso-token-value-xxxxx"}],
                    "register_mode": "protocol+native_ui",
                },
            ) as native_mock:
                with mock.patch.object(reg, "_protocol_http_register") as http_mock:
                    result = reg.register(
                        email="user@example.com",
                        password="Pass123,,,aA1",
                        otp_callback=lambda: "ABC-DEF",
                    )

        native_mock.assert_called_once()
        http_mock.assert_not_called()
        self.assertEqual(result["email"], "user@example.com")
        self.assertEqual(result["sso"], "sso-token-value-xxxxx")
        self.assertEqual(result["register_mode"], "protocol+native_ui")
        client.close.assert_called_once()
        client.create_email_code.assert_not_called()
        client.signup_server_action.assert_not_called()

    def test_register_prefers_protocol_http_when_available(self):
        reg = GrokProtocolRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            extra={"grok_clearance_mode": "never", "grok_register_strategy": "auto"},
        )
        client = mock.Mock()
        cfg = SignupConfig(
            site_key="0x4AAAAAAAhr9JGVDZbrZOo0",
            action_id="action123",
            state_tree="%5B%22%22%5D",
            source="test",
        )
        with mock.patch.object(reg, "_prepare_client", return_value=(client, cfg)):
            with mock.patch.object(
                reg,
                "_protocol_http_register",
                return_value={
                    "email": "user@example.com",
                    "password": "Pass123,,,aA1",
                    "given_name": "Ada",
                    "family_name": "Lovelace",
                    "sso": "sso-http",
                    "sso_rw": "",
                    "cookies": [],
                    "register_mode": "protocol_http",
                },
            ) as http_mock:
                with mock.patch.object(reg, "_native_ui_register") as native_mock:
                    result = reg.register(
                        email="user@example.com",
                        password="Pass123,,,aA1",
                        otp_callback=lambda: "ABC-DEF",
                    )
        http_mock.assert_called_once()
        native_mock.assert_not_called()
        self.assertEqual(result["register_mode"], "protocol_http")
        self.assertEqual(result["sso"], "sso-http")

    def test_clearance_auto_skips_unreachable_flaresolverr(self):
        reg = GrokProtocolRegister(
            log_fn=lambda *_: None,
            extra={
                "grok_clearance_mode": "auto",
                "grok_flaresolverr_url": "http://127.0.0.1:9/v1",
            },
        )
        with mock.patch.object(reg, "_flaresolverr_reachable", return_value=False):
            self.assertFalse(reg._clearance_enabled())

    def test_normalize_flaresolverr_proxy_rewrites_loopback_for_docker(self):
        reg = GrokProtocolRegister(
            log_fn=lambda *_: None,
            extra={"grok_flaresolverr_loopback_proxy_host": "host.docker.internal"},
        )
        self.assertEqual(
            reg._normalize_flaresolverr_proxy_url("http://127.0.0.1:7890"),
            "http://host.docker.internal:7890",
        )
        self.assertEqual(
            reg._normalize_flaresolverr_proxy_url("http://user:pass@localhost:7890"),
            "http://user:pass@host.docker.internal:7890",
        )
        self.assertEqual(
            reg._normalize_flaresolverr_proxy_url("http://192.168.1.18:2260"),
            "http://192.168.1.18:2260",
        )


if __name__ == "__main__":
    unittest.main()
