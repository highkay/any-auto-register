from __future__ import annotations

import base64
import html
import io
import json
import math
import random
import re
import shutil
import string
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, urlparse

import requests
from PIL import Image

from core.browser_runtime import (
    ensure_browser_display_available,
    get_chrome_executable,
    resolve_browser_headless,
    with_chrome_executable,
)
from core.proxy_utils import build_playwright_proxy_config, build_requests_proxy_config


ZAI_SIGNUP_URL = "https://chat.z.ai/auth?redirect_uri=https://z.ai/&action=signup"
ZAI_ME_URL = "https://chat.z.ai/api/v1/auths/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
_SIGNUP_RESPONSE_PATH = "/api/v1/auths/signup"
_VERIFY_EMAIL_RESPONSE_PATH = "/api/v1/auths/verify_email"
_FINISH_SIGNUP_RESPONSE_PATH = "/api/v1/auths/finish_signup"
_ALIYUN_VERIFY_HOST = "captcha-open.aliyuncs.com"
_ALIYUN_VERIFY_ACTION = "VerifyCaptchaV3"
_ALIYUN_TASK_CAPTCHA_SELECTOR = "#aliyunCaptcha-captcha-wrapper"
_ALIYUN_TASK_MODE_HINT = "slide"
_ALIYUN_TASK_CALLBACK_PATH = "__APP_STATE__.captcha.verifyParam"
_ALIYUN_TASK_PROJECT_NAME = "any-auto-register:zai"
_ALIYUN_HOOK_JS = """
(() => {
    if (window.__zaiAliyunState) {
        return;
    }

    const state = window.__zaiAliyunState = {
        payloads: [],
        configs: [],
        errors: [],
        events: [],
        instance: null,
        capture: {
            active: false,
            submissionsBlocked: 0,
        },
    };

    const pushEvent = (event) => {
        state.events.push(event);
        if (state.events.length > 50) {
            state.events.shift();
        }
    };
    const summarizeValue = (value) => {
        const kind = typeof value;
        if (value === null || kind === 'undefined' || kind === 'boolean' || kind === 'number') {
            return {type: kind, value: value === undefined ? null : value};
        }
        if (kind === 'string') {
            return {type: 'string', length: value.length, preview: value.slice(0, 160)};
        }
        if (Array.isArray(value)) {
            return {type: 'array', length: value.length};
        }
        if (kind === 'object') {
            return {type: 'object', keys: Object.keys(value).slice(0, 20)};
        }
        return {type: kind};
    };

    const clone = (value) => {
        if (value === undefined) {
            return null;
        }
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (error) {
            if (typeof value === 'string') {
                return value;
            }
            if (value === null || typeof value !== 'object') {
                return value;
            }
            return {__omcString: String(value)};
        }
    };
    const resolveCallback = (value) => {
        if (typeof value === 'function') {
            return value;
        }
        if (typeof value === 'string' && typeof window[value] === 'function') {
            return window[value];
        }
        return null;
    };
    const rememberPayload = (payload, source) => {
        state.payloads.push({
            source,
            payload: clone(payload),
            url: location.href,
        });
        if (state.payloads.length > 10) {
            state.payloads.shift();
        }
    };

    const wrapInit = (fn) => {
        if (typeof fn !== 'function' || fn.__zaiWrapped) {
            return fn;
        }
        const wrapped = function(config, ...rest) {
            const params = config && typeof config === 'object' ? config : {};
            const originalSuccess = resolveCallback(params.success);
            const originalFail = resolveCallback(params.fail);
            const originalGetInstance = resolveCallback(params.getInstance);
            const patched = { ...params };
            state.configs.push({
                SceneId: typeof params.SceneId === 'string' ? params.SceneId : null,
                prefix: typeof params.prefix === 'string' ? params.prefix : null,
                mode: typeof params.mode === 'string' ? params.mode : null,
                element: typeof params.element === 'string' ? params.element : null,
                button: typeof params.button === 'string' ? params.button : null,
            });
            if (state.configs.length > 10) {
                state.configs.shift();
            }
            pushEvent({
                type: 'init',
                element: patched.element || null,
                button: patched.button || null,
                url: location.href,
            });
            patched.success = function(payload) {
                rememberPayload(payload, 'success');
                pushEvent({
                    type: 'success',
                    payloadType: typeof payload,
                    url: location.href,
                });
                if (state.capture.active) {
                    return;
                }
                if (originalSuccess) {
                    return originalSuccess.apply(this, arguments);
                }
            };
            patched.fail = function(error) {
                const detail = clone(error);
                state.errors.push(detail);
                pushEvent({
                    type: 'fail',
                    error: detail,
                    url: location.href,
                });
                if (state.capture.active) {
                    return;
                }
                if (originalFail) {
                    return originalFail.apply(this, arguments);
                }
            };
            patched.getInstance = function(instance) {
                state.instance = instance || null;
                pushEvent({
                    type: 'instance',
                    hasInstance: Boolean(instance),
                    url: location.href,
                });
                if (originalGetInstance) {
                    return originalGetInstance.apply(this, arguments);
                }
            };
            return fn.call(this, patched, ...rest);
        };
        wrapped.__zaiWrapped = true;
        return wrapped;
    };
    let initValue = window.initAliyunCaptcha;
    Object.defineProperty(window, 'initAliyunCaptcha', {
        configurable: true,
        enumerable: true,
        get() { return initValue; },
        set(value) { initValue = wrapInit(value); },
    });
    if (initValue) {
        window.initAliyunCaptcha = initValue;
    }

    const originalSubmit = HTMLFormElement.prototype.submit;
    HTMLFormElement.prototype.submit = function() {
        if (state.capture.active) {
            state.capture.submissionsBlocked += 1;
            pushEvent({
                type: 'submit-blocked',
                via: 'submit',
                action: this.action || null,
                url: location.href,
            });
            return;
        }
        return originalSubmit.apply(this, arguments);
    };

    if (HTMLFormElement.prototype.requestSubmit) {
        const originalRequestSubmit = HTMLFormElement.prototype.requestSubmit;
        HTMLFormElement.prototype.requestSubmit = function() {
            if (state.capture.active) {
                state.capture.submissionsBlocked += 1;
                pushEvent({
                    type: 'submit-blocked',
                    via: 'requestSubmit',
                    action: this.action || null,
                    url: location.href,
                });
                return;
            }
            return originalRequestSubmit.apply(this, arguments);
        };
    }

    document.addEventListener(
        'submit',
        (event) => {
            if (!state.capture.active) {
                return;
            }
            state.capture.submissionsBlocked += 1;
            pushEvent({
                type: 'submit-blocked',
                via: 'event',
                action: event.target && event.target.action ? event.target.action : null,
                url: location.href,
            });
            event.preventDefault();
            event.stopImmediatePropagation();
        },
        true,
    );

    window.addEventListener(
        'message',
        (event) => {
            pushEvent({
                type: 'message',
                origin: event.origin || null,
                data: summarizeValue(event.data),
                url: location.href,
            });
        },
        true,
    );
})();
"""
_EXTRACT_ALIYUN_PAYLOAD_JS = """
() => {
    const state = window.__zaiAliyunState || {
        configs: [],
        payloads: [],
        errors: [],
        events: [],
        capture: {active: false, submissionsBlocked: 0},
        instance: null,
    };

    const clone = (value) => {
        if (value === undefined) {
            return null;
        }
        try {
            return JSON.parse(JSON.stringify(value));
        } catch (error) {
            if (typeof value === 'string') {
                return value;
            }
            if (value === null || typeof value !== 'object') {
                return value;
            }
            return {__omcString: String(value)};
        }
    };

    const pickPayload = (value, source) => {
        if (value === undefined || value === null) {
            return null;
        }
        if (typeof value === 'string') {
            const text = value.trim();
            if (!text) {
                return null;
            }
            return {
                found: true,
                source,
                value: text,
            };
        }
        if (typeof value === 'object') {
            if (typeof Element !== 'undefined' && value instanceof Element) {
                return null;
            }
            const direct = value.captchaVerifyParam;
            if (typeof direct === 'string' && direct.trim()) {
                return {
                    found: true,
                    source,
                    value: direct.trim(),
                    raw: clone(value),
                };
            }
            const fields = ['sceneId', 'certifyId', 'deviceToken', 'data', 'sig', 'token', 'sessionId', 'appKey'];
            const structured = {};
            let fieldCount = 0;
            for (const field of fields) {
                const fieldValue = value[field];
                if (typeof fieldValue === 'string' && fieldValue.trim()) {
                    structured[field] = fieldValue.trim();
                    fieldCount += 1;
                }
            }
            if (fieldCount > 0) {
                return {
                    found: true,
                    source,
                    value: structured,
                    raw: clone(value),
                };
            }
            if (Object.keys(value).length > 0) {
                return {
                    found: true,
                    source,
                    value: clone(value),
                };
            }
            return null;
        }
        return {
            found: true,
            source,
            value,
        };
    };

    if (Array.isArray(state.payloads) && state.payloads.length > 0) {
        for (let index = state.payloads.length - 1; index >= 0; index -= 1) {
            const entry = state.payloads[index];
            const payload = pickPayload(entry ? entry.payload : null, entry && entry.source ? entry.source : 'hook');
            if (payload) {
                return payload;
            }
        }
    }

    const exactInput = document.querySelector('[name="captchaVerifyParam"], [name="CaptchaVerifyParam"], #captchaVerifyParam');
    if (exactInput && typeof exactInput.value === 'string' && exactInput.value.trim()) {
        return {
            found: true,
            source: 'hidden_input',
            value: exactInput.value.trim(),
        };
    }

    const structuredFields = {};
    let structuredCount = 0;
    for (const field of ['sceneId', 'certifyId', 'deviceToken', 'data', 'sig', 'token', 'sessionId', 'appKey']) {
        const selector = `[name="${field}"], [name="${field[0].toUpperCase()}${field.slice(1)}"], #${field}`;
        const node = document.querySelector(selector);
        if (node && typeof node.value === 'string' && node.value.trim()) {
            structuredFields[field] = node.value.trim();
            structuredCount += 1;
        }
    }
    if (structuredCount > 0) {
        return {
            found: true,
            source: 'hidden_input',
            value: structuredFields,
        };
    }

    const globalValue = pickPayload(window.captchaVerifyParam, 'window_state')
        || pickPayload(window.__captchaVerifyParam, 'window_state');
    if (globalValue) {
        return globalValue;
    }

    return {
        found: false,
        source: null,
        value: null,
        debug: {
            payloadCount: Array.isArray(state.payloads) ? state.payloads.length : 0,
            configCount: Array.isArray(state.configs) ? state.configs.length : 0,
            submissionsBlocked: state.capture ? state.capture.submissionsBlocked || 0 : 0,
        },
    };
}
"""
_DEBUG_ALIYUN_STATE_JS = """
() => {
    const state = window.__zaiAliyunState || {
        configs: [],
        payloads: [],
        errors: [],
        events: [],
        capture: {active: false, submissionsBlocked: 0},
        instance: null,
    };
    const body = document.body;
    const elementState = (selector) => {
        const node = document.querySelector(selector);
        if (!node) {
            return {exists: false};
        }
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return {
            exists: true,
            visible:
                rect.width > 0 &&
                rect.height > 0 &&
                style.visibility !== 'hidden' &&
                style.display !== 'none',
            className: typeof node.className === 'string' ? node.className.slice(0, 120) : '',
            text: typeof node.innerText === 'string' ? node.innerText.slice(0, 160) : '',
            bbox: {
                x: Math.round(rect.x * 100) / 100,
                y: Math.round(rect.y * 100) / 100,
                width: Math.round(rect.width * 100) / 100,
                height: Math.round(rect.height * 100) / 100,
            },
        };
    };
    const iframes = Array.from(document.querySelectorAll('iframe')).slice(0, 12).map((frame) => ({
        src: frame.src || '',
        name: frame.name || '',
        id: frame.id || '',
        title: frame.title || '',
    }));
    const scripts = Array.from(document.scripts)
        .map((script) => script.src || '')
        .filter((src) => /aliyun|aliyuncs|captcha/i.test(src))
        .slice(-12);
    return {
        url: location.href,
        title: document.title,
        hookInstalled: Boolean(window.__zaiAliyunState),
        hasInitAliyunCaptcha: typeof window.initAliyunCaptcha === 'function',
        hasInstance: Boolean(state.instance),
        configs: state.configs || [],
        payloadCount: Array.isArray(state.payloads) ? state.payloads.length : 0,
        lastPayloadSource:
            Array.isArray(state.payloads) && state.payloads.length > 0
                ? state.payloads[state.payloads.length - 1].source || null
                : null,
        errors: state.errors || [],
        events: state.events || [],
        submissionsBlocked: state.capture ? state.capture.submissionsBlocked || 0 : 0,
        iframeCount: document.querySelectorAll('iframe').length,
        iframes,
        scripts,
        elements: {
            wrapper: elementState('#aliyunCaptcha-captcha-wrapper'),
            windowFloat: elementState('#aliyunCaptcha-window-float'),
            imageBox: elementState('#aliyunCaptcha-img-box'),
            sliderBody: elementState('#aliyunCaptcha-sliding-body'),
            slider: elementState('#aliyunCaptcha-sliding-slider'),
            puzzle: elementState('#aliyunCaptcha-puzzle'),
        },
        htmlSnippet: body ? body.innerHTML.slice(0, 1200) : '',
    };
}
"""
_VERIFY_PAGE_PATTERNS = ("已向以下邮箱发送了验证链接", "验证链接", "重新发送")
_SIGNUP_ERROR_PATTERNS = ("captcha verification failed", "验证码", "已被使用", "already", "failed")
_SLIDE_WIDGET_SELECTORS = (
    "#aliyunCaptcha-window-float.window-show",
    "#aliyunCaptcha-window-float",
    "#aliyunCaptcha-img-box",
    "#aliyunCaptcha-captcha-wrapper",
)
_START_VERIFY_LABELS = ("点击开始验证", "开始验证", "安全验证")
_EMAIL_LOGIN_SWITCH_LABELS = ("邮箱登录", "Email login")
_SIGNUP_SUBMIT_LABELS = ("创建账号", "注册", "Create Account", "Sign up", "Submit")
_COMPLETE_SIGNUP_LABELS = ("完成注册", "创建账号", "继续", "提交", "完成")
_ALIYUN_ACTION_BUTTON_LABELS = ("确认", "验证", "提交", "check", "verify", "submit", "next", "下一步")
_ALIYUN_RETRYABLE_VERIFY_CODES = {"F001", "F015"}


