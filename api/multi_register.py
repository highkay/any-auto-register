"""Experimental: serial multi-platform registration on one mailbox lease.

Default off conceptually — callers must pass platforms explicitly.
No success-rate SLA; per-platform failures are recorded and skipped.
"""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from core.config_store import config_store
from core.flags import assert_platform_allowed
from core.task_runtime import RegisterTaskStore, StopTaskRequested

# Dedicated prefix — avoids clashing with api.tasks `/{task_id}` routes.
router = APIRouter(prefix="/multi-tasks", tags=["tasks-multi"])

# Separate store so experimental multi tasks don't collide with single-platform ids.
_multi_store = RegisterTaskStore(max_finished_tasks=50, cleanup_threshold=80)


class MultiRegisterRequest(BaseModel):
    platforms: list[str] = Field(default_factory=list)
    email: Optional[str] = None
    password: Optional[str] = None
    proxy: Optional[str] = None
    executor_type: str = "headed"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


def _log(task_id: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _multi_store.append_log(task_id, entry)
    print(entry)


def _run_multi(task_id: str, req: MultiRegisterRequest) -> None:
    from core.base_mailbox import create_mailbox
    from core.base_platform import RegisterConfig
    from core.db import save_account
    from core.proxy_utils import normalize_proxy_url
    from core.registry import get

    control = _multi_store.control_for(task_id)
    _multi_store.mark_running(task_id)
    platforms = [str(p).strip().lower() for p in (req.platforms or []) if str(p).strip()]
    if not platforms:
        _multi_store.finish(task_id, status="failed", success=0, skipped=0, errors=["platforms 为空"], error="platforms 为空")
        return

    cfg = config_store.get_all()
    extra = {**cfg, **(req.extra or {})}
    proxy = normalize_proxy_url(req.proxy) if req.proxy else None
    success = 0
    failed = 0
    skipped = 0
    errors: list[str] = []

    # One mailbox lease shared across platforms (best-effort).
    mailbox = create_mailbox(
        provider=extra.get("mail_provider", "luckmail"),
        extra=extra,
        proxy=proxy,
    )
    mail_acct = None
    try:
        mail_acct = mailbox.get_email()
        _log(task_id, f"共用邮箱: {mail_acct.email if mail_acct else req.email}")
    except Exception as exc:
        _log(task_id, f"取邮箱失败（将依赖请求 email）: {exc}")

    for index, platform in enumerate(platforms):
        if control.is_stop_requested():
            break
        try:
            assert_platform_allowed(platform, cfg)
        except ValueError as exc:
            failed += 1
            errors.append(f"{platform}: {exc}")
            _log(task_id, f"[{platform}] 跳过(flag): {exc}")
            continue

        attempt = control.start_attempt()
        try:
            control.checkpoint(attempt_id=attempt)
            _log(task_id, f"=== ({index + 1}/{len(platforms)}) 注册 {platform} ===")
            PlatformCls = get(platform)
            config = RegisterConfig(
                executor_type=req.executor_type,
                captcha_solver=req.captcha_solver,
                proxy=proxy,
                extra=extra,
            )
            # Fresh mailbox wrapper with same extra; email fixed when possible
            platform_mailbox = create_mailbox(
                provider=extra.get("mail_provider", "luckmail"),
                extra=extra,
                proxy=proxy,
            )
            inst = PlatformCls(config=config, mailbox=platform_mailbox)
            inst._log_fn = lambda m, p=platform: _log(task_id, f"[{p}] {m}")
            inst.bind_task_control(control)
            email = req.email or (mail_acct.email if mail_acct else None)
            account = inst.register(email=email, password=req.password)
            try:
                save_account(account)
            except Exception as exc:
                _log(task_id, f"[{platform}] 入库失败: {exc}")
            success += 1
            _log(task_id, f"[{platform}] 成功 {account.email}")
        except StopTaskRequested:
            _log(task_id, "任务停止")
            break
        except Exception as exc:
            failed += 1
            errors.append(f"{platform}: {exc}")
            _log(task_id, f"[{platform}] 失败: {exc}（继续下一平台）")
        finally:
            control.finish_attempt(attempt)
            _multi_store.set_progress(task_id, f"{index + 1}/{len(platforms)}")
            _multi_store.update_counters(task_id, success=success)

    status = "stopped" if control.is_stop_requested() else ("done" if success else "failed")
    _multi_store.finish(
        task_id,
        status=status,
        success=success,
        skipped=skipped,
        errors=errors,
        error="" if success else (errors[-1] if errors else "全部失败"),
    )
    _log(task_id, f"多平台结束 success={success} failed={failed} status={status}")


@router.post("/register")
def create_multi_register(req: MultiRegisterRequest, background_tasks: BackgroundTasks):
    platforms = [str(p).strip().lower() for p in (req.platforms or []) if str(p).strip()]
    if len(platforms) < 2:
        raise HTTPException(400, "platforms 至少需要 2 个平台（实验能力，成功率不保证）")
    cfg = config_store.get_all()
    for p in platforms:
        try:
            assert_platform_allowed(p, cfg)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    task_id = f"multi_{int(time.time() * 1000)}"
    _multi_store.create(
        task_id,
        platform="multi",
        total=len(platforms),
        source="multi_platform",
        meta={"platforms": platforms},
    )
    prepared = MultiRegisterRequest(**deepcopy(req.model_dump()))
    background_tasks.add_task(_run_multi, task_id, prepared)
    return {
        "task_id": task_id,
        "platforms": platforms,
        "source": "multi_platform",
        "warning": "实验能力：同邮箱串行多平台，成功率不设 SLA",
    }


@router.get("")
def list_multi_tasks():
    return _multi_store.list_snapshots()


@router.get("/{task_id}")
def get_multi_task(task_id: str):
    if not _multi_store.exists(task_id):
        raise HTTPException(404, "任务不存在")
    return _multi_store.snapshot(task_id)


@router.post("/{task_id}/stop")
def stop_multi_task(task_id: str):
    if not _multi_store.exists(task_id):
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "control": _multi_store.request_stop(task_id)}
