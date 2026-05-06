import unittest
from unittest import mock

from core.base_platform import RegisterConfig
from platforms.nvidia.core import (
    NvidiaRegister,
    _HCAPTCHA_VISUAL_RECOGNITION_POLL_INTERVAL_SECONDS,
    _HCAPTCHA_VISUAL_RECOGNITION_REQUEST_TIMEOUT_SECONDS,
    _HCAPTCHA_VISUAL_RECOGNITION_TIMEOUT_SECONDS,
    _is_nvidia_account_system_url,
    _parse_json_like_payload,
)
from platforms.nvidia.plugin import NvidiaPlatform, _extract_nvidia_verify_link


class DummyMailboxAccount:
    def __init__(self, email: str):
        self.email = email
        self.account_id = email


class DummyMailbox:
    def __init__(self, emails: list[str]):
        self._emails = list(emails)

    def get_email(self):
        return DummyMailboxAccount(self._emails.pop(0))

    def get_current_ids(self, account):
        return set()

    def wait_for_code(self, *args, **kwargs):
        return "123456"


def _visual_solver_kwargs():
    return {
        "timeout_seconds": _HCAPTCHA_VISUAL_RECOGNITION_TIMEOUT_SECONDS,
        "poll_interval_seconds": _HCAPTCHA_VISUAL_RECOGNITION_POLL_INTERVAL_SECONDS,
        "request_timeout_seconds": _HCAPTCHA_VISUAL_RECOGNITION_REQUEST_TIMEOUT_SECONDS,
        "interrupt_checker": mock.ANY,
    }


