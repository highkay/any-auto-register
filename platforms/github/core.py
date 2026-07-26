"""GitHub signup flow (sync Playwright).

Ported structurally from reg-factory register_github.py into any-auto-register
executor + captcha abstractions.
"""
from __future__ import annotations

import random
import re
import string
import time
from typing import Callable

SIGNUP_URL = "https://github.com/signup"
ARKOSE_PUBLIC_KEY = "747B83EC-2CA3-43AD-A7DF-701F286FBABA"
ARKOSE_API_SUBDOMAIN = "github-api.arkoselabs.com"


def rand_password() -> str:
    return "Gh1!" + "".join(random.choices(string.ascii_letters + string.digits, k=14))


def rand_username() -> str:
    adj = random.choice(["cool", "fast", "blue", "red", "neo", "sky", "dev", "byte", "code", "pixel"])
    noun = random.choice(["fox", "wolf", "cat", "owl", "bear", "hawk", "lion", "frog", "deer", "crab"])
    return f"{adj}{noun}{random.randint(1000, 9999)}"


def _log(log_fn, msg: str) -> None:
    (log_fn or print)(msg)


def _checkpoint(control) -> None:
    if control is None:
        return
    fn = getattr(control, "checkpoint", None)
    if callable(fn):
        fn()


def _fill(page, selector: str, value: str, label: str, log_fn=None) -> bool:
    try:
        loc = page.locator(selector).first
        loc.click(timeout=5000)
        loc.fill("")
        loc.type(value, delay=random.randint(20, 60))
        _log(log_fn, f"[GitHub] {label}=ok")
        return True
    except Exception as exc:
        _log(log_fn, f"[GitHub] {label} fill failed: {exc}")
        return False


def detect_captcha(page, max_wait: float = 12.0) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            if any(
                any(k in (f.url or "") for k in ("octocaptcha", "arkose", "funcaptcha"))
                for f in page.frames
            ):
                return True
            if page.locator("iframe[src*=octocaptcha], iframe[src*=arkose]").count() > 0:
                return True
        except Exception:
            pass
        time.sleep(0.8)
    return False


def click_create_account(page) -> bool:
    for sel in (
        'button:has-text("Create account")',
        'button:has-text("Continue")',
        'button[type=submit]',
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=4000)
                return True
        except Exception:
            continue
    return False


def extract_blob(page) -> str | None:
    for fr in page.frames:
        u = fr.url or ""
        if any(k in u for k in ("octocaptcha", "arkose", "funcaptcha")):
            try:
                b = fr.evaluate(
                    """() => {
                      const el = document.querySelector('#funcaptcha');
                      return el ? (el.getAttribute('data-data-exchange-payload') || '') : '';
                    }"""
                )
                if b and str(b).strip():
                    return str(b).strip()
            except Exception:
                pass
    return None


def click_visual_puzzle(page, max_wait: float = 40.0, control=None) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        _checkpoint(control)
        for fr in page.frames:
            u = fr.url or ""
            if any(k in u for k in ("octocaptcha", "arkose", "funcaptcha")):
                try:
                    el = fr.get_by_text("Visual puzzle", exact=False).first
                    if el.count() > 0:
                        el.click(timeout=4000)
                        return True
                except Exception:
                    pass
        time.sleep(1.5)
    return False


def inject_funcaptcha_token(page, token: str, log_fn=None) -> bool:
    injected = False
    try:
        for fr in page.frames:
            if "octocaptcha" in (fr.url or ""):
                origin = fr.evaluate(
                    """() => {
                      const el = document.querySelector('#funcaptcha');
                      return el ? (el.getAttribute('data-target-origin') || '') : '';
                    }"""
                )
                origin = origin or "https://github.com"
                fr.evaluate(
                    """([tok, org]) => {
                        parent.postMessage({event: 'captcha-complete', sessionToken: tok}, org || '*');
                    }""",
                    [token, origin],
                )
                _log(log_fn, f"[GitHub] posted captcha-complete origin={origin}")
                injected = True
                break
    except Exception as exc:
        _log(log_fn, f"[GitHub] inject error: {exc}")
    if not injected:
        try:
            page.evaluate(
                """(tok) => {
                    const msg = {event: 'captcha-complete', sessionToken: tok};
                    window.postMessage(msg, '*');
                }""",
                token,
            )
            injected = True
        except Exception:
            pass
    return injected


def solve_arkose(
    page,
    captcha,
    *,
    log_fn=None,
    control=None,
    use_puzzle: bool = True,
    skip_variants: tuple[str, ...] = ("character",),
) -> bool:
    """Token path first, then GitHub puzzle voting fallback."""
    _checkpoint(control)
    click_visual_puzzle(page, control=control)
    time.sleep(2.0)
    blob = extract_blob(page)
    _log(log_fn, f"[GitHub] arkose blob={'yes len='+str(len(blob)) if blob else 'none'}")

    token = None
    if captcha is not None:
        try:
            token = captcha.solve_funcaptcha(
                SIGNUP_URL,
                ARKOSE_PUBLIC_KEY,
                subdomain=ARKOSE_API_SUBDOMAIN,
                blob=blob,
                timeout_seconds=200,
                interrupt_checker=(control.checkpoint if control else None),
            )
        except Exception as exc:
            _log(log_fn, f"[GitHub] FunCaptcha token failed: {exc}")
    if token:
        if inject_funcaptcha_token(page, token, log_fn=log_fn):
            time.sleep(2.0)
            if not detect_captcha(page, max_wait=3):
                return True

    if use_puzzle:
        try:
            from services.vision_solver.github_puzzle import solve_github_arkose_puzzle

            result = solve_github_arkose_puzzle(
                page,
                interrupt_checker=(control.checkpoint if control else None),
                skip_variants=skip_variants,
            )
            if result == "SKIP_VARIANT":
                _log(log_fn, "[GitHub] SKIP_VARIANT — hard puzzle variant")
                return False
            return bool(result)
        except Exception as exc:
            _log(log_fn, f"[GitHub] puzzle driver failed: {exc}")
    return False


