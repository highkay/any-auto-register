import json
import unittest
from unittest import mock

from platforms.zai.core import ZAI_SIGNUP_URL, ZaiRegister


class ZaiCoreTests(unittest.TestCase):
    def test_register_solves_aliyun_task_before_submitting_signup(self):
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=lambda *_args, **_kwargs: None)

        fake_playwright = mock.Mock()
        fake_browser = mock.Mock()
        fake_context = mock.Mock()
        fake_page = mock.Mock()
        fake_page.url = ZAI_SIGNUP_URL

        fake_context.new_page.return_value = fake_page

        with (
            mock.patch.object(reg, "_launch_browser", return_value=(fake_playwright, fake_browser)),
            mock.patch.object(reg, "_new_context", return_value=fake_context),
            mock.patch.object(reg, "_install_response_capture"),
            mock.patch.object(reg, "_open_signup_page"),
            mock.patch.object(reg, "_fill_signup_form"),
            mock.patch.object(reg, "_solve_aliyun_task", return_value="captcha-param") as solve_aliyun_task_mock,
            mock.patch.object(reg, "_submit_signup") as submit_signup_mock,
            mock.patch.object(reg, "_wait_for_verify_page", return_value="demo-user"),
            mock.patch.object(reg, "_open_verify_link"),
            mock.patch.object(
                reg,
                "_finish_signup",
                return_value={
                    "token": "demo-token",
                    "token_type": "Bearer",
                    "profile_image_url": None,
                    "user": {"id": "demo-user-id"},
                },
            ),
            mock.patch.object(
                reg,
                "_fetch_current_user",
                return_value={"id": "demo-user-id", "email": "demo@example.com", "role": "user"},
            ),
        ):
            result = reg.register(
                email="demo@example.com",
                password="Aa1!demoPass",
                verification_link_callback=lambda: "https://chat.z.ai/auth/verify_email?token=demo&email=demo%40example.com&username=demo-user",
            )

        self.assertEqual(result["captcha_verify_param"], "captcha-param")
        solve_aliyun_task_mock.assert_called_once_with(
            fake_page,
            response_store={},
        )
        submit_signup_mock.assert_called_once()
        self.assertEqual(submit_signup_mock.call_args.kwargs["captcha_verify_param"], "captcha-param")
        fake_context.close.assert_called_once()
        fake_browser.close.assert_called_once()
        fake_playwright.stop.assert_called_once()

    def test_register_does_not_submit_when_captcha_payload_missing(self):
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=lambda *_args, **_kwargs: None)

        fake_playwright = mock.Mock()
        fake_browser = mock.Mock()
        fake_context = mock.Mock()
        fake_page = mock.Mock()
        fake_page.url = ZAI_SIGNUP_URL
        fake_context.new_page.return_value = fake_page

        with (
            mock.patch.object(reg, "_launch_browser", return_value=(fake_playwright, fake_browser)),
            mock.patch.object(reg, "_new_context", return_value=fake_context),
            mock.patch.object(reg, "_install_response_capture"),
            mock.patch.object(reg, "_open_signup_page"),
            mock.patch.object(reg, "_fill_signup_form"),
            mock.patch.object(reg, "_solve_aliyun_task", return_value=""),
            mock.patch.object(reg, "_submit_signup") as submit_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "captchaVerifyParam"):
                reg.register(
                    email="demo@example.com",
                    password="Aa1!demoPass",
                    verification_link_callback=lambda: "",
                )

        submit_mock.assert_not_called()

    def test_register_rejects_guest_session_after_finish_signup(self):
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=lambda *_args, **_kwargs: None)

        fake_playwright = mock.Mock()
        fake_browser = mock.Mock()
        fake_context = mock.Mock()
        fake_page = mock.Mock()
        fake_page.url = ZAI_SIGNUP_URL
        fake_context.new_page.return_value = fake_page

        with (
            mock.patch.object(reg, "_launch_browser", return_value=(fake_playwright, fake_browser)),
            mock.patch.object(reg, "_new_context", return_value=fake_context),
            mock.patch.object(reg, "_install_response_capture"),
            mock.patch.object(reg, "_open_signup_page"),
            mock.patch.object(reg, "_fill_signup_form"),
            mock.patch.object(reg, "_solve_aliyun_task", return_value="captcha-param"),
            mock.patch.object(reg, "_submit_signup"),
            mock.patch.object(reg, "_wait_for_verify_page", return_value="demo-user"),
            mock.patch.object(reg, "_open_verify_link"),
            mock.patch.object(reg, "_finish_signup", return_value={"token": "guest-token"}),
            mock.patch.object(
                reg,
                "_fetch_current_user",
                return_value={"id": "guest-id", "email": "guest-1@guest.com", "role": "guest"},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "guest 会话"):
                reg.register(
                    email="demo@example.com",
                    password="Aa1!demoPass",
                    verification_link_callback=lambda: "https://chat.z.ai/auth/verify_email?token=demo&email=demo%40example.com&username=demo-user",
                )

    def test_finish_signup_fills_all_password_inputs_and_prefers_finish_signup_token(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        fake_page.url = "https://chat.z.ai/auth/verify_email?token=demo"
        first_password = mock.Mock()
        second_password = mock.Mock()
        password_inputs = mock.Mock()
        password_inputs.count.return_value = 2
        password_inputs.nth.side_effect = lambda index: [first_password, second_password][index]
        fake_page.locator.return_value = password_inputs
        response_store: dict = {}

        def wait_until_side_effect(_fn, *args, **kwargs):
            desc = kwargs.get("desc", "")
            if desc == "等待 Z.ai finish_signup 结果超时":
                response_store["finish_signup"] = {
                    "json": {
                        "success": True,
                        "user": {
                            "token": "real-token",
                            "profile_image_url": "https://img.example/avatar.png",
                        },
                    }
                }
            return None

        with (
            mock.patch.object(reg, "_wait_until", side_effect=wait_until_side_effect),
            mock.patch.object(reg, "_click_text_button", return_value=True),
            mock.patch.object(reg, "_extract_auth_token", return_value="guest-token"),
        ):
            result = reg._finish_signup(fake_page, password="Aa1!demoPass", response_store=response_store)

        first_password.fill.assert_called_once_with("Aa1!demoPass")
        second_password.fill.assert_called_once_with("Aa1!demoPass")
        self.assertEqual(result["token"], "real-token")
        self.assertEqual(result["profile_image_url"], "https://img.example/avatar.png")

    def test_open_signup_page_switches_from_phone_login_to_email_signup(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()

        with (
            mock.patch.object(reg, "_has_signup_form", side_effect=[False]) as has_form_mock,
            mock.patch.object(reg, "_click_text_button", return_value=True) as click_mock,
            mock.patch.object(reg, "_wait_until") as wait_mock,
        ):
            reg._open_signup_page(fake_page)

        fake_page.goto.assert_called_once_with(
            ZAI_SIGNUP_URL,
            wait_until="domcontentloaded",
            timeout=120000,
        )
        fake_page.evaluate.assert_called_once()
        click_mock.assert_called_once()
        fake_page.wait_for_timeout.assert_any_call(3500)
        fake_page.wait_for_timeout.assert_any_call(1500)
        has_form_mock.assert_called_once_with(fake_page)
        wait_mock.assert_called_once()

    def test_solve_aliyun_task_uses_same_page_session(self):
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()

        with (
            mock.patch.object(reg, "_open_aliyun_challenge") as open_mock,
            mock.patch.object(reg, "_solve_aliyun_slide", return_value="captcha-param") as solve_slide_mock,
        ):
            result = reg._solve_aliyun_task(fake_page)

        self.assertEqual(result, "captcha-param")
        open_mock.assert_called_once_with(fake_page)
        solve_slide_mock.assert_called_once_with(fake_page, response_store=None)

    def test_extract_aliyun_payload_falls_back_to_verify_request_capture(self):
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        fake_page.evaluate.return_value = {
            "found": False,
            "source": None,
            "value": None,
            "debug": {"payloadCount": 0, "configCount": 0, "submissionsBlocked": 0},
        }
        response_store = {
            "aliyun_verify": {
                "request_post_data": (
                    "Action=VerifyCaptchaV3&CaptchaVerifyParam="
                    "%7B%22sceneId%22%3A%2236qgs6xb%22%2C%22certifyId%22%3A%22WCU0oua9xE%22%2C"
                    "%22deviceToken%22%3A%22token-123%22%7D"
                ),
                "json": {
                    "Result": {
                        "VerifyResult": True,
                        "VerifyCode": "T001",
                        "certifyId": "WCU0oua9xE",
                    }
                },
            }
        }

        result = reg._extract_aliyun_payload(fake_page, response_store=response_store)

        self.assertEqual(
            result,
            {
                "sceneId": "36qgs6xb",
                "certifyId": "WCU0oua9xE",
                "deviceToken": "token-123",
            },
        )

    def test_recognize_slide_action_prefers_aliyun_slide_action_api(self):
        solver = mock.Mock()
        solver.solve_aliyun_slide_action.return_value = {
            "action": "slide",
            "slider": {"x": 30, "y": 870},
            "gap": {"x": 780, "y": 130},
            "imageSize": {"width": 1440, "height": 900},
            "coordinateSpace": "resized_image",
            "gapSource": "image_estimator",
        }
        reg = ZaiRegister(captcha_solver=solver, log_fn=lambda *_args, **_kwargs: None)

        result = reg._recognize_slide_action(
            "screenshot-b64",
            question="请拖动滑块完成拼图",
            background_b64="background-b64",
            piece_b64="piece-b64",
        )

        self.assertEqual(result["gapSource"], "image_estimator")
        solver.solve_aliyun_slide_action.assert_called_once_with(
            "screenshot-b64",
            question="请拖动滑块完成拼图",
            background="background-b64",
            piece="piece-b64",
            timeout_s=45.0,
            project_name="any-auto-register:zai",
            schema_mode="slide",
        )

    def test_solve_aliyun_slide_does_not_click_action_button_after_drag(self):
        logs: list[str] = []
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=logs.append)
        fake_page = mock.Mock()
        fake_page.locator.return_value.first = mock.Mock()

        with (
            mock.patch.object(reg, "_slide_action_bbox", return_value={"x": 530.0, "y": 510.0, "width": 340.0, "height": 260.0}),
            mock.patch.object(
                reg,
                "_locator_bbox",
                side_effect=[
                    {"x": 560.0, "y": 742.0, "width": 60.0, "height": 33.0},
                    {"x": 530.0, "y": 540.0, "width": 340.0, "height": 200.0},
                ],
            ),
            mock.patch.object(reg, "_screenshot_clip_with_hidden", side_effect=[b"slide", b"background"]),
            mock.patch.object(reg, "_locator_screenshot", return_value=b"piece"),
            mock.patch.object(
                reg,
                "_recognize_slide_action",
                return_value={
                    "action": "slide",
                    "slider": {"x": 80, "y": 826},
                    "gap": {"x": 980, "y": 420},
                    "imageSize": {"width": 1440, "height": 900},
                    "coordinateSpace": "resized_image",
                    "gapSource": "llm",
                },
            ),
            mock.patch.object(reg, "_challenge_question", return_value="请拖动滑块完成拼图"),
            mock.patch.object(reg, "_resolve_slide_end_x", return_value=774.583),
            mock.patch.object(reg, "_drag_slider") as drag_mock,
            mock.patch.object(reg, "_sleep_with_checkpoint"),
            mock.patch.object(reg, "_wait_for_aliyun_payload", return_value="captcha-param"),
            mock.patch.object(reg, "_click_aliyun_action_button") as click_mock,
        ):
            result = reg._solve_aliyun_slide(fake_page)

        self.assertEqual(result, "captcha-param")
        drag_mock.assert_called_once()
        click_mock.assert_not_called()
        trace_line = next(line for line in logs if line.startswith("Z.ai Aliyun action trace "))
        trace = json.loads(trace_line.split("trace ", 1)[1])
        self.assertEqual(trace["coordinateSpace"], "resized_image")
        self.assertEqual(trace["gapSource"], "llm")
        self.assertEqual(trace["mappedDrag"]["startX"], 590.0)
        self.assertEqual(trace["mappedDrag"]["endX"], 774.58)
        self.assertEqual(trace["backgroundSize"], None)
        self.assertEqual(trace["pieceSize"], None)

    def test_install_response_capture_records_aliyun_network_trace(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        store: dict = {}

        reg._install_response_capture(fake_page, store)
        callback = fake_page.on.call_args.args[1]

        response = mock.Mock()
        response.url = "https://captcha-open.aliyuncs.com/captcha/open/verify"
        response.status = 200
        response.request.method = "POST"
        response.request.post_data = (
            'Action=VerifyCaptchaV3&CaptchaVerifyParam={"sceneId":"scene-1","token":"token-1"}'
        )
        response.json.return_value = {
            "Result": {
                "VerifyResult": False,
                "VerifyCode": "F015",
            }
        }

        callback(response)

        self.assertEqual(store["aliyun_verify"]["status"], 200)
        trace = store["aliyun_requests"][-1]
        self.assertEqual(trace["host"], "captcha-open.aliyuncs.com")
        self.assertTrue(trace["hasCaptchaVerifyParam"])
        self.assertEqual(trace["captchaVerifyParamType"], "dict")
        self.assertEqual(trace["captchaVerifyParamKeys"], ["sceneId", "token"])
        self.assertEqual(trace["verifyResult"], False)
        self.assertEqual(trace["verifyCode"], "F015")

    def test_wait_for_aliyun_payload_failure_includes_runtime_and_network_trace(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        response_store = {
            "aliyun_requests": [
                {
                    "method": "POST",
                    "status": 200,
                    "host": "captcha-open.aliyuncs.com",
                    "path": "/captcha/open/verify",
                    "action": "VerifyCaptchaV3",
                    "hasCaptchaVerifyParam": True,
                    "verifyResult": False,
                    "verifyCode": "F015",
                }
            ]
        }

        with (
            mock.patch.object(reg, "_extract_aliyun_payload", return_value=None),
            mock.patch.object(reg, "_latest_aliyun_error", return_value=None),
            mock.patch.object(
                reg,
                "_get_aliyun_debug_state",
                return_value={
                    "url": "https://chat.z.ai/auth",
                    "title": "Z.ai",
                    "hookInstalled": True,
                    "hasInitAliyunCaptcha": False,
                    "hasInstance": False,
                    "payloadCount": 0,
                    "errors": [],
                    "events": [],
                    "iframeCount": 1,
                    "iframes": [{"src": "https://captcha-open.aliyuncs.com/"}],
                    "elements": {"wrapper": {"exists": True}},
                },
            ),
            mock.patch.object(reg, "_sleep_with_checkpoint"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                reg._wait_for_aliyun_payload(
                    fake_page,
                    timeout=0.01,
                    response_store=response_store,
                )

        message = str(ctx.exception)
        self.assertIn("hasInitAliyunCaptcha", message)
        self.assertIn("aliyunRequests", message)
        self.assertIn("F015", message)

    def test_closed_loop_drag_step_uses_precise_small_adjustments(self):
        self.assertEqual(ZaiRegister._closed_loop_drag_step(2.5), 2.5)
        self.assertEqual(ZaiRegister._closed_loop_drag_step(-3.25), -3.25)
        self.assertAlmostEqual(ZaiRegister._closed_loop_drag_step(10.0), 5.5)
        self.assertAlmostEqual(ZaiRegister._closed_loop_drag_step(-18.0), -9.9)

    def test_finalize_drag_release_keeps_horizontal_center(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        fake_page.mouse = mock.Mock()

        reg._finalize_drag_release(fake_page, current_x=812.5, current_y=758.5)

        self.assertEqual(
            fake_page.mouse.move.call_args_list,
            [
                mock.call(812.5, 758.75),
                mock.call(812.5, 758.5),
            ],
        )
        self.assertEqual(
            fake_page.wait_for_timeout.call_args_list,
            [mock.call(80), mock.call(120)],
        )

    def test_restore_aliyun_slide_after_refresh_reopens_when_wait_times_out(self):
        reg = ZaiRegister(captcha_solver=mock.Mock(), log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()

        with (
            mock.patch.object(
                reg,
                "_wait_for_aliyun_slide_ready",
                side_effect=TimeoutError("refresh timeout"),
            ) as wait_mock,
            mock.patch.object(reg, "_open_aliyun_challenge") as open_mock,
        ):
            reg._restore_aliyun_slide_after_refresh(fake_page)

        wait_mock.assert_called_once()
        open_mock.assert_called_once_with(fake_page)

    def test_submit_signup_posts_captcha_verify_param_directly(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        fake_page.evaluate.return_value = {
            "ok": True,
            "status": 200,
            "text": '{"success":true}',
            "json": {"success": True},
        }
        response_store: dict = {}

        with mock.patch.object(reg, "_click_text_button", return_value=False):
            result = reg._submit_signup(
                fake_page,
                response_store,
                name="demo-user",
                email="demo@example.com",
                password="Aa1!demoPass",
                captcha_verify_param={"sceneId": "scene-1", "token": "token-1"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(response_store["signup"]["json"]["success"], True)
        payload = fake_page.evaluate.call_args.args[1]
        self.assertEqual(payload["name"], "demo-user")
        self.assertEqual(payload["email"], "demo@example.com")
        self.assertEqual(payload["password"], "Aa1!demoPass")
        self.assertEqual(payload["profile_image_url"], "")
        self.assertEqual(
            payload["captcha_verify_param"],
            '{"sceneId":"scene-1","token":"token-1"}',
        )
        fake_page.goto.assert_called_once_with(
            "https://chat.z.ai/auth/verify?email=demo%40example.com&username=demo-user",
            wait_until="domcontentloaded",
            timeout=120000,
        )

    def test_submit_signup_failure_includes_captcha_summary(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        fake_page.evaluate.return_value = {
            "ok": False,
            "status": 400,
            "text": '{"detail":"The captcha verification failed."}',
            "json": {"detail": "The captcha verification failed."},
        }
        response_store: dict = {}

        with mock.patch.object(reg, "_click_text_button", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                reg._submit_signup(
                    fake_page,
                    response_store,
                    name="demo-user",
                    email="demo@example.com",
                    password="Aa1!demoPass",
                    captcha_verify_param={
                        "sceneId": "scene-1",
                        "certifyId": "cert-1",
                        "token": "token-1",
                    },
                )

        message = str(ctx.exception)
        self.assertIn("The captcha verification failed.", message)
        self.assertIn("captcha_summary=", message)
        self.assertIn("'type': 'str'", message)
        self.assertIn("'length': 60", message)
        self.assertEqual(
            response_store["signup_captcha_summary"],
            {
                "type": "str",
                "length": 60,
            },
        )

    def test_submit_signup_prefers_native_button_when_available(self):
        reg = ZaiRegister(log_fn=lambda *_args, **_kwargs: None)
        fake_page = mock.Mock()
        fake_page.url = "https://chat.z.ai/auth/verify?email=demo%40example.com&username=demo-user"
        response_store: dict = {}

        def wait_until_side_effect(fn, *args, **kwargs):
            response_store["signup"] = {
                "url": "https://chat.z.ai/api/v1/auths/signup",
                "status": 200,
                "json": {"success": True},
            }
            return None

        with (
            mock.patch.object(reg, "_click_text_button", return_value=True) as click_mock,
            mock.patch.object(reg, "_wait_until", side_effect=wait_until_side_effect) as wait_mock,
        ):
            result = reg._submit_signup(
                fake_page,
                response_store,
                name="demo-user",
                email="demo@example.com",
                password="Aa1!demoPass",
                captcha_verify_param={"sceneId": "scene-1", "token": "token-1"},
            )

        self.assertEqual(result["json"]["success"], True)
        click_mock.assert_called_once()
        wait_mock.assert_called_once()
        fake_page.evaluate.assert_not_called()
        fake_page.goto.assert_not_called()
