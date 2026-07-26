#!/usr/bin/env python3
# ruff: noqa: E402, E501
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from core.base_captcha import YesCaptcha
from core.base_mailbox import create_mailbox
from core.browser_backend import sync_playwright
from core.config_store import config_store
from platforms.deepseek.core import (
    DEEPSEEK_DEFAULT_TIMEZONE_ID,
    DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
    DEEPSEEK_HCAPTCHA_SITEKEY,
    USER_AGENT,
    _classify_deepseek_sign_up_state,
    _collect_deepseek_form_state,
    _configure_deepseek_sign_up_page,
    _fill_deepseek_input,
    _launch_deepseek_browser,
    _read_deepseek_hcaptcha_token,
    _request_deepseek_guest_pow_response_via_browser,
    _solve_deepseek_hcaptcha_token,
    _summarize_deepseek_sign_up_state,
    _wait_for_deepseek_sign_up_form,
    build_deepseek_accept_language,
    build_deepseek_page_url,
    extract_deepseek_client_locale,
    normalize_deepseek_ui_locale,
    random_password,
)
from scripts.probe_deepseek_hcaptcha_natural import (
    _collect_challenge_resource_entries,
    _dump_hcaptcha_state,
    _sanitize_payload,
)

DEEPSEEK_SEND_CODE_PATH = "/api/v0/users/create_email_verification_code"
DEFAULT_ARTIFACT = "docs/artifacts/deepseek-send-code-solver-probe.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe DeepSeek send-code flow with external hCaptcha solver."
    )
    parser.add_argument("--proxy", default="socks5://192.168.1.18:1080")
    parser.add_argument("--ui-locale", default="en-US")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--mail-provider", default="outlookemail")
    parser.add_argument("--mail-domain", default="")
    parser.add_argument("--mail-timeout", type=int, default=90)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT)
    return parser.parse_args()


