"""DeepSeek protocol registration/reset helpers."""

from __future__ import annotations

import base64
import json
import random
import string
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from playwright.sync_api import sync_playwright

from core.http_client import HTTPClient, RequestConfig
from core.proxy_utils import build_playwright_proxy_config


DEEPSEEK_BASE_URL = "https://chat.deepseek.com"
DEEPSEEK_USERS_API = f"{DEEPSEEK_BASE_URL}/api/v0/users"
DEEPSEEK_APP_VERSION = "20241129.1"
DEEPSEEK_CLIENT_VERSION = "2.0.0"
DEEPSEEK_DEFAULT_UI_LOCALE = "ja-JP"
DEEPSEEK_DEFAULT_REGION = "US"
DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS = "32400"
DEEPSEEK_DEFAULT_TIMEZONE_ID = "Asia/Tokyo"
DEEPSEEK_DEFAULT_POW_WORKER_URL = (
    "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
)
DEEPSEEK_SIGN_UP_URL = (
    f"{DEEPSEEK_BASE_URL}/sign_up?locale={DEEPSEEK_DEFAULT_UI_LOCALE}"
)
DEEPSEEK_SIGN_IN_URL = (
    f"{DEEPSEEK_BASE_URL}/sign_in?locale={DEEPSEEK_DEFAULT_UI_LOCALE}"
)
DEEPSEEK_FORGOT_PASSWORD_URL = (
    f"{DEEPSEEK_BASE_URL}/forgot_password?locale={DEEPSEEK_DEFAULT_UI_LOCALE}"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
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
    separator = "&" if "?" in path else "?"
    return f"{DEEPSEEK_BASE_URL}{path}{separator}locale={ui_locale}"


def extract_deepseek_client_locale(ui_locale: str) -> str:
    value = str(ui_locale or "").strip()
    if not value:
        return "ja"
    return value.split("-", 1)[0].strip() or "ja"


def build_deepseek_accept_language(ui_locale: str) -> str:
    client_locale = extract_deepseek_client_locale(ui_locale)
    return f"{ui_locale},{client_locale};q=0.9,en;q=0.8"


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


def _launch_deepseek_browser(playwright, *, headless: bool, proxy: str | None = None):
    proxy_cfg = build_playwright_proxy_config(proxy) if proxy else None
    launch_attempts = [{"channel": "msedge"}, {}]
    last_error: Exception | None = None
    for extra in launch_attempts:
        launch_kwargs: dict[str, Any] = {"headless": headless, **extra}
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"DeepSeek 浏览器启动失败: {last_error}") from last_error


def _configure_deepseek_sign_up_page(page, *, ui_locale: str) -> None:
    page.add_init_script(
        f"""() => {{
            try {{
                Object.defineProperty(navigator, 'language', {{ get: () => {json.dumps(ui_locale)} }});
                Object.defineProperty(navigator, 'languages', {{ get: () => [{json.dumps(ui_locale)}, 'en'] }});
                localStorage.setItem('webLocalePreference', {json.dumps(ui_locale.replace('-', '_'))});
                localStorage.setItem('webLocale', {json.dumps(ui_locale.replace('-', '_'))});
            }} catch (err) {{}}
        }}"""
    )


def _open_deepseek_sign_up_browser_page(
    playwright,
    *,
    proxy: str | None,
    ui_locale: str,
    headless: bool,
):
    sign_up_url = build_deepseek_page_url("/sign_up", ui_locale)
    accept_language = build_deepseek_accept_language(ui_locale)
    browser = _launch_deepseek_browser(
        playwright,
        headless=headless,
        proxy=proxy,
    )
    context = browser.new_context(
        locale=ui_locale,
        user_agent=USER_AGENT,
        timezone_id=DEEPSEEK_DEFAULT_TIMEZONE_ID,
        viewport={"width": 1440, "height": 1080},
    )
    context.set_extra_http_headers({"Accept-Language": accept_language})
    page = context.new_page()
    _configure_deepseek_sign_up_page(page, ui_locale=ui_locale)
    page.goto(sign_up_url, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(5000)
    _accept_deepseek_cookie_banner(page)
    return browser, context, page, sign_up_url


def ensure_deepseek_email_sign_up_available_via_browser(
    *,
    proxy: str | None = None,
    ui_locale: str = DEEPSEEK_DEFAULT_UI_LOCALE,
    headless: bool = True,
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
            )
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
    for label in ("必要なクッキーのみ", "すべてのCookieを受け入れる"):
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
                return !email && passwords.length >= 2 && !!sendButton && (phoneOnlyCopy || phonePlaceholder || bodyText.includes('+86'));
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
            browser, context, page, sign_up_url = _open_deepseek_sign_up_browser_page(
                p,
                proxy=proxy,
                ui_locale=ui_locale,
                headless=headless,
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

            with page.expect_response(
                lambda resp: resp.request.method == "POST"
                and "/api/v0/users/create_email_verification_code" in resp.url,
                timeout=30000,
            ) as send_response_info:
                send_code_button.click(timeout=10000)
            send_response = send_response_info.value
            sent_at = time.time()
            send_data = _parse_deepseek_playwright_json_response(
                send_response,
                stage="浏览器发码",
            )
            final_state["send_code_response"] = send_data
            inner = send_data.get("data", {})
            if inner.get("biz_code") not in (0, "0"):
                raise RuntimeError(
                    f"DeepSeek 浏览器发码失败: {inner}"
                )
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
            with page.expect_response(
                lambda resp: resp.request.method == "POST"
                and "/api/v0/users/register" in resp.url,
                timeout=30000,
            ) as register_response_info:
                submit_button.click(timeout=10000)
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

            deadline = time.time() + 45
            while time.time() < deadline:
                current_url = str(page.url or "")
                if current_url.rstrip("/") == DEEPSEEK_BASE_URL:
                    final_state["final_url"] = current_url
                    return final_state
                page.wait_for_timeout(1000)

            body_snippet = str(page.locator("body").inner_text(timeout=3000) or "")[:2000]
            final_state["final_url"] = page.url
            final_state["body_snippet"] = body_snippet
            raise RuntimeError(
                "DeepSeek 浏览器注册未完成: "
                f"state={_collect_deepseek_form_state(page)}"
            )
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
                    launch_kwargs: dict[str, Any] = {"headless": True}
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
        self.ui_locale = str(ui_locale or DEEPSEEK_DEFAULT_UI_LOCALE).strip() or DEEPSEEK_DEFAULT_UI_LOCALE
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
        challenge = (
            data.get("data", {})
            .get("biz_data", {})
            .get("guest_challenge", {})
        )
        if not challenge:
            raise RuntimeError(f"DeepSeek guest challenge 响应异常: {data}")
        return challenge

    def send_email_code(
        self,
        *,
        email: str,
        scenario: str,
        referer: str | None = None,
    ) -> dict[str, Any]:
        target_referer = referer or self.sign_up_url
        self._ensure_settings(referer=target_referer)
        payload = {
            "email": email,
            "turnstile_token": "",
            "locale": self.locale,
            "device_id": self.device_id,
            "scenario": scenario,
        }
        return self._post(
            "/create_email_verification_code",
            payload,
            referer=target_referer,
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
