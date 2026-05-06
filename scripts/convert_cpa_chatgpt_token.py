#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WARNING_BANNER = (
    "!!!!!!!!!!!!!!!!!!!! DO NOT SHARE ANY PART OF THE INFORMATION YOU SEE HERE. "
    "THIS INFORMATION IS SENSITIVE AND CAN GRANT ACCESS TO YOUR ACCOUNT. "
    "SHARING THIS INFORMATION IS LIKE SHARING YOUR PASSWORD. !!!!!!!!!!!!!!!!!!!!"
)

DEFAULT_AMR = ["otp", "urn:openai:amr:otp_email"]


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    if not raw:
        return {}
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_auth_info(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("https://api.openai.com/auth")
    if isinstance(nested, dict) and nested:
        return nested

    flattened: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(key, str) and key.startswith("https://api.openai.com/auth."):
            flattened[key.rsplit(".", 1)[-1]] = value
    return flattened


def _extract_profile_info(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("https://api.openai.com/profile")
    return profile if isinstance(profile, dict) else {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _derive_display_name(email: str) -> str:
    local = str(email or "").split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
    parts = [part for part in local.split() if part]
    if not parts:
        return "OpenAI User"
    return " ".join(part[:1].upper() + part[1:] for part in parts[:3])


def _derive_idp(subject: str) -> str:
    sub = str(subject or "").strip()
    if not sub:
        return "auth0"
    if "|" in sub:
        return sub.split("|", 1)[0] or "auth0"
    return "auth0"


def _normalize_amr(*candidates: Any) -> list[str]:
    for candidate in candidates:
        if isinstance(candidate, list):
            values = [str(item).strip() for item in candidate if str(item).strip()]
            if values:
                return values
    return list(DEFAULT_AMR)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=UTC)

    text = str(value or "").strip()
    if not text:
        return None

    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC)

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_datetime_z(value: datetime | None) -> str:
    if value is None:
        return ""
    utc_value = value.astimezone(UTC)
    return utc_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def convert_cpa_token(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TypeError("input token must be a JSON object")

    source_user = data.get("user")
    source_user = source_user if isinstance(source_user, dict) else {}
    source_account = data.get("account")
    source_account = source_account if isinstance(source_account, dict) else {}

    access_token = _first_non_empty(data.get("access_token"), data.get("accessToken"))
    refresh_token = _first_non_empty(data.get("refresh_token"), data.get("refreshToken"))
    id_token = _first_non_empty(data.get("id_token"), data.get("idToken"))
    session_token = _first_non_empty(data.get("session_token"), data.get("sessionToken"), source_user.get("sessionToken"))

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth_info(access_payload)
    access_profile = _extract_profile_info(access_payload)

    id_payload = _decode_jwt_payload(id_token)
    id_auth = _extract_auth_info(id_payload)

    email = _first_non_empty(
        data.get("email"),
        source_user.get("email"),
        access_profile.get("email"),
        id_payload.get("email"),
    )
    name = _first_non_empty(
        source_user.get("name"),
        id_payload.get("name"),
        _derive_display_name(email),
    )
    subject = _first_non_empty(access_payload.get("sub"), id_payload.get("sub"))
    user_id = _first_non_empty(
        source_user.get("id"),
        access_auth.get("chatgpt_user_id"),
        id_auth.get("chatgpt_user_id"),
        access_auth.get("user_id"),
        id_auth.get("user_id"),
        subject,
    )
    account_id = _first_non_empty(
        source_account.get("id"),
        data.get("account_id"),
        access_auth.get("chatgpt_account_id"),
        id_auth.get("chatgpt_account_id"),
        access_auth.get("account_id"),
        id_auth.get("account_id"),
    )
    plan_type = _first_non_empty(
        source_account.get("planType"),
        access_auth.get("chatgpt_plan_type"),
        id_auth.get("chatgpt_plan_type"),
        "free",
    )
    compute_residency = _first_non_empty(
        source_account.get("computeResidency"),
        access_auth.get("chatgpt_compute_residency"),
        id_auth.get("chatgpt_compute_residency"),
        "no_constraint",
    )
    residency_region = _first_non_empty(
        source_account.get("residencyRegion"),
        access_auth.get("residency_region"),
        id_auth.get("residency_region"),
        compute_residency,
        "no_constraint",
    )
    structure = _first_non_empty(source_account.get("structure"), "personal")

    amr = _normalize_amr(
        source_user.get("amr"),
        access_auth.get("amr"),
        access_payload.get("amr"),
        id_payload.get("amr"),
    )
    issued_at = access_payload.get("iat")
    if not isinstance(issued_at, int):
        issued_at = id_payload.get("iat")
    if not isinstance(issued_at, int):
        issued_at = int(_parse_datetime(data.get("last_refresh") or data.get("expired") or 0).timestamp()) if _parse_datetime(data.get("last_refresh") or data.get("expired") or 0) else 0

    expires_at = (
        _parse_datetime(access_payload.get("exp"))
        or _parse_datetime(data.get("expired"))
        or _parse_datetime(data.get("expires"))
    )

    result = {
        "WARNING_BANNER": WARNING_BANNER,
        "user": {
            "id": user_id,
            "name": name,
            "email": email,
            "idp": _first_non_empty(source_user.get("idp"), _derive_idp(subject)),
            "iat": issued_at,
            "amr": amr,
            "mfa": _to_bool(source_user.get("mfa"), default=("mfa" in amr)),
        },
        "expires": _format_datetime_z(expires_at),
        "account": {
            "id": account_id,
            "planType": plan_type,
            "structure": structure,
            "isConversationClassifierEnabledForWorkspace": _to_bool(
                source_account.get("isConversationClassifierEnabledForWorkspace"),
                default=True,
            ),
            "isFinservEnabledWorkspace": _to_bool(
                source_account.get("isFinservEnabledWorkspace"),
                default=False,
            ),
            "isFedrampCompliantWorkspace": _to_bool(
                source_account.get("isFedrampCompliantWorkspace"),
                default=False,
            ),
            "isDelinquent": _to_bool(source_account.get("isDelinquent"), default=False),
            "residencyRegion": residency_region,
            "computeResidency": compute_residency,
        },
        "accessToken": access_token,
        "authProvider": _first_non_empty(data.get("authProvider"), "openai"),
    }

    if session_token:
        result["sessionToken"] = session_token
    if data.get("rumViewTags") is not None:
        result["rumViewTags"] = data["rumViewTags"]
    elif source_account or source_user or refresh_token:
        result["rumViewTags"] = {"light_account": {"fetched": False}}

    return result


def _load_json(path: str | None) -> dict[str, Any]:
    if not path or path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("input token must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a CPA ChatGPT token JSON into an OpenAI-style token JSON."
    )
    parser.add_argument("input", nargs="?", help="Input JSON file path. Use - or omit to read stdin.")
    parser.add_argument("-o", "--output", help="Output JSON file path. Omit to print to stdout.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write compact JSON instead of pretty-printed JSON.",
    )
    args = parser.parse_args(argv)

    converted = convert_cpa_token(_load_json(args.input))
    if args.compact:
        output = json.dumps(converted, ensure_ascii=False, separators=(",", ":"))
    else:
        output = json.dumps(converted, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    else:
        sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
