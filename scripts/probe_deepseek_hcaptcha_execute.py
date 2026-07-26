#!/usr/bin/env python3
# ruff: noqa: E402, E501
from __future__ import annotations

import argparse
import json
import sys
import time
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
    DEEPSEEK_HCAPTCHA_SITEKEY,
    USER_AGENT,
    _accept_deepseek_cookie_banner,
    _build_deepseek_send_code_request_route,
    _collect_deepseek_form_state,
    _configure_deepseek_sign_up_page,
    _fill_deepseek_input,
    _launch_deepseek_browser,
    _read_deepseek_hcaptcha_token,
    _request_deepseek_guest_pow_response_via_browser,
    _wait_for_deepseek_sign_up_form,
    build_deepseek_accept_language,
    build_deepseek_page_url,
    random_password,
)

HCAPTCHA_HOOK = r"""() => {
    const events = [];
    const tokenSamples = [];
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
    const push = (type, payload = {}) => {
        events.push({ ts: Date.now(), type, payload });
        if (events.length > 200) events.shift();
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
                            sitekey: params && typeof params === 'object' ? String(params.sitekey || '') : '',
                            size: params && typeof params === 'object' ? String(params.size || '') : ''
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
                                    push('execute:resolved', { token: summarizeToken(value && typeof value === 'object' ? (value.response || value.token || value.key) : value) });
                                    rememberToken('execute:resolved', value && typeof value === 'object' ? (value.response || value.token || value.key) : value);
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

HCAPTCHA_EXECUTE = r"""async ({ sitekey, timeoutMs }) => {
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const summarizeToken = (value) => {
        const text = String(value || '').trim();
        if (!text) return { present: false, length: 0, prefix: '' };
        return { present: true, length: text.length, prefix: text.slice(0, 24) };
    };
    const state = window.__deepseekAutoHcaptchaState || (window.__deepseekAutoHcaptchaState = {
        token: '',
        opened: 0,
        closed: 0,
        expired: 0,
        errors: [],
        events: [],
        scriptStatus: 'idle',
        componentStatus: 'idle'
    });
    const push = (type, payload = {}) => {
        state.events.push({ ts: Date.now(), type, payload });
        if (state.events.length > 200) state.events.shift();
    };
    const readToken = (widgetId) => {
        const selectors = [
            'textarea[name="h-captcha-response"]',
            'textarea[name="g-recaptcha-response"]',
            'input[name="h-captcha-response"]',
            'input[name="g-recaptcha-response"]'
        ];
        for (const selector of selectors) {
            const node = document.querySelector(selector);
            const value = String(node && node.value || '').trim();
            if (value) return value;
        }
        try {
            if (window.hcaptcha && typeof window.hcaptcha.getResponse === 'function') {
                const direct = String(window.hcaptcha.getResponse(widgetId) || window.hcaptcha.getResponse() || '').trim();
                if (direct) return direct;
            }
        } catch (_) {}
        return String(state.token || '').trim();
    };
    const snapshot = () => ({
        hasWindowHcaptcha: !!window.hcaptcha,
        hasWindowTurnstile: !!window.turnstile,
        customElementDefined: !!(window.customElements && typeof window.customElements.get === 'function' && window.customElements.get('h-captcha')),
        scripts: Array.from(document.querySelectorAll('script[src]')).map((node) => String(node.src || '')).filter(Boolean),
        frames: Array.from(document.querySelectorAll('iframe')).map((node) => ({
            src: String(node.src || '').slice(0, 300),
            title: String(node.title || ''),
            visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
        })),
        elements: Array.from(document.querySelectorAll('h-captcha')).map((node) => ({
            id: String(node.id || ''),
            siteKey: String(node.getAttribute('site-key') || ''),
            size: String(node.getAttribute('size') || ''),
            jsapi: String(node.getAttribute('jsapi') || ''),
            visible: !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
        })),
        hidden: Array.from(document.querySelectorAll('textarea[name="h-captcha-response"], textarea[name="g-recaptcha-response"], input[name="h-captcha-response"], input[name="g-recaptcha-response"]')).map((node) => ({
            tag: node.tagName,
            name: String(node.name || ''),
            value: summarizeToken(node.value)
        })),
    });
    const markError = (err) => {
        const message = String(err && err.message || err || '').slice(0, 300);
        state.errors.push(message);
        if (state.errors.length > 50) state.errors.shift();
        return message;
    };
    const ensureScript = async () => {
        if (window.hcaptcha && typeof window.hcaptcha.render === 'function') {
            state.scriptStatus = 'ready-existing';
            push('script:reuse');
            return;
        }
        state.scriptStatus = 'loading';
        const callbackName = '_hCaptchaOnLoad';
        const existing = Array.from(document.querySelectorAll('script[src]')).find((node) => /js\.hcaptcha\.com\/1\/api\.js/i.test(String(node.src || '')));
        const previousCallback = typeof window[callbackName] === 'function' ? window[callbackName] : null;
        let callbackResolved = false;
        window[callbackName] = (...args) => {
            callbackResolved = true;
            push('script:callback', { args: args.length });
            if (previousCallback) {
                try { previousCallback(...args); } catch (_) {}
            }
        };
        const script = existing || document.createElement('script');
        if (!existing) {
            script.src = 'https://js.hcaptcha.com/1/api.js?render=explicit&onload=_hCaptchaOnLoad&sentry=true';
            script.async = true;
            script.defer = true;
            document.head.appendChild(script);
            push('script:append', { src: script.src });
        } else {
            push('script:existing', { src: String(script.src || '') });
        }
        script.addEventListener('load', () => push('script:load', { src: String(script.src || '') }));
        script.addEventListener('error', () => {
            state.scriptStatus = 'error';
            markError('hcaptcha script load failed');
            push('script:error', { src: String(script.src || '') });
        });
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (window.hcaptcha && typeof window.hcaptcha.render === 'function') {
                state.scriptStatus = callbackResolved ? 'ready-callback' : 'ready-load';
                push('script:ready', { callbackResolved });
                return;
            }
            await sleep(200);
        }
        state.scriptStatus = 'timeout';
        throw new Error('wait hcaptcha api timeout');
    };
    const defineCustomElement = () => {
        if (customElements.get('h-captcha')) {
            state.componentStatus = 'ready-existing';
            push('component:reuse');
            return;
        }
        class DeepseekAutoHCaptchaElement extends HTMLElement {
            constructor() {
                super();
                this._widgetId = null;
                this._renderPromise = null;
            }
            connectedCallback() {
                if (!this._renderPromise) this._renderPromise = this._render();
            }
            async _render() {
                await ensureScript();
                const params = {
                    sitekey: String(this.getAttribute('site-key') || sitekey || '').trim(),
                    size: String(this.getAttribute('size') || 'invisible'),
                    callback: (value) => {
                        state.token = String(value || '').trim();
                        push('callback', { token: summarizeToken(value) });
                    },
                    'open-callback': () => {
                        state.opened += 1;
                        push('open');
                    },
                    'close-callback': () => {
                        state.closed += 1;
                        push('close');
                    },
                    'expired-callback': () => {
                        state.expired += 1;
                        push('expired');
                    },
                    'chalexpired-callback': () => {
                        state.expired += 1;
                        push('chalexpired');
                    },
                    'error-callback': (err) => {
                        const message = markError(err);
                        push('callback:error', { message });
                    },
                };
                const theme = String(this.getAttribute('theme') || '').trim();
                if (theme) params.theme = theme;
                if (this._widgetId === null || this._widgetId === undefined) {
                    this._widgetId = window.hcaptcha.render(this, params);
                    push('render', { widgetId: this._widgetId, sitekey: params.sitekey, size: params.size });
                }
                this.dispatchEvent(new CustomEvent('loaded', { detail: { widgetId: this._widgetId } }));
                return this._widgetId;
            }
            async executeAsync() {
                if (!this._renderPromise) this._renderPromise = this._render();
                await this._renderPromise;
                push('executeAsync:call', { widgetId: this._widgetId });
                const result = await window.hcaptcha.execute(this._widgetId, { async: true });
                const direct = String(result && typeof result === 'object' ? (result.response || result.token || result.key) : (result || '')).trim();
                if (direct) {
                    state.token = direct;
                    push('executeAsync:resolved', { token: summarizeToken(direct) });
                }
                return result;
            }
            getResponse() {
                try {
                    return String(window.hcaptcha.getResponse(this._widgetId) || '');
                } catch (_) {
                    return '';
                }
            }
        }
        customElements.define('h-captcha', DeepseekAutoHCaptchaElement);
        state.componentStatus = 'defined';
        push('component:defined');
    };

    try {
        await ensureScript();
        defineCustomElement();
        const existingToken = readToken();
        if (existingToken) {
            state.token = existingToken;
            return {
                ok: true,
                source: 'existing-token',
                token: existingToken,
                state,
                snapshot: snapshot(),
            };
        }

        let element = document.getElementById('deepseek-auto-hcaptcha-element');
        if (!element) {
            element = document.createElement('h-captcha');
            element.id = 'deepseek-auto-hcaptcha-element';
            element.setAttribute('site-key', String(sitekey || '').trim());
            element.setAttribute('size', 'invisible');
            element.setAttribute('sentry', 'true');
            element.style.position = 'fixed';
            element.style.left = '0';
            element.style.top = '0';
            element.style.width = '1px';
            element.style.height = '1px';
            element.style.opacity = '0.01';
            element.style.pointerEvents = 'none';
            element.style.zIndex = '2147483647';
            document.body.appendChild(element);
            push('element:append', { id: element.id });
        } else {
            push('element:reuse', { id: String(element.id || '') });
        }

        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const token = readToken();
            if (token) {
                state.token = token;
                return {
                    ok: true,
                    source: 'post-append-token',
                    token,
                    state,
                    snapshot: snapshot(),
                };
            }
            if (typeof element.executeAsync === 'function') break;
            await sleep(200);
        }

        if (typeof element.executeAsync !== 'function') {
            throw new Error('h-captcha custom element executeAsync unavailable');
        }

        try {
            await element.executeAsync();
        } catch (err) {
            push('executeAsync:error', { message: markError(err) });
        }

        while (Date.now() < deadline) {
            const token = readToken(element._widgetId);
            if (token) {
                state.token = token;
                return {
                    ok: true,
                    source: 'executed',
                    token,
                    state,
                    snapshot: snapshot(),
                };
            }
            await sleep(250);
        }
    } catch (err) {
        push('fatal', { message: markError(err) });
    }

    return {
        ok: false,
        source: 'timeout',
        token: '',
        state,
        snapshot: snapshot(),
    };
}"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Actively execute hCaptcha in DeepSeek sign-up flow and probe send code."
    )
    parser.add_argument("--proxy", default="socks5://192.168.1.18:1080")
    parser.add_argument("--ui-locale", default="en-US")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--mail-provider", default="")
    parser.add_argument("--mail-domain", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--wait-after-click", type=float, default=12.0)
    parser.add_argument("--mail-timeout", type=int, default=90)
    parser.add_argument(
        "--artifact",
        default="docs/artifacts/deepseek-hcaptcha-execute-probe.json",
    )
    return parser.parse_args()


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


def _build_mail_extra(args: argparse.Namespace) -> dict[str, Any]:
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
    return extra


def _allocate_mail_context(
    args: argparse.Namespace,
) -> tuple[str, Any | None, Any | None, set[Any], str]:
    explicit = str(args.email or "").strip()
    extra = _build_mail_extra(args)
    provider = str(extra.get("mail_provider") or "luckmail").strip() or "luckmail"
    if explicit:
        return explicit, None, None, set(), provider
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
    try:
        before_ids = set(mailbox.get_current_ids(account) or [])
    except Exception:
        before_ids = set()
    return email, mailbox, account, before_ids, provider


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
                .slice(-120)"""
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


def _is_interesting_challenge_url(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(
        marker in lowered
        for marker in (
            "/api/v0/users/create_guest_challenge",
            "/api/v0/users/create_email_verification_code",
            "13022.",
            "js.hcaptcha.com",
            "newassets.hcaptcha.com",
            "hcaptcha.com/getcaptcha",
            "challenges.cloudflare.com/turnstile",
            "turnstile",
            "hcaptcha",
            "captcha",
            "cloudflare",
            "fp-1.min.js",
        )
    )


def _extract_inner_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        biz_data = data.get("biz_data")
        return {
            "biz_code": data.get("biz_code"),
            "biz_msg": data.get("biz_msg"),
            "biz_data": biz_data,
        }
    return {}


def _wait_for_mail_code(
    mailbox,
    mail_account,
    *,
    before_ids: set[Any],
    timeout: int,
    sent_at: float,
) -> dict[str, Any]:
    started_at = time.time()
    code = mailbox.wait_for_code(
        mail_account,
        keyword="DeepSeek",
        timeout=timeout,
        before_ids=before_ids,
        otp_sent_at=sent_at,
    )
    return {
        "ok": True,
        "code": str(code or ""),
        "elapsed_seconds": round(time.time() - started_at, 2),
    }


def main() -> int:
    args = _parse_args()
    email, mailbox, mail_account, before_ids, provider = _allocate_mail_context(args)
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
        "mail_provider": provider,
    }

    with sync_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser = _launch_deepseek_browser(
                p,
                headless=not args.headed,
                proxy=str(args.proxy or "").strip() or None,
            )
            accept_language = build_deepseek_accept_language(args.ui_locale)
            context = browser.new_context(
                bypass_csp=True,
                locale=args.ui_locale,
                user_agent=USER_AGENT,
                timezone_id=DEEPSEEK_DEFAULT_TIMEZONE_ID,
                viewport={"width": 1440, "height": 1080},
            )
            context.set_extra_http_headers({"Accept-Language": accept_language})
            page = context.new_page()
            _configure_deepseek_sign_up_page(page, ui_locale=args.ui_locale)
            page.add_init_script(f"({HCAPTCHA_HOOK})()")

            page.on(
                "console",
                lambda msg: console_messages.append(
                    {"type": msg.type, "text": str(msg.text)[:500]}
                ),
            )

            def on_request(request) -> None:
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
                            parsed_body = body[:1200]
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
                        parsed_body = json.loads(body or "{}")
                    except Exception:
                        parsed_body = body[:1200]
                    requests_seen.append(
                        {
                            "method": request.method,
                            "url": url[:500],
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

            def on_response(response) -> None:
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

            def on_request_failed(request) -> None:
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
            page.goto(sign_up_url, wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)
            try:
                installed = bool(
                    page.evaluate("() => !!window.__deepseekHcaptchaProbe")
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

            execute_result: Any = {}
            execute_error = ""
            try:
                execute_result = page.evaluate(
                    HCAPTCHA_EXECUTE,
                    {
                        "sitekey": DEEPSEEK_HCAPTCHA_SITEKEY,
                        "timeoutMs": 45000,
                    },
                )
            except Exception as exc:
                execute_error = repr(exc)
                execute_result = {
                    "ok": False,
                    "source": "evaluate-error",
                    "error": execute_error,
                }
            hcaptcha_token = str(
                (
                    execute_result.get("token")
                    if isinstance(execute_result, dict)
                    else ""
                )
                or _read_deepseek_hcaptcha_token(page)
                or ""
            ).strip()

            guest_pow_response = ""
            guest_pow_error = ""
            try:
                guest_pow_response = _request_deepseek_guest_pow_response_via_browser(
                    page,
                    target_path="/api/v0/users/create_email_verification_code",
                    proxy=str(args.proxy or "").strip() or None,
                    ui_locale=args.ui_locale,
                    sign_up_url=sign_up_url,
                )
            except Exception as exc:
                guest_pow_error = repr(exc)

            route_pattern = "**/api/v0/users/create_email_verification_code"
            route_handler = _build_deepseek_send_code_request_route(
                turnstile_token="",
                hcaptcha_token=hcaptcha_token,
                guest_pow_response=guest_pow_response,
            )
            page.route(route_pattern, route_handler)

            response_payload: Any = None
            response_error = ""
            sent_at = time.time()
            try:
                with page.expect_response(
                    lambda resp: resp.request.method == "POST"
                    and "/api/v0/users/create_email_verification_code" in resp.url,
                    timeout=45000,
                ) as send_response_info:
                    send_code_button.click(timeout=10000)
                response = send_response_info.value
                try:
                    response_payload = response.json()
                except Exception:
                    response_payload = str(response.text() or "")[:1200]
                responses_seen.append(
                    {
                        "status": response.status,
                        "url": response.url,
                        "body": _sanitize_payload(response_payload),
                    }
                )
            except Exception as exc:
                response_error = repr(exc)
            finally:
                try:
                    page.unroute(route_pattern, route_handler)
                except Exception:
                    pass

            page.wait_for_timeout(max(0, int(float(args.wait_after_click) * 1000)))
            after_hcaptcha = _dump_hcaptcha_state(page)
            after_state = _collect_deepseek_form_state(page)

            inner = _extract_inner_response(response_payload)
            mail_result: dict[str, Any] | None = None
            if inner.get("biz_code") == 0 and mailbox and mail_account:
                try:
                    mail_result = _wait_for_mail_code(
                        mailbox,
                        mail_account,
                        before_ids=before_ids,
                        timeout=max(1, int(args.mail_timeout)),
                        sent_at=sent_at,
                    )
                except Exception as exc:
                    mail_result = {"ok": False, "error": repr(exc)}

            result.update(
                {
                    "ok": bool(inner.get("biz_code") == 0),
                    "sign_up_url": sign_up_url,
                    "accepted_cookie_label": accepted_cookie_label,
                    "before_state": before_state,
                    "before_hcaptcha": before_hcaptcha,
                    "execute_result": _sanitize_payload(execute_result),
                    "execute_error": execute_error,
                    "hcaptcha_token": _mask_token(hcaptcha_token),
                    "guest_pow": {
                        "present": bool(guest_pow_response),
                        "length": len(guest_pow_response or ""),
                        "prefix": str(guest_pow_response or "")[:24],
                        "error": guest_pow_error,
                    },
                    "response_error": response_error,
                    "response_inner": _sanitize_payload(inner),
                    "after_hcaptcha": after_hcaptcha,
                    "after_state": after_state,
                    "requests_seen": requests_seen,
                    "responses_seen": responses_seen,
                    "captcha_network": captcha_network,
                    "resource_entries": _collect_challenge_resource_entries(page),
                    "console_messages": console_messages,
                }
            )
            if mail_result is not None:
                result["mail_result"] = mail_result
        except Exception as exc:
            result["fatal_error"] = repr(exc)
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
                    result["resource_entries"] = _collect_challenge_resource_entries(page)
                except Exception as resource_exc:
                    result["resource_entries_error"] = repr(resource_exc)
            result["requests_seen"] = requests_seen
            result["responses_seen"] = responses_seen
            result["captcha_network"] = captcha_network
            result["console_messages"] = console_messages
        finally:
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
