#!/usr/bin/env python3
"""End-to-end: headed register (manual Turnstile OK) → Device OAuth → refresh.

Usage:
  uv run python scripts/probe_grok_register_oauth_e2e.py
  uv run python scripts/probe_grok_register_oauth_e2e.py --proxy http://127.0.0.1:7890

Opens a headed browser. If Turnstile auto-fails, wait and click manually.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_mailbox import create_mailbox
from core.config_store import config_store
from core.db import init_db
from platforms.grok.castle import mint_castle_request_token
from platforms.grok.cpa_xai import CLIENT_ID, TOKEN_URL, _mint_xai_device_token_http
from platforms.grok.protocol_register import GrokProtocolRegister


def _mailbox_extra() -> dict:
    keys = [
        "cfworker_api_url",
        "cfworker_admin_token",
        "cfworker_custom_auth",
        "cfworker_domains",
        "cfworker_enabled_domains",
        "cfworker_domain",
        "cfworker_subdomain",
        "cfworker_random_subdomain",
        "cfworker_random_name_subdomain",
        "email_domain_level_count",
        "outlookemail_base_url",
        "outlookemail_api_key",
        "outlookemail_password",
        "outlookemail_group_id",
    ]
    return {k: config_store.get(k, "") for k in keys if config_store.get(k, "")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--mail-provider", default="")
    parser.add_argument("--password", default="TestPass123,,,aA1")
    args = parser.parse_args()
    proxy = (args.proxy or "").strip() or None

    init_db()
    provider = (args.mail_provider or config_store.get("mail_provider", "cfworker") or "cfworker").strip()
    extra = _mailbox_extra()
    extra.update(
        {
            "grok_clearance_mode": "auto",
            "grok_flaresolverr_url": config_store.get("grok_flaresolverr_url", "")
            or "http://127.0.0.1:8191/v1",
            "grok_browser_mode": "headed",
            "grok_force_visible_browser": "1",
            "grok_manual_turnstile": "1",
            "grok_manual_turnstile_timeout": config_store.get(
                "grok_manual_turnstile_timeout", "300"
            )
            or "300",
        }
    )

    print("=== 0) Castle smoke ===", flush=True)
    try:
        ctok = mint_castle_request_token(proxy=proxy, log_fn=print)
        print("castle_ok", len(ctok), flush=True)
    except Exception as e:
        print("castle_fail", type(e).__name__, e, flush=True)

    print(f"=== 1) mailbox provider={provider} ===", flush=True)
    mb = create_mailbox(provider, extra=extra, proxy=None, platform="grok")
    acct = mb.get_email()
    email = acct.email
    before = set(mb.get_current_ids(acct))
    print("email", email, flush=True)

    def otp() -> str:
        print("waiting otp...", flush=True)
        code = mb.wait_for_code(
            acct,
            keyword="",
            timeout=180,
            before_ids=before,
            code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}",
        )
        if code:
            try:
                before.clear()
                before.update(mb.get_current_ids(acct))
            except Exception:
                pass
        print("otp", code, flush=True)
        return code or ""

    print("=== 2) headed register (manual Turnstile allowed) ===", flush=True)
    reg = GrokProtocolRegister(proxy=proxy, log_fn=print, extra=extra)
    try:
        result = reg.register(
            email=email,
            password=args.password,
            otp_callback=otp,
        )
    except Exception as e:
        print("REGISTER_FAIL", type(e).__name__, e, flush=True)
        return 1

    sso = str(result.get("sso") or "").strip()
    print(
        "REGISTER_OK",
        result.get("email"),
        "sso_len",
        len(sso),
        "mode",
        result.get("register_mode"),
        flush=True,
    )
    if not sso:
        print("NO_SSO", flush=True)
        return 2

    print("=== 3) Device OAuth via HTTP SSO ===", flush=True)
    account = SimpleNamespace(
        email=result.get("email"),
        password=result.get("password"),
        token=sso,
        extra={
            "sso": sso,
            "sso_rw": result.get("sso_rw") or "",
            "grok_session_cookies": result.get("cookies") or [],
        },
    )
    try:
        import requests
        from core.proxy_utils import build_requests_proxy_config

        minted = _mint_xai_device_token_http(
            account,
            proxy=proxy,
            timeout_seconds=120,
            log=print,
        )
        print(
            "OAUTH_TOKEN_OK",
            "access_len",
            len(minted.access_token),
            "refresh_len",
            len(minted.refresh_token),
            flush=True,
        )
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": minted.refresh_token,
                "client_id": CLIENT_ID,
            },
            headers={"Accept": "application/json"},
            proxies=build_requests_proxy_config(proxy),
            timeout=30,
        )
        print("REFRESH_HTTP", resp.status_code, (resp.text or "")[:180], flush=True)
        if resp.status_code == 200 and "access_token" in (resp.text or ""):
            print("E2E_OK credentials_valid", flush=True)
            return 0
        print("E2E_PARTIAL oauth_or_refresh_invalid", flush=True)
        return 3
    except Exception as e:
        print("OAUTH_FAIL", type(e).__name__, e, flush=True)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
