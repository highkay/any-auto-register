#!/usr/bin/env python3
# ruff: noqa: E402, E501
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_mailbox import create_mailbox
from core.browser_backend import sync_playwright
from core.config_store import config_store
from platforms.deepseek.core import (
    DEEPSEEK_DEFAULT_TIMEZONE_ID,
    USER_AGENT,
    _accept_deepseek_cookie_banner,
    _apply_deepseek_browser_identity,
    _classify_deepseek_sign_up_state,
    _collect_deepseek_form_state,
    _configure_deepseek_sign_up_page,
    _fill_deepseek_input,
    _launch_deepseek_browser,
    _prewarm_deepseek_session_with_flaresolverr,
    _resolve_deepseek_browser_user_agent,
    _summarize_deepseek_sign_up_state,
    _wait_for_deepseek_sign_up_form,
    build_deepseek_accept_language,
    build_deepseek_page_url,
    random_password,
)

HCAPTCHA_HOOK = r"""() => {
    const events = [];
    const tokenSamples = [];
    const push = (type, payload = {}) => {
        const item = { ts: Date.now(), type, payload };
        events.push(item);
        if (events.length > 200) events.shift();
    };
    const summarizeToken = (value) => {
        const text = String(value || '').trim();
        if (!text) return { present: false, length: 0, prefix: '' };
        return { present: true, length: text.length, prefix: text.slice(0, 24) };
    };
    const rememberToken = (source, value) => {
        const token = summarizeToken(value);
        if (token.present) {
            tokenSamples.push({ ts: Date.now(), source, token });
            if (tokenSamples.length > 50) tokenSamples.shift();
        }
        return token;
    };
    const wrapCallback = (name, fn) => {
        if (typeof fn !== 'function') return fn;
        return function(...args) {
            try { push(`callback:${name}`, { args: args.map((arg) => summarizeToken(arg)) }); } catch (_) {}
            try { rememberToken(`callback:${name}`, args[0]); } catch (_) {}
            return fn.apply(this, args);
        };
    };
    const wrapApi = (api) => {
        if (!api || api.__deepseekProbeWrapped) return api;
        try {
            const originalRender = api.render;
            if (typeof originalRender === 'function') {
                api.render = function(container, params, ...rest) {
                    try {
                        push('render', {
                            container: String(container && (container.id || container.className || container.tagName) || container || '').slice(0, 120),
                            paramKeys: params && typeof params === 'object' ? Object.keys(params) : [],
                            sitekey: params && typeof params === 'object' ? String(params.sitekey || '') : ''
                        });
                        if (params && typeof params === 'object') {
                            for (const key of ['callback', 'error-callback', 'expired-callback', 'open-callback', 'close-callback', 'chalexpired-callback']) {
                                if (typeof params[key] === 'function') params[key] = wrapCallback(key, params[key]);
                            }
                        }
                    } catch (_) {}
                    return originalRender.call(this, container, params, ...rest);
                };
            }
            const originalExecute = api.execute;
            if (typeof originalExecute === 'function') {
                api.execute = function(...args) {
                    try { push('execute:call', { argTypes: args.map((arg) => typeof arg) }); } catch (_) {}
                    const result = originalExecute.apply(this, args);
                    try {
                        if (result && typeof result.then === 'function') {
                            return result.then((value) => {
                                try {
                                    push('execute:resolved', {
                                        resultType: typeof value,
                                        token: value && typeof value === 'object' ? summarizeToken(value.response) : summarizeToken(value)
                                    });
                                    rememberToken('execute:resolved', value && typeof value === 'object' ? value.response : value);
                                } catch (_) {}
                                return value;
                            }, (err) => {
                                try { push('execute:rejected', { message: String(err && err.message || err || '').slice(0, 300) }); } catch (_) {}
                                throw err;
                            });
                        }
                    } catch (_) {}
                    try { push('execute:return', { token: summarizeToken(result) }); rememberToken('execute:return', result); } catch (_) {}
                    return result;
                };
            }
            const originalGetResponse = api.getResponse;
            if (typeof originalGetResponse === 'function') {
                api.getResponse = function(...args) {
                    const value = originalGetResponse.apply(this, args);
                    try { push('getResponse', { token: summarizeToken(value) }); rememberToken('getResponse', value); } catch (_) {}
                    return value;
                };
            }
            Object.defineProperty(api, '__deepseekProbeWrapped', { value: true, configurable: true });
        } catch (err) {
            push('wrap:error', { message: String(err && err.message || err || '').slice(0, 300) });
        }
        return api;
    };

    const existingHcaptcha = window.hcaptcha;
    let currentHcaptcha = undefined;
    try {
        Object.defineProperty(window, 'hcaptcha', {
            configurable: true,
            get() { return currentHcaptcha; },
            set(value) {
                try { push('window.hcaptcha:set', { keys: value && typeof value === 'object' ? Object.keys(value) : [] }); } catch (_) {}
                currentHcaptcha = wrapApi(value);
            }
        });
    } catch (err) {
        push('defineProperty:error', { message: String(err && err.message || err || '').slice(0, 300) });
    }
    if (existingHcaptcha) {
        try {
            currentHcaptcha = wrapApi(existingHcaptcha);
            push('window.hcaptcha:existing', { keys: existingHcaptcha && typeof existingHcaptcha === 'object' ? Object.keys(existingHcaptcha) : [] });
        } catch (err) {
            push('existing:error', { message: String(err && err.message || err || '').slice(0, 300) });
        }
    }

    const scanHiddenTokens = () => {
        try {
            const selectors = [
                'textarea[name="h-captcha-response"]',
                'textarea[name="g-recaptcha-response"]',
                'input[name="h-captcha-response"]',
                'input[name="g-recaptcha-response"]'
            ];
            for (const selector of selectors) {
                document.querySelectorAll(selector).forEach((node) => {
                    const value = String(node.value || '').trim();
                    if (value) rememberToken(`hidden:${selector}`, value);
                });
            }
        } catch (_) {}
    };
    window.__deepseekHcaptchaProbe = {
        events,
        tokenSamples,
        rememberToken,
        dump() {
            scanHiddenTokens();
            const frames = Array.from(document.querySelectorAll('iframe')).map((node) => ({
                src: String(node.src || '').slice(0, 300),
                title: String(node.title || ''),
                name: String(node.name || ''),
                visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
            }));
            const scripts = Array.from(document.querySelectorAll('script[src]')).map((node) => String(node.src || '')).filter((src) => /hcaptcha|turnstile|cloudflare|captcha/i.test(src));
            const hidden = Array.from(document.querySelectorAll('textarea,input')).filter((node) => /captcha-response/i.test(String(node.name || ''))).map((node) => ({
                name: String(node.name || ''),
                id: String(node.id || ''),
                token: summarizeToken(node.value)
            }));
            return {
                hasWindowHcaptcha: !!window.hcaptcha,
                hasWindowTurnstile: !!window.turnstile,
                hcaptchaKeys: window.hcaptcha && typeof window.hcaptcha === 'object' ? Object.keys(window.hcaptcha) : [],
                events: events.slice(-80),
                tokenSamples: tokenSamples.slice(-20),
                frames,
                scripts,
                hidden
            };
        }
    };
    setInterval(scanHiddenTokens, 500);
}"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe whether DeepSeek naturally generates hcaptcha_token."
    )
    parser.add_argument("--proxy", default="socks5://192.168.1.18:1080")
    parser.add_argument("--ui-locale", default="en-US")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--mail-provider", default="")
    parser.add_argument("--mail-domain", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait-after-click", type=float, default=8.0)
    parser.add_argument("--flaresolverr-url", default="")
    parser.add_argument(
        "--artifact",
        default="docs/artifacts/deepseek-hcaptcha-natural-probe.json",
    )
    return parser.parse_args()


def _is_interesting_challenge_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(
        marker in lowered
        for marker in (
            "/api/v0/users/create_guest_challenge",
            "/api/v0/users/create_email_verification_code",
            "13022.",
            "js.hcaptcha.com",
            "challenges.cloudflare.com/turnstile",
            "turnstile",
            "hcaptcha",
            "captcha",
            "cloudflare",
            "fengkongcloud",
            "fp-1.min.js",
        )
    )


def _mask_token(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return value
    if len(text) < 40:
        return value
    return {"present": True, "length": len(text), "prefix": text[:24]}


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if "password" in lowered:
                sanitized[key] = "***"
            elif "token" in lowered or "captcha" in lowered:
                sanitized[key] = _mask_token(child)
            else:
                sanitized[key] = _sanitize_payload(child)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


def _mask_email(email: str) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 4:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-2:]}@{domain}"


def _allocate_email(args: argparse.Namespace) -> str:
    explicit = str(args.email or "").strip()
    if explicit:
        return explicit
    extra = config_store.get_all().copy()
    if str(args.mail_provider or "").strip():
        extra["mail_provider"] = str(args.mail_provider).strip()
    if str(args.mail_domain or "").strip():
        domain = str(args.mail_domain).strip().lstrip("@")
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
        ):
            extra[key] = domain
    provider = str(extra.get("mail_provider") or "luckmail").strip() or "luckmail"
    mailbox = create_mailbox(
        provider=provider,
        extra=extra,
        proxy=str(args.proxy or "").strip() or None,
        platform="deepseek",
    )
    account = mailbox.get_email()
    email = str(getattr(account, "email", "") or "").strip()
    if not email:
        raise RuntimeError(f"邮箱 provider 未返回地址: {provider}")
    return email


def _dump_hcaptcha_state(page) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => window.__deepseekHcaptchaProbe ? window.__deepseekHcaptchaProbe.dump() : { missingProbe: true }"""
        )
    except Exception as exc:
        return {"error": repr(exc)}


