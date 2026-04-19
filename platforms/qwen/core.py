"""Qwen (chat.qwen.ai) registration protocol via Playwright.

Qwen web registration flow (verified via debugging):
    1. Open https://chat.qwen.ai/auth?mode=register
    2. Fill Full Name + Email + Password + Confirm Password
    3. Accept Terms checkbox
    4. Submit form
    5. JWT token issued immediately in "token" cookie
    6. Page shows "pending activation" — email verification is deferred
    7. Activation email sent to user with link:
       https://chat.qwen.ai/api/v1/auths/activate?id=UUID&token=HASH
    8. Calling activation URL activates the account

Note: No OTP, no captcha. Token available immediately; activation is
deferred and can be done by visiting the activation URL from email.
"""

import json
import base64
import hashlib
import random
import re
import secrets
import string
import time
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import Page

QWEN_AUTH_URL = "https://chat.qwen.ai/auth"
QWEN_ACTIVATE_URL = "https://chat.qwen.ai/api/v1/auths/activate"
QWEN_OAUTH_DEVICE_CODE_URL = "https://chat.qwen.ai/api/v1/oauth2/device/code"
QWEN_OAUTH_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_OAUTH_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_OAUTH_SCOPE = "openid profile email model.completion"

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


def call_activation_api(activation_url: str, user_agent: str = UA) -> dict:
    """Call the Qwen activation API directly."""
    parsed = urlparse(activation_url)
    params = parse_qs(parsed.query)
    act_id = params.get("id", [None])[0]
    act_token = params.get("token", [None])[0]

    if not act_id or not act_token:
        return {"ok": False, "error": "Missing id/token params"}

    url = f"{QWEN_ACTIVATE_URL}?id={act_id}&token={act_token}"
    try:
        r = requests.get(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=15,
            allow_redirects=True,
        )
        return {
            "ok": r.status_code in (200, 302),
            "status_code": r.status_code,
            "final_url": r.url,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
        if text:
            try:
                parsed = resp.json()
            except Exception:
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

    def __init__(self, executor, log_fn: Callable = print):
        """executor must be a PlaywrightExecutor (headless or headed)."""
        self.executor = executor
        self.log = log_fn
        self._max_retries = 2

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
            full_name = email.split("@")[0].replace(".", " ").replace("_", " ").title()

        self.log(f"Qwen registration — email: {email}, name: {full_name}")

        page = self.executor.page
        if page is None:
            raise RuntimeError(
                "Qwen requires a browser executor (headless/headed Playwright). "
                "Please select 'headless' or 'headed' executor."
            )

        last_reason = "no token"
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                self.log(f"  Retry {attempt}/{self._max_retries}...")
                time.sleep(5)

            result = self._try_register(page, email, password, full_name)
            tokens = result.get("tokens", {}) if isinstance(result, dict) else {}
            access_token = self._resolve_access_token(tokens)
            if access_token:
                if attempt == 0:
                    self.log("  first-attempt token hit")
                else:
                    self.log(f"  token hit on retry {attempt}/{self._max_retries}")
                return result

            last_reason = str(result.get("error") or "no token")
            if attempt < self._max_retries:
                self.log(f"  retry reason: {last_reason}")
                self.log("  No token, retrying...")
            else:
                self.log(f"  WARNING: No auth token after {self._max_retries + 1} attempts")
                self.log(f"  final failure reason(no token): {last_reason}")

        return {
            "email": email,
            "password": password,
            "full_name": full_name,
            "tokens": {},
            "status": "failed",
            "error": last_reason,
        }

    def _try_register(self, page: Page, email: str, password: str, full_name: str) -> dict:
        """One attempt at registration. Returns dict with tokens."""
        try:
            # Step 1: navigate
            page.goto(f"{QWEN_AUTH_URL}?mode=register", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)

            # Step 2: fill full name
            name_input = self._find_input(
                page,
                selectors=[
                    'input[name="username"]',
                    'input[placeholder*="name" i]',
                    'input[placeholder*="Name" i]',
                ],
            )
            name_input.click()
            page.wait_for_timeout(100)
            name_input.fill(full_name)
            page.wait_for_timeout(100)

            # Step 3: fill email
            email_input = self._find_input(
                page,
                selectors=[
                    'input[type="email"]',
                    'input[autocomplete="email"]',
                    'input[name="email"]',
                ],
            )
            email_input.click()
            page.wait_for_timeout(100)
            email_input.fill(email)
            page.wait_for_timeout(100)

            # Step 4: fill password
            pw_input = self._find_input(
                page,
                selectors=[
                    'input[name="password"]',
                    'input[placeholder*="password" i]',
                ],
            )
            pw_input.click()
            page.wait_for_timeout(100)
            pw_input.fill(password)
            page.wait_for_timeout(100)

            # Step 5: fill confirm password
            cpw_input = self._find_input(
                page,
                selectors=[
                    'input[name="checkPassword"]',
                    'input[name="confirmPassword"]',
                    'input[name="confirm_password"]',
                ],
            )
            cpw_input.click()
            page.wait_for_timeout(100)
            cpw_input.fill(password)
            page.wait_for_timeout(100)

            # Step 6: accept terms
            page.wait_for_timeout(300)
            checkbox = page.query_selector('input[type="checkbox"]')
            if checkbox and checkbox.is_visible():
                checkbox.click()
                page.wait_for_timeout(200)

            # Step 7: submit
            submit_btn = self._find_submit(page)
            submit_btn.click()
            page.wait_for_timeout(4000)

            # Step 8: extract JWT token from "token" cookie
            tokens = self._extract_tokens(page)
            self.log(f"  Current URL: {page.url}")
            self.log(f"  Tokens found: {list(tokens.keys()) if tokens else 'none'}")

            if self._resolve_access_token(tokens):
                oauth_data = obtain_qwen_oauth_tokens_from_logged_in_page(
                    page,
                    log_fn=self.log,
                    poll_timeout_seconds=20,
                )
                if oauth_data:
                    tokens.update(oauth_data)
                    self.log("  OAuth refresh_token acquired")
                else:
                    self.log("  OAuth refresh_token not acquired (continue with web token)")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "status": "success",
                }

            return {
                "email": email,
                "password": password,
                "full_name": full_name,
                "tokens": tokens,
                "status": "failed",
                "error": "no token after submit",
            }

        except Exception as e:
            err = str(e)
            self.log(f"  Error: {err}")

            # Fallback: even when selector steps fail, page may already have token.
            tokens = {}
            try:
                tokens = self._extract_tokens(page)
                self.log(
                    f"  Fallback token check after error: "
                    f"{list(tokens.keys()) if tokens else 'none'}"
                )
            except Exception:
                tokens = {}

            if self._resolve_access_token(tokens):
                self.log("  token recovered by fallback extraction")
                return {
                    "email": email,
                    "password": password,
                    "full_name": full_name,
                    "tokens": tokens,
                    "status": "success",
                }

            return {
                "email": email,
                "password": password,
                "full_name": full_name,
                "tokens": tokens,
                "status": "failed",
                "error": err,
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
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        raise RuntimeError("Could not find submit button")

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