def _mask_token(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return value
    if len(text) < 40:
        return value
    return {"present": True, "length": len(text), "prefix": text[:24]}


def _extract_inner_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    return {
        "biz_code": data.get("biz_code"),
        "biz_msg": data.get("biz_msg"),
        "biz_data": _sanitize_payload(data.get("biz_data")),
    }


def _derive_device_id_from_cookies(cookies: list[dict[str, Any]]) -> str:
    for item in cookies:
        name = str(item.get("name") or "")
        if not name.startswith(".thumbcache_"):
            continue
        raw_value = str(item.get("value") or "").strip()
        if not raw_value:
            continue
        decoded = unquote(raw_value)
        if not decoded:
            continue
        return decoded if decoded.startswith("B") else f"B{decoded}"
    return ""


def _submit_send_code_request(
    page,
    *,
    email: str,
    client_locale: str,
    hcaptcha_token: str,
    device_id: str,
    guest_pow_response: str,
    tz_offset_seconds: str,
) -> dict[str, Any]:
    return page.evaluate(
        """async ({ email, clientLocale, hcaptchaToken, deviceId, guestPowResponse, tzOffset }) => {
            const response = await fetch('/api/v0/users/create_email_verification_code', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'accept': '*/*',
                    'content-type': 'application/json',
                    'x-app-version': '2.0.0',
                    'x-client-locale': clientLocale,
                    'x-client-platform': 'web',
                    'x-client-timezone-offset': tzOffset,
                    'x-client-version': '2.0.0',
                    'x-ds-guest-pow-response': guestPowResponse
                },
                body: JSON.stringify({
                    email,
                    turnstile_token: '',
                    locale: clientLocale,
                    hcaptcha_token: hcaptchaToken,
                    device_id: deviceId,
                    scenario: 'register'
                })
            });
            const text = await response.text();
            let body = text;
            try {
                body = JSON.parse(text);
            } catch (_) {}
            return {
                status: response.status,
                ok: response.ok,
                body
            };
        }""",
        {
            "email": email,
            "clientLocale": client_locale,
            "hcaptchaToken": hcaptcha_token,
            "deviceId": device_id,
            "guestPowResponse": guest_pow_response,
            "tzOffset": str(tz_offset_seconds or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS),
        },
    )


def _probe_solver_health(api_base: str) -> dict[str, Any]:
    url = f"{str(api_base).rstrip('/')}/api/v1/health"
    try:
        response = requests.get(url, timeout=3)
        payload = response.json() if response.text else {}
        return {"ok": response.ok, "status_code": response.status_code, "body": payload}
    except Exception as exc:  # pragma: no cover - probe only
        return {"ok": False, "error": repr(exc), "url": url}


def _build_mailbox_extra(mail_domain: str) -> dict[str, Any]:
    extra = config_store.get_all().copy()
    extra["mail_provider"] = str(extra.get("mail_provider") or "outlookemail").strip()
    domain = str(mail_domain or "").strip().lstrip("@")
    if not domain:
        return extra
    for key in (
        "imail_domain",
        "edumail_domain",
        "boomlify_domain",
        "nullsto_domain",
        "gptmail_domain",
        "maliapi_domain",
        "duckmail_domain",
        "skymail_domain",
        "cloudmail_domain",
        "freemail_domain",
        "opentrashmail_domain",
        "cfrouting_domain",
        "cfworker_domain",
        "outlookemail_domain",
    ):
        extra[key] = domain
    return extra


def _collect_runtime_snapshot(page) -> dict[str, Any]:
    return page.evaluate(
        """() => ({
            url: location.href,
            userAgent: navigator.userAgent,
            language: navigator.language,
            languages: Array.from(navigator.languages || []),
            timezone: (() => {
                try {
                    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
                } catch (_) {
                    return '';
                }
            })(),
            timezoneOffset: new Date().getTimezoneOffset(),
            hasTurnstile: !!window.turnstile,
            hasHcaptcha: !!window.hcaptcha,
            scripts: Array.from(document.scripts || []).map((node) => String(node.src || '')).filter(Boolean),
            iframes: Array.from(document.querySelectorAll('iframe')).map((node) => String(node.src || '')).filter(Boolean),
        })""",
    )


def _open_sign_up_page(playwright, *, proxy: str | None, ui_locale: str, headless: bool):
    sign_up_url = build_deepseek_page_url("/sign_up", ui_locale)
    normalized_locale = normalize_deepseek_ui_locale(ui_locale)
    browser = _launch_deepseek_browser(playwright, headless=headless, proxy=proxy)
    context = browser.new_context(
        locale=normalized_locale,
        user_agent=USER_AGENT,
        timezone_id=DEEPSEEK_DEFAULT_TIMEZONE_ID,
        viewport={"width": 1440, "height": 1080},
    )
    context.set_extra_http_headers(
        {"Accept-Language": build_deepseek_accept_language(normalized_locale)}
    )
    page = context.new_page()
    _configure_deepseek_sign_up_page(page, ui_locale=normalized_locale)
    page.goto(sign_up_url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(5000)
    try:
        from platforms.deepseek.core import _accept_deepseek_cookie_banner

        _accept_deepseek_cookie_banner(page)
    except Exception:
        pass
    return browser, context, page, sign_up_url


def main() -> int:
    args = _parse_args()
    artifact_path = ROOT / args.artifact
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    mail_provider = str(args.mail_provider or "").strip() or "outlookemail"
    password = str(args.password or "").strip() or random_password(12)
    email = str(args.email or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "proxy": args.proxy,
        "ui_locale": args.ui_locale,
        "mail_provider": mail_provider,
    }

    mailbox = None
    mail_account = None
    before_ids: set[str] | None = None
    if not email:
        extra = _build_mailbox_extra(args.mail_domain)
        extra["mail_provider"] = mail_provider
        mailbox = create_mailbox(
            provider=mail_provider,
            extra=extra,
            proxy=str(args.proxy or "").strip() or None,
            platform="deepseek",
        )
        mail_account = mailbox.get_email()
        email = str(getattr(mail_account, "email", "") or "").strip()
        try:
            before_ids = set(mailbox.get_current_ids(mail_account))
        except Exception:
            before_ids = None
    if not email:
        raise SystemExit("No email available for probe")

    solver_key = str(config_store.get("yescaptcha_key", "") or "").strip()
    solver_api_base = str(config_store.get("yescaptcha_api_base", "") or "").strip()
    if not solver_key:
        raise SystemExit("yescaptcha_key missing in config")
    solver = YesCaptcha(solver_key, api_base=solver_api_base or None)
    result["solver_api"] = solver.api
    result["solver_health"] = _probe_solver_health(solver.api)

    browser = None
    context = None
    page = None
    requests_seen: list[dict[str, Any]] = []
    responses_seen: list[dict[str, Any]] = []
    request_failed_seen: list[dict[str, Any]] = []
    console_messages: list[dict[str, Any]] = []

    def remember_request(request) -> None:
        url = str(request.url or "")
        interesting = any(
            marker in url.lower()
            for marker in (
                DEEPSEEK_SEND_CODE_PATH,
                "/api/v0/users/create_guest_challenge",
                "js.hcaptcha.com",
                "turnstile",
                "hcaptcha",
                "13022.",
            )
        )
        if not interesting:
            return
        entry: dict[str, Any] = {"method": request.method, "url": url}
        try:
            if request.method.upper() == "POST":
                entry["post_data"] = _sanitize_payload(request.post_data_json)
        except Exception:
            try:
                entry["post_data_text"] = str(request.post_data or "")[:600]
            except Exception:
                pass
        requests_seen.append(entry)

    def remember_response(response) -> None:
        url = str(response.url or "")
        interesting = any(
            marker in url.lower()
            for marker in (
                DEEPSEEK_SEND_CODE_PATH,
                "/api/v0/users/create_guest_challenge",
                "js.hcaptcha.com",
                "turnstile",
                "hcaptcha",
                "13022.",
            )
        )
        if not interesting:
            return
        entry: dict[str, Any] = {"status": response.status, "url": url}
        try:
            headers = response.headers or {}
            if "content-type" in headers:
                entry["content_type"] = headers.get("content-type")
        except Exception:
            pass
        try:
            if DEEPSEEK_SEND_CODE_PATH in url or "/api/v0/users/create_guest_challenge" in url:
                entry["body"] = _sanitize_payload(response.json())
        except Exception:
            try:
                entry["body_text"] = str(response.text() or "")[:1200]
            except Exception:
                pass
        responses_seen.append(entry)

    def remember_failed(request) -> None:
        url = str(request.url or "")
        if not any(marker in url.lower() for marker in ("hcaptcha", "turnstile", "create_email_verification_code")):
            return
        request_failed_seen.append(
            {
                "url": url,
                "failure": request.failure,
            }
        )

    try:
        with sync_playwright() as playwright:
            browser, context, page, sign_up_url = _open_sign_up_page(
                playwright,
                proxy=str(args.proxy or "").strip() or None,
                ui_locale=args.ui_locale,
                headless=not bool(args.headed),
            )
            page.on("request", remember_request)
            page.on("response", remember_response)
            page.on("requestfailed", remember_failed)
            page.on(
                "console",
                lambda msg: console_messages.append(
                    {"type": msg.type, "text": str(msg.text or "")[:500]}
                ),
            )

            _wait_for_deepseek_sign_up_form(page)
            before_state = _collect_deepseek_form_state(page)
            before_classification = _classify_deepseek_sign_up_state(before_state)
            if before_classification != "email_form":
                raise RuntimeError(
                    "DeepSeek 注册页不是邮箱表单: "
                    + _summarize_deepseek_sign_up_state(
                        before_state, classification=before_classification
                    )
                )

            runtime_before = _collect_runtime_snapshot(page)
            before_hcaptcha = _dump_hcaptcha_state(page)
            before_resources = _collect_challenge_resource_entries(page)

            email_input = page.locator(
                'input.ds-input__input[type="text"], input.ds-input__input[type="email"]'
            ).first
            password_inputs = page.locator('input.ds-input__input[type="password"]')

            _fill_deepseek_input(email_input, email, field_name="email")
            _fill_deepseek_input(password_inputs.nth(0), password, field_name="password")
            _fill_deepseek_input(
                password_inputs.nth(1), password, field_name="confirm_password"
            )

            client_locale = extract_deepseek_client_locale(args.ui_locale)
            cookies_before_send = context.cookies([sign_up_url])
            device_id = _derive_device_id_from_cookies(cookies_before_send)

            if not device_id:
                try:
                    page.locator("button.ds-verify-code-input-countdown").first.click(
                        timeout=3000
                    )
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                cookies_before_send = context.cookies([sign_up_url])
                device_id = _derive_device_id_from_cookies(cookies_before_send)

            page_hcaptcha_token = str(_read_deepseek_hcaptcha_token(page) or "").strip()

            solver_start = time.time()
            solved_hcaptcha_token = ""
            solver_error = ""
            if not page_hcaptcha_token:
                try:
                    solved_hcaptcha_token = _solve_deepseek_hcaptcha_token(
                        solver,
                        page_url=sign_up_url,
                        sitekey=DEEPSEEK_HCAPTCHA_SITEKEY,
                        log_fn=print,
                    )
                except Exception as exc:
                    solver_error = repr(exc)
            hcaptcha_token = page_hcaptcha_token or solved_hcaptcha_token
            solver_elapsed = round(time.time() - solver_start, 2)

            guest_pow_response = ""
            guest_pow_error = ""
            try:
                guest_pow_response = _request_deepseek_guest_pow_response_via_browser(
                    page,
                    target_path=DEEPSEEK_SEND_CODE_PATH,
                    proxy=str(args.proxy or "").strip() or None,
                    ui_locale=args.ui_locale,
                    sign_up_url=sign_up_url,
                    tz_offset_seconds=DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
                )
            except Exception as exc:
                guest_pow_error = repr(exc)

            manual_send_response: Any = None
            manual_send_error = ""
            response_source = ""
            response_payload: Any = None
            sent_at = time.time()
            if hcaptcha_token and device_id and guest_pow_response:
                try:
                    manual_send_response = _submit_send_code_request(
                        page,
                        email=email,
                        client_locale=client_locale,
                        hcaptcha_token=hcaptcha_token,
                        device_id=device_id,
                        guest_pow_response=guest_pow_response,
                        tz_offset_seconds=DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
                    )
                    response_payload = (
                        manual_send_response.get("body")
                        if isinstance(manual_send_response, dict)
                        else manual_send_response
                    )
                    response_source = "manual_fetch"
                except Exception as exc:
                    manual_send_error = repr(exc)
            else:
                missing = []
                if not hcaptcha_token:
                    missing.append("hcaptcha_token")
                if not device_id:
                    missing.append("device_id")
                if not guest_pow_response:
                    missing.append("guest_pow_response")
                manual_send_error = "missing prerequisites: " + ", ".join(missing)

            inner = _extract_inner_response(response_payload)
            after_state = _collect_deepseek_form_state(page)
            after_hcaptcha = _dump_hcaptcha_state(page)
            after_resources = _collect_challenge_resource_entries(page)
            runtime_after = _collect_runtime_snapshot(page)

            mail_result: dict[str, Any] | None = None
            if inner.get("biz_code") == 0 and mailbox and mail_account:
                try:
                    code = mailbox.wait_for_code(
                        mail_account,
                        keyword="DeepSeek",
                        timeout=max(1, int(args.mail_timeout)),
                        before_ids=before_ids,
                        otp_sent_at=sent_at,
                    )
                    mail_result = {"ok": True, "code": code}
                except Exception as exc:
                    mail_result = {"ok": False, "error": repr(exc)}

            screenshot_path = artifact_path.with_suffix(".png")
            page.screenshot(path=str(screenshot_path), full_page=True)

            result.update(
                {
                    "ok": bool(inner.get("biz_code") == 0),
                    "email": email,
                    "password": "***",
                    "sign_up_url": sign_up_url,
                    "client_locale": client_locale,
                    "before_classification": before_classification,
                    "before_summary": _summarize_deepseek_sign_up_state(
                        before_state, classification=before_classification
                    ),
                    "before_hcaptcha": before_hcaptcha,
                    "before_resources": before_resources,
                    "runtime_before": runtime_before,
                    "page_hcaptcha_token": _mask_token(page_hcaptcha_token),
                    "solved_hcaptcha_token": _mask_token(solved_hcaptcha_token),
                    "solver_error": solver_error,
                    "solver_elapsed_secs": solver_elapsed,
                    "guest_pow": {
                        "present": bool(guest_pow_response),
                        "length": len(guest_pow_response or ""),
                        "prefix": str(guest_pow_response or "")[:24],
                        "error": guest_pow_error,
                    },
                    "device_id": _mask_token(device_id),
                    "manual_send_response": _sanitize_payload(manual_send_response),
                    "manual_send_error": manual_send_error,
                    "response_source": response_source,
                    "response_inner": _sanitize_payload(inner),
                    "after_state": after_state,
                    "after_hcaptcha": after_hcaptcha,
                    "after_resources": after_resources,
                    "runtime_after": runtime_after,
                    "cookies_before_send": [
                        {
                            "name": item.get("name"),
                            "value": _mask_token(item.get("value")),
                        }
                        for item in cookies_before_send
                    ],
                    "requests_seen": requests_seen,
                    "responses_seen": responses_seen,
                    "request_failed_seen": request_failed_seen,
                    "console_messages": console_messages,
                    "screenshot": str(screenshot_path.relative_to(ROOT)),
                }
            )
            if mail_result is not None:
                result["mail_result"] = mail_result
    except Exception as exc:
        result["error"] = repr(exc)
        if page is not None:
            try:
                result["fatal_state"] = _collect_deepseek_form_state(page)
            except Exception as state_exc:
                result["fatal_state_error"] = repr(state_exc)
            try:
                result["fatal_hcaptcha"] = _dump_hcaptcha_state(page)
            except Exception as hcaptcha_exc:
                result["fatal_hcaptcha_error"] = repr(hcaptcha_exc)
            try:
                result["runtime_after"] = _collect_runtime_snapshot(page)
            except Exception as runtime_exc:
                result["runtime_after_error"] = repr(runtime_exc)
        result["requests_seen"] = requests_seen
        result["responses_seen"] = responses_seen
        result["request_failed_seen"] = request_failed_seen
        result["console_messages"] = console_messages
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    artifact_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(_sanitize_payload(result), ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
