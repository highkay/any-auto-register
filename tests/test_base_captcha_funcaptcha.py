"""FunCaptcha / PerimeterX / CompositeCaptcha unit tests (mocked HTTP)."""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from core.base_captcha import (
    CapSolverCaptcha,
    CompositeCaptcha,
    EZCaptchaCaptcha,
    PerimeterXSolution,
    YesCaptcha,
)
from core.base_platform import BasePlatform, RegisterConfig
from core.flags import FEATURE_CAPSOLVER


class _DummyPlatform(BasePlatform):
    name = "dummy"
    display_name = "Dummy"
    supported_executors = ["protocol"]

    def register(self, email: str, password: str = None):
        raise NotImplementedError

    def check_valid(self, account):
        return False


class YesCaptchaFunCaptchaTest(unittest.TestCase):
    @patch("requests.post")
    def test_solve_funcaptcha_with_blob(self, post):
        create = MagicMock()
        create.json.return_value = {"taskId": "t1", "errorId": 0}
        create.text = '{"taskId":"t1"}'
        result = MagicMock()
        result.json.return_value = {
            "status": "ready",
            "solution": {"token": "arkose-token-xyz"},
            "errorId": 0,
        }
        post.side_effect = [create, result]

        solver = YesCaptcha("key-1", api_base="https://yes.example")
        token = solver.solve_funcaptcha(
            "https://github.com/signup",
            "747B83EC-2CA3-43AD-A7DF-701F286FBABA",
            subdomain="github-api.arkoselabs.com",
            blob="blob-data",
            timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
        self.assertEqual(token, "arkose-token-xyz")
        create_body = post.call_args_list[0].kwargs.get("json") or post.call_args_list[0][1].get("json")
        if create_body is None:
            create_body = post.call_args_list[0].args[1] if len(post.call_args_list[0].args) > 1 else post.call_args_list[0].kwargs["json"]
        # requests.post(url, json=...)
        called_json = post.call_args_list[0].kwargs["json"]
        task = called_json["task"]
        self.assertEqual(task["type"], "FunCaptchaTaskProxyless")
        self.assertEqual(task["websitePublicKey"], "747B83EC-2CA3-43AD-A7DF-701F286FBABA")
        self.assertIn("arkoselabs.com", task["funcaptchaApiJSSubdomain"])
        self.assertEqual(json.loads(task["data"]), {"blob": "blob-data"})


class CapSolverPerimeterXTest(unittest.TestCase):
    @patch("requests.post")
    def test_solve_perimeterx_cookies(self, post):
        create = MagicMock()
        create.json.return_value = {"taskId": "px1", "errorId": 0}
        create.text = "{}"
        result = MagicMock()
        result.json.return_value = {
            "status": "ready",
            "solution": {"cookies": {"_px3": "abc", "_pxvid": "vid"}},
            "errorId": 0,
        }
        post.side_effect = [create, result]
        solver = CapSolverCaptcha("cap-key")
        sol = solver.solve_perimeterx(
            "https://signup.live.com/",
            "PXzC5j78di",
            timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
        self.assertIsInstance(sol, PerimeterXSolution)
        self.assertTrue(sol.ok)
        self.assertEqual(sol.method, "capsolver")
        self.assertEqual(sol.cookies.get("_px3"), "abc")


class CompositeCaptchaTest(unittest.TestCase):
    def test_falls_through_to_second_solver(self):
        first = MagicMock()
        first.solve_funcaptcha.side_effect = RuntimeError("fail-1")
        second = MagicMock()
        second.solve_funcaptcha.return_value = "token-2"
        composite = CompositeCaptcha([first, second], max_provider_attempts=3, labels=["a", "b"])
        self.assertEqual(
            composite.solve_funcaptcha("https://x", "pk"),
            "token-2",
        )

    def test_skips_not_implemented(self):
        first = MagicMock()
        first.solve_funcaptcha.side_effect = NotImplementedError
        second = MagicMock()
        second.solve_funcaptcha.return_value = "ok"
        composite = CompositeCaptcha([first, second])
        self.assertEqual(composite.solve_funcaptcha("https://x", "pk"), "ok")


class MakeCaptchaTest(unittest.TestCase):
    def test_yescaptcha_default(self):
        p = _DummyPlatform(RegisterConfig(captcha_solver="yescaptcha", extra={"yescaptcha_key": "k"}))
        solver = p._make_captcha()
        self.assertIsInstance(solver, YesCaptcha)

    def test_capsolver_requires_flag(self):
        p = _DummyPlatform(
            RegisterConfig(
                captcha_solver="capsolver",
                extra={"capsolver_key": "k", FEATURE_CAPSOLVER: "0"},
            )
        )
        # flag read from config_store primarily; extra alone may not enable —
        # force via patched store
        with patch("core.config_store.config_store.get_all", return_value={FEATURE_CAPSOLVER: "0"}):
            with self.assertRaises(ValueError):
                p._make_captcha()

    def test_capsolver_when_flag_on(self):
        p = _DummyPlatform(
            RegisterConfig(captcha_solver="capsolver", extra={"capsolver_key": "k"})
        )
        with patch("core.config_store.config_store.get_all", return_value={FEATURE_CAPSOLVER: "1"}):
            solver = p._make_captcha()
        self.assertIsInstance(solver, CapSolverCaptcha)

    def test_auto_chain_builds_composite(self):
        p = _DummyPlatform(
            RegisterConfig(
                captcha_solver="auto",
                extra={
                    "yescaptcha_key": "y",
                    "ezcaptcha_key": "e",
                    "captcha_max_provider_attempts": "2",
                },
            )
        )
        with patch(
            "core.config_store.config_store.get_all",
            return_value={FEATURE_CAPSOLVER: "0"},
        ):
            solver = p._make_captcha()
        self.assertIsInstance(solver, CompositeCaptcha)
        self.assertEqual(solver.max_provider_attempts, 2)
        self.assertEqual(len(solver.solvers), 2)

    def test_unknown_solver(self):
        p = _DummyPlatform(RegisterConfig(captcha_solver="nope"))
        with self.assertRaises(ValueError):
            p._make_captcha()

    def test_ezcaptcha_without_flag(self):
        p = _DummyPlatform(
            RegisterConfig(captcha_solver="ezcaptcha", extra={"ezcaptcha_key": "e"})
        )
        solver = p._make_captcha()
        self.assertIsInstance(solver, EZCaptchaCaptcha)


if __name__ == "__main__":
    unittest.main()