def wait_launch_code(mailbox, account, before_ids, timeout: int, log_fn=None, control=None) -> str:
    if mailbox is None or account is None:
        raise RuntimeError("GitHub 需要邮箱以收取 launch code")
    _log(log_fn, "[GitHub] 等待 launch code 邮件...")
    code = mailbox.wait_for_code(
        account,
        keyword="GitHub",
        timeout=timeout,
        before_ids=before_ids or set(),
        pattern=re.compile(r"\b(\d{6,8})\b"),
    )
    if not code:
        raise TimeoutError("未收到 GitHub launch code")
    _log(log_fn, f"[GitHub] launch code: {code}")
    return str(code)


def register_github(
    page,
    *,
    email: str,
    password: str | None = None,
    username: str | None = None,
    captcha=None,
    mailbox=None,
    mail_account=None,
    before_ids: set | None = None,
    otp_timeout: int = 120,
    log_fn: Callable | None = None,
    control=None,
    country: str = "United States of America",
    skip_variants: tuple[str, ...] = ("character",),
) -> dict:
    password = password or rand_password()
    username = username or rand_username()
    _checkpoint(control)
    _log(log_fn, f"[GitHub] open {SIGNUP_URL}")
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(1.5)

    # Email-first progressive form (GitHub evolves; try common selectors).
    filled_email = (
        _fill(page, 'input[name="user[email]"], input#email, input[type=email]', email, "email", log_fn)
        or _fill(page, "input[type=email]", email, "email", log_fn)
    )
    if not filled_email:
        raise RuntimeError("无法填写 GitHub 邮箱")
    time.sleep(0.6)
    click_create_account(page)
    time.sleep(0.8)
    _fill(
        page,
        'input[name="user[password]"], input#password, input[type=password]',
        password,
        "password",
        log_fn,
    )
    time.sleep(0.5)
    _fill(
        page,
        'input[name="user[login]"], input#login, input[name=username]',
        username,
        "username",
        log_fn,
    )
    time.sleep(0.5)

    # Country optional
    try:
        opener = page.locator('button:has-text("Country"), button:has-text("Region"), [aria-label*="Country" i]').first
        if opener.count() > 0:
            opener.click(timeout=3000)
            time.sleep(0.3)
            page.get_by_text(country, exact=False).first.click(timeout=3000)
    except Exception:
        pass

    # Trigger captcha
    triggered = False
    for _ in range(4):
        _checkpoint(control)
        click_create_account(page)
        if detect_captcha(page, max_wait=8):
            triggered = True
            break
    if triggered:
        _log(log_fn, "[GitHub] Arkose 出现，开始求解")
        ok = solve_arkose(
            page,
            captcha,
            log_fn=log_fn,
            control=control,
            skip_variants=skip_variants,
        )
        if not ok:
            # manual wait window
            _log(log_fn, "[GitHub] 自动验证失败，等待人工 90s...")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                _checkpoint(control)
                if not detect_captcha(page, max_wait=2):
                    ok = True
                    break
                time.sleep(2)
        if not ok:
            raise RuntimeError("GitHub FunCaptcha / puzzle 未通过")
    else:
        _log(log_fn, "[GitHub] 未检测到 Arkose，继续")

    # Launch code
    code = None
    try:
        if page.locator('input[name="launch_code"], input#launch_code, input[placeholder*="code" i]').count() > 0:
            code = wait_launch_code(mailbox, mail_account, before_ids, otp_timeout, log_fn, control)
            _fill(
                page,
                'input[name="launch_code"], input#launch_code, input[placeholder*="code" i]',
                code,
                "launch_code",
                log_fn,
            )
            click_create_account(page)
            time.sleep(2)
    except Exception as exc:
        _log(log_fn, f"[GitHub] launch code step: {exc}")

    # Success heuristic
    url = page.url or ""
    cookies = {}
    try:
        cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    except Exception:
        pass
    logged_in = ("github.com" in url and "signup" not in url) or bool(cookies.get("user_session") or cookies.get("logged_in"))
    if not logged_in:
        # still accept if user_session set
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=2000) or ""
        except Exception:
            pass
        if "Verify" in body and "account" in body.lower():
            raise RuntimeError("GitHub 仍停在验证页")
        _log(log_fn, f"[GitHub] 警告: 登录态不确定 url={url}")

    return {
        "email": email,
        "password": password,
        "username": username,
        "url": url,
        "cookies": cookies,
        "launch_code": code or "",
    }
