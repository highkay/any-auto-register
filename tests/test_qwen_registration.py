import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import Account, RegisterConfig
from platforms.qwen.core import QwenRegister, wait_for_activation_link
from platforms.qwen.cpa_upload import generate_token_json, upload_to_cpa
from platforms.qwen.plugin import QwenPlatform


class _DummyExecutor:
    page = object()


class _ExecutorWithPage:
    def __init__(self, page):
        self.page = page


class _DummyExecutorContext:
    def __enter__(self):
        return _DummyExecutor()

    def __exit__(self, exc_type, exc, tb):
        return False


class _SequenceQwenRegister(QwenRegister):
    def __init__(self, results, logs):
        super().__init__(executor=_DummyExecutor(), log_fn=logs.append)
        self._results = list(results)
        self.calls = 0

    def _try_register(self, page, email, password, full_name):
        current = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return dict(current)


class _FakeCFWorkerMailbox:
    def __init__(self, mails):
        self._mails = mails

    def _get_mails(self, _email: str):
        return list(self._mails)


class _FakeCFWorkerMailboxWithDifferentCurrentEmail(_FakeCFWorkerMailbox):
    def get_email(self):
        return MailboxAccount(email="other@example.com", account_id="dummy")

    def get_current_ids(self, _account):
        return set()


