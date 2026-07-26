"""Qwen (chat.qwen.ai) registration via browser executor.

Aligned with qwen2api (Prodigalgal/qwen2api) verified flow:
    1. Open https://chat.qwen.ai/auth?mode=register
    2. Fill Full Name + Email + Password + Confirm Password
    3. Accept Terms checkbox and submit
    4. Wait briefly for Aliyun WAF; if captcha appears, **discard this attempt**
       (qwen2api default — no captcha solver / no ohmycaptcha). Optional
       `captcha_mode=solve` keeps the legacy in-session slide solver path.
    5. Capture browser cookies even when JWT is not yet issued
    6. Wait for activation email → open activation link with the same cookie jar
    7. Run OAuth device-code flow via API, authorizing with Cookie header
       (`/api/v2/oauth2/authorize`) to obtain access/refresh tokens
"""

import base64
import json
import hashlib
import math
import random
import re
import secrets
import string
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse, parse_qs

import requests
from core.browser_backend import Page
from platforms.zai.core import (
    ZaiRegister as _ZaiAliyunSupport,
    _ALIYUN_HOOK_JS,
    _ALIYUN_RETRYABLE_VERIFY_CODES,
)

QWEN_BASE_URL = "https://chat.qwen.ai"
QWEN_AUTH_URL = f"{QWEN_BASE_URL}/auth"
QWEN_ACTIVATE_URL = f"{QWEN_BASE_URL}/api/v1/auths/activate"
QWEN_OAUTH_DEVICE_CODE_URL = f"{QWEN_BASE_URL}/api/v1/oauth2/device/code"
QWEN_OAUTH_AUTHORIZE_URL = f"{QWEN_BASE_URL}/api/v2/oauth2/authorize"
QWEN_OAUTH_TOKEN_URL = f"{QWEN_BASE_URL}/api/v1/oauth2/token"
QWEN_USER_SETTINGS_URL = f"{QWEN_BASE_URL}/api/v2/users/user/settings/update"
QWEN_OAUTH_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_OAUTH_SCOPE = "openid profile email model.completion"
QWEN_ALIYUN_TASK_PROJECT_NAME = "any-auto-register:qwen"
# qwen2api default: discard captcha attempts; "solve" is legacy optional path;
# "manual" waits for human to clear Aliyun WAF in a headed browser.
QWEN_CAPTCHA_MODE_DISCARD = "discard"
QWEN_CAPTCHA_MODE_SOLVE = "solve"
QWEN_CAPTCHA_MODE_MANUAL = "manual"
QWEN_CAPTCHA_DISCARD_ERROR = "captcha_discard"
_QWEN_ALIYUN_VISIBLE_SELECTORS = (
    "#waf_nc_block",
    "#WAF_NC_WRAPPER",
    "#nocaptcha",
    "#aliyunCaptcha-window-embed.aliyunCaptcha-show",
    "#aliyunCaptcha-window-embed",
    "#aliyunCaptcha-img-box",
    "#aliyunCaptcha-sliding-body",
    "#aliyunCaptcha-sliding-slider",
)
_QWEN_LOCAL_FAIL_TEXT_TOKENS = (
    "验证失败",
    "verification failed",
    "please retry",
)
_QWEN_LOCAL_RETRY_END_OFFSETS = (0.0, 10.0, -10.0)
_QWEN_LOCAL_RETRY_CV_OFFSETS = (0.0, 8.0, -8.0)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _rand_password(n: int = 16) -> str:
    """Generate password with at least one upper, lower, digit, special."""
    chars = string.ascii_letters + string.digits + "!@#$"
    pw = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$"),
    ]
    pw += [random.choice(chars) for _ in range(n - 4)]
    random.shuffle(pw)
    return "".join(pw)


def extract_activation_link(text: str) -> str | None:
    """Extract Qwen activation URL from email text."""
    if not text:
        return None
    urls = re.findall(r'https?://[^\s"\'<>\]]+', text)
    for u in urls:
        if "activate" in u.lower() and "qwen" in u.lower():
            return u
    # Also check markdown-style links
    md_links = re.findall(r'\(([^)]*activate[^)]*)\)', text)
    for u in md_links:
        if "qwen" in u.lower():
            return u.strip()
    return None


def _cookies_to_header(cookies: Any) -> str:
    """Serialize cookie jar/dict into a Cookie request header."""
    if not cookies:
        return ""
    if isinstance(cookies, dict):
        items = cookies.items()
    elif hasattr(cookies, "get_dict"):
        try:
            items = cookies.get_dict().items()
        except Exception:
            return str(cookies)
    else:
        try:
            items = dict(cookies).items()
        except Exception:
            return str(cookies)
    return "; ".join(f"{key}={value}" for key, value in items if key and value is not None)


def _normalize_cookie_dict(cookies: Any) -> dict[str, str]:
    if not cookies:
        return {}
    if isinstance(cookies, dict):
        raw = cookies
    elif hasattr(cookies, "get_dict"):
        try:
            raw = cookies.get_dict()
        except Exception:
            return {}
    else:
        try:
            raw = dict(cookies)
        except Exception:
            return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name:
            continue
        text = str(value or "").strip()
        if text:
            out[name] = text
    return out


