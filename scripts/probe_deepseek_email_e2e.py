#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_mailbox import create_mailbox  # noqa: E402
from core.base_platform import RegisterConfig  # noqa: E402
from core.config_store import config_store  # noqa: E402
from platforms.deepseek.core import random_password  # noqa: E402
from platforms.deepseek.plugin import DeepSeekPlatform  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe DeepSeek email registration end-to-end."
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="Playwright proxy URL, for example socks5://192.168.1.18:1080",
    )
    parser.add_argument("--ui-locale", default="en-US", help="Browser locale")
    parser.add_argument("--region", default="US", help="DeepSeek region")
    parser.add_argument(
        "--tz-offset-seconds",
        default="32400",
        help="DeepSeek timezone offset in seconds",
    )
    parser.add_argument(
        "--mail-provider",
        default="",
        help="Override the mailbox provider from config_store",
    )
    parser.add_argument(
        "--mail-domain",
        default="",
        help="Optional mailbox domain override for providers that support it",
    )
    parser.add_argument(
        "--password",
        default="",
        help="Optional account password. Random password is used if omitted.",
    )
    parser.add_argument(
        "--captcha-solver",
        default="yescaptcha",
        choices=["yescaptcha", "local_solver", "manual"],
        help="Captcha solver mode passed into RegisterConfig.",
    )
    parser.add_argument(
        "--flaresolverr-url",
        default="",
        help="Optional FlareSolverr endpoint, for example http://127.0.0.1:8191/v1",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Launch the browser in headed mode instead of headless.",
    )
    parser.add_argument(
        "--mailbox-attempts",
        default="8",
        help="How many mailbox allocations to try before failing.",
    )
    parser.add_argument(
        "--user-data-dir",
        default="",
        help="Optional persistent browser profile directory for headed/headless Playwright.",
    )
    return parser.parse_args()


def _mask_email(email: str) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 4:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-2:]}@{domain}"


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _prefetch_mail_account(mailbox, *, retries: int = 3, delay_seconds: float = 5.0):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return mailbox.get_email()
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            print(
                json.dumps(
                    {
                        "mailbox_prefetch_retry": attempt,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
            time.sleep(delay_seconds * attempt)
    assert last_error is not None
    raise last_error


def main() -> int:
    args = _parse_args()

    base_extra = config_store.get_all().copy()
    if str(args.mail_provider or "").strip():
        base_extra["mail_provider"] = str(args.mail_provider).strip()
    base_extra["deepseek_ui_locale"] = args.ui_locale
    base_extra["deepseek_region"] = args.region
    base_extra["deepseek_tz_offset_seconds"] = args.tz_offset_seconds
    base_extra["deepseek_mailbox_attempts"] = args.mailbox_attempts
    if str(args.flaresolverr_url or "").strip():
        base_extra["deepseek_flaresolverr_url"] = str(args.flaresolverr_url).strip()
    user_data_dir = ""
    if str(args.user_data_dir or "").strip():
        user_data_dir = str(Path(args.user_data_dir).expanduser().resolve())
        base_extra["deepseek_browser_user_data_dir"] = user_data_dir
    if str(args.mail_domain or "").strip():
        domain_override = str(args.mail_domain).strip().lstrip("@")
        for key in (
            "imail_domain",
            "edumail_domain",
            "boomlify_domain",
            "nullsto_domain",
            "gptmail_domain",
            "maliapi_domain",
            "duckmail_domain",
            "skymail_domain",
            "cloudmail_domain",
            "freemail_domain",
            "opentrashmail_domain",
            "cfrouting_domain",
            "cfworker_domain",
            "cfworker_domain_override",
            "cfworker_domains",
            "cfworker_enabled_domains",
            "laoudo_email",
        ):
            if key.endswith("_email"):
                continue
            base_extra[key] = domain_override

    mail_provider = str(base_extra.get("mail_provider") or "luckmail").strip() or "luckmail"
    proxy = str(args.proxy or "").strip() or None
    password = str(args.password or "").strip() or random_password()

    print(
        json.dumps(
            {
                "mail_provider": mail_provider,
                "proxy": proxy or "",
                "ui_locale": args.ui_locale,
                "region": args.region,
                "headed": bool(args.headed),
                "captcha_solver": args.captcha_solver,
                "flaresolverr_url": str(args.flaresolverr_url or "").strip(),
                "user_data_dir": user_data_dir,
            },
            ensure_ascii=False,
        )
    )

    mailbox = create_mailbox(
        provider=mail_provider,
        extra=base_extra,
        proxy=proxy,
        platform="deepseek",
    )
    platform = DeepSeekPlatform(
        config=RegisterConfig(
            executor_type="headed" if args.headed else "headless",
            captcha_solver=args.captcha_solver,
            proxy=proxy,
            extra=base_extra,
        ),
        mailbox=mailbox,
    )
    platform._log_fn = print

    try:
        account = platform.register(email="", password=password)
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": str(exc),
                "mail_provider": mail_provider,
                "proxy": proxy or "",
                "ui_locale": args.ui_locale,
                "captcha_solver": args.captcha_solver,
                "flaresolverr_url": str(args.flaresolverr_url or "").strip(),
                "user_data_dir": user_data_dir,
            }
        )
        return 1

    account_extra = account.extra or {}
    _print_json(
        {
            "ok": True,
            "platform": account.platform,
            "email": _mask_email(account.email),
            "user_id": account.user_id,
            "mail_provider": mail_provider,
            "proxy": proxy or "",
            "ui_locale": args.ui_locale,
            "captcha_solver": args.captcha_solver,
            "flaresolverr_url": str(args.flaresolverr_url or "").strip(),
            "user_data_dir": user_data_dir,
            "register_via": account_extra.get("register_via", ""),
            "need_birthday": bool(account_extra.get("need_birthday")),
            "username": _mask_email(str(account_extra.get("username") or account.email)),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
