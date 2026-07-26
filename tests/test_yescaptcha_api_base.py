import unittest
from unittest import mock
import os

from core.base_captcha import YesCaptcha
from core.base_platform import BasePlatform, RegisterConfig


class DummyPlatform(BasePlatform):
    name = "dummy"
    display_name = "Dummy"

    def register(self, email: str, password: str = None):
        raise NotImplementedError

    def check_valid(self, account):
        return True


class YesCaptchaApiBaseTests(unittest.TestCase):
    def test_yescaptcha_uses_custom_api_base(self):
        solver = YesCaptcha("client-key", api_base="http://192.168.1.18:38000")
        self.assertEqual(solver.api, "http://192.168.1.18:38000")

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("requests.get")
    def test_yescaptcha_defaults_to_local_ohmycaptcha_when_healthy(self, get_mock):
        resp = mock.Mock()
        resp.text = '{"status":"ok"}'
        resp.json.return_value = {"status": "ok"}
        resp.raise_for_status.return_value = None
        get_mock.return_value = resp

        solver = YesCaptcha("client-key")

        self.assertEqual(solver.api, "http://127.0.0.1:38010")
        get_mock.assert_called_once_with("http://127.0.0.1:38010/api/v1/health", timeout=0.8)

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("requests.get", side_effect=RuntimeError("down"))
    def test_yescaptcha_defaults_to_remote_when_local_ohmycaptcha_unavailable(self, get_mock):
        solver = YesCaptcha("client-key")

        self.assertEqual(solver.api, "https://api.yescaptcha.com")
        get_mock.assert_called_once()

    def test_base_platform_make_captcha_passes_custom_api_base(self):
        platform = DummyPlatform(
            RegisterConfig(
                captcha_solver="yescaptcha",
                extra={
                    "yescaptcha_key": "client-key",
                    "yescaptcha_api_base": "http://192.168.1.18:38000/",
                },
            )
        )

        solver = platform._make_captcha()

        self.assertIsInstance(solver, YesCaptcha)
        self.assertEqual(solver.client_key, "client-key")
        self.assertEqual(solver.api, "http://192.168.1.18:38000")

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("requests.get")
    def test_base_platform_make_captcha_uses_local_ohmycaptcha_when_api_base_missing(self, get_mock):
        resp = mock.Mock()
        resp.text = '{"status":"ok"}'
        resp.json.return_value = {"status": "ok"}
        resp.raise_for_status.return_value = None
        get_mock.return_value = resp
        platform = DummyPlatform(
            RegisterConfig(
                captcha_solver="yescaptcha",
                extra={"yescaptcha_key": "client-key"},
            )
        )

        solver = platform._make_captcha()

        self.assertEqual(solver.api, "http://127.0.0.1:38010")

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_classify_hcaptcha_uses_custom_api_base(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-123"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {"answer": [5, 7, 8]},
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://192.168.1.18:38000")
        answer = solver.classify_hcaptcha("请选择所有人造物体", ["img-a", "img-b"])

        self.assertEqual(answer, [5, 7, 8])
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(
            post_mock.call_args_list[0].args[0],
            "http://192.168.1.18:38000/createTask",
        )
        self.assertEqual(
            post_mock.call_args_list[1].args[0],
            "http://192.168.1.18:38000/getTaskResult",
        )
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["clientKey"], "client-key")
        self.assertEqual(create_payload["task"]["type"], "HCaptchaClassification")
        self.assertEqual(create_payload["task"]["question"], "请选择所有人造物体")
        self.assertEqual(create_payload["task"]["images"], ["img-a", "img-b"])

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_image_passes_prompt_to_custom_api_base(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-visual-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {"text": '{"action":"click","clicks":[{"x":10,"y":20}]}'},
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        text = solver.solve_image("img-b64", prompt="找出所有需要插头才能工作的物品")

        self.assertEqual(text, '{"action":"click","clicks":[{"x":10,"y":20}]}')
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(
            post_mock.call_args_list[0].args[0],
            "http://127.0.0.1:38010/createTask",
        )
        self.assertEqual(create_payload["task"]["type"], "ImageToTextTask")
        self.assertEqual(create_payload["task"]["body"], "img-b64")
        self.assertEqual(
            create_payload["task"]["question"],
            "找出所有需要插头才能工作的物品",
        )

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_image_passes_schema_controls(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-visual-2"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {"text": '{"action":"slide","slider":{"x":1,"y":2},"gap":{"x":3,"y":4}}'},
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        text = solver.solve_image(
            "img-b64",
            prompt="请拖动滑块完成拼图",
            schema_mode="slide",
            timeout_s=45.0,
            model_candidates=["gpt-5.4", "qwen3-vl:235b-instruct"],
        )

        self.assertEqual(text, '{"action":"slide","slider":{"x":1,"y":2},"gap":{"x":3,"y":4}}')
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["task"]["schema_mode"], "slide")
        self.assertEqual(create_payload["task"]["timeout_s"], 45.0)
        self.assertEqual(
            create_payload["task"]["model_candidates"],
            ["gpt-5.4", "qwen3-vl:235b-instruct"],
        )

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_turnstile_session_uses_documented_task_payload(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-turnstile-session-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {
                "token": "0.session-token",
                "solverMode": "session_restore",
                "tokenSource": "cf-turnstile-response",
                "finalURL": "https://accounts.x.ai/sign-up?redirect=grok-com",
                "attempts": 1,
                "restoredCookieCount": 14,
                "restoredOriginCount": 1,
            },
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        solution = solver.solve_turnstile_session(
            "https://accounts.x.ai/sign-up?redirect=grok-com",
            "0x4AAAAAAAhr9JGVDZbrZOo0",
            session_state={
                "cookies": [
                    {
                        "name": "cf_clearance",
                        "value": "cookie-value",
                        "domain": "accounts.x.ai",
                        "path": "/",
                    }
                ],
                "origins": [
                    {
                        "origin": "https://accounts.x.ai",
                        "localStorage": {"flow": "signup"},
                        "sessionStorage": {"step": "5"},
                    }
                ],
                "userAgent": "Mozilla/5.0",
                "viewport": {"width": 1400, "height": 1200},
                "locale": "zh-CN",
                "timezoneId": "Asia/Shanghai",
            },
            widget_hints={
                "responseInputSelector": "input[name=\"cf-turnstile-response\"]",
                "frameUrl": "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/...",
                "widgetBox": {"x": 158, "y": 659, "width": 384, "height": 65},
            },
            runtime_hints={
                "pageBodyText": "您正在登录 完成注册",
                "stepLabel": "grok_signup_step5",
                "tokenMinLength": 20,
            },
            options={
                "pageLoadTimeoutMs": 30000,
                "solveTimeoutMs": 90000,
                "maxAttempts": 2,
            },
            browser_proxy={
                "server": "socks5://127.0.0.1:7890",
            },
        )

        self.assertEqual(solution["solverMode"], "session_restore")
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(
            create_payload["task"]["type"],
            "TurnstileTaskSessionProxyless",
        )
        self.assertEqual(
            create_payload["task"]["websiteURL"],
            "https://accounts.x.ai/sign-up?redirect=grok-com",
        )
        self.assertEqual(
            create_payload["task"]["websiteKey"],
            "0x4AAAAAAAhr9JGVDZbrZOo0",
        )
        self.assertEqual(
            create_payload["task"]["sessionState"]["origins"][0]["sessionStorage"]["step"],
            "5",
        )
        self.assertEqual(
            create_payload["task"]["widgetHints"]["widgetBox"]["width"],
            384,
        )
        self.assertEqual(
            create_payload["task"]["runtimeHints"]["stepLabel"],
            "grok_signup_step5",
        )
        self.assertEqual(create_payload["task"]["options"]["maxAttempts"], 2)
        self.assertEqual(
            create_payload["task"]["browserProxy"]["server"],
            "socks5://127.0.0.1:7890",
        )

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_aliyun_uses_documented_task_payload(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-aliyun-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {
                "token": "aliyun-token",
                "userAgent": "Mozilla/5.0",
                "captchaVerifyParam": {
                    "sceneId": "scene-1",
                    "certifyId": "cert-1",
                    "token": "aliyun-token",
                },
            },
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        solution = solver.solve_aliyun(
            "https://chat.z.ai/auth?redirect_uri=https://z.ai/&action=signup",
            captcha_selector="#aliyunCaptcha-captcha-wrapper",
            mode_hint="slide",
            callback_path="__APP_STATE__.captcha.verifyParam",
            project_name="any-auto-register:zai",
        )

        self.assertEqual(solution["captchaVerifyParam"]["sceneId"], "scene-1")
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["task"]["type"], "AliyunCaptchaTaskProxyless")
        self.assertEqual(
            create_payload["task"]["websiteURL"],
            "https://chat.z.ai/auth?redirect_uri=https://z.ai/&action=signup",
        )
        self.assertEqual(
            create_payload["task"]["captchaSelector"],
            "#aliyunCaptcha-captcha-wrapper",
        )
        self.assertEqual(create_payload["task"]["modeHint"], "slide")
        self.assertEqual(
            create_payload["task"]["callbackPath"],
            "__APP_STATE__.captcha.verifyParam",
        )
        self.assertEqual(create_payload["task"]["project_name"], "any-auto-register:zai")

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_aliyun_slide_action_uses_documented_task_payload(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-aliyun-slide-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {
                "captchaType": "slide",
                "action": "slide",
                "slider": {"x": 30, "y": 870},
                "gap": {"x": 780, "y": 130},
                "imageSize": {"width": 1440, "height": 900},
                "coordinateSpace": "resized_image",
                "gapSource": "image_estimator",
            },
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        solution = solver.solve_aliyun_slide_action(
            "img-b64",
            question="Return slider and gap coordinates for this Aliyun slider screenshot.",
            background="bg-b64",
            piece="piece-b64",
            timeout_s=45.0,
            model_candidates=["gpt-5.4"],
            project_name="any-auto-register:zai",
            schema_mode="slide",
        )

        self.assertEqual(solution["gapSource"], "image_estimator")
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["task"]["type"], "AliyunSlideActionTask")
        self.assertEqual(create_payload["task"]["body"], "img-b64")
        self.assertEqual(create_payload["task"]["background"], "bg-b64")
        self.assertEqual(create_payload["task"]["piece"], "piece-b64")
        self.assertEqual(
            create_payload["task"]["question"],
            "Return slider and gap coordinates for this Aliyun slider screenshot.",
        )
        self.assertEqual(create_payload["task"]["timeout_s"], 45.0)
        self.assertEqual(create_payload["task"]["model_candidates"], ["gpt-5.4"])
        self.assertEqual(create_payload["task"]["project_name"], "any-auto-register:zai")
        self.assertEqual(create_payload["task"]["schema_mode"], "slide")

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_aliyun_click_start_uses_documented_task_payload(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-aliyun-click-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {
                "captchaType": "click",
                "action": "click",
                "clicks": [{"x": 720, "y": 480, "label": "target"}],
                "clickCount": 1,
                "imageSize": {"width": 1440, "height": 900},
                "coordinateSpace": "resized_image",
            },
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        solution = solver.solve_aliyun_click_start(
            "img-b64",
            question="Click the main start verification button.",
            timeout_s=20.0,
            model_candidates="gpt-5.4",
            project_name="any-auto-register:zai",
        )

        self.assertEqual(solution["clickCount"], 1)
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["task"]["type"], "AliyunClickStartTask")
        self.assertEqual(create_payload["task"]["body"], "img-b64")
        self.assertEqual(
            create_payload["task"]["question"],
            "Click the main start verification button.",
        )
        self.assertEqual(create_payload["task"]["timeout_s"], 20.0)
        self.assertEqual(create_payload["task"]["model_candidates"], "gpt-5.4")
        self.assertEqual(create_payload["task"]["project_name"], "any-auto-register:zai")

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_hcaptcha_passes_timeout_controls(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-hcap-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {"gRecaptchaResponse": "token-123"},
        }
        post_mock.side_effect = [create_resp, result_resp]

        checkpoint = mock.Mock()
        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        token = solver.solve_hcaptcha(
            "https://build.nvidia.com/",
            "site-key",
            timeout_seconds=45,
            poll_interval_seconds=2,
            request_timeout_seconds=10,
            interrupt_checker=checkpoint,
        )

        self.assertEqual(token, "token-123")
        self.assertGreaterEqual(checkpoint.call_count, 1)
        self.assertEqual(post_mock.call_args_list[1].kwargs["timeout"], 10)

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_recaptcha_v2_uses_nocaptcha_task(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-recap-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {"gRecaptchaResponse": "token-recap-123"},
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        token = solver.solve_recaptcha_v2(
            "https://cloud.cerebras.ai/?useRecaptchaV2=true",
            "site-key",
            is_invisible=True,
            timeout_seconds=45,
            poll_interval_seconds=2,
            request_timeout_seconds=10,
        )

        self.assertEqual(token, "token-recap-123")
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(create_payload["task"]["type"], "NoCaptchaTaskProxyless")
        self.assertTrue(create_payload["task"]["isInvisible"])
        self.assertEqual(create_payload["task"]["websiteURL"], "https://cloud.cerebras.ai/?useRecaptchaV2=true")
        self.assertEqual(create_payload["task"]["websiteKey"], "site-key")
        self.assertEqual(post_mock.call_args_list[1].kwargs["timeout"], 10)

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_recaptcha_v2_enterprise_uses_enterprise_task(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-recap-2"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {
            "status": "ready",
            "solution": {"gRecaptchaResponse": "token-enterprise-123"},
        }
        post_mock.side_effect = [create_resp, result_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        token = solver.solve_recaptcha_v2(
            "https://cloud.cerebras.ai/?useRecaptchaV2=true",
            "enterprise-site-key",
            enterprise=True,
        )

        self.assertEqual(token, "token-enterprise-123")
        create_payload = post_mock.call_args_list[0].kwargs["json"]
        self.assertEqual(
            create_payload["task"]["type"],
            "RecaptchaV2EnterpriseTaskProxyless",
        )
        self.assertNotIn("isInvisible", create_payload["task"])

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_wait_task_result_honors_interrupt_checker(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-hcap-2"}
        pending_resp = mock.Mock()
        pending_resp.json.return_value = {
            "status": "processing",
            "errorId": 0,
        }
        post_mock.side_effect = [create_resp, pending_resp]

        calls = {"count": 0}

        def interrupt_checker():
            calls["count"] += 1
            if calls["count"] >= 2:
                raise RuntimeError("stop requested")

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        with self.assertRaisesRegex(RuntimeError, "stop requested"):
            solver.solve_hcaptcha(
                "https://build.nvidia.com/",
                "site-key",
                timeout_seconds=30,
                poll_interval_seconds=2,
                request_timeout_seconds=10,
                interrupt_checker=interrupt_checker,
            )

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_aliyun_surfaces_final_structured_error_after_deadline(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-aliyun-final-error"}
        pending_resp = mock.Mock()
        pending_resp.json.return_value = {"status": "processing", "errorId": 0}
        final_error_resp = mock.Mock()
        final_error_resp.json.return_value = {
            "errorId": 1,
            "errorCode": "ERROR_ALIYUN_CALLBACK_NOT_CAPTURED",
            "errorDescription": "no capturable verify payload before timeout",
        }
        post_mock.side_effect = [create_resp, pending_resp, final_error_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        with mock.patch("time.monotonic", side_effect=[0.0, 0.2, 1.1]):
            with self.assertRaisesRegex(RuntimeError, "ERROR_ALIYUN_CALLBACK_NOT_CAPTURED"):
                solver.solve_aliyun(
                    "https://chat.z.ai/auth?redirect_uri=https://z.ai/&action=signup",
                    captcha_selector="#aliyunCaptcha-captcha-wrapper",
                    mode_hint="slide",
                    callback_path="__APP_STATE__.captcha.verifyParam",
                    timeout_seconds=1.0,
                    poll_interval_seconds=3.0,
                    request_timeout_seconds=10.0,
                )

        self.assertEqual(post_mock.call_count, 3)

    @mock.patch("time.sleep", return_value=None)
    @mock.patch("requests.post")
    def test_yescaptcha_solve_aliyun_returns_final_ready_solution_after_deadline(self, post_mock, _sleep_mock):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-aliyun-final-ready"}
        pending_resp = mock.Mock()
        pending_resp.json.return_value = {"status": "processing", "errorId": 0}
        final_ready_resp = mock.Mock()
        final_ready_resp.json.return_value = {
            "status": "ready",
            "solution": {
                "token": "aliyun-token-final",
                "captchaVerifyParam": {"sceneId": "scene-final", "token": "aliyun-token-final"},
            },
        }
        post_mock.side_effect = [create_resp, pending_resp, final_ready_resp]

        solver = YesCaptcha("client-key", api_base="http://127.0.0.1:38010")
        with mock.patch("time.monotonic", side_effect=[0.0, 0.2, 1.1]):
            solution = solver.solve_aliyun(
                "https://chat.z.ai/auth?redirect_uri=https://z.ai/&action=signup",
                captcha_selector="#aliyunCaptcha-captcha-wrapper",
                mode_hint="slide",
                callback_path="__APP_STATE__.captcha.verifyParam",
                timeout_seconds=1.0,
                poll_interval_seconds=3.0,
                request_timeout_seconds=10.0,
            )

        self.assertEqual(solution["token"], "aliyun-token-final")
        self.assertEqual(solution["captchaVerifyParam"]["sceneId"], "scene-final")
        self.assertEqual(post_mock.call_count, 3)


if __name__ == "__main__":
    unittest.main()