def call_activation_api(
    activation_url: str,
    user_agent: str = UA,
    *,
    cookies: dict[str, str] | None = None,
) -> dict:
    """Call the Qwen activation API, optionally replaying browser cookies.

    qwen2api activates by `session.get(verify_url)` with the browser cookie jar
    injected; plain id/token GET is kept as a fallback when cookies are empty.
    """
    cookie_map = _normalize_cookie_dict(cookies)
    target_url = str(activation_url or "").strip()
    if not target_url:
        return {"ok": False, "error": "Missing activation url"}

    # Prefer the original activation link (may be a front-end route that redirects).
    # Fall back to the explicit activate API when only id/token are available.
    parsed = urlparse(target_url)
    params = parse_qs(parsed.query)
    act_id = params.get("id", [None])[0]
    act_token = params.get("token", [None])[0]
    api_url = ""
    if act_id and act_token:
        api_url = f"{QWEN_ACTIVATE_URL}?id={act_id}&token={act_token}"

    try:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html, */*",
            }
        )
        for name, value in cookie_map.items():
            session.cookies.set(name, value, domain=".chat.qwen.ai")

        response = session.get(target_url, timeout=20, allow_redirects=True)
        # If the front-end link failed and we can hit the activate API, retry once.
        if response.status_code >= 400 and api_url and api_url != target_url:
            response = session.get(api_url, timeout=20, allow_redirects=True)

        merged = _normalize_cookie_dict(session.cookies)
        for name, value in cookie_map.items():
            merged.setdefault(name, value)
        token_from_cookie = str(merged.get("token") or "").strip()
        ok = response.status_code in (200, 302) or bool(token_from_cookie)
        return {
            "ok": ok,
            "status_code": response.status_code,
            "final_url": str(getattr(response, "url", "") or ""),
            "cookies": merged,
            "token": token_from_cookie,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "cookies": cookie_map}


def obtain_qwen_oauth_tokens_with_cookies(
    cookies: dict[str, str] | None,
    *,
    email: str = "",
    log_fn: Callable | None = None,
    poll_timeout_seconds: int = 60,
    user_agent: str = UA,
) -> dict:
    """OAuth device-code flow using browser cookies (qwen2api register.py path).

    After signup/activation the session cookie authorizes the device code via
    POST /api/v2/oauth2/authorize, then we poll /api/v1/oauth2/token.
    """
    log = log_fn or (lambda *_args, **_kwargs: None)
    cookie_map = _normalize_cookie_dict(cookies)
    if not cookie_map:
        log("Qwen OAuth cookie flow skipped: empty cookie jar")
        return {}

    cookie_header = _cookies_to_header(cookie_map)
    code_verifier, code_challenge = _generate_qwen_pkce_pair()
    try:
        device_resp = requests.post(
            QWEN_OAUTH_DEVICE_CODE_URL,
            data={
                "client_id": QWEN_OAUTH_CLIENT_ID,
                "scope": QWEN_OAUTH_SCOPE,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
        )
        if device_resp.status_code != 200:
            raise RuntimeError(
                f"Device code failed: HTTP {device_resp.status_code} {device_resp.text[:180]}"
            )
        try:
            device_data = device_resp.json()
        except Exception:
            device_data = {}
        if not isinstance(device_data, dict) or not device_data:
            # Some test doubles omit body text; still accept empty only as hard fail.
            if not isinstance(device_data, dict):
                raise RuntimeError("Device code response invalid")
        device_code = str(device_data.get("device_code") or "").strip()
        user_code = str(device_data.get("user_code") or "").strip()
        if not device_code:
            raise RuntimeError(f"Device code missing: {device_data}")

        if user_code:
            auth_resp = requests.post(
                QWEN_OAUTH_AUTHORIZE_URL,
                json={"approved": True, "user_code": user_code},
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Cookie": cookie_header,
                },
                timeout=20,
            )
            if auth_resp.status_code != 200:
                log(
                    f"Qwen OAuth authorize failed: HTTP {auth_resp.status_code} "
                    f"{(auth_resp.text or '')[:160]}"
                )
            else:
                log(f"Qwen OAuth authorize success{f': {email}' if email else ''}")

        token_payload = _poll_device_flow_token(
            device_code=device_code,
            code_verifier=code_verifier,
            timeout_seconds=poll_timeout_seconds,
            user_agent=user_agent,
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        refresh_token = str(token_payload.get("refresh_token") or "").strip()
        resource_url = str(token_payload.get("resource_url") or "").strip() or "portal.qwen.ai"
        if not access_token or not refresh_token:
            raise RuntimeError("OAuth token payload missing access/refresh token")
        return {
            "oauth_access_token": access_token,
            "refresh_token": refresh_token,
            "resource_url": resource_url,
            "oauth_token_type": str(token_payload.get("token_type") or "").strip(),
            "oauth_scope": str(token_payload.get("scope") or "").strip(),
            "oauth_expires_in": int(token_payload.get("expires_in") or 0),
            "token": access_token,
        }
    except Exception as e:
        log(f"Qwen OAuth cookie flow failed: {e}")
        return {}


def disable_qwen_memory_features(access_token: str, *, user_agent: str = UA) -> bool:
    """Best-effort: disable memory tools for a freshly registered account."""
    token = str(access_token or "").strip()
    if not token:
        return False
    try:
        resp = requests.post(
            QWEN_USER_SETTINGS_URL,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "memory": {"enable_memory": False, "enable_history_memory": False},
                "tools_enabled": {
                    "history_retriever": False,
                    "bio": False,
                },
            },
            timeout=10,
        )
        return resp.status_code in (200, 201, 204)
    except Exception:
        return False


def _b64url_no_padding(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _generate_qwen_pkce_pair() -> tuple[str, str]:
    verifier = _b64url_no_padding(secrets.token_bytes(32))
    challenge = _b64url_no_padding(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def _device_flow_request(code_challenge: str, user_agent: str = UA) -> dict:
    resp = requests.post(
        QWEN_OAUTH_DEVICE_CODE_URL,
        data={
            "client_id": QWEN_OAUTH_CLIENT_ID,
            "scope": QWEN_OAUTH_SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Qwen OAuth device code failed: HTTP {resp.status_code} {resp.text[:180]}")
    data = resp.json() if resp.text else {}
    if not isinstance(data, dict):
        raise RuntimeError("Qwen OAuth device code response invalid")
    if not data.get("device_code") or not data.get("verification_uri_complete"):
        raise RuntimeError(f"Qwen OAuth device code missing fields: {data}")
    return data


def _poll_device_flow_token(
    *,
    device_code: str,
    code_verifier: str,
    timeout_seconds: int = 30,
    user_agent: str = UA,
) -> dict:
    deadline = time.time() + max(6, int(timeout_seconds or 30))
    last_err = ""
    while time.time() < deadline:
        resp = requests.post(
            QWEN_OAUTH_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": QWEN_OAUTH_CLIENT_ID,
                "device_code": device_code,
                "code_verifier": code_verifier,
            },
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=20,
        )
        text = resp.text or ""
        parsed = {}
        try:
            parsed = resp.json()
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        if resp.status_code == 200 and isinstance(parsed, dict):
            access_token = str(parsed.get("access_token") or "").strip()
            refresh_token = str(parsed.get("refresh_token") or "").strip()
            if access_token and refresh_token:
                return parsed
            last_err = f"missing access/refresh token: {parsed}"
            break

        if resp.status_code == 400 and isinstance(parsed, dict):
            err = str(parsed.get("error") or "")
            if err in {"authorization_pending", "slow_down"}:
                time.sleep(2 if err == "authorization_pending" else 3)
                continue
            last_err = f"{err}: {parsed.get('error_description') or ''}".strip(": ")
            break

        last_err = f"HTTP {resp.status_code}: {text[:180]}"
        time.sleep(2)

    raise RuntimeError(f"Qwen OAuth token polling failed: {last_err or 'timeout'}")


def _click_first_confirm_button(page: Page) -> bool:
    name_candidates = [
        "确认",
        "Confirm",
        "Authorize",
        "授权",
        "同意",
        "Allow",
        "Approve",
    ]
    for name in name_candidates:
        try:
            locator = page.get_by_role("button", name=name, exact=True)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click()
                return True
        except Exception:
            continue

    selector_candidates = [
        'button:has-text("确认")',
        'button:has-text("Authorize")',
        'button:has-text("授权")',
        'button:has-text("同意")',
        'button:has-text("Allow")',
    ]
    for selector in selector_candidates:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.click()
                return True
        except Exception:
            continue

    try:
        fallback = page.locator("button").first
        if fallback.count() > 0 and fallback.is_visible():
            fallback.click()
            return True
    except Exception:
        pass
    return False


def _looks_like_oauth_success(page: Page) -> bool:
    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""
    body_text = str(body_text or "")
    return ("认证成功" in body_text) or ("请转到命令行界面" in body_text)


def obtain_qwen_oauth_tokens_from_logged_in_page(
    page: Page,
    *,
    log_fn: Callable | None = None,
    poll_timeout_seconds: int = 30,
) -> dict:
    log = log_fn or (lambda *_args, **_kwargs: None)
    try:
        code_verifier, code_challenge = _generate_qwen_pkce_pair()
        flow = _device_flow_request(code_challenge=code_challenge)
        verification_url = str(flow.get("verification_uri_complete") or "").strip()
        device_code = str(flow.get("device_code") or "").strip()
        if not verification_url or not device_code:
            raise RuntimeError(f"Qwen OAuth device flow missing fields: {flow}")

        page.goto(verification_url, wait_until="domcontentloaded", timeout=30000)
        for _ in range(12):
            if _looks_like_oauth_success(page):
                break
            try:
                if page.locator("button").count() > 0:
                    break
            except Exception:
                pass
            page.wait_for_timeout(800)
        if not _looks_like_oauth_success(page):
            clicked = _click_first_confirm_button(page)
            if not clicked:
                body_preview = ""
                try:
                    body_preview = (page.inner_text("body") or "")[:200].replace("\n", "|")
                except Exception:
                    body_preview = ""
                raise RuntimeError(
                    f"OAuth 授权页未找到可点击的确认按钮; url={page.url}; body={body_preview}"
                )
            page.wait_for_timeout(1500)

        token_payload = _poll_device_flow_token(
            device_code=device_code,
            code_verifier=code_verifier,
            timeout_seconds=poll_timeout_seconds,
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        refresh_token = str(token_payload.get("refresh_token") or "").strip()
        resource_url = str(token_payload.get("resource_url") or "").strip() or "portal.qwen.ai"
        if not access_token or not refresh_token:
            raise RuntimeError("Qwen OAuth token payload missing access/refresh token")

        return {
            "oauth_access_token": access_token,
            "refresh_token": refresh_token,
            "resource_url": resource_url,
            "oauth_token_type": str(token_payload.get("token_type") or "").strip(),
            "oauth_scope": str(token_payload.get("scope") or "").strip(),
            "oauth_expires_in": int(token_payload.get("expires_in") or 0),
        }
    except Exception as e:
        log(f"Qwen OAuth device-flow failed: {e}")
        return {}


def login_qwen_with_password(page: Page, email: str, password: str, *, log_fn: Callable | None = None) -> bool:
    log = log_fn or (lambda *_args, **_kwargs: None)
    if not email or not password:
        return False
    try:
        page.goto(f"{QWEN_AUTH_URL}?mode=login", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(800)
        page.locator('input[name="email"]').fill(email)
        page.locator('input[name="password"]').fill(password)
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_timeout(3500)
        url = str(page.url or "")
        if "chat.qwen.ai" not in url:
            return False
        if "/auth" in url and "mode=login" in url:
            return False
        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""
        if ("输入您的电子邮箱" in body_text) or ("继续使用 Google 登录" in body_text):
            return False
        try:
            cookies = page.context.cookies("https://chat.qwen.ai")
            has_token_cookie = any(
                str(item.get("name") or "") == "token" and str(item.get("value") or "").strip()
                for item in (cookies or [])
            )
            if not has_token_cookie:
                return False
        except Exception:
            return False
        return True
    except Exception as e:
        log(f"Qwen login failed before OAuth flow: {e}")
        return False


def obtain_qwen_oauth_tokens_with_login(
    page: Page,
    *,
    email: str,
    password: str,
    log_fn: Callable | None = None,
    poll_timeout_seconds: int = 30,
) -> dict:
    if not login_qwen_with_password(page, email, password, log_fn=log_fn):
        return {}
    return obtain_qwen_oauth_tokens_from_logged_in_page(
        page,
        log_fn=log_fn,
        poll_timeout_seconds=poll_timeout_seconds,
    )


def wait_for_activation_link(
    mailbox,
    mail_acct=None,
    *,
    account_email: str = "",
    timeout: int = 120,
    before_ids: set = None,
    log_fn: Callable | None = None,
    max_errors: int = 3,
) -> str | None:
    """Poll mailbox for Qwen activation email and extract link.

    Supports:
    - CFWorker-style mailbox exposing `_get_mails(email)`
    - legacy mailbox exposing `get_messages/get_message_body`
    """
    log = log_fn or (lambda *_args, **_kwargs: None)
    seen = set(before_ids or [])

    def _decode_mime_raw(raw: str) -> str:
        raw = str(raw or "")
        if not raw:
            return ""
        try:
            import email

            msg = email.message_from_string(raw)
            chunks: list[str] = []
            if msg.is_multipart():
                for part in msg.walk():
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        continue
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        text = payload.decode(charset, errors="ignore")
                    except Exception:
                        text = payload.decode("utf-8", errors="ignore")
                    if text:
                        chunks.append(text)
            else:
                payload = msg.get_payload(decode=True)
                if payload is not None:
                    charset = msg.get_content_charset() or "utf-8"
                    try:
                        text = payload.decode(charset, errors="ignore")
                    except Exception:
                        text = payload.decode("utf-8", errors="ignore")
                    if text:
                        chunks.append(text)
            return "\n".join(chunks).strip()
        except Exception:
            return ""

    def _collect_mail_text(item: dict) -> str:
        if not isinstance(item, dict):
            return str(item or "")
        raw = str(item.get("raw") or "")
        decoded_raw = _decode_mime_raw(raw)
        return " ".join(
            [
                str(item.get("subject") or ""),
                raw,
                decoded_raw,
                str(item.get("text") or ""),
                str(item.get("content") or ""),
                str(item.get("html") or ""),
                str(item.get("body") or ""),
            ]
        ).strip()

    start = time.time()
    error_count = 0
    while time.time() - start < timeout:
        time.sleep(5)
        try:
            # CFWorker mailbox path
            if account_email and hasattr(mailbox, "_get_mails"):
                messages = mailbox._get_mails(account_email) or []
                for msg in messages:
                    mid = str((msg or {}).get("id") or "")
                    if mid and mid in seen:
                        continue
                    if mid:
                        seen.add(mid)
                    body = _collect_mail_text(msg)
                    link = extract_activation_link(body)
                    if link:
                        return link
                continue

            # Legacy custom mailbox path
            if not (
                mail_acct
                and hasattr(mailbox, "get_messages")
                and hasattr(mailbox, "get_message_body")
            ):
                log("Activation link polling aborted: mailbox does not expose readable message APIs")
                break

            messages = mailbox.get_messages(mail_acct, before_ids=seen)
            for msg in messages or []:
                mid = str((msg or {}).get("id") or "")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                body = mailbox.get_message_body(mail_acct, msg.get("id")) or ""
                link = extract_activation_link(body)
                if link:
                    return link
            error_count = 0
        except Exception as e:
            error_count += 1
            log(f"Activation polling error {error_count}/{max_errors}: {e}")
            if error_count >= max(1, int(max_errors or 1)):
                break
            continue
    return None


class QwenRegister:
    """Automate Qwen account registration via Playwright."""

    _wait_until = _ZaiAliyunSupport._wait_until
    _sleep_with_checkpoint = _ZaiAliyunSupport._sleep_with_checkpoint
    _install_response_capture = _ZaiAliyunSupport._install_response_capture
    _record_aliyun_network_trace = _ZaiAliyunSupport._record_aliyun_network_trace
    _locator_bbox = _ZaiAliyunSupport._locator_bbox
    _locator_screenshot = _ZaiAliyunSupport._locator_screenshot
    _slide_action_bbox = _ZaiAliyunSupport._slide_action_bbox
    _screenshot_clip_with_hidden = _ZaiAliyunSupport._screenshot_clip_with_hidden
    _has_live_piece = _ZaiAliyunSupport._has_live_piece
    _current_piece_center_x = _ZaiAliyunSupport._current_piece_center_x
    _drag_slider_closed_loop = _ZaiAliyunSupport._drag_slider_closed_loop
    _finalize_drag_release = _ZaiAliyunSupport._finalize_drag_release
    _drag_slider = _ZaiAliyunSupport._drag_slider
    _wait_for_aliyun_slide_ready = _ZaiAliyunSupport._wait_for_aliyun_slide_ready
    _get_aliyun_debug_state = _ZaiAliyunSupport._get_aliyun_debug_state
    _debug_summary = _ZaiAliyunSupport._debug_summary
    _latest_aliyun_error = _ZaiAliyunSupport._latest_aliyun_error
    _refresh_aliyun_challenge = _ZaiAliyunSupport._refresh_aliyun_challenge

    _annotate_latest_aliyun_network_trace = staticmethod(
        _ZaiAliyunSupport._annotate_latest_aliyun_network_trace
    )
    _bounded_list_append = staticmethod(_ZaiAliyunSupport._bounded_list_append)
    _closed_loop_drag_step = staticmethod(_ZaiAliyunSupport._closed_loop_drag_step)
    _estimate_gap_center_from_images = staticmethod(
        _ZaiAliyunSupport._estimate_gap_center_from_images
    )
    _summarize_aliyun_requests = staticmethod(
        _ZaiAliyunSupport._summarize_aliyun_requests
    )
    _summarize_aliyun_verify_post_data = staticmethod(
        _ZaiAliyunSupport._summarize_aliyun_verify_post_data
    )

    def __init__(
        self,
        executor,
        log_fn: Callable = print,
        *,
        captcha_solver=None,
        captcha_mode: str | None = None,
        task_control=None,
        max_retries: int | None = None,
    ):
        """executor must be a browser executor (headless or headed).

        captcha_mode:
          - discard (default, qwen2api): captcha appears → fail this attempt
          - solve: use captcha_solver for in-session Aliyun slide solving
          - manual: headed browser — wait for human to clear Aliyun WAF
        """
        self.executor = executor
        self.log = log_fn
        self.captcha_solver = captcha_solver
        mode = str(captcha_mode or QWEN_CAPTCHA_MODE_DISCARD).strip().lower()
        if mode not in {
            QWEN_CAPTCHA_MODE_DISCARD,
            QWEN_CAPTCHA_MODE_SOLVE,
            QWEN_CAPTCHA_MODE_MANUAL,
        }:
            mode = QWEN_CAPTCHA_MODE_DISCARD
        self.captcha_mode = mode
        self._task_control = task_control
        # qwen2api relies on many retries with clean IPs; keep a few local retries.
        # manual mode: usually one human-assisted attempt is enough.
        if max_retries is None:
            self._max_retries = 0 if mode == QWEN_CAPTCHA_MODE_MANUAL else 2
        else:
            self._max_retries = max(0, int(max_retries))
        self._response_store: dict[str, Any] = {}
        self._response_capture_installed = False

    def _checkpoint(self) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint()

    @staticmethod
    def _safe_body_text(page, limit: int = 600) -> str:
        try:
            text = str(page.locator("body").inner_text(timeout=1500) or "").strip()
        except Exception:
            return ""
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]

    @staticmethod
    def _extract_instruction_line(text: str, hints: tuple[str, ...]) -> str | None:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        normalized_hints = tuple(str(token).lower() for token in hints)
        for line in lines:
            lowered = line.lower()
            if any(token in lowered for token in normalized_hints):
                return line
        return None

    def _challenge_question(self, page) -> str:
        text = self._safe_body_text(page, limit=1200)
        line = self._extract_instruction_line(
            text,
            ("drag", "slider", "slide", "拼图", "滑块", "拖动", "滑动"),
        )
        if line:
            return line
        return "请拖动滑块完成拼图"

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
            raise RuntimeError(f"Qwen 阿里云坐标异常: {payload!r}")
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
        if abs(local_mapped_x - estimated_center_x) > 10.0:
            self.log(
                "Qwen Aliyun image estimator disagrees with solver; "
                f"keep llm_local_x={local_mapped_x:.2f}, "
                f"estimated_local_x={estimated_center_x:.2f}, width={background_width}"
            )
        return mapped_gap_x

    def _resolve_cv_slide_end_x(
        self,
        bbox: dict[str, float],
        *,
        background_png: bytes | None,
        piece_png: bytes | None,
    ) -> float | None:
        estimated_gap = self._estimate_gap_center_from_images(background_png, piece_png)
        if estimated_gap is None:
            return None
        estimated_center_x, background_width = estimated_gap
        if background_width <= 0:
            return None
        return bbox["x"] + ((estimated_center_x / background_width) * bbox["width"])

    @staticmethod
    def _png_fingerprint(value: bytes | None) -> str:
        if not value:
            return ""
        return hashlib.sha256(value).hexdigest()[:16]

    def _build_local_drag_strategies(
        self,
        *,
        base_end_x: float,
        cv_end_x: float | None,
    ) -> list[dict[str, Any]]:
        strategies: list[dict[str, Any]] = []

        def _append(profile: str, anchor: str, end_x: float, offset: float) -> None:
            strategies.append(
                {
                    "profile": profile,
                    "anchor": anchor,
                    "end_x": float(end_x),
                    "offset": float(offset),
                }
            )

        for offset in _QWEN_LOCAL_RETRY_END_OFFSETS:
            _append("closed_loop", "solver", base_end_x + offset, offset)
        _append("smooth", "solver", base_end_x, 0.0)
        _append("overshoot", "solver", base_end_x, 0.0)

        if cv_end_x is not None and abs(float(cv_end_x) - float(base_end_x)) > 3.0:
            for offset in _QWEN_LOCAL_RETRY_CV_OFFSETS:
                _append("closed_loop", "cv", cv_end_x + offset, offset)
            _append("smooth", "cv", cv_end_x, 0.0)

        deduped: list[dict[str, Any]] = []
        seen: list[tuple[str, float]] = []
        for item in strategies:
            key = (str(item["profile"]), round(float(item["end_x"]), 2))
            if any(profile == key[0] and abs(value - key[1]) <= 1.0 for profile, value in seen):
                continue
            seen.append(key)
            deduped.append(item)
        return deduped

    def _capture_qwen_challenge_snapshot(
        self,
        page,
        *,
        background_png: bytes | None = None,
        piece_png: bytes | None = None,
    ) -> dict[str, Any]:
        try:
            payload = page.evaluate(
                """() => {
                    const read = (selector) => {
                        const el = document.querySelector(selector);
                        if (!el) {
                            return null;
                        }
                        return {
                            className: String(el.className || ''),
                            style: String(el.getAttribute('style') || ''),
                            text: String(el.textContent || '').slice(0, 160),
                        };
                    };
                    return {
                        body: read('#aliyunCaptcha-sliding-body'),
                        slider: read('#aliyunCaptcha-sliding-slider'),
                        puzzle: read('#aliyunCaptcha-puzzle'),
                        text: String(document.body && document.body.innerText || '').slice(0, 500),
                    };
                }"""
            )
        except Exception:
            payload = {}
        snapshot = payload if isinstance(payload, dict) else {}
        body = snapshot.get("body") if isinstance(snapshot.get("body"), dict) else {}
        slider = snapshot.get("slider") if isinstance(snapshot.get("slider"), dict) else {}
        puzzle = snapshot.get("puzzle") if isinstance(snapshot.get("puzzle"), dict) else {}
        text = str(snapshot.get("text") or "")
        return {
            "body_class": str(body.get("className") or "").strip(),
            "body_text": str(body.get("text") or "").strip(),
            "slider_class": str(slider.get("className") or "").strip(),
            "slider_style": str(slider.get("style") or "").strip(),
            "puzzle_style": str(puzzle.get("style") or "").strip(),
            "text": text,
            "bg_hash": self._png_fingerprint(background_png),
            "piece_hash": self._png_fingerprint(piece_png),
        }

    def _is_qwen_local_slide_fail(self, snapshot: dict[str, Any] | None) -> bool:
        if not isinstance(snapshot, dict):
            return False
        body_class = str(snapshot.get("body_class") or "").strip().lower()
        if "fail" in body_class:
            return True
        combined = " ".join(
            str(snapshot.get(key) or "")
            for key in ("body_text", "text", "slider_class", "slider_style")
        ).strip()
        lowered = combined.lower()
        return any(token in lowered for token in _QWEN_LOCAL_FAIL_TEXT_TOKENS)

    def _wait_for_qwen_local_reset(
        self,
        page,
        *,
        timeout: float,
        bg_hash: str,
    ) -> dict[str, Any]:
        deadline = time.time() + max(float(timeout or 0), 0.1)
        last_snapshot: dict[str, Any] = {}
        while time.time() < deadline:
            if not self._has_aliyun_waf_challenge(page):
                return {"status": "cleared", "snapshot": last_snapshot}
            img_bbox = self._locator_bbox(page.locator("#aliyunCaptcha-img-box").first)
            background_png = None
            if img_bbox is not None:
                background_png = self._screenshot_clip_with_hidden(
                    page,
                    img_bbox,
                    hidden_selectors=["#aliyunCaptcha-puzzle", "#aliyunCaptcha-btn-refresh"],
                )
            piece_png = self._locator_screenshot(page.locator("#aliyunCaptcha-puzzle").first)
            last_snapshot = self._capture_qwen_challenge_snapshot(
                page,
                background_png=background_png,
                piece_png=piece_png,
            )
            current_bg_hash = str(last_snapshot.get("bg_hash") or "")
            if current_bg_hash and bg_hash and current_bg_hash != bg_hash:
                return {"status": "changed", "snapshot": last_snapshot}
            if not self._is_qwen_local_slide_fail(last_snapshot):
                return {"status": "ready", "snapshot": last_snapshot}
            self._sleep_with_checkpoint(0.25)
        return {"status": "timeout", "snapshot": last_snapshot}

    def _drag_slider_with_profile(
        self,
        page,
        *,
        profile: str,
        start_x: float,
        start_y: float,
        end_x: float,
    ) -> None:
        profile_name = str(profile or "closed_loop").strip().lower()
        if profile_name == "closed_loop":
            self._drag_slider(page, start_x=start_x, start_y=start_y, end_x=end_x)
            return

        if profile_name == "smooth":
            steps = 52
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.wait_for_timeout(140)
            for step in range(1, steps + 1):
                progress = step / steps
                eased = 1.0 - math.pow(1.0 - progress, 2.35)
                x = start_x + ((end_x - start_x) * eased)
                y = start_y + (0.8 if step % 4 == 0 else (-0.55 if step % 4 == 1 else 0.2))
                page.mouse.move(x, y)
                page.wait_for_timeout(14 + ((step % 3) * 3))
            page.wait_for_timeout(90)
            page.mouse.up()
            return

        if profile_name == "overshoot":
            overshoot_x = min(end_x + 8.0, start_x + 260.0)
            steps = 58
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.wait_for_timeout(140)
            for step in range(1, steps + 1):
                progress = step / steps
                eased = 1.0 - math.pow(1.0 - progress, 2.15)
                x = start_x + ((overshoot_x - start_x) * eased)
                y = start_y + (0.65 if step % 5 == 0 else (-0.45 if step % 5 == 1 else 0.15))
                page.mouse.move(x, y)
                page.wait_for_timeout(13 + ((step % 4) * 3))
            page.wait_for_timeout(60)
            page.mouse.move(max(start_x, overshoot_x - 3.0), start_y + 0.25)
            page.wait_for_timeout(65)
            page.mouse.move(end_x, start_y)
            page.wait_for_timeout(95)
            page.mouse.up()
            return

        raise RuntimeError(f"Qwen 未知滑块拖动策略: {profile!r}")

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
            "slideBBox": {
                key: round(float(value), 2)
                for key, value in slide_bbox.items()
                if isinstance(value, (int, float))
            },
            "imageBBox": {
                key: round(float(value), 2)
                for key, value in (img_bbox or {}).items()
                if isinstance(value, (int, float))
            }
            or None,
            "sliderHandleBBox": {
                key: round(float(value), 2)
                for key, value in slider_handle_bbox.items()
                if isinstance(value, (int, float))
            },
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
            "backgroundSize": _ZaiAliyunSupport._png_size(background_png),
            "pieceSize": _ZaiAliyunSupport._png_size(piece_png),
        }
        self.log(f"Qwen Aliyun action trace {json.dumps(trace, ensure_ascii=False, default=str)}")

    def _ensure_aliyun_instrumentation(self, page):
        try:
            context = getattr(page, "context", None)
            if context is not None and hasattr(context, "add_init_script"):
                context.add_init_script(script=_ALIYUN_HOOK_JS)
        except Exception:
            pass
        try:
            if hasattr(page, "evaluate"):
                page.evaluate(_ALIYUN_HOOK_JS)
        except Exception:
            pass
        if not self._response_capture_installed and hasattr(page, "on"):
            self._install_response_capture(page, self._response_store)
            self._response_capture_installed = True
        return page

    def _has_aliyun_waf_challenge(self, page) -> bool:
        for selector in _QWEN_ALIYUN_VISIBLE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    return True
            except Exception:
                continue
        body = self._safe_body_text(page, limit=800)
        if "访问验证" in body and "拖动滑块完成拼图" in body:
            return True
        if "Access Verification" in body and "slider" in body.lower():
            return True
        return False

    def _recognize_aliyun_slide_action(
        self,
        screenshot_b64: str,
        *,
        question: str,
        background_b64: str | None = None,
        piece_b64: str | None = None,
    ) -> dict[str, Any]:
        actions = self._recognize_aliyun_slide_actions(
            screenshot_b64,
            question=question,
            background_b64=background_b64,
            piece_b64=piece_b64,
        )
        if actions:
            return actions[0]
        raise RuntimeError("Qwen 阿里云滑块识别结果为空")

    def _recognize_aliyun_slide_actions(
        self,
        screenshot_b64: str,
        *,
        question: str,
        background_b64: str | None = None,
        piece_b64: str | None = None,
    ) -> list[dict[str, Any]]:
        solve_slide_action = getattr(self.captcha_solver, "solve_aliyun_slide_action", None)
        if callable(solve_slide_action):
            actions: list[dict[str, Any]] = []
            for _ in range(3):
                try:
                    action = solve_slide_action(
                        screenshot_b64,
                        question=question,
                        background=background_b64,
                        piece=piece_b64,
                        timeout_s=45.0,
                        project_name=QWEN_ALIYUN_TASK_PROJECT_NAME,
                        schema_mode="slide",
                    )
                except NotImplementedError:
                    action = None
                    break
                if (
                    isinstance(action, dict)
                    and isinstance(action.get("slider"), dict)
                    and isinstance(action.get("gap"), dict)
                ):
                    actions.append(action)
            if actions:
                return actions

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
            try:
                action = json.loads(str(raw or "").strip())
            except Exception:
                action = None
            last_action = action
            if (
                isinstance(action, dict)
                and isinstance(action.get("slider"), dict)
                and isinstance(action.get("gap"), dict)
            ):
                return [action]
        if isinstance(last_action, dict):
            return [last_action]
        raise RuntimeError(f"Qwen 阿里云滑块识别结果异常: {last_action!r}")

    def _select_qwen_slide_action(
        self,
        *,
        bbox: dict[str, float],
        actions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not actions:
            raise RuntimeError("Qwen 阿里云滑块识别缺少候选动作")

        candidates: list[dict[str, Any]] = []
        for index, action in enumerate(actions):
            gap = action.get("gap")
            if not isinstance(gap, dict):
                continue
            image_size = action.get("imageSize") if isinstance(action.get("imageSize"), dict) else {}
            reference_width = self._read_number(image_size.get("width")) or 1440.0
            reference_height = self._read_number(image_size.get("height")) or 900.0
            mapped_gap_x, _ = self._map_point(
                bbox,
                gap,
                reference_width=reference_width,
                reference_height=reference_height,
            )
            candidates.append(
                {
                    "index": index,
                    "action": action,
                    "local_gap_x": mapped_gap_x - bbox["x"],
                }
            )

        if not candidates:
            return actions[0], {"sampleLocalGapXs": [], "selectedLocalGapX": None, "clusterSize": 0}
        if len(candidates) == 1:
            only = candidates[0]
            return only["action"], {
                "sampleLocalGapXs": [round(float(only["local_gap_x"]), 2)],
                "selectedLocalGapX": round(float(only["local_gap_x"]), 2),
                "clusterSize": 1,
            }

        ordered = sorted(candidates, key=lambda item: float(item["local_gap_x"]))
        clusters: list[list[dict[str, Any]]] = []
        for candidate in ordered:
            placed = False
            for cluster in clusters:
                center = sum(float(item["local_gap_x"]) for item in cluster) / len(cluster)
                if abs(float(candidate["local_gap_x"]) - center) <= 6.0:
                    cluster.append(candidate)
                    placed = True
                    break
            if not placed:
                clusters.append([candidate])

        def _cluster_key(cluster: list[dict[str, Any]]) -> tuple[int, float]:
            values = [float(item["local_gap_x"]) for item in cluster]
            spread = max(values) - min(values) if values else 0.0
            return (len(cluster), -spread)

        selected_cluster = sorted(clusters, key=_cluster_key, reverse=True)[0]
        selected_values = sorted(float(item["local_gap_x"]) for item in selected_cluster)
        median_local_x = selected_values[len(selected_values) // 2]
        selected_candidate = min(
            selected_cluster,
            key=lambda item: abs(float(item["local_gap_x"]) - median_local_x),
        )
        sample_local_xs = [round(float(item["local_gap_x"]), 2) for item in ordered]
        return selected_candidate["action"], {
            "sampleLocalGapXs": sample_local_xs,
            "selectedLocalGapX": round(float(selected_candidate["local_gap_x"]), 2),
            "clusterSize": len(selected_cluster),
        }

    def _wait_for_aliyun_challenge_outcome(self, page, *, timeout: float) -> bool:
        deadline = time.time() + max(float(timeout or 0), 0.1)
        while time.time() < deadline:
            tokens = self._extract_tokens(page)
            if self._resolve_access_token(tokens):
                return True
            if not self._has_aliyun_waf_challenge(page):
                return True
            error = self._latest_aliyun_error(page)
            if isinstance(error, dict) and error.get("verifyResult") is False:
                raise RuntimeError(
                    "Qwen 阿里云验证被拒绝: "
                    f"{json.dumps(error, ensure_ascii=False, default=str)}"
                )
            self._sleep_with_checkpoint(0.35)
        return False

    def _solve_aliyun_waf_challenge(self, page) -> None:
        page = self._ensure_aliyun_instrumentation(page)
        self.log("  检测到 Qwen Aliyun WAF 验证，开始同会话求解 ...")
        if not self.captcha_solver:
            raise RuntimeError("Qwen 触发阿里云访问验证，但当前未配置验证码求解器")
        self._wait_for_aliyun_slide_ready(
            page,
            timeout=20.0,
            desc="等待 Qwen 阿里云滑块窗口超时",
        )
        question = self._challenge_question(page)
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            slide_bbox = self._slide_action_bbox(page)
            if not slide_bbox:
                raise RuntimeError("未找到 Qwen 阿里云滑块截图区域")
            slider_handle_bbox = self._locator_bbox(
                page.locator("#aliyunCaptcha-sliding-slider").first
            )
            if not slider_handle_bbox:
                raise RuntimeError("未找到 Qwen 阿里云滑块手柄")

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
            action_samples = self._recognize_aliyun_slide_actions(
                screenshot_b64,
                question=question,
                background_b64=background_b64,
                piece_b64=piece_b64,
            )
            action, action_meta = self._select_qwen_slide_action(
                bbox=slide_bbox,
                actions=action_samples,
            )
            slider = action.get("slider")
            gap = action.get("gap")
            if not isinstance(slider, dict) or not isinstance(gap, dict):
                raise RuntimeError(f"Qwen 阿里云滑块识别缺少 slider/gap: {action!r}")
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
            cv_end_x = self._resolve_cv_slide_end_x(
                slide_bbox,
                background_png=background_png,
                piece_png=piece_png,
            )
            bg_hash = self._png_fingerprint(background_png)
            strategies = self._build_local_drag_strategies(
                base_end_x=end_x,
                cv_end_x=cv_end_x,
            )
            self.log(
                "  Qwen slider ensemble "
                f"samples={action_meta.get('sampleLocalGapXs')} "
                f"selected={action_meta.get('selectedLocalGapX')} "
                f"cluster={action_meta.get('clusterSize')}"
            )
            challenge_changed = False

            for strategy_index, strategy in enumerate(strategies, start=1):
                strategy_end_x = float(strategy.get("end_x") or end_x)
                self._log_aliyun_action_trace(
                    attempt=attempt,
                    action={
                        **action,
                        "strategyProfile": strategy.get("profile"),
                        "strategyAnchor": strategy.get("anchor"),
                        "strategyOffset": strategy.get("offset"),
                    },
                    slide_bbox=slide_bbox,
                    img_bbox=img_bbox,
                    slider_handle_bbox=slider_handle_bbox,
                    reference_width=reference_width,
                    reference_height=reference_height,
                    start_x=start_x,
                    start_y=start_y,
                    end_x=strategy_end_x,
                    background_png=background_png,
                    piece_png=piece_png,
                )
                self.log(
                    "  Qwen local drag strategy "
                    f"{strategy_index}/{len(strategies)} "
                    f"profile={strategy.get('profile')} anchor={strategy.get('anchor')} "
                    f"offset={strategy.get('offset')} bg={bg_hash or 'none'}"
                )
                self._drag_slider_with_profile(
                    page,
                    profile=str(strategy.get("profile") or "closed_loop"),
                    start_x=start_x,
                    start_y=start_y,
                    end_x=strategy_end_x,
                )
                self._sleep_with_checkpoint(0.4)
                try:
                    if self._wait_for_aliyun_challenge_outcome(page, timeout=1.4):
                        return
                except Exception:
                    error = self._latest_aliyun_error(page)
                    verify_code = str((error or {}).get("verifyCode") or "").strip().upper()
                    if verify_code in _ALIYUN_RETRYABLE_VERIFY_CODES:
                        challenge_changed = True
                        break
                    raise
                if not self._has_aliyun_waf_challenge(page):
                    return

                snapshot = self._capture_qwen_challenge_snapshot(
                    page,
                    background_png=background_png,
                    piece_png=piece_png,
                )
                if not self._is_qwen_local_slide_fail(snapshot):
                    continue
                self.log(
                    "  Qwen local slide verification failed "
                    f"(body_class={snapshot.get('body_class')!r}, bg={bg_hash or 'none'})"
                )
                reset_result = self._wait_for_qwen_local_reset(
                    page,
                    timeout=2.2,
                    bg_hash=bg_hash,
                )
                reset_status = str(reset_result.get("status") or "").strip()
                reset_snapshot = (
                    reset_result.get("snapshot")
                    if isinstance(reset_result.get("snapshot"), dict)
                    else {}
                )
                if reset_status == "cleared":
                    return
                if reset_status == "changed":
                    challenge_changed = True
                    self.log("  Qwen challenge content changed after local fail; rebuild candidates")
                    break
                if reset_status == "ready":
                    if self._is_qwen_local_slide_fail(reset_snapshot):
                        challenge_changed = True
                        break
                    continue
                if reset_status == "timeout":
                    challenge_changed = True
                    break

            if not self._has_aliyun_waf_challenge(page):
                return
            if attempt < max_attempts:
                if challenge_changed:
                    self.log(
                        f"  Qwen Aliyun challenge moved to a new state, retry {attempt + 1}/{max_attempts}"
                    )
                else:
                    self.log(
                        f"  Qwen Aliyun challenge still visible, refresh and retry {attempt + 1}/{max_attempts}"
                    )
                    self._refresh_aliyun_challenge(page)
                    self._sleep_with_checkpoint(0.9)
                    self._wait_for_aliyun_slide_ready(
                        page,
                        timeout=8.0,
                        desc="刷新后等待 Qwen 阿里云滑块窗口超时",
                    )
                continue

        raise RuntimeError(
            "Qwen 阿里云验证未解除; "
            f"debug={self._debug_summary(page, response_store=self._response_store)}"
        )

    def _summarize_signup_response(self) -> str:
        entry = self._response_store.get("signup")
        if not isinstance(entry, dict):
            return "signup_response=missing"
        status = int(entry.get("status") or 0)
        if self._is_aliyun_waf_signup_response(entry):
            text = str(entry.get("text") or "")
            preview = re.sub(r"\s+", " ", text).strip()[:160]
            return f"signup_response=aliyun_waf status={status} preview={preview}"
        payload = entry.get("json")
        if isinstance(payload, dict):
            keys = sorted(str(key) for key in payload.keys())[:12]
            detail = str(
                payload.get("detail")
                or payload.get("message")
                or payload.get("error")
                or ""
            ).strip()
            return (
                f"signup_response=json status={status} keys={keys}"
                + (f" detail={detail[:160]}" if detail else "")
            )
        text = str(entry.get("text") or "")
        preview = re.sub(r"\s+", " ", text).strip()[:160]
        return f"signup_response=text status={status} preview={preview}"

    @staticmethod
    def _is_aliyun_waf_signup_response(entry: dict[str, Any] | None) -> bool:
        if not isinstance(entry, dict):
            return False
        text = str(entry.get("text") or "").lower()
        if not text:
            return False
        return any(
            marker in text
            for marker in (
                "waf_nc_block",
                "aliyuncaptcha",
                "访问验证",
                "拖动滑块完成拼图",
                "captcha-open-southeast.aliyuncs.com",
                "aliyun_waf",
            )
        )

    def _build_post_submit_failure_reason(self, page) -> str:
        signup_summary = self._summarize_signup_response()
        if self._has_aliyun_waf_challenge(page) or self._is_aliyun_waf_signup_response(
            self._response_store.get("signup")
        ):
            return (
                "Qwen signup triggered Aliyun WAF challenge but no auth token was issued; "
                f"{signup_summary}; debug={self._debug_summary(page, response_store=self._response_store)}"
            )
        return (
            "Qwen registration failed after submit without auth token; "
            f"{signup_summary}; url={page.url}; body={self._safe_body_text(page, limit=500)}"
        )

    def _await_post_submit_tokens(self, page, *, timeout_seconds: float = 35.0) -> dict[str, Any]:
        """Wait after signup submit.

        qwen2api path (discard): watch ~5s for captcha; if visible, stop without solving.
        manual path: headed browser — wait for human to clear Aliyun WAF.
        Legacy path (solve): attempt in-session Aliyun slide solving when challenge appears.
        """
        mode = self.captcha_mode
        if mode == QWEN_CAPTCHA_MODE_MANUAL:
            # Give the operator enough time to finish the slider in headed mode.
            deadline = time.time() + max(float(timeout_seconds or 0), 180.0)
            announced = False
            while time.time() < deadline:
                tokens = self._extract_tokens(page)
                if self._resolve_access_token(tokens):
                    return tokens
                if self._has_aliyun_waf_challenge(page):
                    if not announced:
                        self.log(
                            "  ⚠️ 检测到阿里云验证码：请在 headed 浏览器中手动完成滑块"
                        )
                        announced = True
                    self._sleep_with_checkpoint(0.8)
                    continue
                # Captcha cleared (or never shown) — wait a bit for cookies/token.
                if announced:
                    self.log("  验证码已消失，等待注册结果...")
                self._sleep_with_checkpoint(0.5)
                tokens = self._extract_tokens(page)
                if self._resolve_access_token(tokens):
                    return tokens
                # No captcha and no token yet — may be pending activation (ok).
                if not self._has_aliyun_waf_challenge(page):
                    # Short settle window after clear.
                    settle_deadline = time.time() + 5.0
                    while time.time() < settle_deadline:
                        tokens = self._extract_tokens(page)
                        if self._resolve_access_token(tokens):
                            return tokens
                        self._sleep_with_checkpoint(0.4)
                    return self._extract_tokens(page)
            return self._extract_tokens(page)

        discard_mode = mode != QWEN_CAPTCHA_MODE_SOLVE
        # qwen2api waits up to 5s for captcha visibility after click.
        captcha_watch_seconds = 5.0 if discard_mode else max(float(timeout_seconds or 0), 1.0)
        deadline = time.time() + max(float(timeout_seconds or 0), captcha_watch_seconds)
        captcha_deadline = time.time() + captcha_watch_seconds

        while time.time() < deadline:
            tokens = self._extract_tokens(page)
            if self._resolve_access_token(tokens):
                return tokens

            has_captcha = self._has_aliyun_waf_challenge(page)
            if has_captcha:
                if discard_mode:
                    # Match qwen2api: do not solve; caller discards this attempt/email.
                    self.log("  captcha_discard: Aliyun WAF detected, skip solver (qwen2api mode)")
                    return tokens
                self._solve_aliyun_waf_challenge(page)
                continue

            # In discard mode, after the short captcha watch window, keep polling for
            # cookies/token a bit longer without treating missing captcha as failure.
            if discard_mode and time.time() > captcha_deadline:
                # No captcha within 5s — qwen2api treats this as signup accepted.
                # Still allow a short grace period for cookies to settle.
                if time.time() > captcha_deadline + 3.0:
                    return self._extract_tokens(page)

            self._sleep_with_checkpoint(0.35)
        return self._extract_tokens(page)

    @staticmethod
    def _resolve_access_token(tokens: dict | None) -> str:
        if not isinstance(tokens, dict):
            return ""
        return str(
            tokens.get("token")
            or tokens.get("cookie:token")
            or tokens.get("access_token")
            or ""
        ).strip()

    def register(
        self,
        email: str,
        password: str = None,
        full_name: str = "",
        _otp_callback: Optional[Callable] = None,
        _captcha_token: str = "",
    ) -> dict:
        if not password:
            password = _rand_password()

        if not full_name:
            # qwen2api uses user######; keep readable names for local accounts.
            full_name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
            if not full_name or full_name.lower() == "user":
                full_name = f"user{random.randint(100000, 999999)}"

        self.log(f"Qwen registration — email: {email}, name: {full_name}")

        page = self.executor.page
        if page is None:
            raise RuntimeError(
                "Qwen requires a browser executor (headless/headed Patchright). "
                "Please select 'headless' or 'headed' executor."
            )
        page = self._ensure_aliyun_instrumentation(page)

        last_reason = "signup failed"
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                self.log(f"  Retry {attempt}/{self._max_retries}...")
                self._sleep_with_checkpoint(5)

            result = self._try_register(page, email, password, full_name)
            if not isinstance(result, dict):
                last_reason = "invalid register result"
                continue

            if result.get("status") == "success":
                tokens = result.get("tokens", {}) if isinstance(result.get("tokens"), dict) else {}
                access_token = self._resolve_access_token(tokens)
                if access_token:
                    if attempt == 0:
                        self.log("  first-attempt token hit")
                    else:
                        self.log(f"  token hit on retry {attempt}/{self._max_retries}")
                else:
                    self.log(
                        "  signup accepted without JWT cookie; "
                        "activation + cookie OAuth will continue outside browser"
                    )
                return result

            last_reason = str(result.get("error") or "signup failed")
            if attempt < self._max_retries:
                self.log(f"  retry reason: {last_reason}")
                self.log("  Signup not accepted, retrying...")
            else:
                self.log(f"  WARNING: Signup failed after {self._max_retries + 1} attempts")
                self.log(f"  final failure reason: {last_reason}")

        return {
            "email": email,
            "password": password,
            "full_name": full_name,
            "tokens": {},
            "cookies": {},
            "status": "failed",
            "error": last_reason,
        }

    def _signup_response_accepted(self) -> bool:
        entry = self._response_store.get("signup")
        if not isinstance(entry, dict):
            return False
        if self._is_aliyun_waf_signup_response(entry):
            return False
        status = int(entry.get("status") or 0)
        if status and status < 400:
            return True
        payload = entry.get("json")
        if isinstance(payload, dict):
            # Successful signup payloads often include token/user fields.
            if any(payload.get(key) for key in ("token", "id", "email", "user")):
                return True
        return False

    def _looks_like_signup_pending_activation(self, page: Page) -> bool:
        """qwen2api treats "no captcha after submit" as signup accepted."""
        if self._has_aliyun_waf_challenge(page):
            return False
        body = self._safe_body_text(page, limit=1000).lower()
        hard_fail_tokens = (
            "already registered",
            "already exists",
            "已被注册",
            "已注册",
            "invalid email",
            "邮箱格式",
            "password is too",
            "密码过",
            "too many requests",
            "请求过于频繁",
        )
        if any(token in body for token in hard_fail_tokens):
            return False
        soft_ok_tokens = (
            "激活",
            "activate",
            "verification email",
            "验证邮件",
            "check your email",
            "查收",
            "已发送",
            "sent",
        )
        if any(token in body for token in soft_ok_tokens):
            return True
        if self._signup_response_accepted():
            return True
        # Leave the pure register form without an error banner.
        url = str(getattr(page, "url", "") or "")
        if "/auth" in url and "mode=register" in url:
            return False
        return True

    def _try_register(self, page: Page, email: str, password: str, full_name: str) -> dict:
        """One attempt at registration. Returns dict with tokens/cookies."""
        try:
            self._response_store.clear()

            # Step 1: navigate
            page.goto(f"{QWEN_AUTH_URL}?mode=register", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(random.randint(800, 1200))

            # Step 2: fill full name (qwen2api uses placeholder*=名称)
            name_input = self._find_input(
                page,
                selectors=[
                    'input[name="username"]',
                    'input[placeholder*="名称"]',
                    'input[placeholder*="name" i]',
                    'input[placeholder*="Name" i]',
                ],
            )
            name_input.click()
            page.wait_for_timeout(random.randint(100, 250))
            name_input.fill(full_name)
            page.wait_for_timeout(random.randint(150, 300))

            # Step 3: fill email (qwen2api uses placeholder*=邮箱)
            email_input = self._find_input(
                page,
                selectors=[
                    'input[type="email"]',
                    'input[autocomplete="email"]',
                    'input[name="email"]',
                    'input[placeholder*="邮箱"]',
                    'input[placeholder*="email" i]',
                ],
            )
            email_input.click()
            page.wait_for_timeout(random.randint(100, 250))
            email_input.fill(email)
            page.wait_for_timeout(random.randint(150, 300))

            # Step 4/5: fill password + confirm (prefer dual password inputs like qwen2api)
            filled_password_pair = False
            try:
                password_inputs = page.locator('input[type="password"]')
                count = int(password_inputs.count() or 0)
                if count >= 2:
                    password_inputs.nth(0).fill(password)
                    page.wait_for_timeout(random.randint(120, 250))
                    password_inputs.nth(1).fill(password)
                    filled_password_pair = True
                elif count == 1:
                    password_inputs.nth(0).fill(password)
            except Exception:
                filled_password_pair = False

            if not filled_password_pair:
                pw_input = self._find_input(
                    page,
                    selectors=[
                        'input[name="password"]',
                        'input[placeholder*="password" i]',
                        'input[placeholder*="密码"]',
                    ],
                )
                pw_input.click()
                page.wait_for_timeout(100)
                pw_input.fill(password)
                page.wait_for_timeout(100)

                try:
                    cpw_input = self._find_input(
                        page,
                        selectors=[
                            'input[name="checkPassword"]',
                            'input[name="confirmPassword"]',
                            'input[name="confirm_password"]',
                            'input[placeholder*="确认"]',
                            'input[placeholder*="confirm" i]',
                        ],
                    )
                    cpw_input.click()
                    page.wait_for_timeout(100)
                    cpw_input.fill(password)
                    page.wait_for_timeout(100)
                except Exception:
                    pass

            # Step 6: accept terms
            page.wait_for_timeout(300)
            try:
                checkbox = page.locator('input[type="checkbox"]').first
                if checkbox.count() > 0 and checkbox.is_visible():
                    checkbox.click()
                    page.wait_for_timeout(200)
            except Exception:
                checkbox = page.query_selector('input[type="checkbox"]')
                if checkbox and checkbox.is_visible():
                    checkbox.click()
                    page.wait_for_timeout(200)

            # Step 7: submit (qwen2api: click 创建账号 then watch captcha ~5s)
            submit_btn = self._find_submit(page)
            submit_btn.click()
            if self.captcha_mode == QWEN_CAPTCHA_MODE_MANUAL:
                post_timeout = 180.0
            elif self.captcha_mode == QWEN_CAPTCHA_MODE_SOLVE:
                post_timeout = 35.0
            else:
                post_timeout = 12.0
            tokens = self._await_post_submit_tokens(page, timeout_seconds=post_timeout)
            cookies = self._extract_cookie_dict(page)

            # Step 8: extract JWT token from "token" cookie / storage
            self.log(f"  Current URL: {page.url}")
            self.log(f"  Tokens found: {list(tokens.keys()) if tokens else 'none'}")
            self.log(f"  Cookies captured: {len(cookies)}")

            # qwen2api discard: captcha → drop this email/attempt (no solver).
            # manual mode already waited for human; if captcha still up → timeout fail.
            if (
                self.captcha_mode == QWEN_CAPTCHA_MODE_DISCARD
                and self._has_aliyun_waf_challenge(page)
                and not self._resolve_access_token(tokens)
            ):
                self.log("  ⚠️ 需打码，此邮箱丢弃 (qwen2api captcha_discard)")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "cookies": cookies,
                    "status": "failed",
                    "error": QWEN_CAPTCHA_DISCARD_ERROR,
                    "captcha_discard": True,
                }
            if (
                self.captcha_mode == QWEN_CAPTCHA_MODE_MANUAL
                and self._has_aliyun_waf_challenge(page)
                and not self._resolve_access_token(tokens)
            ):
                self.log("  手动验证码超时，滑块仍未通过")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "cookies": cookies,
                    "status": "failed",
                    "error": "manual_captcha_timeout",
                }

            if self._resolve_access_token(tokens):
                # Prefer same-session cookie OAuth (qwen2api); fall back to browser device page.
                oauth_data = obtain_qwen_oauth_tokens_with_cookies(
                    cookies,
                    email=email,
                    log_fn=self.log,
                    poll_timeout_seconds=20,
                )
                if not oauth_data:
                    oauth_data = obtain_qwen_oauth_tokens_from_logged_in_page(
                        page,
                        log_fn=self.log,
                        poll_timeout_seconds=20,
                    )
                if oauth_data:
                    tokens.update(oauth_data)
                    self.log("  OAuth refresh_token acquired")
                else:
                    self.log("  OAuth refresh_token not acquired (continue; retry after activation)")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "cookies": cookies,
                    "status": "success",
                    "pending_activation": False,
                }

            # qwen2api: signup can succeed without JWT until the activation mail is clicked.
            if self._looks_like_signup_pending_activation(page):
                self.log("  Signup accepted pending activation email")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "cookies": cookies,
                    "status": "success",
                    "pending_activation": True,
                }

            return {
                "email": email,
                "password": password,
                "full_name": full_name,
                "tokens": tokens,
                "cookies": cookies,
                "status": "failed",
                "error": self._build_post_submit_failure_reason(page),
            }

        except Exception as e:
            err = str(e)
            self.log(f"  Error: {err}")

            # Fallback: even when selector steps fail, page may already have token/cookies.
            tokens = {}
            cookies: dict[str, str] = {}
            try:
                tokens = self._extract_tokens(page)
                cookies = self._extract_cookie_dict(page)
                self.log(
                    f"  Fallback token check after error: "
                    f"{list(tokens.keys()) if tokens else 'none'}"
                )
            except Exception:
                tokens = {}
                cookies = {}

            if self._resolve_access_token(tokens):
                self.log("  token recovered by fallback extraction")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "cookies": cookies,
                    "status": "success",
                    "pending_activation": False,
                }

            pending_ok = False
            try:
                pending_ok = bool(cookies) and self._looks_like_signup_pending_activation(page)
            except Exception:
                pending_ok = bool(cookies) and self._signup_response_accepted()
            if pending_ok:
                self.log("  signup recovered as pending-activation by fallback extraction")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "cookies": cookies,
                    "status": "success",
                    "pending_activation": True,
                }

            return {
                "email": email,
                "password": password,
                "full_name": full_name,
                "tokens": tokens,
                "cookies": cookies,
                "status": "failed",
                "error": err if tokens else self._build_post_submit_failure_reason(page),
            }

    # ---- helpers ----

    @staticmethod
    def _find_input(page, selectors: list):
        """Try multiple selectors until one returns a visible input."""
        for sel in selectors:
            try:
                el = page.wait_for_selector(sel, timeout=5000)
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        el = page.wait_for_selector("input", timeout=10000)
        if el and el.is_visible():
            return el
        raise RuntimeError("Could not find any visible input field on the page")

    @staticmethod
    def _find_submit(page):
        """Find and return submit/continue button."""
        selectors = [
            'button[type="submit"]',
            'button:has-text("创建账号")',
            'button:has-text("Create Account")',
            'button:has-text("Register")',
            'button:has-text("Sign up")',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("注册")',
        ]
        for sel in selectors:
            try:
                el = page.wait_for_selector(sel, timeout=5000)
                if el and el.is_visible() and el.is_enabled():
                    return el
            except Exception:
                continue
        raise RuntimeError("Could not find submit button")

    @staticmethod
    def _extract_cookie_dict(page) -> dict[str, str]:
        """Return all cookies from the browser context as a plain dict."""
        cookies: dict[str, str] = {}
        try:
            raw_cookies = []
            context = getattr(page, "context", None)
            if context is not None and hasattr(context, "cookies"):
                try:
                    raw_cookies = context.cookies("https://chat.qwen.ai") or []
                except Exception:
                    raw_cookies = context.cookies() or []
            for cookie in raw_cookies or []:
                name = str((cookie or {}).get("name") or "").strip()
                value = str((cookie or {}).get("value") or "").strip()
                if name and value:
                    cookies[name] = value
        except Exception:
            return cookies
        return cookies

    @staticmethod
    def _extract_tokens(page) -> dict:
        """Extract auth tokens from localStorage, sessionStorage, and cookies."""
        tokens = {}

        try:
            storage = page.evaluate("() => JSON.stringify(localStorage)")
            data = json.loads(storage)
            for key, value in data.items():
                kl = key.lower()
                if any(kw in kl for kw in ["token", "auth", "credential", "session", "access", "refresh"]):
                    if value and len(value) > 10:
                        tokens[key] = value
        except Exception:
            pass

        try:
            session = page.evaluate("() => JSON.stringify(sessionStorage)")
            data = json.loads(session)
            for key, value in data.items():
                kl = key.lower()
                if any(kw in kl for kw in ["token", "auth", "credential"]):
                    if value and len(value) > 10:
                        tokens[f"session:{key}"] = value
        except Exception:
            pass

        try:
            for cookie in page.context.cookies():
                cl = cookie["name"].lower()
                if any(kw in cl for kw in ["token", "auth", "session"]):
                    tokens[f'cookie:{cookie["name"]}'] = cookie["value"]
        except Exception:
            pass

        return tokens
