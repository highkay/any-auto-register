"""grok2api 自动导入"""

from __future__ import annotations

import logging
import json
from typing import Tuple

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

DEFAULT_POOL = "basic"
POOL_ALIASES = {
    "ssobasic": "basic",
    "sso_basic": "basic",
    "basic": "basic",
    "ssosuper": "super",
    "sso_super": "super",
    "super": "super",
    "ssoheavy": "heavy",
    "sso_heavy": "heavy",
    "heavy": "heavy",
    "auto": "auto",
}
LEGACY_POOL_NAMES = {
    "basic": "ssoBasic",
    "super": "ssoSuper",
}
DEFAULT_QUOTAS = {
    "basic": 80,
    "super": 140,
    "heavy": 140,
}


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "")
    except Exception:
        return ""


def _normalize_quota(pool_name: str, quota) -> int:
    if quota not in (None, ""):
        try:
            return int(quota)
        except Exception:
            pass
    return DEFAULT_QUOTAS.get(pool_name, DEFAULT_QUOTAS[DEFAULT_POOL])


def _normalize_pool_name(pool_name: str | None) -> str:
    raw = str(pool_name or "").strip()
    if not raw:
        return DEFAULT_POOL
    normalized = raw.replace("-", "_").strip().lower()
    return POOL_ALIASES.get(normalized, raw)


def _legacy_pool_name(pool_name: str) -> str:
    return LEGACY_POOL_NAMES.get(pool_name, pool_name)


