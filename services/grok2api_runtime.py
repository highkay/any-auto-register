from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

import requests


def _get_config(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def _admin_key_candidates(app_key: str) -> list[str]:
    raw = str(app_key or "").strip()
    candidates: list[str] = []
    for candidate in (raw, raw[3:] if raw.lower().startswith("sk-") else ""):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def verify_grok2api(api_url: str | None = None, app_key: str | None = None) -> Tuple[bool, str]:
    api_url = str(api_url or _get_config("grok2api_url", "")).strip()
    app_key = str(app_key or _get_config("grok2api_app_key", "")).strip()

    if not api_url:
        return False, "grok2api URL 未配置"
    if not app_key:
        return False, "grok2api App Key 未配置"

    try:
        errors = []
        for candidate_key in _admin_key_candidates(app_key):
            for path in ("/admin/api/verify", "/v1/admin/verify"):
                resp = requests.get(
                    f"{api_url.rstrip('/')}{path}",
                    headers={"Authorization": f"Bearer {candidate_key}"},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return True, "grok2api 鉴权正常"
                text = str(resp.text or "")[:200]
                errors.append(f"{path}: HTTP {resp.status_code} - {text}")
                if resp.status_code in {401, 403}:
                    break
                if resp.status_code not in {404, 405}:
                    break
        return False, "grok2api 鉴权失败: " + "; ".join(errors)
    except Exception as e:
        return False, f"grok2api 连接失败: {e}"


def _is_local_url(api_url: str) -> bool:
    try:
        host = (urlparse(api_url).hostname or "").lower()
    except Exception:
        return False
    return host in {"", "localhost", "127.0.0.1", "::1"}


def ensure_grok2api_ready() -> Tuple[bool, str]:
    api_url = _get_config("grok2api_url", "http://127.0.0.1:8011")
    app_key = _get_config("grok2api_app_key", "grok2api")

    ok, msg = verify_grok2api(api_url=api_url, app_key=app_key)
    if ok:
        return True, msg
    if not _is_local_url(api_url):
        return False, msg

    from services.external_apps import list_status, start, stop

    try:
        status = next((item for item in list_status() if item["name"] == "grok2api"), None)
        if status and not status.get("repo_exists"):
            return False, "grok2api 未安装，请先到“设置 → 插件”里手动安装"
        running = bool(status and status.get("running"))

        if running:
            stop("grok2api")
        start("grok2api")
    except Exception as e:
        return False, f"{msg}; 自动重启 grok2api 失败: {e}"

    return verify_grok2api(api_url=api_url, app_key=app_key)
