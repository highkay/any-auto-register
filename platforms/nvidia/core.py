"""NVIDIA 浏览器注册与 API key 生成核心逻辑。"""

from __future__ import annotations

import base64
import inspect
import json
import random
import re
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from core.base_captcha import BaseCaptcha
from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
    with_chrome_executable,
)
from core.proxy_utils import build_playwright_proxy_config


BUILD_ENTRY_URL = "https://build.nvidia.com/?modal=signin"
BUILD_HOME_URL = "https://build.nvidia.com/"
NVIDIA_KEY_URL = (
    "https://api.ngc.nvidia.com/v3/orgs/{org_name}/keys/type/AI_PLAYGROUNDS_KEY"
)
NVIDIA_USER_CONTEXT_URL = "https://api.ngc.nvidia.com/user-context"
NVIDIA_ME_URL = "https://api.ngc.nvidia.com/v2/users/me"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
_HCAPTCHA_IFRAME_SELECTOR = ",".join(
    [
        'iframe[src*="hcaptcha.com"][src*="frame=checkbox"]',
        'iframe[src*="frame=checkbox"]',
        'iframe[title*="checkbox"]',
        'iframe[title*="hCaptcha"]',
        'iframe[title*="复选框"]',
        'iframe[title*="安全挑战"]',
    ]
)
_HCAPTCHA_TOKEN_MIN_LENGTH = 20
_HCAPTCHA_CHECKED_SENTINEL = "__HCAPTCHA_CHECKED__"
_HCAPTCHA_CHALLENGE_IFRAME_SELECTOR = ",".join(
    [
        'iframe[src*="hcaptcha.com"][src*="frame=challenge"]',
        'iframe[src*="frame=challenge"]',
        'iframe[title*="challenge"]',
        'iframe[title*="挑战"]',
    ]
)
_CAPTCHA_RECOGNITION_WIDTH = 1440.0
_CAPTCHA_RECOGNITION_HEIGHT = 900.0
_HCAPTCHA_TASK_SELECTOR = (
    '.task, [role="group"] button, [role="group"] [role="button"], '
    'button[aria-label*="挑战图片"], button[aria-label*="challenge image"]'
)
_HCAPTCHA_MAX_RETRY_SHELL_HITS = 6
_HCAPTCHA_MAX_EMPTY_CLICK_HITS = 4
_HCAPTCHA_MAX_VISUAL_FAILURE_HITS = 8
_HCAPTCHA_MAX_UNRESOLVED_PROMPT_HITS = 5
_HCAPTCHA_MAX_MISSING_SUBMIT_HITS = 4
_HCAPTCHA_MAX_REPEAT_VISUAL_SIGNATURE_HITS = 3
_HCAPTCHA_DIRECT_TIMEOUT_SECONDS = 45.0
_HCAPTCHA_DIRECT_POLL_INTERVAL_SECONDS = 2.0
_HCAPTCHA_DIRECT_REQUEST_TIMEOUT_SECONDS = 10.0
_HCAPTCHA_VISUAL_RECOGNITION_TIMEOUT_SECONDS = 360.0
_HCAPTCHA_VISUAL_RECOGNITION_POLL_INTERVAL_SECONDS = 3.0
_HCAPTCHA_VISUAL_RECOGNITION_REQUEST_TIMEOUT_SECONDS = 30.0
_HCAPTCHA_VISUAL_SIGNATURE_BUCKET_PX = 50.0


def _safe_body_text(page, limit: int = 600) -> str:
    try:
        text = str(page.locator("body").inner_text(timeout=1500) or "").strip()
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _pick_first_text(source: Any, *keys: str) -> str:
    if not isinstance(source, dict):
        return ""
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        text = value.strip() if isinstance(value, str) else str(value).strip()
        if text:
            return text
    return ""


def _expiry_after_years(years: int = 100) -> str:
    expires_at = datetime.now(timezone.utc) + timedelta(days=365 * years)
    return expires_at.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_nvidia_account_system_url(url: str) -> bool:
    value = str(url or "").strip().lower()
    if not value:
        return False
    return (
        "login.nvgs.nvidia." in value
        or "accounts.nvgs.nvidia." in value
        or "api.ngc.nvidia.com/login" in value
        or "create-account" in value
    )


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