def _admin_key_candidates(app_key: str) -> list[str]:
    raw = str(app_key or "").strip()
    candidates: list[str] = []
    for candidate in (raw, raw[3:] if raw.lower().startswith("sk-") else ""):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _extract_sso(account) -> str:
    extra = getattr(account, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
    if not extra and hasattr(account, "get_extra"):
        try:
            loaded = account.get_extra()
            if isinstance(loaded, dict):
                extra = loaded
        except Exception:
            extra = {}
    if not extra and hasattr(account, "extra_json"):
        try:
            loaded = json.loads(getattr(account, "extra_json", "") or "{}")
            if isinstance(loaded, dict):
                extra = loaded
        except Exception:
            extra = {}
    token = (
        extra.get("sso")
        or extra.get("sso_token")
        or extra.get("sso_rw")
        or getattr(account, "token", "")
    )
    token = str(token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def build_grok2api_payload(
    account,
    pool_name: str | None = None,
    quota=None,
) -> dict:
    token = _extract_sso(account)
    if not token:
        raise ValueError("账号缺少 sso token")

    pool_name = _normalize_pool_name(pool_name or _get_config_value("grok2api_pool"))
    email = getattr(account, "email", "")
    payload = {
        pool_name: [
            {
                "token": token,
                "status": "active",
                "quota": _normalize_quota(pool_name, quota or _get_config_value("grok2api_quota")),
                "tags": [],
                "note": f"auto-import:{email}" if email else "auto-import",
            }
        ]
    }
    return payload


def _request_options() -> dict:
    return {
        "proxies": None,
        "verify": False,
        "timeout": 30,
        "impersonate": "chrome110",
    }


def _build_headers(app_key: str) -> dict:
    return {
        "Authorization": f"Bearer {app_key}",
        "Content-Type": "application/json",
    }


def _build_token_item(account, pool_name: str | None = None, quota=None) -> tuple[str, dict]:
    payload = build_grok2api_payload(account, pool_name=pool_name, quota=quota)
    normalized_pool_name = next(iter(payload.keys()))
    return normalized_pool_name, payload[normalized_pool_name][0]


def _build_legacy_payload(
    account,
    pool_name: str,
    token_item: dict,
    quota=None,
) -> tuple[str, dict]:
    legacy_pool_name = _legacy_pool_name(pool_name)
    legacy_item = {
        "token": token_item["token"],
        "status": token_item.get("status") or "active",
        "quota": _normalize_quota(pool_name, quota or _get_config_value("grok2api_quota")),
        "tags": list(token_item.get("tags") or []),
        "note": token_item.get("note")
        or (f"auto-import:{getattr(account, 'email', '')}" if getattr(account, "email", "") else "auto-import"),
    }
    return legacy_pool_name, {legacy_pool_name: [legacy_item]}


def _format_http_error(prefix: str, resp) -> str:
    message = f"{prefix}: HTTP {resp.status_code}"
    try:
        detail = resp.json()
        if isinstance(detail, dict):
            detail_text = str(detail.get("message") or detail.get("detail") or "").strip()
            if detail_text:
                message = f"{prefix}: {detail_text}"
    except Exception:
        text = str(getattr(resp, "text", "") or "")[:200]
        if text:
            message = f"{message} - {text}"
    return message


def _is_missing_endpoint(resp) -> bool:
    return int(getattr(resp, "status_code", 0) or 0) in {404, 405}


def _is_auth_failure(resp) -> bool:
    return int(getattr(resp, "status_code", 0) or 0) in {401, 403}


def _load_existing_tokens(api_url: str, headers: dict) -> dict:
    resp = cffi_requests.get(
        f"{api_url.rstrip('/')}/v1/admin/tokens",
        headers=headers,
        **_request_options(),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"读取现有 tokens 失败: HTTP {resp.status_code} - {resp.text[:200]}")

    data = resp.json()
    tokens = data.get("tokens", {})
    if not isinstance(tokens, dict):
        raise RuntimeError("读取现有 tokens 失败: 响应格式异常")
    return tokens


def _merge_token(existing_tokens: dict, pool_name: str, token_item: dict) -> dict:
    merged: dict = {}
    new_token = str(token_item.get("token", "") or "").strip()

    for existing_pool_name, pool_tokens in existing_tokens.items():
        merged[existing_pool_name] = list(pool_tokens) if isinstance(pool_tokens, list) else []

    pool_list = merged.setdefault(pool_name, [])
    replaced = False

    for index, existing_item in enumerate(pool_list):
        if not isinstance(existing_item, dict):
            continue
        existing_token = str(existing_item.get("token", "") or "").strip()
        if existing_token == new_token:
            updated_item = dict(existing_item)
            updated_item.update(token_item)
            pool_list[index] = updated_item
            replaced = True
            break

    if not replaced:
        pool_list.append(token_item)

    return merged


def _upload_to_admin_api(
    api_url: str,
    headers: dict,
    pool_name: str,
    token_item: dict,
) -> tuple[bool | None, str]:
    resp = cffi_requests.post(
        f"{api_url.rstrip('/')}/admin/api/tokens/add",
        headers=headers,
        json={"pool": pool_name, "tokens": [token_item["token"]]},
        **_request_options(),
    )
    if resp.status_code in (200, 201):
        try:
            data = resp.json()
        except Exception:
            data = {}
        count = data.get("count") if isinstance(data, dict) else None
        skipped = data.get("skipped") if isinstance(data, dict) else None
        if skipped:
            return True, f"导入成功（已存在跳过 {skipped}）"
        if count is not None:
            return True, f"导入成功（新增 {count}）"
        return True, "导入成功"
    if _is_missing_endpoint(resp):
        return None, _format_http_error("新版 grok2api token 接口不可用", resp)
    if _is_auth_failure(resp):
        return False, _format_http_error("grok2api 鉴权失败", resp)
    return False, _format_http_error("导入失败", resp)


def _upload_to_legacy_admin_api(
    api_url: str,
    headers: dict,
    pool_name: str,
    token_item: dict,
    account,
    quota=None,
) -> tuple[bool, str]:
    upload_url = f"{api_url.rstrip('/')}/v1/admin/tokens"
    existing_tokens = _load_existing_tokens(api_url, headers)
    legacy_pool_name, legacy_payload = _build_legacy_payload(
        account,
        pool_name,
        token_item,
        quota=quota,
    )
    payload = _merge_token(
        existing_tokens,
        legacy_pool_name,
        legacy_payload[legacy_pool_name][0],
    )
    resp = cffi_requests.post(
        upload_url,
        headers=headers,
        json=payload,
        **_request_options(),
    )
    if resp.status_code in (200, 201):
        return True, "导入成功"
    return False, _format_http_error("导入失败", resp)


def upload_to_grok2api(
    account,
    api_url: str | None = None,
    app_key: str | None = None,
    pool_name: str | None = None,
    quota=None,
) -> Tuple[bool, str]:
    """上传 Grok 账号到 grok2api 管理接口。"""
    if not api_url:
        api_url = _get_config_value("grok2api_url")
    if not app_key:
        app_key = _get_config_value("grok2api_app_key")

    api_url = str(api_url or "").strip()
    app_key = str(app_key or "").strip()
    if not api_url:
        return False, "grok2api URL 未配置"
    if not app_key:
        return False, "grok2api App Key 未配置"

    pool_name, token_item = _build_token_item(account, pool_name=pool_name, quota=quota)

    try:
        last_msg = ""
        for candidate_key in _admin_key_candidates(app_key):
            headers = _build_headers(candidate_key)
            ok, msg = _upload_to_admin_api(api_url, headers, pool_name, token_item)
            last_msg = msg
            if ok is not None:
                if ok:
                    return True, msg
                if "鉴权" not in msg:
                    return False, msg
                continue
            legacy_ok, legacy_msg = _upload_to_legacy_admin_api(
                api_url,
                headers,
                pool_name,
                token_item,
                account,
                quota=quota,
            )
            if legacy_ok:
                return True, legacy_msg
            if "401" not in legacy_msg and "403" not in legacy_msg:
                return False, f"{legacy_msg}; {msg}" if msg else legacy_msg
            last_msg = legacy_msg
        return False, last_msg or "grok2api 鉴权失败"
    except Exception as e:
        logger.error(f"grok2api 导入异常: {e}")
        return False, f"导入异常: {e}"