def _collect_challenge_resource_entries(page) -> list[dict[str, Any]]:
    try:
        entries = page.evaluate(
            """() => performance
                .getEntriesByType('resource')
                .filter((entry) => /create_guest_challenge|create_email_verification_code|13022\\.|hcaptcha|turnstile|cloudflare|captcha|fengkongcloud|fp-1\\.min\\.js/i.test(String(entry.name || '')))
                .map((entry) => ({
                    name: String(entry.name || '').slice(0, 500),
                    initiatorType: String(entry.initiatorType || ''),
                    duration: Number(entry.duration || 0),
                    transferSize: Number(entry.transferSize || 0),
                    encodedBodySize: Number(entry.encodedBodySize || 0),
                }))
                .slice(-80)"""
        )
    except Exception as exc:
        return [{"error": repr(exc)}]
    if isinstance(entries, list):
        return [
            {
                "name": str(item.get("name") or "")[:500],
                "initiatorType": str(item.get("initiatorType") or ""),
                "duration": round(float(item.get("duration") or 0), 2),
                "transferSize": int(item.get("transferSize") or 0),
                "encodedBodySize": int(item.get("encodedBodySize") or 0),
            }
            for item in entries
            if isinstance(item, dict)
        ]
    return [{"result": str(entries)[:500]}]


