"""Outlook signup producer (browser, sync Playwright).

Writes successful accounts into ``OutlookAccountModel`` for consumption by
``mail_provider=microsoft`` / ``outlook`` (OutlookMailbox).
"""
from __future__ import annotations

import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlmodel import Session, select

from core.db import OutlookAccountModel, engine
from core.human_mouse import human_press_and_hold

SIGNUP_URL = "https://signup.live.com/signup"
MS_SIGNUP_ARKOSE_KEY = "B7D8911C-5CC8-A9A3-35B0-554ACEE604DA"
PX_APP_ID_DEFAULT = "PXzC5j78di"
# Public Microsoft consumer app id commonly used for device/code Graph mail read
GRAPH_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"


@dataclass
class OutlookProduceResult:
    ok: bool
    email: str = ""
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    account_type: str = "microsoft_oauth"
    error: str = ""
    extra: dict = field(default_factory=dict)


def _log(log_fn, msg: str) -> None:
    (log_fn or print)(msg)


def _checkpoint(control) -> None:
    if control is not None and hasattr(control, "checkpoint"):
        control.checkpoint()


def _rand_password() -> str:
    return "Ow1!" + "".join(random.choices(string.ascii_letters + string.digits, k=12))


def _rand_local() -> str:
    return "user" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def save_outlook_account(
    *,
    email: str,
    password: str,
    client_id: str = "",
    refresh_token: str = "",
    account_type: str = "microsoft_oauth",
    mailapi_url: str = "",
    enabled: bool = True,
) -> OutlookAccountModel:
    """Upsert into outlook_accounts for OutlookMailbox consumption."""
    email = str(email or "").strip().lower()
    account_type = str(account_type or "microsoft_oauth").strip() or "microsoft_oauth"
    if account_type not in {"microsoft_oauth", "mailapi_url"}:
        account_type = "microsoft_oauth"
    with Session(engine) as session:
        row = session.exec(
            select(OutlookAccountModel).where(OutlookAccountModel.email == email)
        ).first()
        if row is None:
            row = OutlookAccountModel(email=email, password=password or "")
        row.password = password or row.password
        row.client_id = client_id or row.client_id or GRAPH_CLIENT_ID
        row.refresh_token = refresh_token or row.refresh_token or ""
        row.account_type = account_type
        row.mailapi_url = mailapi_url or row.mailapi_url or ""
        row.enabled = bool(enabled)
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _fill(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(str(value))
                return True
        except Exception:
            continue
    return False


def _click(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=4000)
                return True
        except Exception:
            continue
    return False


def _solve_funcaptcha(page, captcha, *, log_fn=None, control=None) -> bool:
    if captcha is None:
        return False
    try:
        token = captcha.solve_funcaptcha(
            SIGNUP_URL,
            MS_SIGNUP_ARKOSE_KEY,
            timeout_seconds=180,
            interrupt_checker=(control.checkpoint if control else None),
        )
    except Exception as exc:
        _log(log_fn, f"[Outlook] FunCaptcha failed: {exc}")
        return False
    try:
        page.evaluate(
            """(tok) => {
                const hidden = document.querySelector('input[name="fc-token"], input[name="FunCaptcha"], #fc-token');
                if (hidden) { hidden.value = tok; }
                if (window.ArkoseEnforcement && window.ArkoseEnforcement.setConfig) {
                  try { /* noop */ } catch(e) {}
                }
                window.postMessage({ eventId: 'challenge-complete', payload: { sessionToken: tok } }, '*');
            }""",
            token,
        )
        time.sleep(1.5)
        return True
    except Exception as exc:
        _log(log_fn, f"[Outlook] FunCaptcha inject failed: {exc}")
        return False


def _solve_perimeterx(page, captcha, *, mode: str = "auto", app_id: str = PX_APP_ID_DEFAULT, log_fn=None, control=None) -> bool:
    mode = (mode or "auto").lower()
    # Detect hold button
    hold = None
    try:
        for sel in (
            '#px-captcha',
            'div[id*=px-captcha]',
            'button:has-text("Press and hold")',
            'div:has-text("Press & Hold")',
        ):
            loc = page.locator(sel).first
            if loc.count() > 0:
                hold = loc
                break
    except Exception:
        hold = None

    if mode in {"token", "auto"} and captcha is not None:
        try:
            sol = captcha.solve_perimeterx(
                page.url or SIGNUP_URL,
                app_id,
                interrupt_checker=(control.checkpoint if control else None),
            )
            if sol and sol.ok and sol.cookies:
                cookie_list = [
                    {"name": k, "value": v, "url": page.url or SIGNUP_URL}
                    for k, v in sol.cookies.items()
                ]
                page.context.add_cookies(cookie_list)
                page.reload(wait_until="domcontentloaded")
                time.sleep(1.0)
                _log(log_fn, f"[Outlook] PX token cookies applied method={sol.method}")
                return True
        except Exception as exc:
            _log(log_fn, f"[Outlook] PX token path failed: {exc}")

    if mode in {"human", "auto", "human_hold"} and hold is not None:
        try:
            box = hold.bounding_box()
            if not box:
                return False
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2

            def _done() -> bool:
                try:
                    return hold.count() == 0 or not hold.is_visible()
                except Exception:
                    return True

            held, passed = human_press_and_hold(
                page,
                cx,
                cy,
                is_done=_done,
                interrupt_checker=(control.checkpoint if control else None),
            )
            _log(log_fn, f"[Outlook] PX human_hold {held:.1f}s passed={passed}")
            return bool(passed)
        except Exception as exc:
            _log(log_fn, f"[Outlook] PX human_hold failed: {exc}")
    return False


def produce_outlook_account(
    page,
    *,
    captcha=None,
    desired_email: str | None = None,
    password: str | None = None,
    log_fn: Callable | None = None,
    control=None,
    px_mode: str = "auto",
    px_app_id: str = PX_APP_ID_DEFAULT,
    extract_graph_token: bool = False,
    require_graph_token: bool = False,
    persist: bool = True,
) -> OutlookProduceResult:
    """Run Outlook signup on *page* and optionally persist to outlook_accounts."""
    password = password or _rand_password()
    local = _rand_local()
    _checkpoint(control)
    _log(log_fn, f"[Outlook] open {SIGNUP_URL}")
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=90000)
    time.sleep(1.5)

    # Member name
    member = desired_email.split("@")[0] if desired_email and "@" in desired_email else local
    _fill(page, ['input[name="MemberName"]', "input#MemberName", 'input[type=email]'], member)
    _click(page, ['input[type=submit]', 'button[type=submit]', 'button:has-text("Next")'])
    time.sleep(1.2)

    # Live.com domain sometimes auto; try next
    _fill(page, ['input[name="Password"]', "input#Password", 'input[type=password]'], password)
    _click(page, ['input[type=submit]', 'button[type=submit]', 'button:has-text("Next")'])
    time.sleep(1.2)

    # Name
    _fill(page, ['input[name="FirstName"]', "#FirstName"], random.choice(["Alex", "Sam", "Chris", "Jordan"]))
    _fill(page, ['input[name="LastName"]', "#LastName"], random.choice(["Lee", "Wang", "Smith", "Brown"]))
    _click(page, ['input[type=submit]', 'button[type=submit]', 'button:has-text("Next")'])
    time.sleep(1.0)

    # Birthday
    try:
        for sel, val in (
            ('select[name="BirthMonth"]', "1"),
            ('select[name="BirthDay"]', "15"),
            ('select[name="BirthYear"]', "1990"),
        ):
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.select_option(val)
    except Exception:
        pass
    _click(page, ['input[type=submit]', 'button[type=submit]', 'button:has-text("Next")'])
    time.sleep(1.5)

    # Captcha / PX loops
    for _ in range(6):
        _checkpoint(control)
        body = ""
        try:
            body = (page.locator("body").inner_text(timeout=1500) or "").lower()
        except Exception:
            pass
        if "funCAPTcha" in body or "arkose" in body or page.locator("iframe[src*=arkose], iframe[src*=funcaptcha]").count() > 0:
            _solve_funcaptcha(page, captcha, log_fn=log_fn, control=control)
        if "press" in body and "hold" in body or page.locator("#px-captcha, div[id*=px-captcha]").count() > 0:
            _solve_perimeterx(page, captcha, mode=px_mode, app_id=px_app_id, log_fn=log_fn, control=control)
        _click(page, ['input[type=submit]', 'button[type=submit]', 'button:has-text("Next")', 'button:has-text("Yes")'])
        time.sleep(1.5)
        url = (page.url or "").lower()
        if "account.live.com" in url or "outlook.live.com" in url or "office.com" in url:
            break
        if "passkey" in body or "skip" in body:
            _click(page, ['button:has-text("Skip")', 'a:has-text("Skip")', 'button:has-text("No")'])

    # Determine email
    email = desired_email or ""
    if not email:
        try:
            # read from page or cookies
            email = page.evaluate(
                """() => {
                  const el = document.querySelector('#liveIDMemberName, #userDisplayName, [data-testid=persona-email]');
                  return el ? (el.textContent || el.value || '') : '';
                }"""
            ) or ""
            email = str(email).strip()
        except Exception:
            email = ""
    if not email:
        email = f"{member}@outlook.com"

    refresh_token = ""
    client_id = GRAPH_CLIENT_ID if extract_graph_token else ""
    # Graph token extraction is optional/complex; leave empty unless implemented later.
    if require_graph_token and not refresh_token:
        # Still persist password-only style with enabled=false if required? Design: use enabled flag.
        enabled = False
        _log(log_fn, "[Outlook] require_graph_token 但未拿到 refresh_token，enabled=false")
    else:
        enabled = True

    extra: dict[str, Any] = {"url": page.url or "", "px_mode": px_mode}
    if persist:
        try:
            save_outlook_account(
                email=email,
                password=password,
                client_id=client_id or GRAPH_CLIENT_ID,
                refresh_token=refresh_token,
                account_type="microsoft_oauth",
                enabled=enabled,
            )
            _log(log_fn, f"[Outlook] 已写入 outlook_accounts: {email} enabled={enabled}")
        except Exception as exc:
            return OutlookProduceResult(ok=False, email=email, password=password, error=f"入库失败: {exc}", extra=extra)

    return OutlookProduceResult(
        ok=True,
        email=email,
        password=password,
        client_id=client_id or GRAPH_CLIENT_ID,
        refresh_token=refresh_token,
        account_type="microsoft_oauth",
        extra=extra,
    )
