"""Claude registration: magic-link + optional hCaptcha (sync)."""
from __future__ import annotations

import re
import time
from typing import Callable
from urllib.parse import urlparse

import requests

from core.mailbox_links import (
    CLAUDE_MAGIC_LINK_REGEX,
    supports_magic_link,
    wait_for_magic_link,
)

LOGIN_URL = "https://claude.ai/login"
SEND_MAGIC_LINK_API = "https://claude.ai/api/auth/send_magic_link"


def _log(log_fn, msg: str) -> None:
    (log_fn or print)(msg)


def _checkpoint(control) -> None:
    if control is not None and hasattr(control, "checkpoint"):
        control.checkpoint()


def request_magic_link_http(email: str, *, log_fn=None, proxy: str | None = None) -> bool:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": "https://claude.ai",
        "Referer": LOGIN_URL,
    }
    try:
        resp = session.post(
            SEND_MAGIC_LINK_API,
            json={"email": email},
            headers=headers,
            timeout=30,
        )
        _log(log_fn, f"[Claude] send_magic_link HTTP {resp.status_code}")
        return resp.status_code in (200, 201, 202, 204)
    except Exception as exc:
        _log(log_fn, f"[Claude] send_magic_link error: {exc}")
        return False


def request_magic_link_browser(page, email: str, *, log_fn=None, control=None) -> bool:
    _checkpoint(control)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(1.0)
    try:
        # email input
        for sel in ('input[type=email]', 'input[name=email]', 'input#email'):
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(email)
                break
        for sel in (
            'button:has-text("Continue")',
            'button:has-text("Continue with email")',
            'button[type=submit]',
        ):
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=5000)
                break
        time.sleep(1.5)
        _log(log_fn, "[Claude] magic link requested via browser")
        return True
    except Exception as exc:
        _log(log_fn, f"[Claude] browser magic link failed: {exc}")
        return False


def open_magic_link(page, link: str, *, log_fn=None, control=None) -> None:
    _checkpoint(control)
    _log(log_fn, f"[Claude] open magic link host={urlparse(link).netloc}")
    page.goto(link, wait_until="domcontentloaded", timeout=90000)
    time.sleep(2.0)


def solve_hcaptcha_if_present(page, captcha, *, log_fn=None, control=None, use_vision: bool = True) -> bool:
    """Detect hCaptcha frame and try vision then token."""
    def _has_hcaptcha() -> bool:
        try:
            if any("hcaptcha" in (f.url or "").lower() for f in page.frames):
                return True
            if page.locator('iframe[src*="hcaptcha"]').count() > 0:
                return True
        except Exception:
            pass
        return False

    if not _has_hcaptcha():
        return True

    _log(log_fn, "[Claude] hCaptcha detected")
    interrupt = control.checkpoint if control and hasattr(control, "checkpoint") else None

    if use_vision:
        try:
            from services.vision_solver.schema import load_preset
            from services.vision_solver.solver import solve_on_page

            ok = solve_on_page(page, load_preset("hcaptcha"), interrupt_checker=interrupt)
            if ok and not _has_hcaptcha():
                _log(log_fn, "[Claude] hCaptcha vision passed")
                return True
        except Exception as exc:
            _log(log_fn, f"[Claude] vision hCaptcha failed: {exc}")

    if captcha is not None:
        try:
            # sitekey best-effort
            site_key = page.evaluate(
                """() => {
                  const el = document.querySelector('[data-sitekey], iframe[src*="hcaptcha"]');
                  if (!el) return '';
                  return el.getAttribute('data-sitekey') || '';
                }"""
            ) or ""
            if not site_key:
                # parse from iframe src
                for fr in page.frames:
                    m = re.search(r"sitekey=([^&]+)", fr.url or "")
                    if m:
                        site_key = m.group(1)
                        break
            if site_key:
                token = captcha.solve_hcaptcha(
                    page.url or LOGIN_URL,
                    site_key,
                    interrupt_checker=interrupt,
                )
                page.evaluate(
                    """(tok) => {
                        document.querySelectorAll('[name=h-captcha-response], [name=g-recaptcha-response]')
                          .forEach(el => { el.value = tok; el.dispatchEvent(new Event('input', {bubbles:true})); });
                        if (window.hcaptcha) {
                          try { window.hcaptcha.submit(); } catch (e) {}
                        }
                    }""",
                    token,
                )
                time.sleep(2)
                if not _has_hcaptcha():
                    return True
        except Exception as exc:
            _log(log_fn, f"[Claude] token hCaptcha failed: {exc}")

    # manual wait
    _log(log_fn, "[Claude] waiting manual hCaptcha up to 120s")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        _checkpoint(control)
        if not _has_hcaptcha():
            return True
        time.sleep(2)
    return not _has_hcaptcha()


