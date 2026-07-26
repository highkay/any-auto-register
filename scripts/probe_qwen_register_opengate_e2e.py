"""E2E: Qwen register → OpenGate upload.

Modes:
  discard  — qwen2api style, no solver (default)
  manual   — headed browser, human solves Aliyun captcha
  solve    — legacy captcha solver path

Usage:
  uv run python scripts/probe_qwen_register_opengate_e2e.py --mode manual --count 3
  uv run python scripts/probe_qwen_register_opengate_e2e.py --mode discard --max-attempts 6
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime

import requests

from core.base_mailbox import create_mailbox
from core.base_platform import RegisterConfig
from core.config_store import config_store
from core.db import init_db
from platforms.qwen.opengate_upload import upload_to_opengate
from platforms.qwen.plugin import QwenPlatform
from services.external_sync import sync_account


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _mask(value: str, keep: int = 6) -> str:
    text = str(value or "")
    if len(text) <= keep * 2:
        return text[:2] + "***" if text else ""
    return f"{text[:keep]}...{text[-4:]}"


def _register_one(
    *,
    mail_provider: str,
    extra: dict,
    proxy: str | None,
    captcha_mode: str,
    executor_type: str,
) -> object:
    mailbox = create_mailbox(
        provider=mail_provider,
        extra=extra,
        proxy=None,
        platform="qwen",
    )
    mail_acct = mailbox.get_email()
    email = str(getattr(mail_acct, "email", "") or "").strip()
    if not email:
        raise RuntimeError("mailbox returned empty email")
    _log(f"mailbox email={email}")

    reg_config = RegisterConfig(
        executor_type=executor_type,
        proxy=proxy or None,
        extra=extra,
    )
    platform = QwenPlatform(config=reg_config, mailbox=mailbox)
    platform._log_fn = _log
    return platform.register(email=email, password=None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("discard", "manual", "solve"),
        default="manual",
        help="captcha strategy",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="how many successful accounts to produce (manual mode target)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=0,
        help="max register attempts (0 = count for manual, 6 for discard)",
    )
    parser.add_argument(
        "--executor",
        choices=("headed", "headless"),
        default="",
        help="override executor; manual defaults to headed",
    )
    args = parser.parse_args()

    captcha_mode = args.mode
    executor_type = args.executor or ("headed" if captcha_mode == "manual" else "headless")
    target_count = max(1, int(args.count))
    max_attempts = int(args.max_attempts) or (
        target_count if captcha_mode == "manual" else max(target_count, 6)
    )

    init_db()
    config_store.set_many(
        {
            "opengate_enabled": "1",
            "opengate_api_url": config_store.get("opengate_api_url")
            or "http://192.168.1.18:7860",
            "qwen_captcha_mode": captcha_mode,
        }
    )

    opengate_url = config_store.get("opengate_api_url", "http://192.168.1.18:7860")
    opengate_key = config_store.get("opengate_api_key", "")
    mail_provider = config_store.get("mail_provider", "cfworker") or "cfworker"

    _log("=== Qwen → OpenGate E2E ===")
    _log(f"captcha_mode={captcha_mode}")
    _log(f"executor={executor_type}")
    _log(f"target_success={target_count} max_attempts={max_attempts}")
    _log(f"mail_provider={mail_provider}")
    _log(f"opengate_url={opengate_url}")
    _log(f"opengate_key={_mask(opengate_key) if opengate_key else '(empty)'}")
    if captcha_mode == "manual":
        _log(">>> 请盯着弹出的浏览器窗口，出现阿里云滑块时手动拖过去 <<<")

    before = requests.get(f"{opengate_url.rstrip('/')}/api/accounts", timeout=10)
    before.raise_for_status()
    before_data = before.json()
    _log(f"OpenGate before: count={before_data.get('count')}")

    cfg_all = config_store.get_all()
    extra = dict(cfg_all)
    extra["mail_provider"] = mail_provider
    extra["qwen_captcha_mode"] = captcha_mode
    proxy = str(
        cfg_all.get("proxy") or cfg_all.get("HTTP_PROXY") or cfg_all.get("http_proxy") or ""
    ).strip()

    successes: list[dict] = []
    failures: list[str] = []
    t0 = time.time()

    for attempt in range(1, max_attempts + 1):
        if len(successes) >= target_count:
            break
        _log(f"--- attempt {attempt}/{max_attempts} (ok={len(successes)}/{target_count}) ---")
        try:
            account = _register_one(
                mail_provider=mail_provider,
                extra=extra,
                proxy=proxy or None,
                captcha_mode=captcha_mode,
                executor_type=executor_type,
            )
        except Exception as exc:
            msg = str(exc)
            failures.append(msg)
            _log(f"attempt failed: {msg}")
            traceback.print_exc()
            if captcha_mode == "manual":
                _log("manual 失败后继续下一邮箱（请继续在下个窗口手动过码）")
            continue

        extra_acc = account.extra if isinstance(account.extra, dict) else {}
        _log(f"register OK email={account.email} activated={extra_acc.get('activated')}")
        _log(f"  token={_mask(account.token, 10)}")
        _log(f"  refresh={_mask(str(extra_acc.get('refresh_token') or ''), 8)}")

        ok, msg, detail = upload_to_opengate(
            account,
            api_url=opengate_url,
            api_key=opengate_key or None,
        )
        _log(f"OpenGate upload ok={ok} msg={msg}")
        if not ok:
            failures.append(f"upload failed for {account.email}: {msg}")
            continue

        try:
            sync_results = sync_account(account, log=_log)
        except Exception as exc:
            sync_results = [{"name": "sync", "ok": False, "msg": str(exc)}]
            _log(f"sync warning: {exc}")

        successes.append(
            {
                "email": account.email,
                "activated": extra_acc.get("activated"),
                "has_refresh": bool(extra_acc.get("refresh_token")),
                "opengate_msg": msg,
                "sync_results": sync_results,
            }
        )
        _log(f"progress {len(successes)}/{target_count}")

    # verify OpenGate
    time.sleep(1.0)
    after = requests.get(f"{opengate_url.rstrip('/')}/api/accounts", timeout=10)
    after.raise_for_status()
    after_data = after.json()
    after_emails = {
        str(item.get("email") or "").strip().lower()
        for item in (after_data.get("accounts") or [])
        if isinstance(item, dict)
    }
    verified = [
        item
        for item in successes
        if str(item.get("email") or "").strip().lower() in after_emails
    ]

    summary = {
        "elapsed_s": round(time.time() - t0, 1),
        "captcha_mode": captcha_mode,
        "executor": executor_type,
        "success_count": len(successes),
        "verified_on_opengate": len(verified),
        "opengate_before": before_data.get("count"),
        "opengate_after": after_data.get("count"),
        "successes": successes,
        "failures": failures[-10:],
    }
    _log("=== SUMMARY ===")
    _log(json.dumps(summary, ensure_ascii=False, indent=2))

    if len(successes) < target_count:
        _log(f"INCOMPLETE: got {len(successes)}/{target_count}")
        return 3
    if len(verified) < len(successes):
        _log("WARN: some accounts registered but not visible on OpenGate list")
        return 4
    _log("=== CLOSED LOOP OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