def random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    base = "".join(random.choices(alphabet, k=max(length, 10)))
    return f"{base}!Aa1"


def _default_display_name(email: str) -> str:
    local = str(email or "").split("@", 1)[0]
    letters = re.findall(r"[A-Za-z0-9]+", local)
    if not letters:
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"zai-{suffix}"
    parts = [item[:8] for item in letters[:2] if item]
    if len(parts) == 1:
        parts.append("user")
    return "-".join(parts)[:32]


def _strip_json_fence(text: str) -> str:
    value = str(text or "").strip()
    if not value.startswith("```"):
        return value
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE | re.DOTALL).strip()


def _parse_json_like_payload(payload: Any, *, label: str) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    text = _strip_json_fence(str(payload or "").strip())
    if not text:
        raise RuntimeError(f"{label}返回为空")
    candidates = [text]
    for pattern in (r"(\{[\s\S]*\})", r"(\[[\s\S]*\])"):
        match = re.search(pattern, text)
        if match:
            candidates.append(match.group(1))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise RuntimeError(f"{label}结果不是合法 JSON: {text[:200]!r}")


class ZaiRegister:
    @staticmethod
    def _preferred_browser_launch_extras() -> list[dict[str, Any]]:
        """Prefer F:\\chrome, then system chrome/msedge channels."""
        extras: list[dict[str, Any]] = []
        chrome = get_chrome_executable()
        if chrome:
            extras.append({"executable_path": chrome})

        known_paths = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
        preferred_channels: list[str | None] = []
        for path in known_paths:
            if not path.exists():
                continue
            lowered = str(path).lower()
            if "chrome" in lowered:
                preferred_channels = ["chrome", "msedge", None]
                break
            if "msedge" in lowered:
                preferred_channels = ["msedge", "chrome", None]
                break

        if not preferred_channels:
            if shutil.which("chrome.exe") or shutil.which("chrome"):
                preferred_channels = ["chrome", "msedge", None]
            elif shutil.which("msedge.exe") or shutil.which("msedge"):
                preferred_channels = ["msedge", "chrome", None]
            else:
                preferred_channels = ["chrome", "msedge", None]

        for channel in preferred_channels:
            if channel:
                extras.append({"channel": channel})
            else:
                extras.append({})
        return extras

    def __init__(
        self,
        *,
        proxy: str | None = None,
        captcha_solver=None,
        log_fn: Callable[[str], None] = print,
        headless: bool = True,
        task_control=None,
    ):
        self.proxy = proxy
        self.captcha_solver = captcha_solver
        self.log = log_fn
        self.headless = headless
        self._task_control = task_control

    def _checkpoint(self) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint()

    def _wait_until(
        self,
        fn: Callable[[], bool],
        *,
        timeout: float = 30.0,
        interval: float = 0.5,
        desc: str = "",
        page=None,
    ) -> None:
        deadline = time.time() + timeout
        wait_ms = max(1, int(max(interval, 0.01) * 1000))
        while time.time() < deadline:
            self._checkpoint()
            if fn():
                return
            if page is not None:
                try:
                    page.wait_for_timeout(wait_ms)
                    continue
                except Exception:
                    pass
            time.sleep(interval)
        raise TimeoutError(desc or "等待超时")

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _launch_browser(self):
        from core.browser_backend import BACKEND_NAME, sync_playwright

        backend_name = BACKEND_NAME

        playwright = sync_playwright().start()
        headless, reason = resolve_browser_headless(self.headless, default_headless=True)
        ensure_browser_display_available(headless)
        self.log(f"浏览器模式: {'headless' if headless else 'headed'} ({reason})")
        self.log(f"浏览器后端: {backend_name}")

        base_kwargs: dict[str, Any] = {"headless": headless}
        if self.proxy:
            proxy_cfg = build_playwright_proxy_config(self.proxy)
            if proxy_cfg:
                base_kwargs["proxy"] = proxy_cfg

        last_error: Exception | None = None
        for extra in self._preferred_browser_launch_extras():
            launch_kwargs = with_chrome_executable({**base_kwargs, **extra})
            try:
                browser = playwright.chromium.launch(**launch_kwargs)
                label = (
                    launch_kwargs.get("executable_path")
                    or launch_kwargs.get("channel")
                    or "default"
                )
                self.log(f"浏览器通道: {label}")
                return playwright, browser
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("Z.ai 浏览器启动失败")

    def _new_context(self, browser):
        context = browser.new_context(
            locale="zh-CN",
            user_agent=USER_AGENT,
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1080},
        )
        context.set_extra_http_headers({"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        context.add_init_script(script=_ALIYUN_HOOK_JS)
        return context

    def _install_response_capture(self, page, store: dict[str, Any]) -> None:
        def _capture(response) -> None:
            try:
                url = str(response.url or "")
                method = str(response.request.method or "").upper()
                request_post_data = str(getattr(response.request, "post_data", "") or "")
                is_aliyun_request = _ALIYUN_VERIFY_HOST in url
                if is_aliyun_request:
                    self._record_aliyun_network_trace(
                        store,
                        url=url,
                        method=method,
                        status=int(response.status or 0),
                        request_post_data=request_post_data,
                    )
                if method != "POST":
                    return
                key = ""
                if _SIGNUP_RESPONSE_PATH in url:
                    key = "signup"
                elif _VERIFY_EMAIL_RESPONSE_PATH in url:
                    key = "verify_email"
                elif _FINISH_SIGNUP_RESPONSE_PATH in url:
                    key = "finish_signup"
                elif _ALIYUN_VERIFY_HOST in url and _ALIYUN_VERIFY_ACTION in request_post_data:
                    key = "aliyun_verify"
                if not key:
                    return
                entry: dict[str, Any] = {
                    "url": url,
                    "status": int(response.status or 0),
                    "request_post_data": request_post_data,
                }
                try:
                    entry["json"] = response.json()
                except Exception:
                    try:
                        entry["text"] = response.text()
                    except Exception:
                        entry["text"] = ""
                store[key] = entry
                if key == "aliyun_verify":
                    self._annotate_latest_aliyun_network_trace(
                        store,
                        response_payload=entry.get("json"),
                    )
            except Exception:
                return

        page.on("response", _capture)

    @staticmethod
    def _bounded_list_append(
        store: dict[str, Any],
        key: str,
        item: dict[str, Any],
        *,
        limit: int = 25,
    ) -> None:
        values = store.get(key)
        if not isinstance(values, list):
            values = []
            store[key] = values
        values.append(item)
        if len(values) > limit:
            del values[:-limit]

    @staticmethod
    def _summarize_aliyun_verify_post_data(post_data: str) -> dict[str, Any]:
        raw = str(post_data or "")
        if not raw:
            return {"postLength": 0, "postKeys": []}
        try:
            payload = parse_qs(raw, keep_blank_values=True)
        except Exception:
            return {"postLength": len(raw), "postKeys": [], "parseError": "querystring"}
        summary: dict[str, Any] = {
            "postLength": len(raw),
            "postKeys": sorted(payload.keys())[:20],
            "action": str((payload.get("Action") or [""])[0] or "").strip() or None,
        }
        verify_param = str((payload.get("CaptchaVerifyParam") or [""])[0] or "").strip()
        summary["hasCaptchaVerifyParam"] = bool(verify_param)
        if verify_param:
            summary["captchaVerifyParamLength"] = len(verify_param)
            try:
                parsed = json.loads(verify_param)
            except Exception:
                summary["captchaVerifyParamType"] = "str"
            else:
                summary["captchaVerifyParamType"] = type(parsed).__name__
                if isinstance(parsed, dict):
                    summary["captchaVerifyParamKeys"] = sorted(parsed.keys())[:20]
        return summary

    def _record_aliyun_network_trace(
        self,
        store: dict[str, Any],
        *,
        url: str,
        method: str,
        status: int,
        request_post_data: str,
    ) -> None:
        parsed_url = urlparse(url)
        entry = {
            "ts": round(time.time(), 3),
            "method": method,
            "status": status,
            "host": parsed_url.netloc,
            "path": parsed_url.path,
        }
        if method == "POST":
            entry.update(self._summarize_aliyun_verify_post_data(request_post_data))
        self._bounded_list_append(store, "aliyun_requests", entry)

    @staticmethod
    def _annotate_latest_aliyun_network_trace(
        store: dict[str, Any],
        *,
        response_payload: Any,
    ) -> None:
        entries = store.get("aliyun_requests")
        if not isinstance(entries, list) or not entries:
            return
        latest = entries[-1]
        if not isinstance(latest, dict):
            return
        result = response_payload.get("Result") if isinstance(response_payload, dict) else None
        if not isinstance(result, dict):
            return
        if "VerifyResult" in result:
            latest["verifyResult"] = result.get("VerifyResult")
        if "VerifyCode" in result:
            latest["verifyCode"] = result.get("VerifyCode")

    def _open_signup_page(self, page) -> None:
        self.log("Step1: 打开 Z.ai 邮箱注册页 ...")
        page.goto(ZAI_SIGNUP_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(3500)
        try:
            page.evaluate(_ALIYUN_HOOK_JS)
        except Exception:
            pass
        if not self._has_signup_form(page):
            if self._click_text_button(page, _EMAIL_LOGIN_SWITCH_LABELS):
                page.wait_for_timeout(1500)
        self._wait_until(
            lambda: self._has_signup_form(page),
            timeout=30,
            interval=0.5,
            desc="等待 Z.ai 注册表单超时",
            page=page,
        )

    def _fill_signup_form(self, page, *, name: str, email: str, password: str) -> None:
        self.log("Step2: 填写 Z.ai 注册表单 ...")
        page.locator('input[placeholder="输入您的名称"]').first.fill(name)
        page.locator('input[placeholder="输入您的电子邮箱"]').first.fill(email)
        page.locator('input[placeholder="输入您的密码"]').first.fill(password)

    @staticmethod
    def _has_signup_form(page) -> bool:
        try:
            return (
                page.locator('input[placeholder="输入您的名称"]').count() > 0
                and page.locator('input[placeholder="输入您的电子邮箱"]').count() > 0
                and page.locator('input[placeholder="输入您的密码"]').count() > 0
            )
        except Exception:
            return False

    def _click_text_button(self, page, labels: tuple[str, ...] | list[str]) -> bool:
        for label in labels:
            try:
                locator = page.get_by_role("button", name=label).first
                if locator.count() > 0 and locator.is_visible():
                    locator.click(timeout=3000)
                    return True
            except Exception:
                pass
            try:
                locator = page.locator(f"button:has-text({json.dumps(label)})").first
                if locator.count() > 0 and locator.is_visible():
                    locator.click(timeout=3000)
                    return True
            except Exception:
                continue
        return False

    def _open_aliyun_challenge(self, page) -> None:
        self.log("Step3: 打开阿里云验证窗口 ...")
        try:
            page.evaluate(_ALIYUN_HOOK_JS)
        except Exception:
            pass
        clicked = False
        try:
            locator = page.locator("#aliyunCaptcha-captcha-wrapper").first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=3000)
                clicked = True
        except Exception:
            clicked = False
        if not clicked:
            clicked = self._click_text_button(page, _START_VERIFY_LABELS)
        if not clicked:
            raise RuntimeError("未找到 Z.ai 阿里云验证触发器")
        self._wait_for_aliyun_slide_ready(
            page,
            timeout=20.0,
            desc="等待 Z.ai 阿里云滑块窗口超时",
        )
        self.log(f"Z.ai Aliyun challenge open trace {self._debug_summary(page)}")

    def _wait_for_aliyun_slide_ready(self, page, *, timeout: float, desc: str) -> None:
        self._wait_until(
            lambda: self._slide_action_bbox(page) is not None
            and self._locator_bbox(page.locator("#aliyunCaptcha-sliding-slider").first) is not None,
            timeout=timeout,
            interval=0.2,
            desc=desc,
            page=page,
        )

    def _restore_aliyun_slide_after_refresh(self, page) -> None:
        try:
            self._wait_for_aliyun_slide_ready(
                page,
                timeout=8.0,
                desc="刷新后等待 Z.ai 阿里云滑块窗口超时",
            )
        except Exception:
            self._open_aliyun_challenge(page)

    def _solve_aliyun_slide(
        self,
        page,
        response_store: dict[str, Any] | None = None,
    ) -> Any:
        self.log("Step4: 调用图片识别服务解 Z.ai 阿里云滑块 ...")
        if not self.captcha_solver:
            raise RuntimeError("未配置图片验证码求解器")
        max_attempts = 3
        question = self._challenge_question(page)
        for attempt in range(1, max_attempts + 1):
            slide_bbox = self._slide_action_bbox(page)
            if not slide_bbox:
                raise RuntimeError("未找到 Z.ai 阿里云滑块截图区域")
            slider_handle_bbox = self._locator_bbox(page.locator("#aliyunCaptcha-sliding-slider").first)
            if not slider_handle_bbox:
                raise RuntimeError("未找到 Z.ai 阿里云滑块手柄")

            screenshot = self._screenshot_clip_with_hidden(
                page,
                slide_bbox,
                hidden_selectors=["#aliyunCaptcha-puzzle", "#aliyunCaptcha-btn-refresh"],
            )
            background_png = None
            img_bbox = self._locator_bbox(page.locator("#aliyunCaptcha-img-box").first)
            if img_bbox is not None:
                background_png = self._screenshot_clip_with_hidden(
                    page,
                    img_bbox,
                    hidden_selectors=["#aliyunCaptcha-puzzle", "#aliyunCaptcha-btn-refresh"],
                )
            piece_png = self._locator_screenshot(page.locator("#aliyunCaptcha-puzzle").first)
            screenshot_b64 = base64.b64encode(screenshot).decode("ascii")
            background_b64 = (
                base64.b64encode(background_png).decode("ascii")
                if background_png is not None
                else None
            )
            piece_b64 = (
                base64.b64encode(piece_png).decode("ascii")
                if piece_png is not None
                else None
            )
            action = self._recognize_slide_action(
                screenshot_b64,
                question=question,
                background_b64=background_b64,
                piece_b64=piece_b64,
            )
            slider = action.get("slider")
            gap = action.get("gap")
            if not isinstance(slider, dict) or not isinstance(gap, dict):
                raise RuntimeError(f"Z.ai 阿里云滑块识别缺少 slider/gap: {action!r}")
            image_size = action.get("imageSize") if isinstance(action.get("imageSize"), dict) else {}
            reference_width = self._read_number(image_size.get("width")) or 1440.0
            reference_height = self._read_number(image_size.get("height")) or 900.0

            start_x = slider_handle_bbox["x"] + (slider_handle_bbox["width"] / 2.0)
            start_y = slider_handle_bbox["y"] + (slider_handle_bbox["height"] / 2.0)
            end_x = self._resolve_slide_end_x(
                slide_bbox,
                gap,
                background_png=background_png,
                piece_png=piece_png,
                reference_width=reference_width,
                reference_height=reference_height,
                gap_source=str(action.get("gapSource") or "").strip(),
            )
            self._log_aliyun_action_trace(
                attempt=attempt,
                action=action,
                slide_bbox=slide_bbox,
                img_bbox=img_bbox,
                slider_handle_bbox=slider_handle_bbox,
                reference_width=reference_width,
                reference_height=reference_height,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                background_png=background_png,
                piece_png=piece_png,
            )
            self._drag_slider(page, start_x=start_x, start_y=start_y, end_x=end_x)
            self._sleep_with_checkpoint(0.45)
            try:
                return self._wait_for_aliyun_payload(
                    page,
                    timeout=8.0,
                    response_store=response_store,
                )
            except Exception:
                error = self._latest_aliyun_error(page)
                verify_code = str((error or {}).get("verifyCode") or "").strip().upper()
                if attempt < max_attempts and verify_code in _ALIYUN_RETRYABLE_VERIFY_CODES:
                    self.log(
                        f"Z.ai Aliyun verification rejected with {verify_code}, refresh challenge and retry {attempt + 1}/{max_attempts}"
                    )
                    self._refresh_aliyun_challenge(page)
                    self._sleep_with_checkpoint(0.9)
                    self._restore_aliyun_slide_after_refresh(page)
                    continue
                raise

    def _recognize_slide_action(
        self,
        screenshot_b64: str,
        *,
        question: str,
        background_b64: str | None = None,
        piece_b64: str | None = None,
    ) -> dict[str, Any]:
        solve_slide_action = getattr(self.captcha_solver, "solve_aliyun_slide_action", None)
        if callable(solve_slide_action):
            try:
                action = solve_slide_action(
                    screenshot_b64,
                    question=question,
                    background=background_b64,
                    piece=piece_b64,
                    timeout_s=45.0,
                    project_name=_ALIYUN_TASK_PROJECT_NAME,
                    schema_mode="slide",
                )
            except NotImplementedError:
                action = None
            if isinstance(action, dict):
                return action

        prompts = [
            f"{question}，返回 slide JSON，必须包含 action、slider、gap。",
            (
                f"{question}。这是滑块拼图题，不是点击题。"
                "只返回 slide JSON，格式示例："
                '{"action":"slide","slider":{"x":0,"y":0},"gap":{"x":0,"y":0}}'
            ),
        ]
        last_action: Any = None
        for prompt in prompts:
            raw = self.captcha_solver.solve_image(
                screenshot_b64,
                prompt=prompt,
                schema_mode="slide",
                timeout_s=45.0,
            )
            action = _parse_json_like_payload(raw, label="Z.ai 阿里云滑块识别")
            last_action = action
            if isinstance(action, dict) and isinstance(action.get("slider"), dict) and isinstance(action.get("gap"), dict):
                return action
        if isinstance(last_action, dict):
            return last_action
        raise RuntimeError(f"Z.ai 阿里云滑块识别结果异常: {last_action!r}")

    @staticmethod
    def _rounded_bbox(value: dict[str, Any] | None) -> dict[str, float] | None:
        if not isinstance(value, dict):
            return None
        result: dict[str, float] = {}
        for key in ("x", "y", "width", "height"):
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                result[key] = round(float(raw), 2)
        return result or None

    @staticmethod
    def _png_size(value: bytes | None) -> dict[str, int] | None:
        if not value:
            return None
        try:
            with Image.open(io.BytesIO(value)) as img:
                return {"width": int(img.width), "height": int(img.height)}
        except Exception:
            return None

    def _log_aliyun_action_trace(
        self,
        *,
        attempt: int,
        action: dict[str, Any],
        slide_bbox: dict[str, float],
        img_bbox: dict[str, float] | None,
        slider_handle_bbox: dict[str, float],
        reference_width: float,
        reference_height: float,
        start_x: float,
        start_y: float,
        end_x: float,
        background_png: bytes | None,
        piece_png: bytes | None,
    ) -> None:
        trace = {
            "attempt": attempt,
            "captchaType": action.get("captchaType"),
            "action": action.get("action"),
            "coordinateSpace": action.get("coordinateSpace"),
            "gapSource": action.get("gapSource"),
            "imageSize": action.get("imageSize"),
            "slider": action.get("slider"),
            "gap": action.get("gap"),
            "slideBBox": self._rounded_bbox(slide_bbox),
            "imageBBox": self._rounded_bbox(img_bbox),
            "sliderHandleBBox": self._rounded_bbox(slider_handle_bbox),
            "referenceSize": {
                "width": round(float(reference_width), 2),
                "height": round(float(reference_height), 2),
            },
            "mappedDrag": {
                "startX": round(float(start_x), 2),
                "startY": round(float(start_y), 2),
                "endX": round(float(end_x), 2),
                "distance": round(float(end_x - start_x), 2),
            },
            "backgroundSize": self._png_size(background_png),
            "pieceSize": self._png_size(piece_png),
        }
        self.log(f"Z.ai Aliyun action trace {json.dumps(trace, ensure_ascii=False, default=str)}")

    def _drag_slider(self, page, *, start_x: float, start_y: float, end_x: float) -> None:
        distance = end_x - start_x
        steps = 28
        page.mouse.move(start_x, start_y)
        page.mouse.down()
        page.wait_for_timeout(180)
        if self._has_live_piece(page):
            self._drag_slider_closed_loop(
                page,
                start_x=start_x,
                start_y=start_y,
                target_piece_center_x=end_x,
            )
        else:
            for step in range(1, steps + 1):
                progress = step / steps
                eased = 1 - math.pow(1 - progress, 2)
                x = start_x + (distance * eased)
                jitter = 0.8 if step % 2 == 0 else -0.8
                y = start_y if step == steps else start_y + jitter
                page.mouse.move(x, y)
                if step < steps:
                    page.wait_for_timeout(12 if step < steps - 4 else 18)
            self._finalize_drag_release(
                page,
                current_x=end_x,
                current_y=start_y,
            )
        page.wait_for_timeout(140)
        page.mouse.up()

    @staticmethod
    def _closed_loop_drag_step(delta: float) -> float:
        magnitude = abs(delta)
        if magnitude <= 4.0:
            return delta
        if magnitude <= 12.0:
            return math.copysign(max(1.5, magnitude * 0.55), delta)
        return math.copysign(min(22.0, max(4.0, magnitude * 0.55)), delta)

    def _drag_slider_closed_loop(
        self,
        page,
        *,
        start_x: float,
        start_y: float,
        target_piece_center_x: float,
    ) -> None:
        current_mouse_x = start_x
        for _ in range(24):
            piece_center_x = self._current_piece_center_x(page)
            if piece_center_x is None:
                break
            delta = target_piece_center_x - piece_center_x
            if abs(delta) <= 1.5:
                break
            step = self._closed_loop_drag_step(delta)
            current_mouse_x += step
            page.mouse.move(current_mouse_x, start_y)
            page.wait_for_timeout(30 if abs(step) >= 4.0 else 45)
        piece_center_x = self._current_piece_center_x(page)
        if piece_center_x is not None:
            delta = target_piece_center_x - piece_center_x
            if abs(delta) > 0.25:
                current_mouse_x += max(-3.0, min(3.0, delta))
                page.mouse.move(current_mouse_x, start_y)
                page.wait_for_timeout(45)
        self._finalize_drag_release(page, current_x=current_mouse_x, current_y=start_y)

    def _finalize_drag_release(self, page, *, current_x: float, current_y: float) -> None:
        page.mouse.move(current_x, current_y + 0.25)
        page.wait_for_timeout(80)
        page.mouse.move(current_x, current_y)
        page.wait_for_timeout(120)

    @staticmethod
    def _parse_aliyun_verify_request_post_data(post_data: str) -> Any:
        raw = str(post_data or "").strip()
        if not raw:
            return None
        try:
            payload = parse_qs(raw, keep_blank_values=True)
        except Exception:
            return None
        action = str((payload.get("Action") or [""])[0] or "").strip()
        if action != _ALIYUN_VERIFY_ACTION:
            return None
        verify_param = str((payload.get("CaptchaVerifyParam") or [""])[0] or "").strip()
        if not verify_param:
            return None
        try:
            parsed = json.loads(verify_param)
        except Exception:
            return verify_param
        return parsed if parsed not in ({}, [], "", None) else verify_param

    def _extract_aliyun_payload_from_response_store(
        self, response_store: dict[str, Any] | None
    ) -> Any:
        if not isinstance(response_store, dict):
            return None
        entry = response_store.get("aliyun_verify")
        if not isinstance(entry, dict):
            return None
        result_payload = entry.get("json")
        result_object = result_payload.get("Result") if isinstance(result_payload, dict) else None
        verify_result = result_object.get("VerifyResult") if isinstance(result_object, dict) else None
        if verify_result is False:
            return None
        parsed = self._parse_aliyun_verify_request_post_data(
            entry.get("request_post_data", "")
        )
        return parsed if parsed not in (None, "", {}, []) else None

    def _extract_aliyun_payload(
        self,
        page,
        response_store: dict[str, Any] | None = None,
    ) -> Any:
        try:
            payload = page.evaluate(_EXTRACT_ALIYUN_PAYLOAD_JS)
        except Exception:
            return None
        if isinstance(payload, dict) and payload.get("found"):
            value = payload.get("value")
            raw = payload.get("raw")
            if isinstance(value, str):
                return value.strip()
            if raw is not None:
                return raw
            return value
        return self._extract_aliyun_payload_from_response_store(response_store)

    def _get_aliyun_debug_state(self, page) -> dict[str, Any]:
        try:
            payload = page.evaluate(_DEBUG_ALIYUN_STATE_JS)
            if isinstance(payload, dict):
                return payload
        except Exception as exc:
            return {"url": str(page.url or ""), "debugError": str(exc)}
        return {"url": str(page.url or ""), "debugError": "invalid debug payload"}

    @staticmethod
    def _summarize_aliyun_requests(response_store: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(response_store, dict):
            return []
        entries = response_store.get("aliyun_requests")
        if not isinstance(entries, list):
            return []
        return [
            {
                key: entry.get(key)
                for key in (
                    "ts",
                    "method",
                    "status",
                    "host",
                    "path",
                    "action",
                    "hasCaptchaVerifyParam",
                    "captchaVerifyParamLength",
                    "captchaVerifyParamType",
                    "captchaVerifyParamKeys",
                    "verifyResult",
                    "verifyCode",
                )
                if key in entry
            }
            for entry in entries[-10:]
            if isinstance(entry, dict)
        ]

    def _debug_summary(
        self,
        page,
        *,
        response_store: dict[str, Any] | None = None,
    ) -> str:
        debug = self._get_aliyun_debug_state(page)
        snippet = {
            "url": debug.get("url"),
            "title": debug.get("title"),
            "hookInstalled": debug.get("hookInstalled"),
            "hasInitAliyunCaptcha": debug.get("hasInitAliyunCaptcha"),
            "hasInstance": debug.get("hasInstance"),
            "configs": debug.get("configs"),
            "payloadCount": debug.get("payloadCount"),
            "lastPayloadSource": debug.get("lastPayloadSource"),
            "errors": debug.get("errors"),
            "events": debug.get("events"),
            "submissionsBlocked": debug.get("submissionsBlocked"),
            "iframeCount": debug.get("iframeCount"),
            "iframes": debug.get("iframes"),
            "scripts": debug.get("scripts"),
            "elements": debug.get("elements"),
            "aliyunRequests": self._summarize_aliyun_requests(response_store),
        }
        return json.dumps(snippet, ensure_ascii=True, default=str)

    def _wait_for_aliyun_payload(
        self,
        page,
        *,
        timeout: float,
        response_store: dict[str, Any] | None = None,
    ) -> Any:
        deadline = time.time() + max(float(timeout or 0), 0.1)
        while time.time() < deadline:
            payload = self._extract_aliyun_payload(page, response_store=response_store)
            if payload not in (None, "", {}, []):
                return payload
            error = self._latest_aliyun_error(page)
            if isinstance(error, dict) and error.get("verifyResult") is False:
                break
            self._sleep_with_checkpoint(0.35)
        raise RuntimeError(
            "Z.ai 阿里云验证未产出 captchaVerifyParam; "
            f"debug={self._debug_summary(page, response_store=response_store)}"
        )

    def _click_aliyun_action_button(self, page) -> bool:
        return self._click_text_button(page, _ALIYUN_ACTION_BUTTON_LABELS)

    def _summarize_captcha_verify_param(self, captcha_verify_param: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {"type": type(captcha_verify_param).__name__}
        if captcha_verify_param in (None, "", {}, []):
            summary["empty"] = True
            return summary
        if isinstance(captcha_verify_param, str):
            summary["length"] = len(captcha_verify_param)
            return summary
        if isinstance(captcha_verify_param, dict):
            summary["keys"] = sorted(str(key) for key in captcha_verify_param.keys())
            token = captcha_verify_param.get("token")
            if isinstance(token, str):
                summary["token_length"] = len(token)
            scene_id = captcha_verify_param.get("sceneId")
            if isinstance(scene_id, str):
                summary["scene_id_length"] = len(scene_id)
            certify_id = captcha_verify_param.get("certifyId")
            if isinstance(certify_id, str):
                summary["certify_id_length"] = len(certify_id)
            return summary
        if isinstance(captcha_verify_param, (list, tuple, set)):
            summary["length"] = len(captcha_verify_param)
            return summary
        return summary

    @staticmethod
    def _serialize_captcha_verify_param(captcha_verify_param: Any) -> Any:
        if isinstance(captcha_verify_param, str):
            return captcha_verify_param.strip()
        if isinstance(captcha_verify_param, dict):
            return json.dumps(captcha_verify_param, ensure_ascii=False, separators=(",", ":"))
        if isinstance(captcha_verify_param, (list, tuple)):
            return json.dumps(list(captcha_verify_param), ensure_ascii=False, separators=(",", ":"))
        return captcha_verify_param

    def _latest_aliyun_error(self, page) -> dict[str, Any] | None:
        debug = self._get_aliyun_debug_state(page)
        errors = debug.get("errors")
        if isinstance(errors, list) and errors:
            latest = errors[-1]
            if isinstance(latest, dict):
                return latest
        return None

    def _refresh_aliyun_challenge(self, page) -> None:
        refresh_locator = page.locator("#aliyunCaptcha-btn-refresh").first
        try:
            if refresh_locator.count() > 0 and refresh_locator.is_visible():
                refresh_locator.click(timeout=3000)
                return
        except Exception:
            pass
        try:
            refreshed = page.evaluate(
                """() => {
                    const instance = window.__zaiAliyunState && window.__zaiAliyunState.instance;
                    if (!instance || typeof instance.refresh !== 'function') {
                        return false;
                    }
                    instance.refresh();
                    return true;
                }"""
            )
            if refreshed:
                return
        except Exception:
            pass

    def _submit_signup(
        self,
        page,
        response_store: dict[str, Any],
        *,
        name: str,
        email: str,
        password: str,
        captcha_verify_param: Any,
    ) -> dict[str, Any]:
        self.log("Step5: 提交 Z.ai 注册请求 ...")
        serialized_captcha_verify_param = self._serialize_captcha_verify_param(
            captcha_verify_param
        )
        captcha_summary = self._summarize_captcha_verify_param(
            serialized_captcha_verify_param
        )
        self.log(f"Step5: captcha payload summary {captcha_summary}")
        response_store["signup_captcha_summary"] = captcha_summary
        response_store.pop("signup", None)

        if self._click_text_button(page, _SIGNUP_SUBMIT_LABELS):
            self._wait_until(
                lambda: "signup" in response_store
                or "/auth/verify" in str(page.url or "")
                or any(
                    pattern in self._safe_body_text(page, limit=400)
                    for pattern in _VERIFY_PAGE_PATTERNS
                )
                or any(
                    pattern in self._safe_body_text(page, limit=400).lower()
                    for pattern in _SIGNUP_ERROR_PATTERNS
                ),
                timeout=45,
                interval=0.4,
                desc="等待 Z.ai 原生注册请求结果超时",
                page=page,
            )
            signup_result = response_store.get("signup") or {}
            if signup_result:
                if not isinstance(signup_result, dict):
                    raise RuntimeError(f"Z.ai signup 返回异常: {signup_result!r}")
                if signup_result.get("status", 0) >= 400:
                    detail = signup_result.get("json") or signup_result.get("text") or signup_result
                    raise RuntimeError(
                        f"Z.ai signup 返回异常: {detail}; captcha_summary={captcha_summary}"
                    )
                payload = signup_result.get("json")
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise RuntimeError(
                        f"Z.ai signup 返回异常: {payload}; captcha_summary={captcha_summary}"
                    )
                return signup_result
            body = self._safe_body_text(page, limit=600)
            if body and any(pattern in body.lower() for pattern in _SIGNUP_ERROR_PATTERNS):
                raise RuntimeError(
                    f"Z.ai 原生注册疑似失败: {body}; captcha_summary={captcha_summary}"
                )
            return {"ok": True, "status": 200, "text": "", "json": None}

        payload = {
            "name": name,
            "email": email,
            "password": password,
            "profile_image_url": "",
            "captcha_verify_param": serialized_captcha_verify_param,
        }
        signup_result = page.evaluate(
            """async (payload) => {
                const response = await fetch('/api/v1/auths/signup', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(payload),
                });
                const text = await response.text();
                let json = null;
                try {
                    json = text ? JSON.parse(text) : null;
                } catch (error) {
                    json = null;
                }
                return {
                    ok: response.ok,
                    status: response.status,
                    text,
                    json,
                };
            }""",
            payload,
        )
        response_store["signup"] = signup_result
        if not isinstance(signup_result, dict):
            raise RuntimeError(f"Z.ai signup 返回异常: {signup_result!r}")
        if not signup_result.get("ok"):
            detail = signup_result.get("json") or signup_result.get("text") or signup_result
            raise RuntimeError(f"Z.ai signup 返回异常: {detail}; captcha_summary={captcha_summary}")
        if isinstance(signup_result.get("json"), dict) and signup_result["json"].get("success") is not True:
            raise RuntimeError(f"Z.ai signup 返回异常: {signup_result['json']}; captcha_summary={captcha_summary}")
        verify_url = (
            "https://chat.z.ai/auth/verify"
            f"?email={quote(email, safe='')}"
            f"&username={quote(name, safe='')}"
        )
        page.goto(verify_url, wait_until="domcontentloaded", timeout=120000)
        return signup_result

    def _wait_for_verify_page(self, page, *, email: str) -> str:
        self.log("Step6: 等待 Z.ai 邮件验证页 ...")
        self._wait_until(
            lambda: any(pattern in self._safe_body_text(page, limit=400) for pattern in _VERIFY_PAGE_PATTERNS),
            timeout=30,
            interval=0.4,
            desc="等待 Z.ai 验证说明页超时",
            page=page,
        )
        parsed = urlparse(str(page.url or ""))
        username = parse_qs(parsed.query).get("username", [""])[0].strip()
        if username:
            return username
        return _default_display_name(email)

    def _open_verify_link(self, page, *, verify_link: str, response_store: dict[str, Any]) -> None:
        self.log("Step7: 打开 Z.ai 邮件验证链接 ...")
        page.goto(verify_link, wait_until="domcontentloaded", timeout=120000)
        self._wait_until(
            lambda: "verify_email" in str(page.url or "")
            or "verify_email" in response_store
            or page.locator('input[type="password"]').count() > 0,
            timeout=60,
            interval=0.4,
            desc="等待 Z.ai 邮件验证落地页超时",
            page=page,
        )
        verify_result = response_store.get("verify_email") or {}
        payload = verify_result.get("json")
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"Z.ai verify_email 返回异常: {payload}")

    def _finish_signup(self, page, *, password: str, response_store: dict[str, Any]) -> dict[str, Any]:
        self.log("Step8: 完成 Z.ai 注册并提取 token ...")
        response_store.pop("finish_signup", None)
        self._wait_until(
            lambda: page.locator('input[type="password"]').count() > 0,
            timeout=45,
            interval=0.4,
            desc="等待 Z.ai 完成注册密码框超时",
            page=page,
        )
        before_token = self._extract_auth_token(page)
        password_inputs = page.locator('input[type="password"]')
        password_count = int(password_inputs.count() or 0)
        if password_count <= 0:
            raise RuntimeError("Z.ai 完成注册页未找到密码输入框")
        for index in range(password_count):
            password_inputs.nth(index).fill(password)
        if not self._click_text_button(page, _COMPLETE_SIGNUP_LABELS):
            try:
                password_inputs.nth(max(0, password_count - 1)).press("Enter")
            except Exception:
                pass
        self._wait_until(
            lambda: "finish_signup" in response_store
            or self._extract_auth_token(page) != before_token
            or str(page.url or "").rstrip("/") == "https://chat.z.ai",
            timeout=90,
            interval=0.4,
            desc="等待 Z.ai finish_signup 结果超时",
            page=page,
        )
        finish_result = response_store.get("finish_signup") or {}
        finish_payload = finish_result.get("json")
        if isinstance(finish_payload, dict) and finish_payload.get("success") is False:
            raise RuntimeError(f"Z.ai finish_signup 返回异常: {finish_payload}")
        token = ""
        profile_image_url = ""
        if isinstance(finish_payload, dict):
            user = finish_payload.get("user")
            if isinstance(user, dict):
                token = str(user.get("token") or "").strip()
                profile_image_url = str(user.get("profile_image_url") or "").strip()
        if not token:
            token = self._extract_auth_token(page)
        if not token:
            raise RuntimeError("Z.ai 注册完成后未提取到 bearer token")
        return {"token": token, "profile_image_url": profile_image_url}

    def _extract_auth_token(self, page) -> str:
        token = self._extract_token_from_cookies(page)
        if token:
            return token
        return self._extract_token_from_storage(page)

    def _extract_token_from_cookies(self, page) -> str:
        context = getattr(page, "context", None)
        if context is None:
            return ""
        try:
            cookies = context.cookies("https://chat.z.ai/")
        except TypeError:
            try:
                cookies = context.cookies(["https://chat.z.ai/"])
            except Exception:
                return ""
        except Exception:
            return ""
        if not isinstance(cookies, list):
            return ""
        for item in cookies:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() != "token":
                continue
            token = str(item.get("value") or "").strip()
            if token:
                return token
        return ""

    def _extract_token_from_storage(self, page) -> str:
        for script in (
            "() => window.localStorage.getItem('token') || ''",
            "() => window.sessionStorage.getItem('token') || ''",
        ):
            try:
                token = str(page.evaluate(script) or "").strip()
            except Exception:
                token = ""
            if token:
                return token
        return ""

    @staticmethod
    def _safe_body_text(page, limit: int = 600) -> str:
        try:
            text = str(page.locator("body").inner_text(timeout=1500) or "").strip()
        except Exception:
            return ""
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    def _challenge_question(self, page) -> str:
        text = self._safe_body_text(page, limit=1200)
        line = self._extract_instruction_line(text, ("drag", "slider", "slide", "拼图", "滑块", "拖动", "滑动"))
        if line:
            return line
        return "请拖动滑块完成拼图"

    @staticmethod
    def _extract_instruction_line(text: str, hints: tuple[str, ...]) -> str | None:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        normalized_hints = tuple(str(token).lower() for token in hints)
        for line in lines:
            lowered = line.lower()
            if any(token in lowered for token in normalized_hints):
                return line
        return None

    @staticmethod
    def _read_number(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _map_point(
        self,
        bbox: dict[str, float],
        payload: dict[str, Any],
        *,
        reference_width: float = 1440.0,
        reference_height: float = 900.0,
    ) -> tuple[float, float]:
        x = self._read_number(payload.get("x"))
        y = self._read_number(payload.get("y"))
        if x is None or y is None:
            point = payload.get("point")
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                x = self._read_number(point[0])
                y = self._read_number(point[1])
        if x is None or y is None:
            raise RuntimeError(f"Z.ai 阿里云坐标异常: {payload!r}")
        width = max(float(reference_width or 1440.0), 1.0)
        height = max(float(reference_height or 900.0), 1.0)
        scale_x = bbox["width"] / width
        scale_y = bbox["height"] / height
        return bbox["x"] + (x * scale_x), bbox["y"] + (y * scale_y)

    def _resolve_slide_end_x(
        self,
        bbox: dict[str, float],
        gap_payload: dict[str, Any],
        *,
        background_png: bytes | None,
        piece_png: bytes | None,
        reference_width: float = 1440.0,
        reference_height: float = 900.0,
        gap_source: str | None = None,
    ) -> float:
        mapped_gap_x, _ = self._map_point(
            bbox,
            gap_payload,
            reference_width=reference_width,
            reference_height=reference_height,
        )
        if str(gap_source or "").strip().lower() == "image_estimator":
            return mapped_gap_x
        estimated_gap = self._estimate_gap_center_from_images(background_png, piece_png)
        if estimated_gap is None:
            return mapped_gap_x
        estimated_center_x, background_width = estimated_gap
        if background_width <= 0:
            return mapped_gap_x
        local_mapped_x = mapped_gap_x - bbox["x"]
        if abs(local_mapped_x - estimated_center_x) <= 10.0:
            return mapped_gap_x
        corrected_x = bbox["x"] + ((estimated_center_x / background_width) * bbox["width"])
        self.log(
            "Z.ai Aliyun gap corrected via image estimator "
            f"(llm_local_x={local_mapped_x:.2f}, estimated_local_x={estimated_center_x:.2f}, width={background_width})"
        )
        return corrected_x

    def _locator_bbox(self, locator) -> dict[str, float] | None:
        try:
            if locator.count() == 0 or not locator.is_visible():
                return None
            return locator.bounding_box()
        except Exception:
            return None

    def _locator_screenshot(self, locator) -> bytes | None:
        try:
            if locator.count() == 0 or not locator.is_visible():
                return None
            return locator.screenshot(type="png")
        except Exception:
            return None

    def _slide_action_bbox(self, page) -> dict[str, float] | None:
        img_bbox = self._locator_bbox(page.locator("#aliyunCaptcha-img-box").first)
        slider_bbox = self._locator_bbox(page.locator("#aliyunCaptcha-sliding-body").first)
        if not img_bbox or not slider_bbox:
            return None
        top = min(img_bbox["y"], slider_bbox["y"])
        bottom = max(img_bbox["y"] + img_bbox["height"], slider_bbox["y"] + slider_bbox["height"])
        return {
            "x": img_bbox["x"],
            "y": top,
            "width": img_bbox["width"],
            "height": bottom - top,
        }

    def _screenshot_clip_with_hidden(self, page, clip: dict[str, float], *, hidden_selectors: list[str]) -> bytes:
        hidden_state = page.evaluate(
            """(selectors) => selectors.map((selector) => {
                const element = document.querySelector(selector);
                if (!element) return null;
                const previous = element.getAttribute('style');
                element.style.visibility = 'hidden';
                return {selector, previous};
            })""",
            hidden_selectors,
        )
        try:
            return page.screenshot(type="png", clip=clip)
        finally:
            page.evaluate(
                """(entries) => {
                    for (const entry of entries) {
                        if (!entry) continue;
                        const element = document.querySelector(entry.selector);
                        if (!element) continue;
                        if (entry.previous === null) {
                            element.removeAttribute('style');
                        } else {
                            element.setAttribute('style', entry.previous);
                        }
                    }
                }""",
                hidden_state,
            )

    @staticmethod
    def _estimate_gap_center_from_images(
        background_png: bytes | None,
        piece_png: bytes | None,
    ) -> tuple[float, int] | None:
        if not background_png or not piece_png:
            return None
        try:
            background = Image.open(io.BytesIO(background_png)).convert("RGB")
            piece = Image.open(io.BytesIO(piece_png)).convert("RGBA")
        except Exception:
            return None

        bg_width, bg_height = background.size
        piece_width, piece_height = piece.size
        if piece_height != bg_height or piece_width > bg_width:
            return None

        piece_pixels = piece.load()
        background_pixels = background.load()
        mask_points: list[tuple[int, int]] = []
        mask_x_values: list[int] = []
        for y in range(piece_height):
            for x in range(piece_width):
                _, _, _, alpha = piece_pixels[x, y]
                if alpha <= 20:
                    continue
                mask_points.append((x, y))
                mask_x_values.append(x)

        if not mask_points or not mask_x_values:
            return None
        mask_center_x = float(min(mask_x_values) + max(mask_x_values)) / 2.0

        best_score = float("-inf")
        second_score = float("-inf")
        best_left = 0

        for left in range(0, bg_width - piece_width + 1):
            total = 0.0
            for point_x, point_y in mask_points:
                red, green, blue = background_pixels[left + point_x, point_y]
                max_channel = max(red, green, blue)
                min_channel = min(red, green, blue)
                luminance = (0.299 * red) + (0.587 * green) + (0.114 * blue)
                saturation = ((max_channel - min_channel) / max_channel) if max_channel > 0 else 0.0
                total += luminance - (0.7 * saturation * 255.0)
            score = total / len(mask_points)
            if score > best_score:
                second_score = best_score
                best_score = score
                best_left = left
            elif score > second_score:
                second_score = score

        if best_score == float("-inf"):
            return None
        if second_score != float("-inf") and (best_score - second_score) < 0.8:
            return None
        return best_left + mask_center_x, bg_width

    def _has_live_piece(self, page) -> bool:
        return self._current_piece_center_x(page) is not None

    def _current_piece_center_x(self, page) -> float | None:
        piece_bbox = self._locator_bbox(page.locator("#aliyunCaptcha-puzzle").first)
        if not piece_bbox:
            return None
        return piece_bbox["x"] + (piece_bbox["width"] / 2.0)

    def _fetch_current_user(self, token: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        session = requests.Session()
        session.trust_env = False
        proxies = build_requests_proxy_config(self.proxy)
        if proxies:
            session.proxies = proxies
        response = session.get(ZAI_ME_URL, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        raise RuntimeError(f"Z.ai 当前用户接口返回异常: {payload!r}")

    def _ensure_registered_user(self, user: dict[str, Any], *, expected_email: str) -> None:
        actual_email = str((user or {}).get("email") or "").strip()
        role = str((user or {}).get("role") or "").strip().lower()
        expected_email_text = str(expected_email or "").strip()
        if not actual_email:
            raise RuntimeError("Z.ai 当前用户接口未返回邮箱，无法确认注册完成")
        if role == "guest" or actual_email.lower().endswith("@guest.com"):
            raise RuntimeError(f"Z.ai 注册未完成，当前仍为 guest 会话: {actual_email}")
        if expected_email_text and actual_email.lower() != expected_email_text.lower():
            raise RuntimeError(
                f"Z.ai 注册后当前用户邮箱与预期不一致: expected={expected_email_text} actual={actual_email}"
            )

    def register(
        self,
        *,
        email: str,
        password: str,
        verification_link_callback: Callable[[], str] | None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        if not verification_link_callback:
            raise RuntimeError("Z.ai 注册需要邮箱验证链接回调")

        playwright = None
        browser = None
        context = None
        page = None
        response_store: dict[str, Any] = {}
        display_name_value = str(display_name or "").strip() or _default_display_name(email)

        try:
            playwright, browser = self._launch_browser()
            context = self._new_context(browser)
            page = context.new_page()
            self._install_response_capture(page, response_store)

            self._open_signup_page(page)
            self._fill_signup_form(page, name=display_name_value, email=email, password=password)
            captcha_verify_param = self._solve_aliyun_task(
                page,
                response_store=response_store,
            )
            if captcha_verify_param in (None, "", {}, []):
                raise RuntimeError("Z.ai 阿里云验证未产出 captchaVerifyParam")
            self._submit_signup(
                page,
                response_store,
                name=display_name_value,
                email=email,
                password=password,
                captcha_verify_param=captcha_verify_param,
            )
            username = self._wait_for_verify_page(page, email=email)

            verify_link = str(verification_link_callback() or "").strip()
            if not verify_link:
                raise RuntimeError("未收到 Z.ai 验证邮件链接")

            self._open_verify_link(page, verify_link=verify_link, response_store=response_store)
            finish = self._finish_signup(page, password=password, response_store=response_store)
            token = str(finish.get("token") or "").strip()
            user = self._fetch_current_user(token)
            self._ensure_registered_user(user, expected_email=email)

            return {
                "email": str(user.get("email") or email).strip() or email,
                "password": password,
                "token": token,
                "token_type": str(user.get("token_type") or "Bearer").strip() or "Bearer",
                "user_id": str(user.get("id") or "").strip(),
                "username": str(user.get("name") or username).strip() or username,
                "profile_image_url": str(finish.get("profile_image_url") or user.get("profile_image_url") or "").strip(),
                "captcha_verify_param": captcha_verify_param,
                "verify_link": verify_link,
                "signup_response": response_store.get("signup"),
                "verify_email_response": response_store.get("verify_email"),
                "finish_signup_response": response_store.get("finish_signup"),
            }
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                try:
                    if browser is not None:
                        browser.close()
                finally:
                    if playwright is not None:
                        playwright.stop()

    def _solve_aliyun_task(
        self,
        page,
        response_store: dict[str, Any] | None = None,
    ) -> Any:
        self._open_aliyun_challenge(page)
        return self._solve_aliyun_slide(page, response_store=response_store)


def verify_zai_token(token: str, proxy: str | None = None) -> bool:
    value = str(token or "").strip()
    if not value:
        return False
    session = requests.Session()
    session.trust_env = False
    proxies = build_requests_proxy_config(proxy)
    if proxies:
        session.proxies = proxies
    try:
        response = session.get(
            ZAI_ME_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {value}",
            },
            timeout=15,
        )
        return response.status_code < 400
    except Exception:
        return False