def handle_onboarding(page, *, log_fn=None, control=None) -> None:
    """Best-effort skip birthday / name / org prompts."""
    for _ in range(8):
        _checkpoint(control)
        body = ""
        try:
            body = (page.locator("body").inner_text(timeout=1500) or "").lower()
        except Exception:
            pass
        # birthday-ish
        try:
            if page.locator('input[type=date], select').count() > 0 and "birth" in body:
                # fill adult birthday
                for sel, val in (
                    ('select[name*="year" i], select#year', "1990"),
                    ('select[name*="month" i], select#month', "1"),
                    ('select[name*="day" i], select#day', "1"),
                ):
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        try:
                            loc.select_option(val)
                        except Exception:
                            loc.fill(val)
        except Exception:
            pass
        for text in ("Continue", "Next", "Get started", "Skip", "Agree", "Accept"):
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.I)).first
                if btn.count() > 0:
                    btn.click(timeout=2000)
                    time.sleep(1.0)
                    break
            except Exception:
                continue
        else:
            break
        if "claude.ai/new" in (page.url or "") or "chat" in (page.url or ""):
            break


def extract_session(page) -> dict:
    cookies = {}
    try:
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    except Exception:
        pass
    session_key = cookies.get("sessionKey") or cookies.get("session_key") or ""
    return {"cookies": cookies, "session_key": session_key, "url": page.url or ""}


def register_claude(
    page,
    *,
    email: str,
    mailbox,
    mail_account,
    captcha=None,
    proxy: str | None = None,
    log_fn: Callable | None = None,
    control=None,
    otp_timeout: int = 180,
    use_vision: bool = True,
) -> dict:
    if mailbox is None or mail_account is None:
        raise RuntimeError("Claude 需要 mailbox")
    if not supports_magic_link(mailbox):
        raise RuntimeError(
            "当前 mail_provider 不支持 magic-link 正文拉取，请换 cfworker/maliapi/gptmail 等白名单"
        )

    before = set()
    try:
        before = set(mailbox.get_current_ids(mail_account) or set())
    except Exception:
        before = set()

    ok = request_magic_link_http(email, log_fn=log_fn, proxy=proxy)
    if not ok:
        ok = request_magic_link_browser(page, email, log_fn=log_fn, control=control)
    if not ok:
        raise RuntimeError("Claude magic link 请求失败")

    link = wait_for_magic_link(
        mailbox,
        mail_account,
        link_regex=CLAUDE_MAGIC_LINK_REGEX,
        timeout=otp_timeout,
        before_ids=before,
        poll_interval=3.0,
        task_control=control,
        must_contain="claude",
        log=log_fn or print,
    )
    open_magic_link(page, link, log_fn=log_fn, control=control)
    if not solve_hcaptcha_if_present(
        page, captcha, log_fn=log_fn, control=control, use_vision=use_vision
    ):
        raise RuntimeError("Claude hCaptcha 未通过")
    handle_onboarding(page, log_fn=log_fn, control=control)
    session = extract_session(page)
    if not session.get("session_key") and not session.get("cookies"):
        _log(log_fn, "[Claude] 警告: 未提取到 session cookie，仍返回当前状态")
    return {"email": email, **session, "magic_link": link}
