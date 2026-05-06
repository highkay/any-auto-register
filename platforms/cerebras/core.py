"""Cerebras 浏览器注册与 API key 获取核心逻辑。"""

from __future__ import annotations

import json
import random
import re
import string
import time
from typing import Any, Callable, Optional

from core.browser_runtime import (
    ensure_browser_display_available,
    resolve_browser_headless,
)
from core.proxy_utils import build_playwright_proxy_config


CEREBRAS_CLOUD_URL = "https://cloud.cerebras.ai/"
CEREBRAS_BASE_URL = "https://api.cerebras.ai"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
USE_CASE_LABELS = {
    "hobbyist": "Hobbyist",
    "student": "Student",
    "startup": "Startup",
    "enterprise": "Enterprise",
}
LIST_MY_PROJECTS_QUERY = """query ListMyProjects {
  ListMyProjects {
    id
    name
    state
    organizationType
    awsCustomerId
    role
    isEnabledProjects
    projects {
      id
      name
      role
      __typename
    }
    __typename
  }
}"""
GET_ORGANIZATION_QUERY = """query GetOrganization($organizationId: ID!) {
  GetOrganization(organizationId: $organizationId) {
    id
    name
    organizationType
    tier
    state
    promptCachingDisabled
    sendCachedTokens
    awsCustomerId
    isProjectsEnabled
    __typename
  }
}"""
LIST_ORGANIZATION_API_KEYS_QUERY = """query ListOrganizationApiKeys($organizationId: ID!, $projectId: ID) {
  ListOrganizationApiKeys(organizationId: $organizationId, projectId: $projectId) {
    id
    name
    secretKey
    projectId
    projectName
    state
    createdAt
    lastUsedAt
    __typename
  }
}"""
LIST_MY_REGIONS_QUERY = """query ListMyRegions {
  ListMyRegions {
    id
    name
    baseApiUrl
    __typename
  }
}"""


def _safe_body_text(page, limit: int = 1200) -> str:
    try:
        text = str(page.locator("body").inner_text(timeout=1500) or "").strip()
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _default_full_name(email: str) -> str:
    local = str(email or "").split("@", 1)[0]
    words = re.findall(r"[A-Za-z]+", local)
    cleaned = [word for word in words if word]
    if len(cleaned) >= 2:
        candidate = " ".join(word.capitalize() for word in cleaned[:3])
        if candidate:
            return candidate[:80]
    if len(cleaned) == 1:
        return f"{cleaned[0].capitalize()} User"[:80]
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"Aar User {suffix}"[:80]


