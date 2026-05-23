from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.base_phone import (
    BasePhoneService,
    FiveSimPhoneService,
    FreeSmsToolPhoneService,
    HeroSMSPhoneService,
    PhoneLease,
)

try:
    from smstome_tool import (
        PhoneEntry,
        get_unused_phone,
        mark_phone_blacklisted,
        parse_country_slugs,
        update_global_phone_list,
        wait_for_otp,
    )
except Exception as smstome_import_error:
    _SMSTOME_IMPORT_ERROR = str(smstome_import_error)

    @dataclass
    class PhoneEntry:
        country_slug: str = ""
        phone: str = ""
        detail_url: str = ""

    def parse_country_slugs(value):
        if value is None:
            return []
        if isinstance(value, str):
            raw_items = value.replace(";", ",").split(",")
        else:
            raw_items = list(value)
        normalized = []
        seen = set()
        for item in raw_items:
            slug = str(item or "").strip().lower().replace("_", "-")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            normalized.append(slug)
        return normalized

    def _raise_smstome_unavailable(*_args, **_kwargs):
        raise RuntimeError(f"smstome_tool unavailable: {_SMSTOME_IMPORT_ERROR}")

    get_unused_phone = _raise_smstome_unavailable
    update_global_phone_list = _raise_smstome_unavailable

    def mark_phone_blacklisted(*_args, **_kwargs):
        return None

    def wait_for_otp(*_args, **_kwargs):
        return None


def _to_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= minimum else default


def _prefix_hint(phone: str, width: int = 7) -> str:
    value = str(phone or "").strip()
    return value[: min(len(value), width)] if value else ""


class SMSToMePhoneService(BasePhoneService):
    provider_key = "smstome"
    provider_label = "SMSToMe"

    def __init__(self, config: Optional[dict] = None, log_fn: Optional[Callable[[str], None]] = None):
        super().__init__(config, log_fn=log_fn)
        self.cookie_header = str(self.config.get("smstome_cookie", "") or "").strip() or None
        self.country_slugs = parse_country_slugs(self.config.get("smstome_country_slugs"))
        self.global_file = Path(str(self.config.get("smstome_global_file") or "smstome_all_numbers.txt"))
        self.used_numbers_dir = Path(str(self.config.get("smstome_used_numbers_dir") or "smstome_used"))
        self.task_name = str(self.config.get("smstome_task_name") or "chatgpt_add_phone").strip() or "chatgpt_add_phone"
        self._max_attempts = _to_positive_int(
            self.config.get("smstome_phone_attempts"), 3
        )
        self.otp_timeout_seconds = _to_positive_int(self.config.get("smstome_otp_timeout_seconds"), 45, minimum=10)
        self.poll_interval_seconds = _to_positive_int(self.config.get("smstome_poll_interval_seconds"), 5, minimum=1)
        self.sync_max_pages_per_country = _to_positive_int(
            self.config.get("smstome_sync_max_pages_per_country"),
            5,
        )

    @property
    def enabled(self) -> bool:
        return self._has_pool_file() or bool(self.cookie_header)

    def _has_pool_file(self) -> bool:
        try:
            return self.global_file.exists() and self.global_file.stat().st_size > 0
        except OSError:
            return False

    def ensure_pool_ready(self) -> None:
        if self._has_pool_file():
            return
        if not self.cookie_header:
            raise RuntimeError("未找到 SMSToMe 号码池文件，且未配置 smstome_cookie")

        self.log_fn("SMSToMe 号码池不存在，开始自动同步...")
        count = update_global_phone_list(
            cookie_header=self.cookie_header,
            countries=self.country_slugs or None,
            output_path=self.global_file,
            max_pages_per_country=self.sync_max_pages_per_country,
        )
        if count <= 0:
            raise RuntimeError("SMSToMe 号码池同步后为空")
        self.log_fn(f"SMSToMe 号码池同步完成，共 {count} 个号码")

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def acquire_phone(self, *, exclude_prefixes: Optional[Iterable[str]] = None) -> Optional[PhoneLease]:
        self.ensure_pool_ready()
        entry = get_unused_phone(
            self.task_name,
            country_slug=self.country_slugs or None,
            global_file=self.global_file,
            used_numbers_dir=self.used_numbers_dir,
            exclude_prefixes=exclude_prefixes,
        )
        if not entry:
            return None
        return PhoneLease(
            phone=entry.phone,
            country_slug=entry.country_slug,
            provider=self.provider_key,
            extra={"smstome_entry": entry},
        )

    def mark_blacklisted(self, phone: str) -> None:
        mark_phone_blacklisted(self.task_name, phone, used_numbers_dir=self.used_numbers_dir)

    def wait_for_code(self, entry: PhoneLease | PhoneEntry, *, timeout: Optional[int] = None) -> Optional[str]:
        wait_seconds = _to_positive_int(timeout, self.otp_timeout_seconds, minimum=10)
        raw_entry = (
            entry.extra.get("smstome_entry")
            if isinstance(entry, PhoneLease) and isinstance(entry.extra, dict)
            else entry
        )
        return wait_for_otp(
            raw_entry,
            cookie_header=self.cookie_header,
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            trace=lambda message: self.log_fn(f"[SMSToMe] {message}"),
            raise_on_timeout=False,
        )


def resolve_phone_verification_provider(config: Optional[dict] = None) -> str:
    data = dict(config or {})
    explicit = str(data.get("phone_verification_provider", "") or "").strip().lower()
    if explicit in {"", "auto", "automatic", "default"}:
        explicit = ""
    if explicit in {"five_sim", "five-sim", "5sim"}:
        return "five_sim"
    if explicit in {"hero_sms", "hero-sms"}:
        return "hero_sms"
    if explicit in {"free_sms_tool", "free-sms-tool"}:
        return "free_sms_tool"
    if explicit == "smstome":
        return "smstome"
    if (
        str(data.get("free_sms_tool_api_key", "") or "").strip()
        and not str(data.get("five_sim_api_key", "") or "").strip()
        and not str(data.get("hero_sms_api_key", "") or "").strip()
        and not str(data.get("smstome_cookie", "") or "").strip()
    ):
        return "free_sms_tool"
    if (
        str(data.get("five_sim_api_key", "") or "").strip()
        and not str(data.get("hero_sms_api_key", "") or "").strip()
        and not str(data.get("smstome_cookie", "") or "").strip()
    ):
        return "five_sim"
    if str(data.get("hero_sms_api_key", "") or "").strip() and not str(
        data.get("smstome_cookie", "") or ""
    ).strip():
        return "hero_sms"
    return "smstome"


def create_phone_service(
    config: Optional[dict] = None,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
) -> BasePhoneService:
    provider = resolve_phone_verification_provider(config)
    if provider == "five_sim":
        return FiveSimPhoneService(config, log_fn=log_fn)
    if provider == "hero_sms":
        return HeroSMSPhoneService(config, log_fn=log_fn)
    if provider == "free_sms_tool":
        return FreeSmsToolPhoneService(config, log_fn=log_fn)
    return SMSToMePhoneService(config, log_fn=log_fn)
