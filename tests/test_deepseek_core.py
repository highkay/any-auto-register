import unittest
from unittest import mock

import requests

from platforms.deepseek.core import (
    DEEPSEEK_HCAPTCHA_SITEKEY,
    DEEPSEEK_POW_WORKER_HOST_PAGE_URL,
    DEEPSEEK_TURNSTILE_SITEKEY,
    DeepSeekClient,
    DeepSeekEmailDomainRejected,
    _apply_deepseek_browser_identity,
    _apply_deepseek_flaresolverr_cookies,
    _build_deepseek_browser_identity_override,
    _build_deepseek_guest_pow_header_route,
    _build_deepseek_send_code_request_route,
    _classify_deepseek_sign_up_state,
    _collect_deepseek_flaresolverr_proxy_url,
    _encode_deepseek_guest_pow_response_in_page,
    _encode_deepseek_guest_pow_response_with_context_page,
    _extract_deepseek_flaresolverr_turnstile_token,
    _is_deepseek_email_domain_not_supported,
    _open_deepseek_sign_up_browser_page,
    _read_deepseek_turnstile_sitekey,
    _request_deepseek_flaresolverr_solution,
    _resolve_deepseek_send_code_challenge_tokens,
    _reuse_deepseek_turnstile_on_current_page,
    _solve_deepseek_hcaptcha_token,
    _solve_deepseek_turnstile_by_flaresolverr,
    _solve_deepseek_turnstile_token,
    _summarize_deepseek_sign_up_state,
    _wait_for_deepseek_manual_send_code_success,
    build_deepseek_accept_language,
    build_deepseek_page_url,
    extract_deepseek_client_locale,
    normalize_deepseek_ui_locale,
    resolve_deepseek_flaresolverr_url,
)