class NvidiaRegister:
    def __init__(
        self,
        *,
        captcha_solver=None,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        headless: bool = True,
        task_control=None,
    ):
        self.captcha_solver = captcha_solver
        self.proxy = proxy
        self.log = log_fn
        self.headless = headless
        self._task_control = task_control

    def _checkpoint(self) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint()

    def _solver_optional_kwargs(
        self,
        method: Callable[..., Any],
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
        request_timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            return {}

        params = signature.parameters
        accepts_var_kwargs = any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in params.values()
        )
        kwargs: dict[str, Any] = {}
        if "timeout_seconds" in params or accepts_var_kwargs:
            kwargs["timeout_seconds"] = timeout_seconds
        if "poll_interval_seconds" in params or accepts_var_kwargs:
            kwargs["poll_interval_seconds"] = poll_interval_seconds
        if "request_timeout_seconds" in params or accepts_var_kwargs:
            kwargs["request_timeout_seconds"] = request_timeout_seconds
        if "interrupt_checker" in params or accepts_var_kwargs:
            kwargs["interrupt_checker"] = self._checkpoint
        return kwargs

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

    def _launch_browser(self):
        from patchright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        headless, reason = resolve_browser_headless(
            self.headless, default_headless=True
        )
        ensure_browser_display_available(headless)
        self.log(f"浏览器模式: {'headless' if headless else 'headed'} ({reason})")

        launch_kwargs: dict[str, Any] = with_chrome_executable(headless=headless)
        if self.proxy:
            proxy_cfg = build_playwright_proxy_config(self.proxy)
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception:
            # Fall back to Edge only when the preferred Chrome binary fails.
            edge_kwargs = dict(launch_kwargs)
            edge_kwargs.pop("executable_path", None)
            edge_kwargs["channel"] = "msedge"
            try:
                browser = playwright.chromium.launch(**edge_kwargs)
            except Exception:
                edge_kwargs.pop("channel", None)
                browser = playwright.chromium.launch(**edge_kwargs)
        return playwright, browser

    def _click_text_button(self, page, labels: list[str]) -> bool:
        clicked = page.evaluate(
            """(labels) => {
                const targets = labels.map((item) => String(item || '').trim().toLowerCase()).filter(Boolean);
                const nodes = [...document.querySelectorAll('button, a, [role="button"]')];
                for (const node of nodes) {
                    const text = String(node.innerText || node.textContent || '').trim().toLowerCase();
                    if (!text) continue;
                    if (targets.some((target) => text === target || text.includes(target))) {
                        node.click();
                        return true;
                    }
                }
                return false;
            }""",
            labels,
        )
        return bool(clicked)

    def _dismiss_cookie_banner(self, page) -> None:
        labels = [
            "accept",
            "i accept",
            "accept all",
            "agree",
            "allow all",
            "got it",
            "close",
        ]
        for _ in range(3):
            if self._click_text_button(page, labels):
                page.wait_for_timeout(1200)

    def _click_login_entry(self, page) -> bool:
        for selector in (
            'button[data-nvtrack-nav-object="login-button"]',
            'button[data-nvtrack-nav-object-label="Login"]',
        ):
            try:
                page.locator(selector).first.click(timeout=3000, force=True)
                return True
            except Exception:
                continue
        candidates = [
            ("button", "Login"),
            ("button", "Log in"),
            ("button", "Sign in"),
            ("link", "Login"),
            ("link", "Log in"),
            ("link", "Sign in"),
        ]
        for role, name in candidates:
            try:
                page.get_by_role(role, name=name).first.click(timeout=3000)
                return True
            except Exception:
                continue
        return self._click_text_button(page, ["login", "log in", "sign in"])

    def _open_entry_modal(self, page) -> None:
        self.log("Step1: 打开 NVIDIA 注册入口...")
        page.goto(BUILD_ENTRY_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self._dismiss_cookie_banner(page)

        def _email_ready() -> bool:
            locator = page.locator('input[name="email"]')
            return locator.count() > 0 and locator.first.is_visible()

        for attempt in range(4):
            if _email_ready():
                return
            self._click_login_entry(page)
            page.wait_for_timeout(1500)
            if _email_ready():
                return
            if attempt == 1:
                page.goto(BUILD_ENTRY_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                self._dismiss_cookie_banner(page)

        raise RuntimeError(
            f"未找到 NVIDIA 登录邮箱输入框，url={page.url}, body={_safe_body_text(page)}"
        )

    def _submit_email(self, page, email: str) -> None:
        self.log(f"Step2: 提交邮箱 {email} ...")

        def _redirected() -> bool:
            return _is_nvidia_account_system_url(page.url)

        last_error = None
        for submit_attempt in range(1, 4):
            if _redirected():
                return
            email_input = page.locator('input[name="email"]').first
            try:
                email_input.wait_for(state="visible", timeout=15000)
                email_input.fill(email)
            except Exception as exc:
                if _redirected():
                    return
                last_error = exc
                if submit_attempt >= 3:
                    raise
                self.log(
                    f"Step2: 邮箱输入框暂不可用，准备重试 {submit_attempt + 1}/3"
                )
                page.wait_for_timeout(1000)
                continue
            clicked = False
            try:
                page.get_by_role("button", name="Next").last.click(timeout=3000)
                clicked = True
            except Exception as exc:
                last_error = exc
                clicked = self._click_text_button(page, ["next"])
                if not clicked:
                    try:
                        email_input.press("Enter")
                        clicked = True
                    except Exception:
                        clicked = False
            if not clicked:
                raise RuntimeError("未找到 NVIDIA 登录页 Next 按钮")

            page.wait_for_timeout(1500)
            try:
                self._wait_until(
                    _redirected,
                    timeout=10,
                    interval=0.5,
                    desc=f"提交 NVIDIA 邮箱后未进入账户系统，url={page.url}",
                    page=page,
                )
                return
            except TimeoutError as exc:
                last_error = exc
                if submit_attempt >= 3:
                    raise
                self.log(
                    f"Step2: 邮箱提交后仍停留在入口页，准备重试 {submit_attempt + 1}/3"
                )

        raise last_error or RuntimeError(
            f"提交 NVIDIA 邮箱后未进入账户系统，url={page.url}"
        )

    def _wait_for_create_account(self, page) -> None:
        self.log("Step3: 等待 Create Account 页面...")

        def _ready() -> bool:
            url = page.url.lower()
            return (
                "create-account" in url
                and page.locator("#registration_password").count() > 0
            )

        self._wait_until(
            _ready,
            timeout=60,
            interval=1.0,
            desc=f"未进入 NVIDIA Create Account 页面，url={page.url}, body={_safe_body_text(page)}",
            page=page,
        )
        page.wait_for_timeout(1000)

    def _fill_create_account_form(self, page, email: str, password: str) -> None:
        self.log("Step4: 填写 Create Account 表单...")
        try:
            email_input = page.locator("#emailAddress").first
            if email_input.count() > 0:
                email_input.fill(email)
        except Exception:
            pass

        page.locator("#registration_password").first.fill(password)
        page.locator("#registration_passwordConfirm").first.fill(password)

        for selector in (
            "#data_general_agreement-input",
            "#terms_and_conditions-input",
            "#stay_signin_checkbox-input",
        ):
            try:
                checkbox = page.locator(selector).first
                if checkbox.count() > 0 and not checkbox.is_checked():
                    checkbox.check(force=True)
            except Exception:
                pass

    def _extract_hcaptcha_sitekey(self, page) -> str:
        return str(
            page.evaluate(
                """() => {
                    const direct = document.querySelector('[data-sitekey]');
                    if (direct) return direct.getAttribute('data-sitekey') || '';

                    for (const frame of document.querySelectorAll('iframe[src*="hcaptcha.com"], iframe[src*="newassets.hcaptcha.com"]')) {
                        try {
                            const src = frame.getAttribute('src') || '';
                            const parsed = new URL(src, location.href);
                            const key =
                                parsed.searchParams.get('sitekey') ||
                                parsed.searchParams.get('k');
                            if (key) return key;
                            const hashText = String(parsed.hash || '').replace(/^#/, '');
                            if (hashText) {
                                const hashParams = new URLSearchParams(hashText);
                                const hashKey =
                                    hashParams.get('sitekey') ||
                                    hashParams.get('k');
                                if (hashKey) return hashKey;
                            }
                        } catch (_) {}
                    }
                    return '';
                }"""
            )
            or ""
        ).strip()

    def _inject_hcaptcha_token(self, page, token: str) -> bool:
        result = page.evaluate(
            """(token) => {
                const ensured = [];
                const form = document.querySelector('form') || document.body;
                const selectors = [
                    'textarea[name="h-captcha-response"]',
                    'textarea[name="g-recaptcha-response"]',
                    'input[name="h-captcha-response"]',
                    'input[name="g-recaptcha-response"]',
                    'textarea[id*="h-captcha-response"]',
                    'input[id*="h-captcha-response"]',
                ];

                const fields = [];
                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach((node) => fields.push(node));
                }

                if (!fields.length) {
                    for (const name of ['h-captcha-response', 'g-recaptcha-response']) {
                        const area = document.createElement('textarea');
                        area.name = name;
                        area.style.display = 'none';
                        form.appendChild(area);
                        fields.push(area);
                        ensured.push(name);
                    }
                }

                for (const field of fields) {
                    field.value = token;
                    field.innerHTML = token;
                    field.setAttribute('value', token);
                    field.dispatchEvent(new Event('input', { bubbles: true }));
                    field.dispatchEvent(new Event('change', { bubbles: true }));
                }

                const callbacks = [];
                document.querySelectorAll('[data-callback]').forEach((node) => {
                    const name = node.getAttribute('data-callback');
                    if (name && typeof window[name] === 'function') {
                        try {
                            window[name](token);
                            callbacks.push(name);
                        } catch (_) {}
                    }
                });

                return {
                    fieldCount: fields.length,
                    ensured,
                    callbacks,
                };
            }""",
            token,
        )
        return bool(result.get("fieldCount"))

    def _extract_hcaptcha_token(self, page) -> str:
        token = str(
            page.evaluate(
                """() => {
                    const selectors = [
                        '[name="h-captcha-response"]',
                        '[name="g-recaptcha-response"]',
                        'textarea[id*="h-captcha-response"]',
                        'input[id*="h-captcha-response"]',
                    ];
                    for (const selector of selectors) {
                        const field = document.querySelector(selector);
                        if (field && field.value && field.value.length > 20) {
                            return field.value;
                        }
                    }
                    return '';
                }"""
            )
            or ""
        ).strip()
        return token if len(token) > _HCAPTCHA_TOKEN_MIN_LENGTH else ""

    def _click_hcaptcha_button(self, challenge_frame, labels: list[str]) -> bool:
        return bool(
            challenge_frame.evaluate(
                """(labels) => {
                    const targets = labels
                        .map((item) => String(item || '').trim().toLowerCase())
                        .filter(Boolean);
                    const nodes = [
                        ...document.querySelectorAll('[role="button"], button, a'),
                    ];
                    for (const node of nodes) {
                        const text = [
                            node.innerText || '',
                            node.textContent || '',
                            node.getAttribute('aria-label') || '',
                            node.getAttribute('title') || '',
                        ]
                            .map((item) => String(item || '').trim().toLowerCase())
                            .filter(Boolean)
                            .join(' ');
                        if (!text) continue;
                        if (targets.some((target) => text === target || text.includes(target))) {
                            node.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                labels,
            )
        )

    def _find_hcaptcha_challenge_frame(self, page):
        for frame in page.frames:
            url = str(frame.url or "").lower()
            if "hcaptcha" in url and ("frame=challenge" in url or "challenge" in url):
                return frame
        return None

    def _find_hcaptcha_challenge_iframe(self, page):
        try:
            locator = page.locator("iframe")
            count = locator.count()
        except Exception:
            return None
        best = None
        best_area = -1.0
        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                src = str(candidate.get_attribute("src") or "")
                title = str(candidate.get_attribute("title") or "")
            except Exception:
                continue
            haystack = f"{src} {title}".lower()
            title_lower = title.lower()
            if "hcaptcha" not in haystack:
                continue
            if "frame=checkbox" in haystack or "checkbox" in haystack or "复选框" in title_lower:
                continue
            if "frame=challenge" not in haystack and "challenge" not in haystack and "挑战" not in title:
                continue
            try:
                bbox = candidate.bounding_box()
            except Exception:
                bbox = None
            area = float((bbox or {}).get("width") or 0.0) * float((bbox or {}).get("height") or 0.0)
            if best is None or area >= best_area:
                best = candidate
                best_area = area
        if best is not None:
            return best
        return None

    def _hcaptcha_task_locator(self, challenge_frame):
        tasks = challenge_frame.locator(".task")
        try:
            count = tasks.count()
        except Exception:
            count = 0
        if isinstance(count, int) and count > 0:
            return tasks
        return challenge_frame.locator(
            '[role="group"] button, [role="group"] [role="button"], button[aria-label*="挑战图片"], button[aria-label*="challenge image"]'
        )

    def _is_hcaptcha_checkbox_checked(self, page) -> bool:
        try:
            checked = (
                page.frame_locator(_HCAPTCHA_IFRAME_SELECTOR)
                .first.locator("#checkbox")
                .get_attribute("aria-checked")
            )
        except Exception:
            return False
        return str(checked or "").strip().lower() == "true"

    def _capture_hcaptcha_challenge(self, challenge_frame, challenge_iframe=None) -> dict[str, Any]:
        snapshot = challenge_frame.evaluate(
            """() => {
                const prompt = String(
                    document.querySelector('.prompt-text')?.innerText
                    || document.querySelector('.challenge-prompt')?.innerText
                    || ''
                ).trim();
                const tileNodes = Array.from(
                    document.querySelectorAll(
                        '.task, [role="group"] button, [role="group"] [role="button"], button[aria-label*="挑战图片"], button[aria-label*="challenge image"]'
                    )
                );
                const urls = tileNodes
                    .map((node) => {
                        const imageNode = node.querySelector('.image, img, [style*="background-image"]');
                        if (!imageNode) return '';
                        if (imageNode.tagName === 'IMG') {
                            return imageNode.currentSrc || imageNode.src || '';
                        }
                        const bg = getComputedStyle(imageNode).backgroundImage || '';
                        const match = bg.match(/url\\(["']?(.*?)["']?\\)/);
                        return match ? match[1] : '';
                    })
                    .filter(Boolean);
                const buttons = Array.from(document.querySelectorAll('button,[role="button"],a'))
                    .map((node) => [
                        node.innerText || '',
                        node.textContent || '',
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('title') || '',
                    ]
                        .map((item) => String(item || '').trim())
                        .filter(Boolean)
                        .join(' | '))
                    .filter(Boolean);
                const selected = tileNodes
                    .map((node, index) =>
                        node.className.includes('selected')
                        || node.className.includes('focus')
                        || node.getAttribute('aria-pressed') === 'true'
                        || node.getAttribute('aria-selected') === 'true'
                        || node.getAttribute('aria-current') === 'true'
                        || node.getAttribute('data-selected') === 'true'
                            ? index
                            : null
                    )
                    .filter((value) => value !== null);
                const canvas = document.querySelector('canvas');
                const canvasDataUrl = (() => {
                    try {
                        return canvas ? canvas.toDataURL('image/png') : '';
                    } catch (_) {
                        return '';
                    }
                })();
                return {
                    prompt,
                    urls,
                    body: String(document.body.innerText || '').slice(0, 1200),
                    buttons,
                    selected,
                    canvasDataUrl,
                    tileCount: tileNodes.length,
                };
            }"""
        )
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"NVIDIA hCaptcha challenge 快照异常: {snapshot!r}")
        prompt = _pick_first_text(snapshot, "prompt")
        urls = snapshot.get("urls")
        if not isinstance(urls, list):
            urls = []
        body = _pick_first_text(snapshot, "body")
        buttons = snapshot.get("buttons")
        if not isinstance(buttons, list):
            buttons = []
        selected = snapshot.get("selected")
        if not isinstance(selected, list):
            selected = []
        canvas_data_url = _pick_first_text(snapshot, "canvasDataUrl")
        screenshot_b64 = ""
        if challenge_iframe is not None:
            try:
                screenshot = challenge_iframe.screenshot(type="png")
                if screenshot:
                    screenshot_b64 = base64.b64encode(screenshot).decode()
            except Exception:
                screenshot_b64 = ""
        images_b64 = self._capture_hcaptcha_task_images(
            challenge_frame,
            expected_count=int(snapshot.get("tileCount") or 0),
        )
        return {
            "prompt": prompt,
            "urls": [str(url).strip() for url in urls if str(url).strip()],
            "body": body,
            "buttons": [str(text).strip() for text in buttons if str(text).strip()],
            "selected": {
                int(idx)
                for idx in selected
                if isinstance(idx, int)
            },
            "canvas_b64": canvas_data_url.split(",", 1)[1].strip()
            if canvas_data_url.startswith("data:image")
            else "",
            "screenshot_b64": screenshot_b64,
            "images_b64": images_b64,
            "tile_count": int(snapshot.get("tileCount") or 0),
        }

    def _capture_hcaptcha_task_images(
        self, challenge_frame, *, expected_count: int = 0
    ) -> list[str]:
        tasks = self._hcaptcha_task_locator(challenge_frame)
        try:
            count = tasks.count()
        except Exception:
            return []
        if not isinstance(count, int) or count <= 0:
            return []
        total = count if expected_count <= 0 else min(count, expected_count)
        images_b64: list[str] = []
        for idx in range(total):
            task = tasks.nth(idx)
            try:
                task.wait_for(state="visible", timeout=3000)
                screenshot = task.screenshot(type="png", timeout=5000)
            except Exception:
                return []
            if not screenshot:
                return []
            images_b64.append(base64.b64encode(screenshot).decode())
        return images_b64

    def _is_hcaptcha_retry_shell(self, snapshot: dict[str, Any]) -> bool:
        body = str(snapshot.get("body") or "").strip().lower()
        buttons = [str(text or "").strip().lower() for text in snapshot.get("buttons") or []]
        has_retry_marker = any(
            marker in body for marker in ("请再试一次", "try again", "please try again")
        )
        has_retry_button = any(
            any(
                marker in text
                for marker in (
                    "刷新挑战",
                    "refresh challenge",
                    "refresh",
                    "检查",
                    "check",
                    "verify",
                    "submit",
                    "下一个",
                    "next",
                    "跳过",
                    "skip",
                )
            )
            for text in buttons
        )
        has_real_prompt = bool(str(snapshot.get("prompt") or "").strip())
        has_grid = bool(snapshot.get("urls"))
        has_canvas = bool(snapshot.get("canvas_b64"))
        return has_retry_marker and has_retry_button and not has_real_prompt and not has_grid and not has_canvas

    def _has_hcaptcha_skip_button(self, snapshot: dict[str, Any]) -> bool:
        buttons = [str(text or "").strip().lower() for text in snapshot.get("buttons") or []]
        return any("跳过" in text or "skip" in text for text in buttons)

    def _needs_hcaptcha_visual_recognition(self, snapshot: dict[str, Any]) -> bool:
        prompt = str(snapshot.get("prompt") or "").strip().lower()
        body = str(snapshot.get("body") or "").strip().lower()
        drag_prompt = any(
            marker in f"{prompt} {body}"
            for marker in ("拖", "drag", "匹配位置", "match position")
        )
        has_grid = (
            bool(snapshot.get("urls"))
            or bool(snapshot.get("images_b64"))
            or int(snapshot.get("tile_count") or 0) > 0
        )
        return (
            bool(snapshot.get("prompt"))
            and bool(snapshot.get("screenshot_b64"))
            and (drag_prompt or not has_grid)
            and not self._is_hcaptcha_retry_shell(snapshot)
        )

    def _challenge_iframe_bbox(self, challenge_iframe) -> dict[str, float] | None:
        try:
            bbox = challenge_iframe.bounding_box()
        except Exception:
            return None
        if not isinstance(bbox, dict) or not bbox:
            return None
        return {
            "x": float(bbox["x"]),
            "y": float(bbox["y"]),
            "width": float(bbox["width"]),
            "height": float(bbox["height"]),
        }

    def _map_hcaptcha_point(
        self, bbox: dict[str, float], x: float, y: float
    ) -> tuple[float, float]:
        scale_x = bbox["width"] / _CAPTCHA_RECOGNITION_WIDTH
        scale_y = bbox["height"] / _CAPTCHA_RECOGNITION_HEIGHT
        return bbox["x"] + (x * scale_x), bbox["y"] + (y * scale_y)

    @staticmethod
    def _read_hcaptcha_visual_point(payload: Any) -> tuple[float, float]:
        def _numbers(value: Any) -> list[float]:
            if isinstance(value, (list, tuple)):
                return [float(item) for item in value]
            if value is None:
                return []
            return [float(value)]

        if isinstance(payload, (list, tuple)):
            coords = _numbers(payload)
            if len(coords) >= 2:
                return coords[0], coords[1]
            raise RuntimeError(f"hCaptcha 坐标异常: {payload!r}")

        if not isinstance(payload, dict):
            raise RuntimeError(f"hCaptcha 坐标异常: {payload!r}")

        pair = payload.get("point")
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            return float(pair[0]), float(pair[1])

        xs = _numbers(payload.get("x"))
        ys = _numbers(payload.get("y"))
        if xs and ys:
            return sum(xs) / len(xs), sum(ys) / len(ys)
        if len(xs) >= 2:
            return xs[0], xs[1]
        if len(ys) >= 2:
            return ys[0], ys[1]
        raise RuntimeError(f"hCaptcha 坐标异常: {payload!r}")

    @staticmethod
    def _hcaptcha_visual_action_name(action: dict[str, Any]) -> str:
        return str(
            action.get("action") or action.get("captcha_type") or ""
        ).strip().lower()

    @staticmethod
    def _bucket_hcaptcha_visual_point(point: tuple[float, float]) -> tuple[int, int]:
        bucket = _HCAPTCHA_VISUAL_SIGNATURE_BUCKET_PX
        return (
            int(round(point[0] / bucket) * bucket),
            int(round(point[1] / bucket) * bucket),
        )

    def _hcaptcha_visual_action_signature(
        self, prompt: str, action: dict[str, Any]
    ) -> str:
        action_name = self._hcaptcha_visual_action_name(action)
        if action_name == "click":
            clicks = action.get("clicks")
            if not isinstance(clicks, list):
                return f"{prompt.strip().lower()}::{action_name}::clicks=invalid"
            points: list[tuple[int, int]] = []
            for item in clicks:
                try:
                    points.append(
                        self._bucket_hcaptcha_visual_point(
                            self._read_hcaptcha_visual_point(item)
                        )
                    )
                except Exception:
                    continue
            return f"{prompt.strip().lower()}::{action_name}::clicks={sorted(points)}"
        if action_name == "slide":
            parts: list[str] = []
            for key in ("slider", "gap"):
                value = action.get(key)
                try:
                    parts.append(
                        f"{key}={self._bucket_hcaptcha_visual_point(self._read_hcaptcha_visual_point(value))}"
                    )
                except Exception:
                    parts.append(f"{key}=invalid")
            return f"{prompt.strip().lower()}::{action_name}::" + ",".join(parts)
        if action_name == "drag_match":
            pairs = action.get("pairs")
            if not isinstance(pairs, list):
                return f"{prompt.strip().lower()}::{action_name}::pairs=invalid"
            pair_signatures: list[tuple[tuple[int, int], tuple[int, int]]] = []
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                try:
                    pair_signatures.append(
                        (
                            self._bucket_hcaptcha_visual_point(
                                self._read_hcaptcha_visual_point(pair.get("from"))
                            ),
                            self._bucket_hcaptcha_visual_point(
                                self._read_hcaptcha_visual_point(pair.get("to"))
                            ),
                        )
                    )
                except Exception:
                    continue
            return f"{prompt.strip().lower()}::{action_name}::pairs={pair_signatures}"
        return f"{prompt.strip().lower()}::{action_name}"

    def _hcaptcha_visual_action_summary(self, action: dict[str, Any]) -> str:
        action_name = self._hcaptcha_visual_action_name(action)
        reason = str(action.get("reason") or "").strip()
        reason_part = f", reason={reason[:80]}" if reason else ""
        if action_name == "click":
            clicks = action.get("clicks")
            if not isinstance(clicks, list):
                return f"action={action_name}, clicks=invalid{reason_part}"
            points: list[str] = []
            for item in clicks[:6]:
                try:
                    point_x, point_y = self._read_hcaptcha_visual_point(item)
                    points.append(f"({point_x:.0f},{point_y:.0f})")
                except Exception:
                    points.append("(invalid)")
            suffix = ",..." if len(clicks) > 6 else ""
            return (
                f"action={action_name}, click_count={len(clicks)}, "
                f"clicks=[{','.join(points)}{suffix}]{reason_part}"
            )
        if action_name == "slide":
            return f"action={action_name}, has_slider={isinstance(action.get('slider'), dict)}, has_gap={isinstance(action.get('gap'), dict)}{reason_part}"
        if action_name == "drag_match":
            pairs = action.get("pairs")
            pair_count = len(pairs) if isinstance(pairs, list) else "invalid"
            return f"action={action_name}, pair_count={pair_count}{reason_part}"
        return f"action={action_name or action.get('action') or action.get('captcha_type')}{reason_part}"

    def _recognize_hcaptcha_action(self, prompt: str, image_b64: str) -> dict[str, Any]:
        if not self.captcha_solver:
            raise RuntimeError("未配置 hCaptcha 求解器")
        solve_image = self.captcha_solver.solve_image
        payload = solve_image(
            image_b64,
            prompt=prompt,
            **self._solver_optional_kwargs(
                solve_image,
                timeout_seconds=_HCAPTCHA_VISUAL_RECOGNITION_TIMEOUT_SECONDS,
                poll_interval_seconds=_HCAPTCHA_VISUAL_RECOGNITION_POLL_INTERVAL_SECONDS,
                request_timeout_seconds=_HCAPTCHA_VISUAL_RECOGNITION_REQUEST_TIMEOUT_SECONDS,
            ),
        )
        action = _parse_json_like_payload(payload, label="hCaptcha 视觉识别")
        if not isinstance(action, dict):
            raise RuntimeError(f"hCaptcha 视觉识别结果异常: {action!r}")
        return action

    def _wait_hcaptcha_visual_result(self, page) -> str:
        for _ in range(12):
            self._checkpoint()
            page.wait_for_timeout(1000)
            token = self._extract_hcaptcha_token(page)
            if token:
                self._inject_hcaptcha_token(page, token)
                return token
            if self._is_hcaptcha_checkbox_checked(page):
                page.wait_for_timeout(1000)
                token = self._extract_hcaptcha_token(page)
                if token:
                    self._inject_hcaptcha_token(page, token)
                    return token
                self.log(
                    "Step5: hCaptcha checkbox 已勾选，但未读到 token，按页面态继续"
                )
                return _HCAPTCHA_CHECKED_SENTINEL
        return ""

    def _resolve_hcaptcha_images(self, snapshot: dict[str, Any]) -> list[str]:
        embedded = snapshot.get("images_b64")
        if isinstance(embedded, list):
            images = [str(item).strip() for item in embedded if str(item).strip()]
            if images:
                return images
        urls = snapshot.get("urls")
        if not isinstance(urls, list):
            return []
        clean_urls = [str(url).strip() for url in urls if str(url).strip()]
        if not clean_urls:
            return []
        return self._download_hcaptcha_images(clean_urls)

    def _perform_hcaptcha_visual_action(
        self,
        page,
        challenge_frame,
        bbox: dict[str, float],
        action: dict[str, Any],
    ) -> None:
        action_name = self._hcaptcha_visual_action_name(action)
        if action_name == "click":
            clicks = action.get("clicks")
            if not isinstance(clicks, list) or not clicks:
                raise RuntimeError(f"hCaptcha click 动作缺少 clicks: {action!r}")
            for item in clicks:
                if not isinstance(item, dict):
                    continue
                point_x, point_y = self._read_hcaptcha_visual_point(item)
                abs_x, abs_y = self._map_hcaptcha_point(
                    bbox,
                    point_x,
                    point_y,
                )
                page.mouse.click(abs_x, abs_y)
                page.wait_for_timeout(600)
        elif action_name == "slide":
            slider = action.get("slider")
            gap = action.get("gap")
            if not isinstance(slider, dict) or not isinstance(gap, dict):
                raise RuntimeError(f"hCaptcha slide 动作缺少 slider/gap: {action!r}")
            slider_x, slider_y = self._read_hcaptcha_visual_point(slider)
            gap_x, gap_y = self._read_hcaptcha_visual_point(gap)
            start_x, start_y = self._map_hcaptcha_point(
                bbox,
                slider_x,
                slider_y,
            )
            end_x, end_y = self._map_hcaptcha_point(
                bbox,
                gap_x,
                gap_y,
            )
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.wait_for_timeout(150)
            page.mouse.move(end_x, end_y, steps=20)
            page.wait_for_timeout(150)
            page.mouse.up()
        elif action_name == "drag_match":
            pairs = action.get("pairs")
            if not isinstance(pairs, list) or not pairs:
                raise RuntimeError(f"hCaptcha drag_match 动作缺少 pairs: {action!r}")
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                src = pair.get("from")
                dst = pair.get("to")
                if not isinstance(src, dict) or not isinstance(dst, dict):
                    raise RuntimeError(f"hCaptcha drag_match pair 异常: {pair!r}")
                src_x, src_y = self._read_hcaptcha_visual_point(src)
                dst_x, dst_y = self._read_hcaptcha_visual_point(dst)
                start_x, start_y = self._map_hcaptcha_point(
                    bbox,
                    src_x,
                    src_y,
                )
                end_x, end_y = self._map_hcaptcha_point(
                    bbox,
                    dst_x,
                    dst_y,
                )
                page.mouse.move(start_x, start_y)
                page.mouse.down()
                page.wait_for_timeout(150)
                page.mouse.move(end_x, end_y, steps=20)
                page.wait_for_timeout(150)
                page.mouse.up()
                page.wait_for_timeout(500)
        else:
            raise RuntimeError(f"未支持的 hCaptcha 视觉动作: {action!r}")

        self._click_hcaptcha_button(
            challenge_frame,
            ["检查", "check", "verify", "submit", "下一个", "next"],
        )
        page.wait_for_timeout(1500)

    def _download_hcaptcha_images(self, urls: list[str]) -> list[str]:
        import requests

        images_b64: list[str] = []
        for url in urls:
            if str(url).startswith("data:image"):
                images_b64.append(str(url).split(",", 1)[1])
                continue
            resp = requests.get(
                url,
                timeout=30,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": BUILD_HOME_URL,
                    "Accept": "image/avif,image/webp,image/png,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            images_b64.append(base64.b64encode(resp.content).decode())
        return images_b64

    def _normalize_hcaptcha_targets(self, raw_result: Any, image_count: int) -> list[int]:
        payload = raw_result
        if isinstance(payload, str):
            payload = _parse_json_like_payload(
                payload, label="NVIDIA hCaptcha 分类结果"
            )
        if isinstance(payload, dict):
            for key in ("targets", "indices", "answer", "objects", "result"):
                value = payload.get(key)
                if isinstance(value, list):
                    payload = value
                    break
        if isinstance(raw_result, bool):
            return [0] if raw_result and image_count > 0 else []
        if not isinstance(payload, list):
            raise RuntimeError(f"NVIDIA hCaptcha 分类结果异常: {raw_result!r}")

        targets: list[int] = []
        max_index = image_count - 1
        for value in payload:
            if not isinstance(value, int):
                continue
            if 0 <= value <= max_index and value not in targets:
                targets.append(value)
        return targets

    def _click_hcaptcha_task(self, page, challenge_frame, index: int) -> None:
        tasks = self._hcaptcha_task_locator(challenge_frame)
        try:
            clicked = challenge_frame.evaluate(
                """(payload) => {
                    const nodes = Array.from(document.querySelectorAll(payload.selector));
                    const node = nodes[payload.index];
                    if (!node) return false;
                    node.scrollIntoView({block: 'center', inline: 'center'});
                    node.click();
                    return true;
                }""",
                {"index": int(index), "selector": _HCAPTCHA_TASK_SELECTOR},
            )
            if clicked is True:
                page.wait_for_timeout(350)
                return
        except Exception:
            pass

        task = tasks.nth(index)
        try:
            task.wait_for(state="visible", timeout=10000)
            task.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            task.click(timeout=5000, force=True)
            page.wait_for_timeout(350)
            return
        except Exception:
            pass
        bbox = None
        try:
            bbox = task.bounding_box()
        except Exception:
            bbox = None
        if bbox and bbox.get("width") and bbox.get("height"):
            page.mouse.click(
                float(bbox["x"]) + (float(bbox["width"]) / 2.0),
                float(bbox["y"]) + (float(bbox["height"]) / 2.0),
            )
            page.wait_for_timeout(350)
            return
        raise RuntimeError(f"NVIDIA hCaptcha challenge tile 无法点击: index={index}")

    def _solve_hcaptcha_challenge(self, page) -> str:
        classify_hcaptcha = getattr(self.captcha_solver, "classify_hcaptcha", None)
        if not callable(classify_hcaptcha):
            raise NotImplementedError("captcha solver does not support hCaptcha classification")

        retry_shell_hits = 0
        empty_click_hits = 0
        visual_failure_hits = 0
        unresolved_prompt_hits = 0
        missing_submit_hits = 0
        repeat_visual_signature_hits = 0
        last_visual_signature = ""

        def _record_visual_action(prompt_text: str, action: dict[str, Any]) -> tuple[str, int, str]:
            nonlocal last_visual_signature, repeat_visual_signature_hits
            visual_signature = self._hcaptcha_visual_action_signature(prompt_text, action)
            if visual_signature and visual_signature == last_visual_signature:
                repeat_visual_signature_hits += 1
            else:
                last_visual_signature = visual_signature
                repeat_visual_signature_hits = 1
            return (
                visual_signature,
                repeat_visual_signature_hits,
                self._hcaptcha_visual_action_summary(action),
            )

        def _raise_if_visual_action_stalled(
            visual_signature: str,
            repeat_hits: int,
        ) -> None:
            if visual_signature and repeat_hits >= _HCAPTCHA_MAX_REPEAT_VISUAL_SIGNATURE_HITS:
                raise RuntimeError(
                    f"NVIDIA hCaptcha 视觉动作重复无进展: {repeat_hits} ({visual_signature})"
                )

        page.locator(_HCAPTCHA_IFRAME_SELECTOR).first.wait_for(state="visible", timeout=15000)
        checkbox_frame = page.frame_locator(_HCAPTCHA_IFRAME_SELECTOR).first
        checkbox_frame.locator("#checkbox").click(timeout=15000)
        page.wait_for_timeout(6000)

        challenge_round = 0
        for cycle_index in range(1, 41):
            self._checkpoint()
            token = self._extract_hcaptcha_token(page)
            if token:
                self._inject_hcaptcha_token(page, token)
                return token

            challenge_frame = self._find_hcaptcha_challenge_frame(page)
            challenge_iframe = self._find_hcaptcha_challenge_iframe(page)
            if challenge_frame is None or challenge_iframe is None:
                page.wait_for_timeout(1500)
                continue

            snapshot = self._capture_hcaptcha_challenge(challenge_frame, challenge_iframe)
            prompt = snapshot["prompt"]
            images_b64: list[str] = []

            if self._is_hcaptcha_retry_shell(snapshot):
                retry_shell_hits += 1
                if retry_shell_hits >= _HCAPTCHA_MAX_RETRY_SHELL_HITS:
                    raise RuntimeError(
                        f"NVIDIA hCaptcha retry shell 循环超过阈值: {retry_shell_hits}"
                    )
                self.log(
                    f"Step5: hCaptcha challenge 进入 retry shell，尝试刷新: {snapshot['body']}"
                )
                self._click_hcaptcha_button(
                    challenge_frame,
                    [
                        "刷新挑战",
                        "refresh challenge",
                        "refresh",
                        "检查",
                        "check",
                        "verify",
                        "submit",
                        "下一个",
                        "next",
                        "跳过",
                        "skip",
                    ],
                )
                page.wait_for_timeout(3500)
                continue

            if self._needs_hcaptcha_visual_recognition(snapshot):
                try:
                    bbox = self._challenge_iframe_bbox(challenge_iframe)
                    if not bbox:
                        raise RuntimeError("未获取到 hCaptcha challenge iframe 边界")
                    action = self._recognize_hcaptcha_action(
                        prompt,
                        str(snapshot.get("screenshot_b64") or ""),
                    )
                    visual_signature, repeat_hits, action_summary = _record_visual_action(
                        prompt, action
                    )
                    self.log(
                        f"Step5: hCaptcha 视觉求解 cycle={cycle_index}, repeat={repeat_hits}/{_HCAPTCHA_MAX_REPEAT_VISUAL_SIGNATURE_HITS}, prompt={prompt}, {action_summary}"
                    )
                    _raise_if_visual_action_stalled(visual_signature, repeat_hits)
                    self._perform_hcaptcha_visual_action(
                        page,
                        challenge_frame,
                        bbox,
                        action,
                    )
                    token = self._wait_hcaptcha_visual_result(page)
                    if token:
                        return token
                except Exception as exc:
                    if "视觉动作重复无进展" in str(exc):
                        raise
                    if self._has_hcaptcha_skip_button(snapshot):
                        visual_failure_hits += 1
                        if visual_failure_hits >= _HCAPTCHA_MAX_VISUAL_FAILURE_HITS:
                            raise RuntimeError(
                                f"NVIDIA hCaptcha 视觉求解持续失败: {visual_failure_hits} ({exc})"
                            )
                        if "click 动作缺少 clicks" in str(exc):
                            empty_click_hits += 1
                            if empty_click_hits >= _HCAPTCHA_MAX_EMPTY_CLICK_HITS:
                                raise RuntimeError(
                                    f"NVIDIA hCaptcha 视觉结果持续缺少可执行 clicks: {empty_click_hits}"
                                )
                        self.log(
                            f"Step5: hCaptcha 视觉求解失败，尝试跳过当前题目: {prompt} ({exc})"
                        )
                        self._click_hcaptcha_button(challenge_frame, ["跳过", "skip"])
                        page.wait_for_timeout(2500)
                        continue
                    raise
                continue

            try:
                images_b64 = self._resolve_hcaptcha_images(snapshot)
            except Exception as exc:
                if self._has_hcaptcha_skip_button(snapshot):
                    self.log(
                        f"Step5: hCaptcha challenge 图片准备失败，跳过当前题目: {prompt or snapshot['body']} ({exc})"
                    )
                    self._click_hcaptcha_button(challenge_frame, ["跳过", "skip", "下一个", "next"])
                    page.wait_for_timeout(2500)
                    continue
                raise

            if not prompt or not images_b64:
                screenshot_b64 = str(snapshot.get("screenshot_b64") or "")
                bbox = self._challenge_iframe_bbox(challenge_iframe)
                if prompt and screenshot_b64 and bbox:
                    try:
                        self.log(
                            f"Step5: hCaptcha 图片题缺少可分割图片，回退到整图视觉求解: {prompt}"
                        )
                        action = self._recognize_hcaptcha_action(prompt, screenshot_b64)
                        visual_signature, repeat_hits, action_summary = _record_visual_action(
                            prompt, action
                        )
                        self.log(
                            f"Step5: hCaptcha 整图视觉求解 cycle={cycle_index}, repeat={repeat_hits}/{_HCAPTCHA_MAX_REPEAT_VISUAL_SIGNATURE_HITS}, prompt={prompt}, {action_summary}"
                        )
                        _raise_if_visual_action_stalled(visual_signature, repeat_hits)
                        self._perform_hcaptcha_visual_action(
                            page,
                            challenge_frame,
                            bbox,
                            action,
                        )
                        token = self._wait_hcaptcha_visual_result(page)
                        if token:
                            return token
                        continue
                    except Exception as visual_exc:
                        if self._has_hcaptcha_skip_button(snapshot):
                            visual_failure_hits += 1
                            if visual_failure_hits >= _HCAPTCHA_MAX_VISUAL_FAILURE_HITS:
                                raise RuntimeError(
                                    f"NVIDIA hCaptcha 视觉求解持续失败: {visual_failure_hits} ({visual_exc})"
                                )
                            if "click 动作缺少 clicks" in str(visual_exc):
                                empty_click_hits += 1
                                if empty_click_hits >= _HCAPTCHA_MAX_EMPTY_CLICK_HITS:
                                    raise RuntimeError(
                                        f"NVIDIA hCaptcha 视觉结果持续缺少可执行 clicks: {empty_click_hits}"
                                    )
                            self.log(
                                f"Step5: hCaptcha 整图视觉兜底失败，跳过当前题目: {prompt} ({visual_exc})"
                            )
                            self._click_hcaptcha_button(challenge_frame, ["跳过", "skip", "下一个", "next"])
                            page.wait_for_timeout(2500)
                            continue
                        raise
                if self._has_hcaptcha_skip_button(snapshot):
                    unresolved_prompt_hits += 1
                    if unresolved_prompt_hits >= _HCAPTCHA_MAX_UNRESOLVED_PROMPT_HITS:
                        raise RuntimeError(
                            f"NVIDIA hCaptcha 当前题面持续不可解: {unresolved_prompt_hits}"
                        )
                    self.log(
                        f"Step5: hCaptcha 当前题面尚不可解，先跳过重试: {prompt or snapshot['body']}"
                    )
                    self._click_hcaptcha_button(challenge_frame, ["跳过", "skip"])
                    page.wait_for_timeout(2500)
                    continue
                page.wait_for_timeout(1500)
                continue

            challenge_round += 1
            try:
                raw_result = classify_hcaptcha(
                    prompt,
                    images_b64,
                    **self._solver_optional_kwargs(
                        classify_hcaptcha,
                        timeout_seconds=_HCAPTCHA_VISUAL_RECOGNITION_TIMEOUT_SECONDS,
                        poll_interval_seconds=_HCAPTCHA_VISUAL_RECOGNITION_POLL_INTERVAL_SECONDS,
                        request_timeout_seconds=_HCAPTCHA_VISUAL_RECOGNITION_REQUEST_TIMEOUT_SECONDS,
                    ),
                )
                targets = self._normalize_hcaptcha_targets(raw_result, len(images_b64))
            except Exception as exc:
                if self._has_hcaptcha_skip_button(snapshot):
                    self.log(
                        f"Step5: hCaptcha 分类结果异常，跳过当前题目: {prompt} ({exc})"
                    )
                    self._click_hcaptcha_button(challenge_frame, ["跳过", "skip", "下一个", "next"])
                    page.wait_for_timeout(2500)
                    continue
                raise
            self.log(
                f"Step5: hCaptcha challenge round {challenge_round}, cycle={cycle_index}, prompt={prompt}, targets={targets}"
            )

            try:
                for index in targets:
                    if index in snapshot.get("selected", set()):
                        continue
                    self._click_hcaptcha_task(page, challenge_frame, index)

                if not self._click_hcaptcha_button(
                    challenge_frame, ["检查", "check", "verify", "submit", "下一个", "next"]
                ):
                    raise RuntimeError("未找到 NVIDIA hCaptcha challenge 提交按钮")
            except Exception as exc:
                if "未找到 NVIDIA hCaptcha challenge 提交按钮" in str(exc):
                    missing_submit_hits += 1
                    if missing_submit_hits >= _HCAPTCHA_MAX_MISSING_SUBMIT_HITS:
                        raise RuntimeError(
                            f"NVIDIA hCaptcha 提交阶段无进展，连续 {missing_submit_hits} 次未找到提交按钮"
                        )
                bbox = self._challenge_iframe_bbox(challenge_iframe)
                screenshot_b64 = str(snapshot.get("screenshot_b64") or "")
                if not bbox or not screenshot_b64:
                    raise
                self.log(f"Step5: hCaptcha DOM 题面不可点，回退到整图视觉求解: {exc}")
                try:
                    action = self._recognize_hcaptcha_action(prompt, screenshot_b64)
                    visual_signature, repeat_hits, action_summary = _record_visual_action(
                        prompt, action
                    )
                    self.log(
                        f"Step5: hCaptcha DOM 视觉兜底 cycle={cycle_index}, repeat={repeat_hits}/{_HCAPTCHA_MAX_REPEAT_VISUAL_SIGNATURE_HITS}, prompt={prompt}, {action_summary}"
                    )
                    _raise_if_visual_action_stalled(visual_signature, repeat_hits)
                    self._perform_hcaptcha_visual_action(
                        page,
                        challenge_frame,
                        bbox,
                        action,
                    )
                    token = self._wait_hcaptcha_visual_result(page)
                    if token:
                        return token
                except Exception as visual_exc:
                    if self._has_hcaptcha_skip_button(snapshot):
                        visual_failure_hits += 1
                        if visual_failure_hits >= _HCAPTCHA_MAX_VISUAL_FAILURE_HITS:
                            raise RuntimeError(
                                f"NVIDIA hCaptcha 视觉求解持续失败: {visual_failure_hits} ({visual_exc})"
                            )
                        if "click 动作缺少 clicks" in str(visual_exc):
                            empty_click_hits += 1
                            if empty_click_hits >= _HCAPTCHA_MAX_EMPTY_CLICK_HITS:
                                raise RuntimeError(
                                    f"NVIDIA hCaptcha 视觉结果持续缺少可执行 clicks: {empty_click_hits}"
                                )
                        self.log(
                            f"Step5: hCaptcha 网格题视觉兜底失败，跳过当前题目: {prompt} ({visual_exc})"
                        )
                        self._click_hcaptcha_button(challenge_frame, ["跳过", "skip", "下一个", "next"])
                        page.wait_for_timeout(2500)
                        continue
                    raise

            token = self._wait_hcaptcha_visual_result(page)
            if token:
                return token
        raise RuntimeError("NVIDIA hCaptcha challenge 分类后仍未拿到 token")

    def _solve_hcaptcha(self, page) -> None:
        if not self.captcha_solver:
            raise RuntimeError("未配置 hCaptcha 求解器")
        sitekey = ""
        load_failure_markers = (
            "验证程序加载失败",
            "challenge failed to load",
            "failed to load",
            "ad blocker",
        )
        for wait_round in range(1, 11):
            sitekey = self._extract_hcaptcha_sitekey(page)
            if sitekey:
                break
            body = _safe_body_text(page, 600)
            if any(marker.lower() in body.lower() for marker in load_failure_markers):
                self.log(
                    f"Step5: hCaptcha 组件尚未完成加载，等待重试 {wait_round}/10 ..."
                )
            page.wait_for_timeout(2000)
        if not sitekey:
            raise RuntimeError("未提取到 NVIDIA hCaptcha sitekey")

        self.log(f"Step5: 求解 hCaptcha (sitekey={sitekey[:8]}...) ...")
        classify_method = getattr(self.captcha_solver, "classify_hcaptcha", None)
        classify_impl = getattr(type(self.captcha_solver), "classify_hcaptcha", None)
        supports_hcaptcha_classification = callable(classify_method) and (
            classify_impl is None or classify_impl is not BaseCaptcha.classify_hcaptcha
        )

        challenge_error: Exception | None = None
        if supports_hcaptcha_classification:
            try:
                token = self._solve_hcaptcha_challenge(page)
                if token == _HCAPTCHA_CHECKED_SENTINEL:
                    page.wait_for_timeout(800)
                    return
                if not token:
                    raise RuntimeError("NVIDIA hCaptcha challenge 未返回 token")
                page.wait_for_timeout(800)
                return
            except Exception as exc:
                challenge_error = exc
                self.log(f"Step5: challenge 分类求解失败，转 direct token 求解: {exc}")

        direct_error: Exception | None = None
        try:
            solve_hcaptcha_method = self.captcha_solver.solve_hcaptcha
            solve_kwargs = self._solver_optional_kwargs(
                solve_hcaptcha_method,
                timeout_seconds=_HCAPTCHA_DIRECT_TIMEOUT_SECONDS,
                poll_interval_seconds=_HCAPTCHA_DIRECT_POLL_INTERVAL_SECONDS,
                request_timeout_seconds=_HCAPTCHA_DIRECT_REQUEST_TIMEOUT_SECONDS,
            )
            token = solve_hcaptcha_method(page.url, sitekey, **solve_kwargs)
            if not token:
                raise RuntimeError("hCaptcha 未返回 token")
            if not self._inject_hcaptcha_token(page, token):
                raise RuntimeError("hCaptcha token 注入失败")
            page.wait_for_timeout(800)
            return
        except NotImplementedError as exc:
            direct_error = exc
        except Exception as exc:
            direct_error = exc

        solver_name = type(self.captcha_solver).__name__
        if challenge_error is not None and direct_error is not None:
            if isinstance(challenge_error, NotImplementedError) and isinstance(
                direct_error, NotImplementedError
            ):
                raise RuntimeError(
                    f"当前验证码求解器 {solver_name} 暂不支持 NVIDIA hCaptcha，请改用 yescaptcha 或 manual"
                ) from direct_error
            raise RuntimeError(
                f"NVIDIA hCaptcha 求解失败：challenge={challenge_error}; direct={direct_error}"
            ) from direct_error
        if direct_error is not None:
            if isinstance(direct_error, NotImplementedError):
                raise RuntimeError(
                    f"当前验证码求解器 {solver_name} 暂不支持 NVIDIA hCaptcha，请改用 yescaptcha 或 manual"
                ) from direct_error
            raise RuntimeError(f"NVIDIA hCaptcha direct token 求解失败: {direct_error}") from direct_error
        if challenge_error is not None:
            if isinstance(challenge_error, NotImplementedError):
                raise RuntimeError(
                    f"当前验证码求解器 {solver_name} 暂不支持 NVIDIA hCaptcha，请改用 yescaptcha 或 manual"
                ) from challenge_error
            raise RuntimeError(
                f"NVIDIA hCaptcha challenge 分类求解失败: {challenge_error}"
            ) from challenge_error

    def _submit_create_account(self, page) -> None:
        self.log("Step6: 提交 Create Account ...")
        page.wait_for_timeout(1200)
        try:
            page.get_by_role("button", name="Create Account").click(timeout=5000)
        except Exception:
            if self._click_text_button(page, ["create account", "创建账户"]):
                page.wait_for_timeout(2500)
                return
            submit = page.locator('button[type="submit"]').last
            if submit.count() == 0:
                raise RuntimeError("未找到 NVIDIA Create Account 提交按钮")
            submit.click()
        page.wait_for_timeout(2500)

    def _page_looks_like_email_verification(self, page) -> bool:
        code_input = page.locator(
            'input[autocomplete="one-time-code"], input[name*="code"], input[id*="code"], input[type="number"][maxlength="1"]'
        )
        if code_input.count() > 0:
            return True
        url = str(getattr(page, "url", "") or "").lower()
        if "profile-complete" in url:
            return True
        text = _safe_body_text(page, 800).lower()
        return any(
            marker in text
            for marker in (
                "verification code",
                "check your email",
                "verify your email",
                "enter code",
                "security code",
                "验证您的电子邮件",
                "验证码",
                "6 位验证码",
                "请求新验证码",
            )
        )

    def _submit_email_code(self, page, code: str) -> None:
        digits = re.sub(r"\D+", "", str(code or ""))
        if not digits:
            raise RuntimeError("NVIDIA 邮箱验证码为空")

        self.log("Step7: 提交 NVIDIA 验证码 ...")
        split_inputs = page.locator('input[maxlength="1"]')
        if split_inputs.count() >= 6:
            for idx, ch in enumerate(digits[: split_inputs.count()]):
                split_inputs.nth(idx).fill(ch)
        else:
            code_input = page.locator(
                'input[autocomplete="one-time-code"], input[name*="code"], input[id*="code"], input[type="number"][maxlength="1"]'
            ).first
            code_input.wait_for(state="visible", timeout=15000)
            code_input.fill(digits[:6])

        for label in ("verify", "continue", "submit", "next", "验证", "继续"):
            if self._click_text_button(page, [label]):
                page.wait_for_timeout(1500)
                return
        try:
            page.locator('button[type="submit"]').last.click(timeout=2000)
        except Exception:
            pass
        page.wait_for_timeout(1500)

    def _page_looks_like_privacy_consent(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").lower()
        if "/consent/" in url or "callback/consent" in url:
            try:
                if page.locator("button").count() > 0 or page.locator('input[type="checkbox"]').count() > 0:
                    return True
            except Exception:
                pass
            return bool(_safe_body_text(page, 1000).strip())
        text = _safe_body_text(page, 1000).lower()
        markers = (
            "快完成了",
            "请确认以下信息以完成注册",
            "推荐设置",
            "提交",
        )
        return sum(1 for marker in markers if marker in text) >= 2

    def _submit_privacy_consent(self, page) -> None:
        self.log("Step8: 提交 NVIDIA 隐私确认 ...")
        try:
            self._wait_until(
                lambda: bool(_safe_body_text(page, 600).strip()) or page.locator("button").count() > 0,
                timeout=15,
                interval=0.5,
                desc="等待 NVIDIA 隐私确认页渲染超时",
                page=page,
            )
        except Exception:
            pass
        try:
            checkboxes = page.locator('input[type="checkbox"]')
            total = min(int(checkboxes.count() or 0), 4)
            for idx in range(total):
                box = checkboxes.nth(idx)
                try:
                    if not box.is_checked():
                        box.check(force=True, timeout=2000)
                except Exception:
                    continue
        except Exception:
            pass

        if self._click_text_button(page, ["提交", "submit", "continue", "accept", "agree", "同意", "确认"]):
            page.wait_for_timeout(2500)
            return
        try:
            page.get_by_role("button", name="提交").click(timeout=3000)
        except Exception:
            try:
                page.get_by_role("button", name="Accept").click(timeout=3000)
            except Exception:
                submit = page.locator('button[type="submit"]')
                if submit.count() == 0:
                    raise RuntimeError(
                        f"未找到 NVIDIA 隐私确认按钮，url={page.url}, body={_safe_body_text(page, 1200)}"
                    )
                submit.last.click(timeout=3000)
        page.wait_for_timeout(2500)

    def _page_looks_like_cloud_account_setup(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").lower()
        if "cloudaccounts.nvidia.com" in url and "select-account" in url:
            return True
        text = _safe_body_text(page, 1200).lower()
        return (
            "create an nvidia cloud account" in text
            or ("cloud account" in text and "account name" in text)
        )

    def _build_cloud_account_name(self, email: str = "") -> str:
        prefix = re.sub(r"[^a-z0-9]+", "-", str(email or "").split("@", 1)[0].lower()).strip("-")
        if not prefix:
            prefix = "nvidia"
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"aar-{prefix[:18]}-{suffix}"

    def _submit_cloud_account_setup(self, page, email: str = "") -> None:
        self.log("Step9: 创建 NVIDIA Cloud Account ...")
        name = self._build_cloud_account_name(email)
        input_locator = page.locator('input[type="text"], input:not([type]), textarea').first
        input_locator.wait_for(state="visible", timeout=15000)
        input_locator.fill(name)
        if self._click_text_button(page, ["create nvidia cloud account", "create cloud account"]):
            page.wait_for_timeout(4000)
            return
        try:
            page.get_by_role("button", name="Create NVIDIA Cloud Account").click(timeout=3000)
        except Exception:
            page.locator('button[type="submit"]').last.click(timeout=3000)
        page.wait_for_timeout(4000)

    def _page_looks_like_build_verify_gate(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").lower()
        if "build.nvidia.com" not in url:
            return False
        text = _safe_body_text(page, 1200).lower()
        return "verify your account" in text and "api access" in text

    def _submit_build_verify_gate(self, page) -> None:
        self.log("Step10: 触发 NVIDIA 最终账号验证 ...")
        if self._click_text_button(page, ["verify"]):
            page.wait_for_timeout(3000)
            return
        try:
            page.get_by_role("button", name="Verify").click(timeout=3000)
        except Exception:
            page.locator('button[type="submit"]').last.click(timeout=3000)
        page.wait_for_timeout(3000)

    def _page_looks_like_verify_success_page(self, page) -> bool:
        url = str(getattr(page, "url", "") or "").lower()
        if "profile-management/verify-email" not in url:
            return False
        text = _safe_body_text(page, 1000).lower()
        return (
            "验证成功" in text
            or (
                "return to the original page" in text
                and "automatically close" in text
            )
        )

    def _return_from_verify_success_page(self, page) -> None:
        self.log("Step10: 邮件验证成功，返回 NVIDIA Build 继续 ...")
        page.goto(BUILD_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

    def _request_email_code(
        self,
        otp_callback: Callable[..., str],
        *,
        exclude_codes: set[str] | None = None,
        otp_sent_at: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(otp_callback)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            params = signature.parameters
            accepts_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in params.values()
            )
            if accepts_kwargs or "exclude_codes" in params:
                kwargs["exclude_codes"] = exclude_codes or set()
            if accepts_kwargs or "otp_sent_at" in params:
                kwargs["otp_sent_at"] = otp_sent_at
        if kwargs:
            return str(otp_callback(**kwargs) or "")
        return str(otp_callback() or "")

    def _fetch_json(
        self,
        page,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        referrer: str | None = None,
    ) -> dict[str, Any]:
        return page.evaluate(
            """async (payload) => {
                const options = {
                    method: payload.method || 'GET',
                    headers: payload.headers || {},
                    credentials: 'include',
                };
                if (payload.body !== undefined && payload.body !== null) {
                    options.body = JSON.stringify(payload.body);
                }
                if (payload.referrer) {
                    options.referrer = payload.referrer;
                }

                const response = await fetch(payload.url, options);
                const text = await response.text();
                let json = null;
                try {
                    json = text ? JSON.parse(text) : null;
                } catch (_) {}

                return {
                    ok: response.ok,
                    status: response.status,
                    text,
                    json,
                };
            }""",
            {
                "url": url,
                "method": method,
                "headers": headers or {},
                "body": body,
                "referrer": referrer,
            },
        )

    def _unwrap_payload(self, payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _fetch_first_json(
        self,
        page,
        urls: list[str],
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        referrer: str | None = None,
    ) -> dict[str, Any]:
        last_result: dict[str, Any] | None = None
        for url in urls:
            try:
                result = self._fetch_json(
                    page,
                    url,
                    method=method,
                    headers=headers,
                    body=body,
                    referrer=referrer,
                )
            except Exception:
                continue
            last_result = result
            payload = result.get("json")
            if isinstance(payload, (dict, list)):
                return result
        return last_result or {"ok": False, "status": 0, "text": "", "json": None}

    def _try_resolve_session_context(self, probe_page) -> dict[str, Any] | None:
        try:
            probe_page.goto(BUILD_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            probe_page.wait_for_timeout(2000)

            user_context_resp = self._fetch_first_json(
                probe_page,
                [NVIDIA_USER_CONTEXT_URL, "/user-context"],
            )
            user_context = self._unwrap_payload(user_context_resp.get("json"))
            if not isinstance(user_context, dict):
                user_context = {}

            me_resp = self._fetch_first_json(
                probe_page,
                [NVIDIA_ME_URL, "/v2/users/me"],
            )
            me_payload = self._unwrap_payload(me_resp.get("json"))
            user = me_payload.get("user") if isinstance(me_payload, dict) else {}
            if not isinstance(user, dict):
                user = {}

            org_name = _pick_first_text(user_context, "orgName", "org_name")
            if not org_name:
                roles = user.get("roles")
                if isinstance(roles, list):
                    for role in roles:
                        if not isinstance(role, dict):
                            continue
                        org = role.get("org")
                        if not isinstance(org, dict):
                            continue
                        org_name = _pick_first_text(org, "name")
                        if org_name:
                            self._fetch_first_json(
                                probe_page,
                                [NVIDIA_USER_CONTEXT_URL, "/user-context"],
                                method="POST",
                                headers={"Content-Type": "application/json"},
                                body={"orgName": org_name},
                            )
                            break

            if not org_name:
                return None

            return {
                "org_name": org_name,
                "user": user,
                "user_context": user_context,
            }
        except Exception:
            return None

    def _complete_post_create_flow(
        self,
        page,
        probe_page,
        *,
        otp_callback: Optional[Callable[..., str]] = None,
        verification_link_callback: Optional[Callable[[], str]] = None,
        email: str = "",
    ) -> dict[str, Any]:
        deadline = time.time() + 150
        tried_codes: set[str] = set()
        verification_link_used = False
        while time.time() < deadline:
            context_data = self._try_resolve_session_context(probe_page)
            if context_data:
                return context_data

            if self._page_looks_like_email_verification(page):
                if otp_callback is None:
                    raise RuntimeError("NVIDIA 注册后进入邮箱验证，但当前任务未配置邮箱回调")
                if tried_codes:
                    self.log("Step7: 验证页仍存在，等待新的 NVIDIA 验证码 ...")
                code = self._request_email_code(
                    otp_callback,
                    exclude_codes=set(tried_codes),
                    otp_sent_at=None,
                )
                raw_code = str(code or "").strip()
                normalized_code = re.sub(r"\D+", "", raw_code)
                if not normalized_code:
                    if tried_codes:
                        raise RuntimeError("NVIDIA 验证页要求新的验证码，但未获取到新验证码")
                    raise RuntimeError("NVIDIA 需要邮箱验证，但未获取到验证码")
                if raw_code in tried_codes or normalized_code in tried_codes:
                    page.wait_for_timeout(1500)
                    continue
                self._submit_email_code(page, raw_code)
                tried_codes.update({raw_code, normalized_code})
                continue

            if self._page_looks_like_privacy_consent(page):
                self._submit_privacy_consent(page)
                continue

            if self._page_looks_like_cloud_account_setup(page):
                self._submit_cloud_account_setup(page, email=email)
                continue

            if self._page_looks_like_build_verify_gate(page):
                if verification_link_callback is not None and not verification_link_used:
                    verify_link = str(verification_link_callback() or "").strip()
                    if verify_link:
                        self.log("Step10: 使用邮件验证链接完成 NVIDIA 最终验证 ...")
                        page.goto(verify_link, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000)
                        verification_link_used = True
                        continue
                self._submit_build_verify_gate(page)
                continue

            if self._page_looks_like_verify_success_page(page):
                self._return_from_verify_success_page(page)
                continue

            page.wait_for_timeout(2500)

        raise RuntimeError(
            f"NVIDIA 注册后未拿到可用登录态，url={page.url}, body={_safe_body_text(page)}"
        )

    def _build_key_name(self, email: str) -> str:
        prefix = re.sub(r"[^a-z0-9]+", "-", email.split("@", 1)[0].lower()).strip("-")
        if not prefix:
            prefix = "nvidia"
        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"aar-{prefix[:18]}-{suffix}"

    def _generate_api_key(self, probe_page, *, org_name: str, email: str) -> dict[str, Any]:
        self.log(f"Step8: 创建 NVIDIA API key (org={org_name}) ...")
        payload = {
            "name": self._build_key_name(email),
            "type": "AI_PLAYGROUNDS_KEY",
            "expiryDate": _expiry_after_years(100),
            "policies": [
                {
                    "product": "nv-cloud-functions",
                    "scopes": ["invoke_function"],
                    "resources": [{"id": "*", "type": "account-functions"}],
                }
            ],
        }
        result = self._fetch_json(
            probe_page,
            NVIDIA_KEY_URL.format(org_name=org_name),
            method="POST",
            headers={"accept": "*/*", "content-type": "application/json"},
            body=payload,
            referrer=BUILD_HOME_URL,
        )
        payload_json = result.get("json")
        if not isinstance(payload_json, dict):
            payload_json = {}
        if int(result.get("status") or 0) >= 400:
            message = _pick_first_text(payload_json, "message", "error") or str(
                result.get("text") or "创建 NVIDIA API key 失败"
            )
            raise RuntimeError(message)

        api_key = _pick_first_text(payload_json, "value")
        api_key_obj = payload_json.get("apiKey") if isinstance(payload_json.get("apiKey"), dict) else {}
        if not api_key:
            api_key = _pick_first_text(api_key_obj, "value")
        if not api_key:
            raise RuntimeError(f"NVIDIA API key 返回缺少 value: {payload_json}")

        return {
            "api_key": api_key,
            "key_id": _pick_first_text(api_key_obj, "keyId", "id"),
            "key_expiry": _pick_first_text(api_key_obj, "expiryDate") or payload["expiryDate"],
        }

    def register(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Optional[Callable[[], str]] = None,
        verification_link_callback: Optional[Callable[[], str]] = None,
    ) -> dict[str, Any]:
        playwright = None
        browser = None
        context = None
        probe_page = None
        try:
            playwright, browser = self._launch_browser()
            context = browser.new_context(
                viewport={"width": 1440, "height": 1080},
                user_agent=USER_AGENT,
            )
            page = context.new_page()

            self._open_entry_modal(page)
            self._submit_email(page, email)
            self._wait_for_create_account(page)
            self._fill_create_account_form(page, email, password)
            self._solve_hcaptcha(page)
            self._submit_create_account(page)

            probe_page = context.new_page()
            session_context = self._complete_post_create_flow(
                page,
                probe_page,
                otp_callback=otp_callback,
                verification_link_callback=verification_link_callback,
                email=email,
            )
            key_info = self._generate_api_key(
                probe_page,
                org_name=session_context["org_name"],
                email=email,
            )
            user = session_context.get("user") if isinstance(session_context, dict) else {}
            if not isinstance(user, dict):
                user = {}
            self.log("NVIDIA 注册链路完成")
            return {
                "email": email,
                "password": password,
                "api_key": key_info["api_key"],
                "base_url": NVIDIA_BASE_URL,
                "org_name": session_context["org_name"],
                "key_id": key_info.get("key_id", ""),
                "key_expiry": key_info.get("key_expiry", ""),
                "user_verified": user.get("verified"),
                "user_blocked": user.get("blocked"),
            }
        finally:
            try:
                if probe_page:
                    probe_page.close()
            except Exception:
                pass
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if playwright:
                    playwright.stop()
            except Exception:
                pass
