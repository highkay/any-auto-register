import unittest
from itertools import chain, repeat
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import RegisterConfig
from platforms.cerebras.core import CerebrasRegister, _default_full_name
from platforms.cerebras.plugin import (
    CerebrasPlatform,
    _extract_cerebras_magic_link,
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
                "subject": "Sign in to Cerebras Link",
                "raw": (
                    "Click the button below:\n"
                    "https://cloud.cerebras.ai/auth/magic-link?callbackUrl=3Dhttps%3A%2F%2Fcloud=\r\n"
                    ".cerebras.ai%2F&amp;token=3Ddemo-token&amp;email=3Duser%40example.com"
                ),
            }
        ]


class DummyRotatingMailbox(DummyMailbox):
    def __init__(self, emails: list[str]):
        self._accounts = [
            MailboxAccount(email=email, account_id=f"token-{idx}")
            for idx, email in enumerate(emails, start=1)
        ]
        self._index = 0

    def get_email(self):
        account = self._accounts[min(self._index, len(self._accounts) - 1)]
        self._index += 1
        return account


class DummyLocator:
    def __init__(self):
        self.filled: list[str] = []
        self.clicked = 0
        self.waited = 0

    def wait_for(self, **kwargs):
        self.waited += 1

    def fill(self, value: str):
        self.filled.append(value)

    def click(self, **kwargs):
        self.clicked += 1

    def is_enabled(self):
        return True

    @property
    def first(self):
        return self


class DummyOnboardingPage:
    def __init__(self):
        self.url = "https://cloud.cerebras.ai/platform/org_demo/onboarding"
        self.full_name_locator = DummyLocator()
        self.student_button = DummyLocator()
        self.continue_button = DummyLocator()
        self.wait_calls: list[int] = []

    def locator(self, selector: str):
        if selector == 'input[name="fullName"]':
            return self.full_name_locator
        raise AssertionError(selector)

    def get_by_role(self, role: str, name: str):
        if role != "button":
            raise AssertionError(role)
        mapping = {
            "Student": self.student_button,
            "Continue": self.continue_button,
        }
        return mapping[name]

    def wait_for_timeout(self, wait_ms: int):
        self.wait_calls.append(wait_ms)


class DummySubmitEmailPage:
    def __init__(self):
        self.url = "https://cloud.cerebras.ai/?useRecaptchaV2=true"
        self.email_locator = DummyLocator()
        self.continue_button = DummyLocator()
        self.wait_calls: list[int] = []

    def locator(self, selector: str):
        if selector == 'input[type="email"]':
            return self.email_locator
        raise AssertionError(selector)

    def get_by_role(self, role: str, name: str):
        if role != "button" or name != "CONTINUE WITH EMAIL":
            raise AssertionError((role, name))
        return self.continue_button

    def wait_for_timeout(self, wait_ms: int):
        self.wait_calls.append(wait_ms)


class DummyRecaptchaPage:
    def __init__(self):
        self.url = "https://cloud.cerebras.ai/?useRecaptchaV2=true"
        self.wait_calls: list[int] = []

    def wait_for_timeout(self, wait_ms: int):
        self.wait_calls.append(wait_ms)


class DummyButtonCollection:
    def __init__(self, buttons: list[DummyLocator]):
        self._buttons = buttons

    def count(self):
        return len(self._buttons)

    def nth(self, index: int):
        return self._buttons[index]


class DummyPlanPage:
    def __init__(self):
        self.url = "https://cloud.cerebras.ai/platform/org_demo/onboarding"
        self.buttons = [DummyLocator(), DummyLocator()]

    def get_by_role(self, role: str, name: str):
        if role != "button" or name != "Get Started":
            raise AssertionError((role, name))
        return DummyButtonCollection(self.buttons)

    def wait_for_timeout(self, wait_ms: int):
        pass


