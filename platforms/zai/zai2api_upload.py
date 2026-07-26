"""zai2api token pool upload helper."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from sqlmodel import Session

from core.db import AccountModel, engine

SYNC_NAME = "zai2api"


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


def persist_zai2api_sync_result(
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


def _build_tokens_url(api_url: str | None) -> str:
    base_url = str(api_url or "").strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/v1/tokens"):
        return base_url
    return f"{base_url}/v1/tokens"


def _auth_headers(auth_token: str) -> dict[str, str]:
    token = str(auth_token or "").strip()
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def upload_to_zai2api(
    account: Any,
    *,
    api_url: str | None = None,
    auth_token: str | None = None,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    tokens_url = _build_tokens_url(api_url)
    if not tokens_url:
        return False, "zai2api URL 未配置", {}

    resolved_token = str(auth_token or "").strip()
    if not resolved_token:
        return False, "zai2api AUTH_TOKEN 未配置", {"tokens_url": tokens_url}

    extra = _get_account_extra(account)
    token_value = (
        str(getattr(account, "token", "") or "").strip()
        or str(extra.get("access_token") or "").strip()
    )
    if not token_value:
        return False, "Z.ai 账号缺少 bearer token", {"tokens_url": tokens_url}

    identifier = str(getattr(account, "email", "") or extra.get("username") or "").strip()
    client = session or requests.Session()
    try:
        response = client.post(
            tokens_url,
            headers=_auth_headers(resolved_token),
            json={"token": token_value},
            timeout=timeout,
        )
    except Exception as exc:
        return (
            False,
            f"请求 zai2api 失败: {exc}",
            {"tokens_url": tokens_url, "identifier": identifier},
        )

    detail = {
        "tokens_url": tokens_url,
        "identifier": identifier,
        "status_code": response.status_code,
    }
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text.strip()}
    detail["response"] = payload

    if response.status_code >= 400:
        if isinstance(payload, dict):
            message = str(
                payload.get("detail")
                or payload.get("message")
                or payload.get("error")
                or response.text
            ).strip()
        else:
            message = str(payload).strip()
        return False, message or f"zai2api 请求失败: HTTP {response.status_code}", detail

    return True, f"已导入 zai2api: {identifier or 'token'}", detail