class DeepSeekCoreTests(unittest.TestCase):
    def test_build_deepseek_page_url_does_not_append_locale_query(self):
        self.assertEqual(
            build_deepseek_page_url("/sign_up", "en-US"),
            "https://chat.deepseek.com/sign_up",
        )

    def test_resolve_deepseek_flaresolverr_url_appends_v1(self):
        self.assertEqual(
            resolve_deepseek_flaresolverr_url("http://127.0.0.1:8191"),
            "http://127.0.0.1:8191/v1",
        )

    def test_resolve_deepseek_flaresolverr_url_requires_explicit_value(self):
        self.assertEqual(resolve_deepseek_flaresolverr_url(None), "")

    def test_collect_deepseek_flaresolverr_proxy_url_bridges_loopback(self):
        logs: list[str] = []
        self.assertEqual(
            _collect_deepseek_flaresolverr_proxy_url(
                "socks5h://127.0.0.1:7890",
                log_fn=logs.append,
            ),
            "socks5://host.docker.internal:7890",
        )
        self.assertTrue(any("host.docker.internal" in entry for entry in logs))

    def test_collect_deepseek_flaresolverr_proxy_url_preserves_auth(self):
        self.assertEqual(
            _collect_deepseek_flaresolverr_proxy_url(
                "http://user:p%40ss@127.0.0.1:7890",
                log_fn=lambda *_: None,
            ),
            "http://user:p%40ss@host.docker.internal:7890",
        )

    def test_build_deepseek_browser_identity_override_aligns_chrome_metadata(self):
        payload = _build_deepseek_browser_identity_override(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.7499.40 Safari/537.36",
            accept_language="en-US,en;q=0.9",
        )

        self.assertEqual(payload["acceptLanguage"], "en-US,en;q=0.9")
        self.assertEqual(payload["platform"], "Win32")
        self.assertEqual(
            payload["userAgentMetadata"]["brands"][0],
            {"brand": "Google Chrome", "version": "143"},
        )
        self.assertEqual(payload["userAgentMetadata"]["platform"], "Windows")
        self.assertEqual(payload["userAgentMetadata"]["fullVersion"], "143.0.7499.40")

    def test_locale_helpers_match_live_browser_contract(self):
        self.assertEqual(normalize_deepseek_ui_locale("en_US"), "en-US")
        self.assertEqual(extract_deepseek_client_locale("en-US"), "en_US")
        self.assertEqual(build_deepseek_accept_language("en-US"), "en-US,en;q=0.9")

    def test_apply_deepseek_browser_identity_uses_cdp_override(self):
        context = mock.Mock()
        page = mock.Mock()
        cdp = mock.Mock()
        context.new_cdp_session.return_value = cdp

        _apply_deepseek_browser_identity(
            context,
            page,
            browser_user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
            ),
            accept_language="en-US,en;q=0.9",
            log_fn=lambda *_: None,
        )

        context.new_cdp_session.assert_called_once_with(page)
        cdp.send.assert_called_once()
        self.assertEqual(cdp.send.call_args.args[0], "Emulation.setUserAgentOverride")
        self.assertEqual(
            cdp.send.call_args.args[1]["userAgentMetadata"]["fullVersion"],
            "142.0.0.0",
        )

    def test_apply_deepseek_flaresolverr_cookies_adds_context_cookies(self):
        page = mock.Mock()
        page.context.add_cookies = mock.Mock()

        names = _apply_deepseek_flaresolverr_cookies(
            page,
            [
                {
                    "name": "cf_clearance",
                    "value": "cookie-value",
                    "domain": "chat.deepseek.com",
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
        self.assertEqual(payload[0]["domain"], "chat.deepseek.com")
        self.assertEqual(payload[0]["sameSite"], "None")
        self.assertEqual(payload[0]["expires"], 1893456000.0)

    def test_extract_deepseek_flaresolverr_turnstile_token_from_solution(self):
        token = "0.flare-token-12345678901234567890"
        self.assertEqual(
            _extract_deepseek_flaresolverr_turnstile_token(
                {"response": f'<input name="cf-turnstile-response" value="{token}">'}
            ),
            token,
        )

    def test_open_deepseek_sign_up_browser_page_prewarms_before_goto(self):
        playwright = mock.Mock()
        browser = mock.Mock()
        context = mock.Mock()
        page = mock.Mock()
        browser.new_context.return_value = context
        context.pages = []
        context.new_page.return_value = page
        events: list[str] = []
        page.goto.side_effect = lambda *args, **kwargs: events.append("goto")

        def record_prewarm(*args, **kwargs):
            events.append("prewarm")
            return {}

        with mock.patch(
            "platforms.deepseek.core._resolve_deepseek_browser_user_agent",
            return_value="Mozilla/5.0 Chrome/135.0.0.0",
        ):
            with mock.patch(
                "platforms.deepseek.core._launch_deepseek_browser",
                return_value=browser,
            ):
                with mock.patch(
                    "platforms.deepseek.core._apply_deepseek_browser_identity"
                ):
                    with mock.patch(
                        "platforms.deepseek.core._configure_deepseek_sign_up_page"
                    ):
                        with mock.patch(
                            "platforms.deepseek.core._prewarm_deepseek_session_with_flaresolverr",
                            side_effect=record_prewarm,
                        ) as prewarm_mock:
                            with mock.patch(
                                "platforms.deepseek.core._accept_deepseek_cookie_banner"
                            ):
                                _open_deepseek_sign_up_browser_page(
                                    playwright,
                                    proxy="socks5://192.168.1.18:1083",
                                    ui_locale="en-US",
                                    headless=True,
                                    flaresolverr_url="http://127.0.0.1:8191/v1",
                                    align_flaresolverr_identity=True,
                                    log_fn=lambda *_: None,
                                )

        self.assertEqual(events[:2], ["prewarm", "goto"])
        self.assertEqual(
            prewarm_mock.call_args.kwargs["target_url"],
            "https://chat.deepseek.com/sign_up",
        )
        self.assertFalse(prewarm_mock.call_args.kwargs["reload_after"])

    def test_solve_deepseek_turnstile_by_flaresolverr_reloads_and_restores_form(self):
        token = "0.flare-token-12345678901234567890"
        page = mock.Mock()
        page.url = "https://chat.deepseek.com/sign_up"

        with mock.patch(
            "platforms.deepseek.core._prewarm_deepseek_session_with_flaresolverr",
            return_value={
                "response": f'<input name="cf-turnstile-response" value="{token}">'
            },
        ) as prewarm_mock:
            with mock.patch(
                "platforms.deepseek.core._restore_deepseek_sign_up_form_after_flaresolverr_reload"
            ) as restore_mock:
                with mock.patch(
                    "platforms.deepseek.core._inject_deepseek_turnstile_token",
                    return_value=True,
                ) as inject_mock:
                    with mock.patch(
                        "platforms.deepseek.core._wait_deepseek_turnstile_token",
                        return_value="page-token-12345678901234567890",
                    ) as wait_mock:
                        resolved = _solve_deepseek_turnstile_by_flaresolverr(
                            page,
                            email="demo@example.com",
                            password="Pass!Aa1",
                            log_fn=lambda *_: None,
                            proxy="socks5://192.168.1.18:1083",
                            flaresolverr_url="http://127.0.0.1:8191/v1",
                        )

        self.assertEqual(resolved, "page-token-12345678901234567890")
        self.assertTrue(prewarm_mock.call_args.kwargs["reload_after"])
        restore_mock.assert_called_once_with(
            page,
            email="demo@example.com",
            password="Pass!Aa1",
            log_fn=mock.ANY,
            stage_label="浏览器发码前",
        )
        inject_mock.assert_called_once_with(page, token)
        wait_mock.assert_called()

    def test_solve_deepseek_turnstile_by_flaresolverr_reuses_widget_when_token_missing(self):
        page = mock.Mock()
        page.url = "https://chat.deepseek.com/sign_up"

        with mock.patch(
            "platforms.deepseek.core._prewarm_deepseek_session_with_flaresolverr",
            return_value={},
        ) as prewarm_mock:
            with mock.patch(
                "platforms.deepseek.core._restore_deepseek_sign_up_form_after_flaresolverr_reload"
            ) as restore_mock:
                with mock.patch(
                    "platforms.deepseek.core._read_deepseek_turnstile_sitekey",
                    return_value=DEEPSEEK_TURNSTILE_SITEKEY,
                ) as sitekey_mock:
                    with mock.patch(
                        "platforms.deepseek.core._has_deepseek_turnstile_runtime",
                        return_value=True,
                    ) as runtime_mock:
                        with mock.patch(
                            "platforms.deepseek.core._render_deepseek_turnstile_widget",
                            return_value=True,
                        ) as render_mock:
                            with mock.patch(
                                "platforms.deepseek.core._wait_deepseek_turnstile_token",
                                side_effect=["", "page-token-12345678901234567890"],
                            ) as wait_mock:
                                with mock.patch(
                                    "platforms.deepseek.core._reset_deepseek_turnstile_widget",
                                    return_value=True,
                                ) as reset_mock:
                                    with mock.patch(
                                        "platforms.deepseek.core._reuse_deepseek_turnstile_on_current_page",
                                        return_value="page-token-12345678901234567890",
                                    ) as reuse_mock:
                                        resolved = _solve_deepseek_turnstile_by_flaresolverr(
                                            page,
                                            email="demo@example.com",
                                            password="Pass!Aa1",
                                            log_fn=lambda *_: None,
                                            proxy="socks5://192.168.1.18:1083",
                                            flaresolverr_url="http://127.0.0.1:8191/v1",
                                        )

        self.assertEqual(resolved, "page-token-12345678901234567890")
        self.assertTrue(prewarm_mock.call_args.kwargs["reload_after"])
        restore_mock.assert_called_once()
        sitekey_mock.assert_called_once_with(page)
        runtime_mock.assert_called_once_with(page)
        render_mock.assert_called_once_with(page, DEEPSEEK_TURNSTILE_SITEKEY)
        reset_mock.assert_called_once_with(page)
        reuse_mock.assert_called_once_with(page, log_fn=mock.ANY)
        self.assertEqual(wait_mock.call_count, 1)

    def test_reuse_deepseek_turnstile_on_current_page_prefers_real_mouse_clicks(self):
        page = mock.Mock()
        frame = mock.Mock()
        frame_body = mock.Mock()
        frame.locator.return_value = frame_body
        page.mouse = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core._wait_deepseek_turnstile_token",
            side_effect=["", "page-token-12345678901234567890"],
        ) as wait_mock:
            with mock.patch(
                "platforms.deepseek.core._find_deepseek_turnstile_widget",
                return_value=(
                    frame,
                    {"x": 120.0, "y": 240.0, "width": 320.0, "height": 64.0},
                ),
            ) as find_mock:
                with mock.patch(
                    "platforms.deepseek.core._set_deepseek_turnstile_overlay_visibility"
                ) as overlay_mock:
                    with mock.patch(
                        "platforms.deepseek.core._kick_deepseek_turnstile_widget",
                        return_value="cf-overlay",
                    ) as kick_mock:
                        resolved = _reuse_deepseek_turnstile_on_current_page(
                            page,
                            log_fn=lambda *_: None,
                        )

        self.assertEqual(resolved, "page-token-12345678901234567890")
        find_mock.assert_called()
        overlay_mock.assert_called_with(page, visible=True)
        kick_mock.assert_called_once_with(page)
        frame.locator.assert_called_once_with("body")
        frame_body.click.assert_called_once()
        page.mouse.move.assert_called_once()
        page.mouse.down.assert_called_once_with()
        page.mouse.up.assert_called_once_with()
        self.assertEqual(wait_mock.call_count, 2)

    def test_build_send_code_route_injects_hcaptcha_and_guest_pow(self):
        request = mock.Mock()
        request.post_data = '{"email":"user@example.com","turnstile_token":""}'
        request.headers = {"content-type": "application/json"}
        route = mock.Mock()
        route.request = request

        handler = _build_deepseek_send_code_request_route(
            turnstile_token="turn-token",
            hcaptcha_token="hcap-token",
            guest_pow_response="pow-token",
        )
        handler(route)

        route.continue_.assert_called_once()
        kwargs = route.continue_.call_args.kwargs
        self.assertEqual(kwargs["headers"]["x-ds-guest-pow-response"], "pow-token")
        payload = __import__("json").loads(kwargs["post_data"])
        self.assertEqual(payload["turnstile_token"], "turn-token")
        self.assertEqual(payload["hcaptcha_token"], "hcap-token")

    def test_build_guest_pow_header_route_preserves_existing_pow(self):
        request = mock.Mock()
        request.headers = {"x-ds-guest-pow-response": "existing-pow"}
        route = mock.Mock()
        route.request = request

        handler = _build_deepseek_guest_pow_header_route("new-pow")
        handler(route)

        route.continue_.assert_called_once_with()

    def test_deepseek_email_domain_not_supported_detection(self):
        inner = {"biz_code": 4, "biz_msg": "EMAIL_DOMAIN_NOT_SUPPORTED"}

        self.assertTrue(_is_deepseek_email_domain_not_supported(inner))
        self.assertFalse(
            _is_deepseek_email_domain_not_supported(
                {"biz_code": 4, "biz_msg": "RECAPTCHA_VERIFY_FAILED"}
            )
        )

    def test_deepseek_email_domain_rejected_carries_domain(self):
        exc = DeepSeekEmailDomainRejected(
            "first@mail.highkay.qzz.io",
            {"biz_code": 4, "biz_msg": "EMAIL_DOMAIN_NOT_SUPPORTED"},
        )

        self.assertEqual(exc.domain, "mail.highkay.qzz.io")
        self.assertIn("mail.highkay.qzz.io", str(exc))

    def test_solve_deepseek_hcaptcha_token_uses_ohmycaptcha_compatible_solver(self):
        solver = mock.Mock()
        solver.solve_hcaptcha.return_value = "hcap-token"
        checkpoint = mock.Mock()

        token = _solve_deepseek_hcaptcha_token(
            solver,
            page_url="https://chat.deepseek.com/sign_up",
            sitekey=DEEPSEEK_HCAPTCHA_SITEKEY,
            log_fn=lambda *_: None,
            interrupt_checker=checkpoint,
        )

        self.assertEqual(token, "hcap-token")
        solver.solve_hcaptcha.assert_called_once()
        self.assertEqual(solver.solve_hcaptcha.call_args.args[1], DEEPSEEK_HCAPTCHA_SITEKEY)
        self.assertEqual(
            solver.solve_hcaptcha.call_args.kwargs["timeout_seconds"],
            180.0,
        )
        self.assertIs(
            solver.solve_hcaptcha.call_args.kwargs["interrupt_checker"],
            checkpoint,
        )

    def test_resolve_send_code_challenge_tokens_reads_existing_hcaptcha_token(self):
        solver = mock.Mock()
        page = mock.Mock()
        checkpoint = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core._read_deepseek_turnstile_sitekey",
            return_value=DEEPSEEK_TURNSTILE_SITEKEY,
        ) as sitekey_mock:
            with mock.patch(
                "platforms.deepseek.core._solve_deepseek_turnstile_token",
                return_value="",
            ) as solve_turnstile_mock:
                with mock.patch(
                    "platforms.deepseek.core._solve_deepseek_turnstile_by_flaresolverr",
                    return_value="",
                ) as flare_mock:
                    with mock.patch(
                        "platforms.deepseek.core._read_deepseek_hcaptcha_token",
                        return_value="hcap-token",
                    ) as read_hcaptcha_mock:
                        with mock.patch(
                            "platforms.deepseek.core._solve_deepseek_hcaptcha_token"
                        ) as solve_hcaptcha_mock:
                            turnstile_token, hcaptcha_token = (
                                _resolve_deepseek_send_code_challenge_tokens(
                                    page=page,
                                    email="user@example.com",
                                    password="Pass!Aa1",
                                    sign_up_url="https://chat.deepseek.com/sign_up",
                                    captcha_solver=solver,
                                    hcaptcha_sitekey=DEEPSEEK_HCAPTCHA_SITEKEY,
                                    proxy="socks5://192.168.1.18:1080",
                                    flaresolverr_url="http://127.0.0.1:8191/v1",
                                    log_fn=lambda *_: None,
                                    interrupt_checker=checkpoint,
                                )
                            )

        self.assertEqual(turnstile_token, "")
        self.assertEqual(hcaptcha_token, "hcap-token")
        sitekey_mock.assert_called_once_with(page)
        solve_turnstile_mock.assert_called_once()
        flare_mock.assert_called_once()
        read_hcaptcha_mock.assert_called_once_with(page)
        solve_hcaptcha_mock.assert_not_called()

    def test_resolve_send_code_challenge_tokens_falls_back_to_solver_hcaptcha_token(self):
        solver = mock.Mock()
        page = mock.Mock()
        checkpoint = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core._read_deepseek_turnstile_sitekey",
            return_value="",
        ) as sitekey_mock:
            with mock.patch(
                "platforms.deepseek.core._solve_deepseek_turnstile_token"
            ) as solve_turnstile_mock:
                with mock.patch(
                    "platforms.deepseek.core._solve_deepseek_turnstile_by_flaresolverr"
                ) as flare_mock:
                    with mock.patch(
                        "platforms.deepseek.core._read_deepseek_hcaptcha_token",
                        return_value="",
                    ) as read_hcaptcha_mock:
                        with mock.patch(
                            "platforms.deepseek.core._solve_deepseek_hcaptcha_token",
                            return_value="hcap-token",
                        ) as solve_hcaptcha_mock:
                            turnstile_token, hcaptcha_token = (
                                _resolve_deepseek_send_code_challenge_tokens(
                                    page=page,
                                    email="user@example.com",
                                    password="Pass!Aa1",
                                    sign_up_url="https://chat.deepseek.com/sign_up",
                                    captcha_solver=solver,
                                    hcaptcha_sitekey=DEEPSEEK_HCAPTCHA_SITEKEY,
                                    proxy="socks5://192.168.1.18:1080",
                                    flaresolverr_url=None,
                                    log_fn=lambda *_: None,
                                    interrupt_checker=checkpoint,
                                )
                            )

        self.assertEqual(turnstile_token, "")
        self.assertEqual(hcaptcha_token, "hcap-token")
        sitekey_mock.assert_called_once_with(page)
        solve_turnstile_mock.assert_not_called()
        flare_mock.assert_not_called()
        read_hcaptcha_mock.assert_called_once_with(page)
        solve_hcaptcha_mock.assert_called_once_with(
            solver,
            page_url="https://chat.deepseek.com/sign_up",
            sitekey=DEEPSEEK_HCAPTCHA_SITEKEY,
            log_fn=mock.ANY,
            interrupt_checker=checkpoint,
        )

    def test_read_deepseek_turnstile_sitekey_uses_runtime_fallback(self):
        page = mock.Mock()
        page.evaluate.side_effect = ["", True]
        page.frames = []

        self.assertEqual(
            _read_deepseek_turnstile_sitekey(page),
            DEEPSEEK_TURNSTILE_SITEKEY,
        )

    def test_solve_deepseek_turnstile_token_prefers_session_solver(self):
        solver = mock.Mock()
        solver.solve_turnstile_session.return_value = {
            "token": "turn-token",
            "solverMode": "session",
            "attempts": 1,
        }
        page = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core._render_deepseek_turnstile_widget",
            return_value=False,
        ):
            with mock.patch(
                "platforms.deepseek.core._collect_deepseek_turnstile_session_state",
                return_value={"cookies": []},
            ):
                with mock.patch(
                    "platforms.deepseek.core._collect_deepseek_turnstile_widget_hints",
                    return_value={"responseInputSelector": 'input[name="cf-turnstile-response"]'},
                ):
                    with mock.patch(
                        "platforms.deepseek.core._collect_deepseek_turnstile_runtime_hints",
                        return_value={"stepLabel": "deepseek_send_code"},
                    ):
                        with mock.patch(
                            "platforms.deepseek.core._collect_deepseek_turnstile_solver_proxy",
                            return_value={"server": "socks5://127.0.0.1:1080"},
                        ):
                            with mock.patch(
                                "platforms.deepseek.core._inject_deepseek_turnstile_token",
                                return_value=True,
                            ):
                                with mock.patch(
                                    "platforms.deepseek.core._wait_deepseek_turnstile_token",
                                    side_effect=["", "turn-token"],
                                ):
                                    token = _solve_deepseek_turnstile_token(
                                        solver,
                                        page=page,
                                        page_url="https://chat.deepseek.com/sign_up",
                                        sitekey=DEEPSEEK_TURNSTILE_SITEKEY,
                                        proxy="socks5://127.0.0.1:1080",
                                        log_fn=lambda *_: None,
                                        interrupt_checker=None,
                                    )

        self.assertEqual(token, "turn-token")
        solver.solve_turnstile_session.assert_called_once()
        solver.solve_turnstile.assert_not_called()

    def test_solve_deepseek_turnstile_token_falls_back_to_proxyless_solver(self):
        solver = mock.Mock()
        solver.solve_turnstile.return_value = "turn-token"
        page = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core._render_deepseek_turnstile_widget",
            return_value=False,
        ):
            with mock.patch(
                "platforms.deepseek.core._inject_deepseek_turnstile_token",
                return_value=False,
            ):
                token = _solve_deepseek_turnstile_token(
                    solver,
                    page=page,
                    page_url="https://chat.deepseek.com/sign_up",
                    sitekey=DEEPSEEK_TURNSTILE_SITEKEY,
                    proxy=None,
                    log_fn=lambda *_: None,
                    interrupt_checker=None,
                )

        self.assertEqual(token, "turn-token")
        solver.solve_turnstile.assert_called_once()

    def test_encode_guest_pow_response_in_page_uses_existing_browser_page(self):
        page = mock.Mock()
        page.evaluate.return_value = {"salt": "salt-1", "answer": 42}

        token = _encode_deepseek_guest_pow_response_in_page(
            page,
            {
                "algorithm": "DeepSeekHashV1",
                "challenge": "challenge-1",
                "salt": "salt-1",
                "difficulty": 1,
                "signature": "sig-1",
                "expire_at": 1893456000,
            },
            pow_worker_url="https://worker.example/pow.js",
        )

        self.assertEqual(token, "eyJzYWx0Ijoic2FsdC0xIiwiYW5zd2VyIjo0Mn0=")
        page.evaluate.assert_called_once()
        self.assertEqual(
            page.evaluate.call_args.args[1]["workerUrl"],
            "https://worker.example/pow.js",
        )

    def test_encode_guest_pow_response_with_context_page_uses_worker_origin_page(self):
        page = mock.Mock()
        pow_page = mock.Mock()
        page.context.new_page.return_value = pow_page
        pow_page.evaluate.return_value = {"salt": "salt-1", "answer": 42}

        token = _encode_deepseek_guest_pow_response_with_context_page(
            page,
            {
                "algorithm": "DeepSeekHashV1",
                "challenge": "challenge-1",
                "salt": "salt-1",
                "difficulty": 1,
                "signature": "sig-1",
                "expire_at": 1893456000,
            },
            pow_worker_url="https://fe-static.deepseek.com/chat/static/worker.js",
        )

        self.assertEqual(token, "eyJzYWx0Ijoic2FsdC0xIiwiYW5zd2VyIjo0Mn0=")
        page.context.new_page.assert_called_once_with()
        pow_page.goto.assert_called_once_with(
            DEEPSEEK_POW_WORKER_HOST_PAGE_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        self.assertEqual(
            pow_page.evaluate.call_args.args[1]["workerUrl"],
            "https://fe-static.deepseek.com/chat/static/worker.js",
        )
        pow_page.close.assert_called_once_with()

    def test_send_email_code_passes_hcaptcha_and_guest_pow(self):
        client = DeepSeekClient(log_fn=lambda *_: None, ui_locale="en-US")
        client._device_id = "deepseek-device"
        try:
            with mock.patch.object(client, "_ensure_settings") as ensure_settings:
                with mock.patch.object(client, "_post", return_value={"data": {"biz_code": 0}}) as post:
                    client.send_email_code(
                        email="user@example.com",
                        scenario="register",
                        hcaptcha_token="hcap-token",
                    )

            ensure_settings.assert_called_once()
            post.assert_called_once()
            self.assertEqual(post.call_args.args[0], "/create_email_verification_code")
            payload = post.call_args.args[1]
            self.assertEqual(payload["hcaptcha_token"], "hcap-token")
            self.assertEqual(payload["locale"], "en_US")
            self.assertEqual(payload["device_id"], "deepseek-device")
            self.assertTrue(post.call_args.kwargs["include_guest_pow"])
            self.assertEqual(
                post.call_args.kwargs["guest_target_path"],
                "/api/v0/users/create_email_verification_code",
            )
        finally:
            client.close()

    def test_request_deepseek_flaresolverr_solution_retries_until_token(self):
        token = "0.flare-token-12345678901234567890"
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
                    {"name": "ds_session_id", "value": "session"},
                ]
            },
        }
        second_resp = mock.Mock()
        second_resp.raise_for_status.return_value = None
        second_resp.json.return_value = {
            "status": "ok",
            "solution": {
                "response": (
                    f'<input name="cf-turnstile-response" value="{token}">'
                ),
                "cookies": [
                    {"name": "__cf_bm", "value": "bm"},
                    {"name": "ds_session_id", "value": "session"},
                ],
            },
        }
        destroy_resp = mock.Mock()
        session = mock.Mock()
        session.post.side_effect = [create_resp, first_resp, second_resp, destroy_resp]

        with mock.patch("platforms.deepseek.core.requests.Session", return_value=session):
            with mock.patch("platforms.deepseek.core.time.sleep") as sleep_mock:
                solution = _request_deepseek_flaresolverr_solution(
                    log_fn=lambda *_: None,
                    stage_label="浏览器发码前",
                    target_url="https://chat.deepseek.com/sign_up",
                    flaresolverr_url="http://127.0.0.1:8191/v1",
                )

        self.assertEqual(
            _extract_deepseek_flaresolverr_turnstile_token(solution),
            token,
        )
        self.assertEqual(session.post.call_count, 4)
        sleep_mock.assert_called_once_with(1.0)

    def test_request_deepseek_flaresolverr_solution_surfaces_http_error_body(self):
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

        with mock.patch("platforms.deepseek.core.requests.Session", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "Cloudflare IP is banned"):
                _request_deepseek_flaresolverr_solution(
                    log_fn=lambda *_: None,
                    stage_label="浏览器发码前",
                    target_url="https://chat.deepseek.com/sign_up",
                    flaresolverr_url="http://127.0.0.1:8191/v1",
                )

        self.assertEqual(session.post.call_count, 3)

    def test_wait_for_manual_send_code_success_retries_until_response_succeeds(self):
        failed_response = mock.Mock()
        failed_response.request.method = "POST"
        failed_response.url = (
            "https://chat.deepseek.com/api/v0/users/create_email_verification_code"
        )
        failed_response.status = 200
        failed_response.json.return_value = {
            "data": {
                "biz_code": 2,
                "biz_msg": "RECAPTCHA_VERIFY_FAILED",
            }
        }
        success_response = mock.Mock()
        success_response.request.method = "POST"
        success_response.url = (
            "https://chat.deepseek.com/api/v0/users/create_email_verification_code"
        )
        success_response.status = 200
        success_response.json.return_value = {
            "data": {
                "biz_code": 0,
                "biz_data": {"send_window_secs": 60},
            }
        }
        page = mock.Mock()
        page.is_closed.return_value = False
        page.wait_for_response.side_effect = [failed_response, success_response]
        body = mock.Mock()
        body.inner_text.return_value = "Resend after 59s"
        page.locator.return_value = body
        send_code_button = mock.Mock()
        send_code_button.inner_text.return_value = "Resend after 59s"
        logs: list[str] = []
        checkpoint = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core.time.monotonic",
            side_effect=[0.0, 0.1, 0.2],
        ):
            response, attempts = _wait_for_deepseek_manual_send_code_success(
                page=page,
                email="user@example.com",
                send_code_button=send_code_button,
                timeout_seconds=120,
                log_fn=logs.append,
                interrupt_checker=checkpoint,
            )

        self.assertIs(response, success_response)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            attempts[0]["response"]["data"]["biz_msg"],
            "RECAPTCHA_VERIFY_FAILED",
        )
        self.assertTrue(attempts[0]["page_state"]["has_resend_countdown"])
        self.assertEqual(attempts[1]["response"]["data"]["biz_code"], 0)
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        self.assertEqual(checkpoint.call_count, 2)
        self.assertTrue(any("继续等待下一次点击" in entry for entry in logs))

    def test_wait_for_manual_send_code_success_times_out_without_success_response(self):
        page = mock.Mock()
        page.is_closed.return_value = False
        page.wait_for_response.side_effect = TimeoutError("still waiting")
        send_code_button = mock.Mock()
        checkpoint = mock.Mock()

        with mock.patch(
            "platforms.deepseek.core.time.monotonic",
            side_effect=[0.0, 0.1, 0.2, 31.0],
        ):
            with self.assertRaisesRegex(TimeoutError, "倒计时不代表成功"):
                _wait_for_deepseek_manual_send_code_success(
                    page=page,
                    email="user@example.com",
                    send_code_button=send_code_button,
                    timeout_seconds=30,
                    log_fn=lambda *_: None,
                    interrupt_checker=checkpoint,
                )

        self.assertEqual(page.wait_for_response.call_count, 2)
        self.assertEqual(checkpoint.call_count, 2)

    def test_classify_deepseek_sign_up_state_detects_email_form(self):
        state = {
            "body": "DeepSeek sign up",
            "inputs": [
                {"type": "email"},
                {"type": "password"},
                {"type": "password"},
                {"type": "tel"},
            ],
            "buttons": [
                {"className": "ds-link-button ds-verify-code-input-countdown"},
            ],
        }

        self.assertEqual(_classify_deepseek_sign_up_state(state), "email_form")

    def test_classify_deepseek_sign_up_state_detects_phone_only_branch(self):
        state = {
            "title": "DeepSeek - Into the Unknown",
            "url": "https://chat.deepseek.com/sign_up",
            "body": (
                "Only phone number registration is supported in your region.\n"
                "+86\nSend code\nSign up"
            ),
            "inputs": [
                {"type": "tel"},
                {"type": "password"},
                {"type": "password"},
                {"type": "tel"},
            ],
            "buttons": [
                {"className": "ds-link-button ds-verify-code-input-countdown"},
            ],
        }

        self.assertEqual(_classify_deepseek_sign_up_state(state), "phone_only")
        summary = _summarize_deepseek_sign_up_state(state, classification="phone_only")
        self.assertIn("classification=phone_only", summary)
        self.assertIn("Only phone number registration is supported", summary)


if __name__ == "__main__":
    unittest.main()
