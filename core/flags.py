"""Experimental feature flags backed by config_store / CONFIG_KEYS.

Flags are plain string config values. Truthy: 1/true/yes/on/enabled (case-insensitive).
Empty or anything else is off (fail-closed for gated capabilities).
"""
from __future__ import annotations

from typing import Mapping

# Keep in sync with api/config.py CONFIG_KEYS and Settings experimental section.
FEATURE_CLAUDE_REGISTER = "feature_claude_register"
FEATURE_GITHUB_REGISTER = "feature_github_register"
FEATURE_OUTLOOK_PRODUCER = "feature_outlook_producer"
FEATURE_VISION_CAPTCHA = "feature_vision_captcha"
FEATURE_CAPSOLVER = "feature_capsolver"

FEATURE_FLAG_KEYS: tuple[str, ...] = (
    FEATURE_CLAUDE_REGISTER,
    FEATURE_GITHUB_REGISTER,
    FEATURE_OUTLOOK_PRODUCER,
    FEATURE_VISION_CAPTCHA,
    FEATURE_CAPSOLVER,
)

# Platforms gated by a feature flag (name -> flag key).
FEATURE_GATED_PLATFORMS: dict[str, str] = {
    "claude": FEATURE_CLAUDE_REGISTER,
    "github": FEATURE_GITHUB_REGISTER,
}

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})


def normalize_flag_value(value) -> str:
    """Normalize a stored flag to '1' or '0'."""
    raw = str(value or "").strip().lower()
    return "1" if raw in _TRUTHY else "0"


def is_truthy(value) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def flag_enabled(name: str, cfg: Mapping[str, object] | None = None) -> bool:
    """Return True if the named feature flag is enabled.

    When *cfg* is omitted, reads from ``config_store.get_all()``.
    """
    if cfg is None:
        from core.config_store import config_store

        cfg = config_store.get_all()
    return is_truthy(cfg.get(name, ""))


def require_flag(name: str, *, cfg: Mapping[str, object] | None = None, label: str | None = None) -> None:
    """Raise ValueError if flag is off (fail-closed)."""
    if flag_enabled(name, cfg):
        return
    display = label or name
    raise ValueError(f"实验功能未启用: {display}（请在设置中打开 {name}）")


def platform_feature_flag(platform: str) -> str | None:
    """Return the feature flag key that gates *platform*, if any."""
    key = str(platform or "").strip().lower()
    return FEATURE_GATED_PLATFORMS.get(key)


def assert_platform_allowed(platform: str, cfg: Mapping[str, object] | None = None) -> None:
    """Raise ValueError when a gated platform is requested with flag off."""
    flag = platform_feature_flag(platform)
    if not flag:
        return
    require_flag(flag, cfg=cfg, label=f"platform={platform}")