def _accept_cookie_banner_for_probe(page) -> str:
    for label in (
        "Accept all cookies",
        "Necessary cookies only",
        "すべてのCookieを受け入れる",
        "必要なクッキーのみ",
        "接受所有 Cookie",
        "仅必要 Cookie",
    ):
        locator = page.get_by_role("button", name=label)
        try:
            if locator.count() == 0:
                continue
            locator.first.click(timeout=3000)
            page.wait_for_timeout(1000)
            return label
        except Exception:
            continue
    return ""


def main() -> int:
    args = _parse_args()
    email = _allocate_email(args)
    password = str(args.password or "").strip() or random_password()
    artifact_path = ROOT / args.artifact
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    requests_seen: list[dict[str, Any]] = []
    responses_seen: list[dict[str, Any]] = []
    captcha_network: list[dict[str, Any]] = []
    console_messages: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "ok": False,
        "email": _mask_email(email),
        "proxy": str(args.proxy or "").strip(),
        "ui_locale": args.ui_locale,
        "headed": bool(args.headed),
        "flaresolverr_url": str(args.flaresolverr_url or "").strip(),
    }

    with sync_playwright() as p:
        browser = None
        context = None
        try:
            browser = _launch_deepseek_browser(
                p,
                headless=not args.headed,
                proxy=str(args.proxy or "").strip() or None,
            )
            accept_language = build_deepseek_accept_language(args.ui_locale)
            browser_user_agent = (
                _resolve_deepseek_browser_user_agent(args.flaresolverr_url)
                if str(args.flaresolverr_url or "").strip()
                else USER_AGENT
            )
            context = browser.new_context(
                locale=args.ui_locale,
                user_agent=browser_user_agent,
                timezone_id=DEEPSEEK_DEFAULT_TIMEZONE_ID,
                viewport={"width": 1440, "height": 1080},
            )
            context.set_extra_http_headers({"Accept-Language": accept_language})
            page = context.new_page()
            if str(args.flaresolverr_url or "").strip():
                _apply_deepseek_browser_identity(
                    context,
                    page,
                    browser_user_agent=browser_user_agent,
                    accept_language=accept_language,
                    log_fn=lambda message: print(message),
                )
            _configure_deepseek_sign_up_page(page, ui_locale=args.ui_locale)
            page.add_init_script(f"({HCAPTCHA_HOOK})()")

            page.on(
                "console",
                lambda msg: console_messages.append(
                    {"type": msg.type, "text": str(msg.text)[:500]}
                ),
            )

            def on_request(request):
                url = str(request.url or "")
                if _is_interesting_challenge_url(url):
                    entry: dict[str, Any] = {
                        "kind": "request",
                        "method": request.method,
                        "url": url[:500],
                        "resource_type": request.resource_type,
                    }
                    if (
                        "/api/v0/users/create_guest_challenge" in url
                        or "/api/v0/users/create_email_verification_code" in url
                    ):
                        body = str(request.post_data or "")
                        try:
                            parsed_body: Any = json.loads(body or "{}")
                        except Exception:
                            parsed_body = body[:1000]
                        entry["post_data"] = _sanitize_payload(parsed_body)
                        entry["headers"] = _sanitize_payload(
                            {
                                key: value
                                for key, value in request.headers.items()
                                if key.lower()
                                in {
                                    "accept-language",
                                    "content-type",
                                    "origin",
                                    "referer",
                                    "user-agent",
                                }
                                or key.lower().startswith("x-")
                            }
                        )
                    captcha_network.append(entry)
                if "/api/v0/users/create_email_verification_code" in url:
                    body = str(request.post_data or "")
                    try:
                        parsed_body: Any = json.loads(body or "{}")
                    except Exception:
                        parsed_body = body[:1000]
                    requests_seen.append(
                        {
                            "method": request.method,
                            "url": url,
                            "post_data": _sanitize_payload(parsed_body),
                            "headers": _sanitize_payload(
                                {
                                    key: value
                                    for key, value in request.headers.items()
                                    if key.lower()
                                    in {
                                        "accept-language",
                                        "content-type",
                                        "origin",
                                        "referer",
                                        "user-agent",
                                    }
                                    or key.lower().startswith("x-")
                                }
                            ),
                        }
                    )

            def on_response(response):
                url = str(response.url or "")
                if _is_interesting_challenge_url(url):
                    entry: dict[str, Any] = {
                        "kind": "response",
                        "status": response.status,
                        "url": url[:500],
                        "resource_type": response.request.resource_type,
                    }
                    if (
                        "/api/v0/users/create_guest_challenge" in url
                        or "/api/v0/users/create_email_verification_code" in url
                    ):
                        try:
                            parsed_body: Any = response.json()
                        except Exception:
                            try:
                                parsed_body = str(response.text() or "")[:1200]
                            except Exception:
                                parsed_body = ""
                        entry["body"] = _sanitize_payload(parsed_body)
                    captcha_network.append(entry)

            def on_request_failed(request):
                url = str(request.url or "")
                if not _is_interesting_challenge_url(url):
                    return
                failure = getattr(request, "failure", None)
                if callable(failure):
                    try:
                        failure = failure()
                    except Exception:
                        failure = None
                captcha_network.append(
                    {
                        "kind": "requestfailed",
                        "method": request.method,
                        "url": url[:500],
                        "resource_type": request.resource_type,
                        "failure": str(failure or "")[:500],
                    }
                )

            page.on("request", on_request)
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            sign_up_url = build_deepseek_page_url("/sign_up", args.ui_locale)
            if str(args.flaresolverr_url or "").strip():
                try:
                    _prewarm_deepseek_session_with_flaresolverr(
                        page,
                        log_fn=lambda message: print(message),
                        proxy=str(args.proxy or "").strip() or None,
                        target_url=sign_up_url,
                        stage_label="hcaptcha natural probe",
                        flaresolverr_url=str(args.flaresolverr_url).strip(),
                        reload_after=False,
                    )
                except Exception as exc:
                    print(f"[DeepSeek] FlareSolverr probe prewarm failed: {exc}")
            page.goto(sign_up_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)
            try:
                installed = bool(
                    page.evaluate(
                        "() => !!window.__deepseekHcaptchaProbe"
                    )
                )
            except Exception:
                installed = False
            if not installed:
                page.evaluate(f"({HCAPTCHA_HOOK})()")
            _accept_deepseek_cookie_banner(page)
            accepted_cookie_label = _accept_cookie_banner_for_probe(page)
            _wait_for_deepseek_sign_up_form(page)
            before_state = _collect_deepseek_form_state(page)
            before_hcaptcha = _dump_hcaptcha_state(page)
            before_resource_entries = _collect_challenge_resource_entries(page)

            email_input = page.locator(
                'input.ds-input__input[type="text"], input.ds-input__input[type="email"]'
            ).first
            password_inputs = page.locator('input.ds-input__input[type="password"]')
            send_code_button = page.locator("button.ds-verify-code-input-countdown").first

            _fill_deepseek_input(email_input, email, field_name="email")
            _fill_deepseek_input(password_inputs.nth(0), password, field_name="password")
            _fill_deepseek_input(
                password_inputs.nth(1),
                password,
                field_name="confirm_password",
            )
            after_fill_hcaptcha = _dump_hcaptcha_state(page)

            response_payload: Any = None
            response_error = ""
            try:
                with page.expect_response(
                    lambda resp: resp.request.method == "POST"
                    and "/api/v0/users/create_email_verification_code" in resp.url,
                    timeout=35000,
                ) as send_response_info:
                    send_code_button.click(timeout=10000)
                response = send_response_info.value
                try:
                    response_payload = response.json()
                except Exception:
                    response_payload = str(response.text() or "")[:1000]
                responses_seen.append(
                    {
                        "status": response.status,
                        "url": response.url,
                        "body": _sanitize_payload(response_payload),
                    }
                )
            except Exception as exc:
                response_error = repr(exc)

            page.wait_for_timeout(max(0, int(args.wait_after_click * 1000)))
            after_click_hcaptcha = _dump_hcaptcha_state(page)
            after_state = _collect_deepseek_form_state(page)
            after_resource_entries = _collect_challenge_resource_entries(page)
            screenshot_path = artifact_path.with_suffix(".png")
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:
                screenshot_path = Path("")

            result.update(
                {
                    "ok": True,
                    "sign_up_url": sign_up_url,
                    "before_classification": _classify_deepseek_sign_up_state(before_state),
                    "before_summary": _summarize_deepseek_sign_up_state(
                        before_state,
                        classification=_classify_deepseek_sign_up_state(before_state),
                    ),
                    "accepted_cookie_label": accepted_cookie_label,
                    "response_error": response_error,
                    "requests_seen": requests_seen,
                    "responses_seen": responses_seen,
                    "captcha_network": captcha_network[-120:],
                    "console_messages": console_messages[-60:],
                    "before_hcaptcha": before_hcaptcha,
                    "after_fill_hcaptcha": after_fill_hcaptcha,
                    "after_click_hcaptcha": after_click_hcaptcha,
                    "before_resource_entries": before_resource_entries,
                    "after_resource_entries": after_resource_entries,
                    "after_state": after_state,
                    "screenshot": str(screenshot_path.relative_to(ROOT)) if screenshot_path else "",
                }
            )
        except Exception as exc:
            result.update({"error": repr(exc)})
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()

    artifact_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
