#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from curl_cffi import requests as curl_requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_phone import HeroSMSPhoneService
from core.config_store import config_store

KIMI_COUNTRY_CODE_URL = "https://www.kimi.com/api/user/sms/country-code"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _match_kimi_country(
    items: list[dict[str, Any]],
    *,
    country_name: str,
    phone_code: str = "",
) -> dict[str, Any] | None:
    target_name = _normalize_text(country_name)
    target_code = str(phone_code or "").strip()

    for item in items:
        item_name = _normalize_text(item.get("country_name"))
        item_code = str(item.get("phone_code") or "").strip()
        if target_name and target_name == item_name:
            return item
        if target_code and item_code == target_code:
            return item

    for item in items:
        item_name = _normalize_text(item.get("country_name"))
        if target_name and target_name in item_name:
            return item
    return None


def _fetch_kimi_country_codes() -> dict[str, Any]:
    response = curl_requests.get(KIMI_COUNTRY_CODE_URL, timeout=20)
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Kimi 国家码接口返回异常: {type(payload).__name__}")
    return {
        "status": response.status_code,
        "item_count": len(payload),
        "items": payload,
    }


def _probe_kimi_country(country_name: str, phone_code: str = "") -> dict[str, Any]:
    payload = _fetch_kimi_country_codes()
    items = payload.pop("items")
    match = _match_kimi_country(items, country_name=country_name, phone_code=phone_code)
    payload["ok"] = bool(match)
    payload["match"] = (
        {
            "country_name": str(match.get("country_name") or ""),
            "phone_code": str(match.get("phone_code") or ""),
        }
        if match
        else None
    )
    return payload


def _build_hero_sms_config(
    all_config: dict[str, Any],
    *,
    api_key: str,
    service: str,
    country_name: str,
) -> dict[str, Any]:
    data = dict(all_config or {})
    if api_key:
        data["hero_sms_api_key"] = api_key
    if service:
        data["hero_sms_service"] = service
    if country_name:
        data["hero_sms_country"] = country_name
    return data


def _probe_hero_sms_offer(
    all_config: dict[str, Any],
    *,
    api_key: str,
    service: str,
    country_name: str,
) -> dict[str, Any]:
    config = _build_hero_sms_config(
        all_config,
        api_key=api_key,
        service=service,
        country_name=country_name,
    )
    service_client = HeroSMSPhoneService(config)
    result: dict[str, Any] = {
        "enabled": service_client.enabled,
        "requested_service": service_client.requested_service,
        "requested_country": service_client.requested_country,
    }
    if not service_client.enabled:
        result["ok"] = False
        result["error"] = "HeroSMS API key not configured"
        return result

    try:
        service_code = service_client._resolve_service_code()
        country_id = service_client._resolve_requested_country_id()
        candidates = service_client._build_offer_candidates(service_code)
    except Exception as exc:
        result["ok"] = False
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        return result

    top_candidate = candidates[0] if candidates else None
    result.update(
        {
            "ok": True,
            "service_code": service_code,
            "country_id": country_id,
            "candidate_count": len(candidates),
            "top_candidate": top_candidate,
        }
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "No-submit technical probe for the Kimi phone route. "
            "It only reads the Kimi public country-code list and HeroSMS offer surfaces."
        )
    )
    parser.add_argument("--country", default="Chile", help="Country name, e.g. Chile")
    parser.add_argument("--phone-code", default="", help="Expected Kimi phone code, e.g. +56")
    parser.add_argument("--service", default="Kimi", help="HeroSMS service label/code")
    parser.add_argument("--attempts", type=int, default=1, help="Probe attempts")
    parser.add_argument("--pause-seconds", type=float, default=1.0, help="Pause between attempts")
    parser.add_argument(
        "--hero-sms-api-key",
        default="",
        help="Optional HeroSMS API key. Falls back to project config/env when omitted.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    all_config = config_store.get_all()
    api_key = str(args.hero_sms_api_key or all_config.get("hero_sms_api_key") or "").strip()
    attempts = max(int(args.attempts or 1), 1)
    results: list[dict[str, Any]] = []

    for index in range(attempts):
        results.append(
            {
                "attempt": index + 1,
                "at": _utc_now_iso(),
                "kimi_country_probe": _probe_kimi_country(args.country, args.phone_code),
                "hero_sms_offer_probe": _probe_hero_sms_offer(
                    all_config,
                    api_key=api_key,
                    service=args.service,
                    country_name=args.country,
                ),
            }
        )
        if index + 1 < attempts:
            time.sleep(max(float(args.pause_seconds or 0), 0.0))

    print(
        json.dumps(
            {
                "safe_mode": "no_submit_no_buy_no_sms",
                "country": args.country,
                "phone_code": args.phone_code,
                "service": args.service,
                "attempts": attempts,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
