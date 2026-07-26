"""Upload existing Qwen accounts (email+password) from local DB to OpenGate."""

from __future__ import annotations

import json
import sys
import time

import requests
from sqlmodel import Session, select

from core.db import AccountModel, engine, init_db
from platforms.qwen.opengate_upload import persist_opengate_sync_result, upload_to_opengate


def main() -> int:
    init_db()
    opengate_url = "http://192.168.1.18:7860"

    before = requests.get(f"{opengate_url}/api/accounts", timeout=15).json()
    before_emails = {
        str(a.get("email") or "").lower() for a in (before.get("accounts") or [])
    }
    print(f"OpenGate before: {before.get('count')}", flush=True)

    results: list[dict] = []
    with Session(engine) as session:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "qwen")
            .order_by(AccountModel.id.asc())
        ).all()
        print(f"DB qwen accounts: {len(rows)}", flush=True)

        for row in rows:
            email = str(row.email or "").strip()
            password = str(row.password or "").strip()
            already = email.lower() in before_emails
            print(f"--- upload {email} (id={row.id}, already={already}) ---", flush=True)

            ok, msg, detail = upload_to_opengate(
                row,
                api_url=opengate_url,
                api_key="",
                timeout=90,
            )
            try:
                persist_opengate_sync_result(row, ok=ok, msg=msg, detail=detail)
            except Exception as exc:
                print(f"  persist warn: {exc}", flush=True)

            login_ok = None
            response = detail.get("response")
            if isinstance(response, dict):
                login_ok = response.get("loginSucceeded")

            item = {
                "id": row.id,
                "email": email,
                "has_password": bool(password),
                "ok": ok,
                "msg": msg,
                "status_code": detail.get("status_code"),
                "loginSucceeded": login_ok,
                "already_before": already,
            }
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            time.sleep(0.5)

    time.sleep(1.5)
    after = requests.get(f"{opengate_url}/api/accounts", timeout=15).json()
    after_accounts = after.get("accounts") or []
    after_map = {
        str(a.get("email") or "").lower(): a
        for a in after_accounts
        if isinstance(a, dict)
    }

    verified = []
    for item in results:
        email = str(item["email"]).lower()
        hit = after_map.get(email)
        verified.append(
            {
                "email": item["email"],
                "upload_ok": item["ok"],
                "on_opengate": hit is not None,
                "authenticated": None if hit is None else hit.get("authenticated"),
                "loginSucceeded": item.get("loginSucceeded"),
                "msg": item["msg"],
            }
        )

    summary = {
        "uploaded_ok": sum(1 for x in results if x["ok"]),
        "uploaded_fail": sum(1 for x in results if not x["ok"]),
        "opengate_before": before.get("count"),
        "opengate_after": after.get("count"),
        "verified": verified,
    }
    print("=== SUMMARY ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    if summary["uploaded_fail"]:
        return 2
    if sum(1 for x in verified if x["on_opengate"]) < len(results):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