class CerebrasPluginTests(unittest.TestCase):
    def test_extract_cerebras_magic_link_decodes_quoted_printable_href(self):
        raw = (
            "https://cloud.cerebras.ai/auth/magic-link?callbackUrl=3Dhttps%3A%2F%2Fcloud=\r\n"
            ".cerebras.ai%2F&amp;token=3Ddemo-token&amp;email=3Duser%40example.com"
        )
        self.assertEqual(
            _extract_cerebras_magic_link(raw),
            "https://cloud.cerebras.ai/auth/magic-link?callbackUrl=https%3A%2F%2Fcloud.cerebras.ai%2F&token=demo-token&email=user%40example.com",
        )

    def test_default_full_name_guarantees_two_words_for_single_token_local_part(self):
        self.assertEqual(_default_full_name("tmpyukupt3040@example.com"), "Tmpyukupt User")

    def test_register_uses_mailbox_magic_link_and_returns_api_key_account(self):
        mailbox = DummyMailbox("user@example.com")
        platform = CerebrasPlatform(
            config=RegisterConfig(
                executor_type="headless",
                extra={"cerebras_use_case": "startup"},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        def register_side_effect(**kwargs):
            link = kwargs["verification_link_callback"]()
            self.assertIn("cloud.cerebras.ai/auth/magic-link", link)
            self.assertEqual(kwargs["email"], "user@example.com")
            self.assertEqual(kwargs["use_case"], "startup")
            return {
                "email": "user@example.com",
                "password": "MagicLink!Aa1",
                "api_key": "csk-demo-key",
                "base_url": "https://api.cerebras.ai",
                "organization_id": "org_demo",
                "organization_name": "Personal",
                "project_id": "prj_demo",
                "project_name": "Default Project",
                "api_key_id": "key_demo",
                "api_key_name": "Default Key",
            }

        with mock.patch("platforms.cerebras.core.CerebrasRegister") as register_cls:
            register_cls.return_value.register.side_effect = register_side_effect
            account = platform.register(email="", password="MagicLink!Aa1")

        self.assertEqual(account.platform, "cerebras")
        self.assertEqual(account.email, "user@example.com")
        self.assertEqual(account.token, "csk-demo-key")
        self.assertEqual(account.extra["api_key"], "csk-demo-key")
        self.assertEqual(account.extra["organization_id"], "org_demo")
        self.assertEqual(account.extra["project_id"], "prj_demo")

    def test_register_retries_with_new_mailbox_when_context_is_empty(self):
        mailbox = DummyRotatingMailbox(["first@example.com", "second@example.com"])
        platform = CerebrasPlatform(
            config=RegisterConfig(
                executor_type="headless",
                extra={"cerebras_mailbox_attempts": 2},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        responses = [
            RuntimeError("Cerebras 当前账号没有可用组织: {}"),
            {
                "email": "second@example.com",
                "password": "MagicLink!Aa1",
                "api_key": "csk-demo-key-2",
                "base_url": "https://api.cerebras.ai",
                "organization_id": "org_demo_2",
                "organization_name": "Personal",
                "project_id": "prj_demo_2",
                "project_name": "Default Project",
                "api_key_id": "key_demo_2",
                "api_key_name": "Default Key",
            },
        ]

        def register_side_effect(**kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch("platforms.cerebras.core.CerebrasRegister") as register_cls:
            register_cls.return_value.register.side_effect = register_side_effect
            account = platform.register(email="", password="MagicLink!Aa1")

        self.assertEqual(account.email, "second@example.com")
        self.assertEqual(account.extra["organization_id"], "org_demo_2")
        self.assertEqual(register_cls.return_value.register.call_count, 2)

    @mock.patch("requests.get")
    def test_check_valid_uses_models_endpoint(self, mock_get):
        mock_get.return_value.status_code = 200
        account = type(
            "A",
            (),
            {
                "token": "",
                "extra": {
                    "api_key": "csk-demo-key",
                    "base_url": "https://api.cerebras.ai",
                },
            },
        )()

        platform = CerebrasPlatform(config=RegisterConfig())
        self.assertTrue(platform.check_valid(account))
        mock_get.assert_called_once_with(
            "https://api.cerebras.ai/v1/models",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer csk-demo-key",
            },
            timeout=15,
        )

    def test_detect_post_signin_state_distinguishes_loading_and_onboarding(self):
        page = type(
            "P",
            (),
            {"url": "https://cloud.cerebras.ai/platform/org_demo/onboarding"},
        )()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)
        with mock.patch("platforms.cerebras.core._safe_body_text", return_value="Loading..."):
            self.assertEqual(reg._detect_post_signin_state(page), "loading")
        with mock.patch(
            "platforms.cerebras.core._safe_body_text",
            return_value="Enter Details Full Name Continue Submitting...",
        ):
            self.assertEqual(reg._detect_post_signin_state(page), "loading")
        with mock.patch(
            "platforms.cerebras.core._safe_body_text",
            return_value="Enter Details Full Name Continue",
        ):
            self.assertEqual(reg._detect_post_signin_state(page), "onboarding")

    def test_fill_onboarding_waits_for_ready_state_before_filling(self):
        page = DummyOnboardingPage()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)

        with mock.patch.object(
            reg,
            "_wait_for_post_signin_state",
            return_value="onboarding",
        ):
            with mock.patch.object(
                reg,
                "_wait_for_allowed_post_signin_state",
                return_value="plan",
            ):
                reg._fill_onboarding(
                    page,
                    email="user.name@example.com",
                    full_name="",
                    use_case="student",
                )

        self.assertEqual(page.full_name_locator.filled, ["User Name"])
        self.assertEqual(page.student_button.clicked, 1)
        self.assertEqual(page.continue_button.clicked, 1)

    def test_wait_for_allowed_post_signin_state_retries_until_state_advances(self):
        page = DummyOnboardingPage()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)
        states = iter(["onboarding", "onboarding", "plan"])
        retry_calls: list[str] = []

        with mock.patch(
            "platforms.cerebras.core.time.time",
            side_effect=[0.0, 0.0, 0.0, 0.0, 6.0, 6.0, 7.0, 7.0],
        ):
            with mock.patch.object(
                reg,
                "_detect_post_signin_state",
                side_effect=lambda _page: next(states),
            ):
                state = reg._wait_for_allowed_post_signin_state(
                    page,
                    allowed_states={"plan"},
                    retryable_states={"onboarding"},
                    timeout=20,
                    retry_action=lambda: retry_calls.append("retry"),
                    retry_interval=5,
                )

        self.assertEqual(state, "plan")
        self.assertEqual(retry_calls, ["retry"])

    def test_regression_task_log_2565_continue_retries_until_plan(self):
        page = DummyOnboardingPage()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)
        state_history = iter(["onboarding", "onboarding", "plan"])

        with mock.patch.object(
            reg,
            "_wait_for_post_signin_state",
            return_value="onboarding",
        ):
            with mock.patch(
                "platforms.cerebras.core.time.time",
                side_effect=chain([0.0, 0.0, 0.0, 0.0, 6.0, 6.0, 7.0], repeat(7.0)),
            ):
                with mock.patch.object(
                    reg,
                    "_detect_post_signin_state",
                    side_effect=lambda _page: next(state_history),
                ):
                    reg._fill_onboarding(
                        page,
                        email="tmplccyyf4447@20210513.xyz",
                        full_name="",
                        use_case="student",
                    )

        self.assertEqual(page.continue_button.clicked, 2)

    def test_click_plan_get_started_does_not_treat_onboarding_as_success(self):
        page = DummyPlanPage()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)

        with mock.patch.object(
            reg,
            "_wait_for_allowed_post_signin_state",
            side_effect=["onboarding", "get_started"],
        ):
            reg._click_plan_get_started(page)

        self.assertEqual(page.buttons[0].clicked, 1)
        self.assertEqual(page.buttons[1].clicked, 1)

    def test_regression_task_log_2567_get_started_retries_after_onboarding_bounce(self):
        page = DummyPlanPage()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)

        with mock.patch.object(
            reg,
            "_wait_for_allowed_post_signin_state",
            side_effect=["onboarding", "get_started"],
        ):
            reg._click_plan_get_started(page)

        self.assertEqual(page.buttons[0].clicked, 1)
        self.assertEqual(page.buttons[1].clicked, 1)

    def test_submit_email_solves_recaptcha_v2_before_waiting_for_check_email(self):
        page = DummySubmitEmailPage()
        reg = CerebrasRegister(log_fn=lambda *_args, **_kwargs: None)
        body_texts = iter(
            [
                "",
                "",
                "Check your email user@example.com",
            ]
        )
        recaptcha_states = iter([True, False])

        with mock.patch(
            "platforms.cerebras.core._safe_body_text",
            side_effect=lambda *_args, **_kwargs: next(body_texts),
        ):
            with mock.patch.object(
                reg,
                "_has_recaptcha_v2_challenge",
                side_effect=lambda _page: next(recaptcha_states, False),
            ):
                with mock.patch.object(reg, "_solve_recaptcha_v2") as solve_mock:
                    reg._submit_email(page, "user@example.com")

        self.assertEqual(solve_mock.call_count, 1)
        self.assertEqual(page.email_locator.filled, ["user@example.com", "user@example.com"])
        self.assertEqual(page.continue_button.clicked, 2)

    def test_solve_recaptcha_v2_prefers_visible_sitekey_and_injects_token(self):
        solver = mock.Mock()
        solver.solve_recaptcha_v2.return_value = "recaptcha-token-123"
        page = DummyRecaptchaPage()
        reg = CerebrasRegister(
            captcha_solver=solver,
            log_fn=lambda *_args, **_kwargs: None,
        )

        with mock.patch.object(
            reg,
            "_extract_recaptcha_v2_sitekeys",
            return_value={
                "visible": "visible-site-key",
                "invisible": "invisible-site-key",
                "enterprise": "enterprise-site-key",
            },
        ):
            with mock.patch.object(reg, "_inject_recaptcha_token", return_value=True) as inject_mock:
                token = reg._solve_recaptcha_v2(page)

        self.assertEqual(token, "recaptcha-token-123")
        solver.solve_recaptcha_v2.assert_called_once()
        call_args = solver.solve_recaptcha_v2.call_args
        self.assertEqual(call_args.args[:2], (page.url, "visible-site-key"))
        self.assertFalse(call_args.kwargs["enterprise"])
        self.assertFalse(call_args.kwargs["is_invisible"])
        inject_mock.assert_called_once_with(page, "recaptcha-token-123")

    def test_solve_recaptcha_v2_waits_for_late_sitekey(self):
        solver = mock.Mock()
        solver.solve_recaptcha_v2.return_value = "recaptcha-token-late"
        page = DummyRecaptchaPage()
        reg = CerebrasRegister(
            captcha_solver=solver,
            log_fn=lambda *_args, **_kwargs: None,
        )

        sitekey_states = iter(
            [
                {"visible": "", "invisible": "", "enterprise": ""},
                {"visible": "", "invisible": "late-site-key", "enterprise": ""},
            ]
        )

        with mock.patch.object(
            reg,
            "_extract_recaptcha_v2_sitekeys",
            side_effect=lambda _page: next(sitekey_states),
        ):
            with mock.patch.object(reg, "_inject_recaptcha_token", return_value=True):
                token = reg._solve_recaptcha_v2(page)

        self.assertEqual(token, "recaptcha-token-late")
        call_args = solver.solve_recaptcha_v2.call_args
        self.assertEqual(call_args.args[:2], (page.url, "late-site-key"))
        self.assertFalse(call_args.kwargs["enterprise"])
        self.assertTrue(call_args.kwargs["is_invisible"])


if __name__ == "__main__":
    unittest.main()
