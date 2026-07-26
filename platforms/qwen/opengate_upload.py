"""OpenGate (Qwen Gate) account upload helper.

OpenGate dashboard accepts email + password and performs its own login:
  POST {base}/api/accounts
  Authorization: Bearer <API_KEY>
  {"email": "...", "password": "..."}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from sqlmodel import Session

from core.db import AccountModel, engine

SYNC_NAME = "opengate"
DEFAULT_OPENGATE_URL = "http://192.168.1.18:7860"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_config_value(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, default) or default or "").strip()
    except Exception:
        return str(default or "").strip()


def _get_account_extra(account: Any) -> dict[str, Any]:
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {})
    if isinstance(extra, dict):
        return extra
    raw = getattr(account, "extra_json", None)
    if isinstance(raw, str) and raw.strip():
        try:
            import json

            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _record_sync_result(
    extra: dict[str, Any],
    *,
    ok: bool,
    msg: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sync_statuses = extra.get("sync_statuses")
    if not isinstance(sync_statuses, dict):
        sync_statuses = {}

    state = sync_statuses.get(SYNC_NAME)
    if not isinstance(state, dict):
        state = {}

    now = _utcnow_iso()
    state["last_attempt_ok"] = bool(ok)
    state["last_message"] = msg
    state["last_attempt_at"] = now
    state["uploaded"] = bool(state.get("uploaded")) or bool(ok)
    if ok:
        state["uploaded_at"] = now
    if detail:
        state["detail"] = detail

    sync_statuses[SYNC_NAME] = state
    extra["sync_statuses"] = sync_statuses
    return state


def persist_opengate_sync_result(
    account: Any,
    *,
    ok: bool,
    msg: str,
    detail: dict[str, Any] | None = None,
) -> None:
    if isinstance(account, AccountModel) and account.id is not None:
        with Session(engine) as session:
            row = session.get(AccountModel, account.id)
            if row is not None:
                extra = row.get_extra()
                _record_sync_result(extra, ok=ok, msg=msg, detail=detail)
                row.set_extra(extra)
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
                session.commit()
                session.refresh(row)
                return

    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        _record_sync_result(extra, ok=ok, msg=msg, detail=detail)


def _normalize_base_url(api_url: str | None) -> str:
    raw = str(api_url or "").strip()
    if not raw:
        return ""
    # Accept dashboard URLs like http://host:7860/dashboard
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return raw.rstrip("/")


def _build_accounts_url(api_url: str | None) -> str:
    base = _normalize_base_url(api_url)
    if not base:
        return ""
    if base.endswith("/api/accounts"):
        return base
    return f"{base}/api/accounts"


def _auth_headers(api_key: str) -> dict[str, str]:
    token = str(api_key or "").strip()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _extract_error_message(payload: Any, *, status_code: int, raw_text: str = "") -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return message
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    text = str(raw_text or "").strip()
    if text:
        return text[:300]
    return f"OpenGate 请求失败: HTTP {status_code}"


def upload_to_opengate(
    account: Any,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    timeout: int = 60,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Upload a Qwen account (email + password) into an OpenGate instance."""
    resolved_url = str(api_url or "").strip() or _get_config_value(
        "opengate_api_url",
        DEFAULT_OPENGATE_URL,
    )
    resolved_key = str(api_key or "").strip() or _get_config_value("opengate_api_key", "")
    accounts_url = _build_accounts_url(resolved_url)
    if not accounts_url:
        return False, "OpenGate URL 未配置", {}

    email = str(getattr(account, "email", "") or "").strip()
    password = str(getattr(account, "password", "") or "").strip()
    if not email:
        return False, "账号缺少 email", {"accounts_url": accounts_url}
    if not password:
        return False, "账号缺少 password（OpenGate 需要用户名密码登录）", {
            "accounts_url": accounts_url,
            "email": email,
        }

    client = session or requests.Session()
    try:
        response = client.post(
            accounts_url,
            headers=_auth_headers(resolved_key),
            json={"email": email, "password": password},
            timeout=timeout,
        )
    except Exception as exc:
        return (
            False,
            f"请求 OpenGate 失败: {exc}",
            {"accounts_url": accounts_url, "email": email},
        )

    detail: dict[str, Any] = {
        "accounts_url": accounts_url,
        "email": email,
        "status_code": response.status_code,
    }
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": (response.text or "").strip()}
    detail["response"] = payload

    # Already registered on OpenGate — treat as success for sync purposes.
    if response.status_code == 409:
        msg = _extract_error_message(
            payload,
            status_code=response.status_code,
            raw_text=response.text,
        )
        return True, f"OpenGate 已存在: {email} ({msg})", detail

    if response.status_code >= 400:
        return (
            False,
            _extract_error_message(
                payload,
                status_code=response.status_code,
                raw_text=response.text,
            ),
            detail,
        )

    login_ok = None
    login_error = ""
    if isinstance(payload, dict):
        if "loginSucceeded" in payload:
            login_ok = bool(payload.get("loginSucceeded"))
        login_error = str(payload.get("loginError") or "").strip()

    if login_ok is True:
        return True, f"已导入 OpenGate 并登录成功: {email}", detail
    if login_ok is False:
        suffix = f"（登录失败: {login_error}）" if login_error else "（账号已添加，登录未成功）"
        return True, f"已导入 OpenGate: {email}{suffix}", detail
    return True, f"已导入 OpenGate: {email}", detail
