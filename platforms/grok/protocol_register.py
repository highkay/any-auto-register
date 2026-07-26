"""Grok protocol registration orchestrator.

Pipeline (aligned with Charles-0509/Grok-Register):
  1. curl_cffi chrome131 TLS session (+ optional FlareSolverr clearance)
  2. scrape signup config
  3. gRPC CreateEmailValidationCode
  4. mailbox OTP
  5. gRPC VerifyEmailValidationCode
  6. offscreen Turnstile mint (browser only here)
  7. Next.js Server Action create user + SSO hop
"""

from __future__ import annotations

import os
import random
import string
import time
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

import requests

from platforms.grok.protocol_client import (
    DEFAULT_IMPERSONATE,
    DEFAULT_USER_AGENT,
    GrokProtocolClient,
    GrokProtocolError,
    SignupConfig,
)

DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
_FLARESOLVERR_COOKIE_MARKERS = {"cf_clearance", "__cf_bm", "xai_anon_id"}
_DISABLED = {"0", "false", "no", "off", "none", "disabled"}
_LOCAL_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _rand_password(n: int = 12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n)) + ",,,aA1"


def _rand_name(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n)).capitalize()


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _DISABLED


class GrokProtocolRegister:
    def __init__(
        self,
        *,
        captcha_solver=None,
        yescaptcha_key: str = "",
        proxy: Optional[str] = None,
        log_fn: Callable[[str], None] = print,
        task_control=None,
        extra: Optional[dict[str, Any]] = None,
    ):
        self.captcha_solver = captcha_solver
        self.yescaptcha_key = yescaptcha_key or ""
        self.proxy = (proxy or "").strip() or None
        self.log = log_fn
        self._task_control = task_control
        self.extra = dict(extra or {})

    def _checkpoint(self) -> None:
        if self._task_control is not None:
            self._task_control.checkpoint()

    def _resolve_flaresolverr_endpoint(self) -> str:
        for key in (
            self.extra.get("grok_flaresolverr_url"),
            self.extra.get("flaresolverr_url"),
            os.getenv("GROK_FLARESOLVERR_URL"),
            os.getenv("FLARESOLVERR_URL"),
            DEFAULT_FLARESOLVERR_URL,
        ):
            value = str(key or "").strip()
            if value and value.lower() not in _DISABLED:
                return value
        return ""

    def _flaresolverr_reachable(self, endpoint: str) -> bool:
        if not endpoint:
            return False
        http = requests.Session()
        http.trust_env = False
        try:
            # FlareSolverr exposes / or /v1; probe with short timeout.
            base = endpoint.rstrip("/")
            candidates = [base]
            if base.endswith("/v1"):
                candidates.append(base[: -len("/v1")] or base)
            else:
                candidates.append(base + "/v1")
            # Prefer root health which returns "FlareSolverr is ready!"
            root = base.rsplit("/v1", 1)[0] if "/v1" in base else base
            if root and root not in candidates:
                candidates.insert(0, root)
            for url in candidates:
                try:
                    resp = http.get(url.rstrip("/") or url, timeout=2.5)
                    if resp.status_code < 500:
                        return True
                except Exception:
                    continue
            return False
        finally:
            http.close()

    def _clearance_enabled(self) -> bool:
        mode = str(
            self.extra.get("grok_clearance_mode")
            or self.extra.get("clearance_mode")
            or os.getenv("GROK_CLEARANCE_MODE")
            or "auto"
        ).strip().lower()
        if mode in {"never", "off", "0", "false", "disabled"}:
            return False
        if mode in {"always", "force", "on", "1", "true"}:
            return True
        # auto: only when FlareSolverr actually responds (avoid long timeouts)
        endpoint = self._resolve_flaresolverr_endpoint()
        if not endpoint:
            return False
        ok = self._flaresolverr_reachable(endpoint)
        if not ok:
            self.log(f"协议清障 auto：FlareSolverr 不可达，跳过 ({endpoint})")
        return ok

    def _resolve_flaresolverr_loopback_proxy_host(self) -> str:
        enabled = str(
            self.extra.get("grok_flaresolverr_bridge_loopback_proxy", "true") or "true"
        ).strip().lower()
        if enabled in _DISABLED:
            return ""
        value = str(
            self.extra.get("grok_flaresolverr_loopback_proxy_host")
            or os.getenv("GROK_FLARESOLVERR_LOOPBACK_PROXY_HOST")
            or "host.docker.internal"
        ).strip()
        if value.lower() in _DISABLED:
            return ""
        return value

    def _normalize_flaresolverr_proxy_url(self, proxy_url: Optional[str]) -> Optional[str]:
        """Rewrite 127.0.0.1 proxy to host.docker.internal for containerized FS."""
        raw = str(proxy_url or "").strip()
        if not raw:
            return None
        parts = urlsplit(raw)
        host = str(parts.hostname or "").strip()
        if not parts.scheme or not host:
            return raw
        if host.lower() not in _LOCAL_LOOPBACK_HOSTS:
            return raw
        bridge_host = self._resolve_flaresolverr_loopback_proxy_host()
        if not bridge_host or bridge_host.lower() == host.lower():
            return raw
        port = parts.port
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        auth = ""
        if parts.username is not None:
            auth = quote(parts.username, safe="")
            if parts.password is not None:
                auth = f"{auth}:{quote(parts.password, safe='')}"
            auth = f"{auth}@"
        if ":" in bridge_host and not bridge_host.startswith("["):
            netloc = f"{auth}[{bridge_host}]:{port}"
        else:
            netloc = f"{auth}{bridge_host}:{port}"
        normalized = parts._replace(netloc=netloc).geturl()
        self.log(
            f"  FlareSolverr 代理回环地址已改写: {host}:{port} -> {bridge_host}:{port}"
        )
        return normalized

    def _fetch_clearance_bundle(self) -> tuple[list[dict[str, Any]], str]:
        endpoint = self._resolve_flaresolverr_endpoint()
        if not endpoint:
            return [], ""
        # Candidate proxy URLs for FS (Docker cannot use host 127.0.0.1 as-is).
        proxy_candidates: list[Optional[str]] = []
        rewritten = self._normalize_flaresolverr_proxy_url(self.proxy)
        if rewritten:
            proxy_candidates.append(rewritten)
        if self.proxy and self.proxy not in proxy_candidates:
            proxy_candidates.append(self.proxy)
        # Last resort: FS direct egress (worked in probe when no proxy)
        allow_direct = str(
            self.extra.get("grok_flaresolverr_allow_direct", "1") or "1"
        ).strip().lower() not in _DISABLED
        if allow_direct and None not in proxy_candidates:
            proxy_candidates.append(None)

        last_error: Exception | None = None
        for proxy_url in proxy_candidates:
            label = proxy_url or "direct"
            self.log(f"协议清障: FlareSolverr 预热 accounts.x.ai (proxy={label})")
            try:
                cookies, ua = self._fetch_clearance_bundle_once(endpoint, proxy_url)
                if cookies:
                    return cookies, ua
            except Exception as exc:
                last_error = exc
                self.log(f"  清障失败 proxy={label}: {exc}")
                continue
        if last_error:
            raise last_error
        return [], ""

    def _fetch_clearance_bundle_once(
        self,
        endpoint: str,
        proxy_url: Optional[str],
    ) -> tuple[list[dict[str, Any]], str]:
        session_id = f"grok-proto-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        http = requests.Session()
        http.trust_env = False
        solution: dict[str, Any] = {}
        max_attempts = max(1, int(self.extra.get("grok_flaresolverr_attempts") or 3))
        try:
            create_payload: dict[str, Any] = {
                "cmd": "sessions.create",
                "session": session_id,
            }
            if proxy_url:
                create_payload["proxy"] = {"url": proxy_url}
            create_resp = http.post(endpoint, json=create_payload, timeout=30)
            if create_resp.status_code >= 400:
                raise RuntimeError(f"FlareSolverr create HTTP {create_resp.status_code}")
            create_data = create_resp.json()
            if str(create_data.get("status") or "").lower() != "ok":
                raise RuntimeError(create_data.get("message") or "FlareSolverr create failed")

            target = "https://accounts.x.ai/sign-up?redirect=grok-com"
            for attempt in range(1, max_attempts + 1):
                self._checkpoint()
                req_payload: dict[str, Any] = {
                    "cmd": "request.get",
                    "session": session_id,
                    "url": target,
                    "maxTimeout": 120000,
                }
                # Some FS builds only honor proxy on the request body.
                if proxy_url:
                    req_payload["proxy"] = {"url": proxy_url}
                resp = http.post(endpoint, json=req_payload, timeout=150)
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"FlareSolverr request HTTP {resp.status_code}: "
                        f"{(resp.text or '')[:180]}"
                    )
                data = resp.json()
                if str(data.get("status") or "").lower() != "ok":
                    raise RuntimeError(data.get("message") or "FlareSolverr request failed")
                solution = data.get("solution") or {}
                names = {
                    str(c.get("name") or "").strip()
                    for c in (solution.get("cookies") or [])
                    if str(c.get("name") or "").strip()
                }
                self.log(f"  清障 attempt {attempt}: cookies={sorted(names)}")
                if "cf_clearance" in names:
                    break
                if attempt < max_attempts and _FLARESOLVERR_COOKIE_MARKERS.intersection(names):
                    time.sleep(1.0)
                    continue
            cookies = list(solution.get("cookies") or [])
            ua = str(solution.get("userAgent") or solution.get("user_agent") or "").strip()
            if not any(str(c.get("name") or "") == "cf_clearance" for c in cookies):
                # Still return partial cookies; caller may continue TLS-only path
                self.log("  清障未拿到 cf_clearance，返回当前 cookies")
            return cookies, ua
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

    def _impersonate_chain(self) -> list[str]:
        primary = str(
            self.extra.get("grok_cf_impersonate")
            or self.extra.get("cf_impersonate")
            or os.getenv("GROK_CF_IMPERSONATE")
            or DEFAULT_IMPERSONATE
        ).strip() or DEFAULT_IMPERSONATE
        fallback_raw = str(
            self.extra.get("grok_cf_impersonate_fallback")
            or self.extra.get("cf_impersonate_fallback")
            or os.getenv("GROK_CF_IMPERSONATE_FALLBACK")
            or "chrome124,chrome120"
        )
        chain = [primary]
        for part in fallback_raw.split(","):
            name = part.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    def _prepare_client(self) -> tuple[GrokProtocolClient, SignupConfig]:
        clearance_cookies: list[dict[str, Any]] = []
        clearance_ua = ""
        if self._clearance_enabled():
            try:
                clearance_cookies, clearance_ua = self._fetch_clearance_bundle()
            except Exception as exc:
                mode = str(
                    self.extra.get("grok_clearance_mode")
                    or os.getenv("GROK_CLEARANCE_MODE")
                    or "auto"
                ).strip().lower()
                if mode in {"always", "force"}:
                    raise
                self.log(f"协议清障失败，继续 TLS 直连: {exc}")

        last_error: Exception | None = None
        for profile in self._impersonate_chain():
            self._checkpoint()
            client = GrokProtocolClient(
                proxy=self.proxy,
                impersonate=profile,
                user_agent=clearance_ua or DEFAULT_USER_AGENT,
                log_fn=self.log,
                task_control=self._task_control,
            )
            if clearance_cookies:
                client.apply_clearance_cookies(clearance_cookies, clearance_ua)
            try:
                cfg = client.fetch_config()
                return client, cfg
            except GrokProtocolError as exc:
                last_error = exc
                self.log(f"协议预热失败 profile={profile}: {exc}")
                client.close()
                # Auto clearance on CF block if not already applied
                if (
                    getattr(exc, "code", "") in {"cf_403", "cf_blocked"}
                    and not clearance_cookies
                    and self._clearance_enabled()
                ):
                    try:
                        clearance_cookies, clearance_ua = self._fetch_clearance_bundle()
                    except Exception as clear_exc:
                        self.log(f"协议清障二次尝试失败: {clear_exc}")
                continue
            except Exception as exc:
                last_error = exc
                self.log(f"协议预热异常 profile={profile}: {exc}")
                client.close()
                continue
        raise last_error or GrokProtocolError("协议预热失败", code="warm")

    def _native_ui_register(
        self,
        client: GrokProtocolClient,
        *,
        email: str,
        password: str,
        given_name: str,
        family_name: str,
        otp_callback: Optional[Callable[[], str]] = None,
        seed_code: str = "",
    ) -> dict[str, Any]:
        """Original-style SPA registration (no third-party captcha).

        Walks email → OTP → profile → Turnstile click → submit → SSO cookies.
        This is option A: same browser path as before, not YesCaptcha.
        """
        from platforms.grok.core import GrokRegister

        # Force real headed browser (visible window). Offscreen/headless often fails
        # managed Turnstile; user path is explicitly headed.
        browser_mode = str(
            self.extra.get("grok_browser_mode")
            or self.extra.get("executor_type")
            or os.getenv("GROK_BROWSER_MODE")
            or "headed"
        ).strip().lower() or "headed"
        want_headless = browser_mode in {"headless", "headless=true", "1"}
        if want_headless:
            self.log("注册 strategy=native_ui_complete (headless 按配置启用)")
        else:
            self.log(
                "注册 strategy=native_ui_complete "
                "(有头可见窗口 email→otp→profile→Turnstile→提交)"
            )

        helper = GrokRegister(
            captcha_solver=None,
            yescaptcha_key="",
            proxy=self.proxy,
            log_fn=self.log,
            headless=want_headless,
            task_control=self._task_control,
            extra=self.extra,
        )

        # Soft Turnstile: same-page / FlareSolverr, then headed manual wait — no YesCaptcha.
        def _soft_solve_turnstile(page) -> str:
            try:
                existing = helper._wait_turnstile_token(page, wait_rounds=1, wait_ms=1)
                if existing:
                    return existing
                token, _err = helper._reuse_turnstile_on_current_page(page)
                if token:
                    return token
                try:
                    token = helper._solve_turnstile_by_flaresolverr(page) or ""
                except Exception as fs_exc:
                    self.log(f"  soft Turnstile FlareSolverr: {fs_exc}")
                    token = ""
                if token:
                    return token
                # Headed handoff: user clicks checkbox in the open window.
                return helper._wait_for_manual_turnstile(page) or ""
            except Exception as exc:
                self.log(f"  soft Turnstile: {exc}")
                try:
                    return helper._wait_for_manual_turnstile(page) or ""
                except Exception:
                    return ""

        helper._solve_turnstile_on_page = _soft_solve_turnstile  # type: ignore[method-assign]

        playwright = browser = context = None
        try:
            playwright, browser = helper._launch_browser()
            # Prefer real Chrome UA (no forced FS Linux UA) for headed Turnstile trust.
            ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1400, "height": 1200}}
            # Only set UA when protocol client has a non-default, cookie-aligned one.
            ua = str(client.user_agent or "").strip()
            if ua and "Chrome/142" not in ua:
                ctx_kwargs["user_agent"] = ua
            context = browser.new_context(**ctx_kwargs)
            helper._install_turnstile_patch(context)
            cookies = client.export_playwright_cookies()
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as exc:
                    self.log(f"  native_ui 注入 cookies 部分失败: {exc}")

            page = context.new_page()
            helper._goto_email_signup(page)
            # Make sure we really left the gate ("使用邮箱注册") before typing email.
            try:
                if not helper._page_has_email_input(page):
                    helper._ensure_email_signup_form(
                        page,
                        timeout=20,
                        stage_label="native_ui 邮箱入口",
                    )
            except Exception as gate_exc:
                self.log(f"  邮箱入口二次确认: {gate_exc}")
            # Align trust signals with real SPA: ensure Castle is present before send-code.
            try:
                from platforms.grok.castle import ensure_castle_on_page, resolve_castle_pk

                ensure_castle_on_page(
                    page,
                    pk=resolve_castle_pk(self.extra),
                    log_fn=self.log,
                )
                # Castle inject can remount SPA; re-open email form if needed.
                if not helper._page_has_email_input(page):
                    helper._ensure_email_signup_form(
                        page,
                        timeout=15,
                        stage_label="Castle 后邮箱入口",
                    )
            except Exception as castle_exc:
                self.log(f"  Castle 注入/mint 跳过: {castle_exc}")
            if not helper._page_has_email_input(page):
                raise RuntimeError(
                    "未能进入邮箱输入页（仍停在注册入口）。"
                    f" url={getattr(page, 'url', '')}"
                )
            helper._submit_email(page, email)

            ui_code = str(seed_code or "").replace("-", "").replace(" ", "").strip()
            if otp_callback is not None:
                self.log("native_ui: 等待验证码...")
                fresh = str(otp_callback() or "").replace("-", "").replace(" ", "").strip()
                if fresh:
                    ui_code = fresh
                    self.log(f"native_ui: 验证码 {ui_code}")
            if not ui_code:
                raise RuntimeError("未获取到邮箱验证码")

            otp_for_ui = ui_code
            if len(ui_code) == 6 and "-" not in ui_code:
                otp_for_ui = f"{ui_code[:3]}-{ui_code[3:]}"
            try:
                helper._submit_otp(page, otp_for_ui)
            except Exception as otp_exc:
                try:
                    title = str(page.title() or "")
                    url_now = str(getattr(page, "url", "") or "")
                    body = str(page.locator("body").inner_text(timeout=1500) or "")[:240]
                except Exception:
                    title, url_now, body = "", "", ""
                raise RuntimeError(
                    f"{otp_exc} | title={title!r} url={url_now!r} body={body!r}"
                ) from otp_exc

            helper._fill_user_form(page, given_name, family_name, password)
            # Refresh Castle token near final submit (browser SPA does this too).
            try:
                from platforms.grok.castle import ensure_castle_on_page, resolve_castle_pk

                ensure_castle_on_page(
                    page,
                    pk=resolve_castle_pk(self.extra),
                    log_fn=self.log,
                )
            except Exception as castle_exc:
                self.log(f"  提交前 Castle 刷新跳过: {castle_exc}")

            token = _soft_solve_turnstile(page)
            if token and token not in {"manual-auth-cookie", "manual-navigated"}:
                self.log(f"  Turnstile token ok len={len(token)}")
            elif token:
                self.log(f"  Turnstile 人工阶段已通过（{token}）")
            else:
                self.log("  Turnstile 仍无 token，尝试提交；失败会再等人工操作")

            # Finish in-browser like original core.register (SSO from cookies).
            try:
                helper._submit_register(page)
            except Exception as submit_exc:
                self.log(f"  首次提交失败: {submit_exc}；再试 Turnstile/人工 + 提交")
                _soft_solve_turnstile(page)
                helper._submit_register(page)

            helper._accept_tos_if_needed(page)
            cookies = context.cookies()
            if not helper._has_auth_cookies(cookies):
                # Give human more time to finish challenge / click continue.
                self.log("  尚未拿到 sso，继续等待人工完成注册（若窗口仍开着）…")
                helper._wait_for_manual_turnstile(page)
                cookies = context.cookies()
            if not helper._has_auth_cookies(cookies):
                cookies = helper._wait_for_auth_cookies(page, timeout=60)
            sso = helper._pick_cookie(cookies, "sso")
            sso_rw = helper._pick_cookie(cookies, "sso-rw")
            if not sso:
                # Final manual window: user may still finish login in the open browser.
                self.log("  仍无 sso，最后一次等待人工完成…")
                helper._wait_for_manual_turnstile(page)
                try:
                    cookies = context.cookies()
                except Exception:
                    cookies = []
                if not helper._has_auth_cookies(cookies):
                    try:
                        cookies = helper._wait_for_auth_cookies(page, timeout=30)
                    except Exception as wait_exc:
                        raise RuntimeError(
                            f"浏览器注册完成但未拿到 sso cookie "
                            f"url={getattr(page, 'url', '')} token_len={len(token or '')}: {wait_exc}"
                        ) from wait_exc
                sso = helper._pick_cookie(cookies, "sso")
                sso_rw = helper._pick_cookie(cookies, "sso-rw")
            if not sso:
                raise RuntimeError(
                    f"浏览器注册完成但未拿到 sso cookie "
                    f"url={getattr(page, 'url', '')} token_len={len(token or '')}"
                )

            self.log(f"  ✅ 浏览器同页注册成功 sso={sso[:40]}...")
            submit_meta = dict(getattr(helper, "_register_submit_meta", {}) or {})
            if submit_meta.get("saw_transient_error"):
                self.log(
                    "  诊断: 注册按钮曾出现瞬态「请重试」toast "
                    f"(email_retries={submit_meta.get('email_transient_retries')}, "
                    f"final_retries={submit_meta.get('transient_error_retries')})。"
                    "这通常不是永久标记；后续 invalid_grant 更应查 Castle/代理。"
                )
            return {
                "email": email,
                "password": password,
                "given_name": given_name,
                "family_name": family_name,
                "sso": sso,
                "sso_rw": sso_rw,
                "cookies": cookies,
                "register_mode": "protocol+native_ui",
                "validation_code": ui_code,
                "turnstile_token": token,
                "register_submit_meta": submit_meta,
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

    def _protocol_http_register(
        self,
        client: GrokProtocolClient,
        cfg: SignupConfig,
        *,
        email: str,
        password: str,
        given_name: str,
        family_name: str,
        otp_callback: Optional[Callable[[], str]],
    ) -> dict[str, Any]:
        """TLS + gRPC create/verify + Turnstile mint + Next server action."""
        from platforms.grok.castle import mint_castle_request_token, resolve_castle_pk
        from platforms.grok.protocol_client import build_signup_body
        from platforms.grok.turnstile_offscreen import mint_turnstile_token

        self.log("注册 strategy=protocol_http (gRPC 发码/验码 + Server Action)")
        castle = ""
        try:
            castle = mint_castle_request_token(
                proxy=self.proxy,
                pk=resolve_castle_pk(self.extra),
                user_agent=client.user_agent,
                log_fn=self.log,
                task_control=self._task_control,
            )
        except Exception as exc:
            self.log(f"  Castle mint 跳过: {exc}")

        client.create_email_code(email, castle_token=castle)
        if not otp_callback:
            raise RuntimeError("protocol_http 需要 otp_callback")
        self.log("protocol_http: 等待验证码...")
        code = str(otp_callback() or "").replace("-", "").replace(" ", "").strip()
        if not code:
            raise RuntimeError("未获取到邮箱验证码")
        client.verify_email_code(email, code)
        try:
            client.validate_password(email, password)
        except Exception as exc:
            self.log(f"  ValidatePassword 跳过: {exc}")

        turnstile = ""
        # Prefer same proxy as registration; if mint fails, retry direct (CF often
        # blocks residential proxy paths for Turnstile while allowing TLS signup).
        mint_proxies: list[Optional[str]] = []
        if self.proxy:
            mint_proxies.append(self.proxy)
        if None not in mint_proxies:
            mint_proxies.append(None)
        for mint_proxy in mint_proxies:
            try:
                self.log(
                    f"  Turnstile browser mint proxy={'yes' if mint_proxy else 'direct'} …"
                )
                turnstile = mint_turnstile_token(
                    site_key=cfg.site_key,
                    page_url="https://accounts.x.ai/sign-up?redirect=grok-com",
                    proxy=mint_proxy,
                    cookies=client.export_playwright_cookies(),
                    user_agent=client.user_agent,
                    log_fn=self.log,
                    task_control=self._task_control,
                    mode=str(self.extra.get("grok_turnstile_mode") or "headed"),
                    timeout=float(self.extra.get("grok_turnstile_timeout") or 90),
                )
                if turnstile:
                    break
            except Exception as exc:
                self.log(f"  Turnstile browser mint 失败: {exc}")
                turnstile = ""
        # Fallback: YesCaptcha / configured captcha solver (optional).
        if not turnstile and (
            self.captcha_solver is not None or str(self.yescaptcha_key or "").strip()
        ):
            try:
                solver = self.captcha_solver
                if solver is None:
                    from core.base_captcha import YesCaptcha

                    # Prefer explicit public API when local reverse-proxy is down.
                    api_base = str(
                        self.extra.get("yescaptcha_api_base")
                        or "https://api.yescaptcha.com"
                    ).strip()
                    if "127.0.0.1" in api_base or "localhost" in api_base:
                        api_base = "https://api.yescaptcha.com"
                    solver = YesCaptcha(
                        client_key=self.yescaptcha_key, api_base=api_base
                    )
                self.log("  Turnstile: 回退 YesCaptcha/solver …")
                turnstile = str(
                    solver.solve_turnstile(
                        page_url="https://accounts.x.ai/sign-up?redirect=grok-com",
                        site_key=cfg.site_key,
                    )
                    or ""
                ).strip()
                if turnstile:
                    self.log(f"  Turnstile solver 成功 len={len(turnstile)}")
            except Exception as exc:
                self.log(f"  Turnstile solver 失败: {exc}")
                turnstile = ""
        if not turnstile:
            raise RuntimeError(
                "protocol_http 未拿到 Turnstile token（Server Action 会拒绝）"
            )

        try:
            castle2 = mint_castle_request_token(
                proxy=self.proxy,
                pk=resolve_castle_pk(self.extra),
                user_agent=client.user_agent,
                log_fn=self.log,
                task_control=self._task_control,
            )
        except Exception:
            castle2 = castle
        body = build_signup_body(
            email,
            password,
            code,
            turnstile,
            given_name=given_name,
            family_name=family_name,
            castle_token=castle2 or "",
        )
        _text, sso = client.signup_server_action(body, cfg.action_id, cfg.state_tree)
        sso_rw = ""
        for name, value, _domain in client.export_cookie_pairs():
            if name == "sso-rw" and value:
                sso_rw = value
                break
        self.log(f"  ✅ protocol_http 注册成功 sso_len={len(sso)}")
        return {
            "email": email,
            "password": password,
            "given_name": given_name,
            "family_name": family_name,
            "sso": sso,
            "sso_rw": sso_rw,
            "cookies": client.export_playwright_cookies(),
            "register_mode": "protocol_http",
            "validation_code": code,
            "turnstile_token": turnstile,
        }

    def register(
        self,
        email: str,
        password: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict[str, Any]:
        if not email:
            raise RuntimeError("未获取到可用邮箱")
        if not password:
            password = _rand_password()
        given_name = _rand_name()
        family_name = _rand_name()

        client: GrokProtocolClient | None = None
        try:
            self.log(
                f"Grok 协议注册启动: {email} proxy={'yes' if self.proxy else 'no'}"
            )
            client, cfg = self._prepare_client()
            self.log(
                f"协议预热完成 site_key={cfg.site_key[:16]}... action={cfg.action_id[:12]}..."
            )

            if not otp_callback:
                def otp_callback() -> str:  # type: ignore[no-redef]
                    return input("验证码: ").strip()

            strategy = str(
                self.extra.get("grok_register_strategy")
                or self.extra.get("register_strategy")
                or "auto"
            ).strip().lower()
            prefer_http = strategy in {"auto", "protocol", "protocol_http", "http", ""}
            prefer_ui = strategy in {"auto", "native_ui", "ui", "browser", ""}

            last_error: Exception | None = None
            if prefer_http and strategy != "native_ui":
                try:
                    return self._protocol_http_register(
                        client,
                        cfg,
                        email=email,
                        password=password,
                        given_name=given_name,
                        family_name=family_name,
                        otp_callback=otp_callback,
                    )
                except Exception as exc:
                    last_error = exc
                    self.log(f"protocol_http 失败，回退 native_ui: {exc}")
                    if strategy in {"protocol", "protocol_http", "http"}:
                        raise

            if prefer_ui:
                browser_result = self._native_ui_register(
                    client,
                    email=email,
                    password=password,
                    given_name=given_name,
                    family_name=family_name,
                    otp_callback=otp_callback,
                )
                return {
                    "email": browser_result["email"],
                    "password": browser_result["password"],
                    "given_name": browser_result["given_name"],
                    "family_name": browser_result["family_name"],
                    "sso": browser_result["sso"],
                    "sso_rw": browser_result.get("sso_rw") or "",
                    "cookies": browser_result.get("cookies") or [],
                    "register_mode": browser_result.get("register_mode")
                    or "protocol+native_ui",
                }

            raise last_error or RuntimeError("无可用注册策略")
        finally:
            if client is not None:
                client.close()
