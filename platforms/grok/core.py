"""
Grok (x.ai) 浏览器 UI 注册链（headless / headed）。

默认注册请走 protocol_register（TLS + gRPC + offscreen Turnstile）。
本模块保留完整浏览器 UI 流程，用于：
- 用户显式选择 headless/headed
- 协议链失败后的可选回退

浏览器步骤：
1. 打开带 redirect 的邮箱注册入口
2. 提交邮箱并等待验证码页
3. 填写验证码并进入资料页
4. 填资料、解决 Turnstile、提交注册
5. 接受 ToS，并持续等待 sso / sso-rw cookie 稳定出现
"""

from __future__ import annotations

import ctypes
import os
import random
import re
import string
import time
from typing import Any, Callable, Optional, Tuple
from urllib.parse import quote, urlsplit

import requests

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
    with_chrome_executable,
)
from core.proxy_utils import build_playwright_proxy_config

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
TURNSTILE_TOKEN_MIN_LENGTH = 20
DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
_FLARESOLVERR_COOKIE_MARKERS = {"cf_clearance", "__cf_bm", "xai_anon_id"}
_LOCAL_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DISABLED_VALUES = {"0", "false", "no", "off", "none", "disabled"}
_EMAIL_SIGNUP_BUTTON_LABELS = [
    "使用邮箱注册",
    "邮箱注册",
    "sign up with email",
    "signup with email",
    "use email",
    "email signup",
]
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TURNSTILE_PATCH_JS = r"""
(() => {
  function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  const screenX = getRandomInt(800, 1200);
  const screenY = getRandomInt(400, 600);

  try {
    Object.defineProperty(MouseEvent.prototype, 'screenX', {
      configurable: true,
      value: screenX,
    });
  } catch (_) {}
  try {
    Object.defineProperty(MouseEvent.prototype, 'screenY', {
      configurable: true,
      value: screenY,
    });
  } catch (_) {}
})();
"""


def _rand_name(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n)).capitalize()


def _rand_password(n: int = 12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n)) + ",,,aA1"


def _safe_body_text(page, limit: int = 600) -> str:
    try:
        text = str(page.locator("body").inner_text(timeout=1500) or "").strip()
    except Exception:
        return ""
    return " ".join(text.split())[:limit]


_TURNSTILE_SITEKEY_RE = re.compile(r"^0x[0-9A-Za-z_-]{10,}$")
_CHROME_VERSION_RE = re.compile(
    r"Chrome/(?P<major>\d+)(?:\.(?P<minor>\d+))?(?:\.(?P<build>\d+))?(?:\.(?P<patch>\d+))?"
)
_TURNSTILE_WIDGET_INPUT_RE = re.compile(
    r"^cf-chl-widget-(?P<widget_id>[A-Za-z0-9_-]+)_response$"
)
# x.ai often flashes a generic toast on the first register click; second click works.
_TRANSIENT_RETRY_STRONG_MARKERS = (
    "出了点问题",
    "Something went wrong",
    "something went wrong",
)
_REGISTER_SUBMIT_BUTTON_LABELS = (
    "完成注册",
    "创建账户",
    "Create account",
    "Sign up",
    "Continue",
    "继续",
)


