"""gpt-load 导入辅助逻辑。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests
from sqlmodel import Session

from core.db import AccountModel, engine

SYNC_NAME = "gpt_load"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auth_headers(api_key: str) -> dict[str, str]:
    key = str(api_key or "").strip()
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "X-API-Key": key,
    }


def _unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def persist_gpt_load_sync_result(
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


def list_groups(
    *,
    api_url: str,
    api_key: str,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    resp = client.get(
        f"{api_url.rstrip('/')}/api/groups",
        headers=_auth_headers(api_key),
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = _unwrap_payload(payload)
    if not isinstance(data, list):
        raise RuntimeError(f"gpt-load groups 返回异常: {payload}")
    return [item for item in data if isinstance(item, dict)]


def resolve_group(
    *,
    api_url: str,
    api_key: str,
    group_name: str,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    target = str(group_name or "").strip()
    if not target:
        return None
    for group in list_groups(
        api_url=api_url,
        api_key=api_key,
        timeout=timeout,
        session=session,
    ):
        if str(group.get("name") or "").strip() == target:
            return group
    return None


def upload_key_to_group(
    *,
    api_url: str,
    api_key: str,
    group_id: int,
    key_value: str,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    client = session or requests.Session()
    body = {"group_id": group_id, "keys_text": str(key_value or "").strip()}
    resp = client.post(
        f"{api_url.rstrip('/')}/api/keys/add-multiple",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    payload = resp.json()
    if resp.status_code >= 400:
        raise RuntimeError(payload.get("message") or payload.get("error") or str(payload))

    data = _unwrap_payload(payload)
    detail = data if isinstance(data, dict) else {"raw": data}
    added = _safe_int(detail.get("added_count") or detail.get("addedCount"))
    ignored = _safe_int(detail.get("ignored_count") or detail.get("ignoredCount"))

    if added > 0:
        return True, f"已导入 {added} 个 key", detail
    if ignored > 0:
        return True, f"key 已存在，忽略 {ignored} 个", detail
    message = payload.get("message") if isinstance(payload, dict) else ""
    if resp.ok:
        return True, str(message or "导入成功"), detail
    return False, str(message or "导入失败"), detail


def upload_account_to_gpt_load(
    account: Any,
    *,
    api_url: str,
    api_key: str,
    group_name: str,
    provider_label: str,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    extra = _get_account_extra(account)
    provider_key = str(extra.get("api_key") or getattr(account, "token", "") or "").strip()
    if not provider_key:
        return False, f"账号缺少 {provider_label} API key", {}

    group = resolve_group(
        api_url=api_url,
        api_key=api_key,
        group_name=group_name,
        timeout=timeout,
        session=session,
    )
    if not group:
        return False, f"gpt-load 分组不存在: {group_name}", {}

    group_id = _safe_int(group.get("id"))
    if group_id <= 0:
        return False, f"gpt-load 分组 ID 无效: {group}", {"group": group}

    ok, msg, detail = upload_key_to_group(
        api_url=api_url,
        api_key=api_key,
        group_id=group_id,
        key_value=provider_key,
        timeout=timeout,
        session=session,
    )
    enriched_detail = {
        **detail,
        "group_id": group_id,
        "group_name": str(group.get("name") or "").strip(),
    }
    return ok, msg, enriched_detail


def upload_nvidia_account_to_gpt_load(
    account: Any,
    *,
    api_url: str,
    api_key: str,
    group_name: str,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    return upload_account_to_gpt_load(
        account,
        api_url=api_url,
        api_key=api_key,
        group_name=group_name,
        provider_label="NVIDIA",
        timeout=timeout,
        session=session,
    )


def upload_cerebras_account_to_gpt_load(
    account: Any,
    *,
    api_url: str,
    api_key: str,
    group_name: str,
    timeout: int = 15,
    session: requests.Session | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    return upload_account_to_gpt_load(
        account,
        api_url=api_url,
        api_key=api_key,
        group_name=group_name,
        provider_label="Cerebras",
        timeout=timeout,
        session=session,
    )
