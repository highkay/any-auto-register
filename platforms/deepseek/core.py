"""DeepSeek protocol registration/reset helpers."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import string
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, urlsplit

import requests

from core.browser_backend import sync_playwright
from core.browser_runtime import get_chrome_executable, with_chrome_executable
from core.http_client import HTTPClient, RequestConfig
from core.platform_email_domains import extract_email_domain
from core.proxy_utils import build_playwright_proxy_config

DEEPSEEK_BASE_URL = "https://chat.deepseek.com"
DEEPSEEK_USERS_API = f"{DEEPSEEK_BASE_URL}/api/v0/users"
DEEPSEEK_APP_VERSION = "20241129.1"
DEEPSEEK_CLIENT_VERSION = "2.0.0"
DEEPSEEK_DEFAULT_UI_LOCALE = "en-US"
DEEPSEEK_DEFAULT_REGION = "US"
DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS = "-25200"
DEEPSEEK_DEFAULT_TIMEZONE_ID = "America/Los_Angeles"
DEEPSEEK_DEFAULT_POW_WORKER_URL = (
    "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
)
DEEPSEEK_POW_WORKER_HOST_PAGE_URL = "https://fe-static.deepseek.com/chat/"
DEEPSEEK_TURNSTILE_SITEKEY = "0x4AAAAAAA1jQEh8YFk064tz"
DEEPSEEK_HCAPTCHA_SITEKEY = "352e5376-f2cc-43fe-a744-e51640449610"
DEEPSEEK_HCAPTCHA_TIMEOUT_SECONDS = 180.0
DEEPSEEK_HCAPTCHA_POLL_INTERVAL_SECONDS = 3.0
DEEPSEEK_HCAPTCHA_REQUEST_TIMEOUT_SECONDS = 30.0
DEEPSEEK_MANUAL_SEND_CODE_TIMEOUT_SECONDS = 600
DEEPSEEK_SIGN_UP_URL = f"{DEEPSEEK_BASE_URL}/sign_up"
DEEPSEEK_SIGN_IN_URL = f"{DEEPSEEK_BASE_URL}/sign_in"
DEEPSEEK_FORGOT_PASSWORD_URL = f"{DEEPSEEK_BASE_URL}/forgot_password"
DEEPSEEK_TURNSTILE_TOKEN_MIN_LENGTH = 20
DEEPSEEK_FLARESOLVERR_COOKIE_MARKERS = {"cf_clearance", "__cf_bm"}
DEEPSEEK_RESEND_COUNTDOWN_RE = re.compile(r"\bresend\s+after\s+\d+\s*s\b", re.I)
_DEEPSEEK_LOCAL_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DEEPSEEK_DISABLED_VALUES = {"0", "false", "no", "off", "none", "disabled"}
_DEEPSEEK_CHROME_VERSION_RE = re.compile(
    r"Chrome/(?P<major>\d+)\.(?P<minor>\d+)\.(?P<build>\d+)\.(?P<patch>\d+)"
)
_DEEPSEEK_FLARESOLVERR_USER_AGENT_CACHE: dict[str, str] = {}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)


class DeepSeekEmailDomainRejected(RuntimeError):
    def __init__(self, email: str, response: dict[str, Any] | None = None):
        self.email = str(email or "").strip()
        self.domain = extract_email_domain(self.email)
        self.response = response or {}
        detail = self.domain or self.email or "-"
        super().__init__(
            f"DeepSeek 邮箱域名不支持: {detail}; response={self.response}"
        )


_POW_SOLVE_EVAL = """
async ({ challenge, workerUrl }) => {
  const worker = new Worker(workerUrl)
  try {
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('worker timeout')), 30000)
      worker.onmessage = (event) => {
        const data = event.data || {}
        if (data.type === 'pow-answer') {
          clearTimeout(timer)
          resolve(data.answer)
          return
        }
        if (data.type === 'pow-error') {
          clearTimeout(timer)
          const message =
            data.error?.message ||
            data.error?.toString?.() ||
            JSON.stringify(data.error || data)
          reject(new Error(message))
          return
        }
        clearTimeout(timer)
        reject(new Error(JSON.stringify(data)))
      }
      worker.onerror = (event) => {
        clearTimeout(timer)
        reject(new Error(event.message || 'worker error'))
      }
      worker.postMessage({
        type: 'pow-challenge',
        challenge: {
          algorithm: challenge.algorithm,
          challenge: challenge.challenge,
          salt: challenge.salt,
          difficulty: challenge.difficulty,
          signature: challenge.signature,
          expireAt: challenge.expire_at,
        },
      })
    })
  } finally {
    worker.terminate()
  }
}
"""

_DEEPSEEK_POW_SOLVER_LOCK = threading.Lock()


def build_deepseek_page_url(path: str, ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE) -> str:
    _ = ui_locale
    return f"{DEEPSEEK_BASE_URL}{path}"


def normalize_deepseek_ui_locale(ui_locale: str) -> str:
    value = str(ui_locale or "").strip().replace("_", "-")
    if not value:
        return DEEPSEEK_DEFAULT_UI_LOCALE
    parts = [part.strip() for part in value.split("-") if part.strip()]
    if not parts:
        return DEEPSEEK_DEFAULT_UI_LOCALE
    if len(parts) == 1:
        return parts[0].lower()
    return f"{parts[0].lower()}-{parts[1].upper()}"


def extract_deepseek_client_locale(ui_locale: str) -> str:
    normalized = normalize_deepseek_ui_locale(ui_locale)
    return normalized.replace("-", "_")


def extract_deepseek_language(ui_locale: str) -> str:
    client_locale = extract_deepseek_client_locale(ui_locale)
    return client_locale.split("_", 1)[0].strip().lower() or "en"


def build_deepseek_accept_language(ui_locale: str) -> str:
    normalized = normalize_deepseek_ui_locale(ui_locale)
    return f"{normalized},{extract_deepseek_language(normalized)};q=0.9"


def random_password(length: int = 18) -> str:
    chars = string.ascii_letters + string.digits
    core = "".join(random.choice(chars) for _ in range(max(length - 4, 10)))
    return f"Aa1!{core}"


def random_device_id(prefix: str = "probe") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def mask_email(email: str) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 6:
        return f"{local[:1]}***@{domain}"
    return f"{local[:3]}***{local[-3:]}@{domain}"


def resolve_deepseek_flaresolverr_url(candidate: str | None = None) -> str:
    resolved = str(candidate or "").strip()
    if not resolved:
        return ""
    return resolved if resolved.endswith("/v1") else f"{resolved.rstrip('/')}/v1"


def _resolve_deepseek_flaresolverr_loopback_proxy_host() -> str:
    enabled = str(
        os.getenv("DEEPSEEK_FLARESOLVERR_BRIDGE_LOOPBACK_PROXY", "true") or "true"
    ).strip().lower()
    if enabled in _DEEPSEEK_DISABLED_VALUES:
        return ""
    value = str(
        os.getenv("DEEPSEEK_FLARESOLVERR_LOOPBACK_PROXY_HOST")
        or "host.docker.internal"
    ).strip()
    if value.lower() in _DEEPSEEK_DISABLED_VALUES:
        return ""
    return value


def _normalize_deepseek_flaresolverr_proxy_server(
    server: str,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> str:
    parts = urlsplit(server)
    host = str(parts.hostname or "").strip()
    if not parts.scheme or not host or parts.port is None:
        return server
    if host.lower() not in _DEEPSEEK_LOCAL_LOOPBACK_HOSTS:
        return server
    bridge_host = _resolve_deepseek_flaresolverr_loopback_proxy_host()
    if not bridge_host or bridge_host.lower() == host.lower():
        return server
    if ":" in bridge_host and not bridge_host.startswith("["):
        netloc = f"[{bridge_host}]:{parts.port}"
    else:
        netloc = f"{bridge_host}:{parts.port}"
    normalized = parts._replace(netloc=netloc).geturl()
    if log_fn:
        log_fn(
            "  FlareSolverr 代理为本机回环地址，已改写为"
            f" {normalized} 以便容器内浏览器访问"
        )
    return normalized


def _collect_deepseek_flaresolverr_proxy_url(
    proxy: str | None,
    *,
    log_fn: Callable[[str], None] | None = None,
) -> Optional[str]:
    if not proxy:
        return None
    try:
        proxy_cfg = build_playwright_proxy_config(proxy)
    except Exception:
        return None
    if not proxy_cfg:
        return None
    server = str(proxy_cfg.get("server") or "").strip()
    if not server:
        return None
    server = _normalize_deepseek_flaresolverr_proxy_server(
        server,
        log_fn=log_fn,
    )
    username = str(proxy_cfg.get("username") or "").strip()
    password = str(proxy_cfg.get("password") or "").strip()
    if not username and not password:
        return server
    parts = urlsplit(server)
    if not parts.scheme or not parts.hostname or parts.port is None:
        return server
    auth = quote(username, safe="")
    if password:
        auth = f"{auth}:{quote(password, safe='')}"
    return f"{parts.scheme}://{auth}@{parts.hostname}:{parts.port}"


def _resolve_deepseek_browser_user_agent(flaresolverr_url: str | None = None) -> str:
    endpoint = resolve_deepseek_flaresolverr_url(flaresolverr_url)
    if not endpoint:
        return USER_AGENT
    root_url = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
    cached = _DEEPSEEK_FLARESOLVERR_USER_AGENT_CACHE.get(root_url)
    if cached:
        return cached
    try:
        resp = requests.get(root_url, timeout=10)
        resp.raise_for_status()
        payload = resp.json() if getattr(resp, "content", b"") else {}
        user_agent = str((payload or {}).get("userAgent") or "").strip()
        if user_agent:
            _DEEPSEEK_FLARESOLVERR_USER_AGENT_CACHE[root_url] = user_agent
            return user_agent
    except Exception:
        pass
    _DEEPSEEK_FLARESOLVERR_USER_AGENT_CACHE[root_url] = USER_AGENT
    return USER_AGENT


def _build_deepseek_browser_identity_override(
    browser_user_agent: str,
    *,
    accept_language: str,
) -> dict[str, Any]:
    user_agent = str(browser_user_agent or "").strip() or USER_AGENT
    match = _DEEPSEEK_CHROME_VERSION_RE.search(user_agent)
    major = str((match.group("major") if match else "") or "135")
    full_version = ".".join(
        (
            str((match.group("major") if match else "") or "135"),
            str((match.group("minor") if match else "") or "0"),
            str((match.group("build") if match else "") or "0"),
            str((match.group("patch") if match else "") or "0"),
        )
    )
    platform = "Linux x86_64"
    metadata_platform = "Linux"
    platform_version = "6.0.0"
    if "Windows NT" in user_agent:
        platform = "Win32"
        metadata_platform = "Windows"
        platform_version = "10.0.0"
    elif "Mac OS X" in user_agent:
        platform = "MacIntel"
        metadata_platform = "macOS"
        platform_version = "14.0.0"
    return {
        "userAgent": user_agent,
        "acceptLanguage": accept_language,
        "platform": platform,
        "userAgentMetadata": {
            "brands": [
                {"brand": "Google Chrome", "version": major},
                {"brand": "Chromium", "version": major},
                {"brand": "Not/A)Brand", "version": "99"},
            ],
            "fullVersion": full_version,
            "platform": metadata_platform,
            "platformVersion": platform_version,
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        },
    }


def _apply_deepseek_browser_identity(
    context,
    page,
    *,
    browser_user_agent: str,
    accept_language: str,
    log_fn: Callable[[str], None] | None = None,
) -> None:
    try:
        cdp = context.new_cdp_session(page)
        cdp.send(
            "Emulation.setUserAgentOverride",
            _build_deepseek_browser_identity_override(
                browser_user_agent,
                accept_language=accept_language,
            ),
        )
        if log_fn:
            log_fn("[DeepSeek] 浏览器身份已对齐到 FlareSolverr Chrome 指纹")
    except Exception as exc:
        if log_fn:
            log_fn(f"[DeepSeek] 浏览器身份对齐失败，继续原链: {exc}")


def _extract_deepseek_flaresolverr_error_detail(response: Any) -> str:
    message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = str(
                payload.get("message")
                or payload.get("error")
                or payload.get("msg")
                or ""
            ).strip()
    except Exception:
        message = ""
    if not message:
        message = str(getattr(response, "text", "") or "").strip()
    message = re.sub(r"\s+", " ", message).strip()
    if len(message) > 500:
        message = f"{message[:500]}..."
    return message


def _raise_for_deepseek_flaresolverr_status(response: Any, context: str) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = getattr(response, "status_code", "")
        detail = _extract_deepseek_flaresolverr_error_detail(response)
        suffix = f": {detail}" if detail else f": {exc}"
        raise RuntimeError(f"{context} HTTP {status_code}{suffix}") from exc


def _extract_deepseek_flaresolverr_turnstile_token(solution: Any) -> str:
    if not isinstance(solution, dict):
        return ""
    for key in ("turnstileToken", "turnstile_token", "token"):
        value = str(solution.get(key) or "").strip()
        if len(value) >= DEEPSEEK_TURNSTILE_TOKEN_MIN_LENGTH:
            return value
    response = str(solution.get("response") or "")
    if not response:
        return ""
    patterns = (
        r'name=["\']cf-turnstile-response["\'][^>]*value=["\']([^"\']{20,})',
        r'id=["\']cf-chl-widget-[^"\']+["\'][^>]*value=["\']([^"\']{20,})',
    )
    for pattern in patterns:
        match = re.search(pattern, response, flags=re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()
    return ""


def _apply_deepseek_flaresolverr_cookies(
    page, cookies: list[dict[str, Any]]
) -> list[str]:
    if not cookies:
        return []
    payload = []
    names = []
    seen: set[tuple[str, str, str]] = set()
    for raw_cookie in cookies:
        name = str(raw_cookie.get("name") or "").strip()
        value = str(raw_cookie.get("value") or "")
        if not name:
            continue
        domain = (
            str(raw_cookie.get("domain") or DEEPSEEK_BASE_URL.replace("https://", "")).strip()
            or DEEPSEEK_BASE_URL.replace("https://", "")
        )
        path = str(raw_cookie.get("path") or "/").strip() or "/"
        dedupe_key = (name, domain, path)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        same_site = str(raw_cookie.get("sameSite") or "Lax").strip().title()
        if same_site not in {"Lax", "None", "Strict"}:
            same_site = "Lax"
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "httpOnly": bool(raw_cookie.get("httpOnly")),
            "secure": bool(raw_cookie.get("secure", True)),
            "sameSite": same_site,
        }
        expiry = raw_cookie.get("expiry")
        try:
            if expiry not in (None, ""):
                item["expires"] = float(expiry)
        except Exception:
            pass
        payload.append(item)
        names.append(name)
    if payload:
        page.context.add_cookies(payload)
    return names


def _request_deepseek_flaresolverr_solution(
    *,
    log_fn: Callable[[str], None],
    stage_label: str,
    target_url: str,
    proxy: str | None = None,
    flaresolverr_url: str | None = None,
) -> dict[str, Any]:
    endpoint = resolve_deepseek_flaresolverr_url(flaresolverr_url)
    if not endpoint:
        raise RuntimeError("未配置可用的 FlareSolverr endpoint")
    proxy_url = _collect_deepseek_flaresolverr_proxy_url(proxy, log_fn=log_fn)
    proxy_label = "task" if proxy_url else "none"
    log_fn(
        f"  {stage_label}: 调用 FlareSolverr 预热 DeepSeek 会话态 (proxy={proxy_label})"
    )
    session_id = f"deepseek-flare-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
    http = requests.Session()
    http.trust_env = False
    solution: dict[str, Any] = {}
    try:
        max_attempts = max(1, int(os.getenv("DEEPSEEK_FLARESOLVERR_ATTEMPTS") or 3))
    except Exception:
        max_attempts = 3
    try:
        create_payload: dict[str, Any] = {
            "cmd": "sessions.create",
            "session": session_id,
        }
        if proxy_url:
            create_payload["proxy"] = {"url": proxy_url}
        create_resp = http.post(endpoint, json=create_payload, timeout=30)
        _raise_for_deepseek_flaresolverr_status(
            create_resp,
            "FlareSolverr 创建 session 失败",
        )
        create_data = create_resp.json()
        if str(create_data.get("status") or "").lower() != "ok":
            raise RuntimeError(
                create_data.get("message") or "FlareSolverr 创建 session 失败"
            )

        for attempt in range(1, max_attempts + 1):
            req_payload = {
                "cmd": "request.get",
                "session": session_id,
                "url": target_url,
                "maxTimeout": 120000,
            }
            resp = http.post(endpoint, json=req_payload, timeout=150)
            _raise_for_deepseek_flaresolverr_status(
                resp,
                "FlareSolverr 请求失败",
            )
            data = resp.json()
            if str(data.get("status") or "").lower() != "ok":
                raise RuntimeError(data.get("message") or "FlareSolverr 请求失败")
            solution = data.get("solution") or {}
            token = _extract_deepseek_flaresolverr_turnstile_token(solution)
            cookie_names = sorted(
                {
                    str(cookie.get("name") or "").strip()
                    for cookie in (solution.get("cookies") or [])
                    if str(cookie.get("name") or "").strip()
                }
            )
            log_fn(
                f"  {stage_label}: FlareSolverr attempt {attempt}: "
                f"cookies={cookie_names} token={'yes' if token else 'no'}"
            )
            if token or "cf_clearance" in cookie_names:
                break
            if (
                attempt < max_attempts
                and DEEPSEEK_FLARESOLVERR_COOKIE_MARKERS.intersection(cookie_names)
            ):
                log_fn(
                    f"  {stage_label}: 尚未拿到 cf_clearance，继续预热 FlareSolverr 会话态"
                )
                time.sleep(1.0)
                continue
            if attempt >= max_attempts and cookie_names:
                log_fn(
                    f"  {stage_label}: 最终仍未拿到 cf_clearance，使用当前 cookies 继续"
                )
                break
        return solution
    finally:
        try:
            http.post(
                endpoint,
                json={"cmd": "sessions.destroy", "session": session_id},
                timeout=20,
            )
        except Exception:
            pass
        http.close()


def _prewarm_deepseek_session_with_flaresolverr(
    page,
    *,
    log_fn: Callable[[str], None],
    proxy: str | None,
    target_url: str,
    stage_label: str = "FlareSolverr",
    flaresolverr_url: str | None = None,
    reload_after: bool = False,
) -> dict[str, Any]:
    solution = _request_deepseek_flaresolverr_solution(
        log_fn=log_fn,
        stage_label=stage_label,
        target_url=target_url,
        proxy=proxy,
        flaresolverr_url=flaresolverr_url,
    )
    injected_names = _apply_deepseek_flaresolverr_cookies(
        page,
        solution.get("cookies") or [],
    )
    solution["injectedCookieNames"] = sorted(set(injected_names))
    if injected_names:
        log_fn(
            "  FlareSolverr cookies 已注入当前 DeepSeek 上下文:"
            f" {sorted(set(injected_names))}"
        )
    if reload_after:
        log_fn("  已注入会话态，刷新当前 DeepSeek 页面以重建验证组件")
        page.reload(wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1500)
    return solution


def _restore_deepseek_sign_up_form_after_flaresolverr_reload(
    page,
    *,
    email: str,
    password: str,
    log_fn: Callable[[str], None],
    stage_label: str,
) -> None:
    _accept_deepseek_cookie_banner(page)
    _wait_for_deepseek_sign_up_form(page, timeout_ms=15000)
    log_fn(f"[DeepSeek] {stage_label} 预热后回到注册页，重新填写邮箱与密码")
    email_input = page.locator(
        'input.ds-input__input[type="text"], input.ds-input__input[type="email"]'
    ).first
    password_inputs = page.locator('input.ds-input__input[type="password"]')
    _fill_deepseek_input(email_input, email, field_name="email")
    _fill_deepseek_input(password_inputs.nth(0), password, field_name="password")
    _fill_deepseek_input(
        password_inputs.nth(1),
        password,
        field_name="confirm_password",
    )


def _read_deepseek_turnstile_token(page) -> str:
    return str(
        page.evaluate(
            r"""() => {
                const selectors = [
                    'input[id^="cf-chl-widget-"]',
                    'input[name="cf-turnstile-response"]',
                    'textarea[name="cf-turnstile-response"]',
                    'textarea[name="g-recaptcha-response"]',
                ];
                const inputs = [];
                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach((node) => inputs.push(node));
                }
                for (const input of inputs) {
                    const value = String(input.value || '').trim();
                    if (value) return value;
                }
                try {
                    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        for (const input of inputs) {
                            const widgetId = String(
                                input.getAttribute('data-widget-id') ||
                                input.getAttribute('data-widgetid') ||
                                ''
                            ).trim();
                            if (widgetId) {
                                const response = String(window.turnstile.getResponse(widgetId) || '').trim();
                                if (response) return response;
                            }
                        }
                        return String(window.turnstile.getResponse() || '').trim();
                    }
                } catch (_) {}
                return '';
            }"""
        )
        or ""
    ).strip()


def _wait_deepseek_turnstile_token(page, *, wait_rounds: int = 20, wait_ms: int = 400) -> str:
    for _ in range(wait_rounds):
        token = _read_deepseek_turnstile_token(page)
        if len(token) >= DEEPSEEK_TURNSTILE_TOKEN_MIN_LENGTH:
            return token
        page.wait_for_timeout(max(wait_ms, 1))
    return ""


def _has_deepseek_turnstile_runtime(page) -> bool:
    try:
        if any(
            "challenges.cloudflare.com" in str(getattr(frame, "url", "") or "")
            for frame in getattr(page, "frames", []) or []
        ):
            return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                """() => Boolean(
                    (window.turnstile && typeof window.turnstile.render === 'function') ||
                    document.getElementById('cf-turnstile') ||
                    document.querySelector('iframe[src*="challenges.cloudflare.com"]')
                )"""
            )
        )
    except Exception:
        return False


def _extract_deepseek_turnstile_sitekey_from_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
    except Exception:
        return ""
    for candidate in ("k", "sitekey"):
        value = str(parse_qs(parsed.query).get(candidate, [""])[0] or "").strip()
        if value:
            return value
    return ""


def _read_deepseek_turnstile_sitekey(page) -> str:
    sitekey = str(
        page.evaluate(
            """() => {
                const explicit = document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey');
                if (explicit) return explicit;
                for (const iframe of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
                    try {
                        const parsed = new URL(iframe.src, location.href);
                        const value = parsed.searchParams.get('k') || parsed.searchParams.get('sitekey');
                        if (value) return value;
                    } catch (_) {}
                }
                return '';
            }"""
        )
        or ""
    ).strip()
    if sitekey:
        return sitekey
    try:
        frame_urls = [
            str(frame.url or "")
            for frame in getattr(page, "frames", []) or []
            if "challenges.cloudflare.com" in str(getattr(frame, "url", "") or "")
        ]
    except Exception:
        frame_urls = []
    for url in frame_urls:
        sitekey = _extract_deepseek_turnstile_sitekey_from_url(url)
        if sitekey:
            return sitekey
    has_turnstile_runtime = False
    try:
        has_turnstile_runtime = bool(
            page.evaluate(
                """() => Boolean(
                    document.getElementById('cf-turnstile') ||
                    document.getElementById('cf-overlay') ||
                    document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')
                )"""
            )
        )
    except Exception:
        has_turnstile_runtime = False
    if has_turnstile_runtime:
        return DEEPSEEK_TURNSTILE_SITEKEY
    return ""


def _find_deepseek_turnstile_widget(page) -> tuple[Any | None, dict[str, Any] | None]:
    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" not in str(getattr(frame, "url", "") or ""):
                continue
            try:
                frame_el = frame.frame_element()
                box = frame_el.bounding_box()
            except Exception:
                box = None
            if box and box["width"] > 100 and box["height"] >= 50:
                return frame, box
    except Exception:
        pass
    return None, None


def _render_deepseek_turnstile_widget(page, sitekey: str) -> bool:
    resolved_sitekey = str(sitekey or "").strip()
    if not resolved_sitekey:
        return False
    try:
        return bool(
            page.evaluate(
                """async (sitekey) => {
                    const overlay = document.getElementById('cf-overlay');
                    const mount = document.getElementById('cf-turnstile');
                    if (!mount) return false;
                    if (overlay) {
                        overlay.style.display = 'block';
                    }
                    const waitReady = async () => {
                        const deadline = Date.now() + 15000;
                        while (Date.now() < deadline) {
                            if (window.turnstile && typeof window.turnstile.render === 'function') {
                                return true;
                            }
                            await new Promise((resolve) => setTimeout(resolve, 250));
                        }
                        return false;
                    };
                    if (!(await waitReady())) return false;
                    if (!document.querySelector('iframe[src*="challenges.cloudflare.com"]')) {
                        try {
                            window.turnstile.render('#cf-turnstile', { sitekey });
                        } catch (_) {}
                    }
                    await new Promise((resolve) => setTimeout(resolve, 1200));
                    return Boolean(document.querySelector('iframe[src*="challenges.cloudflare.com"]'));
                }""",
                resolved_sitekey,
            )
        )
    except Exception:
        return False


def _set_deepseek_turnstile_overlay_visibility(page, *, visible: bool) -> None:
    try:
        page.evaluate(
            """(visible) => {
                const overlay = document.getElementById('cf-overlay');
                if (overlay) {
                    overlay.style.display = visible ? 'block' : 'none';
                }
            }""",
            bool(visible),
        )
    except Exception:
        pass


def _reset_deepseek_turnstile_widget(page) -> bool:
    try:
        return bool(
            page.evaluate(
                r"""() => {
                    try {
                        if (window.turnstile && typeof window.turnstile.reset === 'function') {
                            const widgetIds = [];
                            for (const input of document.querySelectorAll('input[id^="cf-chl-widget-"]')) {
                                const match = String(input.id || '').match(/^cf-chl-widget-([A-Za-z0-9_-]+)_response$/);
                                if (match && match[1]) widgetIds.push(match[1]);
                            }
                            for (const node of document.querySelectorAll('[data-widget-id], [data-widgetid]')) {
                                const value = String(
                                    node.getAttribute('data-widget-id') ||
                                    node.getAttribute('data-widgetid') ||
                                    ''
                                ).trim();
                                if (value) widgetIds.push(value);
                            }
                            if (widgetIds.length) {
                                for (const widgetId of widgetIds) {
                                    window.turnstile.reset(widgetId);
                                }
                                return true;
                            }
                            window.turnstile.reset();
                            return true;
                        }
                    } catch (_) {}
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _kick_deepseek_turnstile_widget(page) -> str:
    try:
        return str(
            page.evaluate(
                r"""() => {
                    function isVisible(node) {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                            return false;
                        }
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }

                    function dispatchClick(node) {
                        if (!node) return false;
                        try {
                            const rect = node.getBoundingClientRect();
                            const clickOffsetX = Math.min(
                                Math.max(rect.width * 0.18, 12),
                                Math.max(rect.width - 6, 12),
                            );
                            const clientX = rect.left + clickOffsetX;
                            const clientY = rect.top + rect.height / 2;
                            const screenX = Math.floor(800 + Math.random() * 401);
                            const screenY = Math.floor(400 + Math.random() * 301);
                            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                                node.dispatchEvent(new MouseEvent(type, {
                                    bubbles: true,
                                    cancelable: true,
                                    composed: true,
                                    clientX,
                                    clientY,
                                    screenX,
                                    screenY,
                                }));
                            }
                            if (typeof node.click === 'function') node.click();
                            return true;
                        } catch (_) {}
                        return false;
                    }

                    const responseInput =
                        document.querySelector('input[name="cf-turnstile-response"]') ||
                        document.querySelector('textarea[name="cf-turnstile-response"]') ||
                        document.querySelector('input[id^="cf-chl-widget-"]');
                    if (responseInput) {
                        let current = responseInput;
                        for (let depth = 0; current && depth < 8; depth += 1) {
                            const shadowFrame = current.shadowRoot && current.shadowRoot.querySelector('iframe');
                            if (shadowFrame && isVisible(shadowFrame) && dispatchClick(shadowFrame)) {
                                return depth === 0
                                    ? 'response-input-shadow-iframe'
                                    : `response-ancestor-shadow-iframe:${depth}`;
                            }
                            const shadowTarget = current.shadowRoot && (
                                current.shadowRoot.querySelector('input[type="checkbox"]') ||
                                current.shadowRoot.querySelector('[role="checkbox"]') ||
                                current.shadowRoot.querySelector('input') ||
                                current.shadowRoot.querySelector('label') ||
                                current.shadowRoot.querySelector('button')
                            );
                            if (shadowTarget && dispatchClick(shadowTarget)) {
                                return depth === 0
                                    ? 'response-input-shadow-target'
                                    : `response-ancestor-shadow-target:${depth}`;
                            }
                            if (isVisible(current) && dispatchClick(current)) {
                                return depth === 0
                                    ? 'response-input-parent'
                                    : `response-ancestor-click:${depth}`;
                            }
                            current = current.parentElement;
                        }
                    }

                    const candidates = [
                        document.getElementById('cf-turnstile'),
                        document.getElementById('cf-overlay'),
                        document.querySelector('[data-sitekey]'),
                        document.querySelector('iframe[src*="challenges.cloudflare.com"]'),
                    ];
                    for (const node of candidates) {
                        if (isVisible(node) && dispatchClick(node)) {
                            return node.id || node.tagName.toLowerCase();
                        }
                    }

                    for (const node of Array.from(document.querySelectorAll('div, span, iframe, label, button'))) {
                        const text = String(node.textContent || '').trim();
                        if (
                            (text && /one more step before you proceed/i.test(text)) ||
                            String(node.id || '').includes('turnstile') ||
                            String(node.className || '').includes('turnstile')
                        ) {
                            if (isVisible(node) && dispatchClick(node)) {
                                return node.id || node.tagName.toLowerCase();
                            }
                        }
                    }
                    return '';
                }"""
            )
            or ""
        ).strip()
    except Exception:
        return ""


def _reuse_deepseek_turnstile_on_current_page(
    page,
    *,
    log_fn: Callable[[str], None],
) -> str:
    for attempt in range(1, 4):
        token = _wait_deepseek_turnstile_token(page, wait_rounds=1, wait_ms=1)
        if token:
            log_fn(f"[DeepSeek] Turnstile token: {token[:40]}...")
            return token

        frame, box = _find_deepseek_turnstile_widget(page)
        _set_deepseek_turnstile_overlay_visibility(page, visible=True)
        host_action = _kick_deepseek_turnstile_widget(page)
        if not box:
            action_desc = host_action or "wait-only"
            log_fn(f"[DeepSeek] Turnstile reuse #{attempt}: via {action_desc}")
            if host_action:
                page.wait_for_timeout(900)
            token = _wait_deepseek_turnstile_token(page, wait_rounds=18, wait_ms=450)
            if token:
                log_fn(f"[DeepSeek] Turnstile token: {token[:40]}...")
                return token
            page.wait_for_timeout(900 + attempt * 180)
            continue

        click_offset_x = min(28, max(18, box["width"] * 0.08))
        click_x = box["x"] + click_offset_x
        click_y = box["y"] + box["height"] / 2
        action_desc = host_action or "widget-click"
        log_fn(
            f"[DeepSeek] Turnstile reuse #{attempt}: ({click_x:.1f}, {click_y:.1f}) via {action_desc}"
        )
        try:
            if frame is not None:
                frame.locator("body").click(
                    position={
                        "x": click_offset_x,
                        "y": box["height"] / 2,
                    },
                    timeout=2500,
                )
                page.wait_for_timeout(150)
            page.mouse.move(click_x, click_y)
            page.mouse.down()
            page.wait_for_timeout(120)
            page.mouse.up()
        except Exception:
            pass

        token = _wait_deepseek_turnstile_token(page, wait_rounds=36, wait_ms=450)
        if token:
            log_fn(f"[DeepSeek] Turnstile token: {token[:40]}...")
            return token

        page.wait_for_timeout(900 + attempt * 180)
    return ""


def _inject_deepseek_turnstile_token(page, token: str) -> bool:
    return bool(
        page.evaluate(
            """(token) => {
                const selectors = [
                    'input[id^="cf-chl-widget-"]',
                    'input[name="cf-turnstile-response"]',
                    'textarea[name="cf-turnstile-response"]',
                    'textarea[name="g-recaptcha-response"]',
                ];
                const inputs = [];
                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach((node) => inputs.push(node));
                }
                if (!inputs.length) {
                    const fallback = document.createElement('input');
                    fallback.type = 'hidden';
                    fallback.name = 'cf-turnstile-response';
                    document.body.appendChild(fallback);
                    inputs.push(fallback);
                }
                for (const input of inputs) {
                    input.value = token;
                    input.setAttribute('value', token);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }
                return inputs.length > 0;
            }""",
            token,
        )
    )


def _capture_deepseek_storage_snapshot(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const localStorageData = {};
            const sessionStorageData = {};
            try {
                for (let idx = 0; idx < window.localStorage.length; idx += 1) {
                    const key = window.localStorage.key(idx);
                    if (!key) continue;
                    localStorageData[key] = window.localStorage.getItem(key);
                }
            } catch (_) {}
            try {
                for (let idx = 0; idx < window.sessionStorage.length; idx += 1) {
                    const key = window.sessionStorage.key(idx);
                    if (!key) continue;
                    sessionStorageData[key] = window.sessionStorage.getItem(key);
                }
            } catch (_) {}
            return {
                origin: window.location.origin,
                localStorage: localStorageData,
                sessionStorage: sessionStorageData,
            };
        }"""
    ) or {}


def _collect_deepseek_turnstile_session_state(page) -> dict[str, Any]:
    try:
        cookies = page.context.cookies()
    except Exception:
        cookies = []
    storage_snapshot = _capture_deepseek_storage_snapshot(page)
    viewport = getattr(page, "viewport_size", None) or {}
    if not viewport:
        try:
            viewport = page.evaluate(
                """() => ({
                    width: Math.max(window.innerWidth || 0, 1),
                    height: Math.max(window.innerHeight || 0, 1),
                })"""
            ) or {}
        except Exception:
            viewport = {}
    runtime = page.evaluate(
        """() => ({
            userAgent: navigator.userAgent || '',
            locale: navigator.language || '',
            timezoneId: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
        })"""
    ) or {}
    origins = []
    if storage_snapshot and storage_snapshot.get("origin"):
        origins.append(storage_snapshot)
    return {
        "cookies": cookies,
        "origins": origins,
        "userAgent": str(runtime.get("userAgent") or USER_AGENT),
        "viewport": {
            "width": int(viewport.get("width") or 1440),
            "height": int(viewport.get("height") or 1080),
        },
        "locale": str(runtime.get("locale") or DEEPSEEK_DEFAULT_UI_LOCALE),
        "timezoneId": str(runtime.get("timezoneId") or DEEPSEEK_DEFAULT_TIMEZONE_ID),
    }


def _collect_deepseek_turnstile_widget_hints(page) -> dict[str, Any]:
    frame_url = ""
    try:
        for candidate in getattr(page, "frames", []) or []:
            url = str(getattr(candidate, "url", "") or "")
            if "challenges.cloudflare.com" in url:
                frame_url = url
                break
    except Exception:
        frame_url = ""
    hints: dict[str, Any] = {
        "responseInputSelector": 'input[name="cf-turnstile-response"]',
    }
    if frame_url:
        hints["frameUrl"] = frame_url
    return hints


def _collect_deepseek_turnstile_runtime_hints(page) -> dict[str, Any]:
    body_text = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=2000) or "")
    except Exception:
        body_text = ""
    return {
        "stepLabel": "deepseek_send_code",
        "tokenMinLength": DEEPSEEK_TURNSTILE_TOKEN_MIN_LENGTH,
        "runtimeReady": True,
        "pageBodyText": body_text[:220],
    }


def _collect_deepseek_turnstile_solver_proxy(
    proxy: str | None,
) -> Optional[dict[str, str]]:
    if not proxy:
        return None
    try:
        return build_playwright_proxy_config(proxy)
    except Exception:
        return None


def _solve_deepseek_turnstile_token(
    captcha_solver,
    *,
    page,
    page_url: str,
    sitekey: str,
    proxy: str | None,
    log_fn: Callable[[str], None],
    interrupt_checker=None,
) -> str:
    if not captcha_solver:
        return ""
    resolved_sitekey = str(sitekey or "").strip()
    if not resolved_sitekey:
        return ""
    try:
        render_ready = _render_deepseek_turnstile_widget(page, resolved_sitekey)
        if render_ready:
            page.wait_for_timeout(400)
            existing = _wait_deepseek_turnstile_token(page, wait_rounds=3, wait_ms=300)
            if existing:
                log_fn(f"[DeepSeek] 页面内已获取 Turnstile token: {existing[:40]}...")
                return existing
        errors: list[str] = []
        solve_turnstile_session = getattr(captcha_solver, "solve_turnstile_session", None)
        if callable(solve_turnstile_session):
            try:
                browser_proxy = _collect_deepseek_turnstile_solver_proxy(proxy)
                proxy_label = "task" if browser_proxy else "none"
                log_fn(
                    "[DeepSeek] 求解 Turnstile 同会话 token "
                    f"(sitekey={resolved_sitekey[:8]}..., proxy={proxy_label})"
                )
                solution = solve_turnstile_session(
                    page_url,
                    resolved_sitekey,
                    session_state=_collect_deepseek_turnstile_session_state(page),
                    widget_hints=_collect_deepseek_turnstile_widget_hints(page),
                    runtime_hints=_collect_deepseek_turnstile_runtime_hints(page),
                    browser_proxy=browser_proxy,
                    options={
                        "pageLoadTimeoutMs": 30000,
                        "solveTimeoutMs": 90000,
                        "maxAttempts": 2,
                        "captureDebugArtifacts": True,
                    },
                    interrupt_checker=interrupt_checker,
                )
                token = str((solution or {}).get("token") or "").strip()
                if token:
                    log_fn(
                        "[DeepSeek] 已获取 Turnstile 同会话 token "
                        f"(mode={(solution or {}).get('solverMode')}, attempts={(solution or {}).get('attempts')})"
                    )
                    if _inject_deepseek_turnstile_token(page, token):
                        page.wait_for_timeout(400)
                        return _wait_deepseek_turnstile_token(page, wait_rounds=2, wait_ms=250) or token
                    return token
            except Exception as exc:
                errors.append(f"同会话 Turnstile 求解失败: {exc}")
        solve_turnstile = getattr(captcha_solver, "solve_turnstile", None)
        if callable(solve_turnstile):
            try:
                log_fn(
                    "[DeepSeek] 求解 Turnstile "
                    f"(sitekey={resolved_sitekey[:8]}...)"
                )
                token = str(
                    solve_turnstile(
                        page_url,
                        resolved_sitekey,
                        timeout_seconds=DEEPSEEK_HCAPTCHA_TIMEOUT_SECONDS,
                        poll_interval_seconds=DEEPSEEK_HCAPTCHA_POLL_INTERVAL_SECONDS,
                        request_timeout_seconds=DEEPSEEK_HCAPTCHA_REQUEST_TIMEOUT_SECONDS,
                        interrupt_checker=interrupt_checker,
                    )
                    or ""
                ).strip()
                if token:
                    log_fn(f"[DeepSeek] 已获取 Turnstile token: {token[:40]}...")
                    if _inject_deepseek_turnstile_token(page, token):
                        page.wait_for_timeout(400)
                        return _wait_deepseek_turnstile_token(page, wait_rounds=2, wait_ms=250) or token
                    return token
            except Exception as exc:
                errors.append(f"Turnstile 求解失败: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return ""
    finally:
        _set_deepseek_turnstile_overlay_visibility(page, visible=False)


def _build_deepseek_send_code_request_route(
    *,
    turnstile_token: str = "",
    hcaptcha_token: str = "",
    guest_pow_response: str = "",
):
    def _handler(route):
        continue_kwargs: dict[str, Any] = {}
        try:
            post_data = str(route.request.post_data or "").strip()
            payload = json.loads(post_data or "{}")
            if isinstance(payload, dict):
                changed = False
                current_turnstile = str(payload.get("turnstile_token") or "").strip()
                if not current_turnstile and turnstile_token:
                    payload["turnstile_token"] = turnstile_token
                    changed = True
                current_hcaptcha = str(payload.get("hcaptcha_token") or "").strip()
                if not current_hcaptcha and hcaptcha_token:
                    payload["hcaptcha_token"] = hcaptcha_token
                    changed = True
                if changed:
                    continue_kwargs["post_data"] = json.dumps(
                        payload,
                        ensure_ascii=False,
                    )
        except Exception:
            pass
        if guest_pow_response:
            try:
                headers = dict(route.request.headers)
                if not str(headers.get("x-ds-guest-pow-response") or "").strip():
                    headers["x-ds-guest-pow-response"] = guest_pow_response
                    continue_kwargs["headers"] = headers
            except Exception:
                pass
        if continue_kwargs:
            route.continue_(**continue_kwargs)
            return
        route.continue_()

    return _handler


def _build_deepseek_turnstile_request_route(token: str):
    return _build_deepseek_send_code_request_route(turnstile_token=token)


def _build_deepseek_guest_pow_header_route(guest_pow_response: str):
    def _handler(route):
        if guest_pow_response:
            try:
                headers = dict(route.request.headers)
                if not str(headers.get("x-ds-guest-pow-response") or "").strip():
                    headers["x-ds-guest-pow-response"] = guest_pow_response
                    route.continue_(headers=headers)
                    return
            except Exception:
                pass
        route.continue_()

    return _handler


def _extract_deepseek_guest_challenge(data: dict[str, Any]) -> dict[str, Any]:
    challenge = (
        data.get("data", {})
        .get("biz_data", {})
        .get("guest_challenge", {})
    )
    if not challenge:
        raise RuntimeError(f"DeepSeek guest challenge 响应异常: {data}")
    return challenge


def _is_deepseek_email_domain_not_supported(inner: dict[str, Any]) -> bool:
    if not isinstance(inner, dict):
        return False
    text = " ".join(
        str(inner.get(key) or "")
        for key in ("biz_msg", "biz_message", "message", "msg", "error")
    ).upper()
    return "EMAIL_DOMAIN_NOT_SUPPORTED" in text


def _read_deepseek_hcaptcha_token(page) -> str:
    token = page.evaluate(
        """() => {
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
                    const value = String(window.hcaptcha.getResponse() || '').trim();
                    if (value) return value;
                }
            } catch (_) {}
            return '';
        }""",
    )
    return str(token or "").strip()


def _encode_deepseek_guest_pow_response_in_page(
    page,
    challenge: dict[str, Any],
    *,
    pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
) -> str:
    if not challenge:
        raise RuntimeError("DeepSeek guest challenge 为空")
    answer = page.evaluate(
        _POW_SOLVE_EVAL,
        {
            "challenge": challenge,
            "workerUrl": str(pow_worker_url or DEEPSEEK_DEFAULT_POW_WORKER_URL),
        },
    )
    salt = str(answer.get("salt") or challenge.get("salt") or "").strip()
    raw_answer = answer.get("answer")
    if not salt:
        raise RuntimeError(f"DeepSeek PoW 返回缺少 salt: {answer}")
    if raw_answer is None:
        raise RuntimeError(f"DeepSeek PoW 返回缺少 answer: {answer}")
    body = json.dumps({"salt": salt, "answer": int(raw_answer)}, separators=(",", ":"))
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


def _encode_deepseek_guest_pow_response_with_context_page(
    page,
    challenge: dict[str, Any],
    *,
    pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
) -> str:
    pow_page = None
    try:
        pow_page = page.context.new_page()
        pow_page.goto(
            DEEPSEEK_POW_WORKER_HOST_PAGE_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        return _encode_deepseek_guest_pow_response_in_page(
            pow_page,
            challenge,
            pow_worker_url=pow_worker_url,
        )
    finally:
        if pow_page is not None:
            try:
                pow_page.close()
            except Exception:
                pass


def _request_deepseek_guest_pow_response_via_browser(
    page,
    *,
    target_path: str,
    proxy: str | None = None,
    ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
    sign_up_url: str | None = None,
    tz_offset_seconds: str = DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
    pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
) -> str:
    data = page.evaluate(
        """async ({ targetPath, locale, tzOffset, appVersion, clientVersion }) => {
            const response = await fetch('/api/v0/users/create_guest_challenge', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'accept': '*/*',
                    'content-type': 'application/json',
                    'x-app-version': appVersion,
                    'x-client-locale': locale,
                    'x-client-platform': 'web',
                    'x-client-timezone-offset': tzOffset,
                    'x-client-version': clientVersion
                },
                body: JSON.stringify({ target_path: targetPath })
            });
            const text = await response.text();
            try {
                return JSON.parse(text);
            } catch (_) {
                return { status: response.status, body: text };
            }
        }""",
        {
            "targetPath": target_path,
            "locale": extract_deepseek_client_locale(ui_locale),
            "tzOffset": str(tz_offset_seconds or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS),
            "appVersion": DEEPSEEK_APP_VERSION,
            "clientVersion": DEEPSEEK_CLIENT_VERSION,
        },
    )
    challenge = _extract_deepseek_guest_challenge(data)
    return _encode_deepseek_guest_pow_response_with_context_page(
        page,
        challenge,
        pow_worker_url=pow_worker_url,
    )


def _solve_deepseek_hcaptcha_token(
    captcha_solver,
    *,
    page_url: str,
    sitekey: str,
    log_fn: Callable[[str], None],
    interrupt_checker=None,
) -> str:
    if not captcha_solver:
        return ""
    solve_hcaptcha = getattr(captcha_solver, "solve_hcaptcha", None)
    if not callable(solve_hcaptcha):
        return ""
    resolved_sitekey = str(sitekey or "").strip()
    if not resolved_sitekey:
        return ""
    log_fn(
        "[DeepSeek] 求解 hCaptcha "
        f"(sitekey={resolved_sitekey[:8]}...)"
    )
    token = str(
        solve_hcaptcha(
            page_url,
            resolved_sitekey,
            timeout_seconds=DEEPSEEK_HCAPTCHA_TIMEOUT_SECONDS,
            poll_interval_seconds=DEEPSEEK_HCAPTCHA_POLL_INTERVAL_SECONDS,
            request_timeout_seconds=DEEPSEEK_HCAPTCHA_REQUEST_TIMEOUT_SECONDS,
            interrupt_checker=interrupt_checker,
        )
        or ""
    ).strip()
    if token:
        log_fn(f"[DeepSeek] 已获取 hCaptcha token: {token[:40]}...")
    return token


def _solve_deepseek_turnstile_by_flaresolverr(
    page,
    *,
    email: str,
    password: str,
    log_fn: Callable[[str], None],
    proxy: str | None,
    flaresolverr_url: str | None = None,
) -> str:
    current_url = str(getattr(page, "url", "") or "").strip() or DEEPSEEK_SIGN_UP_URL
    solution = _prewarm_deepseek_session_with_flaresolverr(
        page,
        log_fn=log_fn,
        proxy=proxy,
        target_url=current_url,
        stage_label="浏览器发码前",
        flaresolverr_url=flaresolverr_url,
        reload_after=True,
    )
    _restore_deepseek_sign_up_form_after_flaresolverr_reload(
        page,
        email=email,
        password=password,
        log_fn=log_fn,
        stage_label="浏览器发码前",
    )
    token = _extract_deepseek_flaresolverr_turnstile_token(solution)
    if token:
        if _inject_deepseek_turnstile_token(page, token):
            return _wait_deepseek_turnstile_token(page, wait_rounds=2, wait_ms=250) or token
        return token
    sitekey = _read_deepseek_turnstile_sitekey(page)
    if sitekey and _has_deepseek_turnstile_runtime(page):
        _render_deepseek_turnstile_widget(page, sitekey)
        token = _wait_deepseek_turnstile_token(page, wait_rounds=3, wait_ms=300)
        if token:
            return token
    _reset_deepseek_turnstile_widget(page)
    page.wait_for_timeout(700)
    token = _reuse_deepseek_turnstile_on_current_page(page, log_fn=log_fn)
    if token:
        return token
    token = _wait_deepseek_turnstile_token(page, wait_rounds=8, wait_ms=450)
    return token


def _resolve_deepseek_send_code_challenge_tokens(
    *,
    page,
    email: str,
    password: str,
    sign_up_url: str,
    captcha_solver,
    hcaptcha_sitekey: str,
    proxy: str | None,
    flaresolverr_url: str | None,
    log_fn: Callable[[str], None],
    interrupt_checker=None,
) -> tuple[str, str]:
    turnstile_token = ""
    turnstile_sitekey = _read_deepseek_turnstile_sitekey(page)
    if turnstile_sitekey:
        try:
            turnstile_token = _solve_deepseek_turnstile_token(
                captcha_solver,
                page=page,
                page_url=sign_up_url,
                sitekey=turnstile_sitekey,
                proxy=proxy,
                log_fn=log_fn,
                interrupt_checker=interrupt_checker,
            )
        except Exception as exc:
            log_fn(f"[DeepSeek] Turnstile 求解失败，继续原链: {exc}")
    if not turnstile_token and flaresolverr_url:
        try:
            turnstile_token = _solve_deepseek_turnstile_by_flaresolverr(
                page,
                email=email,
                password=password,
                log_fn=log_fn,
                proxy=proxy,
                flaresolverr_url=flaresolverr_url,
            )
            if turnstile_token:
                log_fn(
                    "[DeepSeek] FlareSolverr 已获取 Turnstile token: "
                    f"{turnstile_token[:40]}..."
                )
        except Exception as exc:
            log_fn(f"[DeepSeek] FlareSolverr Turnstile 预热失败，继续原链: {exc}")

    hcaptcha_token = ""
    try:
        hcaptcha_token = _read_deepseek_hcaptcha_token(page)
        if hcaptcha_token:
            log_fn(
                "[DeepSeek] 页面内已存在 hCaptcha token: "
                f"{hcaptcha_token[:40]}..."
            )
        elif hcaptcha_sitekey:
            hcaptcha_token = _solve_deepseek_hcaptcha_token(
                captcha_solver,
                page_url=sign_up_url,
                sitekey=hcaptcha_sitekey,
                log_fn=log_fn,
                interrupt_checker=interrupt_checker,
            )
    except Exception as exc:
        log_fn(f"[DeepSeek] 读取页面内 hCaptcha token 失败，继续原链: {exc}")
    return turnstile_token, hcaptcha_token


def _deepseek_browser_launch_attempts() -> list[dict[str, Any]]:
    chrome = get_chrome_executable()
    attempts: list[dict[str, Any]] = []
    if chrome:
        attempts.append({"executable_path": chrome})
    attempts.extend([{"channel": "chrome"}, {}, {"channel": "msedge"}])
    return attempts


def _launch_deepseek_browser(playwright, *, headless: bool, proxy: str | None = None):
    proxy_cfg = build_playwright_proxy_config(proxy) if proxy else None
    last_error: Exception | None = None
    for extra in _deepseek_browser_launch_attempts():
        launch_kwargs: dict[str, Any] = with_chrome_executable(
            {"headless": headless, **extra}
        )
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"DeepSeek 浏览器启动失败: {last_error}") from last_error


def _configure_deepseek_sign_up_page(page, *, ui_locale: str) -> None:
    normalized_locale = normalize_deepseek_ui_locale(ui_locale)
    page.add_init_script(
        f"""() => {{
            try {{
                Object.defineProperty(navigator, 'language', {{ get: () => {json.dumps(normalized_locale)} }});
                Object.defineProperty(navigator, 'languages', {{ get: () => [{json.dumps(normalized_locale)}, 'en'] }});
                localStorage.setItem('webLocalePreference', {json.dumps(normalized_locale.replace('-', '_'))});
                localStorage.setItem('webLocale', {json.dumps(normalized_locale.replace('-', '_'))});
            }} catch (err) {{}}
        }}"""
    )


def _open_deepseek_sign_up_browser_page(
    playwright,
    *,
    proxy: str | None,
    ui_locale: str,
    headless: bool,
    user_data_dir: str | None = None,
    flaresolverr_url: str | None = None,
    align_flaresolverr_identity: bool = False,
    log_fn: Callable[[str], None] | None = None,
):
    sign_up_url = build_deepseek_page_url("/sign_up", ui_locale)
    normalized_locale = normalize_deepseek_ui_locale(ui_locale)
    accept_language = build_deepseek_accept_language(normalized_locale)
    browser_user_agent = (
        _resolve_deepseek_browser_user_agent(flaresolverr_url)
        if align_flaresolverr_identity
        else USER_AGENT
    )
    browser = None
    if str(user_data_dir or "").strip():
        proxy_cfg = build_playwright_proxy_config(proxy) if proxy else None
        profile_dir = Path(str(user_data_dir).strip()).expanduser().resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        context = None
        for extra in _deepseek_browser_launch_attempts():
            launch_kwargs: dict[str, Any] = with_chrome_executable(
                {
                    "headless": headless,
                    "locale": normalized_locale,
                    "user_agent": browser_user_agent,
                    "timezone_id": DEEPSEEK_DEFAULT_TIMEZONE_ID,
                    "viewport": {"width": 1440, "height": 1080},
                    **extra,
                }
            )
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir),
                    **launch_kwargs,
                )
                browser = context.browser
                break
            except Exception as exc:
                last_error = exc
        if context is None:
            raise RuntimeError(f"DeepSeek 持久化浏览器启动失败: {last_error}") from last_error
    else:
        browser = _launch_deepseek_browser(
            playwright,
            headless=headless,
            proxy=proxy,
        )
        context = browser.new_context(
            locale=normalized_locale,
            user_agent=browser_user_agent,
            timezone_id=DEEPSEEK_DEFAULT_TIMEZONE_ID,
            viewport={"width": 1440, "height": 1080},
        )
    context.set_extra_http_headers({"Accept-Language": accept_language})
    page = context.pages[0] if context.pages else context.new_page()
    if align_flaresolverr_identity:
        _apply_deepseek_browser_identity(
            context,
            page,
            browser_user_agent=browser_user_agent,
            accept_language=accept_language,
            log_fn=log_fn,
        )
    _configure_deepseek_sign_up_page(page, ui_locale=normalized_locale)
    if align_flaresolverr_identity:
        effective_log = log_fn or (lambda *_: None)
        try:
            _prewarm_deepseek_session_with_flaresolverr(
                page,
                log_fn=effective_log,
                proxy=proxy,
                target_url=sign_up_url,
                stage_label="注册页前",
                flaresolverr_url=flaresolverr_url,
                reload_after=False,
            )
        except Exception as exc:
            if log_fn:
                log_fn(f"[DeepSeek] 注册页前 FlareSolverr 预热失败，继续原链: {exc}")
    page.goto(sign_up_url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(5000)
    _accept_deepseek_cookie_banner(page)
    return browser, context, page, sign_up_url


def ensure_deepseek_email_sign_up_available_via_browser(
    *,
    proxy: str | None = None,
    ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
    headless: bool = True,
    user_data_dir: str | None = None,
    flaresolverr_url: str | None = None,
) -> dict[str, Any]:
    with sync_playwright() as p:
        browser = None
        context = None
        try:
            browser, context, page, sign_up_url = _open_deepseek_sign_up_browser_page(
                p,
                proxy=proxy,
                ui_locale=ui_locale,
                headless=headless,
                user_data_dir=user_data_dir,
                flaresolverr_url=flaresolverr_url,
                align_flaresolverr_identity=bool(flaresolverr_url),
                log_fn=None,
            )
            _wait_for_deepseek_sign_up_form(page)
            state = _collect_deepseek_form_state(page)
            classification = _classify_deepseek_sign_up_state(state)
            result = {
                "classification": classification,
                "sign_up_url": sign_up_url,
                "state": state,
                "summary": _summarize_deepseek_sign_up_state(
                    state,
                    classification=classification,
                ),
            }
            if classification == "email_form":
                return result
            if classification == "phone_only":
                raise RuntimeError(
                    "DeepSeek 当前出口命中手机号注册页，不支持邮箱注册: "
                    + result["summary"]
                )
            raise RuntimeError("DeepSeek 注册页前置检查失败: " + result["summary"])
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()


def _accept_deepseek_cookie_banner(page) -> None:
    for label in (
        "Accept all cookies",
        "Accept All",
        "Allow all",
        "必要なクッキーのみ",
        "すべてのCookieを受け入れる",
    ):
        locator = page.get_by_role("button", name=label)
        if locator.count() == 0:
            continue
        try:
            locator.first.click(timeout=2000)
            page.wait_for_timeout(500)
            return
        except Exception:
            continue


def _collect_deepseek_form_state(page) -> dict[str, Any]:
    try:
        inputs = page.locator("input.ds-input__input").evaluate_all(
            """nodes => nodes.map((node, index) => ({
                index,
                type: node.getAttribute('type') || '',
                placeholder: node.getAttribute('placeholder') || '',
                value: node.value || ''
            }))"""
        )
    except Exception as exc:
        inputs = [{"error": repr(exc)}]

    try:
        buttons = page.locator("button").evaluate_all(
            """nodes => nodes.map((node, index) => ({
                index,
                text: (node.textContent || '').trim(),
                className: node.className || ''
            }))"""
        )
    except Exception as exc:
        buttons = [{"error": repr(exc)}]

    return {
        "url": str(page.url or ""),
        "title": str(page.title() or ""),
        "body": str(page.locator("body").inner_text(timeout=3000) or "")[:1200],
        "inputs": inputs,
        "buttons": buttons,
    }


def _classify_deepseek_sign_up_state(state: dict[str, Any]) -> str:
    body = str((state or {}).get("body") or "")
    body_lower = body.lower()
    inputs = list((state or {}).get("inputs") or [])
    buttons = list((state or {}).get("buttons") or [])

    has_email_input = any(
        isinstance(item, dict) and str(item.get("type") or "").lower() in {"text", "email"}
        for item in inputs
    )
    password_count = sum(
        1
        for item in inputs
        if isinstance(item, dict) and str(item.get("type") or "").lower() == "password"
    )
    has_send_button = any(
        isinstance(item, dict)
        and "verify-code-input-countdown" in str(item.get("className") or "")
        for item in buttons
    )
    phone_placeholder_hits = (
        "電話番号" in body
        or "手机号" in body
        or "phone number" in body_lower
    )
    phone_only_copy_hits = (
        "電話番号での登録のみ対応しています" in body
        or "仅支持手机号注册" in body
        or "only phone number registration is supported" in body_lower
    )
    china_dial_code_hits = "+86" in body

    if has_email_input and password_count >= 2 and has_send_button:
        return "email_form"
    if (
        not has_email_input
        and password_count >= 2
        and has_send_button
        and (phone_placeholder_hits or phone_only_copy_hits or china_dial_code_hits)
    ):
        return "phone_only"
    if phone_only_copy_hits:
        return "phone_only"
    return "unknown"


def _summarize_deepseek_sign_up_state(state: dict[str, Any], *, classification: str | None = None) -> str:
    payload = state or {}
    resolved = classification or _classify_deepseek_sign_up_state(payload)
    body = str(payload.get("body") or "").replace("\r", " ").replace("\n", " ").strip()
    if len(body) > 180:
        body = body[:180] + "..."
    return (
        f"classification={resolved} "
        f"title={str(payload.get('title') or '-')!r} "
        f"url={str(payload.get('url') or '-')!r} "
        f"body={body!r}"
    )


def _wait_for_deepseek_sign_up_form(page, *, timeout_ms: int = 30000) -> None:
    initial_state = _collect_deepseek_form_state(page)
    initial_classification = _classify_deepseek_sign_up_state(initial_state)
    if initial_classification == "email_form":
        return
    if initial_classification == "phone_only":
        raise RuntimeError(
            "DeepSeek 当前出口命中手机号注册页，不支持邮箱注册: "
            + _summarize_deepseek_sign_up_state(
                initial_state,
                classification=initial_classification,
            )
        )

    try:
        page.wait_for_function(
            """() => {
                const email = document.querySelector(
                    'input.ds-input__input[type="text"], input.ds-input__input[type="email"]'
                );
                const passwords = document.querySelectorAll('input.ds-input__input[type="password"]');
                const sendButton = document.querySelector('button.ds-verify-code-input-countdown');
                if (!!email && passwords.length >= 2 && !!sendButton) {
                    return true;
                }
                const bodyText = (document.body?.innerText || '').toLowerCase();
                const phoneOnlyCopy =
                    bodyText.includes('only phone number registration is supported') ||
                    bodyText.includes('仅支持手机号注册') ||
                    bodyText.includes('電話番号での登録のみ対応しています');
                const phonePlaceholder = Array.from(
                    document.querySelectorAll('input.ds-input__input[type="tel"]')
                ).some((node) => {
                    const value = (node.getAttribute('placeholder') || '').toLowerCase();
                    return (
                        value.includes('phone') ||
                        value.includes('電話') ||
                        value.includes('手机号')
                    );
                });
                return (
                    !email &&
                    passwords.length >= 2 &&
                    !!sendButton &&
                    (phoneOnlyCopy || phonePlaceholder || bodyText.includes('+86'))
                );
            }""",
            timeout=timeout_ms,
        )
    except Exception as exc:
        timeout_state = _collect_deepseek_form_state(page)
        timeout_classification = _classify_deepseek_sign_up_state(timeout_state)
        if timeout_classification == "phone_only":
            raise RuntimeError(
                "DeepSeek 当前出口命中手机号注册页，不支持邮箱注册: "
                + _summarize_deepseek_sign_up_state(
                    timeout_state,
                    classification=timeout_classification,
                )
            ) from exc
        raise RuntimeError(
            "DeepSeek 注册页未出现邮箱表单: "
            + _summarize_deepseek_sign_up_state(
                timeout_state,
                classification=timeout_classification,
            )
        ) from exc

    resolved_state = _collect_deepseek_form_state(page)
    resolved_classification = _classify_deepseek_sign_up_state(resolved_state)
    if resolved_classification == "email_form":
        return
    if resolved_classification == "phone_only":
        raise RuntimeError(
            "DeepSeek 当前出口命中手机号注册页，不支持邮箱注册: "
            + _summarize_deepseek_sign_up_state(
                resolved_state,
                classification=resolved_classification,
            )
        )
    raise RuntimeError(
        "DeepSeek 注册页未出现邮箱表单: "
        + _summarize_deepseek_sign_up_state(
            resolved_state,
            classification=resolved_classification,
        )
    )


def _fill_deepseek_input(locator, value: str, *, field_name: str) -> None:
    locator.wait_for(state="visible", timeout=10000)
    locator.click(timeout=5000)
    locator.fill("")
    locator.fill(value)
    actual = locator.input_value(timeout=5000)
    if actual == value:
        return

    locator.click(timeout=5000)
    locator.press("ControlOrMeta+a")
    locator.type(value, delay=20)
    actual = locator.input_value(timeout=5000)
    if actual != value:
        raise RuntimeError(
            f"DeepSeek {field_name} 填写失败: expected={value!r} actual={actual!r}"
        )


def _parse_deepseek_playwright_json_response(response, *, stage: str) -> dict[str, Any]:
    try:
        return response.json()
    except Exception as exc:
        try:
            body = str(response.text() or "")[:800]
        except Exception as body_exc:
            body = f"<read_body_failed {body_exc!r}>"
        raise RuntimeError(
            f"DeepSeek {stage} 响应不是 JSON: status={response.status} body={body!r}"
        ) from exc


def _parse_deepseek_http_json_response(response, *, stage: str) -> dict[str, Any]:
    try:
        return response.json()
    except Exception as exc:
        body = str(getattr(response, "text", "") or "")[:800]
        raise RuntimeError(
            "DeepSeek "
            f"{stage} 响应不是 JSON: status={response.status_code} body={body!r}"
        ) from exc


def _extract_deepseek_user_payload(data: dict[str, Any]) -> dict[str, Any]:
    inner = data.get("data", {})
    biz_data = inner.get("biz_data") or {}
    user = biz_data.get("user") or {}
    return {
        "id": str(user.get("id") or "").strip(),
        "token": str(user.get("token") or "").strip(),
        "email": str(user.get("email") or "").strip(),
        "need_birthday": bool(user.get("need_birthday")),
    }


def _locate_deepseek_submit_button(page):
    primary = page.locator("button.ds-atom-button.ds-basic-button--primary")
    if primary.count() > 0:
        return primary.first
    for label in ("新規登録", "Sign up"):
        candidate = page.get_by_role("button", name=label)
        if candidate.count() > 0:
            return candidate.first
    return primary.first


def _deepseek_text_indicates_resend_countdown(text: str) -> bool:
    return bool(DEEPSEEK_RESEND_COUNTDOWN_RE.search(str(text or "")))


def _collect_deepseek_send_code_page_state(page, send_code_button=None) -> dict[str, Any]:
    button_text = ""
    body_text = ""
    try:
        if send_code_button is not None:
            button_text = str(send_code_button.inner_text(timeout=3000) or "")
    except Exception:
        button_text = ""
    try:
        body_text = str(page.locator("body").inner_text(timeout=3000) or "")
    except Exception:
        body_text = ""
    has_resend_countdown = _deepseek_text_indicates_resend_countdown(
        f"{button_text}\n{body_text}"
    )
    return {
        "button_text": button_text[:200],
        "body_text": body_text[:1200],
        "has_resend_countdown": has_resend_countdown,
    }


def _render_deepseek_manual_send_code_banner(
    page,
    *,
    email: str,
    timeout_seconds: int,
) -> None:
    banner_text = (
        "DeepSeek manual handoff\n"
        f"Email: {email}\n"
        "1. Click Send code in this window.\n"
        "2. Complete the challenge manually.\n"
        "3. The task will resume after a successful response.\n"
        f"Timeout: {max(int(timeout_seconds or 0), 30)}s\n"
        "Countdown alone does not mean success."
    )
    try:
        page.evaluate(
            """(bannerText) => {
                let banner = document.getElementById('codex-deepseek-manual-handoff');
                if (!banner) {
                    banner = document.createElement('div');
                    banner.id = 'codex-deepseek-manual-handoff';
                    banner.style.position = 'fixed';
                    banner.style.right = '16px';
                    banner.style.top = '16px';
                    banner.style.zIndex = '2147483647';
                    banner.style.background = 'rgba(17, 24, 39, 0.92)';
                    banner.style.color = '#f9fafb';
                    banner.style.padding = '12px 14px';
                    banner.style.borderRadius = '12px';
                    banner.style.font = '12px/1.5 Consolas, monospace';
                    banner.style.maxWidth = '420px';
                    banner.style.whiteSpace = 'pre-wrap';
                    banner.style.boxShadow = '0 12px 32px rgba(0,0,0,0.35)';
                    document.body.appendChild(banner);
                }
                banner.textContent = bannerText;
            }""",
            banner_text,
        )
    except Exception:
        pass


def _wait_for_deepseek_manual_send_code_success(
    *,
    page,
    email: str,
    send_code_button,
    timeout_seconds: int,
    log_fn: Callable[[str], None],
    interrupt_checker=None,
):
    timeout_seconds = max(int(timeout_seconds or 0), 30)
    deadline = time.monotonic() + timeout_seconds
    attempts: list[dict[str, Any]] = []
    last_inner: dict[str, Any] = {}
    last_page_state: dict[str, Any] = {}

    _render_deepseek_manual_send_code_banner(
        page,
        email=email,
        timeout_seconds=timeout_seconds,
    )
    log_fn(
        "[DeepSeek] 已进入人工接力发码模式，请在浏览器窗口点击 Send code 并完成 challenge"
    )
    while time.monotonic() < deadline:
        if interrupt_checker is not None:
            interrupt_checker()
        try:
            if page.is_closed():
                raise RuntimeError("DeepSeek 人工接力浏览器页面已关闭")
        except AttributeError:
            pass
        try:
            response = page.wait_for_response(
                lambda resp: resp.request.method == "POST"
                and "/api/v0/users/create_email_verification_code" in resp.url,
                timeout=1000,
            )
        except Exception:
            continue

        send_data = _parse_deepseek_playwright_json_response(
            response,
            stage="浏览器人工发码",
        )
        page.wait_for_timeout(1000)
        page_state = _collect_deepseek_send_code_page_state(
            page,
            send_code_button=send_code_button,
        )
        inner = send_data.get("data", {})
        last_inner = inner
        last_page_state = page_state
        attempts.append({"response": send_data, "page_state": page_state})
        if inner.get("biz_code") in (0, "0") or _is_deepseek_email_domain_not_supported(
            inner
        ):
            return response, attempts
        log_fn(
            "[DeepSeek] 人工发码未成功，继续等待下一次点击: "
            f"biz_code={inner.get('biz_code')} biz_msg={inner.get('biz_msg')}"
        )

    raise TimeoutError(
        "DeepSeek 人工接力发码超时；未收到成功响应。"
        "注意倒计时不代表成功。"
        f" last_inner={last_inner}; last_page_state={last_page_state}"
    )


def register_deepseek_via_browser(
    *,
    email: str,
    password: str,
    mailbox,
    mail_account,
    before_ids: set,
    otp_timeout: int,
    proxy: str | None = None,
    ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
    headless: bool = True,
    user_data_dir: str | None = None,
    flaresolverr_url: str | None = None,
    captcha_solver=None,
    hcaptcha_sitekey: str = DEEPSEEK_HCAPTCHA_SITEKEY,
    task_control=None,
    tz_offset_seconds: str = DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
    pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
    manual_send_code_handoff: bool = False,
    manual_send_code_timeout_seconds: int = DEEPSEEK_MANUAL_SEND_CODE_TIMEOUT_SECONDS,
    log_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    sign_up_url = build_deepseek_page_url("/sign_up", ui_locale)
    final_state: dict[str, Any] = {
        "email": email,
        "sign_up_url": sign_up_url,
        "used_browser": True,
    }

    with sync_playwright() as p:
        browser = None
        context = None

        try:
            if manual_send_code_handoff and headless:
                raise RuntimeError("DeepSeek 人工接力发码需要 headed 浏览器")
            browser, context, page, sign_up_url = _open_deepseek_sign_up_browser_page(
                p,
                proxy=proxy,
                ui_locale=ui_locale,
                headless=headless,
                user_data_dir=user_data_dir,
                flaresolverr_url=flaresolverr_url,
                align_flaresolverr_identity=bool(flaresolverr_url),
                log_fn=log_fn,
            )
            log_fn(f"[DeepSeek] 浏览器注册页: {sign_up_url}")
            _wait_for_deepseek_sign_up_form(page)

            email_input = page.locator(
                'input.ds-input__input[type="text"], input.ds-input__input[type="email"]'
            ).first
            password_inputs = page.locator('input.ds-input__input[type="password"]')
            send_code_button = page.locator("button.ds-verify-code-input-countdown").first
            code_input = page.locator(
                'input.ds-input__input[type="tel"], input.ds-input__input[inputmode="numeric"]'
            ).first

            _fill_deepseek_input(email_input, email, field_name="email")
            _fill_deepseek_input(password_inputs.nth(0), password, field_name="password")
            _fill_deepseek_input(
                password_inputs.nth(1),
                password,
                field_name="confirm_password",
            )

            def _checkpoint() -> None:
                if task_control is not None:
                    task_control.checkpoint()

            turnstile_token = ""
            hcaptcha_token = ""
            if manual_send_code_handoff:
                log_fn("[DeepSeek] captcha_solver=manual，切换到人工接力发码模式")
            else:
                turnstile_token, hcaptcha_token = (
                    _resolve_deepseek_send_code_challenge_tokens(
                        page=page,
                        email=email,
                        password=password,
                        sign_up_url=sign_up_url,
                        captcha_solver=captcha_solver,
                        hcaptcha_sitekey=hcaptcha_sitekey,
                        proxy=proxy,
                        flaresolverr_url=flaresolverr_url,
                        log_fn=log_fn,
                        interrupt_checker=_checkpoint,
                    )
                )

            send_code_pow_response = ""
            try:
                send_code_pow_response = _request_deepseek_guest_pow_response_via_browser(
                    page,
                    target_path="/api/v0/users/create_email_verification_code",
                    proxy=proxy,
                    ui_locale=ui_locale,
                    sign_up_url=sign_up_url,
                    tz_offset_seconds=tz_offset_seconds,
                    pow_worker_url=pow_worker_url,
                )
                if send_code_pow_response:
                    log_fn("[DeepSeek] 已生成发码 PoW header")
            except Exception as exc:
                log_fn(f"[DeepSeek] 发码 PoW 生成失败，继续原链: {exc}")

            route_pattern = "**/api/v0/users/create_email_verification_code"
            route_handler = None
            if manual_send_code_handoff:
                if send_code_pow_response:
                    route_handler = _build_deepseek_guest_pow_header_route(
                        send_code_pow_response
                    )
                    page.route(route_pattern, route_handler)
                try:
                    send_response, manual_attempts = (
                        _wait_for_deepseek_manual_send_code_success(
                            page=page,
                            email=email,
                            send_code_button=send_code_button,
                            timeout_seconds=manual_send_code_timeout_seconds,
                            log_fn=log_fn,
                            interrupt_checker=_checkpoint,
                        )
                    )
                finally:
                    if route_handler is not None:
                        try:
                            page.unroute(route_pattern, route_handler)
                        except Exception:
                            pass
                final_state["send_code_mode"] = "manual_handoff"
                final_state["send_code_attempts"] = manual_attempts
            else:
                if turnstile_token or hcaptcha_token or send_code_pow_response:
                    route_handler = _build_deepseek_send_code_request_route(
                        turnstile_token=turnstile_token,
                        hcaptcha_token=hcaptcha_token,
                        guest_pow_response=send_code_pow_response,
                    )
                    page.route(route_pattern, route_handler)
                try:
                    with page.expect_response(
                        lambda resp: resp.request.method == "POST"
                        and "/api/v0/users/create_email_verification_code" in resp.url,
                        timeout=30000,
                    ) as send_response_info:
                        send_code_button.click(timeout=10000)
                finally:
                    if route_handler is not None:
                        try:
                            page.unroute(route_pattern, route_handler)
                        except Exception:
                            pass
                send_response = send_response_info.value
                final_state["send_code_mode"] = "automatic"
            sent_at = time.time()
            send_data = _parse_deepseek_playwright_json_response(
                send_response,
                stage="浏览器发码",
            )
            final_state["send_code_response"] = send_data
            inner = send_data.get("data", {})
            if inner.get("biz_code") not in (0, "0"):
                if _is_deepseek_email_domain_not_supported(inner):
                    raise DeepSeekEmailDomainRejected(email, inner)
                page.wait_for_timeout(1000)
                send_code_page_state = _collect_deepseek_send_code_page_state(
                    page,
                    send_code_button=send_code_button,
                )
                final_state["send_code_page_state"] = send_code_page_state
                raise RuntimeError(
                    "DeepSeek 浏览器发码失败: "
                    f"{inner}; page_state={send_code_page_state}"
                )
            else:
                log_fn(
                    "[DeepSeek] 浏览器已发送注册验证码"
                    f" send_window_secs={(inner.get('biz_data') or {}).get('send_window_secs')}"
                )

            code = mailbox.wait_for_code(
                mail_account,
                keyword="DeepSeek",
                timeout=otp_timeout,
                before_ids=before_ids,
                otp_sent_at=sent_at,
            )
            final_state["code"] = code
            log_fn(f"[DeepSeek] 浏览器注册验证码: {code}")

            _fill_deepseek_input(code_input, code, field_name="code")
            submit_button = _locate_deepseek_submit_button(page)
            register_pow_response = ""
            try:
                register_pow_response = _request_deepseek_guest_pow_response_via_browser(
                    page,
                    target_path="/api/v0/users/register",
                    proxy=proxy,
                    ui_locale=ui_locale,
                    sign_up_url=sign_up_url,
                    tz_offset_seconds=tz_offset_seconds,
                    pow_worker_url=pow_worker_url,
                )
                if register_pow_response:
                    log_fn("[DeepSeek] 已生成注册 PoW header")
            except Exception as exc:
                log_fn(f"[DeepSeek] 注册 PoW 生成失败，继续原链: {exc}")
            register_route_pattern = "**/api/v0/users/register"
            register_route_handler = None
            if register_pow_response:
                register_route_handler = _build_deepseek_guest_pow_header_route(
                    register_pow_response
                )
                page.route(register_route_pattern, register_route_handler)
            try:
                with page.expect_response(
                    lambda resp: resp.request.method == "POST"
                    and "/api/v0/users/register" in resp.url,
                    timeout=30000,
                ) as register_response_info:
                    submit_button.click(timeout=10000)
            finally:
                if register_route_handler is not None:
                    try:
                        page.unroute(register_route_pattern, register_route_handler)
                    except Exception:
                        pass
            register_response = register_response_info.value
            register_data = _parse_deepseek_playwright_json_response(
                register_response,
                stage="浏览器注册",
            )
            final_state["register_response"] = register_data
            register_inner = register_data.get("data", {})
            if register_inner.get("biz_code") not in (0, "0"):
                raise RuntimeError(
                    f"DeepSeek 浏览器注册失败: {register_inner}"
                )
            final_state["register_user"] = _extract_deepseek_user_payload(
                register_data
            )
            final_state["final_url"] = str(page.url or "").strip() or sign_up_url
            final_state["body_snippet"] = str(
                page.locator("body").inner_text(timeout=3000) or ""
            )[:2000]
            return final_state
        except Exception:
            final_state["error_state"] = _collect_deepseek_form_state(page)
            raise
        finally:
            if context is not None:
                context.close()
            if browser is not None:
                browser.close()


def solve_deepseek_pow(
    challenge: dict[str, Any],
    *,
    proxy: str | None = None,
    ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
    sign_up_url: str | None = None,
    pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
) -> dict[str, Any]:
    if not challenge:
        raise RuntimeError("DeepSeek guest challenge 为空")
    required = ("algorithm", "challenge", "salt", "difficulty", "signature", "expire_at")
    missing = [key for key in required if not challenge.get(key)]
    if missing:
        raise RuntimeError(f"DeepSeek guest challenge 缺少字段: {missing}")

    worker_url = str(pow_worker_url or DEEPSEEK_DEFAULT_POW_WORKER_URL).strip()
    if not worker_url:
        raise RuntimeError("DeepSeek PoW Worker URL 未配置")

    target_url = sign_up_url or build_deepseek_page_url("/sign_up", ui_locale)
    accept_language = build_deepseek_accept_language(ui_locale)
    page_proxy_candidates = [None]
    if proxy:
        page_proxy_candidates.append(proxy)

    last_error: Exception | None = None
    for page_proxy in page_proxy_candidates:
        proxy_cfg = build_playwright_proxy_config(page_proxy) if page_proxy else None
        try:
            with _DEEPSEEK_POW_SOLVER_LOCK:
                with sync_playwright() as p:
                    launch_kwargs: dict[str, Any] = with_chrome_executable(
                        headless=True
                    )
                    if proxy_cfg:
                        launch_kwargs["proxy"] = proxy_cfg
                    browser = p.chromium.launch(**launch_kwargs)
                    context = browser.new_context(
                        locale=ui_locale,
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 720},
                    )
                    context.set_extra_http_headers(
                        {"Accept-Language": accept_language}
                    )
                    page = context.new_page()
                    try:
                        page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=120000,
                        )
                        answer = page.evaluate(
                            _POW_SOLVE_EVAL,
                            {
                                "challenge": challenge,
                                "workerUrl": worker_url,
                            },
                        )
                    finally:
                        context.close()
                        browser.close()
            break
        except Exception as exc:
            last_error = exc
    else:
        raise RuntimeError(f"DeepSeek PoW 求解失败: {last_error}") from last_error

    salt = str(answer.get("salt") or challenge.get("salt") or "").strip()
    raw_answer = answer.get("answer")
    if not salt:
        raise RuntimeError(f"DeepSeek PoW 返回缺少 salt: {answer}")
    if raw_answer is None:
        raise RuntimeError(f"DeepSeek PoW 返回缺少 answer: {answer}")
    return {"salt": salt, "answer": int(raw_answer)}


def encode_guest_pow_response(
    challenge: dict[str, Any],
    *,
    proxy: str | None = None,
    ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
    sign_up_url: str | None = None,
    pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
) -> str:
    payload = solve_deepseek_pow(
        challenge,
        proxy=proxy,
        ui_locale=ui_locale,
        sign_up_url=sign_up_url,
        pow_worker_url=pow_worker_url,
    )
    body = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


@dataclass
class DeepSeekRegisterResult:
    email: str
    password: str
    user_id: str
    username: str
    token: str
    need_birthday: bool
    device_id: str
    region: str = DEEPSEEK_DEFAULT_REGION


class DeepSeekClient:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        timeout: int = 30,
        ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
        region: str = DEEPSEEK_DEFAULT_REGION,
        tz_offset_seconds: str = DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
        pow_worker_url: str = DEEPSEEK_DEFAULT_POW_WORKER_URL,
    ):
        self.proxy = proxy
        self.log = log_fn
        self.timeout = timeout
        self.ui_locale = normalize_deepseek_ui_locale(ui_locale)
        self.locale = extract_deepseek_client_locale(self.ui_locale)
        self.region = str(region or DEEPSEEK_DEFAULT_REGION).strip() or DEEPSEEK_DEFAULT_REGION
        self.tz_offset_seconds = (
            str(tz_offset_seconds or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS).strip()
            or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS
        )
        self.pow_worker_url = (
            str(pow_worker_url or DEEPSEEK_DEFAULT_POW_WORKER_URL).strip()
            or DEEPSEEK_DEFAULT_POW_WORKER_URL
        )
        self.sign_up_url = build_deepseek_page_url("/sign_up", self.ui_locale)
        self.sign_in_url = build_deepseek_page_url("/sign_in", self.ui_locale)
        self.forgot_password_url = build_deepseek_page_url(
            "/forgot_password", self.ui_locale
        )
        self._http = HTTPClient(
            proxy_url=proxy,
            config=RequestConfig(
                timeout=timeout,
                max_retries=1,
                retry_delay=0.2,
                impersonate="chrome136",
            ),
        )
        self._device_id: Optional[str] = None
        self._cookies: dict[str, str] = {}

    @property
    def device_id(self) -> str:
        if not self._device_id:
            self._device_id = random_device_id("deepseek")
        return self._device_id

    def _base_headers(self, *, referer: str) -> dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": build_deepseek_accept_language(self.ui_locale),
            "content-type": "application/json",
            "origin": DEEPSEEK_BASE_URL,
            "referer": referer,
            "user-agent": USER_AGENT,
            "x-app-version": DEEPSEEK_APP_VERSION,
            "x-client-locale": self.locale,
            "x-client-platform": "web",
            "x-client-timezone-offset": self.tz_offset_seconds,
            "x-client-version": DEEPSEEK_CLIENT_VERSION,
        }

    def _capture_cookies(self, response) -> None:
        try:
            jar = response.cookies.jar
        except Exception:
            jar = None
        if jar is not None:
            for cookie in jar:
                if cookie and getattr(cookie, "name", None):
                    self._cookies[str(cookie.name)] = str(cookie.value)
            return
        try:
            cookie_dict = response.cookies.get_dict()
        except Exception:
            cookie_dict = {}
        for key, value in cookie_dict.items():
            self._cookies[str(key)] = str(value)

    def _cookie_header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self._cookies.items())

    def _attach_cookie_header(self, headers: dict[str, str]) -> dict[str, str]:
        merged = dict(headers)
        if self._cookies:
            merged["cookie"] = self._cookie_header()
        return merged

    def _ensure_settings(self, *, referer: str) -> None:
        for scope in ("main", "model"):
            response = self._http.get(
                f"{DEEPSEEK_BASE_URL}/api/v0/client/settings?did={self.device_id}&scope={scope}",
                headers={
                    "accept": "application/json, text/plain, */*",
                    "accept-language": build_deepseek_accept_language(self.ui_locale),
                    "referer": referer,
                    "user-agent": USER_AGENT,
                },
            )
            self._capture_cookies(response)
        response = self._http.get(
            f"{DEEPSEEK_BASE_URL}/api/v0/client/settings?did=&scope=banner",
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": build_deepseek_accept_language(self.ui_locale),
                "referer": referer,
                "user-agent": USER_AGENT,
            },
        )
        self._capture_cookies(response)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        referer: str,
        include_guest_pow: bool = False,
        guest_target_path: Optional[str] = None,
    ) -> dict[str, Any]:
        headers = self._attach_cookie_header(self._base_headers(referer=referer))
        if include_guest_pow:
            target_path = guest_target_path or path
            challenge = self.get_guest_challenge(
                target_path=target_path,
                referer=referer,
            )
            headers["x-ds-guest-pow-response"] = encode_guest_pow_response(
                challenge,
                proxy=self.proxy,
                ui_locale=self.ui_locale,
                sign_up_url=self.sign_up_url,
                pow_worker_url=self.pow_worker_url,
            )
        response = self._http.post(
            f"{DEEPSEEK_USERS_API}{path}",
            json=payload,
            headers=headers,
        )
        self._capture_cookies(response)
        return _parse_deepseek_http_json_response(
            response,
            stage=f"API {path}",
        )

    def get_guest_challenge(self, *, target_path: str, referer: str) -> dict[str, Any]:
        response = self._http.post(
            f"{DEEPSEEK_USERS_API}/create_guest_challenge",
            json={"target_path": target_path},
            headers=self._attach_cookie_header(self._base_headers(referer=referer)),
        )
        self._capture_cookies(response)
        data = _parse_deepseek_http_json_response(
            response,
            stage="guest challenge",
        )
        return _extract_deepseek_guest_challenge(data)

    def send_email_code(
        self,
        *,
        email: str,
        scenario: str,
        hcaptcha_token: str = "",
        turnstile_token: str = "",
        referer: str | None = None,
    ) -> dict[str, Any]:
        target_referer = referer or self.sign_up_url
        self._ensure_settings(referer=target_referer)
        payload = {
            "email": email,
            "turnstile_token": str(turnstile_token or ""),
            "locale": self.locale,
            "device_id": self.device_id,
            "scenario": scenario,
        }
        if hcaptcha_token:
            payload["hcaptcha_token"] = str(hcaptcha_token)
        return self._post(
            "/create_email_verification_code",
            payload,
            referer=target_referer,
            include_guest_pow=True,
            guest_target_path="/api/v0/users/create_email_verification_code",
        )

    def check_email_code(
        self,
        *,
        email: str,
        code: str,
        referer: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/check_email_code",
            {"email": email, "email_verification_code": code},
            referer=referer or self.sign_up_url,
        )

    def reset_password(
        self,
        *,
        email: str,
        code: str,
        password: str,
        referer: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            "/email_reset_password",
            {
                "email": email,
                "email_verification_code": code,
                "password": password,
            },
            referer=referer or self.forgot_password_url,
        )

    def login(
        self,
        *,
        email: str,
        password: str,
        referer: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "email": email,
            "password": password,
            "device_id": self.device_id,
            "os": "web",
        }
        return self._post("/login", payload, referer=referer or self.sign_in_url)

    def register(
        self,
        *,
        email: str,
        password: str,
        code: str,
        referer: str | None = None,
    ) -> DeepSeekRegisterResult:
        target_referer = referer or self.sign_up_url
        self._ensure_settings(referer=target_referer)
        payload = {
            "locale": self.locale,
            "region": self.region,
            "payload": {
                "email": email,
                "email_verification_code": code,
                "password": password,
            },
            "device_id": self.device_id,
            "os": "web",
        }
        data = self._post(
            "/register",
            payload,
            referer=target_referer,
            include_guest_pow=True,
            guest_target_path="/api/v0/users/register",
        )
        inner = data.get("data", {})
        biz_code = inner.get("biz_code")
        if biz_code not in (0, "0"):
            raise RuntimeError(f"DeepSeek register 失败: {inner}")
        biz_data = inner.get("biz_data") or {}
        user = biz_data.get("user") or {}
        user_id = str(user.get("id") or "").strip()
        token = str(user.get("token") or "").strip()
        if not user_id or not token:
            raise RuntimeError(f"DeepSeek register 返回缺少用户信息: {data}")
        masked_email = str(user.get("email") or "").strip()
        username = mask_email(masked_email) if masked_email else mask_email(email)
        return DeepSeekRegisterResult(
            email=email,
            password=password,
            user_id=user_id,
            username=username,
            token=token,
            need_birthday=bool(user.get("need_birthday")),
            device_id=self.device_id,
            region=self.region,
        )

    def close(self) -> None:
        self._http.close()
