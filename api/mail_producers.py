"""Mail producer tasks (Outlook self-registration → outlook_accounts)."""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from core.config_store import config_store
from core.flags import FEATURE_OUTLOOK_PRODUCER, require_flag
from core.task_runtime import RegisterTaskStore, SkipCurrentAttemptRequested, StopTaskRequested

router = APIRouter(prefix="/mail-producers", tags=["mail-producers"])

_producer_store = RegisterTaskStore(max_finished_tasks=100, cleanup_threshold=120)


class OutlookProducerRequest(BaseModel):
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    executor_type: str = "headed"  # headed | headless
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


def _log(task_id: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _producer_store.append_log(task_id, entry)
    print(entry)


def _run_outlook_producer(task_id: str, req: OutlookProducerRequest) -> None:
    from core.base_platform import BasePlatform, RegisterConfig
    from core.executors.playwright import PlaywrightExecutor
    from core.proxy_utils import normalize_proxy_url
    from services.outlook_registration import produce_outlook_account

    control = _producer_store.control_for(task_id)
    _producer_store.mark_running(task_id)
    cfg_all = config_store.get_all()
    extra = {**cfg_all, **(req.extra or {})}
    success = 0
    failed = 0
    skipped = 0
    errors: list[str] = []
    count = max(1, int(req.count or 1))
    concurrency = max(1, min(int(req.concurrency or 1), count))
    lock = threading.Lock()

    def _one(i: int) -> None:
        nonlocal success, failed, skipped
        attempt = None
        try:
            control.checkpoint()
            attempt = control.start_attempt()
            control.checkpoint(attempt_id=attempt)
            _log(task_id, f"开始生产 Outlook 邮箱 {i + 1}/{count}")
            proxy = normalize_proxy_url(req.proxy) if req.proxy else None
            captcha = None
            try:
                class _Shim(BasePlatform):
                    name = "outlook_producer"
                    display_name = "Outlook Producer"
                    supported_executors = ["headed", "headless"]

                    def register(self, email=None, password=None):
                        raise NotImplementedError

                    def check_valid(self, account):
                        return False

                shim = _Shim(
                    RegisterConfig(
                        executor_type=req.executor_type,
                        captcha_solver=req.captcha_solver,
                        proxy=proxy,
                        extra=extra,
                    )
                )
                captcha = shim._make_captcha()
            except Exception as exc:
                _log(task_id, f"captcha 初始化跳过: {exc}")

            headless = str(req.executor_type).lower() != "headed"
            with PlaywrightExecutor(proxy=proxy, headless=headless) as ex:
                result = produce_outlook_account(
                    ex.page,
                    captcha=captcha,
                    log_fn=lambda m: _log(task_id, m),
                    control=control,
                    px_mode=str(extra.get("outlook_px_mode") or "auto"),
                    px_app_id=str(extra.get("outlook_px_app_id") or "PXzC5j78di"),
                    extract_graph_token=str(extra.get("outlook_extract_graph_token") or "").lower()
                    in {"1", "true", "yes", "on"},
                    require_graph_token=str(extra.get("outlook_require_graph_token") or "").lower()
                    in {"1", "true", "yes", "on"},
                    persist=True,
                )
            if result.ok:
                with lock:
                    success += 1
                _log(task_id, f"成功: {result.email}")
            else:
                with lock:
                    failed += 1
                    errors.append(result.error or result.email or "failed")
                _log(task_id, f"失败: {result.error or result.email}")
        except StopTaskRequested:
            _log(task_id, "任务停止")
            raise
        except SkipCurrentAttemptRequested:
            with lock:
                skipped += 1
            _log(task_id, f"跳过第 {i + 1} 个")
        except Exception as exc:
            with lock:
                failed += 1
                errors.append(str(exc))
            _log(task_id, f"异常: {exc}")
        finally:
            control.finish_attempt(attempt)
            _producer_store.set_progress(task_id, f"{success + failed + skipped}/{count}")

    try:
        if concurrency <= 1:
            for i in range(count):
                if control.is_stop_requested():
                    _log(task_id, "收到停止请求")
                    break
                _one(i)
        else:
            threads: list[threading.Thread] = []
            for i in range(count):
                if control.is_stop_requested():
                    break
                t = threading.Thread(target=_one, args=(i,), daemon=True)
                threads.append(t)
                t.start()
                while sum(1 for x in threads if x.is_alive()) >= concurrency:
                    time.sleep(0.2)
            for t in threads:
                t.join()
        status = "stopped" if control.is_stop_requested() else ("done" if success else "failed")
    except StopTaskRequested:
        status = "stopped"

    _producer_store.finish(
        task_id,
        status=status,
        success=success,
        skipped=skipped,
        errors=errors,
        error="" if success else (errors[-1] if errors else ""),
    )
    _log(task_id, f"结束 success={success} failed={failed} skipped={skipped} status={status}")


@router.post("/outlook")
def start_outlook_producer(req: OutlookProducerRequest, background_tasks: BackgroundTasks):
    cfg = config_store.get_all()
    try:
        require_flag(FEATURE_OUTLOOK_PRODUCER, cfg=cfg)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    task_id = f"mailprod_{int(time.time() * 1000)}"
    _producer_store.create(
        task_id,
        platform="outlook",
        total=max(1, int(req.count or 1)),
        source="mail_producer",
        meta={
            "count": req.count,
            "concurrency": req.concurrency,
            "executor_type": req.executor_type,
        },
    )
    prepared = OutlookProducerRequest(**deepcopy(req.model_dump()))
    background_tasks.add_task(_run_outlook_producer, task_id, prepared)
    return {"task_id": task_id, "platform": "outlook", "source": "mail_producer"}


@router.get("/tasks")
def list_producer_tasks():
    return _producer_store.list_snapshots()


@router.get("/tasks/{task_id}")
def get_producer_task(task_id: str):
    if not _producer_store.exists(task_id):
        raise HTTPException(404, "任务不存在")
    return _producer_store.snapshot(task_id)


@router.post("/tasks/{task_id}/stop")
def stop_producer_task(task_id: str):
    if not _producer_store.exists(task_id):
        raise HTTPException(404, "任务不存在")
    control = _producer_store.request_stop(task_id)
    _log(task_id, "收到停止请求")
    return {"ok": True, "control": control}


@router.get("/tasks/{task_id}/logs")
def get_producer_task_logs(task_id: str):
    if not _producer_store.exists(task_id):
        raise HTTPException(404, "任务不存在")
    snap = _producer_store.snapshot(task_id)
    return {
        "task_id": task_id,
        "logs": snap.get("logs") or [],
        "status": snap.get("status"),
        "control": snap.get("control") or {},
    }