class GrokRegister:
    def __init__(
        self,
        captcha_solver=None,
        yescaptcha_key: str = "",
        proxy=None,
        log_fn=print,
        headless: bool = False,
        task_control=None,
        extra: Optional[dict[str, Any]] = None,
    ):
        self.captcha_solver = captcha_solver
        self.key = yescaptcha_key
        self.proxy = proxy
        self.log = log_fn
        self.headless = headless
        self._task_control = task_control
        self.extra = dict(extra or {})
        self._browser_user_agent = ""
        # Diagnostics for later Device OAuth / invalid_grant correlation.
        self._register_submit_meta: dict[str, Any] = {
            "submit_attempts": 0,
            "transient_error_retries": 0,
            "saw_transient_error": False,
            "email_transient_retries": 0,
        }

    def _checkpoint(self) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint()

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _page_wait(self, page, wait_ms: int) -> None:
        self._checkpoint()
        try:
            page.wait_for_timeout(max(int(wait_ms or 0), 1))
        except Exception:
            self._sleep_with_checkpoint(max(float(wait_ms or 0) / 1000.0, 0.01))

    def _install_turnstile_patch(self, context) -> None:
        try:
            context.add_init_script(_TURNSTILE_PATCH_JS)
            self.log("已注入 turnstilePatch 等价补丁")
        except Exception as exc:
            self.log(f"[Debug] turnstilePatch 注入失败: {exc}")

    def _wait_until(
        self,
        fn: Callable[[], bool],
        timeout: float = 30.0,
        interval: float = 0.5,
        desc: str = "",
        page=None,
    ) -> None:
        deadline = time.monotonic() + timeout
        wait_ms = max(1, int(max(interval, 0.01) * 1000))
        while time.monotonic() < deadline:
            self._checkpoint()
            if fn():
                return
            if page is not None:
                self._page_wait(page, wait_ms)
            else:
                self._sleep_with_checkpoint(interval)
        raise TimeoutError(desc or "等待超时")

    @staticmethod
    def _has_auth_cookies(cookies: list) -> bool:
        return any(cookie.get("name") in {"sso", "sso-rw"} for cookie in cookies)

    def _detect_blocked_signup_page(self, page) -> str:
        title = ""
        body = ""
        try:
            title = str(page.title() or "").strip()
        except Exception:
            title = ""
        body = _safe_body_text(page, limit=900)
        merged = f"{title}\n{body}".lower()
        markers = (
            "attention required",
            "you have been blocked",
            "unable to access x.ai",
            "cloudflare ray id",
            "performance & security by cloudflare",
        )
        if not any(marker in merged for marker in markers):
            return ""
        summary_parts = []
        if title:
            summary_parts.append(f"title={title}")
        if body:
            summary_parts.append(f"body={body[:280]}")
        return " | ".join(summary_parts) or "Cloudflare block page"

    @staticmethod
    def _is_email_signup_button_text(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().lower().split())
        if not normalized:
            return False
        exact_tokens = {
            "使用邮箱注册",
            "邮箱注册",
            "sign up with email",
            "signup with email",
            "use email",
            "email signup",
        }
        return normalized in exact_tokens

    def _signup_gate_state(self, page) -> str:
        if page.locator("input[type=email]").count() > 0:
            return "email_input"
        if self._detect_blocked_signup_page(page):
            return "blocked"
        visible_buttons = page.evaluate(
            """() => {
                function isVisible(node) {
                    if (!node) return false;
                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                    const rect = node.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                const texts = [...document.querySelectorAll('button, a, [role="button"]')]
                  .filter((node) => isVisible(node))
                  .map((node) => String(node.innerText || node.textContent || '').trim())
                  .filter(Boolean);
                return texts;
            }"""
        )
        if any(
            self._is_email_signup_button_text(str(item or ""))
            for item in (visible_buttons or [])
        ):
            return "email_button"
        return "loading"

    @staticmethod
    def _document_ready_state(page) -> str:
        try:
            return str(page.evaluate("() => document.readyState || ''") or "").strip().lower()
        except Exception:
            return ""

    def _human_click_locator(self, page, locator) -> bool:
        """Real mouse trajectory click.

        x.ai signup gate ignores Playwright synthetic locator.click() and bare
        node.click(); only trusted pointer input advances past "使用邮箱注册".
        """
        try:
            if locator.count() <= 0:
                return False
            target = locator.first
            if not target.is_visible():
                return False
            box = target.bounding_box()
            if not box or float(box.get("width") or 0) <= 0 or float(box.get("height") or 0) <= 0:
                target.click(timeout=3000)
                return True
            click_x = float(box["x"]) + float(box["width"]) / 2
            click_y = float(box["y"]) + float(box["height"]) / 2
            page.mouse.move(click_x, click_y, steps=20)
            self._page_wait(page, 120)
            page.mouse.down()
            self._page_wait(page, 60)
            page.mouse.up()
            return True
        except Exception:
            return False

    def _click_text_button(self, page, labels: list[str]) -> bool:
        for label in labels:
            try:
                locator = page.get_by_role("button", name=label)
                if self._human_click_locator(page, locator):
                    return True
            except Exception:
                pass
            try:
                locator = page.locator(f"text={label}")
                if self._human_click_locator(page, locator):
                    return True
            except Exception:
                pass
        # Last resort: locate by text and human-click via bounding rect from DOM.
        try:
            box = page.evaluate(
                """(labels) => {
                    const targets = labels
                      .map((item) => String(item || '').trim().toLowerCase())
                      .filter(Boolean);
                    if (!targets.length) return null;

                    function isVisible(node) {
                      if (!node) return false;
                      const style = window.getComputedStyle(node);
                      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                      const rect = node.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    }

                    const nodes = [...document.querySelectorAll('button, a, [role="button"]')];
                    for (const node of nodes) {
                      if (!isVisible(node) || node.disabled || node.getAttribute('aria-disabled') === 'true') continue;
                      const text = String(node.innerText || node.textContent || '').trim().toLowerCase();
                      if (!text) continue;
                      if (targets.some((target) => text === target || text.includes(target))) {
                        const rect = node.getBoundingClientRect();
                        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
                      }
                    }
                    return null;
                }""",
                labels,
            )
            if box and float(box.get("width") or 0) > 0:
                click_x = float(box["x"]) + float(box["width"]) / 2
                click_y = float(box["y"]) + float(box["height"]) / 2
                page.mouse.move(click_x, click_y, steps=18)
                self._page_wait(page, 100)
                page.mouse.down()
                self._page_wait(page, 50)
                page.mouse.up()
                return True
        except Exception:
            pass
        return False

    def _ensure_email_signup_form(
        self,
        page,
        *,
        timeout: float,
        stage_label: str,
    ) -> None:
        deadline = time.monotonic() + timeout
        click_attempt = 0
        while time.monotonic() < deadline:
            self._checkpoint()
            blocked_detail = self._detect_blocked_signup_page(page)
            if blocked_detail:
                raise RuntimeError(
                    f"Grok 注册页被 Cloudflare/WAF 封禁，当前代理不可用: {blocked_detail}"
                )
            if self._page_has_email_input(page):
                page.locator("input[type=email]").first.wait_for(
                    state="visible", timeout=10000
                )
                return

            gate_state = self._signup_gate_state(page)
            if gate_state != "email_button":
                self._page_wait(page, 500)
                continue

            # Wait for hydration / CF jsd before first trusted click.
            if click_attempt == 0:
                for _ in range(12):
                    if self._document_ready_state(page) == "complete":
                        break
                    self._page_wait(page, 400)
            click_attempt += 1
            clicked = self._click_text_button(page, _EMAIL_SIGNUP_BUTTON_LABELS)
            if not clicked:
                raise RuntimeError(
                    f"未找到邮箱注册入口按钮，url={page.url}, body={_safe_body_text(page)}"
                )
            self._page_wait(page, 1500)
            if self._page_has_email_input(page):
                page.locator("input[type=email]").first.wait_for(
                    state="visible", timeout=10000
                )
                return
            if click_attempt == 1 or click_attempt % 2 == 0:
                self.log(
                    f"  {stage_label}: 点击邮箱注册后仍未进入邮箱输入页，继续等待并重试 ({click_attempt})"
                )
            self._page_wait(page, 700)

        raise RuntimeError(
            f"等待邮箱输入框超时，url={page.url}, ready_state={self._document_ready_state(page)}, body={_safe_body_text(page)}"
        )

    def _launch_browser(self):
        from patchright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        headless, reason = resolve_browser_headless(
            self.headless, default_headless=False
        )
        # Prefer real headed window for Turnstile; never park off-screen unless asked.
        force_visible = str(
            (self.extra or {}).get("grok_force_visible_browser", "1")
        ).strip().lower() not in {"0", "false", "no", "off"}
        if force_visible and headless and not self.headless:
            # requested headed but runtime flipped to headless — keep visible attempt
            headless = False
            reason = f"{reason}; force_visible"
        ensure_browser_display_available(headless)
        self.log(f"浏览器模式: {'headless' if headless else 'headed'} ({reason})")
        browser_user_agent = self._resolve_browser_user_agent()
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
        }
        if not headless:
            # Visible window on-screen (offscreen -2400 coords hurts managed Turnstile).
            launch_kwargs["args"] = [
                "--disable-blink-features=AutomationControlled",
                "--window-position=80,60",
                "--window-size=1400,1000",
            ]
        if self.proxy:
            proxy_cfg = build_playwright_proxy_config(self.proxy)
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
        try:
            browser = playwright.chromium.launch(
                **with_chrome_executable(launch_kwargs, channel="chrome")
            )
        except Exception:
            browser = playwright.chromium.launch(**with_chrome_executable(launch_kwargs))
        self.log(f"浏览器 UA: {browser_user_agent}")
        return playwright, browser

    def _page_has_otp_form(self, page) -> bool:
        return bool(
            page.evaluate(
                """() => {
                    const selectors = [
                      'input[data-input-otp="true"]',
                      'input[name="code"]',
                      'input[autocomplete="one-time-code"]',
                      'input[inputmode="numeric"]',
                    ];
                    return selectors.some((selector) => document.querySelector(selector));
                }"""
            )
        )

    def _page_has_profile_form(self, page) -> bool:
        return bool(
            page.evaluate(
                """() => {
                    const givenInput =
                      document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
                    const familyInput =
                      document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
                    const passwordInput =
                      document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');
                    return !!(givenInput && familyInput && passwordInput);
                }"""
            )
        )

    def _goto_email_signup(self, page) -> None:
        self.log("Step1: 打开 Grok 注册页...")
        page.goto(SIGNUP_URL, wait_until="domcontentloaded")
        self._page_wait(page, 1500)

        gate_state = "loading"

        def _gate_ready() -> bool:
            nonlocal gate_state
            gate_state = self._signup_gate_state(page)
            return gate_state in {"email_input", "email_button", "blocked"}

        self._wait_until(
            _gate_ready,
            timeout=20,
            interval=0.5,
            desc="等待 Grok 注册入口超时",
            page=page,
        )

        if gate_state == "blocked":
            blocked_detail = self._detect_blocked_signup_page(page)
            raise RuntimeError(
                f"Grok 注册页被 Cloudflare/WAF 封禁，当前代理不可用: {blocked_detail}"
            )

        if gate_state == "email_button":
            self._ensure_email_signup_form(
                page,
                timeout=18,
                stage_label="Step1",
            )
            return

        page.locator("input[type=email]").first.wait_for(state="visible", timeout=10000)

    def _submit_email(self, page, email: str) -> None:
        self.log(f"Step2: 提交邮箱 {email} ...")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self._checkpoint()
            # Prefer real keyboard typing so React controlled state updates.
            filled = False
            try:
                email_box = page.locator(
                    'input[data-testid="email"], input[type="email"], input[name="email"], input[autocomplete="email"]'
                ).first
                if email_box.count() > 0 and email_box.is_visible():
                    email_box.click(timeout=2500)
                    try:
                        email_box.fill("")
                    except Exception:
                        pass
                    email_box.type(email, delay=35)
                    current = str(email_box.input_value() or "").strip()
                    filled = current == email
            except Exception:
                filled = False
            if not filled:
                filled_status = page.evaluate(
                    """(email) => {
                        function isVisible(node) {
                            if (!node) return false;
                            const style = window.getComputedStyle(node);
                            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                            const rect = node.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        }

                        const input = Array.from(document.querySelectorAll(
                            'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]'
                        )).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
                        if (!input) return 'not-ready';

                        input.focus();
                        input.click();
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        const tracker = input._valueTracker;
                        if (tracker) tracker.setValue('');
                        if (setter) setter.call(input, email);
                        else input.value = email;
                        input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
                        input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        if ((input.value || '').trim() !== email || !input.checkValidity()) {
                            return 'fill-failed';
                        }
                        return 'filled';
                    }""",
                    email,
                )
                if filled_status == "not-ready":
                    self._page_wait(page, 500)
                    continue
                if filled_status != "filled":
                    self._page_wait(page, 500)
                    continue

            # Blur / Tab so React validators and Castle hooks see the final value.
            try:
                page.locator(
                    'input[data-testid="email"], input[type="email"], input[name="email"]'
                ).first.press("Tab")
            except Exception:
                pass
            self._page_wait(page, 500)
            if self._click_email_register_button(page):
                # Give SPA time to disable button / fire create-email action.
                self._page_wait(page, 1200)
                break
            self._page_wait(page, 500)
        else:
            raise RuntimeError("未找到邮箱输入框或注册按钮")

        # Wait longer: create-email may be slow under proxy / CF.
        # First click often surfaces a transient "出了点问题，请重试" toast — re-click.
        max_email_submit_rounds = 4
        for email_round in range(1, max_email_submit_rounds + 1):
            try:
                self._wait_until(
                    lambda: self._page_has_otp_form(page)
                    or self._page_has_profile_form(page),
                    timeout=18 if email_round == 1 else 12,
                    interval=0.45,
                    desc="等待邮箱验证码页超时",
                    page=page,
                )
                if email_round > 1:
                    self.log(
                        f"  Step2: 邮箱注册按钮第 {email_round} 次提交成功"
                        f"（此前出现过瞬态错误提示）"
                    )
                return
            except Exception:
                body = _safe_body_text(page)
                body_lower = body.lower()
                if any(
                    marker in body
                    for marker in ("域名", "已被拒绝", "其他邮箱地址")
                ) or any(
                    marker in body_lower for marker in ("disposable", "rejected")
                ):
                    raise RuntimeError(f"邮箱域名被拒绝: {body[:200]}")

                transient = self._has_transient_retry_error(page)
                still_on_email = (
                    self._page_has_email_input(page)
                    and not self._page_has_otp_form(page)
                    and not self._page_has_profile_form(page)
                )
                if still_on_email and email_round < max_email_submit_rounds:
                    if transient:
                        self._register_submit_meta["email_transient_retries"] = (
                            int(
                                self._register_submit_meta.get(
                                    "email_transient_retries", 0
                                )
                            )
                            + 1
                        )
                        self._register_submit_meta["saw_transient_error"] = True
                        self.log(
                            f"  Step2: 邮箱提交出现「出了点问题/请重试」类提示，"
                            f"第 {email_round + 1}/{max_email_submit_rounds} 次重试…"
                        )
                    else:
                        self.log(
                            f"  Step2: 仍在邮箱页，第 {email_round + 1}/"
                            f"{max_email_submit_rounds} 次重试提交…"
                        )
                    self._page_wait(page, 700 + email_round * 250)
                    self._click_email_register_button(page)
                    self._page_wait(page, 1000)
                    continue

                if transient:
                    raise RuntimeError(
                        "邮箱提交被服务端拒绝（通用错误，常见于 bot/WAF/Castle 校验失败；"
                        f"已重试 {email_round} 次）: {body[:220]}"
                    )
                raise RuntimeError(f"邮箱提交失败: {body[:200]}")

    def _submit_otp(self, page, code: str) -> None:
        self.log(f"Step3: 提交邮箱验证码 {code} ...")
        clean_code = str(code or "").replace("-", "").replace(" ", "").strip()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            self._checkpoint()
            filled = page.evaluate(
                r"""(code) => {
                    if (!code) return 'empty-code';

                    function isVisible(node) {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }

                    function setInputValue(input, value) {
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        const tracker = input._valueTracker;
                        if (tracker) tracker.setValue('');
                        if (setter) setter.call(input, value);
                        else input.value = value;
                        input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
                        input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                    }

                    const aggregate = Array.from(document.querySelectorAll(
                      'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
                    )).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);
                    if (aggregate) {
                        aggregate.focus();
                        aggregate.click();
                        setInputValue(aggregate, code);
                        return String(aggregate.value || '').replace(/\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
                    }

                    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
                        if (!isVisible(node) || node.disabled || node.readOnly) return false;
                        const maxLength = Number(node.maxLength || 0);
                        const ac = String(node.autocomplete || '').toLowerCase();
                        return maxLength === 1 || ac === 'one-time-code';
                    });
                    if (otpBoxes.length >= code.length) {
                        for (let idx = 0; idx < code.length; idx += 1) {
                            const ch = code[idx] || '';
                            const box = otpBoxes[idx];
                            box.focus();
                            box.click();
                            setInputValue(box, ch);
                            box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
                            box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
                        }
                        const merged = otpBoxes.slice(0, code.length).map((node) => String(node.value || '').trim()).join('');
                        return merged.length ? 'filled-boxes' : 'boxes-failed';
                    }

                    return 'not-ready';
                }""",
                clean_code,
            )
            if filled == "not-ready":
                self._page_wait(page, 500)
                continue
            if "failed" in str(filled):
                self._page_wait(page, 500)
                continue

            clicked = page.evaluate(
                r"""() => {
                    function isVisible(node) {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }

                    const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
                        return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
                    });
                    const target = buttons.find((node) => {
                        const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
                        return (
                            text.includes('确认邮箱') ||
                            text.includes('继续') ||
                            text.includes('下一步') ||
                            text.includes('confirm') ||
                            text.includes('continue') ||
                            text.includes('next')
                        );
                    });
                    if (!target) return 'no-button';
                    target.focus();
                    target.click();
                    return 'clicked';
                }"""
            )
            if clicked in ("clicked", "no-button"):
                break
            self._page_wait(page, 500)
        else:
            raise RuntimeError("验证码已获取，但自动填写/提交失败")

        self._wait_until(
            lambda: self._page_has_profile_form(page),
            timeout=25,
            interval=0.5,
            desc="等待完成注册页超时",
            page=page,
        )
        self.log("  已进入完成注册页")

    def _fill_user_form(
        self, page, given_name: str, family_name: str, password: str
    ) -> None:
        self.log(f"Step4: 填写用户信息 {given_name} {family_name} ...")
        deadline = time.monotonic() + 20
        payload = {
            "given_name": given_name,
            "family_name": family_name,
            "password": password,
        }
        while time.monotonic() < deadline:
            self._checkpoint()
            filled = page.evaluate(
                """(payload) => {
                    const givenName = String(payload?.given_name || '');
                    const familyName = String(payload?.family_name || '');
                    const password = String(payload?.password || '');
                    function isVisible(node) {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }

                    function pickInput(selector) {
                        return Array.from(document.querySelectorAll(selector)).find((node) => {
                            return isVisible(node) && !node.disabled && !node.readOnly;
                        }) || null;
                    }

                    function setInputValue(input, value) {
                        input.focus();
                        input.click();
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                        const tracker = input._valueTracker;
                        if (tracker) tracker.setValue('');
                        if (setter) setter.call(input, value);
                        else input.value = value;
                        input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
                        input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        input.blur();
                        return String(input.value || '').trim() === String(value || '').trim();
                    }

                    const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
                    const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
                    const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');
                    if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

                    const ok1 = setInputValue(givenInput, givenName);
                    const ok2 = setInputValue(familyInput, familyName);
                    const ok3 = setInputValue(passwordInput, password);
                    return ok1 && ok2 && ok3 ? 'filled' : 'fill-failed';
                }""",
                payload,
            )
            if filled == "not-ready":
                self._page_wait(page, 500)
                continue
            if filled == "filled":
                self._page_wait(page, 400)
                return
            self._page_wait(page, 500)
        raise RuntimeError("最终注册页资料填写失败")

    @staticmethod
    def _find_turnstile_widget(
        page,
    ) -> Tuple[Optional[Any], Optional[dict[str, Any]]]:
        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url:
                continue
            try:
                frame_el = frame.frame_element()
                box = frame_el.bounding_box()
            except Exception:
                box = None
            if box and box["width"] > 100 and box["height"] >= 50:
                return frame, box
        return None, None

    @staticmethod
    def _read_turnstile_widget_ids(page) -> list[str]:
        try:
            values = page.evaluate(
                r"""() => {
                    const ids = new Set();
                    for (const input of document.querySelectorAll('input[id^="cf-chl-widget-"]')) {
                        const match = String(input.id || '').match(/^cf-chl-widget-([A-Za-z0-9_-]+)_response$/);
                        if (match && match[1]) ids.add(match[1]);
                    }
                    for (const node of document.querySelectorAll('[data-widget-id], [data-widgetid]')) {
                        const value = String(
                            node.getAttribute('data-widget-id') ||
                            node.getAttribute('data-widgetid') ||
                            ''
                        ).trim();
                        if (value) ids.add(value);
                    }
                    return Array.from(ids);
                }"""
            )
        except Exception:
            return []
        return [
            str(item or "").strip()
            for item in (values or [])
            if str(item or "").strip()
        ]

    @staticmethod
    def _extract_turnstile_sitekey_from_url(url: str) -> str:
        value = str(url or "").strip()
        if not value:
            return ""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(value)
            segments = [segment for segment in parsed.path.split("/") if segment]
            for segment in segments:
                if _TURNSTILE_SITEKEY_RE.match(segment):
                    return segment
        except Exception:
            return ""
        return ""

    @staticmethod
    def _read_turnstile_token(page) -> str:
        return str(
            page.evaluate(
                r"""() => {
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
                    const fromInput =
                      document.querySelector('input[id^="cf-chl-widget-"]')?.value ||
                      document.querySelector('input[name="cf-turnstile-response"]')?.value ||
                      '';
                    if (fromInput) return String(fromInput || '').trim();
                    try {
                      if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                        for (const widgetId of widgetIds) {
                          const response = String(window.turnstile.getResponse(widgetId) || '').trim();
                          if (response) return response;
                        }
                        return String(window.turnstile.getResponse() || '').trim();
                      }
                    } catch (_) {}
                    return '';
                }"""
            )
            or ""
        ).strip()

    @staticmethod
    def _read_turnstile_sitekey(page) -> str:
        inline_sitekey = str(
            page.evaluate(
                """() => {
                    const byData = document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey');
                    if (byData) return byData;

                    for (const iframe of document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]')) {
                        try {
                            const parsed = new URL(iframe.src, location.href);
                            const sitekey = parsed.searchParams.get('k');
                            if (sitekey) return sitekey;
                        } catch (_) {}
                    }
                    return '';
                }"""
            )
            or ""
        ).strip()
        if inline_sitekey:
            return inline_sitekey

        try:
            frame_urls = [
                str(frame.url or "")
                for frame in getattr(page, "frames", []) or []
                if "challenges.cloudflare.com" in str(getattr(frame, "url", "") or "")
            ]
        except Exception:
            frame_urls = []

        for url in frame_urls:
            sitekey = GrokRegister._extract_turnstile_sitekey_from_url(url)
            if sitekey:
                return sitekey

        iframe_urls = page.evaluate(
            """() => {
                return Array.from(document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]'))
                  .map((iframe) => String(iframe.getAttribute('src') || ''));
            }"""
        ) or []
        for url in iframe_urls:
            sitekey = GrokRegister._extract_turnstile_sitekey_from_url(url)
            if sitekey:
                return sitekey
        return ""

    @staticmethod
    def _has_turnstile_error(page) -> bool:
        keywords = [
            "验证失败",
            "故障排除",
            "verification failed",
            "troubleshoot",
            "try again",
        ]
        texts = []
        try:
            texts.append(page.locator("body").inner_text(timeout=800))
        except Exception:
            pass

        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url:
                continue
            try:
                texts.append(frame.locator("body").inner_text(timeout=500))
            except Exception:
                continue

        merged = "\n".join(texts).lower()
        return any(keyword.lower() in merged for keyword in keywords)

    @staticmethod
    def _inject_turnstile_token(page, token: str) -> bool:
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

    def _wait_turnstile_token(
        self, page, wait_rounds: int = 25, wait_ms: int = 500
    ) -> str:
        for _ in range(wait_rounds):
            token = self._read_turnstile_token(page)
            if len(token) >= TURNSTILE_TOKEN_MIN_LENGTH:
                return token
            self._page_wait(page, wait_ms)
        return ""

    @staticmethod
    def _has_turnstile_runtime(page) -> bool:
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
                    """() => {
                        return Boolean(
                            (window.turnstile && typeof window.turnstile.render === 'function') ||
                            document.querySelector('iframe[src*="challenges.cloudflare.com"]')
                        );
                    }"""
                )
            )
        except Exception:
            return False

    @staticmethod
    def _reset_turnstile_widget(page) -> bool:
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

    def _read_turnstile_state_signature(self, page) -> tuple[Any, ...]:
        _, box = self._find_turnstile_widget(page)
        frame_url = ""
        frame_body = ""
        for frame in getattr(page, "frames", []) or []:
            url = str(getattr(frame, "url", "") or "")
            if "challenges.cloudflare.com" not in url:
                continue
            frame_url = url
            try:
                frame_body = " ".join(
                    str(frame.locator("body").inner_text(timeout=400) or "").split()
                )[:160]
            except Exception:
                frame_body = ""
            break
        return (
            min(len(self._read_turnstile_token(page)), TURNSTILE_TOKEN_MIN_LENGTH),
            bool(box),
            self._has_turnstile_runtime(page),
            frame_url,
            frame_body,
        )

    @staticmethod
    def _capture_storage_snapshot(page) -> dict[str, Any]:
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

    def _collect_turnstile_session_state(self, page) -> dict[str, Any]:
        try:
            cookies = page.context.cookies()
        except Exception:
            cookies = []
        storage_snapshot = self._capture_storage_snapshot(page)
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
            "userAgent": str(runtime.get("userAgent") or UA),
            "viewport": {
                "width": int(viewport.get("width") or 1400),
                "height": int(viewport.get("height") or 1200),
            },
            "locale": str(runtime.get("locale") or "en-US"),
            "timezoneId": str(runtime.get("timezoneId") or ""),
        }

    def _collect_turnstile_widget_hints(self, page) -> dict[str, Any]:
        frame, box = self._find_turnstile_widget(page)
        frame_url = ""
        if frame is not None:
            frame_url = str(getattr(frame, "url", "") or "")
        if not frame_url:
            for candidate in getattr(page, "frames", []) or []:
                url = str(getattr(candidate, "url", "") or "")
                if "challenges.cloudflare.com" in url:
                    frame_url = url
                    break
        hints: dict[str, Any] = {
            "responseInputSelector": 'input[name="cf-turnstile-response"]',
        }
        widget_ids = self._read_turnstile_widget_ids(page)
        if widget_ids:
            hints["widgetIds"] = widget_ids
        if frame_url:
            hints["frameUrl"] = frame_url
        if box:
            hints["widgetBox"] = box
        return hints

    def _collect_turnstile_runtime_hints(self, page) -> dict[str, Any]:
        hints: dict[str, Any] = {
            "stepLabel": "grok_signup_step5",
            "tokenMinLength": TURNSTILE_TOKEN_MIN_LENGTH,
            "runtimeReady": self._has_turnstile_runtime(page),
        }
        body_text = _safe_body_text(page, limit=220)
        if body_text:
            hints["pageBodyText"] = body_text
        return hints

    def _collect_turnstile_solver_proxy(self) -> Optional[dict[str, str]]:
        if not self.proxy:
            return None
        try:
            return build_playwright_proxy_config(self.proxy)
        except Exception:
            return None

    def _resolve_flaresolverr_endpoint(self) -> str:
        candidates = [
            self.extra.get("grok_flaresolverr_url"),
            self.extra.get("flaresolverr_url"),
            os.getenv("GROK_FLARESOLVERR_URL"),
            os.getenv("FLARESOLVERR_URL"),
            DEFAULT_FLARESOLVERR_URL,
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if not value:
                continue
            return value if value.endswith("/v1") else f"{value.rstrip('/')}/v1"
        return ""

    def _resolve_browser_user_agent(self) -> str:
        cached = str(getattr(self, "_browser_user_agent", "") or "").strip()
        if cached:
            return cached
        endpoint = self._resolve_flaresolverr_endpoint()
        if endpoint:
            root_url = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
            try:
                resp = requests.get(root_url, timeout=10)
                resp.raise_for_status()
                payload = resp.json() if resp.content else {}
                ua = str((payload or {}).get("userAgent") or "").strip()
                if ua:
                    self._browser_user_agent = ua
                    return ua
            except Exception:
                pass
        self._browser_user_agent = UA
        return UA

    def _build_browser_identity_override(self, browser_user_agent: str) -> dict[str, Any]:
        ua = str(browser_user_agent or "").strip() or UA
        match = _CHROME_VERSION_RE.search(ua)
        major = str((match.group("major") if match else "") or "142")
        full_version = ".".join(
            (
                str((match.group("major") if match else "") or "142"),
                str((match.group("minor") if match else "") or "0"),
                str((match.group("build") if match else "") or "0"),
                str((match.group("patch") if match else "") or "0"),
            )
        )
        accept_language = str(
            self.extra.get("grok_browser_accept_language") or "en-US,en;q=0.9"
        ).strip() or "en-US,en;q=0.9"
        return {
            "userAgent": ua,
            "acceptLanguage": accept_language,
            "platform": "Linux x86_64",
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Google Chrome", "version": major},
                    {"brand": "Chromium", "version": major},
                    {"brand": "Not/A)Brand", "version": "99"},
                ],
                "fullVersion": full_version,
                "platform": "Linux",
                "platformVersion": "6.0.0",
                "architecture": "x86",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        }

    def _apply_browser_identity(self, context, page, browser_user_agent: str) -> None:
        try:
            cdp = context.new_cdp_session(page)
            cdp.send(
                "Emulation.setUserAgentOverride",
                self._build_browser_identity_override(browser_user_agent),
            )
            self.log("浏览器身份已对齐到 FlareSolverr Chrome 指纹")
        except Exception as exc:
            self.log(f"[Debug] 浏览器身份对齐失败，继续原链: {exc}")

    def _collect_flaresolverr_proxy_url(self) -> Optional[str]:
        proxy_cfg = self._collect_turnstile_solver_proxy()
        if not proxy_cfg:
            return None
        server = str(proxy_cfg.get("server") or "").strip()
        if not server:
            return None
        server = self._normalize_flaresolverr_proxy_server(server)
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

    def _resolve_flaresolverr_loopback_proxy_host(self) -> str:
        enabled = str(
            self.extra.get("grok_flaresolverr_bridge_loopback_proxy", "true") or "true"
        ).strip().lower()
        if enabled in _DISABLED_VALUES:
            return ""
        value = str(
            self.extra.get("grok_flaresolverr_loopback_proxy_host")
            or os.getenv("GROK_FLARESOLVERR_LOOPBACK_PROXY_HOST")
            or "host.docker.internal"
        ).strip()
        if value.lower() in _DISABLED_VALUES:
            return ""
        return value

    def _normalize_flaresolverr_proxy_server(self, server: str) -> str:
        parts = urlsplit(server)
        host = str(parts.hostname or "").strip()
        if not parts.scheme or not host or parts.port is None:
            return server
        if host.lower() not in _LOCAL_LOOPBACK_HOSTS:
            return server
        bridge_host = self._resolve_flaresolverr_loopback_proxy_host()
        if not bridge_host or bridge_host.lower() == host.lower():
            return server
        if ":" in bridge_host and not bridge_host.startswith("["):
            netloc = f"[{bridge_host}]:{parts.port}"
        else:
            netloc = f"{bridge_host}:{parts.port}"
        normalized = parts._replace(netloc=netloc).geturl()
        self.log(
            "  FlareSolverr 代理为本机回环地址，已改写为"
            f" {normalized} 以便容器内浏览器访问"
        )
        return normalized

    @staticmethod
    def _extract_flaresolverr_error_detail(response: Any) -> str:
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

    def _raise_for_flaresolverr_status(self, response: Any, context: str) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = getattr(response, "status_code", "")
            detail = self._extract_flaresolverr_error_detail(response)
            suffix = f": {detail}" if detail else f": {exc}"
            raise RuntimeError(f"{context} HTTP {status_code}{suffix}") from exc

    @staticmethod
    def _extract_flaresolverr_turnstile_token(solution: Any) -> str:
        if not isinstance(solution, dict):
            return ""
        for key in ("turnstileToken", "turnstile_token", "token"):
            value = str(solution.get(key) or "").strip()
            if len(value) >= TURNSTILE_TOKEN_MIN_LENGTH:
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

    def _apply_flaresolverr_cookies(self, page, cookies: list[dict[str, Any]]) -> list[str]:
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
            domain = str(raw_cookie.get("domain") or "accounts.x.ai").strip() or "accounts.x.ai"
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

    def _request_flaresolverr_solution(
        self,
        *,
        stage_label: str,
        target_url: str,
    ) -> dict[str, Any]:
        endpoint = self._resolve_flaresolverr_endpoint()
        if not endpoint:
            raise RuntimeError("未配置可用的 FlareSolverr endpoint")
        proxy_url = self._collect_flaresolverr_proxy_url()
        proxy_label = "task" if proxy_url else "none"
        self.log(
            f"  {stage_label}: 调用 FlareSolverr 预热 x.ai 会话态 (proxy={proxy_label})"
        )
        session_id = f"grok-flare-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        http = requests.Session()
        http.trust_env = False
        solution: dict[str, Any] = {}
        max_attempts = max(
            1,
            int(self.extra.get("grok_flaresolverr_attempts") or 3),
        )
        try:
            create_payload: dict[str, Any] = {
                "cmd": "sessions.create",
                "session": session_id,
            }
            if proxy_url:
                create_payload["proxy"] = {"url": proxy_url}
            create_resp = http.post(endpoint, json=create_payload, timeout=30)
            self._raise_for_flaresolverr_status(
                create_resp,
                "FlareSolverr 创建 session 失败",
            )
            create_data = create_resp.json()
            if str(create_data.get("status") or "").lower() != "ok":
                raise RuntimeError(create_data.get("message") or "FlareSolverr 创建 session 失败")

            for attempt in range(1, max_attempts + 1):
                self._checkpoint()
                req_payload = {
                    "cmd": "request.get",
                    "session": session_id,
                    "url": target_url,
                    "maxTimeout": 120000,
                }
                resp = http.post(endpoint, json=req_payload, timeout=150)
                self._raise_for_flaresolverr_status(
                    resp,
                    "FlareSolverr 请求失败",
                )
                data = resp.json()
                if str(data.get("status") or "").lower() != "ok":
                    raise RuntimeError(data.get("message") or "FlareSolverr 请求失败")
                solution = data.get("solution") or {}
                cookie_names = sorted(
                    {
                        str(cookie.get("name") or "").strip()
                        for cookie in (solution.get("cookies") or [])
                        if str(cookie.get("name") or "").strip()
                    }
                )
                self.log(f"  {stage_label}: FlareSolverr attempt {attempt}: cookies={cookie_names}")
                if "cf_clearance" in cookie_names:
                    break
                if (
                    attempt < max_attempts
                    and _FLARESOLVERR_COOKIE_MARKERS.intersection(cookie_names)
                ):
                    self.log(
                        f"  {stage_label}: 尚未拿到 cf_clearance，继续预热 FlareSolverr 会话态"
                    )
                    self._sleep_with_checkpoint(1.0)
                    continue
                if attempt >= max_attempts and cookie_names:
                    self.log(
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

    def _prewarm_session_with_flaresolverr(
        self,
        page,
        *,
        stage_label: str,
        target_url: str,
        reload_after: bool = False,
    ) -> dict[str, Any]:
        solution = self._request_flaresolverr_solution(
            stage_label=stage_label,
            target_url=target_url,
        )
        injected_names = self._apply_flaresolverr_cookies(
            page,
            solution.get("cookies") or [],
        )
        solution["injectedCookieNames"] = sorted(set(injected_names))
        if injected_names:
            self.log(
                f"  {stage_label}: FlareSolverr cookies 已注入当前 Patchright 上下文:"
                f" {sorted(set(injected_names))}"
            )
        if reload_after:
            self.log(f"  {stage_label}: 已注入会话态，刷新当前页面以重建 x.ai 验证组件")
            page.reload(wait_until="domcontentloaded", timeout=90000)
            self._page_wait(page, 1500)
        return solution

    def _page_has_email_input(self, page) -> bool:
        try:
            locator = page.locator(
                'input[type="email"]:visible, input[name="email"]:visible, input[autocomplete="email"]:visible'
            )
            if locator.count() <= 0:
                # fallback without :visible for older engines
                locator = page.locator('input[type="email"], input[name="email"]')
            return locator.count() > 0 and locator.first.is_visible()
        except Exception:
            try:
                return page.locator("input[type=email]").count() > 0
            except Exception:
                return False

    def _restore_profile_page_after_flaresolverr_reload(
        self,
        page,
        email: str,
        code: str,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> None:
        deadline = time.monotonic() + 35
        retried_email = False
        retried_otp = False
        while time.monotonic() < deadline:
            self._checkpoint()
            blocked_detail = self._detect_blocked_signup_page(page)
            if blocked_detail:
                raise RuntimeError(
                    f"Step5 前 FlareSolverr 预热后页面被 Cloudflare/WAF 封禁: {blocked_detail}"
                )
            if self._page_has_profile_form(page):
                return
            gate_state = self._signup_gate_state(page)
            if gate_state == "email_button":
                self.log("  Step5 前预热后回到注册入口，重新选择邮箱注册")
                self._ensure_email_signup_form(
                    page,
                    timeout=12,
                    stage_label="Step5 前预热后",
                )
                continue
            if self._page_has_email_input(page) and not retried_email:
                retried_email = True
                self.log("  Step5 前预热后回到邮箱页，重新提交邮箱以恢复资料页")
                self._submit_email(page, email)
                continue
            if self._page_has_otp_form(page) and not retried_otp:
                retried_otp = True
                fresh_code = ""
                if otp_callback is not None:
                    self.log("  Step5 前预热后回到验证码页，重新拉取最新验证码")
                    fresh_code = str(otp_callback() or "").replace("-", "").replace(" ", "").strip()
                if not fresh_code:
                    fresh_code = code
                    self.log("  Step5 前预热后未拿到新验证码，回退到原验证码重试")
                else:
                    self.log(f"  Step5 前预热后新验证码: {fresh_code}")
                self._submit_otp(page, fresh_code)
                continue
            self._page_wait(page, 700)
        raise RuntimeError("Step5 前 FlareSolverr 预热后未恢复到资料填写页")

    def _prewarm_before_signup(self, page) -> None:
        try:
            self._prewarm_session_with_flaresolverr(
                page,
                stage_label="Step1 前",
                target_url=SIGNUP_URL,
                reload_after=False,
            )
        except Exception as exc:
            self.log(f"  Step1 前 FlareSolverr 预热失败，继续原链: {exc}")

    def _prewarm_before_turnstile(
        self,
        page,
        email: str,
        code: str,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> None:
        current_url = str(getattr(page, "url", "") or "").strip() or SIGNUP_URL
        solution = self._prewarm_session_with_flaresolverr(
            page,
            stage_label="Step5 前",
            target_url=current_url,
            reload_after=True,
        )
        self._restore_profile_page_after_flaresolverr_reload(
            page,
            email,
            code,
            otp_callback=otp_callback,
        )
        token = self._extract_flaresolverr_turnstile_token(solution)
        if token and self._inject_turnstile_token(page, token):
            self._page_wait(page, 400)

    def _solve_turnstile_by_flaresolverr(self, page) -> str:
        current_url = str(getattr(page, "url", "") or "").strip() or SIGNUP_URL
        solution = self._prewarm_session_with_flaresolverr(
            page,
            stage_label="Step5 卡住后",
            target_url=current_url,
            reload_after=False,
        )
        token = self._extract_flaresolverr_turnstile_token(solution)
        if token and self._inject_turnstile_token(page, token):
            self._page_wait(page, 400)
            return self._read_turnstile_token(page) or token
        self._reset_turnstile_widget(page)
        self._page_wait(page, 700)
        token, _ = self._reuse_turnstile_on_current_page(page)
        if token:
            return token
        return self._read_turnstile_token(page)

    @staticmethod
    def _patch_turnstile_frame_mouse_event(frame) -> bool:
        try:
            return bool(
                frame.evaluate(
                    """() => {
                        try {
                            const sx = Math.floor(800 + Math.random() * 401);
                            const sy = Math.floor(400 + Math.random() * 301);
                            Object.defineProperty(MouseEvent.prototype, 'screenX', {
                                configurable: true,
                                value: sx,
                            });
                            Object.defineProperty(MouseEvent.prototype, 'screenY', {
                                configurable: true,
                                value: sy,
                            });
                            return true;
                        } catch (_) {}
                        return false;
                    }"""
                )
            )
        except Exception:
            return False

    @staticmethod
    def _click_turnstile_shadow_checkbox(frame) -> str:
        try:
            return str(
                frame.evaluate(
                    r"""() => {
                        function dispatchClick(node) {
                            if (!node) return false;
                            try {
                                const rect = node.getBoundingClientRect();
                                const clientX = rect.left + Math.max(Math.min(rect.width / 2, rect.width - 4), 4);
                                const clientY = rect.top + Math.max(Math.min(rect.height / 2, rect.height - 4), 4);
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

                        function findShadowClickTarget(root) {
                            if (!root || typeof root.querySelector !== 'function') return null;
                            const selectors = [
                                'input[type="checkbox"]',
                                '[role="checkbox"]',
                                'input',
                                'label',
                                'button',
                            ];
                            for (const selector of selectors) {
                                const node = root.querySelector(selector);
                                if (node) return node;
                            }
                            return null;
                        }

                        try {
                            const body = document.body;
                            const bodyShadow = body && body.shadowRoot;
                            const bodyTarget = findShadowClickTarget(bodyShadow);
                            if (bodyTarget && dispatchClick(bodyTarget)) {
                                return 'frame-body-shadow-target';
                            }

                            const walker = document.createTreeWalker(document, NodeFilter.SHOW_ELEMENT);
                            while (walker.nextNode()) {
                                const node = walker.currentNode;
                                if (!node || !node.shadowRoot) continue;
                                const target = findShadowClickTarget(node.shadowRoot);
                                if (target && dispatchClick(target)) {
                                    return 'frame-shadow-host-target';
                                }
                            }

                            const directTarget =
                                document.querySelector('input[type="checkbox"]') ||
                                document.querySelector('[role="checkbox"]') ||
                                document.querySelector('label') ||
                                document.querySelector('button');
                            if (directTarget && dispatchClick(directTarget)) {
                                return 'frame-direct-target';
                            }
                        } catch (_) {}
                        return '';
                    }"""
                )
                or ""
            ).strip()
        except Exception:
            return ""

    @staticmethod
    def _kick_turnstile_widget(page) -> str:
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
                                const clientX = rect.left + Math.min(Math.max(rect.width * 0.18, 12), Math.max(rect.width - 6, 12));
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

                        const nodes = Array.from(document.querySelectorAll('div, span, iframe, label, button'))
                            .filter((node) => {
                                if (!isVisible(node)) return false;
                                const text = [
                                    node.className || '',
                                    node.id || '',
                                    node.getAttribute?.('src') || '',
                                    node.getAttribute?.('title') || '',
                                ].join(' ').toLowerCase();
                                return text.includes('turnstile') || text.includes('cf-chl-widget');
                            });
                        if (nodes.length && dispatchClick(nodes[0])) {
                            return 'turnstile-visible-node';
                        }
                        return '';
                    }"""
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _reuse_turnstile_on_current_page(self, page) -> tuple[str, str]:
        last_error = "Turnstile 同页复用未返回 token"
        previous_signature: tuple[Any, ...] | None = None
        for attempt in range(1, 4):
            token = self._wait_turnstile_token(page, wait_rounds=1, wait_ms=1)
            if token:
                self.log(f"  Turnstile token: {token[:40]}...")
                return token, ""

            frame, box = self._find_turnstile_widget(page)
            if not box:
                host_action = self._kick_turnstile_widget(page)
                action_suffix = f"，已尝试触发 placeholder({host_action})" if host_action else ""
                self.log(
                    f"  Turnstile reuse #{attempt}: 未找到可点击 iframe，等待组件继续加载{action_suffix}"
                )
                last_error = "未找到可点击的 Turnstile iframe"
                if host_action:
                    self._page_wait(page, 900)
                token = self._wait_turnstile_token(page, wait_rounds=8, wait_ms=450)
                if token:
                    self.log(f"  Turnstile token: {token[:40]}...")
                    return token, ""
                self._page_wait(page, 900 + attempt * 180)
                continue

            click_offset_x = min(28, max(18, box["width"] * 0.08))
            click_x = box["x"] + click_offset_x
            click_y = box["y"] + box["height"] / 2

            host_action = self._kick_turnstile_widget(page)
            action_desc = host_action or "widget-click"
            self.log(
                f"  Turnstile reuse #{attempt}: ({click_x:.1f}, {click_y:.1f}) via {action_desc}"
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
                    self._page_wait(page, 150)
                page.mouse.move(click_x, click_y)
                page.mouse.down()
                self._page_wait(page, 120)
                page.mouse.up()
            except Exception as exc:
                last_error = str(exc)

            # 真实 widget 会先经历 Cloudflare browser-check，再异步把 token 写回隐藏 input。
            # 这里必须在首次点击后长等，过早二次点击/原生补点会打断已接近成功的验证流程。
            token = self._wait_turnstile_token(page, wait_rounds=36, wait_ms=450)
            if token:
                self.log(f"  Turnstile token: {token[:40]}...")
                return token, ""

            if attempt == 3 and not self.headless:
                self.log(
                    f"  Turnstile reuse #{attempt}: 同页点击后仍无 token，最后尝试 native follow-up ({click_x:.1f}, {click_y:.1f})"
                )
                try:
                    token = self._native_click_turnstile(page, box, click_offset_x)
                    if token:
                        self.log(f"  Turnstile token: {token[:40]}...")
                        return token, ""
                except Exception as exc:
                    last_error = str(exc)

            has_turnstile_error = self._has_turnstile_error(page)
            signature = self._read_turnstile_state_signature(page)
            if not has_turnstile_error and signature == previous_signature:
                last_error = (
                    "Turnstile 页面状态未变化，当前 x.ai 验证未被同页复用链路推进"
                )
                self.log("  Turnstile 状态连续未变化，同页复用未推进组件")
                break
            previous_signature = signature

            if has_turnstile_error:
                self.log("  检测到 Turnstile 验证失败提示，重置当前页 widget 后重试...")
                self._reset_turnstile_widget(page)
            self._page_wait(page, 900 + attempt * 180)

        return "", last_error

    def _solve_turnstile_by_same_session_solver(self, page) -> str:
        if not self.captcha_solver:
            return ""
        solver_name = type(self.captcha_solver).__name__.lower()
        if "manual" in solver_name:
            return ""
        solve_turnstile_session = getattr(
            self.captcha_solver, "solve_turnstile_session", None
        )
        if not callable(solve_turnstile_session):
            self.log("  当前验证码服务不支持同会话 Turnstile 兜底")
            return ""
        client_key = getattr(self.captcha_solver, "client_key", None)
        if client_key is not None and not str(client_key).strip():
            self.log("  未配置 YesCaptcha key，跳过同会话验证码服务兜底")
            return ""
        sitekey = self._read_turnstile_sitekey(page)
        if not sitekey:
            self.log("  未提取到 Turnstile sitekey，跳过同会话验证码服务兜底")
            return ""
        session_state = self._collect_turnstile_session_state(page)
        widget_hints = self._collect_turnstile_widget_hints(page)
        runtime_hints = self._collect_turnstile_runtime_hints(page)
        browser_proxy = self._collect_turnstile_solver_proxy()
        proxy_label = "task" if browser_proxy else "none"
        self.log(
            f"  同页复用未完成，调用同会话 Turnstile solver 兜底 (sitekey={sitekey[:8]}..., proxy={proxy_label})"
        )
        solution = solve_turnstile_session(
            page.url,
            sitekey,
            session_state=session_state,
            widget_hints=widget_hints,
            runtime_hints=runtime_hints,
            browser_proxy=browser_proxy,
            options={
                "pageLoadTimeoutMs": 30000,
                "solveTimeoutMs": 90000,
                "maxAttempts": 2,
                "captureDebugArtifacts": True,
            },
            interrupt_checker=self._checkpoint,
        )
        if isinstance(solution, dict):
            token = str(solution.get("token") or "").strip()
            if token:
                self.log(
                    "  同会话 solver 返回 token"
                    f" (mode={solution.get('solverMode')}, attempts={solution.get('attempts')},"
                    f" proxyMode={solution.get('proxyMode')}, proxyServer={solution.get('proxyServer')},"
                    f" finalURL={solution.get('finalURL')})"
                )
            else:
                self.log("  同会话 solver 未返回 token")
        else:
            token = str(solution or "").strip()
        if not token:
            return ""
        if self._inject_turnstile_token(page, token):
            self._page_wait(page, 400)
            return self._read_turnstile_token(page) or token
        return ""

    def _native_click_turnstile(self, page, box, offset_x: float) -> str:
        try:
            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
        except Exception as exc:
            raise RuntimeError(f"当前系统不支持原生点击: {exc}") from exc

        page.bring_to_front()
        metrics = page.evaluate(
            """() => ({
                screenX,
                screenY,
                outerWidth,
                outerHeight,
                innerWidth,
                innerHeight,
                dpr: window.devicePixelRatio,
            })"""
        )

        border_x = max(0, (metrics["outerWidth"] - metrics["innerWidth"]) / 2)
        chrome_y = max(0, metrics["outerHeight"] - metrics["innerHeight"] - border_x)
        raw_x = metrics["screenX"] + border_x + box["x"] + offset_x
        raw_y = metrics["screenY"] + chrome_y + box["y"] + box["height"] / 2
        dpr = float(metrics.get("dpr") or 1.0)
        points = [(raw_x, raw_y)]
        if abs(dpr - 1.0) > 0.05:
            points.append((raw_x * dpr, raw_y * dpr))

        for idx, (screen_x, screen_y) in enumerate(points, start=1):
            self.log(f"  Native click #{idx}: ({screen_x:.1f}, {screen_y:.1f})")
            user32.SetCursorPos(int(screen_x), int(screen_y))
            self._sleep_with_checkpoint(0.15)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            self._sleep_with_checkpoint(0.12)
            user32.mouse_event(0x0004, 0, 0, 0, 0)

            token = self._wait_turnstile_token(page, wait_rounds=18, wait_ms=450)
            if token:
                return token

        raise RuntimeError("Native click 后仍未获取到 token")

    def _solve_turnstile_by_solver(self, page) -> str:
        if not self.captcha_solver:
            return ""
        solver_name = type(self.captcha_solver).__name__.lower()
        if "manual" in solver_name:
            return ""
        client_key = getattr(self.captcha_solver, "client_key", None)
        if client_key is not None and not str(client_key).strip():
            self.log("  未配置 YesCaptcha key，跳过验证码服务兜底")
            return ""
        sitekey = self._read_turnstile_sitekey(page)
        if not sitekey:
            self.log("  未提取到 Turnstile sitekey，跳过验证码服务兜底")
            return ""
        self.log(f"  兜底: 调用验证码服务解 Turnstile (sitekey={sitekey[:8]}...)")
        token = self.captcha_solver.solve_turnstile(
            page.url,
            sitekey,
            interrupt_checker=self._checkpoint,
        )
        if not token:
            return ""
        if self._inject_turnstile_token(page, token):
            self._page_wait(page, 400)
            return self._read_turnstile_token(page) or str(token or "").strip()
        return ""

    def _solve_turnstile_on_page(self, page) -> str:
        self.log("Step5: 点击页面内 Turnstile 复选框...")
        existing = self._wait_turnstile_token(page, wait_rounds=1, wait_ms=1)
        if existing:
            self.log(f"  Turnstile token 已存在: {existing[:40]}...")
            return existing

        try:
            self._wait_until(
                lambda: self._has_turnstile_runtime(page)
                or bool(self._find_turnstile_widget(page)[1])
                or len(self._read_turnstile_token(page)) >= TURNSTILE_TOKEN_MIN_LENGTH,
                timeout=12,
                interval=0.5,
                desc="等待 Turnstile 组件就绪超时",
                page=page,
            )
        except Exception:
            pass

        token, last_error = self._reuse_turnstile_on_current_page(page)
        if token:
            return token

        try:
            token = self._solve_turnstile_by_flaresolverr(page)
            if token:
                self.log(f"  Turnstile token(FlareSolverr 会话兜底): {token[:40]}...")
                return token
            last_error = (
                f"{last_error}；FlareSolverr 未推进 Turnstile"
                if last_error
                else "FlareSolverr 未推进 Turnstile"
            )
        except Exception as exc:
            flaresolverr_error = f"FlareSolverr 预热会话失败: {exc}"
            self.log(f"  {flaresolverr_error}")
            last_error = (
                f"{last_error}；{flaresolverr_error}"
                if last_error
                else flaresolverr_error
            )

        try:
            token = self._solve_turnstile_by_same_session_solver(page)
            if token:
                self.log(f"  Turnstile token(同会话兜底): {token[:40]}...")
                return token
            last_error = (
                f"{last_error}；同会话 Turnstile solver 未返回 token"
                if last_error
                else "同会话 Turnstile solver 未返回 token"
            )
        except Exception as exc:
            session_solver_error = f"同会话 Turnstile solver 失败: {exc}"
            self.log(f"  {session_solver_error}")
            last_error = (
                f"{last_error}；{session_solver_error}"
                if last_error
                else session_solver_error
            )

        try:
            token = self._solve_turnstile_by_solver(page)
            if token:
                self.log(f"  Turnstile token(兜底): {token[:40]}...")
                return token
        except Exception as exc:
            solver_error = f"外部 Turnstile solver 失败: {exc}"
            self.log(f"  {solver_error}")
            if not last_error:
                last_error = solver_error

        # Headed path: pause for human click when automation cannot mint a token.
        token = self._wait_for_manual_turnstile(page)
        if token:
            return token

        raise RuntimeError(last_error or "Turnstile 求解失败")

    def _manual_turnstile_enabled(self) -> bool:
        raw = str(
            (self.extra or {}).get("grok_manual_turnstile")
            or os.getenv("GROK_MANUAL_TURNSTILE")
            or "1"
        ).strip().lower()
        if raw in {"0", "false", "no", "off", "disabled"}:
            return False
        if raw in {"1", "true", "yes", "on"}:
            return True
        # auto / empty: headed only
        return not bool(self.headless)

    def _manual_turnstile_timeout_seconds(self) -> float:
        raw = (
            (self.extra or {}).get("grok_manual_turnstile_timeout")
            or os.getenv("GROK_MANUAL_TURNSTILE_TIMEOUT")
            or 300
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 300.0
        return max(5.0, min(value, 1800.0))

    def _wait_for_manual_turnstile(self, page, *, timeout: float | None = None) -> str:
        """Pause headed registration and wait for the user to solve Turnstile.

        Success conditions (any):
        - cf-turnstile-response token appears
        - auth cookies (sso) appear after user finishes flow
        - page navigates away from the signup form to account/tos success
        """
        if not self._manual_turnstile_enabled():
            return ""
        if self.headless:
            self.log("  无头模式跳过人工 Turnstile 等待（可设 grok_manual_turnstile=1 强制）")
            # still allow if explicitly forced above
            raw = str(
                (self.extra or {}).get("grok_manual_turnstile")
                or os.getenv("GROK_MANUAL_TURNSTILE")
                or ""
            ).strip().lower()
            if raw not in {"1", "true", "yes", "on"}:
                return ""

        wait_s = float(timeout if timeout is not None else self._manual_turnstile_timeout_seconds())
        deadline = time.monotonic() + wait_s
        self.log("=" * 56)
        self.log("【需要人工操作】自动 Turnstile 未通过")
        self.log("请在弹出的浏览器窗口中：")
        self.log("  1) 点击 Cloudflare / Turnstile 复选框（或完成挑战）")
        self.log("  2) 若可点「完成注册 / 继续」，请一并点击")
        self.log(f"程序将等待最多 {int(wait_s)} 秒，检测到 token 或登录 cookie 后自动继续")
        self.log("任务停止/跳过仍可通过任务控制生效")
        self.log("=" * 56)

        last_heartbeat = 0.0
        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                token = self._read_turnstile_token(page)
            except Exception:
                token = ""
            if len(token) >= TURNSTILE_TOKEN_MIN_LENGTH:
                self.log(f"  人工 Turnstile 成功: token len={len(token)}")
                return token

            try:
                cookies = page.context.cookies()
            except Exception:
                cookies = []
            if self._has_auth_cookies(cookies):
                self.log("  人工操作后已出现 sso cookie，视为挑战通过")
                return token or "manual-auth-cookie"

            try:
                url = str(getattr(page, "url", "") or "")
                path = urlsplit(url).path or ""
                # Avoid matching host "accounts.x.ai" — only real path changes.
                if (
                    path.startswith("/account")
                    or "/oauth2/device" in path
                    or path.rstrip("/").endswith("/account")
                ):
                    self.log(f"  人工操作后页面已跳转: {url}")
                    return token or "manual-navigated"
            except Exception:
                pass

            remaining = int(deadline - time.monotonic())
            if time.monotonic() - last_heartbeat >= 15:
                self.log(f"  …仍在等待人工 Turnstile（剩余约 {remaining}s）")
                last_heartbeat = time.monotonic()
            self._page_wait(page, 1000)

        self.log(f"  人工 Turnstile 等待超时（{int(wait_s)}s）")
        return ""

    def _click_email_register_button(self, page) -> bool:
        """Human click the email-step 注册 button."""
        try:
            btn = page.get_by_role("button", name="注册", exact=True)
            if self._human_click_locator(page, btn):
                return True
        except Exception:
            pass
        try:
            btn = page.locator('button[type="submit"]').first
            if self._human_click_locator(page, btn):
                return True
        except Exception:
            pass
        if self._click_text_button(
            page,
            ["注册", "Sign up", "Continue", "继续", "下一步", "Next"],
        ):
            return True
        try:
            page.locator('input[type="email"]').first.press("Enter")
            return True
        except Exception:
            return False

    @staticmethod
    def _has_transient_retry_error(page) -> bool:
        """Detect x.ai generic toast: 出了点问题 / Something went wrong / 请重试.

        Avoid matching bare Cloudflare "try again" / "verification failed" text
        inside the Turnstile iframe — those are handled by _has_turnstile_error.
        """
        body = _safe_body_text(page, limit=1400)
        if not body:
            return False
        if any(marker in body for marker in _TRANSIENT_RETRY_STRONG_MARKERS):
            return True
        # Chinese toast is usually "出了点问题，请重试" — accept 请重试 with nearby context.
        if "请重试" in body and (
            "出了点问题" in body
            or "出错" in body
            or "失败" in body
            or "错误" in body
            or "问题" in body
        ):
            return True
        lower = body.lower()
        if "something went wrong" in lower:
            return True
        if "please try again" in lower and (
            "wrong" in lower or "error" in lower or "problem" in lower
        ):
            return True
        return False

    def _refresh_castle_before_submit(self, page) -> None:
        """Refresh Castle request token before a register re-submit.

        Missing/stale Castle is a known cause of later Device OAuth invalid_grant
        even when SSO cookies are issued. Retry after a generic toast should not
        proceed without re-minting trust signals when possible.
        """
        try:
            from platforms.grok.castle import ensure_castle_on_page, resolve_castle_pk

            token = ensure_castle_on_page(
                page,
                pk=resolve_castle_pk(self.extra),
                log_fn=self.log,
            )
            if token:
                self.log(f"  提交前 Castle 已刷新 len={len(token)}")
        except Exception as exc:
            self.log(f"  提交前 Castle 刷新跳过: {exc}")

    def _register_turnstile_ready(self, page) -> tuple[bool, int]:
        """Return (ready, token_len). ready=True when no CF widget or token present."""
        try:
            info = page.evaluate(
                r"""() => {
                    const cfInput = document.querySelector(
                      'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                    );
                    const cfPresent = !!cfInput
                      || !!document.querySelector(
                        'iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]'
                      );
                    const token = String((cfInput && cfInput.value) || '').trim();
                    return { present: cfPresent, tokenLen: token.length };
                }"""
            ) or {}
        except Exception:
            return True, 0
        present = bool(info.get("present"))
        token_len = int(info.get("tokenLen") or 0)
        if not present:
            return True, token_len
        return token_len >= TURNSTILE_TOKEN_MIN_LENGTH, token_len

    def _click_register_submit(self, page) -> str:
        """Click final 完成注册 / Create account with trusted mouse when possible."""
        ready, token_len = self._register_turnstile_ready(page)
        if not ready:
            return f"wait-cloudflare:{token_len}"

        # Prefer real pointer events — synthetic node.click() is flaky on x.ai.
        for label in _REGISTER_SUBMIT_BUTTON_LABELS:
            try:
                locator = page.get_by_role("button", name=label, exact=False)
                if self._human_click_locator(page, locator):
                    return "clicked"
            except Exception:
                pass
        if self._click_text_button(page, list(_REGISTER_SUBMIT_BUTTON_LABELS)):
            return "clicked"

        return str(
            page.evaluate(
                r"""() => {
                    function isVisible(node) {
                        if (!node) return false;
                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const rect = node.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    }

                    const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
                    const cfPresent = !!cfInput
                      || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
                    if (cfPresent) {
                        const token = String((cfInput && cfInput.value) || '').trim();
                        if (token.length < 20) return `wait-cloudflare:${token.length}`;
                    }

                    const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
                        return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
                    });
                    const submitBtn = buttons.find((node) => {
                        const text = String(node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
                        return (
                            text.includes('完成注册') ||
                            text.includes('创建账户') ||
                            text.includes('createaccount') ||
                            text.includes('signup') ||
                            text.includes('sign up') ||
                            text.includes('continue') ||
                            text.includes('继续')
                        );
                    });
                    if (!submitBtn) return 'no-submit-button';
                    submitBtn.focus();
                    submitBtn.click();
                    return 'clicked';
                }"""
            )
            or ""
        )

    def _wait_register_submit_outcome(
        self, page, *, timeout: float = 12.0
    ) -> str:
        """Poll after final submit: success | transient_error | turnstile_error | timeout."""
        deadline = time.monotonic() + max(float(timeout), 1.0)
        saw_transient = False
        while time.monotonic() < deadline:
            self._checkpoint()
            if self._tos_or_account_or_cookie(page):
                return "success"
            if self._has_turnstile_error(page) and not self._has_transient_retry_error(
                page
            ):
                # Prefer classifying strong page toast as transient when both match.
                return "turnstile_error"
            if self._has_transient_retry_error(page):
                saw_transient = True
                # Toast can flash while navigation is already in flight.
                self._page_wait(page, 350)
                if self._tos_or_account_or_cookie(page):
                    return "success"
                if self._page_has_profile_form(page):
                    return "transient_error"
            self._page_wait(page, 350)

        if self._tos_or_account_or_cookie(page):
            return "success"
        if saw_transient and self._page_has_profile_form(page):
            return "transient_error"
        if self._has_turnstile_error(page):
            return "turnstile_error"
        return "timeout"

    def _tos_or_account_or_cookie(self, page) -> bool:
        url = str(getattr(page, "url", "") or "")
        body = _safe_body_text(page, limit=800)
        try:
            checkbox_count = page.locator("input[type=checkbox]").count()
        except Exception:
            checkbox_count = 0
        try:
            cookies = page.context.cookies()
        except Exception:
            cookies = []
        return (
            checkbox_count >= 2
            or "/accept-tos" in url
            or "/account" in url
            or "接受服务条款" in body
            or "您的账户" in body
            or self._has_auth_cookies(cookies)
        )

    def _account_ready(self, page) -> bool:
        url = str(getattr(page, "url", "") or "")
        body = _safe_body_text(page, limit=800)
        try:
            cookies = page.context.cookies()
        except Exception:
            cookies = []
        return (
            "/account" in url
            or "您的账户" in body
            or self._has_auth_cookies(cookies)
        )

    def _submit_register(self, page) -> None:
        """Submit final profile form with fast retry on generic toast.

        x.ai frequently shows「出了点问题，请重试」on the *first* click of
        完成注册; a second click on the same page usually succeeds. Waiting a
        full 18s before retry wastes Turnstile lifetime and can leave weaker
        trust signals — both correlate with later Device OAuth invalid_grant.
        """
        self.log("Step6: 提交完成注册...")
        last_error = "等待注册后跳转超时"
        max_attempts = 5
        for submit_attempt in range(1, max_attempts + 1):
            self._register_submit_meta["submit_attempts"] = submit_attempt

            if submit_attempt > 1:
                # Re-mint Castle between retries so a recovered submit still
                # carries a fresh trust token into the successful registration.
                self._refresh_castle_before_submit(page)

            ready, token_len = self._register_turnstile_ready(page)
            if not ready or self._has_turnstile_error(page):
                self.log(
                    f"  Step6: Turnstile 未就绪(token_len={token_len})，"
                    f"提交前重新求解 (attempt={submit_attempt})"
                )
                self._solve_turnstile_on_page(page)

            submit_state = self._click_register_submit(page)
            if submit_state.startswith("wait-cloudflare"):
                self.log(
                    f"  Step6: 点击时仍缺 Turnstile token({submit_state})，重解后再次点击"
                )
                self._solve_turnstile_on_page(page)
                submit_state = self._click_register_submit(page)
            if submit_state == "no-submit-button" and self._tos_or_account_or_cookie(
                page
            ):
                self._page_wait(page, 800)
                return
            if submit_state not in {"clicked", "no-submit-button"} and not str(
                submit_state
            ).startswith("wait-cloudflare"):
                self.log(f"  Step6: 提交按钮状态异常: {submit_state}")

            # First poll short: transient toast usually appears within 1–3s.
            outcome = self._wait_register_submit_outcome(
                page, timeout=6.0 if submit_attempt == 1 else 10.0
            )
            if outcome == "success":
                if self._register_submit_meta.get("saw_transient_error"):
                    self.log(
                        "  Step6: 此前出现「出了点问题/请重试」后重试提交已成功；"
                        "账号通常可用，但若后续 Device OAuth 出现 invalid_grant，"
                        "优先检查 Castle/代理一致性而非此 toast 本身"
                    )
                self._page_wait(page, 900)
                return

            if outcome == "transient_error":
                self._register_submit_meta["saw_transient_error"] = True
                self._register_submit_meta["transient_error_retries"] = int(
                    self._register_submit_meta.get("transient_error_retries", 0)
                ) + 1
                last_error = (
                    "完成注册出现通用错误提示（出了点问题/请重试），"
                    f"url={getattr(page, 'url', '')}, body={_safe_body_text(page)[:180]}"
                )
                self.log(
                    f"  Step6: {last_error}；"
                    f"立即第 {submit_attempt + 1}/{max_attempts} 次重试"
                    "（先刷新 Castle，必要时重过 Turnstile）"
                )
                # Brief pause so the toast/button settle; do not burn full timeout.
                self._page_wait(page, 600 + submit_attempt * 200)
                continue

            if outcome == "turnstile_error":
                last_error = "Cloudflare 验证失败"
            else:
                # Give a longer success window once before classifying as timeout.
                outcome2 = self._wait_register_submit_outcome(page, timeout=8.0)
                if outcome2 == "success":
                    self._page_wait(page, 900)
                    return
                if outcome2 == "transient_error":
                    self._register_submit_meta["saw_transient_error"] = True
                    self._register_submit_meta["transient_error_retries"] = int(
                        self._register_submit_meta.get("transient_error_retries", 0)
                    ) + 1
                    last_error = "完成注册出现通用错误提示（延迟出现）"
                    self.log(f"  Step6: {last_error}，继续重试")
                    self._page_wait(page, 700)
                    continue
                if outcome2 == "turnstile_error":
                    last_error = "Cloudflare 验证失败"
                else:
                    last_error = (
                        f"等待注册后跳转超时，url={getattr(page, 'url', '')}, "
                        f"body={_safe_body_text(page)[:200]}"
                    )

            if submit_attempt < max_attempts:
                self.log(
                    f"  Step6: 提交未完成({last_error})，"
                    f"刷新 Castle + Turnstile 后重试 ({submit_attempt + 1}/{max_attempts})"
                )
                self._refresh_castle_before_submit(page)
                self._solve_turnstile_on_page(page)

        raise RuntimeError(last_error)

    def _accept_tos_if_needed(self, page) -> None:
        try:
            self._wait_until(
                lambda: self._tos_or_account_or_cookie(page),
                timeout=12,
                interval=0.5,
                page=page,
            )
        except Exception:
            pass

        try:
            checkbox_count = page.locator("input[type=checkbox]").count()
        except Exception:
            checkbox_count = 0
        if checkbox_count < 2:
            self._page_wait(page, 2500)
            try:
                checkbox_count = page.locator("input[type=checkbox]").count()
            except Exception:
                checkbox_count = 0
            if checkbox_count < 2:
                return

        self.log("Step7: 接受 ToS ...")
        checkbox_labels = [
            "我确认已阅读并接受 企业服务条款，并知晓 隐私政策。",
            "我确认我已年满 18 岁。",
        ]
        for label in checkbox_labels:
            try:
                checkbox = page.get_by_role("checkbox", name=label)
                if not checkbox.is_checked():
                    checkbox.check()
            except Exception:
                pass

        clicked = self._click_text_button(page, ["继续", "continue"])
        if not clicked:
            try:
                page.get_by_role("button", name="继续").click()
            except Exception:
                pass

        self._wait_until(
            lambda: self._account_ready(page),
            timeout=20,
            interval=0.5,
            desc="等待账户页超时",
            page=page,
        )
        self._page_wait(page, 1500)

    def _wait_for_auth_cookies(self, page, timeout: float = 30.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        last_seen_names: set[str] = set()
        last_submit_retry = 0.0

        while time.monotonic() < deadline:
            self._checkpoint()
            try:
                cookies = page.context.cookies()
            except Exception:
                cookies = []
            for cookie in cookies:
                name = str(cookie.get("name", "") or "").strip()
                if name:
                    last_seen_names.add(name)
            if self._has_auth_cookies(cookies):
                return cookies

            now = time.monotonic()
            if now - last_submit_retry >= 2.5 and self._page_has_profile_form(page):
                submit_state = self._click_register_submit(page)
                if submit_state.startswith("wait-cloudflare"):
                    token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
                    self.log(f"[Debug] 最终页仍等待 Cloudflare 通过，当前 token 长度={token_len}")
                    self._solve_turnstile_on_page(page)
                    submit_state = self._click_register_submit(page)
                if submit_state == "clicked":
                    self.log("[Debug] 最终页仍停留在完成注册，已重试提交")
                last_submit_retry = now

            self._page_wait(page, 1000)

        raise RuntimeError(
            f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
        )

    @staticmethod
    def _pick_cookie(cookies: list, name: str) -> str:
        domains = [
            ".x.ai",
            "accounts.x.ai",
            ".grok.com",
            ".grokusercontent.com",
            ".grokipedia.com",
        ]
        for domain in domains:
            for cookie in cookies:
                if cookie.get("name") == name and cookie.get("domain") == domain:
                    return str(cookie.get("value", "") or "")
        for cookie in cookies:
            if cookie.get("name") == name:
                return str(cookie.get("value", "") or "")
        return ""

    def register(
        self,
        email: str,
        password: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        if not password:
            password = _rand_password()
        given_name = _rand_name()
        family_name = _rand_name()

        playwright = None
        browser = None
        context = None
        try:
            playwright, browser = self._launch_browser()
            context = browser.new_context(
                viewport={"width": 1400, "height": 1200},
            )
            page = context.new_page()

            self._prewarm_before_signup(page)
            self._goto_email_signup(page)
            self._submit_email(page, email)

            if not otp_callback:
                code = input("验证码: ").strip()
            else:
                self.log("等待验证码...")
                code = otp_callback() or ""
            if not code:
                raise RuntimeError("未获取到验证码")

            self._submit_otp(page, code)
            self._fill_user_form(page, given_name, family_name, password)
            self._solve_turnstile_on_page(page)
            self._submit_register(page)
            self._accept_tos_if_needed(page)

            cookies = context.cookies()
            if not self._has_auth_cookies(cookies):
                cookies = self._wait_for_auth_cookies(page, timeout=25)
            sso = self._pick_cookie(cookies, "sso")
            sso_rw = self._pick_cookie(cookies, "sso-rw")
            if not sso:
                raise RuntimeError("注册成功但未提取到 sso cookie")

            self.log(f"  ✅ sso={sso[:40]}...")
            if self._register_submit_meta.get("saw_transient_error"):
                self.log(
                    "  诊断: 注册按钮曾出现瞬态「请重试」toast "
                    f"(email_retries={self._register_submit_meta.get('email_transient_retries')}, "
                    f"final_retries={self._register_submit_meta.get('transient_error_retries')})。"
                    "这通常是单次请求失败而非永久封号；"
                    "若后续 Device OAuth invalid_grant，优先核对 Castle 与注册代理一致性。"
                )
            self.log("Grok 注册链路完成")
            return {
                "email": email,
                "password": password,
                "given_name": given_name,
                "family_name": family_name,
                "sso": sso,
                "sso_rw": sso_rw,
                "cookies": cookies,
                "register_submit_meta": dict(self._register_submit_meta),
            }
        finally:
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