class _FakeHttpResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class QwenRegistrationTests(unittest.TestCase):
    def test_get_platform_actions_contains_upload_cpa_and_opengate(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        actions = platform.get_platform_actions()
        action_ids = [item.get("id") for item in actions]
        self.assertIn("upload_cpa", action_ids)
        self.assertIn("upload_opengate", action_ids)

    def test_register_stops_immediately_when_tokens_exist(self):
        logs = []
        reg = _SequenceQwenRegister(
            results=[
                {
                    "email": "demo@example.com",
                    "password": "Abc123!@#",
                    "full_name": "Demo",
                    "tokens": {"cookie:token": "tok_demo"},
                    "status": "success",
                }
            ],
            logs=logs,
        )

        result = reg.register(email="demo@example.com", password="Abc123!@#", full_name="Demo")

        self.assertEqual(reg.calls, 1)
        self.assertEqual(result.get("status"), "success")
        self.assertEqual(result.get("tokens", {}).get("cookie:token"), "tok_demo")
        self.assertTrue(any("first-attempt token hit" in msg for msg in logs))

    def test_register_returns_failed_after_retry_exhausted(self):
        logs = []
        reg = _SequenceQwenRegister(
            results=[
                {"status": "failed", "tokens": {}, "error": "attempt-1"},
                {"status": "failed", "tokens": {}, "error": "attempt-2"},
                {"status": "failed", "tokens": {}, "error": "attempt-3"},
            ],
            logs=logs,
        )

        with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
            result = reg.register(email="demo@example.com", password="Abc123!@#", full_name="Demo")

        self.assertEqual(reg.calls, 3)
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result.get("error"), "attempt-3")
        self.assertTrue(
            any("final failure reason: attempt-3" in msg for msg in logs)
            or any("final failure reason(no token): attempt-3" in msg for msg in logs)
        )

    def test_register_accepts_pending_activation_without_token(self):
        logs = []
        reg = _SequenceQwenRegister(
            results=[
                {
                    "email": "demo@example.com",
                    "password": "Abc123!@#",
                    "full_name": "Demo",
                    "tokens": {},
                    "cookies": {"session": "s1"},
                    "status": "success",
                    "pending_activation": True,
                }
            ],
            logs=logs,
        )

        result = reg.register(email="demo@example.com", password="Abc123!@#", full_name="Demo")

        self.assertEqual(result.get("status"), "success")
        self.assertTrue(result.get("pending_activation"))
        self.assertTrue(any("without JWT cookie" in msg for msg in logs))

    def test_wait_for_post_submit_tokens_solves_aliyun_waf_before_returning_token(self):
        page = mock.Mock()
        logs = []
        reg = QwenRegister(
            executor=_ExecutorWithPage(page),
            captcha_solver=mock.Mock(),
            captcha_mode="solve",
            log_fn=logs.append,
        )

        with mock.patch.object(
            reg,
            "_extract_tokens",
            side_effect=[{}, {}, {"cookie:token": "tok_after_waf"}],
        ):
            with mock.patch.object(
                reg,
                "_has_aliyun_waf_challenge",
                side_effect=[True, False],
            ):
                with mock.patch.object(reg, "_solve_aliyun_waf_challenge") as solve_mock:
                    with mock.patch.object(reg, "_sleep_with_checkpoint", return_value=None):
                        with mock.patch(
                            "platforms.qwen.core.time.time",
                            side_effect=[0.0, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0],
                        ):
                            tokens = reg._await_post_submit_tokens(page, timeout_seconds=5.0)

        self.assertEqual(tokens.get("cookie:token"), "tok_after_waf")
        solve_mock.assert_called_once_with(page)

    def test_await_post_submit_tokens_discards_captcha_without_solver(self):
        page = mock.Mock()
        logs = []
        reg = QwenRegister(
            executor=_ExecutorWithPage(page),
            captcha_solver=None,
            captcha_mode="discard",
            log_fn=logs.append,
        )

        with mock.patch.object(reg, "_extract_tokens", return_value={}):
            with mock.patch.object(reg, "_has_aliyun_waf_challenge", return_value=True):
                with mock.patch.object(reg, "_solve_aliyun_waf_challenge") as solve_mock:
                    with mock.patch.object(reg, "_sleep_with_checkpoint", return_value=None):
                        with mock.patch(
                            "platforms.qwen.core.time.time",
                            side_effect=[0.0, 0.1, 0.2],
                        ):
                            tokens = reg._await_post_submit_tokens(page, timeout_seconds=5.0)

        self.assertEqual(tokens, {})
        solve_mock.assert_not_called()
        self.assertTrue(any("captcha_discard" in msg for msg in logs))

    def test_ensure_aliyun_instrumentation_evaluates_current_page_and_installs_capture(self):
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/auth?mode=register"
        context = mock.Mock()
        page.context = context
        executor = _ExecutorWithPage(page)
        reg = QwenRegister(executor=executor, log_fn=lambda *_: None)

        with mock.patch.object(reg, "_install_response_capture") as install_capture:
            active_page = reg._ensure_aliyun_instrumentation(page)

        self.assertIs(active_page, page)
        context.add_init_script.assert_called_once()
        page.evaluate.assert_called_once()
        install_capture.assert_called_once_with(page, reg._response_store)

    def test_is_qwen_local_slide_fail_detects_body_class_and_text(self):
        reg = QwenRegister(executor=_ExecutorWithPage(mock.Mock()), log_fn=lambda *_: None)

        self.assertTrue(reg._is_qwen_local_slide_fail({"body_class": "fail"}))
        self.assertTrue(reg._is_qwen_local_slide_fail({"text": "验证失败，请重试"}))
        self.assertFalse(reg._is_qwen_local_slide_fail({"body_class": "null", "text": "拖动滑块完成拼图"}))

    def test_build_local_drag_strategies_includes_cv_variants_without_duplicates(self):
        reg = QwenRegister(executor=_ExecutorWithPage(mock.Mock()), log_fn=lambda *_: None)

        strategies = reg._build_local_drag_strategies(base_end_x=700.0, cv_end_x=712.0)
        pairs = [
            (item.get("profile"), item.get("anchor"), round(float(item.get("end_x") or 0.0), 2))
            for item in strategies
        ]

        self.assertIn(("closed_loop", "solver", 700.0), pairs)
        self.assertIn(("smooth", "solver", 700.0), pairs)
        self.assertIn(("overshoot", "solver", 700.0), pairs)
        self.assertIn(("closed_loop", "cv", 712.0), pairs)
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_recognize_aliyun_slide_actions_collects_multiple_solver_samples(self):
        solver = mock.Mock()
        solver.solve_aliyun_slide_action.side_effect = [
            {"slider": {"x": 1, "y": 2}, "gap": {"x": 10, "y": 20}},
            {"slider": {"x": 1, "y": 2}, "gap": {"x": 12, "y": 20}},
            {"slider": {"x": 1, "y": 2}, "gap": {"x": 11, "y": 20}},
        ]
        reg = QwenRegister(
            executor=_ExecutorWithPage(mock.Mock()),
            captcha_solver=solver,
            log_fn=lambda *_: None,
        )

        actions = reg._recognize_aliyun_slide_actions(
            "img-b64",
            question="请拖动滑块完成拼图",
            background_b64="bg-b64",
            piece_b64="piece-b64",
        )

        self.assertEqual(len(actions), 3)
        self.assertEqual(solver.solve_aliyun_slide_action.call_count, 3)

    def test_select_qwen_slide_action_prefers_largest_local_gap_cluster(self):
        reg = QwenRegister(executor=_ExecutorWithPage(mock.Mock()), log_fn=lambda *_: None)
        bbox = {"x": 490.0, "y": 301.0, "width": 300.0, "height": 248.0}
        actions = [
            {"gap": {"x": 960, "y": 10}, "slider": {"x": 0, "y": 0}, "imageSize": {"width": 1440, "height": 900}},
            {"gap": {"x": 962, "y": 10}, "slider": {"x": 0, "y": 0}, "imageSize": {"width": 1440, "height": 900}},
            {"gap": {"x": 1010, "y": 10}, "slider": {"x": 0, "y": 0}, "imageSize": {"width": 1440, "height": 900}},
        ]

        action, meta = reg._select_qwen_slide_action(bbox=bbox, actions=actions)

        self.assertEqual(action["gap"]["x"], 962)
        self.assertEqual(meta["clusterSize"], 2)
        self.assertEqual(meta["sampleLocalGapXs"], [200.0, 200.42, 210.42])
        self.assertEqual(meta["selectedLocalGapX"], 200.42)

    def test_resolve_slide_end_x_keeps_solver_point_when_image_estimator_disagrees(self):
        reg = QwenRegister(executor=_ExecutorWithPage(mock.Mock()), log_fn=lambda *_: None)
        bbox = {"x": 490.0, "y": 301.0, "width": 300.0, "height": 248.0}
        gap = {"x": 1012, "y": 126}

        with mock.patch.object(reg, "_estimate_gap_center_from_images", return_value=(273.5, 300)):
            end_x = reg._resolve_slide_end_x(
                bbox,
                gap,
                background_png=b"bg",
                piece_png=b"piece",
                reference_width=1440.0,
                reference_height=900.0,
                gap_source="llm",
            )

        self.assertAlmostEqual(end_x - bbox["x"], 210.83, places=2)

    def test_drag_slider_with_profile_runs_smooth_and_overshoot_paths(self):
        page = mock.Mock()
        page.mouse = mock.Mock()
        reg = QwenRegister(executor=_ExecutorWithPage(page), log_fn=lambda *_: None)

        reg._drag_slider_with_profile(
            page,
            profile="smooth",
            start_x=100.0,
            start_y=200.0,
            end_x=240.0,
        )
        reg._drag_slider_with_profile(
            page,
            profile="overshoot",
            start_x=100.0,
            start_y=200.0,
            end_x=240.0,
        )

        self.assertGreaterEqual(page.mouse.down.call_count, 2)
        self.assertGreaterEqual(page.mouse.up.call_count, 2)
        self.assertGreater(page.mouse.move.call_count, 4)
        self.assertTrue(page.wait_for_timeout.called)

    def test_solve_aliyun_waf_challenge_reinstalls_instrumentation_on_live_page(self):
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/auth?mode=register"
        reg = QwenRegister(
            executor=_ExecutorWithPage(page),
            captcha_solver=mock.Mock(),
            log_fn=lambda *_: None,
        )
        reg.captcha_solver.solve_aliyun_slide_action.return_value = {
            "action": "slide",
            "captchaType": "slide",
            "slider": {"x": 78, "y": 828},
            "gap": {"x": 1200, "y": 280},
            "imageSize": {"width": 1440, "height": 900},
        }

        with mock.patch.object(reg, "_ensure_aliyun_instrumentation", return_value=page) as ensure_mock:
            with mock.patch.object(reg, "_wait_for_aliyun_slide_ready"):
                with mock.patch.object(reg, "_challenge_question", return_value="请拖动滑块完成拼图"):
                    with mock.patch.object(reg, "_slide_action_bbox", return_value={"x": 490.0, "y": 301.0, "width": 300.0, "height": 248.0}):
                        with mock.patch.object(reg, "_locator_bbox", side_effect=[{"x": 490.0, "y": 509.0, "width": 40.0, "height": 40.0}, {"x": 490.0, "y": 301.0, "width": 300.0, "height": 200.0}]):
                            with mock.patch.object(reg, "_screenshot_clip_with_hidden", side_effect=[b"slide", b"background"]):
                                with mock.patch.object(reg, "_locator_screenshot", return_value=b"piece"):
                                    with mock.patch.object(reg, "_resolve_slide_end_x", return_value=754.0):
                                        with mock.patch.object(reg, "_log_aliyun_action_trace"):
                                            with mock.patch.object(reg, "_drag_slider"):
                                                with mock.patch.object(reg, "_sleep_with_checkpoint"):
                                                    with mock.patch.object(reg, "_wait_for_aliyun_challenge_outcome", return_value=True):
                                                        reg._solve_aliyun_waf_challenge(page)

        ensure_mock.assert_called_once_with(page)

    def test_solve_aliyun_waf_challenge_tries_multiple_local_strategies_before_refresh(self):
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/auth?mode=register"
        reg = QwenRegister(
            executor=_ExecutorWithPage(page),
            captcha_solver=mock.Mock(),
            log_fn=lambda *_: None,
        )
        reg.captcha_solver.solve_aliyun_slide_action.return_value = {
            "action": "slide",
            "captchaType": "slide",
            "slider": {"x": 78, "y": 828},
            "gap": {"x": 1200, "y": 280},
            "imageSize": {"width": 1440, "height": 900},
        }

        with mock.patch.object(reg, "_ensure_aliyun_instrumentation", return_value=page):
            with mock.patch.object(reg, "_wait_for_aliyun_slide_ready"):
                with mock.patch.object(reg, "_challenge_question", return_value="请拖动滑块完成拼图"):
                    with mock.patch.object(reg, "_slide_action_bbox", return_value={"x": 490.0, "y": 301.0, "width": 300.0, "height": 248.0}):
                        with mock.patch.object(reg, "_locator_bbox", side_effect=[{"x": 490.0, "y": 509.0, "width": 40.0, "height": 40.0}, {"x": 490.0, "y": 301.0, "width": 300.0, "height": 200.0}]):
                            with mock.patch.object(reg, "_screenshot_clip_with_hidden", side_effect=[b"slide", b"background"]):
                                with mock.patch.object(reg, "_locator_screenshot", return_value=b"piece"):
                                    with mock.patch.object(reg, "_resolve_slide_end_x", return_value=754.0):
                                        with mock.patch.object(reg, "_resolve_cv_slide_end_x", return_value=None):
                                            with mock.patch.object(reg, "_build_local_drag_strategies", return_value=[
                                                {"profile": "closed_loop", "anchor": "solver", "end_x": 754.0, "offset": 0.0},
                                                {"profile": "smooth", "anchor": "solver", "end_x": 754.0, "offset": 0.0},
                                            ]):
                                                with mock.patch.object(reg, "_log_aliyun_action_trace"):
                                                    with mock.patch.object(reg, "_drag_slider_with_profile") as drag_mock:
                                                        with mock.patch.object(reg, "_sleep_with_checkpoint"):
                                                            with mock.patch.object(reg, "_wait_for_aliyun_challenge_outcome", side_effect=[False, True]):
                                                                with mock.patch.object(reg, "_has_aliyun_waf_challenge", return_value=True):
                                                                    with mock.patch.object(reg, "_capture_qwen_challenge_snapshot", return_value={"body_class": "fail", "text": "验证失败，请重试"}):
                                                                        with mock.patch.object(reg, "_is_qwen_local_slide_fail", side_effect=[True, False]):
                                                                            with mock.patch.object(reg, "_wait_for_qwen_local_reset", return_value={"status": "ready", "snapshot": {}}):
                                                                                with mock.patch.object(reg, "_refresh_aliyun_challenge") as refresh_mock:
                                                                                    reg._solve_aliyun_waf_challenge(page)

        self.assertEqual(drag_mock.call_count, 2)
        refresh_mock.assert_not_called()

    def test_build_post_submit_failure_reason_classifies_aliyun_waf_html(self):
        page = mock.Mock()
        reg = QwenRegister(executor=_ExecutorWithPage(page), log_fn=lambda *_: None)
        reg._response_store["signup"] = {
            "status": 200,
            "text": "<div id='waf_nc_block'>访问验证 拖动滑块完成拼图 aliyunCaptcha</div>",
        }

        with mock.patch.object(reg, "_has_aliyun_waf_challenge", return_value=False):
            with mock.patch.object(reg, "_debug_summary", return_value='{"hookInstalled":true}'):
                reason = reg._build_post_submit_failure_reason(page)

        self.assertIn("Aliyun WAF challenge", reason)
        self.assertIn("signup_response=aliyun_waf", reason)

    def test_plugin_register_defaults_to_captcha_discard_without_solver(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {"cookie:token": "tok_ok"},
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch.object(platform, "_make_captcha") as make_captcha:
                with mock.patch("platforms.qwen.core.QwenRegister") as register_cls:
                    register_cls.return_value.register.return_value = fake_result
                    account = platform.register(email="demo@example.com", password="Abc123!@#")

        self.assertEqual(account.token, "tok_ok")
        make_captcha.assert_not_called()
        self.assertIsNone(register_cls.call_args.kwargs.get("captcha_solver"))
        self.assertEqual(register_cls.call_args.kwargs.get("captcha_mode"), "discard")

    def test_plugin_register_builds_captcha_solver_when_mode_solve(self):
        platform = QwenPlatform(
            config=RegisterConfig(executor_type="headless", extra={"qwen_captcha_mode": "solve"}),
            mailbox=None,
        )
        captcha_solver = mock.Mock()
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {"cookie:token": "tok_ok"},
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch.object(platform, "_make_captcha", return_value=captcha_solver):
                with mock.patch("platforms.qwen.core.QwenRegister") as register_cls:
                    register_cls.return_value.register.return_value = fake_result
                    account = platform.register(email="demo@example.com", password="Abc123!@#")

        self.assertEqual(account.token, "tok_ok")
        self.assertIs(register_cls.call_args.kwargs["captcha_solver"], captcha_solver)
        self.assertEqual(register_cls.call_args.kwargs.get("captcha_mode"), "solve")

    def test_plugin_success_requires_non_empty_token(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {"cookie:token": "tok_ok"},
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                account = platform.register(email="demo@example.com", password="Abc123!@#")

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.token, "tok_ok")

    def test_plugin_raises_when_registration_status_failed(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "failed",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {},
            "error": "no token",
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                with self.assertRaises(RuntimeError):
                    platform.register(email="demo@example.com", password="Abc123!@#")

    def test_plugin_raises_when_success_but_token_missing(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {},
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                with self.assertRaises(RuntimeError):
                    platform.register(email="demo@example.com", password="Abc123!@#")

    def test_plugin_register_extracts_oauth_fields_from_raw_tokens(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        fake_result = {
            "status": "success",
            "email": "demo@example.com",
            "password": "Abc123!@#",
            "tokens": {
                "cookie:token": "tok_ok",
                "oauth_payload": (
                    '{"oauth_access_token":"oa_demo",'
                    '"refreshToken":"rt_demo","resource_url":"portal.qwen.ai"}'
                ),
            },
        }

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch("platforms.qwen.core.QwenRegister.register", return_value=fake_result):
                account = platform.register(email="demo@example.com", password="Abc123!@#")

        self.assertEqual(account.token, "tok_ok")
        self.assertEqual((account.extra or {}).get("oauth_access_token"), "oa_demo")
        self.assertEqual((account.extra or {}).get("refresh_token"), "rt_demo")
        self.assertEqual((account.extra or {}).get("resource_url"), "portal.qwen.ai")

    def test_activate_action_can_bootstrap_mailbox_and_activate(self):
        platform = QwenPlatform(
            config=RegisterConfig(extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="",
        )
        fake_mailbox = _FakeCFWorkerMailbox(
            mails=[
                {
                    "id": 1,
                    "subject": "Activate your Qwen account",
                    "raw": (
                        "Click to activate: "
                        "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def"
                    ),
                }
            ]
        )

        with mock.patch("core.base_mailbox.create_mailbox", return_value=fake_mailbox):
            with mock.patch("platforms.qwen.core.call_activation_api", return_value={"ok": True}):
                with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
                    result = platform.execute_action("activate_account", account, {})

        self.assertTrue(result.get("ok"))

    def test_activate_action_reports_timeout_with_default_wait_seconds(self):
        platform = QwenPlatform(
            config=RegisterConfig(
                extra={
                    "mail_provider": "cfworker",
                    "mailbox_otp_timeout_seconds": 30,
                }
            ),
            mailbox=None,
        )
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="",
        )
        fake_mailbox = _FakeCFWorkerMailbox(mails=[])

        with mock.patch("core.base_mailbox.create_mailbox", return_value=fake_mailbox):
            with mock.patch("platforms.qwen.core.wait_for_activation_link", return_value=None):
                result = platform.execute_action("activate_account", account, {})

        self.assertFalse(result.get("ok"))
        self.assertIn("在 30s 内未找到激活邮件", str(result.get("error")))

    def test_activate_action_ignores_current_mailbox_email_for_cfworker_lookup(self):
        platform = QwenPlatform(
            config=RegisterConfig(extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )
        account = Account(
            platform="qwen",
            email="target@example.com",
            password="Abc123!@#",
            token="",
        )
        fake_mailbox = _FakeCFWorkerMailboxWithDifferentCurrentEmail(
            mails=[
                {
                    "id": 1,
                    "subject": "Activate",
                    "raw": "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def",
                }
            ]
        )

        with mock.patch("core.base_mailbox.create_mailbox", return_value=fake_mailbox):
            with mock.patch("platforms.qwen.core.call_activation_api", return_value={"ok": True}):
                with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
                    result = platform.execute_action("activate_account", account, {})

        self.assertTrue(result.get("ok"))

    def test_wait_for_activation_link_can_decode_base64_html_raw_mail(self):
        import base64

        html = (
            '<html><body>'
            '<a href="https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def">Activate</a>'
            "</body></html>"
        )
        b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
        raw = (
            "MIME-Version: 1.0\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            f"{b64}\r\n"
        )
        mailbox = _FakeCFWorkerMailbox(
            mails=[{"id": 1, "subject": "Activate", "raw": raw}]
        )

        with mock.patch("platforms.qwen.core.time.sleep", return_value=None):
            link = wait_for_activation_link(
                mailbox,
                account_email="target@example.com",
                timeout=5,
            )

        self.assertEqual(
            link,
            "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def",
        )

    def test_get_user_info_fallbacks_to_chats_endpoint(self):
        # Header: {"alg":"HS256","typ":"JWT"}
        # Payload: {"id":"u1","exp":1778728455}
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6InUxIiwiZXhwIjoxNzc4NzI4NDU1fQ.sig"
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token=token,
        )

        responses = [
            _FakeHttpResp(404, {"detail": "Not Found"}),
            _FakeHttpResp(403, {"detail": "restricted"}),
            _FakeHttpResp(200, []),
        ]

        with mock.patch("curl_cffi.requests.get", side_effect=responses):
            result = platform.execute_action("get_user_info", account, {})

        self.assertTrue(result.get("ok"))
        data = result.get("data", {})
        self.assertEqual(data.get("来源"), "会话列表接口")
        self.assertEqual(data.get("会话数量"), 0)
        self.assertEqual(data.get("用户ID"), "u1")

    def test_upload_cpa_action_uses_qwen_uploader(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="qwen_token_abc",
            extra={
                "refresh_token": "qwen_refresh_token_xyz",
                "resource_url": "portal.qwen.ai",
            },
        )

        with mock.patch(
            "platforms.qwen.cpa_upload.generate_token_json",
            return_value={"email": "demo@example.com", "access_token": "qwen_token_abc"},
        ) as build_mock:
            with mock.patch(
                "platforms.qwen.cpa_upload.upload_to_cpa",
                return_value=(True, "上传成功"),
            ) as upload_mock:
                result = platform.execute_action(
                    "upload_cpa",
                    account,
                    {"api_url": "http://cpa.local", "api_key": "k"},
                )

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("data"), "上传成功")
        build_arg = build_mock.call_args.args[0]
        self.assertEqual(getattr(build_arg, "email", ""), "demo@example.com")
        self.assertEqual(getattr(build_arg, "access_token", ""), "qwen_token_abc")
        self.assertEqual(getattr(build_arg, "refresh_token", ""), "qwen_refresh_token_xyz")
        self.assertEqual(getattr(build_arg, "resource_url", ""), "portal.qwen.ai")
        upload_mock.assert_called_once_with(
            {"email": "demo@example.com", "access_token": "qwen_token_abc"},
            api_url="http://cpa.local",
            api_key="k",
        )

    def test_upload_cpa_action_reads_refresh_from_raw_tokens(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="qwen_token_abc",
            extra={
                "raw_tokens": {
                    "oauth_payload": '{"refreshToken":"rt_raw","resource_url":"portal.qwen.ai"}',
                }
            },
        )

        with mock.patch(
            "platforms.qwen.cpa_upload.generate_token_json",
            return_value={"email": "demo@example.com", "access_token": "qwen_token_abc"},
        ) as build_mock:
            with mock.patch(
                "platforms.qwen.cpa_upload.upload_to_cpa",
                return_value=(True, "上传成功"),
            ):
                result = platform.execute_action(
                    "upload_cpa",
                    account,
                    {"api_url": "http://cpa.local", "api_key": "k"},
                )

        self.assertTrue(result.get("ok"))
        build_arg = build_mock.call_args.args[0]
        self.assertEqual(getattr(build_arg, "refresh_token", ""), "rt_raw")
        self.assertEqual(getattr(build_arg, "resource_url", ""), "portal.qwen.ai")

    def test_upload_cpa_action_bootstraps_oauth_when_refresh_missing(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        account = Account(
            platform="qwen",
            email="demo@example.com",
            password="Abc123!@#",
            token="web_token_only",
            extra={},
        )

        with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
            with mock.patch(
                "platforms.qwen.core.obtain_qwen_oauth_tokens_with_login",
                return_value={
                    "oauth_access_token": "oauth_access_123",
                    "refresh_token": "oauth_refresh_456",
                    "resource_url": "portal.qwen.ai",
                },
            ):
                with mock.patch(
                    "platforms.qwen.cpa_upload.generate_token_json",
                    return_value={"email": "demo@example.com", "access_token": "oauth_access_123"},
                ) as build_mock:
                    with mock.patch(
                        "platforms.qwen.cpa_upload.upload_to_cpa",
                        return_value=(True, "上传成功"),
                    ):
                        result = platform.execute_action(
                            "upload_cpa",
                            account,
                            {"api_url": "http://cpa.local", "api_key": "k"},
                        )

        self.assertTrue(result.get("ok"))
        build_arg = build_mock.call_args.args[0]
        self.assertEqual(getattr(build_arg, "access_token", ""), "oauth_access_123")
        self.assertEqual(getattr(build_arg, "refresh_token", ""), "oauth_refresh_456")
        self.assertEqual(getattr(build_arg, "resource_url", ""), "portal.qwen.ai")
        self.assertEqual(
            (result.get("account_extra_patch") or {}).get("refresh_token"),
            "oauth_refresh_456",
        )

    def test_qwen_cpa_upload_requires_refresh_token(self):
        ok, msg = upload_to_cpa(
            {
                "type": "qwen",
                "email": "demo@example.com",
                "access_token": "token_only",
                "refresh_token": "",
            },
            api_url="http://cpa.local",
            api_key="k",
        )
        self.assertFalse(ok)
        self.assertIn("refresh_token", msg)

    def test_qwen_cpa_generate_token_json_contains_oauth_fields(self):
        class _A:
            pass

        a = _A()
        a.email = "demo@example.com"
        a.access_token = "token"
        a.refresh_token = "rt_demo"
        a.resource_url = "portal.qwen.ai"

        token_json = generate_token_json(a)
        self.assertEqual(token_json.get("type"), "qwen")
        self.assertEqual(token_json.get("provider"), "qwen")
        self.assertEqual(token_json.get("email"), "demo@example.com")
        self.assertEqual(token_json.get("access_token"), "token")
        self.assertEqual(token_json.get("refresh_token"), "rt_demo")
        self.assertEqual(token_json.get("resource_url"), "portal.qwen.ai")

    def test_obtain_oauth_with_cookies_authorizes_device_code(self):
        from platforms.qwen.core import obtain_qwen_oauth_tokens_with_cookies

        device_resp = _FakeHttpResp(
            200,
            {
                "device_code": "dev-1",
                "user_code": "USER1",
                "verification_uri_complete": "https://chat.qwen.ai/device?user_code=USER1",
            },
        )
        auth_resp = _FakeHttpResp(200, {"ok": True})
        token_resp = _FakeHttpResp(
            200,
            {
                "access_token": "oauth_access",
                "refresh_token": "oauth_refresh",
                "resource_url": "portal.qwen.ai",
                "token_type": "Bearer",
                "scope": "openid",
                "expires_in": 3600,
            },
        )

        with mock.patch(
            "platforms.qwen.core.requests.post",
            side_effect=[device_resp, auth_resp, token_resp],
        ) as post_mock:
            result = obtain_qwen_oauth_tokens_with_cookies(
                {"token": "web_jwt"},
                email="demo@example.com",
            )

        self.assertEqual(result.get("oauth_access_token"), "oauth_access")
        self.assertEqual(result.get("refresh_token"), "oauth_refresh")
        self.assertEqual(post_mock.call_count, 3)
        authorize_call = post_mock.call_args_list[1]
        self.assertIn("/api/v2/oauth2/authorize", authorize_call.args[0])
        self.assertEqual(authorize_call.kwargs.get("json"), {"approved": True, "user_code": "USER1"})
        self.assertIn("token=web_jwt", authorize_call.kwargs["headers"].get("Cookie", ""))

    def test_call_activation_api_replays_cookies(self):
        from platforms.qwen.core import call_activation_api

        class _FakeSession:
            def __init__(self):
                self.headers = {}
                self.cookies = mock.Mock()
                self.cookies.get_dict.return_value = {"token": "after_activate"}
                self.cookies.set = mock.Mock()

            def get(self, url, timeout=20, allow_redirects=True):
                resp = mock.Mock()
                resp.status_code = 200
                resp.url = url
                return resp

        with mock.patch("platforms.qwen.core.requests.Session", return_value=_FakeSession()):
            result = call_activation_api(
                "https://chat.qwen.ai/api/v1/auths/activate?id=abc&token=def",
                cookies={"token": "before"},
            )

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("token"), "after_activate")
        self.assertEqual((result.get("cookies") or {}).get("token"), "after_activate")

    def test_plugin_register_uses_cookie_oauth_after_activation(self):
        platform = QwenPlatform(config=RegisterConfig(executor_type="headless"), mailbox=None)
        logs = []
        platform._log_fn = logs.append

        class _FakeReg:
            def __init__(self, *args, **kwargs):
                pass

            def register(self, email, password=None, full_name=""):
                return {
                    "email": email,
                    "password": password or "Pw1!",
                    "full_name": "Demo",
                    "tokens": {},
                    "cookies": {"token": "pre_act"},
                    "status": "success",
                    "pending_activation": True,
                }

        class _Mail:
            def get_email(self):
                return MailboxAccount(email="demo@example.com", account_id="1")

            def get_current_ids(self, _acct):
                return set()

        platform.mailbox = _Mail()

        with mock.patch.object(platform, "_make_captcha", return_value=None):
            with mock.patch.object(platform, "_make_executor", return_value=_DummyExecutorContext()):
                with mock.patch("platforms.qwen.core.QwenRegister", _FakeReg):
                    with mock.patch(
                        "platforms.qwen.core.wait_for_activation_link",
                        return_value="https://chat.qwen.ai/api/v1/auths/activate?id=1&token=2",
                    ):
                        with mock.patch(
                            "platforms.qwen.core.call_activation_api",
                            return_value={
                                "ok": True,
                                "status_code": 200,
                                "token": "after_act",
                                "cookies": {"token": "after_act"},
                            },
                        ):
                            with mock.patch(
                                "platforms.qwen.core.obtain_qwen_oauth_tokens_with_cookies",
                                return_value={
                                    "oauth_access_token": "oa",
                                    "refresh_token": "rt",
                                    "resource_url": "portal.qwen.ai",
                                },
                            ) as cookie_oauth:
                                with mock.patch(
                                    "platforms.qwen.core.obtain_qwen_oauth_tokens_with_login"
                                ) as login_oauth:
                                    with mock.patch(
                                        "platforms.qwen.core.disable_qwen_memory_features",
                                        return_value=True,
                                    ):
                                        account = platform.register("demo@example.com", "Pw1!")

        self.assertEqual(account.token, "after_act")
        self.assertEqual(account.extra.get("refresh_token"), "rt")
        self.assertTrue(account.extra.get("activated"))
        cookie_oauth.assert_called_once()
        login_oauth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