class CerebrasRegister:
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

    def _launch_browser(self):
        from patchright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        headless, reason = resolve_browser_headless(
            self.headless, default_headless=True
        )
        ensure_browser_display_available(headless)
        self.log(f"浏览器模式: {'headless' if headless else 'headed'} ({reason})")

        launch_kwargs: dict[str, Any] = {"headless": headless, "channel": "msedge"}
        if self.proxy:
            proxy_cfg = build_playwright_proxy_config(self.proxy)
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception:
            launch_kwargs.pop("channel", None)
            browser = playwright.chromium.launch(**launch_kwargs)
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
        for _ in range(3):
            if self._click_text_button(page, ["cancel", "close", "save my preferences"]):
                page.wait_for_timeout(1000)

    @staticmethod
    def _is_cloudflare_region_ban(body: str) -> bool:
        text = str(body or "")
        return "Access denied" in text and "banned the country or region" in text

    def _open_landing(self, page) -> None:
        self.log("Step1: 打开 Cerebras Cloud ...")
        page.goto(CEREBRAS_CLOUD_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(2500)
        self._dismiss_cookie_banner(page)
        body = _safe_body_text(page)
        if self._is_cloudflare_region_ban(body):
            raise RuntimeError("Cerebras Cloud 当前出口被 Cloudflare 地区封禁，请使用可访问的非 CN 出口代理")

        def _email_ready() -> bool:
            locator = page.locator('input[type="email"]')
            return locator.count() > 0 and locator.first.is_visible()

        self._wait_until(
            _email_ready,
            timeout=30,
            interval=0.5,
            desc="等待 Cerebras 邮箱输入框超时",
            page=page,
        )

    def _click_continue_with_email(self, page) -> None:
        page.get_by_role("button", name="CONTINUE WITH EMAIL").click(timeout=10000)

    def _has_recaptcha_v2_challenge(self, page) -> bool:
        current_url = str(page.url or "").lower()
        if "userecaptchav2=true" in current_url:
            return True
        try:
            return (
                page.locator(
                    'iframe[src*="google.com/recaptcha/api2/anchor"][src*="size=normal"]'
                ).count()
                > 0
            )
        except Exception:
            return False

    def _extract_recaptcha_v2_sitekeys(self, page) -> dict[str, str]:
        payload = page.evaluate(
            """() => {
                const extractFromUrl = (raw) => {
                    try {
                        const parsed = new URL(String(raw || ''), location.href);
                        return parsed.searchParams.get('k') || parsed.searchParams.get('sitekey') || '';
                    } catch (_) {
                        return '';
                    }
                };

                const result = {
                    visible: '',
                    invisible: '',
                    enterprise: '',
                };

                for (const frame of document.querySelectorAll('iframe[src*="google.com/recaptcha/api2/anchor"]')) {
                    const src = frame.getAttribute('src') || '';
                    const key = extractFromUrl(src);
                    if (!key) continue;
                    try {
                        const parsed = new URL(src, location.href);
                        const size = String(parsed.searchParams.get('size') || '').toLowerCase();
                        if (size === 'invisible') {
                            if (!result.invisible) result.invisible = key;
                        } else if (!result.visible) {
                            result.visible = key;
                        }
                    } catch (_) {
                        if (!result.visible) result.visible = key;
                    }
                }

                for (const node of document.querySelectorAll('[data-sitekey]')) {
                    const key = String(node.getAttribute('data-sitekey') || '').trim();
                    if (key && !result.visible) {
                        result.visible = key;
                    }
                }

                for (const script of document.querySelectorAll('script[src*="google.com/recaptcha/enterprise.js"]')) {
                    const src = script.getAttribute('src') || '';
                    const key = extractFromUrl(src) || String(script.getAttribute('data-sitekey') || '').trim();
                    if (key && !result.enterprise) {
                        result.enterprise = key;
                    }
                }

                return result;
            }"""
        ) or {}
        return {
            "visible": str(payload.get("visible") or "").strip(),
            "invisible": str(payload.get("invisible") or "").strip(),
            "enterprise": str(payload.get("enterprise") or "").strip(),
        }

    def _inject_recaptcha_token(self, page, token: str) -> bool:
        result = page.evaluate(
            """(token) => {
                const ensured = [];
                const form = document.querySelector('form') || document.body;
                const selectors = [
                    'textarea[name="g-recaptcha-response"]',
                    'input[name="g-recaptcha-response"]',
                    'textarea[id*="g-recaptcha-response"]',
                    'input[id*="g-recaptcha-response"]',
                ];
                const fields = [];

                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach((node) => fields.push(node));
                }

                if (!fields.length) {
                    const area = document.createElement('textarea');
                    area.name = 'g-recaptcha-response';
                    area.style.display = 'none';
                    form.appendChild(area);
                    fields.push(area);
                    ensured.push('g-recaptcha-response');
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

                const seen = new Set();
                const visit = (node) => {
                    if (!node) return;
                    const nodeType = typeof node;
                    if (nodeType !== 'object' && nodeType !== 'function') return;
                    if (seen.has(node)) return;
                    seen.add(node);
                    for (const [key, value] of Object.entries(node)) {
                        if (key === 'callback' && typeof value === 'function') {
                            try {
                                value(token);
                                callbacks.push('___grecaptcha_cfg.callback');
                            } catch (_) {}
                            continue;
                        }
                        visit(value);
                    }
                };

                try {
                    visit(window.___grecaptcha_cfg?.clients || {});
                } catch (_) {}

                return {
                    fieldCount: fields.length,
                    ensured,
                    callbacks,
                };
            }""",
            token,
        )
        return bool((result or {}).get("fieldCount"))

    def _solve_recaptcha_v2(self, page) -> str:
        if not self.captcha_solver:
            raise RuntimeError("Cerebras 触发 reCAPTCHA v2 风控，但当前未配置可用验证码求解器")

        solver = self.captcha_solver
        solver_name = type(solver).__name__
        solve_method = getattr(solver, "solve_recaptcha_v2", None)
        if not callable(solve_method):
            raise RuntimeError(
                f"当前验证码求解器 {solver_name} 暂不支持 Cerebras reCAPTCHA v2，请改用 yescaptcha/OhMyCaptcha 或 manual"
            )

        client_key = getattr(solver, "client_key", None)
        if client_key is not None and not str(client_key).strip():
            raise RuntimeError("Cerebras 触发 reCAPTCHA v2 风控，但当前未配置 YesCaptcha/OhMyCaptcha key")

        sitekeys: dict[str, str] = {}
        body_text = ""

        def _sitekeys_ready() -> bool:
            nonlocal sitekeys, body_text
            body_text = _safe_body_text(page)
            sitekeys = self._extract_recaptcha_v2_sitekeys(page)
            return bool(
                sitekeys.get("visible")
                or sitekeys.get("invisible")
                or sitekeys.get("enterprise")
            )

        self._wait_until(
            _sitekeys_ready,
            timeout=15,
            interval=0.5,
            desc=f"Cerebras reCAPTCHA v2 页面未加载出 sitekey，url={page.url}, body={body_text}",
            page=page,
        )
        site_key = sitekeys.get("visible") or sitekeys.get("invisible") or sitekeys.get("enterprise") or ""
        if not site_key:
            raise RuntimeError(
                f"Cerebras 触发 reCAPTCHA v2 风控，但未提取到 sitekey，url={page.url}, body={body_text or _safe_body_text(page)}"
            )

        enterprise = not bool(sitekeys.get("visible") or sitekeys.get("invisible")) and bool(sitekeys.get("enterprise"))
        is_invisible = not bool(sitekeys.get("visible")) and bool(sitekeys.get("invisible"))
        self.log(f"  检测到 Cerebras reCAPTCHA v2 风控，尝试调用验证码服务 (sitekey={site_key[:8]}...)")

        try:
            token = solve_method(
                page.url,
                site_key,
                enterprise=enterprise,
                is_invisible=is_invisible,
                timeout_seconds=150,
                poll_interval_seconds=3,
                request_timeout_seconds=30,
                interrupt_checker=self._checkpoint,
            )
        except NotImplementedError as exc:
            raise RuntimeError(
                f"当前验证码求解器 {solver_name} 暂不支持 Cerebras reCAPTCHA v2，请改用 yescaptcha/OhMyCaptcha 或 manual"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Cerebras reCAPTCHA v2 求解失败: {exc}") from exc

        if not token:
            raise RuntimeError("Cerebras reCAPTCHA v2 求解返回空 token")
        if not self._inject_recaptcha_token(page, token):
            raise RuntimeError("Cerebras reCAPTCHA v2 token 注入失败")
        page.wait_for_timeout(1200)
        return token

    def _submit_email(self, page, email: str) -> None:
        self.log(f"Step2: 提交 Cerebras 邮箱 {email} ...")
        email_locator = page.locator('input[type="email"]').first
        email_locator.fill(email)
        self._click_continue_with_email(page)

        deadline = time.time() + 35
        recaptcha_solved_at = 0.0
        while time.time() < deadline:
            self._checkpoint()
            body = _safe_body_text(page)
            if self._is_cloudflare_region_ban(body):
                raise RuntimeError("Cerebras Cloud 当前出口被 Cloudflare 地区封禁，请使用可访问的非 CN 出口代理")
            if "Check your email" in body and email.lower() in body.lower():
                return
            if self._has_recaptcha_v2_challenge(page):
                if recaptcha_solved_at and (time.time() - recaptcha_solved_at) >= 12:
                    raise RuntimeError(
                        f"Cerebras 触发 reCAPTCHA v2 风控，求解后仍未进入检查邮箱页，url={page.url}, body={body}"
                    )
                if not recaptcha_solved_at:
                    self._solve_recaptcha_v2(page)
                    recaptcha_solved_at = time.time()
                    email_locator.fill(email)
                    self._click_continue_with_email(page)
                page.wait_for_timeout(600)
                continue
            page.wait_for_timeout(500)
        raise TimeoutError("等待 Cerebras 检查邮箱页超时")

    def _complete_magic_link(self, page, magic_link: str) -> None:
        self.log("Step3: 打开 Cerebras 激活链接 ...")
        page.goto(magic_link, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(2500)
        self._dismiss_cookie_banner(page)
        body = _safe_body_text(page)
        if "Complete Sign-in" not in body:
            raise RuntimeError(f"Cerebras magic-link 页面异常: {body}")
        page.get_by_role("button", name="Continue").click(timeout=10000)
        self._wait_for_post_signin_state(
            page,
            timeout=45,
            desc=f"Cerebras 登录后控制台加载超时，url={page.url}, body={_safe_body_text(page)}",
        )

    def _detect_post_signin_state(self, page) -> str:
        text = _safe_body_text(page)
        current_url = str(page.url or "")
        current_url_lower = current_url.lower()
        if "This organization does not exist" in text:
            return "missing_org"
        if "/apikeys" in current_url_lower or "API keys" in text:
            return "apikeys"
        if "/get-started" in current_url_lower or "Get started with Cerebras" in text:
            return "get_started"
        if "/onboarding" in current_url_lower and "Submitting..." in text:
            return "loading"
        if "Build with Cerebras" in text:
            return "plan"
        if "/onboarding" in current_url_lower and "Enter Details" in text:
            return "onboarding"
        if "/platform/" in current_url_lower and "Loading..." in text:
            return "loading"
        return ""

    def _wait_for_post_signin_state(
        self,
        page,
        *,
        timeout: float = 45.0,
        desc: str = "",
    ) -> str:
        def _ready() -> bool:
            return self._detect_post_signin_state(page) not in {"", "loading"}

        self._wait_until(
            _ready,
            timeout=timeout,
            interval=0.5,
            desc=desc
            or f"Cerebras 页面状态未就绪，url={page.url}, body={_safe_body_text(page)}",
            page=page,
        )
        return self._detect_post_signin_state(page)

    def _wait_for_allowed_post_signin_state(
        self,
        page,
        *,
        allowed_states: set[str],
        retryable_states: set[str] | None = None,
        timeout: float = 45.0,
        retry_action: Optional[Callable[[], None]] = None,
        retry_interval: float = 5.0,
    ) -> str:
        deadline = time.time() + timeout
        retryable = {state for state in (retryable_states or set()) if state}
        next_retry_at = time.time() + max(retry_interval, 0.5)
        last_state = ""
        while time.time() < deadline:
            self._checkpoint()
            last_state = self._detect_post_signin_state(page)
            if last_state in allowed_states:
                return last_state
            if last_state and last_state not in retryable and last_state != "loading":
                return last_state
            if retry_action and last_state in retryable and time.time() >= next_retry_at:
                try:
                    retry_action()
                except Exception:
                    pass
                next_retry_at = time.time() + max(retry_interval, 0.5)
            page.wait_for_timeout(500)
        return last_state

    def _fill_onboarding(self, page, *, email: str, full_name: str, use_case: str) -> None:
        state = self._wait_for_post_signin_state(
            page,
            timeout=35,
            desc=f"Cerebras onboarding 页面未就绪，url={page.url}, body={_safe_body_text(page)}",
        )
        if state != "onboarding":
            return
        self.log("Step4: 填写 Cerebras onboarding ...")
        page.locator('input[name="fullName"]').wait_for(state="visible", timeout=30000)
        page.locator('input[name="fullName"]').fill(full_name or _default_full_name(email))
        label = USE_CASE_LABELS.get(str(use_case or "").strip().lower(), "Hobbyist")
        page.get_by_role("button", name=label).click(timeout=10000)
        continue_button = page.get_by_role("button", name="Continue").first
        self._wait_until(
            lambda: continue_button.is_enabled(),
            timeout=10,
            interval=0.25,
            desc="等待 Cerebras onboarding Continue 按钮可点击超时",
            page=page,
        )
        continue_button.click(timeout=10000)
        next_state = self._wait_for_allowed_post_signin_state(
            page,
            allowed_states={"plan", "get_started", "apikeys", "missing_org"},
            retryable_states={"onboarding"},
            timeout=45,
            retry_action=lambda: continue_button.click(timeout=10000),
        )
        if next_state not in {"plan", "get_started", "apikeys", "missing_org"}:
            raise RuntimeError(
                f"Cerebras onboarding 提交后状态异常: state={next_state or 'unknown'} "
                f"url={page.url} body={_safe_body_text(page)}"
            )

    def _click_plan_get_started(self, page) -> None:
        buttons = page.get_by_role("button", name="Get Started")
        button_count = min(buttons.count(), 3)
        if button_count <= 0:
            raise RuntimeError("Cerebras 套餐页未找到 Get Started 按钮")

        last_error = None
        for idx in range(button_count):
            button = buttons.nth(idx)
            try:
                button.wait_for(state="visible", timeout=10000)
                button.click(timeout=10000)
                state = self._wait_for_allowed_post_signin_state(
                    page,
                    allowed_states={"get_started", "apikeys", "missing_org"},
                    retryable_states={"plan", "onboarding"},
                    timeout=20,
                    retry_action=lambda button=button: button.click(timeout=10000),
                    retry_interval=4,
                )
                if state in {"get_started", "apikeys", "missing_org"}:
                    return
                last_error = RuntimeError(
                    f"Cerebras 套餐页点击 Get Started 后状态异常: state={state or 'unknown'} "
                    f"url={page.url} body={_safe_body_text(page)}"
                )
            except Exception as exc:
                last_error = exc

        if self._click_text_button(page, ["Get Started"]):
            state = self._wait_for_allowed_post_signin_state(
                page,
                allowed_states={"get_started", "apikeys", "missing_org"},
                retryable_states={"plan", "onboarding"},
                timeout=20,
            )
            if state in {"get_started", "apikeys", "missing_org"}:
                return
            last_error = RuntimeError(
                f"Cerebras 套餐页文本按钮点击后状态异常: state={state or 'unknown'} "
                f"url={page.url} body={_safe_body_text(page)}"
            )

        if last_error is not None:
            raise last_error
        raise RuntimeError("Cerebras 套餐页点击 Get Started 失败")

    def _ensure_get_started(self, page) -> None:
        state = self._wait_for_post_signin_state(
            page,
            timeout=45,
            desc=f"等待 Cerebras 控制台页面超时，url={page.url}, body={_safe_body_text(page)}",
        )
        if state == "plan":
            self.log("Step5: 选择 Cerebras Free 套餐 ...")
            self._click_plan_get_started(page)
            state = self._wait_for_post_signin_state(
                page,
                timeout=45,
                desc=f"等待 Cerebras get-started 页面超时，url={page.url}, body={_safe_body_text(page)}",
            )
        if state not in {"get_started", "apikeys", "missing_org"}:
            raise RuntimeError(
                f"Cerebras 当前页面状态异常: state={state or 'unknown'} url={page.url} body={_safe_body_text(page)}"
            )

    def _graphql(
        self,
        page,
        *,
        operation_name: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = page.evaluate(
            """async ({ operationName, query, variables }) => {
                const response = await fetch('/api/graphql', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({
                        operationName,
                        variables: variables || {},
                        extensions: { clientLibrary: { name: '@apollo/client', version: '4.1.4' } },
                        query,
                    }),
                });
                return { status: response.status, text: await response.text() };
            }""",
            {
                "operationName": operation_name,
                "query": query,
                "variables": variables or {},
            },
        )
        payload = json.loads(str(result.get("text") or "{}") or "{}")
        status = int(result.get("status") or 0)
        if status >= 400:
            raise RuntimeError(f"Cerebras GraphQL {operation_name} 失败: {payload}")
        return payload

    def _email_ban_status(self, page, *, email: str) -> dict[str, Any]:
        result = page.evaluate(
            """async ({ email }) => {
                const response = await fetch('/api/emailban', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({ email }),
                });
                return { status: response.status, text: await response.text() };
            }""",
            {"email": email},
        )
        payload = json.loads(str(result.get("text") or "{}") or "{}")
        status = int(result.get("status") or 0)
        if status >= 400:
            raise RuntimeError(f"Cerebras /api/emailban 失败: {payload}")
        return payload if isinstance(payload, dict) else {}

    def _load_account_context(self, page, *, email: str) -> dict[str, Any]:
        list_payload: dict[str, Any] = {}
        projects: list[dict[str, Any]] = []
        emailban: dict[str, Any] = {}
        deadline = time.time() + 45
        while time.time() < deadline:
            list_payload = self._graphql(
                page,
                operation_name="ListMyProjects",
                query=LIST_MY_PROJECTS_QUERY,
            )
            raw_projects = (list_payload.get("data") or {}).get("ListMyProjects") or []
            projects = [item for item in raw_projects if isinstance(item, dict)]

            emailban = self._email_ban_status(page, email=email)
            if bool(emailban.get("isBanned")):
                domain = str(emailban.get("emailDomain") or "").strip()
                raise RuntimeError(f"Cerebras 邮箱域名被封禁: {domain or email}")

            if projects:
                break
            page.wait_for_timeout(3000)

        if not projects:
            raise RuntimeError(f"Cerebras 当前账号没有可用组织: {list_payload}")

        org = projects[0]
        organization_id = str(org.get("id") or "").strip()
        if not organization_id:
            raise RuntimeError(f"Cerebras 组织 ID 缺失: {org}")

        org_payload = self._graphql(
            page,
            operation_name="GetOrganization",
            query=GET_ORGANIZATION_QUERY,
            variables={"organizationId": organization_id},
        )
        organization = (org_payload.get("data") or {}).get("GetOrganization") or {}
        state = str(organization.get("state") or "").strip().upper()
        if state and state != "ACTIVE":
            raise RuntimeError(f"Cerebras 组织状态异常: {state}")

        region_payload = self._graphql(
            page,
            operation_name="ListMyRegions",
            query=LIST_MY_REGIONS_QUERY,
        )
        regions = (region_payload.get("data") or {}).get("ListMyRegions") or []
        region = regions[0] if isinstance(regions, list) and regions else {}

        default_project = None
        for item in org.get("projects") or []:
            project_id = str((item or {}).get("id") or "").strip()
            if isinstance(item, dict) and project_id and project_id != "all":
                default_project = item
                break
        if default_project is None:
            for item in org.get("projects") or []:
                if isinstance(item, dict) and str(item.get("id") or "").strip():
                    default_project = item
                    break

        return {
            "organization_id": organization_id,
            "organization_name": str(org.get("name") or organization.get("name") or "").strip(),
            "project_id": str((default_project or {}).get("id") or "").strip(),
            "project_name": str((default_project or {}).get("name") or "").strip(),
            "region_id": str((region or {}).get("id") or "").strip(),
            "region_name": str((region or {}).get("name") or "").strip(),
        }

    def _list_api_keys(self, page, *, organization_id: str) -> list[dict[str, Any]]:
        payload = self._graphql(
            page,
            operation_name="ListOrganizationApiKeys",
            query=LIST_ORGANIZATION_API_KEYS_QUERY,
            variables={"organizationId": organization_id},
        )
        items = (payload.get("data") or {}).get("ListOrganizationApiKeys") or []
        return [item for item in items if isinstance(item, dict)]

    def _open_api_keys_page(self, page, *, organization_id: str) -> None:
        page.goto(
            f"https://cloud.cerebras.ai/platform/{organization_id}/apikeys",
            wait_until="domcontentloaded",
            timeout=120000,
        )
        page.wait_for_timeout(2500)
        self._dismiss_cookie_banner(page)

    def _create_api_key_via_ui(self, page, *, key_name: str) -> None:
        self.log("Step6: 当前组织未拿到默认 key，尝试 UI 手动生成 ...")
        page.get_by_role("button", name="GENERATE API KEY").click(timeout=10000)
        page.locator('[data-testid="key-input"]').wait_for(state="visible", timeout=15000)
        page.locator('[data-testid="key-input"]').fill(key_name)
        page.get_by_role("button", name="CREATE").click(timeout=10000)
        page.wait_for_timeout(5000)

    def _ensure_api_key(self, page, *, organization_id: str, email: str) -> dict[str, Any]:
        deadline = time.time() + 45
        while time.time() < deadline:
            items = self._list_api_keys(page, organization_id=organization_id)
            for item in items:
                secret_key = str(item.get("secretKey") or "").strip()
                if secret_key:
                    return item
            page.wait_for_timeout(3000)

        self._open_api_keys_page(page, organization_id=organization_id)
        key_name = f"Cerebras {str(email or '').split('@', 1)[0][:24] or 'AAR'}"
        self._create_api_key_via_ui(page, key_name=key_name)
        deadline = time.time() + 30
        while time.time() < deadline:
            items = self._list_api_keys(page, organization_id=organization_id)
            for item in items:
                secret_key = str(item.get("secretKey") or "").strip()
                if secret_key:
                    return item
            page.wait_for_timeout(3000)
        raise RuntimeError("Cerebras API key 获取失败")

    def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str,
        use_case: str,
        verification_link_callback: Optional[Callable[[], str]] = None,
    ) -> dict[str, Any]:
        playwright = None
        browser = None
        context = None
        page = None
        try:
            playwright, browser = self._launch_browser()
            context = browser.new_context(
                viewport={"width": 1440, "height": 1080},
                user_agent=USER_AGENT,
            )
            page = context.new_page()

            self._open_landing(page)
            self._submit_email(page, email)

            if verification_link_callback is None:
                raise RuntimeError("Cerebras 当前流程需要邮件激活链接，但任务未提供邮箱回调")
            self.log("Step3: 等待 Cerebras 激活邮件 ...")
            magic_link = str(verification_link_callback() or "").strip()
            if not magic_link:
                raise RuntimeError("未获取到 Cerebras 激活链接")

            self._complete_magic_link(page, magic_link)
            self._fill_onboarding(
                page,
                email=email,
                full_name=full_name or _default_full_name(email),
                use_case=use_case,
            )
            self._ensure_get_started(page)
            context_info = self._load_account_context(page, email=email)
            key_info = self._ensure_api_key(
                page,
                organization_id=context_info["organization_id"],
                email=email,
            )
            self.log("Cerebras 注册链路完成")
            return {
                "email": email,
                "password": password,
                "api_key": str(key_info.get("secretKey") or "").strip(),
                "base_url": CEREBRAS_BASE_URL,
                "organization_id": context_info["organization_id"],
                "organization_name": context_info["organization_name"],
                "project_id": str(key_info.get("projectId") or context_info.get("project_id") or "").strip(),
                "project_name": str(key_info.get("projectName") or context_info.get("project_name") or "").strip(),
                "api_key_id": str(key_info.get("id") or "").strip(),
                "api_key_name": str(key_info.get("name") or "").strip(),
                "api_key_created_at": str(key_info.get("createdAt") or "").strip(),
                "api_key_last_used_at": str(key_info.get("lastUsedAt") or "").strip(),
                "region_id": context_info.get("region_id", ""),
                "region_name": context_info.get("region_name", ""),
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
