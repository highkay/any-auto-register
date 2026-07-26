"""Unified sync browser backend.

Prefer Patchright for stealth-compatible browser automation and fall back to
Playwright only when Patchright is unavailable.
"""

from __future__ import annotations

try:
    from patchright import sync_api as _sync_api

    BACKEND_NAME = "patchright"
except Exception:
    from playwright import sync_api as _sync_api

    BACKEND_NAME = "playwright"

sync_playwright = _sync_api.sync_playwright
Page = _sync_api.Page
Locator = _sync_api.Locator
TimeoutError = _sync_api.TimeoutError

__all__ = [
    "BACKEND_NAME",
    "Locator",
    "Page",
    "TimeoutError",
    "sync_playwright",
]
