"""Turnstile Solver 进程管理 - 后端启动时自动拉起"""
import subprocess
import sys
import os
import time
import threading
from pathlib import Path

import requests

_proc: subprocess.Popen = None
_log_file = None
_lock = threading.Lock()
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _read_env_file_value(key: str) -> str | None:
    """Read a single key from project .env without requiring dotenv."""
    if not _ENV_FILE.exists():
        return None
    try:
        for raw in _ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() != key:
                continue
            text = value.strip()
            if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
                text = text[1:-1]
            return text
    except Exception:
        return None
    return None


def _env_get(key: str, default: str = "") -> str:
    """Prefer process env, then project .env, then default."""
    raw = os.getenv(key)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    file_val = _read_env_file_value(key)
    if file_val is not None and str(file_val).strip() != "":
        return str(file_val).strip()
    return default


def _solver_enabled() -> bool:
    # APP_ENABLE_SOLVER=0 / false / no 可屏蔽本地 Turnstile Solver 自动启动
    return _env_get("APP_ENABLE_SOLVER", "1").lower() not in {"0", "false", "no"}


def _solver_port() -> int:
    return int(_env_get("SOLVER_PORT", "8889"))


def _solver_url() -> str:
    return (_env_get("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{_solver_port()}").rstrip("/")


def _solver_bind_host() -> str:
    return _env_get("SOLVER_BIND_HOST", "0.0.0.0")


def _solver_browser_type() -> str:
    return _env_get("SOLVER_BROWSER_TYPE", "camoufox")


def is_running() -> bool:
    try:
        r = requests.get(f"{_solver_url()}/", timeout=2)
        return r.status_code < 500
    except Exception:
        return False


def start():
    global _proc, _log_file
    with _lock:
        if not _solver_enabled():
            print("[Solver] 已禁用，跳过自动启动")
            return
        if is_running():
            print("[Solver] 已在运行")
            return
        solver_script = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "start.py"
        )
        log_path = os.path.join(
            os.path.dirname(__file__), "turnstile_solver", "solver.log"
        )
        _log_file = open(log_path, "a", encoding="utf-8")
        _proc = subprocess.Popen(
            [
                sys.executable,
                "-u",
                solver_script,
                "--browser_type",
                _solver_browser_type(),
                "--host",
                _solver_bind_host(),
                "--port",
                str(_solver_port()),
            ],
            stdout=_log_file,
            stderr=subprocess.STDOUT,
        )
        # 等待服务就绪（最多30s）
        for _ in range(30):
            time.sleep(1)
            if is_running():
                print(f"[Solver] 已启动 PID={_proc.pid}")
                return
            if _proc.poll() is not None:
                print(f"[Solver] 启动失败，退出码={_proc.returncode}，日志: {log_path}")
                _proc = None
                if _log_file:
                    _log_file.close()
                    _log_file = None
                return
        print(f"[Solver] 启动超时，日志: {log_path}")


def stop():
    global _proc, _log_file
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            _proc.wait(timeout=5)
            print("[Solver] 已停止")
        _proc = None
        if _log_file:
            _log_file.close()
            _log_file = None


def start_async():
    """在后台线程启动，不阻塞主进程"""
    t = threading.Thread(target=start, daemon=True)
    t.start()
