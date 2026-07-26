#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


API_BASE = "https://api.twilio.com/2010-04-01"


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll Twilio inbound SMS messages for a single Twilio number."
    )
    parser.add_argument(
        "--to",
        default=os.getenv("TWILIO_PHONE_NUMBER", "").strip(),
        help="Twilio phone number to inspect, in E.164 format. Defaults to TWILIO_PHONE_NUMBER.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.getenv("TWILIO_FETCH_LIMIT", "20")),
        help="How many recent messages to fetch each poll. Default: 20.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("TWILIO_POLL_INTERVAL", "5")),
        help="Polling interval in seconds. Default: 5.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Fetch once and exit.",
    )
    parser.add_argument(
        "--show-outbound",
        action="store_true",
        help="Also print outbound messages. By default only inbound messages are shown.",
    )
    return parser.parse_args()


def _format_timestamp(raw: str | None) -> str:
    text = str(raw or "").strip()
    if not text:
        return "-"
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return text


def _fetch_messages(
    *,
    account_sid: str,
    auth_token: str,
    to_number: str,
    limit: int,
) -> list[dict[str, Any]]:
    url = f"{API_BASE}/Accounts/{account_sid}/Messages.json"
    response = requests.get(
        url,
        params={
            "To": to_number,
            "PageSize": max(1, min(limit, 100)),
        },
        auth=HTTPBasicAuth(account_sid, auth_token),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return list(payload.get("messages") or [])


def _is_inbound(direction: str) -> bool:
    text = str(direction or "").strip().lower()
    return text.startswith("inbound")


def _print_message(message: dict[str, Any]) -> None:
    sid = str(message.get("sid") or "")
    direction = str(message.get("direction") or "")
    from_number = str(message.get("from") or "")
    to_number = str(message.get("to") or "")
    status = str(message.get("status") or "")
    body = str(message.get("body") or "").replace("\r", " ").replace("\n", " ").strip()
    body = body[:200] if body else ""
    sent_at = _format_timestamp(message.get("date_sent") or message.get("date_created"))
    num_media = str(message.get("num_media") or "0")

    print(f"[{sent_at}] {sid}")
    print(f"  direction={direction} status={status} from={from_number} to={to_number} media={num_media}")
    print(f"  body={body!r}")


def main() -> int:
    args = _parse_args()
    account_sid = _require_env("TWILIO_ACCOUNT_SID")
    auth_token = _require_env("TWILIO_AUTH_TOKEN")
    to_number = str(args.to or "").strip()
    if not to_number:
        raise SystemExit("missing --to or TWILIO_PHONE_NUMBER")

    print(f"Polling Twilio messages for to={to_number}, limit={args.limit}, interval={args.interval}s")
    seen_sids: set[str] = set()

    while True:
        try:
            messages = _fetch_messages(
                account_sid=account_sid,
                auth_token=auth_token,
                to_number=to_number,
                limit=args.limit,
            )
        except requests.HTTPError as exc:
            body = ""
            if exc.response is not None:
                body = exc.response.text[:500]
            print(f"HTTP error: {exc} body={body}", file=sys.stderr)
            return 1
        except requests.RequestException as exc:
            print(f"request error: {exc}", file=sys.stderr)
            return 1

        fresh = []
        for message in messages:
            sid = str(message.get("sid") or "")
            if not sid or sid in seen_sids:
                continue
            if not args.show_outbound and not _is_inbound(message.get("direction", "")):
                continue
            fresh.append(message)

        for message in reversed(fresh):
            sid = str(message.get("sid") or "")
            seen_sids.add(sid)
            _print_message(message)

        if args.once:
            break

        time.sleep(max(args.interval, 1.0))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
