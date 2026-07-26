"""xAI Device OAuth minting and CLIProxyAPI auth-file upload for Grok accounts."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from core.config_store import config_store
from core.proxy_utils import build_requests_proxy_config
from core.task_runtime import TaskInterruption

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
DEVICE_VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
DEVICE_APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
_DEFAULT_OAUTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}
REGISTRATION_RUNTIME_EXTRA_KEY = "grok_registration_runtime"
GROK_SESSION_COOKIES_EXTRA_KEY = "grok_session_cookies"
_REGISTRATION_RUNTIME_EXTRA_KEYS = (
    "grok_flaresolverr_url",
    "flaresolverr_url",
    "grok_flaresolverr_attempts",
    "grok_flaresolverr_bridge_loopback_proxy",
    "grok_flaresolverr_loopback_proxy_host",
    "grok_browser_accept_language",
)
_SESSION_COOKIE_NAMES = frozenset({"sso", "sso-rw", "sso_jwt", "cf_clearance", "__cf_bm"})
_SSO_COOKIE_NAMES = frozenset({"sso", "sso-rw", "sso_jwt"})
_PLAYWRIGHT_SAME_SITE_VALUES = frozenset({"Strict", "Lax", "None"})


class XaiDeviceOAuthError(RuntimeError):
    """The xAI device authorization did not complete successfully."""


@dataclass(frozen=True)
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DeviceToken:
    access_token: str
    refresh_token: str
    id_token: str
    expires_in: int


def _as_bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: Any, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return resolved if resolved > 0 else default


def build_registration_runtime(extra: dict[str, Any], *, headless: bool) -> dict[str, Any]:
    """Persist only browser settings that must survive until Device OAuth starts."""
    runtime: dict[str, Any] = {"browser_headless": bool(headless)}
    for key in _REGISTRATION_RUNTIME_EXTRA_KEYS:
        value = (extra or {}).get(key)
        if value not in (None, ""):
            runtime[key] = value
    return runtime


def _account_extra(account) -> dict[str, Any]:
    """Read platform metadata from either the runtime Account or saved AccountModel."""
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        return extra

    get_extra = getattr(account, "get_extra", None)
    if callable(get_extra):
        try:
            extra = get_extra()
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = None
        if isinstance(extra, dict):
            return extra

    raw_extra = getattr(account, "extra_json", "")
    if isinstance(raw_extra, str):
        try:
            extra = json.loads(raw_extra or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = None
        if isinstance(extra, dict):
            return extra
    return {}


def _resolve_grok_runtime_extra(account) -> tuple[dict[str, Any], str]:
    current = {
        key: value
        for key in _REGISTRATION_RUNTIME_EXTRA_KEYS
        if (value := config_store.get(key, "")) not in (None, "")
    }
    extra = _account_extra(account)
    saved = extra.get(REGISTRATION_RUNTIME_EXTRA_KEY)
    if not isinstance(saved, dict):
        return current, "当前配置"

    runtime = dict(current)
    runtime.update(
        {
            key: value
            for key, value in saved.items()
            if key in {*_REGISTRATION_RUNTIME_EXTRA_KEYS, "browser_headless"}
        }
    )
    return runtime, "账号注册快照"


def _sanitize_file_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9@._-]+", "-", str(value or "").strip()).strip("-")


def credential_file_name(email: str, sub: str = "") -> str:
    segment = _sanitize_file_segment(email) or _sanitize_file_segment(sub)
    if not segment:
        segment = str(int(time.time() * 1000))
    return f"xai-{segment}.json"


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        padding = "=" * (-len(parts[1]) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def build_xai_auth_payload(
    *,
    email: str,
    access_token: str,
    refresh_token: str,
    id_token: str = "",
    expires_in: int = 21600,
) -> dict[str, Any]:
    access_token = str(access_token or "").strip()
    refresh_token = str(refresh_token or "").strip()
    if not access_token or not refresh_token:
        raise ValueError("xAI OAuth token response is incomplete")

    claims = _jwt_claims(access_token)
    expired = ""
    exp = claims.get("exp")
    iat = claims.get("iat")
    if isinstance(exp, (int, float)):
        expired = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(iat, (int, float)) and exp > iat:
            expires_in = int(exp - iat)

    payload = {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": int(expires_in or 21600),
        "expired": expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": str(email or "").strip(),
        "sub": str(claims.get("sub") or claims.get("principal_id") or "").strip(),
        "base_url": DEFAULT_BASE_URL,
        "token_endpoint": TOKEN_URL,
        "redirect_uri": "http://127.0.0.1:56121/callback",
        "disabled": False,
        "headers": dict(DEFAULT_HEADERS),
    }
    if str(id_token or "").strip():
        payload["id_token"] = str(id_token).strip()
    return payload


def _request_device_code(proxy: str | None) -> DeviceCodeSession:
    response = requests.post(
        DEVICE_CODE_URL,
        data={"client_id": CLIENT_ID, "scope": SCOPE},
        headers={"Accept": "application/json"},
        proxies=build_requests_proxy_config(proxy),
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise XaiDeviceOAuthError(f"xAI device-code 接口返回非 JSON（HTTP {response.status_code}）") from exc
    if response.status_code != 200 or not isinstance(body, dict):
        raise XaiDeviceOAuthError(f"xAI device-code 接口失败（HTTP {response.status_code}）")

    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise XaiDeviceOAuthError("xAI device-code 响应缺少授权字段")
    verification_uri = str(body.get("verification_uri") or "https://accounts.x.ai/oauth2/device").strip()
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri_complete=str(
            body.get("verification_uri_complete") or f"{verification_uri}?user_code={user_code}"
        ).strip(),
        expires_in=_positive_int(body.get("expires_in"), 1800),
        interval=_positive_int(body.get("interval"), 5),
    )


def _poll_device_token(
    session: DeviceCodeSession,
    *,
    proxy: str | None,
    cancel: threading.Event,
    result: dict[str, DeviceToken | Exception],
) -> None:
    deadline = time.monotonic() + max(session.expires_in - 5, 30)
    interval = max(session.interval, 1)
    try:
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise XaiDeviceOAuthError("xAI Device OAuth 已取消")
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": session.device_code,
                    "client_id": CLIENT_ID,
                },
                headers={"Accept": "application/json"},
                proxies=build_requests_proxy_config(proxy),
                timeout=30,
            )
            try:
                body = response.json()
            except ValueError:
                body = {}

            if response.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
                refresh_token = str(body.get("refresh_token") or "").strip()
                if not refresh_token:
                    raise XaiDeviceOAuthError("xAI OAuth 响应缺少 refresh_token")
                result["token"] = DeviceToken(
                    access_token=str(body["access_token"]).strip(),
                    refresh_token=refresh_token,
                    id_token=str(body.get("id_token") or "").strip(),
                    expires_in=_positive_int(body.get("expires_in"), 21600),
                )
                return

            error = str(body.get("error") or "") if isinstance(body, dict) else ""
            if error == "slow_down":
                interval = min(interval + 5, 30)
            elif error in {"expired_token", "access_denied"}:
                raise XaiDeviceOAuthError(f"xAI Device OAuth 被拒绝或过期：{error}")
            elif error and error != "authorization_pending":
                raise XaiDeviceOAuthError(f"xAI token 接口失败：{error}")
            if cancel.wait(interval):
                raise XaiDeviceOAuthError("xAI Device OAuth 已取消")
        raise XaiDeviceOAuthError("等待 xAI Device OAuth 授权超时")
    except Exception as exc:
        result["error"] = exc


def _visible_locator(page, selectors: list[str]):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def _click_labels(_helper, page, labels: list[str], *, allow_submit_fallback: bool = True) -> bool:
    """Click only an exact visible label so consent cannot hit a cookie CTA."""
    for label in labels:
        for locator_factory in (
            lambda: page.get_by_role("button", name=label, exact=True),
            lambda: page.get_by_text(label, exact=True),
        ):
            try:
                locator = locator_factory()
                if locator.count() > 0 and locator.first.is_visible():
                    locator.first.click(timeout=3000)
                    return True
            except Exception:
                continue
    if not allow_submit_fallback:
        return False
    locator = _visible_locator(page, ["button[type='submit']", "input[type='submit']"])
    if locator is None:
        return False
    try:
        locator.click(timeout=3000)
        return True
    except Exception:
        return False


def _approve_device_consent(page) -> bool:
    """Submit xAI device consent with the server-required allow action."""
    try:
        return bool(
            page.evaluate(
                """
                () => {
                    const form = document.querySelector('form');
                    if (!form) return false;

                    let action = form.querySelector('input[name="action"]');
                    if (!action) {
                        action = document.createElement('input');
                        action.type = 'hidden';
                        action.name = 'action';
                        form.appendChild(action);
                    }
                    action.value = 'allow';

                    const allowButton = [...form.querySelectorAll('button')].find((button) => {
                        const label = (button.innerText || button.textContent || '').trim();
                        return ['Allow', '允许', 'Authorize', 'Approve', '同意', '批准'].includes(label);
                    });
                    if (allowButton) {
                        allowButton.click();
                        return true;
                    }
                    if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                    } else {
                        form.submit();
                    }
                    return true;
                }
                """
            )
        )
    except Exception:
        return False


def _page_has_turnstile(helper, page) -> bool:
    try:
        return bool(helper._has_turnstile_runtime(page) or helper._find_turnstile_widget(page)[1])
    except Exception:
        return False


def _page_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1000) or "")
    except Exception:
        return ""


def _page_contains(page_text: str, *phrases: str) -> bool:
    normalized = " ".join(page_text.casefold().split())
    return any(phrase.casefold() in normalized for phrase in phrases)


def select_grok_session_cookies(raw_cookies: Any) -> list[dict[str, Any]]:
    """Keep only xAI cookies needed to continue Device OAuth after registration."""
    if not isinstance(raw_cookies, list):
        return []

    selected: list[dict[str, Any]] = []
    for raw in raw_cookies:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        value = str(raw.get("value") or "").strip()
        domain = str(raw.get("domain") or "").strip().lower()
        is_session_cookie = name.casefold() in _SESSION_COOKIE_NAMES or name.casefold().startswith("sso")
        if not name or not value or not is_session_cookie:
            continue
        if domain.lstrip(".") != "x.ai" and not domain.endswith(".x.ai"):
            continue

        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(raw.get("path") or "/"),
            "secure": bool(raw.get("secure", True)),
            "httpOnly": bool(raw.get("httpOnly", False)),
        }
        expires = raw.get("expires")
        if isinstance(expires, (int, float)) and expires >= 0:
            cookie["expires"] = expires
        same_site = str(raw.get("sameSite") or "").strip()
        if same_site in _PLAYWRIGHT_SAME_SITE_VALUES:
            cookie["sameSite"] = same_site
        selected.append(cookie)
    return selected


def _advance_browser_authorization(
    helper,
    page,
    device: DeviceCodeSession,
    email: str,
    password: str,
    *,
    submitted_states: set[str],
) -> bool:
    """Advance one known xAI Device OAuth screen without re-submitting it."""
    page_url = _safe_page_url(str(getattr(page, "url", "") or ""))
    page_path = urlsplit(page_url).path
    page_text = _page_text(page)

    def _submit_once(state: str, action: Callable[[], bool]) -> bool:
        key = f"{page_url}:{state}"
        if key in submitted_states:
            return False
        if not action():
            return False
        submitted_states.add(key)
        return True

    def _was_submitted(state: str) -> bool:
        return f"{page_url}:{state}" in submitted_states

    if "/oauth2/device/done" in page_url:
        return False

    if _page_contains(page_text, "invalid action"):
        raise XaiDeviceOAuthError("xAI Device OAuth 授权确认失败：Invalid action")

    # Only a real /account route is an xAI redirect screen.  The generic
    # "redirecting" copy can also briefly occur on the Device page itself.
    is_account_redirect = page_path == "/account" or page_path.startswith("/account/")
    if is_account_redirect:
        return _submit_once(
            "account-redirect",
            lambda: _click_labels(helper, page, ["Continue", "继续"]),
        )

    if "/oauth2/device/consent" in page_url or _page_contains(page_text, "授权 grok build"):
        return _submit_once(
            "consent",
            lambda: _approve_device_consent(page),
        )

    if _page_contains(page_text, "全部允许", "accept all"):
        return _submit_once(
            "cookie-banner",
            lambda: _click_labels(helper, page, ["全部允许", "Accept all"], allow_submit_fallback=False),
        )

    if _page_contains(page_text, "使用邮箱登录", "continue with email"):
        return _submit_once(
            "email-login-choice",
            lambda: _click_labels(
                helper,
                page,
                ["使用邮箱登录", "Continue with email"],
                allow_submit_fallback=False,
            ),
        )

    user_code = _visible_locator(page, ["input[name='user_code']", "input[name*='user_code' i]"])
    if user_code is not None:
        # A login may return to the same sanitized device URL.  Treat that as a
        # separate stage so the required second Continue is not suppressed.
        returned_from_account = any(key.endswith(":account-redirect") for key in submitted_states)
        device_state = "device-code-after-account" if returned_from_account else "device-code-initial"
        if _was_submitted(device_state):
            return False
        user_code.fill(device.user_code)
        return _submit_once(
            device_state,
            lambda: _click_labels(helper, page, ["Continue", "继续", "Next", "下一步"]),
        )

    # The xAI sign-in page usually renders both fields.  Handling the email
    # field first repeatedly submits an empty password and causes a redirect loop.
    password_input = _visible_locator(
        page,
        ["input[type='password']", "input[autocomplete='current-password']", "input[name='password']"],
    )
    if password_input is not None:
        if _was_submitted("password-login"):
            return False
        email_input = _visible_locator(
            page,
            ["input[type='email']", "input[autocomplete='email']", "input[name='email']", "input[name='username']"],
        )
        if email_input is not None:
            email_input.fill(email)
        password_input.fill(password)
        if _page_has_turnstile(helper, page):
            helper._solve_turnstile_on_page(page)
        return _submit_once(
            "password-login",
            lambda: _click_labels(helper, page, ["Sign in", "登录", "Log in", "Continue", "继续"]),
        )

    email_input = _visible_locator(
        page,
        ["input[type='email']", "input[autocomplete='email']", "input[name='email']", "input[name='username']"],
    )
    if email_input is not None:
        if _was_submitted("email-login"):
            return False
        email_input.fill(email)
        return _submit_once(
            "email-login",
            lambda: _click_labels(helper, page, ["Continue", "继续", "Next", "下一步"]),
        )

    return False


def _inject_grok_session(context, account) -> None:
    extra = _account_extra(account)
    sso = str(extra.get("sso") or extra.get("sso_token") or getattr(account, "token", "") or "").strip()
    sso_rw = str(extra.get("sso_rw") or "").strip()
    stored = select_grok_session_cookies(extra.get(GROK_SESSION_COOKIES_EXTRA_KEY))
    cookies: list[dict[str, Any]] = list(stored)

    # The registration browser can set host-only cookies.  Device OAuth moves
    # between accounts.x.ai and auth.x.ai, so clone only SSO cookies across
    # those hosts while preserving Cloudflare cookies on their original host.
    seen = {(cookie["name"], cookie["domain"], cookie["path"]) for cookie in cookies}
    for cookie in stored:
        if cookie["name"].casefold() not in _SSO_COOKIE_NAMES and not cookie["name"].casefold().startswith("sso"):
            continue
        for domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
            key = (cookie["name"], domain, cookie["path"])
            if key in seen:
                continue
            cloned = dict(cookie)
            cloned["domain"] = domain
            cookies.append(cloned)
            seen.add(key)

    # Old account records predate grok_session_cookies.  Keep them usable by
    # injecting their persisted SSO values at the parent xAI domain.
    for name, value in (("sso", sso), ("sso-rw", sso_rw)):
        if value and (name, ".x.ai", "/") not in seen:
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".x.ai",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            )
            seen.add((name, ".x.ai", "/"))
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception:
            # Reusing the registration SSO is an optimization, not a prerequisite.
            pass


def _safe_page_url(url: str) -> str:
    """Keep OAuth device codes out of task logs while retaining navigation context."""
    parsed = urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _resolve_cpa_proxy(account) -> tuple[str | None, str]:
    configured_proxy = str(config_store.get("grok_cpa_proxy", "") or "").strip()
    if configured_proxy:
        return configured_proxy, "CPA 配置"
    extra = _account_extra(account)
    registration_proxy = str(extra.get("registration_proxy") or "").strip()
    if registration_proxy:
        return registration_proxy, "账号注册"
    return None, "直连"


def _resolve_cpa_headless(account, runtime_extra: dict[str, Any]) -> tuple[bool, str]:
    configured = config_store.get("grok_cpa_headless", "")
    if configured not in (None, ""):
        return _as_bool(configured), "CPA 配置"
    if "browser_headless" in runtime_extra:
        return _as_bool(runtime_extra["browser_headless"]), "账号注册"
    return False, "默认 headed"


def _account_sso(account) -> str:
    extra = _account_extra(account)
    return str(
        extra.get("sso")
        or extra.get("sso_token")
        or getattr(account, "token", "")
        or ""
    ).strip()


def _confirm_device_oauth_http(
    sso: str,
    device: DeviceCodeSession,
    *,
    proxy: str | None,
    log: Callable[[str], None] = print,
) -> None:
    """Approve Device OAuth with SSO cookie only (no browser).

    Matches Charles-0509/Grok-Register internal/oauth ConfirmHTTP.
    """
    if not sso:
        raise XaiDeviceOAuthError("缺少 sso，无法纯 HTTP 授权")

    session = requests.Session()
    session.trust_env = False
    proxies = build_requests_proxy_config(proxy)
    cookie = f"sso={sso}"
    common = {
        "User-Agent": _DEFAULT_OAUTH_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://accounts.x.ai",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
    }
    try:
        verify_resp = session.post(
            DEVICE_VERIFY_URL,
            data={"user_code": device.user_code},
            headers={
                **common,
                "Referer": device.verification_uri_complete
                or "https://accounts.x.ai/oauth2/device",
            },
            proxies=proxies,
            timeout=30,
            allow_redirects=False,
        )
        loc = str(verify_resp.headers.get("Location") or "")
        if "error=rate_limited" in loc or "error=rate_limited" in (verify_resp.text or ""):
            raise XaiDeviceOAuthError("xAI Device OAuth rate_limited")
        if verify_resp.status_code == 403:
            raise XaiDeviceOAuthError("xAI Device OAuth HTTP verify challenge/403")
        if "/oauth2/device/done" in loc:
            log("xAI Device OAuth：HTTP verify 已直接完成")
            return

        consent_ref = loc
        if not consent_ref:
            consent_ref = (
                "https://accounts.x.ai/oauth2/device/consent?user_code="
                + device.user_code
            )
        elif consent_ref.startswith("/"):
            consent_ref = "https://accounts.x.ai" + consent_ref

        approve_resp = session.post(
            DEVICE_APPROVE_URL,
            data={
                "user_code": device.user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={**common, "Referer": consent_ref},
            proxies=proxies,
            timeout=30,
            allow_redirects=False,
        )
        aloc = str(approve_resp.headers.get("Location") or "")
        body = str(approve_resp.text or "")
        lower = body.lower()
        if "error=rate_limited" in aloc:
            raise XaiDeviceOAuthError("xAI Device OAuth rate_limited")
        if (
            "device authorized" in lower
            or "设备已授权" in body
            or "/oauth2/device/done" in aloc
            or 200 <= approve_resp.status_code < 300
        ):
            log("xAI Device OAuth：HTTP approve 成功")
            return
        if approve_resp.status_code == 403:
            raise XaiDeviceOAuthError("xAI Device OAuth HTTP approve challenge/403")
        raise XaiDeviceOAuthError(
            f"xAI Device OAuth HTTP 未知响应 status={approve_resp.status_code}"
        )
    finally:
        session.close()


def _mint_xai_device_token_http(
    account,
    *,
    proxy: str | None,
    timeout_seconds: int,
    log: Callable[[str], None] = print,
) -> DeviceToken:
    sso = _account_sso(account)
    if not sso:
        raise XaiDeviceOAuthError("账号缺少 sso，跳过 HTTP OAuth")
    device = _request_device_code(proxy)
    cancel = threading.Event()
    poll_result: dict[str, DeviceToken | Exception] = {}
    poller = threading.Thread(
        target=_poll_device_token,
        kwargs={
            "session": device,
            "proxy": proxy,
            "cancel": cancel,
            "result": poll_result,
        },
        daemon=True,
    )
    poller.start()
    try:
        _confirm_device_oauth_http(sso, device, proxy=proxy, log=log)
        deadline = time.monotonic() + min(max(timeout_seconds, 30), device.expires_in)
        while time.monotonic() < deadline:
            token = poll_result.get("token")
            if isinstance(token, DeviceToken):
                return token
            error = poll_result.get("error")
            if isinstance(error, Exception):
                raise error
            time.sleep(0.5)
        raise XaiDeviceOAuthError("等待 xAI HTTP Device OAuth token 超时")
    finally:
        cancel.set()
        poller.join(timeout=1)


def _mint_xai_device_token(
    account,
    *,
    proxy: str | None,
    headless: bool,
    runtime_extra: dict[str, Any],
    timeout_seconds: int,
    captcha_solver=None,
    yescaptcha_key: str = "",
    task_control=None,
    log: Callable[[str], None] = print,
) -> DeviceToken:
    # Prefer pure-HTTP SSO approve (reference Grok-Register path) before browser.
    prefer_http = _as_bool(
        runtime_extra.get("grok_oauth_http")
        if isinstance(runtime_extra, dict)
        else None,
        True,
    )
    if prefer_http and _account_sso(account):
        try:
            log("xAI Device OAuth：尝试纯 HTTP SSO 授权")
            return _mint_xai_device_token_http(
                account,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
                log=log,
            )
        except Exception as exc:
            log(f"xAI Device OAuth HTTP 路径失败，回退浏览器：{exc}")

    from platforms.grok.core import GrokRegister

    email = str(getattr(account, "email", "") or "").strip()
    password = str(getattr(account, "password", "") or "").strip()
    if not email or not password:
        raise XaiDeviceOAuthError("xAI OAuth 需要账号邮箱和密码")

    device = _request_device_code(proxy)
    helper = GrokRegister(
        captcha_solver=captcha_solver,
        yescaptcha_key=yescaptcha_key,
        proxy=proxy,
        log_fn=log,
        headless=headless,
        task_control=task_control,
        extra=runtime_extra,
    )
    cancel = threading.Event()
    poll_result: dict[str, DeviceToken | Exception] = {}
    playwright = browser = context = poller = None
    try:
        playwright, browser = helper._launch_browser()
        context = browser.new_context()
        helper._install_turnstile_patch(context)
        _inject_grok_session(context, account)
        page = context.new_page()
        last_navigation_url = ""
        submitted_states: set[str] = set()

        def _log_navigation(frame) -> None:
            nonlocal last_navigation_url
            if frame != page.main_frame:
                return
            safe_url = _safe_page_url(getattr(frame, "url", ""))
            if safe_url and safe_url != last_navigation_url:
                last_navigation_url = safe_url
                log(f"xAI Device OAuth：页面 {safe_url}")

        page.on("framenavigated", _log_navigation)
        log("xAI Device OAuth：已打开本机授权浏览器")
        page.goto(device.verification_uri_complete, wait_until="domcontentloaded")
        helper._page_wait(page, 500)

        poller = threading.Thread(
            target=_poll_device_token,
            kwargs={"session": device, "proxy": proxy, "cancel": cancel, "result": poll_result},
            daemon=True,
        )
        poller.start()
        deadline = time.monotonic() + min(max(timeout_seconds, 30), device.expires_in)
        manual_notice_logged = False
        while time.monotonic() < deadline:
            helper._checkpoint()
            token = poll_result.get("token")
            if isinstance(token, DeviceToken):
                return token
            error = poll_result.get("error")
            if isinstance(error, Exception):
                raise error
            try:
                acted = _advance_browser_authorization(
                    helper,
                    page,
                    device,
                    email,
                    password,
                    submitted_states=submitted_states,
                )
            except XaiDeviceOAuthError:
                raise
            except Exception as exc:
                raise XaiDeviceOAuthError(f"xAI 授权页操作失败：{type(exc).__name__}") from exc
            if not acted and not manual_notice_logged:
                log("xAI Device OAuth：如页面要求 MFA 或人工确认，请在打开的浏览器中完成")
                manual_notice_logged = True
            helper._page_wait(page, 500)
        raise XaiDeviceOAuthError("等待 xAI 浏览器授权超时")
    finally:
        cancel.set()
        if poller is not None:
            poller.join(timeout=1)
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
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass


def _normalize_management_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        raise ValueError("未配置 CPA 管理地址")
    if url.endswith("/v0/management/auth-files"):
        return url
    if url.endswith("/v0/management"):
        return f"{url}/auth-files"
    return f"{url}/v0/management/auth-files"


def _write_auth_archive(auth_dir: str, filename: str, payload: dict[str, Any]) -> Path | None:
    if not str(auth_dir or "").strip():
        return None
    directory = Path(auth_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    fd, temporary_name = tempfile.mkstemp(prefix=".xai-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        return destination
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def upload_xai_auth_payload(
    payload: dict[str, Any],
    *,
    management_url: str,
    management_token: str,
) -> tuple[str, int]:
    token = str(management_token or "").strip()
    if not token:
        raise ValueError("未配置 CPA 管理 Token")
    filename = credential_file_name(str(payload.get("email") or ""), str(payload.get("sub") or ""))
    response = requests.post(
        _normalize_management_url(management_url),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        files={"file": (filename, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"CPA 管理端上传失败（HTTP {response.status_code}）")
    return filename, int(response.status_code)


def mint_and_upload_xai_cpa(
    account,
    *,
    captcha_solver=None,
    task_control=None,
    log: Callable[[str], None] = print,
) -> tuple[bool, str, dict[str, Any]]:
    """Run xAI Device OAuth in a browser and import the resulting JSON into CPA."""
    management_url = str(config_store.get("grok_cpa_management_url", "") or "").strip()
    management_token = str(config_store.get("grok_cpa_management_token", "") or "").strip()
    proxy, proxy_source = _resolve_cpa_proxy(account)
    runtime_extra, runtime_source = _resolve_grok_runtime_extra(account)
    timeout_seconds = _positive_int(config_store.get("grok_cpa_timeout_seconds", ""), 300)
    headless, headless_source = _resolve_cpa_headless(account, runtime_extra)
    archive_dir = str(config_store.get("grok_cpa_auth_dir", "") or "").strip()
    yescaptcha_key = str(config_store.get("yescaptcha_key", "") or "").strip()
    if not management_url or not management_token:
        return False, "xAI CPA 未配置管理地址或管理 Token", {}

    try:
        log(f"xAI Device OAuth：代理来源 {proxy_source}")
        log(f"xAI Device OAuth：注册运行参数来源 {runtime_source}")
        log(f"xAI Device OAuth：浏览器模式来源 {headless_source}")
        minted = _mint_xai_device_token(
            account,
            proxy=proxy,
            headless=headless,
            runtime_extra=runtime_extra,
            timeout_seconds=timeout_seconds,
            captcha_solver=captcha_solver,
            yescaptcha_key=yescaptcha_key,
            task_control=task_control,
            log=log,
        )
        payload = build_xai_auth_payload(
            email=str(getattr(account, "email", "") or ""),
            access_token=minted.access_token,
            refresh_token=minted.refresh_token,
            id_token=minted.id_token,
            expires_in=minted.expires_in,
        )
        filename, status_code = upload_xai_auth_payload(
            payload,
            management_url=management_url,
            management_token=management_token,
        )
        archive = _write_auth_archive(archive_dir, filename, payload)
        metadata: dict[str, Any] = {
            "uploaded": True,
            "file_name": filename,
            "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "management_status": status_code,
        }
        if archive is not None:
            metadata["archived"] = True
        return True, f"xAI OAuth 认证文件已上传到 CPA：{filename}", metadata
    except TaskInterruption:
        raise
    except XaiDeviceOAuthError as exc:
        return False, str(exc), {}
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        return False, str(exc), {}
    except Exception as exc:
        return False, f"xAI OAuth/CPA 操作失败：{type(exc).__name__}", {}
