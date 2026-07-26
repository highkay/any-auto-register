import unittest
from unittest import mock

import requests

from platforms.grok.core import GrokRegister


class GrokCoreTests(unittest.TestCase):
    def test_extract_turnstile_sitekey_from_frame_url_path(self):
        sitekey = GrokRegister._extract_turnstile_sitekey_from_url(
            "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/f/ov2/av0/rch/otyu7/0x4AAAAAAAhr9JGVDZbrZOo0/light/fbE/failure_retry/flexible?lang=auto"
        )

        self.assertEqual(sitekey, "0x4AAAAAAAhr9JGVDZbrZOo0")

    def test_detect_blocked_signup_page_extracts_cloudflare_block(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.title.return_value = "Attention Required! | Cloudflare"

        with mock.patch("platforms.grok.core._safe_body_text", return_value="Sorry, you have been blocked. Cloudflare Ray ID: 123"):
            message = reg._detect_blocked_signup_page(page)

        self.assertIn("Attention Required! | Cloudflare", message)
        self.assertIn("Cloudflare Ray ID", message)

    def test_signup_gate_state_only_marks_real_email_signup_entry(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.locator.return_value.count.return_value = 0

        with mock.patch.object(reg, "_detect_blocked_signup_page", return_value=""):
            page.evaluate.return_value = ["Confirm email", "Go back"]
            self.assertEqual(reg._signup_gate_state(page), "loading")

            page.evaluate.return_value = ["Sign up with email", "Sign in"]
            self.assertEqual(reg._signup_gate_state(page), "email_button")

    def test_ensure_email_signup_form_clicks_when_document_is_interactive(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        page.locator.return_value.first.wait_for.return_value = None

        with mock.patch.object(reg, "_detect_blocked_signup_page", return_value=""):
            with mock.patch.object(
                reg,
                "_page_has_email_input",
                side_effect=[False, True],
            ):
                with mock.patch.object(
                    reg,
                    "_signup_gate_state",
                    return_value="email_button",
                ):
                    with mock.patch.object(
                        reg,
                        "_document_ready_state",
                        return_value="complete",
                    ) as ready_state_mock:
                        with mock.patch.object(
                            reg,
                            "_click_text_button",
                            return_value=True,
                        ) as click_mock:
                            with mock.patch.object(reg, "_page_wait") as wait_mock:
                                reg._ensure_email_signup_form(
                                    page,
                                    timeout=5,
                                    stage_label="Step1",
                                )

        click_mock.assert_called_once()
        self.assertIs(click_mock.call_args.args[0], page)
        self.assertIn("使用邮箱注册", click_mock.call_args.args[1])
        ready_state_mock.assert_called()
        wait_mock.assert_any_call(page, 1500)

    def test_human_click_locator_uses_mouse_trajectory(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=False,
        )
        page = mock.Mock()
        locator = mock.Mock()
        target = mock.Mock()
        locator.count.return_value = 1
        locator.first = target
        target.is_visible.return_value = True
        target.bounding_box.return_value = {"x": 100.0, "y": 200.0, "width": 40.0, "height": 20.0}
        page.mouse.move.return_value = None
        page.mouse.down.return_value = None
        page.mouse.up.return_value = None

        with mock.patch.object(reg, "_page_wait"):
            ok = reg._human_click_locator(page, locator)

        self.assertTrue(ok)
        page.mouse.move.assert_called_once_with(120.0, 210.0, steps=20)
        page.mouse.down.assert_called_once()
        page.mouse.up.assert_called_once()
        target.click.assert_not_called()

    def test_wait_until_calls_task_checkpoint_and_page_wait(self):
        task_control = mock.Mock()
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            task_control=task_control,
        )
        page = mock.Mock()
        states = iter([False, False, True])

        reg._wait_until(lambda: next(states), timeout=1.0, interval=0.25, page=page)

        self.assertGreaterEqual(task_control.checkpoint.call_count, 1)
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        page.wait_for_timeout.assert_called_with(250)

    def test_has_turnstile_runtime_detects_frame_or_api(self):
        page = mock.Mock()
        frame = mock.Mock()
        frame.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile"
        page.frames = [frame]
        self.assertTrue(GrokRegister._has_turnstile_runtime(page))

        page = mock.Mock()
        page.frames = []
        page.evaluate.return_value = True
        self.assertTrue(GrokRegister._has_turnstile_runtime(page))

        page.evaluate.return_value = False
        self.assertFalse(GrokRegister._has_turnstile_runtime(page))

    def test_read_turnstile_widget_ids_extracts_hidden_input_suffix(self):
        page = mock.Mock()
        page.evaluate.return_value = ["vib13", "widget-2"]

        self.assertEqual(
            GrokRegister._read_turnstile_widget_ids(page),
            ["vib13", "widget-2"],
        )

    def test_solve_turnstile_by_solver_passes_interrupt_checker(self):
        solver = mock.Mock()
        solver.solve_turnstile.return_value = "token-12345678901234567890"
        reg = GrokRegister(
            captcha_solver=solver,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            task_control=mock.Mock(),
        )
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"

        with mock.patch.object(reg, "_read_turnstile_sitekey", return_value="site-key-123"):
            with mock.patch.object(reg, "_inject_turnstile_token", return_value=True):
                with mock.patch.object(reg, "_read_turnstile_token", return_value="page-token-12345678901234567890"):
                    token = reg._solve_turnstile_by_solver(page)

        self.assertEqual(token, "page-token-12345678901234567890")
        interrupt_checker = solver.solve_turnstile.call_args.kwargs["interrupt_checker"]
        self.assertIs(interrupt_checker.__self__, reg)
        self.assertIs(interrupt_checker.__func__, reg._checkpoint.__func__)

    def test_solve_turnstile_by_same_session_solver_passes_rich_payload_and_interrupt_checker(self):
        solver = mock.Mock()
        solver.solve_turnstile_session.return_value = {
            "token": "token-12345678901234567890",
            "solverMode": "session_restore",
            "attempts": 1,
            "proxyMode": "task",
            "proxyServer": "socks5://127.0.0.1:7890",
            "finalURL": "https://accounts.x.ai/sign-up?redirect=grok-com",
        }
        logs: list[str] = []
        reg = GrokRegister(
            captcha_solver=solver,
            proxy="socks5h://127.0.0.1:7890",
            log_fn=logs.append,
            headless=True,
            task_control=mock.Mock(),
        )
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"

        with mock.patch.object(reg, "_read_turnstile_sitekey", return_value="site-key-123"):
            with mock.patch.object(
                reg,
                "_collect_turnstile_session_state",
                return_value={
                    "cookies": [{"name": "cf_clearance", "value": "cookie-value"}],
                    "origins": [{"origin": "https://accounts.x.ai"}],
                    "userAgent": "Mozilla/5.0",
                },
            ) as session_state_mock:
                with mock.patch.object(
                    reg,
                    "_collect_turnstile_widget_hints",
                    return_value={"frameUrl": "https://challenges.cloudflare.com/frame"},
                ) as widget_hints_mock:
                    with mock.patch.object(
                        reg,
                        "_collect_turnstile_runtime_hints",
                        return_value={"stepLabel": "grok_signup_step5"},
                    ) as runtime_hints_mock:
                        with mock.patch.object(reg, "_inject_turnstile_token", return_value=True):
                            with mock.patch.object(
                                reg,
                                "_read_turnstile_token",
                                return_value="page-token-12345678901234567890",
                            ):
                                token = reg._solve_turnstile_by_same_session_solver(page)

        self.assertEqual(token, "page-token-12345678901234567890")
        self.assertTrue(session_state_mock.called)
        self.assertTrue(widget_hints_mock.called)
        self.assertTrue(runtime_hints_mock.called)
        solve_kwargs = solver.solve_turnstile_session.call_args.kwargs
        self.assertEqual(
            solve_kwargs["browser_proxy"],
            {"server": "socks5://127.0.0.1:7890"},
        )
        interrupt_checker = solve_kwargs["interrupt_checker"]
        self.assertIs(interrupt_checker.__self__, reg)
        self.assertIs(interrupt_checker.__func__, reg._checkpoint.__func__)
        self.assertTrue(any("proxyMode=task" in entry for entry in logs))

    def test_collect_turnstile_solver_proxy_normalizes_proxy(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy="socks5h://127.0.0.1:7890",
            log_fn=lambda *_: None,
            headless=True,
        )

        self.assertEqual(
            reg._collect_turnstile_solver_proxy(),
            {"server": "socks5://127.0.0.1:7890"},
        )

    def test_collect_turnstile_solver_proxy_returns_none_without_proxy(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )

        self.assertIsNone(reg._collect_turnstile_solver_proxy())

    def test_resolve_browser_user_agent_uses_flaresolverr_root(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            extra={"grok_flaresolverr_url": "http://127.0.0.1:8191/v1"},
        )
        response = mock.Mock()
        response.content = b'{"userAgent":"Mozilla/5.0 (X11; Linux x86_64) Chrome/142.0.0.0 Safari/537.36"}'
        response.json.return_value = {
            "userAgent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/142.0.0.0 Safari/537.36"
        }
        response.raise_for_status.return_value = None

        with mock.patch("platforms.grok.core.requests.get", return_value=response) as get_mock:
            ua = reg._resolve_browser_user_agent()

        self.assertEqual(
            ua,
            "Mozilla/5.0 (X11; Linux x86_64) Chrome/142.0.0.0 Safari/537.36",
        )
        get_mock.assert_called_once_with("http://127.0.0.1:8191", timeout=10)

    def test_build_browser_identity_override_aligns_chrome_metadata(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )

        payload = reg._build_browser_identity_override(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.62 Safari/537.36"
        )

        self.assertEqual(
            payload["userAgent"],
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.7444.62 Safari/537.36",
        )
        self.assertEqual(payload["acceptLanguage"], "en-US,en;q=0.9")
        self.assertEqual(payload["platform"], "Linux x86_64")
        self.assertEqual(
            payload["userAgentMetadata"]["brands"],
            [
                {"brand": "Google Chrome", "version": "142"},
                {"brand": "Chromium", "version": "142"},
                {"brand": "Not/A)Brand", "version": "99"},
            ],
        )
        self.assertEqual(
            payload["userAgentMetadata"]["fullVersion"],
            "142.0.7444.62",
        )
        self.assertEqual(payload["userAgentMetadata"]["platform"], "Linux")

    def test_apply_browser_identity_uses_cdp_override(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        context = mock.Mock()
        page = mock.Mock()
        cdp = mock.Mock()
        context.new_cdp_session.return_value = cdp

        reg._apply_browser_identity(
            context,
            page,
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        )

        context.new_cdp_session.assert_called_once_with(page)
        cdp.send.assert_called_once()
        call_args = cdp.send.call_args.args
        self.assertEqual(call_args[0], "Emulation.setUserAgentOverride")
        self.assertEqual(call_args[1]["userAgentMetadata"]["fullVersion"], "142.0.0.0")
        self.assertEqual(
            call_args[1]["userAgentMetadata"]["brands"][0],
            {"brand": "Google Chrome", "version": "142"},
        )

    def test_collect_flaresolverr_proxy_url_normalizes_proxy(self):
        logs: list[str] = []
        reg = GrokRegister(
            captcha_solver=None,
            proxy="socks5h://127.0.0.1:7890",
            log_fn=logs.append,
            headless=True,
        )

        self.assertEqual(
            reg._collect_flaresolverr_proxy_url(),
            "socks5://host.docker.internal:7890",
        )
        self.assertTrue(any("host.docker.internal" in entry for entry in logs))

    def test_collect_flaresolverr_proxy_url_can_preserve_loopback_proxy(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy="socks5h://127.0.0.1:7890",
            log_fn=lambda *_: None,
            headless=True,
            extra={"grok_flaresolverr_bridge_loopback_proxy": "false"},
        )

        self.assertEqual(
            reg._collect_flaresolverr_proxy_url(),
            "socks5://127.0.0.1:7890",
        )

    def test_collect_flaresolverr_proxy_url_preserves_proxy_auth_when_bridging_loopback(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy="http://user:p%40ss@127.0.0.1:7890",
            log_fn=lambda *_: None,
            headless=True,
        )

        self.assertEqual(
            reg._collect_flaresolverr_proxy_url(),
            "http://user:p%40ss@host.docker.internal:7890",
        )

    def test_apply_flaresolverr_cookies_adds_context_cookies(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.context.add_cookies = mock.Mock()

        names = reg._apply_flaresolverr_cookies(
            page,
            [
                {
                    "name": "cf_clearance",
                    "value": "cookie-value",
                    "domain": "accounts.x.ai",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                    "expiry": 1893456000,
                }
            ],
        )

        self.assertEqual(names, ["cf_clearance"])
        payload = page.context.add_cookies.call_args.args[0]
        self.assertEqual(payload[0]["name"], "cf_clearance")
        self.assertEqual(payload[0]["domain"], "accounts.x.ai")
        self.assertEqual(payload[0]["sameSite"], "None")
        self.assertEqual(payload[0]["expires"], 1893456000.0)

    def test_collect_turnstile_widget_hints_includes_widget_ids(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.frames = []

        with mock.patch.object(reg, "_find_turnstile_widget", return_value=(None, None)):
            with mock.patch.object(reg, "_read_turnstile_widget_ids", return_value=["vib13"]):
                hints = reg._collect_turnstile_widget_hints(page)

        self.assertEqual(hints["responseInputSelector"], 'input[name="cf-turnstile-response"]')
        self.assertEqual(hints["widgetIds"], ["vib13"])

    def test_request_flaresolverr_solution_retries_until_cf_clearance(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            extra={"grok_flaresolverr_url": "http://127.0.0.1:8191/v1"},
        )
        create_resp = mock.Mock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"status": "ok"}
        first_resp = mock.Mock()
        first_resp.raise_for_status.return_value = None
        first_resp.json.return_value = {
            "status": "ok",
            "solution": {
                "cookies": [
                    {"name": "__cf_bm", "value": "bm"},
                    {"name": "xai_anon_id", "value": "anon"},
                ]
            },
        }
        second_resp = mock.Mock()
        second_resp.raise_for_status.return_value = None
        second_resp.json.return_value = {
            "status": "ok",
            "solution": {
                "cookies": [
                    {"name": "__cf_bm", "value": "bm"},
                    {"name": "cf_clearance", "value": "clear"},
                    {"name": "xai_anon_id", "value": "anon"},
                ]
            },
        }
        destroy_resp = mock.Mock()
        session = mock.Mock()
        session.post.side_effect = [create_resp, first_resp, second_resp, destroy_resp]

        with mock.patch("platforms.grok.core.requests.Session", return_value=session):
            with mock.patch.object(reg, "_sleep_with_checkpoint") as sleep_mock:
                solution = reg._request_flaresolverr_solution(
                    stage_label="Step5 前",
                    target_url="https://accounts.x.ai/sign-up?redirect=grok-com",
                )

        self.assertEqual(
            [cookie["name"] for cookie in solution["cookies"]],
            ["__cf_bm", "cf_clearance", "xai_anon_id"],
        )
        self.assertEqual(session.post.call_count, 4)
        sleep_mock.assert_called_once_with(1.0)

    def test_request_flaresolverr_solution_surfaces_http_error_body(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
            extra={"grok_flaresolverr_url": "http://127.0.0.1:8191/v1"},
        )
        create_resp = mock.Mock()
        create_resp.raise_for_status.return_value = None
        create_resp.json.return_value = {"status": "ok"}
        fail_resp = mock.Mock()
        fail_resp.status_code = 500
        fail_resp.text = '{"status":"error","message":"Cloudflare IP is banned"}'
        fail_resp.json.return_value = {
            "status": "error",
            "message": "Cloudflare IP is banned",
        }
        fail_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        destroy_resp = mock.Mock()
        session = mock.Mock()
        session.post.side_effect = [create_resp, fail_resp, destroy_resp]

        with mock.patch("platforms.grok.core.requests.Session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "Cloudflare IP is banned"):
                reg._request_flaresolverr_solution(
                    stage_label="Step1 前",
                    target_url="https://accounts.x.ai/sign-up?redirect=grok-com",
                )

        self.assertEqual(session.post.call_count, 3)

    def test_reuse_turnstile_waits_for_token_before_native_follow_up(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=False,
        )
        page = mock.Mock()
        frame = mock.Mock()
        frame.locator.return_value.click.return_value = None
        page.mouse.move.return_value = None
        page.mouse.down.return_value = None
        page.mouse.up.return_value = None
        box = {"x": 158.0, "y": 659.0, "width": 384.0, "height": 65.0}

        with mock.patch.object(
            reg,
            "_wait_turnstile_token",
            side_effect=["", "page-token-12345678901234567890"],
        ):
            with mock.patch.object(
                reg,
                "_find_turnstile_widget",
                return_value=(frame, box),
            ):
                with mock.patch.object(
                    reg,
                    "_kick_turnstile_widget",
                    return_value="response-ancestor-click:1",
                ):
                    with mock.patch.object(reg, "_native_click_turnstile") as native_mock:
                        token, error = reg._reuse_turnstile_on_current_page(page)

        self.assertEqual(token, "page-token-12345678901234567890")
        self.assertEqual(error, "")
        native_mock.assert_not_called()
        frame.locator.assert_called_once_with("body")

    def test_restore_profile_page_after_flaresolverr_reload_resubmits_email_and_otp(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()

        with mock.patch.object(reg, "_detect_blocked_signup_page", return_value=""):
            with mock.patch.object(
                reg,
                "_page_has_profile_form",
                side_effect=[False, False, True],
            ):
                with mock.patch.object(
                    reg,
                    "_signup_gate_state",
                    return_value="email_input",
                ):
                    with mock.patch.object(
                        reg,
                        "_page_has_email_input",
                        side_effect=[True, False],
                    ):
                        with mock.patch.object(
                            reg,
                            "_page_has_otp_form",
                            side_effect=[True],
                        ):
                            with mock.patch.object(reg, "_submit_email") as submit_email_mock:
                                with mock.patch.object(reg, "_submit_otp") as submit_otp_mock:
                                    with mock.patch.object(reg, "_page_wait"):
                                        reg._restore_profile_page_after_flaresolverr_reload(
                                            page,
                                            "demo@example.com",
                                            "ABC123",
                                        )

        submit_email_mock.assert_called_once_with(page, "demo@example.com")
        submit_otp_mock.assert_called_once_with(page, "ABC123")

    def test_restore_profile_page_after_flaresolverr_reload_reenters_email_signup(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()

        with mock.patch.object(reg, "_detect_blocked_signup_page", return_value=""):
            with mock.patch.object(
                reg,
                "_page_has_profile_form",
                side_effect=[False, True],
            ):
                with mock.patch.object(
                    reg,
                    "_signup_gate_state",
                    return_value="email_button",
                ):
                    with mock.patch.object(
                        reg, "_ensure_email_signup_form"
                    ) as ensure_mock:
                        with mock.patch.object(reg, "_page_wait"):
                            reg._restore_profile_page_after_flaresolverr_reload(
                                page,
                                "demo@example.com",
                                "ABC123",
                            )

        ensure_mock.assert_called_once_with(
            page,
            timeout=12,
            stage_label="Step5 前预热后",
        )

    def test_install_turnstile_patch_adds_init_script(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        context = mock.Mock()

        reg._install_turnstile_patch(context)

        script = context.add_init_script.call_args.args[0]
        self.assertIn("MouseEvent.prototype", script)
        self.assertIn("screenX", script)
        self.assertIn("screenY", script)

    def test_click_turnstile_shadow_checkbox_searches_shadow_hosts(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        frame = mock.Mock()
        frame.evaluate.return_value = "frame-shadow-host-target"

        action = reg._click_turnstile_shadow_checkbox(frame)

        self.assertEqual(action, "frame-shadow-host-target")

    def test_wait_for_auth_cookies_retries_submit_on_final_form(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.context.cookies.side_effect = [
            [],
            [],
            [{"name": "sso", "value": "token-123"}],
        ]

        with mock.patch.object(reg, "_page_has_profile_form", side_effect=[True, False]):
            with mock.patch.object(reg, "_click_register_submit", return_value="clicked") as click_mock:
                with mock.patch.object(reg, "_page_wait") as wait_mock:
                    cookies = reg._wait_for_auth_cookies(page, timeout=5)

        self.assertEqual(cookies[0]["name"], "sso")
        click_mock.assert_called_once_with(page)
        wait_mock.assert_called()

    def test_has_transient_retry_error_detects_xai_toast(self):
        page = mock.Mock()
        with mock.patch(
            "platforms.grok.core._safe_body_text",
            return_value="出了点问题，请重试",
        ):
            self.assertTrue(GrokRegister._has_transient_retry_error(page))
        with mock.patch(
            "platforms.grok.core._safe_body_text",
            return_value="Something went wrong. Please try again.",
        ):
            self.assertTrue(GrokRegister._has_transient_retry_error(page))
        with mock.patch(
            "platforms.grok.core._safe_body_text",
            return_value="Cloudflare verification failed, try again",
        ):
            self.assertFalse(GrokRegister._has_transient_retry_error(page))

    def test_submit_register_retries_immediately_on_transient_toast(self):
        logs: list[str] = []
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=logs.append,
            headless=True,
        )
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-up"

        with mock.patch.object(reg, "_register_turnstile_ready", return_value=(True, 40)):
            with mock.patch.object(reg, "_has_turnstile_error", return_value=False):
                with mock.patch.object(
                    reg, "_click_register_submit", return_value="clicked"
                ) as click_mock:
                    with mock.patch.object(
                        reg,
                        "_wait_register_submit_outcome",
                        side_effect=["transient_error", "success"],
                    ) as outcome_mock:
                        with mock.patch.object(reg, "_refresh_castle_before_submit") as castle_mock:
                            with mock.patch.object(reg, "_page_wait"):
                                reg._submit_register(page)

        self.assertEqual(click_mock.call_count, 2)
        self.assertEqual(outcome_mock.call_count, 2)
        castle_mock.assert_called_once_with(page)
        self.assertTrue(reg._register_submit_meta["saw_transient_error"])
        self.assertEqual(reg._register_submit_meta["transient_error_retries"], 1)
        self.assertEqual(reg._register_submit_meta["submit_attempts"], 2)
        self.assertTrue(any("出了点问题" in line or "请重试" in line for line in logs))

    def test_fill_user_form_passes_single_payload_argument(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        page.evaluate.return_value = "filled"

        with mock.patch.object(reg, "_page_wait"):
            reg._fill_user_form(page, "Demo", "User", "Pass!Aa1")

        evaluate_args = page.evaluate.call_args.args
        self.assertEqual(len(evaluate_args), 2)
        self.assertEqual(
            evaluate_args[1],
            {
                "given_name": "Demo",
                "family_name": "User",
                "password": "Pass!Aa1",
            },
        )

    def test_solve_turnstile_on_page_prefers_page_click_before_solver_in_headless(self):
        reg = GrokRegister(
            captcha_solver=mock.Mock(),
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()
        with mock.patch.object(reg, "_wait_turnstile_token", return_value=""):
            with mock.patch.object(reg, "_wait_until", return_value=None):
                with mock.patch.object(
                    reg,
                    "_reuse_turnstile_on_current_page",
                    return_value=("page-token-12345678901234567890", ""),
                ):
                    with mock.patch.object(
                        reg, "_solve_turnstile_by_same_session_solver"
                    ) as same_session_mock:
                        with mock.patch.object(
                            reg, "_solve_turnstile_by_solver"
                        ) as solver_mock:
                            token = reg._solve_turnstile_on_page(page)

        self.assertEqual(token, "page-token-12345678901234567890")
        same_session_mock.assert_not_called()
        solver_mock.assert_not_called()

    def test_solve_turnstile_on_page_uses_solver_as_nonfatal_final_fallback(self):
        reg = GrokRegister(
            captcha_solver=mock.Mock(),
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()

        with mock.patch.object(reg, "_wait_turnstile_token", return_value=""):
            with mock.patch.object(reg, "_wait_until", return_value=None):
                with mock.patch.object(
                    reg,
                    "_reuse_turnstile_on_current_page",
                    return_value=("", "same-page-stalled"),
                ):
                    with mock.patch.object(
                        reg,
                        "_solve_turnstile_by_flaresolverr",
                        return_value="",
                    ):
                        with mock.patch.object(
                            reg, "_solve_turnstile_by_same_session_solver", return_value=""
                        ) as same_session_mock:
                            with mock.patch.object(
                                reg,
                                "_solve_turnstile_by_solver",
                                return_value="solver-token-12345678901234567890",
                            ) as solver_mock:
                                token = reg._solve_turnstile_on_page(page)

        self.assertEqual(token, "solver-token-12345678901234567890")
        same_session_mock.assert_called_once_with(page)
        solver_mock.assert_called_once_with(page)

    def test_solve_turnstile_on_page_uses_flaresolverr_before_same_session_solver(self):
        reg = GrokRegister(
            captcha_solver=mock.Mock(),
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()

        with mock.patch.object(reg, "_wait_turnstile_token", return_value=""):
            with mock.patch.object(reg, "_wait_until", return_value=None):
                with mock.patch.object(
                    reg,
                    "_reuse_turnstile_on_current_page",
                    return_value=("", "same-page-stalled"),
                ):
                    with mock.patch.object(
                        reg,
                        "_solve_turnstile_by_flaresolverr",
                        return_value="flare-token-12345678901234567890",
                    ) as flaresolverr_mock:
                        with mock.patch.object(
                            reg, "_solve_turnstile_by_same_session_solver"
                        ) as same_session_mock:
                            with mock.patch.object(
                                reg, "_solve_turnstile_by_solver"
                            ) as solver_mock:
                                token = reg._solve_turnstile_on_page(page)

        self.assertEqual(token, "flare-token-12345678901234567890")
        flaresolverr_mock.assert_called_once_with(page)
        same_session_mock.assert_not_called()
        solver_mock.assert_not_called()

    def test_solve_turnstile_on_page_uses_same_session_solver_after_same_page_reuse_stalls(self):
        reg = GrokRegister(
            captcha_solver=mock.Mock(),
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        page = mock.Mock()

        with mock.patch.object(reg, "_wait_turnstile_token", return_value=""):
            with mock.patch.object(reg, "_wait_until", return_value=None):
                with mock.patch.object(
                    reg,
                    "_reuse_turnstile_on_current_page",
                    return_value=(
                        "",
                        "Turnstile 页面状态未变化，当前 x.ai 验证未被同页复用链路推进",
                    ),
                ):
                    with mock.patch.object(reg, "_solve_turnstile_by_solver") as solver_mock:
                        with mock.patch.object(
                            reg,
                            "_solve_turnstile_by_flaresolverr",
                            return_value="",
                        ):
                            with mock.patch.object(
                                reg,
                            "_solve_turnstile_by_same_session_solver",
                            return_value="session-token-12345678901234567890",
                            ) as same_session_mock:
                                token = reg._solve_turnstile_on_page(page)

        self.assertEqual(token, "session-token-12345678901234567890")
        solver_mock.assert_not_called()
        same_session_mock.assert_called_once_with(page)

    def test_register_prewarms_before_signup_only(self):
        reg = GrokRegister(
            captcha_solver=None,
            proxy=None,
            log_fn=lambda *_: None,
            headless=True,
        )
        context = mock.Mock()
        context.cookies.return_value = [{"name": "sso", "value": "token-123"}]
        browser = mock.Mock()
        browser.new_context.return_value = context
        page = mock.Mock()
        page.url = "https://accounts.x.ai/sign-up?redirect=grok-com"
        context.new_page.return_value = page

        with mock.patch.object(reg, "_launch_browser", return_value=(mock.Mock(), browser)):
            with mock.patch.object(reg, "_prewarm_before_signup") as before_signup_mock:
                with mock.patch.object(reg, "_goto_email_signup"):
                    with mock.patch.object(reg, "_submit_email"):
                        with mock.patch.object(reg, "_submit_otp"):
                            with mock.patch.object(reg, "_fill_user_form"):
                                with mock.patch.object(
                                    reg,
                                    "_solve_turnstile_on_page",
                                    return_value="token-12345678901234567890",
                                ):
                                    with mock.patch.object(reg, "_submit_register"):
                                        with mock.patch.object(reg, "_accept_tos_if_needed"):
                                            with mock.patch.object(
                                                reg,
                                                "_has_auth_cookies",
                                                return_value=True,
                                            ):
                                                with mock.patch.object(
                                                    reg,
                                                    "_pick_cookie",
                                                    side_effect=["sso-token", "sso-rw-token"],
                                                ):
                                                    result = reg.register(
                                                        email="demo@example.com",
                                                        password="Pass!Aa1",
                                                        otp_callback=lambda: "ABC123",
                                                    )

        before_signup_mock.assert_called_once_with(page)
        self.assertEqual(result["sso"], "sso-token")


if __name__ == "__main__":
    unittest.main()
