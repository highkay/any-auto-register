"""Browser runtime helpers for headless/headed resolution and Chrome path."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

# Prefer the portable Chrome under F:\chrome for all local launches.
DEFAULT_CHROME_EXECUTABLE = Path(r"F:\chrome\chrome.exe")
_CHROME_ENV_NAMES = ("CHROME_EXECUTABLE", "CHROME_PATH", "PLAYWRIGHT_CHROME_PATH")


def parse_env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None

    value = str(raw).strip().lower()
    if not value:
        return None
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False

    logger.warning("忽略无效布尔环境变量 %s=%r", name, raw)
    return None


def resolve_browser_headless(
    requested_headless: bool | None,
    *,
    default_headless: bool = True,
    override_env_names: Iterable[str] = ("PLAYWRIGHT_HEADLESS", "REGISTER_HEADLESS"),
) -> tuple[bool, str]:
    for env_name in override_env_names:
        override = parse_env_bool(env_name)
        if override is not None:
            return override, f"env:{env_name}={str(override).lower()}"

    if requested_headless is not None:
        return bool(
            requested_headless
        ), f"requested:{str(bool(requested_headless)).lower()}"

    return bool(default_headless), f"default:{str(bool(default_headless)).lower()}"


def ensure_browser_display_available(headless: bool) -> None:
    if headless:
        return
    if not sys.platform.startswith("linux"):
        return
    if os.getenv("DISPLAY"):
        return

    raise RuntimeError(
        "当前为 Linux 有头浏览器模式，但未检测到 DISPLAY。"
        "Docker 内请启用 Xvfb；本地 Linux 请先启动图形环境或改用无头模式。"
    )


def _normalize_chrome_candidate(raw: str | Path | None) -> Path | None:
    if raw is None:
        return None
    text = str(raw).strip().strip('"').strip("'")
    if not text:
        return None
    path = Path(text).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    return path


def get_chrome_executable() -> str | None:
    """Resolve the preferred Chrome binary.

    Priority:
    1. CHROME_EXECUTABLE / CHROME_PATH / PLAYWRIGHT_CHROME_PATH
    2. F:\\chrome\\chrome.exe
    """
    candidates: list[Path] = []
    for env_name in _CHROME_ENV_NAMES:
        path = _normalize_chrome_candidate(os.getenv(env_name))
        if path is not None:
            candidates.append(path)
    candidates.append(DEFAULT_CHROME_EXECUTABLE)

    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return str(path)
    return None


def with_chrome_executable(
    launch_kwargs: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Inject F:\\chrome executable_path into Playwright launch kwargs.

    When a local Chrome binary is available, prefer it over channel=chrome /
    bundled Chromium. Leaves channel=msedge (and other non-chrome channels)
    alone so callers can still fall back to Edge intentionally.
    """
    opts: dict[str, Any] = dict(launch_kwargs or {})
    opts.update(extra)
    # Drop explicit None channel so callers can pass channel=None safely.
    if opts.get("channel") is None:
        opts.pop("channel", None)

    chrome = get_chrome_executable()
    if not chrome:
        return opts

    channel = opts.get("channel")
    channel_text = str(channel or "").strip().lower()
    if channel_text and channel_text not in {
        "chrome",
        "chromium",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
    }:
        return opts

    opts.pop("channel", None)
    opts["executable_path"] = chrome
    return opts