class NvidiaPluginTests(unittest.TestCase):
    def test_parse_json_like_payload_accepts_fenced_json(self):
        payload = """```json
{"action":"click","clicks":[{"x":10,"y":20}]}
```"""
        parsed = _parse_json_like_payload(payload, label="hCaptcha 视觉识别")
        self.assertEqual(parsed["action"], "click")
        self.assertEqual(parsed["clicks"][0]["x"], 10)

    def test_account_system_url_helper_accepts_com_create_account(self):
        self.assertTrue(
            _is_nvidia_account_system_url(
                "https://login.nvgs.nvidia.com/v1/create-account?email=user@example.com"
            )
        )

    def test_extract_nvidia_verify_link_decodes_quoted_printable_href(self):
        raw = (
            "Having trouble with the code? Use <a href='https://login.nvgs.nvidia.cn/profile-management/"
            "verify-email?code=3DeyJhbGciOiJIUzI1NiJ9.abc123&amp;locale=3Dzh-CN&amp;theme=3DNoir'>"
            "this link</a> instead."
        )
        self.assertEqual(
            _extract_nvidia_verify_link(raw),
            "https://login.nvgs.nvidia.cn/profile-management/verify-email?code=eyJhbGciOiJIUzI1NiJ9.abc123&locale=zh-CN&theme=Noir",
        )

    def test_register_retries_new_mailbox_when_first_email_lands_on_login_branch(self):
        mailbox = DummyMailbox(["first@example.com", "second@example.com"])
        platform = NvidiaPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={"nvidia_mailbox_attempts": 2},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        register_mock = mock.Mock()
        register_mock.side_effect = [
            RuntimeError("未进入 NVIDIA Create Account 页面，url=https://login.nvgs.nvidia.cn/v1/login?foo=bar"),
            {
                "email": "second@example.com",
                "password": "Nv!passAa1",
                "api_key": "nv-key",
                "base_url": "https://integrate.api.nvidia.com",
            },
        ]

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.nvidia.core.NvidiaRegister") as register_cls:
                register_cls.return_value.register = register_mock
                account = platform.register(email="", password="Nv!passAa1")

        self.assertEqual(account.email, "second@example.com")
        self.assertEqual(register_mock.call_count, 2)
        first_call = register_mock.call_args_list[0]
        second_call = register_mock.call_args_list[1]
        self.assertEqual(first_call.kwargs["email"], "first@example.com")
        self.assertEqual(second_call.kwargs["email"], "second@example.com")

    def test_register_defaults_to_extended_mailbox_attempt_budget(self):
        mailbox = DummyMailbox([f"user{i}@example.com" for i in range(12)])
        platform = NvidiaPlatform(
            config=RegisterConfig(
                executor_type="headless",
                captcha_solver="manual",
                extra={},
            ),
            mailbox=mailbox,
        )
        platform._log_fn = lambda *args, **kwargs: None

        register_mock = mock.Mock()
        register_mock.side_effect = [
            RuntimeError("未进入 NVIDIA Create Account 页面，url=https://login.nvgs.nvidia.cn/v1/login?foo=bar")
        ] * 11 + [
            {
                "email": "user11@example.com",
                "password": "Nv!passAa1",
                "api_key": "nv-key",
                "base_url": "https://integrate.api.nvidia.com",
            }
        ]

        with mock.patch.object(platform, "_make_captcha", return_value=mock.Mock()):
            with mock.patch("platforms.nvidia.core.NvidiaRegister") as register_cls:
                register_cls.return_value.register = register_mock
                account = platform.register(email="", password="Nv!passAa1")

        self.assertEqual(account.email, "user11@example.com")
        self.assertEqual(register_mock.call_count, 12)

    def test_register_passes_task_control_to_nvidia_register(self):
        mailbox = DummyMailbox(["user@example.com"])
        platform = NvidiaPlatform(
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
            with mock.patch("platforms.nvidia.core.NvidiaRegister") as register_cls:
                register_cls.return_value.register.return_value = {
                    "email": "user@example.com",
                    "password": "Nv!passAa1",
                    "api_key": "nv-key",
                    "base_url": "https://integrate.api.nvidia.com",
                }
                platform.register(email="", password="Nv!passAa1")

        self.assertIs(
            register_cls.call_args.kwargs["task_control"],
            platform._task_control,
        )

    def test_solve_hcaptcha_reports_solver_capability_error(self):
        solver = mock.Mock()
        solver.solve_hcaptcha.side_effect = NotImplementedError("unsupported")
        solver.classify_hcaptcha.side_effect = NotImplementedError("unsupported")
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/v1/create-account"

        with mock.patch.object(reg, "_extract_hcaptcha_sitekey", return_value="site-key-123"):
            with mock.patch.object(reg, "_solve_hcaptcha_challenge", side_effect=NotImplementedError("unsupported")):
                with self.assertRaisesRegex(RuntimeError, "暂不支持 NVIDIA hCaptcha"):
                    reg._solve_hcaptcha(page)

    def test_solve_hcaptcha_direct_fallback_uses_bounded_yescaptcha_controls(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.side_effect = RuntimeError("challenge fail")
        solver.solve_hcaptcha.return_value = "token-123"
        task_control = mock.Mock()
        reg = NvidiaRegister(
            captcha_solver=solver,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            task_control=task_control,
        )
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/v1/create-account"

        with mock.patch.object(reg, "_extract_hcaptcha_sitekey", return_value="site-key-123"):
            with mock.patch.object(reg, "_solve_hcaptcha_challenge", side_effect=RuntimeError("challenge fail")):
                with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                    reg._solve_hcaptcha(page)

        solver.solve_hcaptcha.assert_called_once()
        self.assertEqual(
            solver.solve_hcaptcha.call_args.kwargs["timeout_seconds"],
            45.0,
        )
        self.assertEqual(
            solver.solve_hcaptcha.call_args.kwargs["poll_interval_seconds"],
            2.0,
        )
        self.assertEqual(
            solver.solve_hcaptcha.call_args.kwargs["request_timeout_seconds"],
            10.0,
        )
        interrupt_checker = solver.solve_hcaptcha.call_args.kwargs["interrupt_checker"]
        self.assertIs(interrupt_checker.__self__, reg)
        self.assertIs(interrupt_checker.__func__, reg._checkpoint.__func__)

    def test_wait_until_calls_task_checkpoint(self):
        task_control = mock.Mock()
        reg = NvidiaRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            task_control=task_control,
        )
        page = mock.Mock()
        states = iter([False, True])

        reg._wait_until(lambda: next(states), timeout=1.0, interval=0.25, page=page)

        self.assertGreaterEqual(task_control.checkpoint.call_count, 1)

    def test_submit_email_retries_when_page_stays_on_build_entry(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://build.nvidia.com/?modal=signin"
        email_input = mock.Mock()
        page.locator.return_value.first = email_input
        page.get_by_role.return_value.last.click.side_effect = [Exception("not found"), None]

        with mock.patch.object(reg, "_click_text_button", side_effect=[True, True]) as click_mock:
            with mock.patch.object(
                reg,
                "_wait_until",
                side_effect=[TimeoutError("still on build"), None],
            ) as wait_mock:
                reg._submit_email(page, "user@example.com")

        self.assertEqual(email_input.fill.call_count, 2)
        self.assertEqual(click_mock.call_count, 1)
        self.assertEqual(wait_mock.call_count, 2)
        self.assertIs(wait_mock.call_args_list[0].kwargs["page"], page)
        self.assertIs(wait_mock.call_args_list[1].kwargs["page"], page)

    def test_wait_until_uses_page_wait_for_timeout_when_page_is_provided(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        states = iter([False, False, True])

        reg._wait_until(lambda: next(states), timeout=1.0, interval=0.25, page=page)

        self.assertEqual(page.wait_for_timeout.call_count, 2)
        page.wait_for_timeout.assert_called_with(250)

    def test_click_login_entry_prefers_playwright_role_click(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        selector_locator = mock.Mock()
        selector_locator.first.click.side_effect = Exception("no selector click")
        page.locator.return_value = selector_locator
        login_locator = mock.Mock()
        login_locator.first.click.return_value = None
        page.get_by_role.return_value = login_locator

        clicked = reg._click_login_entry(page)

        self.assertTrue(clicked)
        login_locator.first.click.assert_called_once()

    def test_click_login_entry_uses_specific_login_button_selector_first(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        selector_locator = mock.Mock()
        selector_locator.first.click.return_value = None
        page.locator.return_value = selector_locator

        clicked = reg._click_login_entry(page)

        self.assertTrue(clicked)
        selector_locator.first.click.assert_called_once()

    def test_submit_email_returns_when_redirect_happens_before_retry_fill(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://build.nvidia.com/?modal=signin"
        email_input = mock.Mock()
        page.locator.return_value.first = email_input
        page.get_by_role.return_value.last.click.side_effect = [Exception("not found")]

        wait_results = [TimeoutError("still on build")]

        def wait_until_side_effect(*args, **kwargs):
            result = wait_results.pop(0)
            if isinstance(result, Exception):
                page.url = "https://login.nvgs.nvidia.cn/v1/create-account?email=user@example.com"
                raise result
            return result

        with mock.patch.object(reg, "_click_text_button", return_value=True):
            with mock.patch.object(reg, "_wait_until", side_effect=wait_until_side_effect):
                reg._submit_email(page, "user@example.com")

        self.assertEqual(email_input.fill.call_count, 1)

    def test_submit_email_returns_when_redirect_happens_to_com_create_account(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://build.nvidia.com/?modal=signin"
        email_input = mock.Mock()
        page.locator.return_value.first = email_input
        page.get_by_role.return_value.last.click.side_effect = [Exception("not found")]

        wait_results = [TimeoutError("still on build")]

        def wait_until_side_effect(*args, **kwargs):
            result = wait_results.pop(0)
            if isinstance(result, Exception):
                page.url = "https://login.nvgs.nvidia.com/v1/create-account?email=user@example.com"
                raise result
            return result

        with mock.patch.object(reg, "_click_text_button", return_value=True):
            with mock.patch.object(reg, "_wait_until", side_effect=wait_until_side_effect):
                reg._submit_email(page, "user@example.com")

        self.assertEqual(email_input.fill.call_count, 1)

    def test_wait_for_create_account_passes_page_to_wait_until(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/v1/create-account"
        page.locator.return_value.count.return_value = 1

        with mock.patch.object(reg, "_wait_until", return_value=None) as wait_mock:
            reg._wait_for_create_account(page)

        self.assertIs(wait_mock.call_args.kwargs["page"], page)

    def test_submit_create_account_falls_back_to_text_button_labels(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.get_by_role.return_value.click.side_effect = Exception("not found")

        with mock.patch.object(reg, "_click_text_button", return_value=True) as click_mock:
            reg._submit_create_account(page)

        click_mock.assert_called_once_with(page, ["create account", "创建账户"])
        page.wait_for_timeout.assert_any_call(1200)
        page.wait_for_timeout.assert_any_call(2500)

    def test_extract_hcaptcha_sitekey_supports_hash_fragment(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.evaluate.return_value = "hash-sitekey-123"

        sitekey = reg._extract_hcaptcha_sitekey(page)

        self.assertEqual(sitekey, "hash-sitekey-123")
        script = page.evaluate.call_args.args[0]
        self.assertIn("parsed.hash", script)
        self.assertIn("URLSearchParams(hashText)", script)

    def test_hcaptcha_retry_shell_detection(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        self.assertTrue(
            reg._is_hcaptcha_retry_shell(
                {
                    "body": "请再试一次 ⚠️ 检查 ZH",
                    "buttons": ["检查", "ZH"],
                    "prompt": "",
                    "urls": [],
                }
            )
        )
        self.assertFalse(
            reg._is_hcaptcha_retry_shell(
                {
                    "body": "请再试一次 ⚠️",
                    "buttons": ["刷新挑战。 | 刷新挑战。", "跳过挑战 | 跳过挑战"],
                    "prompt": "找出所有需要插头才能工作的物品",
                    "urls": [],
                    "canvas_b64": "canvas-b64",
                }
            )
        )
        self.assertFalse(
            reg._is_hcaptcha_retry_shell(
                {
                    "body": "请选择所有包含汽车的图片",
                    "buttons": ["检查"],
                    "prompt": "请选择所有包含汽车的图片",
                    "urls": ["img-1"],
                }
            )
        )

    def test_solve_hcaptcha_challenge_retry_shell_accepts_next_button(self):
        solver = mock.Mock()
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    return_value={
                        "prompt": "",
                        "urls": [],
                        "body": "请再试一次 ⚠️",
                        "buttons": ["下一个", "ZH"],
                        "selected": set(),
                        "canvas_b64": "",
                        "screenshot_b64": "iframe-shot-b64",
                    },
                ):
                    with mock.patch.object(
                        reg, "_extract_hcaptcha_token", side_effect=["", "page-token-1234567890"]
                    ):
                        with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                            with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True) as click_button:
                                token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        self.assertGreaterEqual(click_button.call_count, 1)
        labels = click_button.call_args_list[0].args[1]
        self.assertIn("下一个", labels)
        self.assertIn("next", labels)
        self.assertIn("跳过", labels)
        self.assertIn("skip", labels)

    def test_solve_hcaptcha_challenge_fails_fast_on_retry_shell_loop(self):
        solver = mock.Mock()
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        retry_snapshot = {
            "prompt": "",
            "urls": [],
            "body": "请再试一次 ⚠️",
            "buttons": ["下一个", "ZH"],
            "selected": set(),
            "canvas_b64": "",
            "screenshot_b64": "iframe-shot-b64",
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg, "_capture_hcaptcha_challenge", side_effect=[dict(retry_snapshot) for _ in range(6)]
                ):
                    with mock.patch.object(reg, "_extract_hcaptcha_token", return_value=""):
                        with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                            with self.assertRaisesRegex(RuntimeError, "retry shell 循环超过阈值"):
                                reg._solve_hcaptcha_challenge(page)

    def test_solve_hcaptcha_challenge_fails_fast_on_repeated_empty_visual_clicks(self):
        solver = mock.Mock()
        solver.solve_image.return_value = '{"action":"click","clicks":[]}'
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }
        visual_snapshot = {
            "prompt": "选择所有人造物体",
            "urls": [],
            "body": "",
            "buttons": ["跳过"],
            "selected": set(),
            "canvas_b64": "canvas-b64",
            "screenshot_b64": "iframe-shot-b64",
            "tile_count": 0,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg, "_capture_hcaptcha_challenge", side_effect=[dict(visual_snapshot) for _ in range(4)]
                ):
                    with mock.patch.object(reg, "_extract_hcaptcha_token", return_value=""):
                        with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                            with self.assertRaisesRegex(
                                RuntimeError,
                                "视觉结果持续缺少可执行 clicks|视觉动作重复无进展",
                            ):
                                reg._solve_hcaptcha_challenge(page)

    def test_solve_hcaptcha_challenge_fails_fast_on_repeated_visual_parse_errors(self):
        solver = mock.Mock()
        solver.solve_image.return_value = "not-json"
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }
        visual_snapshot = {
            "prompt": "选择所有人造物体",
            "urls": [],
            "body": "",
            "buttons": ["跳过"],
            "selected": set(),
            "canvas_b64": "canvas-b64",
            "screenshot_b64": "iframe-shot-b64",
            "tile_count": 0,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg, "_capture_hcaptcha_challenge", side_effect=[dict(visual_snapshot) for _ in range(8)]
                ):
                    with mock.patch.object(reg, "_extract_hcaptcha_token", return_value=""):
                        with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                            with self.assertRaisesRegex(RuntimeError, "视觉求解持续失败"):
                                reg._solve_hcaptcha_challenge(page)

    def test_solve_hcaptcha_challenge_fails_fast_on_repeated_same_visual_signature(self):
        solver = mock.Mock()
        solver.solve_image.return_value = (
            '{"captcha_type":"drag_match","action":"drag_match","pairs":[{"id":1,"from":{"x":100,"y":100},"to":{"x":200,"y":200}}]}'
        )
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }
        visual_snapshot = {
            "prompt": "拖动正确的对象以完成图案",
            "urls": [],
            "body": "拖动正确的对象以完成图案",
            "buttons": ["跳过"],
            "selected": set(),
            "canvas_b64": "canvas-b64",
            "screenshot_b64": "iframe-shot-b64",
            "tile_count": 0,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    side_effect=[dict(visual_snapshot) for _ in range(6)],
                ):
                    with mock.patch.object(reg, "_extract_hcaptcha_token", return_value=""):
                        with self.assertRaisesRegex(RuntimeError, "视觉动作重复无进展"):
                            reg._solve_hcaptcha_challenge(page)

    def test_solve_hcaptcha_challenge_does_not_repeat_fail_when_clicks_change(self):
        solver = mock.Mock()
        solver.solve_image.side_effect = [
            '{"captcha_type":"click","action":"click","clicks":[{"x":171,"y":315},{"x":813,"y":641}]}',
            '{"captcha_type":"click","action":"click","clicks":[{"x":233,"y":401},{"x":1125,"y":332},{"x":211,"y":611},{"x":544,"y":610}]}',
            '{"captcha_type":"click","action":"click","clicks":[{"x":380,"y":410},{"x":910,"y":610}]}',
        ]
        logs: list[str] = []
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=logs.append, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }
        visual_snapshot = {
            "prompt": "请点击有光泽的物体",
            "urls": [],
            "body": "请点击有光泽的物体",
            "buttons": ["跳过"],
            "selected": set(),
            "canvas_b64": "canvas-b64",
            "screenshot_b64": "iframe-shot-b64",
            "tile_count": 0,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    side_effect=[dict(visual_snapshot) for _ in range(3)],
                ):
                    with mock.patch.object(reg, "_extract_hcaptcha_token", return_value=""):
                        with mock.patch.object(reg, "_perform_hcaptcha_visual_action", return_value=None):
                            with mock.patch.object(
                                reg,
                                "_wait_hcaptcha_visual_result",
                                side_effect=["", "", "page-token-1234567890"],
                            ):
                                token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        self.assertEqual(solver.solve_image.call_count, 3)
        self.assertTrue(any("click_count=4" in line for line in logs))
        self.assertTrue(all("repeat=1/3" in line for line in logs if "视觉求解" in line))

    def test_find_hcaptcha_challenge_iframe_prefers_real_challenge_over_checkbox(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        iframe_locator = mock.Mock()
        checkbox_iframe = mock.Mock()
        challenge_iframe = mock.Mock()
        unrelated_iframe = mock.Mock()
        page.locator.return_value = iframe_locator
        iframe_locator.count.return_value = 3
        iframe_locator.nth.side_effect = lambda idx: [checkbox_iframe, challenge_iframe, unrelated_iframe][idx]

        checkbox_iframe.get_attribute.side_effect = lambda name: {
            "src": "https://assets-cn1.hcaptcha.com/captcha.html#frame=checkbox&id=abc",
            "title": "包含 hCaptcha 安全挑战复选框的小部件",
        }.get(name)
        checkbox_iframe.bounding_box.return_value = {"x": 1, "y": 2, "width": 302, "height": 76}

        challenge_iframe.get_attribute.side_effect = lambda name: {
            "src": "https://assets-cn1.hcaptcha.com/captcha.html#frame=challenge&id=abc",
            "title": "hCaptcha挑战",
        }.get(name)
        challenge_iframe.bounding_box.return_value = {"x": 3, "y": 4, "width": 520, "height": 570}

        unrelated_iframe.get_attribute.side_effect = lambda name: {
            "src": "https://example.com/embed",
            "title": "third-party-widget",
        }.get(name)
        unrelated_iframe.bounding_box.return_value = {"x": 5, "y": 6, "width": 100, "height": 80}

        self.assertIs(reg._find_hcaptcha_challenge_iframe(page), challenge_iframe)

    def test_page_looks_like_email_verification_for_profile_complete_cn(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/v1/profile-complete?foo=bar"
        page.locator.return_value.count.return_value = 0

        self.assertTrue(reg._page_looks_like_email_verification(page))

    def test_page_looks_like_privacy_consent_for_static_login_consent(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://static-login.nvidia.com/service/default/noir/consent/developer/v1-1"
        button_locator = mock.Mock()
        button_locator.count.return_value = 1
        checkbox_locator = mock.Mock()
        checkbox_locator.count.return_value = 0

        def locator_side_effect(selector):
            if selector == "button":
                return button_locator
            if selector == 'input[type="checkbox"]':
                return checkbox_locator
            raise AssertionError(selector)

        page.locator.side_effect = locator_side_effect

        self.assertTrue(reg._page_looks_like_privacy_consent(page))

    def test_page_looks_like_privacy_consent_does_not_trigger_on_submit_text_alone(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://build.nvidia.com/"
        with mock.patch("platforms.nvidia.core._safe_body_text", return_value="提交"):
            self.assertFalse(reg._page_looks_like_privacy_consent(page))

    def test_page_looks_like_cloud_account_setup(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://cloudaccounts.nvidia.com/sf/v2/select-account?count=0"

        self.assertTrue(reg._page_looks_like_cloud_account_setup(page))

    def test_page_looks_like_build_verify_gate(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://build.nvidia.com/"
        with mock.patch(
            "platforms.nvidia.core._safe_body_text",
            return_value="Please verify your account to get API access. Verify",
        ):
            self.assertTrue(reg._page_looks_like_build_verify_gate(page))

    def test_page_looks_like_verify_success_page(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/zh-CN/profile-management/verify-email?code=demo"
        with mock.patch(
            "platforms.nvidia.core._safe_body_text",
            return_value="验证成功！ 返回原始页面并继续。 此页面将尝试在 0 秒内自动关闭。",
        ):
            self.assertTrue(reg._page_looks_like_verify_success_page(page))

    def test_try_resolve_session_context_prefers_absolute_ngc_endpoints(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        probe_page = mock.Mock()
        probe_page.goto.return_value = None
        probe_page.wait_for_timeout.return_value = None

        def fetch_side_effect(page, url, **kwargs):
            if url == "https://api.ngc.nvidia.com/user-context":
                return {"json": {"orgName": "demo-org"}}
            if url == "https://api.ngc.nvidia.com/v2/users/me":
                return {"json": {"user": {"verified": True}}}
            raise AssertionError(url)

        with mock.patch.object(reg, "_fetch_json", side_effect=fetch_side_effect) as fetch_mock:
            result = reg._try_resolve_session_context(probe_page)

        self.assertEqual(result["org_name"], "demo-org")
        self.assertEqual(result["user"]["verified"], True)
        self.assertEqual(fetch_mock.call_args_list[0].args[1], "https://api.ngc.nvidia.com/user-context")
        self.assertEqual(fetch_mock.call_args_list[1].args[1], "https://api.ngc.nvidia.com/v2/users/me")

    def test_submit_email_code_clicks_cn_continue_button(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        split_inputs = mock.Mock()
        split_inputs.count.return_value = 6
        digit_inputs = [mock.Mock() for _ in range(6)]
        split_inputs.nth.side_effect = lambda idx: digit_inputs[idx]
        page.locator.return_value = split_inputs

        with mock.patch.object(reg, "_click_text_button", side_effect=[False, False, False, False, False, True]) as click_mock:
            reg._submit_email_code(page, "123456")

        self.assertEqual([node.fill.call_args.args[0] for node in digit_inputs], list("123456"))
        self.assertEqual(
            [call.args[1][0] for call in click_mock.call_args_list],
            ["verify", "continue", "submit", "next", "验证", "继续"],
        )

    def test_submit_privacy_consent_clicks_submit(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        checkbox_locator = mock.Mock()
        checkbox_locator.count.return_value = 2
        checkbox_one = mock.Mock()
        checkbox_one.is_checked.return_value = False
        checkbox_two = mock.Mock()
        checkbox_two.is_checked.return_value = True
        checkbox_locator.nth.side_effect = lambda idx: [checkbox_one, checkbox_two][idx]

        def locator_side_effect(selector):
            if selector == 'input[type="checkbox"]':
                return checkbox_locator
            raise AssertionError(selector)

        page.locator.side_effect = locator_side_effect

        with mock.patch.object(reg, "_click_text_button", return_value=True) as click_mock:
            reg._submit_privacy_consent(page)

        checkbox_one.check.assert_called_once()
        checkbox_two.check.assert_not_called()
        click_mock.assert_called_once_with(
            page,
            ["提交", "submit", "continue", "accept", "agree", "同意", "确认"],
        )

    def test_submit_cloud_account_setup_fills_name_and_clicks_button(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        input_locator = mock.Mock()
        page.locator.return_value.first = input_locator

        with mock.patch.object(reg, "_build_cloud_account_name", return_value="aar-demo-123456"):
            with mock.patch.object(reg, "_click_text_button", return_value=True) as click_mock:
                reg._submit_cloud_account_setup(page, email="demo@example.com")

        input_locator.wait_for.assert_called_once()
        input_locator.fill.assert_called_once_with("aar-demo-123456")
        click_mock.assert_called_once_with(
            page,
            ["create nvidia cloud account", "create cloud account"],
        )

    def test_submit_build_verify_gate_clicks_verify(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()

        with mock.patch.object(reg, "_click_text_button", return_value=True) as click_mock:
            reg._submit_build_verify_gate(page)

        click_mock.assert_called_once_with(page, ["verify"])

    def test_return_from_verify_success_page_goes_back_to_build(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()

        reg._return_from_verify_success_page(page)

        page.goto.assert_called_once_with(
            "https://build.nvidia.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )

    def test_complete_post_create_flow_retries_with_new_email_code(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        probe_page = mock.Mock()
        otp_calls = []
        codes = iter(["111-111", "222-222"])

        def otp_callback(**kwargs):
            otp_calls.append(dict(kwargs))
            return next(codes)

        with mock.patch("platforms.nvidia.core.time.time", return_value=1000.0):
            with mock.patch.object(
                reg,
                "_try_resolve_session_context",
                side_effect=[None, None, {"org_name": "demo-org"}],
            ):
                with mock.patch.object(
                    reg,
                    "_page_looks_like_email_verification",
                    side_effect=[True, True],
                ):
                    with mock.patch.object(reg, "_submit_email_code") as submit_mock:
                        with mock.patch.object(reg, "_page_looks_like_privacy_consent", return_value=False):
                            with mock.patch.object(reg, "_page_looks_like_cloud_account_setup", return_value=False):
                                result = reg._complete_post_create_flow(
                                    page,
                                    probe_page,
                                    otp_callback=otp_callback,
                                    email="user@example.com",
                                )

        self.assertEqual(result["org_name"], "demo-org")
        self.assertEqual([call.args[1] for call in submit_mock.call_args_list], ["111-111", "222-222"])
        self.assertEqual(otp_calls[0]["exclude_codes"], set())
        self.assertIsNone(otp_calls[0]["otp_sent_at"])
        self.assertEqual(otp_calls[1]["exclude_codes"], {"111-111", "111111"})
        self.assertIsNone(otp_calls[1]["otp_sent_at"])

    def test_complete_post_create_flow_uses_verification_link_on_build_gate(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://build.nvidia.com/"
        probe_page = mock.Mock()

        with mock.patch("platforms.nvidia.core.time.time", return_value=1000.0):
            with mock.patch.object(
                reg,
                "_try_resolve_session_context",
                side_effect=[None, {"org_name": "demo-org"}],
            ):
                with mock.patch.object(reg, "_page_looks_like_email_verification", return_value=False):
                    with mock.patch.object(reg, "_page_looks_like_privacy_consent", return_value=False):
                        with mock.patch.object(reg, "_page_looks_like_cloud_account_setup", return_value=False):
                            with mock.patch.object(reg, "_page_looks_like_build_verify_gate", return_value=True):
                                result = reg._complete_post_create_flow(
                                    page,
                                    probe_page,
                                    verification_link_callback=lambda: "https://login.nvgs.nvidia.cn/profile-management/verify-email?code=demo",
                                    email="user@example.com",
                                )

        self.assertEqual(result["org_name"], "demo-org")
        page.goto.assert_called_once_with(
            "https://login.nvgs.nvidia.cn/profile-management/verify-email?code=demo",
            wait_until="domcontentloaded",
            timeout=30000,
        )

    def test_complete_post_create_flow_returns_from_verify_success_page(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        probe_page = mock.Mock()

        with mock.patch("platforms.nvidia.core.time.time", return_value=1000.0):
            with mock.patch.object(
                reg,
                "_try_resolve_session_context",
                side_effect=[None, {"org_name": "demo-org"}],
            ):
                with mock.patch.object(reg, "_page_looks_like_email_verification", return_value=False):
                    with mock.patch.object(reg, "_page_looks_like_privacy_consent", return_value=False):
                        with mock.patch.object(reg, "_page_looks_like_cloud_account_setup", return_value=False):
                            with mock.patch.object(reg, "_page_looks_like_build_verify_gate", return_value=False):
                                with mock.patch.object(reg, "_page_looks_like_verify_success_page", return_value=True):
                                    with mock.patch.object(reg, "_return_from_verify_success_page") as return_mock:
                                        result = reg._complete_post_create_flow(
                                            page,
                                            probe_page,
                                            email="user@example.com",
                                        )

        self.assertEqual(result["org_name"], "demo-org")
        return_mock.assert_called_once_with(page)

    def test_hcaptcha_drag_prompt_uses_visual_recognition_even_with_urls(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        self.assertTrue(
            reg._needs_hcaptcha_visual_recognition(
                {
                    "prompt": "请把上方图标拖至匹配位置",
                    "body": "请把上方图标拖至匹配位置",
                    "urls": ["https://imgs.example.com/piece.png"],
                    "screenshot_b64": "shot",
                    "buttons": ["跳过挑战"],
                }
            )
        )

    def test_solve_hcaptcha_falls_back_to_page_challenge_when_direct_solver_fails(self):
        solver = mock.Mock()
        solver.solve_hcaptcha.side_effect = RuntimeError("ERROR_CAPTCHA_UNSOLVABLE")
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/v1/create-account"

        with mock.patch.object(reg, "_extract_hcaptcha_sitekey", return_value="site-key-123"):
            with mock.patch.object(reg, "_solve_hcaptcha_challenge", return_value="page-token") as challenge_mock:
                reg._solve_hcaptcha(page)

        challenge_mock.assert_called_once_with(page)

    def test_solve_hcaptcha_waits_for_late_sitekey(self):
        solver = mock.Mock()
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.url = "https://login.nvgs.nvidia.cn/v1/create-account"

        with mock.patch.object(
            reg,
            "_extract_hcaptcha_sitekey",
            side_effect=["", "", "site-key-123"],
        ):
            with mock.patch("platforms.nvidia.core._safe_body_text", return_value="验证程序加载失败，请检查您的浏览器设置"):
                with mock.patch.object(reg, "_solve_hcaptcha_challenge", return_value="page-token") as challenge_mock:
                    reg._solve_hcaptcha(page)

        self.assertGreaterEqual(page.wait_for_timeout.call_count, 2)
        challenge_mock.assert_called_once_with(page)

    def test_solve_hcaptcha_challenge_clicks_targets_from_classification(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [5, 7, 8]
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        checkbox = mock.Mock()
        frame_locator = mock.Mock()
        frame_locator.locator.return_value = checkbox
        page.frame_locator.return_value.first = frame_locator

        challenge_frame = mock.Mock()
        task_locator = mock.Mock()
        task_buttons = [mock.Mock() for _ in range(9)]
        task_locator.nth.side_effect = lambda idx: task_buttons[idx]
        challenge_frame.locator.return_value = task_locator
        challenge_iframe = mock.Mock()

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    return_value={
                        "prompt": "请选择所有人造物体",
                        "urls": [f"https://imgs.example.com/{idx}.jpeg" for idx in range(9)],
                        "body": "",
                    },
                ):
                    with mock.patch.object(
                        reg, "_download_hcaptcha_images", return_value=[f"img-{idx}" for idx in range(9)]
                    ):
                        with mock.patch.object(
                            reg, "_extract_hcaptcha_token", side_effect=["", "page-token-1234567890"]
                        ):
                            with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                                with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                    token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        checkbox_wait.wait_for.assert_called_once()
        checkbox.click.assert_called_once()
        solver.classify_hcaptcha.assert_called_once_with(
            "请选择所有人造物体",
            [f"img-{idx}" for idx in range(9)],
            **_visual_solver_kwargs(),
        )
        for idx in (5, 7, 8):
            task_buttons[idx].click.assert_called_once()

    def test_solve_hcaptcha_challenge_uses_embedded_tile_images_when_urls_missing(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [0, 1]
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        checkbox = mock.Mock()
        frame_locator = mock.Mock()
        frame_locator.locator.return_value = checkbox
        page.frame_locator.return_value.first = frame_locator

        challenge_frame = mock.Mock()
        task_locator = mock.Mock()
        task_buttons = [mock.Mock() for _ in range(2)]
        task_locator.nth.side_effect = lambda idx: task_buttons[idx]
        challenge_frame.locator.return_value = task_locator
        challenge_iframe = mock.Mock()

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    return_value={
                        "prompt": "请选择所有人造物体",
                        "urls": [],
                        "images_b64": ["img-0", "img-1"],
                        "body": "",
                        "buttons": ["检查"],
                        "selected": set(),
                    },
                ):
                    with mock.patch.object(
                        reg, "_extract_hcaptcha_token", side_effect=["", "page-token-1234567890"]
                    ):
                        with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                            with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        solver.classify_hcaptcha.assert_called_once_with(
            "请选择所有人造物体",
            ["img-0", "img-1"],
            **_visual_solver_kwargs(),
        )
        solver.solve_image.assert_not_called()

    def test_solve_hcaptcha_challenge_skips_invalid_classification_result(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = None
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox
        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        snapshots = [
            {
                "prompt": "请选择所有人造物体",
                "urls": [],
                "images_b64": ["img-0"],
                "body": "",
                "buttons": ["跳过", "检查"],
                "selected": set(),
            },
            {
                "prompt": "",
                "urls": [],
                "images_b64": [],
                "body": "",
                "buttons": [],
                "selected": set(),
            },
        ]

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(reg, "_capture_hcaptcha_challenge", side_effect=snapshots):
                    with mock.patch.object(
                        reg, "_extract_hcaptcha_token", side_effect=["", "page-token-1234567890"]
                    ):
                        with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                            with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True) as click_button:
                                token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        click_button.assert_any_call(challenge_frame, ["跳过", "skip", "下一个", "next"])

    def test_solve_hcaptcha_challenge_uses_full_frame_when_prompt_images_missing(self):
        solver = mock.Mock()
        solver.solve_image.return_value = '{"action":"click","clicks":[{"x":720,"y":450}]}'
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }
        snapshot = {
            "prompt": "请点击有光泽的物体",
            "urls": [],
            "images_b64": [],
            "body": "请点击有光泽的物体\n跳过\nZH",
            "buttons": ["跳过", "检查"],
            "selected": set(),
            "screenshot_b64": "iframe-shot-b64",
            "tile_count": 3,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(reg, "_capture_hcaptcha_challenge", return_value=snapshot):
                    with mock.patch.object(
                        reg,
                        "_extract_hcaptcha_token",
                        side_effect=["", "", "page-token-1234567890"],
                    ):
                        with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                            with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                                token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        solver.classify_hcaptcha.assert_not_called()
        solver.solve_image.assert_called_once_with(
            "iframe-shot-b64",
            prompt="请点击有光泽的物体",
            **_visual_solver_kwargs(),
        )
        page.mouse.click.assert_called_once_with(460.0, 425.0)

    def test_normalize_hcaptcha_targets_filters_invalid_values(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        targets = reg._normalize_hcaptcha_targets([5, 5, -1, 7, "8", 8, 10], 9)
        self.assertEqual(targets, [5, 7, 8])

    def test_solve_hcaptcha_challenge_retries_retry_shell_before_classification(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [1]
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox
        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame

        challenge_frame = mock.Mock()
        task_locator = mock.Mock()
        task_buttons = [mock.Mock() for _ in range(3)]
        task_locator.nth.side_effect = lambda idx: task_buttons[idx]
        challenge_frame.locator.return_value = task_locator
        challenge_iframe = mock.Mock()

        snapshots = [
            {"prompt": "", "urls": [], "body": "请再试一次 ⚠️ 检查 ZH", "buttons": ["检查", "ZH"]},
            {
                "prompt": "请选择所有人造物体",
                "urls": [f"https://imgs.example.com/{idx}.jpeg" for idx in range(3)],
                "body": "",
                "buttons": ["检查"],
            },
        ]

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(reg, "_capture_hcaptcha_challenge", side_effect=snapshots):
                    with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True) as click_button:
                        with mock.patch.object(
                            reg, "_download_hcaptcha_images", return_value=[f"img-{idx}" for idx in range(3)]
                        ):
                            with mock.patch.object(
                                reg, "_extract_hcaptcha_token", side_effect=["", "", "", "page-token-1234567890"]
                            ):
                                with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                    token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        self.assertGreaterEqual(click_button.call_count, 2)
        checkbox.click.assert_called()
        solver.classify_hcaptcha.assert_called_once_with(
            "请选择所有人造物体",
            [f"img-{idx}" for idx in range(3)],
            **_visual_solver_kwargs(),
        )

    def test_solve_hcaptcha_challenge_rechecks_skip_prompt_before_skipping(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [1]
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "true"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox
        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame

        challenge_frame = mock.Mock()
        task_locator = mock.Mock()
        task_buttons = [mock.Mock() for _ in range(3)]
        task_locator.nth.side_effect = lambda idx: task_buttons[idx]
        challenge_frame.locator.return_value = task_locator
        challenge_iframe = mock.Mock()

        snapshots = [
            {
                "prompt": "选择所有人类创造的物品",
                "urls": [],
                "body": "",
                "buttons": ["跳过", "ZH"],
            },
            {
                "prompt": "选择所有人类创造的物品",
                "urls": [f"https://imgs.example.com/{idx}.jpeg" for idx in range(3)],
                "body": "",
                "buttons": ["检查"],
            },
        ]

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(reg, "_capture_hcaptcha_challenge", side_effect=snapshots):
                    with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True) as click_button:
                        with mock.patch.object(
                            reg, "_download_hcaptcha_images", return_value=[f"img-{idx}" for idx in range(3)]
                        ):
                            with mock.patch.object(
                                reg, "_extract_hcaptcha_token", side_effect=["", "", "", ""]
                            ):
                                with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                    token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "__HCAPTCHA_CHECKED__")
        solver.classify_hcaptcha.assert_called_once_with(
            "选择所有人类创造的物品",
            [f"img-{idx}" for idx in range(3)],
            **_visual_solver_kwargs(),
        )
        self.assertEqual(click_button.call_count, 2)

    def test_solve_hcaptcha_challenge_accepts_checked_checkbox_without_token(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [1]
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "true"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame

        challenge_frame = mock.Mock()
        task_locator = mock.Mock()
        task_buttons = [mock.Mock() for _ in range(3)]
        task_locator.nth.side_effect = lambda idx: task_buttons[idx]
        challenge_frame.locator.return_value = task_locator
        challenge_iframe = mock.Mock()

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    return_value={
                        "prompt": "请选择所有人造物体",
                        "urls": [f"https://imgs.example.com/{idx}.jpeg" for idx in range(3)],
                        "body": "",
                        "buttons": ["检查"],
                    },
                ):
                    with mock.patch.object(
                        reg, "_download_hcaptcha_images", return_value=[f"img-{idx}" for idx in range(3)]
                    ):
                        with mock.patch.object(
                            reg, "_extract_hcaptcha_token", side_effect=["", "", ""]
                        ):
                            with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True):
                                with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                    token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "__HCAPTCHA_CHECKED__")

    def test_solve_hcaptcha_challenge_uses_visual_recognition_for_canvas_prompt(self):
        solver = mock.Mock()
        solver.solve_image.return_value = '{"captcha_type":"click","action":"click","clicks":[{"x":720,"y":450}]}'
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    return_value={
                        "prompt": "请把上方图标拖至匹配位置",
                        "urls": ["https://example.com/drag-piece.png"],
                        "body": "",
                        "buttons": ["跳过挑战 | 跳过挑战"],
                        "screenshot_b64": "iframe-shot-b64",
                    },
                ):
                    with mock.patch.object(
                        reg, "_extract_hcaptcha_token", side_effect=["", "", "page-token-1234567890"]
                    ):
                        with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                            with mock.patch.object(reg, "_click_hcaptcha_button", return_value=False):
                                token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        solver.solve_image.assert_called_once_with(
            "iframe-shot-b64",
            prompt="请把上方图标拖至匹配位置",
            **_visual_solver_kwargs(),
        )
        page.mouse.click.assert_called_once_with(460.0, 425.0)

    def test_perform_hcaptcha_visual_action_accepts_pair_and_range_points(self):
        reg = NvidiaRegister(captcha_solver=None, proxy=None, log_fn=lambda *_: None, headless=True)
        page = mock.Mock()
        page.mouse = mock.Mock()
        challenge_frame = mock.Mock()
        bbox = {"x": 0.0, "y": 0.0, "width": 1440.0, "height": 900.0}

        click_action = {
            "action": "click",
            "clicks": [
                {"x": [130, 570], "label": "勺子食物"},
            ],
        }
        drag_action = {
            "action": "drag_match",
            "pairs": [
                {
                    "from": {"x": [100, 200], "label": "可拖动块"},
                    "to": {"x": [300, 500], "y": [600, 800], "label": "目标范围"},
                }
            ],
        }

        with mock.patch.object(reg, "_click_hcaptcha_button", return_value=False):
            reg._perform_hcaptcha_visual_action(page, challenge_frame, bbox, click_action)
            reg._perform_hcaptcha_visual_action(page, challenge_frame, bbox, drag_action)

        page.mouse.click.assert_called_once_with(130.0, 570.0)
        self.assertEqual(page.mouse.move.call_args_list[0].args, (100.0, 200.0))
        self.assertEqual(page.mouse.move.call_args_list[1].args, (400.0, 700.0))
        self.assertEqual(page.mouse.move.call_args_list[1].kwargs, {"steps": 20})

    def test_solve_hcaptcha_challenge_falls_back_to_visual_when_tasks_not_visible(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [0, 1]
        solver.solve_image.return_value = '{"action":"click","clicks":[{"x":[130,570]}]}'
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        hidden_tasks = mock.Mock()
        hidden_tasks.count.return_value = 0
        visible_tasks = mock.Mock()
        visible_tasks.first.wait_for.side_effect = Exception("not visible")
        failed_task = mock.Mock()
        failed_task.click.side_effect = RuntimeError("not clickable")
        failed_task.bounding_box.return_value = None
        visible_tasks.nth.return_value = failed_task
        challenge_frame.locator.side_effect = [hidden_tasks, visible_tasks]
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(
                    reg,
                    "_capture_hcaptcha_challenge",
                    return_value={
                        "prompt": "选出示例图片中所有可见的内容",
                        "urls": ["https://imgs.example.com/0.png", "https://imgs.example.com/1.png"],
                        "body": "",
                        "buttons": ["检查"],
                        "selected": set(),
                        "screenshot_b64": "iframe-shot-b64",
                    },
                ):
                    with mock.patch.object(
                        reg, "_download_hcaptcha_images", return_value=["img-0", "img-1"]
                    ):
                        with mock.patch.object(
                            reg, "_extract_hcaptcha_token", side_effect=["", "page-token-1234567890"]
                        ):
                            with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                with mock.patch.object(reg, "_click_hcaptcha_button", return_value=False):
                                    token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        solver.classify_hcaptcha.assert_called_once_with(
            "选出示例图片中所有可见的内容",
            ["img-0", "img-1"],
            **_visual_solver_kwargs(),
        )
        solver.solve_image.assert_called_once_with(
            "iframe-shot-b64",
            prompt="选出示例图片中所有可见的内容",
            **_visual_solver_kwargs(),
        )
        page.mouse.click.assert_called_once_with(165.0, 485.0)

    def test_solve_hcaptcha_challenge_skips_when_visual_fallback_on_grid_fails(self):
        solver = mock.Mock()
        solver.classify_hcaptcha.return_value = [0]
        solver.solve_image.return_value = '{"action":"click","clicks":[]}'
        reg = NvidiaRegister(captcha_solver=solver, proxy=None, log_fn=lambda *_: None, headless=True)

        checkbox_wait = mock.Mock()
        checkbox = mock.Mock()
        checkbox.get_attribute.return_value = "false"
        checkbox_frame = mock.Mock()
        checkbox_frame.locator.return_value = checkbox

        page = mock.Mock()
        page.locator.return_value.first = checkbox_wait
        page.frame_locator.return_value.first = checkbox_frame
        page.mouse = mock.Mock()

        challenge_frame = mock.Mock()
        hidden_tasks = mock.Mock()
        hidden_tasks.count.return_value = 0
        visible_tasks = mock.Mock()
        visible_tasks.first.wait_for.side_effect = Exception("not visible")
        failed_task = mock.Mock()
        failed_task.click.side_effect = RuntimeError("not clickable")
        failed_task.bounding_box.return_value = None
        visible_tasks.nth.return_value = failed_task
        challenge_frame.locator.side_effect = [hidden_tasks, visible_tasks]
        challenge_iframe = mock.Mock()
        challenge_iframe.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 720.0,
            "height": 450.0,
        }

        snapshots = [
            {
                "prompt": "选出示例图片中所有可见的内容",
                "urls": ["https://imgs.example.com/0.png"],
                "body": "",
                "buttons": ["跳过", "检查"],
                "selected": set(),
                "screenshot_b64": "iframe-shot-b64",
            },
            {
                "prompt": "",
                "urls": [],
                "body": "",
                "buttons": [],
                "selected": set(),
                "screenshot_b64": "iframe-shot-b64",
            },
        ]

        with mock.patch.object(reg, "_find_hcaptcha_challenge_frame", return_value=challenge_frame):
            with mock.patch.object(reg, "_find_hcaptcha_challenge_iframe", return_value=challenge_iframe):
                with mock.patch.object(reg, "_capture_hcaptcha_challenge", side_effect=snapshots):
                    with mock.patch.object(
                        reg, "_download_hcaptcha_images", return_value=["img-0"]
                    ):
                        with mock.patch.object(
                            reg, "_extract_hcaptcha_token", side_effect=["", "page-token-1234567890"]
                        ):
                            with mock.patch.object(reg, "_inject_hcaptcha_token", return_value=True):
                                with mock.patch.object(reg, "_click_hcaptcha_button", return_value=True) as click_button:
                                    token = reg._solve_hcaptcha_challenge(page)

        self.assertEqual(token, "page-token-1234567890")
        solver.solve_image.assert_called_once_with(
            "iframe-shot-b64",
            prompt="选出示例图片中所有可见的内容",
            **_visual_solver_kwargs(),
        )
        click_button.assert_any_call(challenge_frame, ["跳过", "skip", "下一个", "next"])


if __name__ == "__main__":
    unittest.main()
