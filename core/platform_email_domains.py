from __future__ import annotations

import json
from typing import Any

DEFAULT_BLOCKED_EMAIL_DOMAINS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "deepseek": (
        "apple.edu.pl",
        "imail.edu.vn",
        "bscse.okcx.edu.rs",
        "bseee.okcx.edu.rs",
        "usa.priyo.edu.pl",
        "mail.highkay.com",
        "highkay.qzz.io",
        "highlu.de",
        "20210513.xyz",
        "highkay.com",
        "edumail.edu.rs",
        "oxfor.edu.pl",
        "zikzak.site",
        "nondon.store",
        "nullsto.edu.pl",
        "io.vn",
        "nik.edu.pl",
        "mailer.edu.pl",
        "gddp2018.edu.vn",
    ),
    "grok": (
        "nik.edu.pl",
        "mailo.edu.pl",
        "oxfor.edu.pl",
    ),
}


def normalize_email_domain(value: Any) -> str:
    domain = str(value or "").strip().lower()
    if not domain:
        return ""
    if "@" in domain:
        domain = domain.rsplit("@", 1)[1]
    if domain.startswith("@"):
        domain = domain[1:]
    return domain.strip().strip(".")


def extract_email_domain(address: Any) -> str:
    return normalize_email_domain(address)


def email_domain_matches_suffix(domain: Any, blocked_suffix: Any) -> bool:
    normalized_domain = normalize_email_domain(domain)
    normalized_suffix = normalize_email_domain(blocked_suffix)
    if not normalized_domain or not normalized_suffix:
        return False
    return normalized_domain == normalized_suffix or normalized_domain.endswith(
        f".{normalized_suffix}"
    )


def is_email_domain_blocked(domain: Any, blocked_domains: Any) -> bool:
    normalized_domain = normalize_email_domain(domain)
    if not normalized_domain:
        return False
    return any(
        email_domain_matches_suffix(normalized_domain, blocked)
        for blocked in parse_email_domain_list(blocked_domains)
    )


def parse_email_domain_list(value: Any) -> list[str]:
    if not value:
        return []

    items: list[Any]
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            items = parsed
        else:
            items = [part for chunk in text.splitlines() for part in chunk.split(",")]
    else:
        items = [value]

    domains: list[str] = []
    seen: set[str] = set()
    for item in items:
        domain = normalize_email_domain(item)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


def platform_blocked_email_domains_key(platform: Any) -> str:
    normalized = str(platform or "").strip().lower()
    if not normalized:
        return ""
    return f"{normalized}_blocked_email_domains"


def resolve_platform_blocked_email_domains(
    platform: Any,
    config: dict[str, Any] | None = None,
) -> list[str]:
    normalized_platform = str(platform or "").strip().lower()
    defaults = list(DEFAULT_BLOCKED_EMAIL_DOMAINS_BY_PLATFORM.get(normalized_platform, ()))
    if not normalized_platform:
        return defaults

    raw_config = config if isinstance(config, dict) else {}
    configured = parse_email_domain_list(
        raw_config.get(platform_blocked_email_domains_key(normalized_platform), "")
    )

    merged: list[str] = []
    seen: set[str] = set()
    for domain in defaults + configured:
        normalized = normalize_email_domain(domain)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged
