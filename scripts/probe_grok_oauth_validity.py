#!/usr/bin/env python3
"""Probe Grok SSO → Device OAuth → refresh_token validity.

Usage:
  uv run python scripts/probe_grok_oauth_validity.py
  uv run python scripts/probe_grok_oauth_validity.py --email someone@example.com
  uv run python scripts/probe_grok_oauth_validity.py --limit 5 --proxy http://127.0.0.1:7890

Does not register new accounts. Uses existing grok rows with SSO cookies.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from sqlmodel import Session, select

from core.db import AccountModel, engine, init_db
from core.proxy_utils import build_requests_proxy_config
from platforms.grok.cpa_xai import (
    CLIENT_ID,
    TOKEN_URL,
    _confirm_device_oauth_http,
    _mint_xai_device_token_http,
    _request_device_code,
)
from platforms.grok.protocol_client import is_session_sso, jwt_payload_map


def _account_sso(account: AccountModel) -> str:
    extra: dict = {}
    try:
        extra = json.loads(account.extra_json or "{}")
    except Exception:
        extra = {}
    return str(extra.get("sso") or extra.get("sso_token") or account.token or "").strip()


def _sso_summary(sso: str) -> str:
    if not sso:
        return "empty"
    payload = jwt_payload_map(sso) or {}
    keys = ",".join(sorted(payload.keys())[:8])
    return f"len={len(sso)} sessionish={is_session_sso(sso)} keys=[{keys}]"


def _try_refresh(refresh_token: str, proxy: str | None) -> tuple[bool, str]:
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
        headers={"Accept": "application/json"},
        proxies=build_requests_proxy_config(proxy),
        timeout=30,
    )
    text = (response.text or "")[:220].replace("\n", " ")
    if response.status_code == 200:
        try:
            body = response.json()
        except Exception:
            return False, f"HTTP 200 non-json: {text}"
        if body.get("access_token"):
            return True, f"refresh_ok access_len={len(str(body.get('access_token')))}"
        return False, f"HTTP 200 missing access_token: {text}"
    return False, f"HTTP {response.status_code}: {text}"


def _try_device_oauth(sso: str, proxy: str | None, log) -> tuple[bool, str]:
    class _Acc:
        email = "probe@local"
        password = "x"
        token = sso
        extra = {"sso": sso}

    try:
        token = _mint_xai_device_token_http(
            _Acc(),
            proxy=proxy,
            timeout_seconds=90,
            log=log,
        )
    except Exception as exc:
        return False, f"mint_fail: {type(exc).__name__}: {exc}"

    ok, detail = _try_refresh(token.refresh_token, proxy)
    if ok:
        return True, (
            f"oauth_ok access_len={len(token.access_token)} "
            f"refresh_len={len(token.refresh_token)} | {detail}"
        )
    return False, (
        f"oauth_token_ok_but_refresh_fail access_len={len(token.access_token)} | {detail}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://127.0.0.1:7890")
    parser.add_argument("--email", default="")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--quiet-http", action="store_true")
    args = parser.parse_args()
    proxy = (args.proxy or "").strip() or None
    log = (lambda *_: None) if args.quiet_http else print

    init_db()
    print("=== device code smoke ===", flush=True)
    try:
        dev = _request_device_code(proxy)
        print("device_code_ok", dev.user_code, dev.verification_uri_complete, flush=True)
    except Exception as e:
        print("device_code_fail", type(e).__name__, e, flush=True)
        return 2

    with Session(engine) as session:
        query = (
            select(AccountModel)
            .where(AccountModel.platform == "grok")
            .order_by(AccountModel.id.desc())
        )
        rows = list(session.exec(query).all())

    if args.email:
        rows = [r for r in rows if str(r.email or "").lower() == args.email.lower()]

    picked = []
    for row in rows:
        sso = _account_sso(row)
        if sso:
            picked.append((row, sso))
        if len(picked) >= max(1, args.limit):
            break

    if not picked:
        print("NO_SSO_ACCOUNTS", flush=True)
        return 3

    print(f"=== oauth probe n={len(picked)} proxy={proxy or 'direct'} ===", flush=True)
    ok_n = fail_n = 0
    for account, sso in picked:
        print(f"\n-- {account.email} id={account.id} {_sso_summary(sso)}", flush=True)
        ok, detail = _try_device_oauth(sso, proxy, log)
        print(("OK " if ok else "FAIL"), detail, flush=True)
        if ok:
            ok_n += 1
        else:
            fail_n += 1

    print(f"\nSUMMARY ok={ok_n} fail={fail_n}", flush=True)
    return 0 if ok_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
