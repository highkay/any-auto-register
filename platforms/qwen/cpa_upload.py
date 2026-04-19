"""Qwen 账号导入 CPA 辅助。"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone, timedelta
from typing import Any


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    if not raw:
        return {}
    parts = raw.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded.decode("utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def generate_token_json(account: Any) -> dict[str, Any]:
    """生成适配 CPA 管理端 auth-file 的 Qwen token JSON。"""
    email = str(getattr(account, "email", "") or "").strip()
    access_token = str(getattr(account, "access_token", "") or "").strip()
    refresh_token = str(getattr(account, "refresh_token", "") or "").strip()
    resource_url = str(getattr(account, "resource_url", "") or "").strip()
    if not resource_url:
        resource_url = "portal.qwen.ai"
    payload = _decode_jwt_payload(access_token) if access_token else {}

    account_id = str(
        payload.get("id")
        or payload.get("user_id")
        or payload.get("sub")
        or ""
    ).strip()

    expired_str = ""
    exp_timestamp = payload.get("exp")
    if isinstance(exp_timestamp, int) and exp_timestamp > 0:
        exp_dt = datetime.fromtimestamp(exp_timestamp, tz=timezone(timedelta(hours=8)))
        expired_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    now = datetime.now(tz=timezone(timedelta(hours=8)))
    return {
        "type": "qwen",
        "provider": "qwen",
        "email": email,
        "expired": expired_str,
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "resource_url": resource_url,
        "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }


def upload_to_cpa(
    token_data: dict[str, Any],
    api_url: str | None = None,
    api_key: str | None = None,
) -> tuple[bool, str]:
    """复用通用 CPA 上传实现。"""
    access_token = str((token_data or {}).get("access_token") or "").strip()
    refresh_token = str((token_data or {}).get("refresh_token") or "").strip()
    if not access_token:
        return False, "Qwen 凭证缺少 access_token"
    if not refresh_token:
        return (
            False,
            "Qwen 凭证缺少 refresh_token（需 Qwen OAuth 设备登录凭证，普通网页 token 不可用）",
        )

    from platforms.chatgpt.cpa_upload import upload_to_cpa as _upload

    return _upload(token_data, api_url=api_url, api_key=api_key)
