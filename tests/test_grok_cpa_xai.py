import base64
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from platforms.grok.cpa_xai import (
    DEFAULT_BASE_URL,
    GROK_SESSION_COOKIES_EXTRA_KEY,
    REGISTRATION_RUNTIME_EXTRA_KEY,
    TOKEN_URL,
    XaiDeviceOAuthError,
    _account_extra,
    _advance_browser_authorization,
    _approve_device_consent,
    _click_labels,
    _inject_grok_session,
    _resolve_cpa_headless,
    _resolve_cpa_proxy,
    _resolve_grok_runtime_extra,
    build_registration_runtime,
    build_xai_auth_payload,
    select_grok_session_cookies,
    upload_xai_auth_payload,
)


def _jwt(claims: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class GrokCpaXaiTests(unittest.TestCase):
    def test_session_cookie_selection_preserves_xai_cookie_attributes(self):
        cookies = select_grok_session_cookies(
            [
                {
                    "name": "sso",
                    "value": "sso-value",
                    "domain": "accounts.x.ai",
                    "path": "/auth",
                    "expires": 1234,
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
                {"name": "unrelated", "value": "skip", "domain": "accounts.x.ai"},
                {"name": "sso", "value": "skip", "domain": "example.com"},
            ]
        )

        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["domain"], "accounts.x.ai")
        self.assertEqual(cookies[0]["path"], "/auth")
        self.assertEqual(cookies[0]["sameSite"], "Lax")

    def test_session_injection_restores_original_and_cross_domain_sso_cookies(self):
        context = mock.Mock()
        account = type(
            "Account",
            (),
            {
                "token": "",
                "extra": {
                    GROK_SESSION_COOKIES_EXTRA_KEY: [
                        {
                            "name": "sso",
                            "value": "sso-value",
                            "domain": "accounts.x.ai",
                            "path": "/",
                            "secure": True,
                            "httpOnly": True,
                        },
                        {
                            "name": "cf_clearance",
                            "value": "cf-value",
                            "domain": "accounts.x.ai",
                            "path": "/",
                            "secure": True,
                        },
                    ]
                },
            },
        )()

        _inject_grok_session(context, account)

        injected = context.add_cookies.call_args.args[0]
        self.assertIn(("sso", "accounts.x.ai"), {(item["name"], item["domain"]) for item in injected})
        self.assertIn(("sso", ".x.ai"), {(item["name"], item["domain"]) for item in injected})
        self.assertIn(("sso", "auth.x.ai"), {(item["name"], item["domain"]) for item in injected})
        self.assertIn(("cf_clearance", "accounts.x.ai"), {(item["name"], item["domain"]) for item in injected})
        self.assertNotIn(("cf_clearance", ".x.ai"), {(item["name"], item["domain"]) for item in injected})

    def test_legacy_sso_is_injected_at_parent_xai_domain(self):
        context = mock.Mock()
        account = type("Account", (), {"token": "", "extra": {"sso": "sso-value", "sso_rw": "rw-value"}})()

        _inject_grok_session(context, account)

        injected = context.add_cookies.call_args.args[0]
        self.assertEqual(
            {(item["name"], item["domain"]) for item in injected},
            {("sso", ".x.ai"), ("sso-rw", ".x.ai")},
        )

    def test_account_extra_reads_persisted_account_model_metadata(self):
        account = type(
            "AccountModel",
            (),
            {"extra_json": json.dumps({"registration_proxy": "socks5h://proxy:1080"})},
        )()

        self.assertEqual(
            _account_extra(account),
            {"registration_proxy": "socks5h://proxy:1080"},
        )

    def test_exact_label_click_does_not_delegate_to_fuzzy_helper(self):
        page = mock.Mock()
        button = mock.Mock()
        button.count.return_value = 1
        button.first.is_visible.return_value = True
        page.get_by_role.return_value = button
        helper = mock.Mock()

        clicked = _click_labels(helper, page, ["Allow"], allow_submit_fallback=False)

        self.assertTrue(clicked)
        page.get_by_role.assert_called_once_with("button", name="Allow", exact=True)
        button.first.click.assert_called_once_with(timeout=3000)
        helper._click_text_button.assert_not_called()

    def test_account_redirect_allows_one_follow_up_device_code_submit(self):
        page = mock.Mock()
        page.url = "https://accounts.x.ai/account"
        user_code = mock.Mock()

        def find_visible(_page, selectors):
            return user_code if "user_code" in " ".join(selectors) and "/device" in page.url else None

        with (
            mock.patch("platforms.grok.cpa_xai._visible_locator", side_effect=find_visible),
            mock.patch("platforms.grok.cpa_xai._page_text", side_effect=["正在重定向", "", ""]),
            mock.patch("platforms.grok.cpa_xai._click_labels", return_value=True) as click_mock,
        ):
            state: set[str] = set()
            account_step = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=state,
            )
            page.url = "https://accounts.x.ai/oauth2/device"
            device_step = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=state,
            )
            duplicate_device_step = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=state,
            )

        self.assertTrue(account_step)
        self.assertTrue(device_step)
        self.assertFalse(duplicate_device_step)
        self.assertEqual(click_mock.call_count, 2)
        self.assertIn("Continue", click_mock.call_args_list[0].args[2])
        user_code.fill.assert_called_once_with("ABCD-EFGH")

    def test_redirect_copy_does_not_turn_device_page_into_account_redirect(self):
        page = mock.Mock()
        page.url = "https://accounts.x.ai/oauth2/device"
        user_code = mock.Mock()

        with (
            mock.patch("platforms.grok.cpa_xai._visible_locator", return_value=user_code),
            mock.patch("platforms.grok.cpa_xai._page_text", return_value="Redirecting"),
            mock.patch("platforms.grok.cpa_xai._click_labels", return_value=True),
        ):
            acted = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=set(),
            )

        self.assertTrue(acted)
        user_code.fill.assert_called_once_with("ABCD-EFGH")

    def test_email_login_choice_is_handled_before_generic_login_inputs(self):
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-in"

        with (
            mock.patch("platforms.grok.cpa_xai._visible_locator", return_value=None),
            mock.patch("platforms.grok.cpa_xai._page_text", return_value="使用邮箱登录"),
            mock.patch("platforms.grok.cpa_xai._click_labels", return_value=True) as click_mock,
        ):
            acted = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=set(),
            )

        self.assertTrue(acted)
        self.assertIn("使用邮箱登录", click_mock.call_args.args[2])
        self.assertFalse(click_mock.call_args.kwargs["allow_submit_fallback"])

    def test_consent_sets_allow_action_before_submit(self):
        page = mock.Mock()
        page.url = "https://accounts.x.ai/oauth2/device/consent"
        page.evaluate.return_value = True

        with (
            mock.patch("platforms.grok.cpa_xai._page_text", return_value="授权 Grok Build"),
        ):
            acted = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=set(),
            )

        self.assertTrue(acted)
        script = page.evaluate.call_args.args[0]
        self.assertIn("action.value = 'allow'", script)
        self.assertIn("form.requestSubmit()", script)

    def test_consent_submit_returns_false_when_page_evaluation_fails(self):
        page = mock.Mock()
        page.evaluate.side_effect = RuntimeError("browser closed")

        self.assertFalse(_approve_device_consent(page))

    def test_invalid_consent_action_fails_without_waiting_for_timeout(self):
        page = mock.Mock()
        page.url = "https://auth.x.ai/oauth2/device/approve"

        with mock.patch("platforms.grok.cpa_xai._page_text", return_value="Invalid action"):
            with self.assertRaisesRegex(XaiDeviceOAuthError, "Invalid action"):
                _advance_browser_authorization(
                    mock.Mock(),
                    page,
                    mock.Mock(user_code="ABCD-EFGH"),
                    "user@example.com",
                    "secret",
                    submitted_states=set(),
                )

    def test_password_page_is_not_submitted_as_email_only_and_is_not_repeated(self):
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-in"
        email_input = mock.Mock()
        password_input = mock.Mock()

        def find_visible(_page, selectors):
            selector = " ".join(selectors)
            if "user_code" in selector:
                return None
            if "password" in selector:
                return password_input
            return email_input

        with (
            mock.patch("platforms.grok.cpa_xai._visible_locator", side_effect=find_visible),
            mock.patch("platforms.grok.cpa_xai._page_has_turnstile", return_value=False),
            mock.patch("platforms.grok.cpa_xai._click_labels", return_value=True) as click_mock,
        ):
            state: set[str] = set()
            first = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=state,
            )
            second = _advance_browser_authorization(
                mock.Mock(),
                page,
                mock.Mock(user_code="ABCD-EFGH"),
                "user@example.com",
                "secret",
                submitted_states=state,
            )

        self.assertTrue(first)
        self.assertFalse(second)
        email_input.fill.assert_called_once_with("user@example.com")
        password_input.fill.assert_called_once_with("secret")
        self.assertEqual(click_mock.call_count, 1)
        self.assertIn("Sign in", click_mock.call_args.args[2])

    def test_registration_runtime_keeps_only_browser_settings(self):
        runtime = build_registration_runtime(
            {
                "grok_flaresolverr_url": "http://solver:8191/v1",
                "grok_flaresolverr_attempts": "4",
                "yescaptcha_key": "must-not-be-persisted",
            },
            headless=False,
        )

        self.assertEqual(
            runtime,
            {
                "browser_headless": False,
                "grok_flaresolverr_url": "http://solver:8191/v1",
                "grok_flaresolverr_attempts": "4",
            },
        )

    def test_cpa_uses_registration_runtime_snapshot_before_current_config(self):
        account = type(
            "Account",
            (),
            {
                "extra": {
                    REGISTRATION_RUNTIME_EXTRA_KEY: {
                        "browser_headless": False,
                        "grok_flaresolverr_url": "http://signup-solver:8191/v1",
                    }
                }
            },
        )()
        with mock.patch("platforms.grok.cpa_xai.config_store.get", return_value="http://current-solver:8191/v1"):
            runtime, source = _resolve_grok_runtime_extra(account)

        self.assertEqual(source, "账号注册快照")
        self.assertEqual(runtime["grok_flaresolverr_url"], "http://signup-solver:8191/v1")

    def test_cpa_headless_falls_back_to_registration_runtime(self):
        account = type("Account", (), {})()
        with mock.patch("platforms.grok.cpa_xai.config_store.get", return_value=""):
            headless, source = _resolve_cpa_headless(account, {"browser_headless": True})

        self.assertTrue(headless)
        self.assertEqual(source, "账号注册")

    def test_cpa_proxy_falls_back_to_account_registration_proxy(self):
        account = type("Account", (), {"extra": {"registration_proxy": "socks5h://proxy:1080"}})()
        with mock.patch("platforms.grok.cpa_xai.config_store.get", return_value=""):
            proxy, source = _resolve_cpa_proxy(account)

        self.assertEqual(proxy, "socks5h://proxy:1080")
        self.assertEqual(source, "账号注册")

    def test_cpa_reads_runtime_and_proxy_from_persisted_account_model(self):
        saved_extra = {
            "registration_proxy": "socks5h://signup-proxy:1080",
            REGISTRATION_RUNTIME_EXTRA_KEY: {
                "browser_headless": False,
                "grok_flaresolverr_url": "http://signup-solver:8191/v1",
            },
        }
        account = type(
            "AccountModel",
            (),
            {"extra_json": json.dumps(saved_extra)},
        )()

        with mock.patch("platforms.grok.cpa_xai.config_store.get", return_value=""):
            runtime, runtime_source = _resolve_grok_runtime_extra(account)
            proxy, proxy_source = _resolve_cpa_proxy(account)

        self.assertFalse(runtime["browser_headless"])
        self.assertEqual(runtime["grok_flaresolverr_url"], "http://signup-solver:8191/v1")
        self.assertEqual(runtime_source, "账号注册快照")
        self.assertEqual(proxy, "socks5h://signup-proxy:1080")
        self.assertEqual(proxy_source, "账号注册")

    def test_explicit_cpa_proxy_takes_precedence(self):
        account = type("Account", (), {"extra": {"registration_proxy": "socks5h://signup:1080"}})()
        with mock.patch("platforms.grok.cpa_xai.config_store.get", return_value="socks5h://cpa:1080"):
            proxy, source = _resolve_cpa_proxy(account)

        self.assertEqual(proxy, "socks5h://cpa:1080")
        self.assertEqual(source, "CPA 配置")

    def test_payload_matches_cliproxy_xai_auth_schema(self):
        now = int(datetime.now(timezone.utc).timestamp())
        payload = build_xai_auth_payload(
            email="user@example.com",
            access_token=_jwt({"sub": "xai-user", "iat": now, "exp": now + 3600}),
            refresh_token="refresh-token",
            id_token="id-token",
        )

        self.assertEqual(payload["type"], "xai")
        self.assertEqual(payload["auth_kind"], "oauth")
        self.assertEqual(payload["sub"], "xai-user")
        self.assertEqual(payload["expires_in"], 3600)
        self.assertEqual(payload["base_url"], DEFAULT_BASE_URL)
        self.assertEqual(payload["token_endpoint"], TOKEN_URL)
        self.assertFalse(payload["disabled"])
        self.assertEqual(payload["headers"]["x-xai-token-auth"], "xai-grok-cli")

    def test_upload_posts_multipart_to_management_auth_files(self):
        response = mock.Mock(ok=True, status_code=200)
        payload = {"type": "xai", "email": "user@example.com", "sub": "xai-user"}

        with mock.patch("platforms.grok.cpa_xai.requests.post", return_value=response) as post_mock:
            filename, status_code = upload_xai_auth_payload(
                payload,
                management_url="http://cpa.test:18317/v0/management",
                management_token="management-token",
            )

        self.assertEqual(filename, "xai-user@example.com.json")
        self.assertEqual(status_code, 200)
        self.assertEqual(
            post_mock.call_args.args[0],
            "http://cpa.test:18317/v0/management/auth-files",
        )
        self.assertEqual(
            post_mock.call_args.kwargs["headers"]["Authorization"],
            "Bearer management-token",
        )
        uploaded = post_mock.call_args.kwargs["files"]["file"]
        self.assertEqual(uploaded[0], filename)
        self.assertEqual(uploaded[2], "application/json")
        self.assertEqual(json.loads(uploaded[1].decode())["type"], "xai")


if __name__ == "__main__":
    unittest.main()
