"""DS2API 管理后台导入辅助逻辑。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from sqlmodel import Session

from core.db import AccountModel, engine

SYNC_NAME = "ds2api"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_account_extra(account: Any) -> dict[str, Any]:
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {})
    return extra if isinstance(extra, dict) else {}


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


def persist_ds2api_sync_result(
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


def _build_accounts_url(api_url: str | None) -> str:
    base_url = str(api_url or "").strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/admin/accounts"):
        return base_url
    if base_url.endswith("/admin"):
        return f"{base_url}/accounts"
    return f"{base_url}/admin/accounts"


def _build_account_test_url(api_url: str | None) -> str:
    accounts_url = _build_accounts_url(api_url)
    if not accounts_url:
        return ""
    return f"{accounts_url}/test"


def _extract_response_payload(response: requests.Response) -> tuple[str, dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text, {"raw": text} if text else {}

    if isinstance(payload, dict):
        message = str(
            payload.get("detail")
            or payload.get("message")
            or payload.get("error")
            or ""
        ).strip()
        return message, payload

    text = str(payload).strip()
    return text, {"raw": payload}


def _is_duplicate_message(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    normalized = text.lower()
    return "已存在" in text or "already exists" in normalized


def _request_headers(admin_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }


def _build_account_payload(account: Any) -> tuple[dict[str, str], str]:
    extra = _get_account_extra(account)
    email = (
        str(getattr(account, "email", "") or "").strip()
        or str(extra.get("email") or "").strip()
        or str(extra.get("username") or "").strip()
    )
    mobile = (
        str(getattr(account, "mobile", "") or "").strip()
        or str(extra.get("mobile") or "").strip()
    )
    password = str(getattr(account, "password", "") or "").strip()
    if not password:
        raise RuntimeError("DeepSeek 账号缺少 password，无法同步到 DS2API")

    payload: dict[str, str] = {"password": password}
    identifier = ""
    if email:
        payload["email"] = email
        identifier = email
    elif mobile:
        payload["mobile"] = mobile
        identifier = mobile
    else:
        raise RuntimeError("DeepSeek 账号缺少 email/mobile，无法同步到 DS2API")

    display_name = str(extra.get("display_name") or extra.get("nickname") or "").strip()
    if display_name:
        payload["name"] = display_name

    return payload, identifier


def upload_to_ds2api(
    account: Any,
    *,
    api_url: str | None = None,
    admin_key: str | None = None,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    accounts_url = _build_accounts_url(api_url)
    test_url = _build_account_test_url(api_url)
    if not accounts_url:
        return False, "DS2API Admin URL 未配置", {}

    resolved_key = str(admin_key or "").strip()
    if not resolved_key:
        return False, "DS2API Admin Key 未配置", {"accounts_url": accounts_url}

    try:
        payload, identifier = _build_account_payload(account)
    except RuntimeError as exc:
        return False, str(exc), {"accounts_url": accounts_url}

    client = session or requests.Session()
    try:
        response = client.post(
            accounts_url,
            headers=_request_headers(resolved_key),
            json=payload,
            timeout=timeout,
        )
    except Exception as exc:
        return (
            False,
            f"请求 DS2API 失败: {exc}",
            {"accounts_url": accounts_url, "identifier": identifier},
        )

    message, response_payload = _extract_response_payload(response)
    detail = {
        "accounts_url": accounts_url,
        "test_url": test_url,
        "identifier": identifier,
        "add_status_code": response.status_code,
    }
    if isinstance(response_payload, dict):
        total_accounts = response_payload.get("total_accounts")
        if total_accounts is not None:
            detail["total_accounts"] = total_accounts

    duplicate = False
    add_message = message
    if response.status_code >= 400:
        if _is_duplicate_message(message):
            duplicate = True
            add_message = f"DS2API 已存在该账号: {identifier}"
            detail["duplicate"] = True
        else:
            return (
                False,
                message or f"DS2API 请求失败: HTTP {response.status_code}",
                detail,
            )
    else:
        total_accounts = detail.get("total_accounts")
        if total_accounts is not None:
            add_message = f"已导入 DS2API: {identifier}（总账号 {total_accounts}）"
        else:
            add_message = f"已导入 DS2API: {identifier}"

    try:
        refresh_response = client.post(
            test_url,
            headers=_request_headers(resolved_key),
            json={"identifier": identifier},
            timeout=timeout,
        )
    except Exception as exc:
        detail["refresh_success"] = False
        detail["refresh_message"] = str(exc)
        return False, f"{add_message}; 刷新 token 失败: {exc}", detail

    refresh_message, refresh_payload = _extract_response_payload(refresh_response)
    refresh_success = False
    if isinstance(refresh_payload, dict):
        refresh_success = bool(refresh_payload.get("success"))
        detail["refresh_payload"] = refresh_payload
        if "session_count" in refresh_payload:
            detail["session_count"] = refresh_payload.get("session_count")
    detail["refresh_status_code"] = refresh_response.status_code
    detail["refresh_success"] = refresh_success
    detail["refresh_message"] = refresh_message

    if refresh_response.status_code >= 400 or not refresh_success:
        failure_message = refresh_message or f"HTTP {refresh_response.status_code}"
        return False, f"{add_message}; 刷新 token 失败: {failure_message}", detail

    action_text = "已存在并刷新 token" if duplicate else "已导入并刷新 token"
    if duplicate:
        return True, f"DS2API {action_text}: {identifier}", detail
    return True, f"DS2API {action_text}: {identifier}", detail
