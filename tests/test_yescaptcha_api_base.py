import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
