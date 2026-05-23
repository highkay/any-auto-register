from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

from .proxy_utils import build_mailbox_proxy_config, create_mailbox_requests_session


def _to_positive_int(value, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return default
    return parsed if parsed >= minimum else default


def _to_optional_float(value: Any) -> Optional[float]:
    try:
        raw = str(value or "").strip()
        if not raw:
            return None
        return float(raw)
    except Exception:
        return None


def _slugify_label(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    return text.strip("-")


def _normalize_lookup_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _normalize_phone_exception_prefixes(
    prefixes: Optional[Iterable[str]],
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in prefixes or []:
        digits = re.sub(r"\D+", "", str(item or ""))
        if len(digits) < 4 or len(digits) > 7 or digits in seen:
            continue
        seen.add(digits)
        normalized.append(digits)
    return normalized


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class PhoneLease:
    phone: str
    activation_id: str = ""
    country_id: Optional[int] = None
    country_name: str = ""
    country_slug: str = ""
    provider: str = ""
    service_code: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class BasePhoneService(ABC):
    provider_key: str = ""
    provider_label: str = ""

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.config = dict(config or {})
        self.log_fn = log_fn or (lambda _msg: None)

    def _log(self, message: str) -> None:
        self.log_fn(message)

    @staticmethod
    def prefix_hint(phone: str, width: int = 7) -> str:
        value = str(phone or "").strip()
        return value[: min(len(value), width)] if value else ""

    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint(
            consume_skip=consume_skip,
            attempt_id=getattr(self, "_task_attempt_token", None),
        )

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _run_polling_wait(
        self,
        *,
        timeout: int,
        poll_interval: float,
        poll_once: Callable[[], Optional[str]],
        timeout_message: str | None = None,
        raise_on_timeout: bool = True,
    ) -> Optional[str]:
        timeout_seconds = max(int(timeout or 0), 1)
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            self._checkpoint()
            code = poll_once()
            if code:
                return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep_with_checkpoint(min(float(poll_interval), remaining))

        self._checkpoint()
        if raise_on_timeout:
            raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")
        return None

    def _message_id_value(self, message: Any) -> str:
        import hashlib

        if isinstance(message, dict):
            for key in ("id", "message_id", "uid", "mail_id", "_id"):
                value = str(message.get(key) or "").strip()
                if value:
                    return value
            raw = json.dumps(message, ensure_ascii=False, sort_keys=True)
        else:
            raw = str(message or "")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        text = str(text or "")
        if not text:
            return None

        patterns = []
        if pattern:
            patterns.append(pattern)

        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{3}(?:[-\s]?\d{3,5}))",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{3}(?:[-\s]?\d{3,5}))",
                r"(?<!#)(?<!\d)(\d{3}(?:[-\s]?\d{3,5}))(?!\d)",
            ]
        )

        for regex in patterns:
            match = re.search(regex, text)
            if match:
                return match.group(1) if match.groups() else match.group(0)
        return None

    @property
    @abstractmethod
    def enabled(self) -> bool:
        ...

    @property
    def max_attempts(self) -> int:
        return 1

    @abstractmethod
    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneLease]:
        ...

    @abstractmethod
    def wait_for_code(
        self, entry: PhoneLease, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        ...

    def mark_blacklisted(self, phone: str) -> None:
        _ = phone

    def report_code_requested(self, entry: PhoneLease) -> None:
        _ = entry

    def finish_activation(self, entry: PhoneLease) -> None:
        _ = entry

    def cancel_activation(self, entry: PhoneLease) -> None:
        _ = entry


class HeroSMSPhoneService(BasePhoneService):
    provider_key = "hero_sms"
    provider_label = "HeroSMS"
    REST_BASE = "https://hero-sms.com/api/v1"
    STUB_BASE = "https://hero-sms.com/stubs/handler_api.php"

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(config, log_fn=log_fn)
        self.api_key = str(self.config.get("hero_sms_api_key", "") or "").strip()
        self.requested_service = str(
            self.config.get("hero_sms_service", "Kimi") or "Kimi"
        ).strip()
        self.requested_country = str(self.config.get("hero_sms_country", "") or "").strip()
        self.operator = str(self.config.get("hero_sms_operator", "") or "").strip()
        self.configured_max_price = _to_optional_float(
            self.config.get("hero_sms_max_price")
        )
        self._max_attempts = _to_positive_int(
            self.config.get("hero_sms_phone_attempts"), 3
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("hero_sms_otp_timeout_seconds"), 120, minimum=10
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("hero_sms_poll_interval_seconds"), 5, minimum=1
        )
        self._requests_session = create_mailbox_requests_session(
            build_mailbox_proxy_config(None)
        )
        self._service_catalog: list[dict[str, Any]] | None = None
        self._country_catalog: list[dict[str, Any]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _rest_headers(self) -> dict[str, str]:
        return {"Authorization": f"ApiKey {self.api_key}"}

    def _parse_http_response(self, response) -> Any:
        text = str(response.text or "").strip()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "json" in content_type or text.startswith("{") or text.startswith("["):
            try:
                return response.json()
            except Exception:
                pass
        return text

    def _error_summary(self, payload: Any) -> str:
        if isinstance(payload, dict):
            title = str(payload.get("title") or payload.get("status") or "").strip()
            details = str(
                payload.get("details")
                or payload.get("message")
                or payload.get("msg")
                or ""
            ).strip()
            info = payload.get("info")
            parts = [part for part in (title, details) if part]
            summary = ": ".join(parts) if parts else json.dumps(payload, ensure_ascii=False)
            if info:
                return f"{summary} | info={json.dumps(info, ensure_ascii=False)}"
            return summary
        return str(payload or "").strip()

    def _rest_get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> dict:
        response = self._requests_session.get(
            f"{self.REST_BASE}{path}",
            params=params,
            headers=self._rest_headers(),
            timeout=20,
        )
        payload = self._parse_http_response(response)
        if response.status_code >= 400:
            raise RuntimeError(self._error_summary(payload))
        if not isinstance(payload, dict):
            raise RuntimeError(f"HeroSMS REST 返回了非 JSON 响应: {payload}")
        return payload

    def _stub_request(self, params: dict[str, Any]) -> Any:
        query = {"api_key": self.api_key, **params}
        response = self._requests_session.get(
            self.STUB_BASE,
            params=query,
            timeout=20,
        )
        payload = self._parse_http_response(response)
        if response.status_code >= 400:
            raise RuntimeError(self._error_summary(payload))
        return payload

    def _load_service_catalog(self) -> list[dict[str, Any]]:
        if self._service_catalog is None:
            payload = self._stub_request({"action": "getServicesList", "lang": "en"})
            services = payload.get("services") if isinstance(payload, dict) else None
            if not isinstance(services, list):
                raise RuntimeError(
                    f"HeroSMS 服务列表返回异常: {self._error_summary(payload)}"
                )
            self._service_catalog = services
        return self._service_catalog

    def _load_country_catalog(self) -> list[dict[str, Any]]:
        if self._country_catalog is None:
            payload = self._stub_request({"action": "getCountries"})
            if not isinstance(payload, list):
                raise RuntimeError(
                    f"HeroSMS 国家列表返回异常: {self._error_summary(payload)}"
                )
            self._country_catalog = payload
        return self._country_catalog

    def _resolve_service_code(self) -> str:
        requested = str(self.requested_service or "").strip()
        if not requested:
            raise RuntimeError("HeroSMS 未配置 service")

        requested_lower = requested.lower()
        services = self._load_service_catalog()

        exact_code = next(
            (
                item
                for item in services
                if str(item.get("code") or "").strip().lower() == requested_lower
            ),
            None,
        )
        if exact_code:
            return str(exact_code.get("code") or "").strip()

        exact_name = next(
            (
                item
                for item in services
                if str(item.get("name") or "").strip().lower() == requested_lower
            ),
            None,
        )
        if exact_name:
            code = str(exact_name.get("code") or "").strip()
            self._log(
                f"[HeroSMS] 服务名 {requested} 解析为 {code} ({exact_name.get('name')})"
            )
            return code

        partial_match = next(
            (
                item
                for item in services
                if requested_lower in str(item.get("name") or "").strip().lower()
            ),
            None,
        )
        if partial_match:
            code = str(partial_match.get("code") or "").strip()
            self._log(
                f"[HeroSMS] 服务名 {requested} 模糊解析为 {code} ({partial_match.get('name')})"
            )
            return code

        raise RuntimeError(f"HeroSMS 未找到服务 {requested}")

    def _resolve_country_name(self, country_id: int) -> str:
        for item in self._load_country_catalog():
            try:
                if int(item.get("id")) == int(country_id):
                    return str(item.get("eng") or item.get("chn") or item.get("rus") or "")
            except Exception:
                continue
        return ""

    def _resolve_requested_country_id(self) -> Optional[int]:
        raw = str(self.requested_country or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)

        target = raw.lower()
        for item in self._load_country_catalog():
            names = {
                str(item.get("eng") or "").strip().lower(),
                str(item.get("chn") or "").strip().lower(),
                str(item.get("rus") or "").strip().lower(),
            }
            if target in names:
                return int(item.get("id"))
        raise RuntimeError(f"HeroSMS 未找到国家 {raw}")

    def _build_offer_candidates(self, service_code: str) -> list[dict[str, Any]]:
        payload = self._rest_get_json("/activations/offers", {"services": service_code})
        service_offers = ((payload.get("data") or {}) if isinstance(payload, dict) else {}).get(
            service_code
        )
        if not isinstance(service_offers, dict) or not service_offers:
            raise RuntimeError(f"HeroSMS 未找到 {service_code} 的报价")

        requested_country_id = self._resolve_requested_country_id()
        candidates = []
        for country_id_raw, offer in service_offers.items():
            try:
                country_id = int(country_id_raw)
            except Exception:
                continue
            if requested_country_id is not None and country_id != requested_country_id:
                continue
            prices = offer.get("prices") if isinstance(offer, dict) else {}
            counts = offer.get("counts") if isinstance(offer, dict) else {}
            price = _to_optional_float((prices or {}).get("default"))
            min_price = _to_optional_float((prices or {}).get("min"))
            selected_price = price if price is not None else min_price
            total = int((counts or {}).get("total") or 0)
            default_count = int((counts or {}).get("defaultPrice") or 0)
            physical = int((counts or {}).get("physical") or 0)
            if total <= 0 or selected_price is None:
                continue
            if (
                self.configured_max_price is not None
                and selected_price > self.configured_max_price
            ):
                continue

            country_name = self._resolve_country_name(country_id)
            candidates.append(
                {
                    "country_id": country_id,
                    "country_name": country_name,
                    "country_slug": _slugify_label(country_name),
                    "price": selected_price,
                    "min_price": min_price,
                    "total": total,
                    "physical": physical,
                    "default_count": default_count,
                }
            )

        if not candidates:
            if requested_country_id is not None:
                raise RuntimeError(
                    f"HeroSMS 服务 {service_code} 在指定国家 {requested_country_id} 没有可售号码"
                )
            raise RuntimeError(f"HeroSMS 服务 {service_code} 没有可售号码")

        candidates.sort(
            key=lambda item: (
                float(item["price"]),
                0 if int(item["default_count"]) > 0 else 1,
                -int(item["default_count"]),
                -int(item["physical"]),
                -int(item["total"]),
                int(item["country_id"]),
            )
        )
        return candidates

    def _looks_like_no_numbers(self, payload: Any) -> bool:
        summary = self._error_summary(payload).upper()
        return "NO_NUMBERS" in summary or "NUMBERS GONE" in summary

    def _looks_like_wrong_max_price(self, payload: Any) -> bool:
        summary = self._error_summary(payload).upper()
        return "WRONG_MAX_PRICE" in summary

    def _parse_number_payload(
        self, payload: Any, candidate: dict[str, Any], service_code: str
    ) -> PhoneLease:
        if isinstance(payload, dict):
            activation_id = str(
                payload.get("activationId") or payload.get("id") or ""
            ).strip()
            phone = str(
                payload.get("phoneNumber") or payload.get("phone") or ""
            ).strip()
            if activation_id and phone:
                return PhoneLease(
                    phone=phone,
                    activation_id=activation_id,
                    country_id=int(candidate["country_id"]),
                    country_name=str(candidate["country_name"] or ""),
                    country_slug=str(candidate["country_slug"] or ""),
                    provider=self.provider_key,
                    service_code=service_code,
                    extra=dict(payload),
                )

        text = str(payload or "").strip()
        if text.startswith("ACCESS_NUMBER:"):
            _, activation_id, phone = text.split(":", 2)
            return PhoneLease(
                phone=phone,
                activation_id=activation_id,
                country_id=int(candidate["country_id"]),
                country_name=str(candidate["country_name"] or ""),
                country_slug=str(candidate["country_slug"] or ""),
                provider=self.provider_key,
                service_code=service_code,
                extra={"raw": text},
            )
        raise RuntimeError(self._error_summary(payload) or "HeroSMS 取号失败")

    def _request_number(
        self,
        service_code: str,
        candidate: dict[str, Any],
        *,
        exclude_prefixes: Optional[Iterable[str]] = None,
        pin_price: bool = True,
    ) -> PhoneLease:
        query: dict[str, Any] = {
            "action": "getNumberV2",
            "service": service_code,
            "country": int(candidate["country_id"]),
        }
        if self.operator:
            query["operator"] = self.operator
        prefixes = _normalize_phone_exception_prefixes(exclude_prefixes)
        if prefixes:
            query["phoneException"] = ",".join(prefixes[:20])
        if pin_price:
            max_price = (
                self.configured_max_price
                if self.configured_max_price is not None
                else float(candidate["price"])
            )
            query["maxPrice"] = max_price
            query["fixedPrice"] = "true"

        payload = self._stub_request(query)
        return self._parse_number_payload(payload, candidate, service_code)

    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneLease]:
        if not self.enabled:
            raise RuntimeError("HeroSMS 未配置 API Key")

        service_code = self._resolve_service_code()
        candidates = self._build_offer_candidates(service_code)
        last_error = ""

        for candidate in candidates:
            country_name = candidate["country_name"] or str(candidate["country_id"])
            price = float(candidate["price"])
            self._log(
                f"[HeroSMS] 尝试取号: service={service_code} country={country_name}"
                f"({candidate['country_id']}) price={price:.4f} total={candidate['total']}"
            )
            try:
                lease = self._request_number(
                    service_code,
                    candidate,
                    exclude_prefixes=exclude_prefixes,
                    pin_price=True,
                )
                self._log(
                    f"[HeroSMS] 取号成功: {lease.phone} activation_id={lease.activation_id}"
                )
                try:
                    self._mark_ready(lease.activation_id, stage="取号后标记号码就绪")
                except Exception as ready_error:
                    self._log(f"[HeroSMS] 取号后标记号码就绪失败: {ready_error}")
                return lease
            except Exception as error:
                last_error = str(error)
                if self._looks_like_wrong_max_price(last_error):
                    self._log(
                        f"[HeroSMS] 国家 {country_name} 报价已波动，取消固定价格后重试一次"
                    )
                    try:
                        lease = self._request_number(
                            service_code,
                            candidate,
                            exclude_prefixes=exclude_prefixes,
                            pin_price=False,
                        )
                        self._log(
                            f"[HeroSMS] 取号成功: {lease.phone} activation_id={lease.activation_id}"
                        )
                        try:
                            self._mark_ready(lease.activation_id, stage="取号后标记号码就绪")
                        except Exception as ready_error:
                            self._log(f"[HeroSMS] 取号后标记号码就绪失败: {ready_error}")
                        return lease
                    except Exception as retry_error:
                        last_error = str(retry_error)
                if self._looks_like_no_numbers(last_error):
                    self._log(
                        f"[HeroSMS] 国家 {country_name} 当前无可用号码，继续尝试下一国家"
                    )
                    continue
                raise RuntimeError(last_error) from error

        raise RuntimeError(last_error or "HeroSMS 没有可用号码")

    def _get_status_payload(self, activation_id: str) -> Any:
        return self._stub_request({"action": "getStatus", "id": activation_id})

    def _get_status_v2_payload(self, activation_id: str) -> Any:
        return self._stub_request({"action": "getStatusV2", "id": activation_id})

    def _get_all_sms_payload(self, activation_id: str) -> Any:
        return self._stub_request({"action": "getAllSms", "id": activation_id})

    def _extract_code_from_status(self, payload: Any) -> Optional[str]:
        text = str(payload or "").strip()
        if text.startswith("STATUS_OK:") or text.startswith("STATUS_WAIT_RETRY:"):
            return text.split(":", 1)[1].strip()
        return None

    def _extract_code_from_status_v2(self, payload: Any) -> Optional[str]:
        if isinstance(payload, dict):
            sms_payload = payload.get("sms")
            if isinstance(sms_payload, dict):
                code = str(sms_payload.get("code") or "").strip()
                if code:
                    return code
            for key in ("code", "smsCode", "sms_code", "otp", "otp_code"):
                code = str(payload.get(key) or "").strip()
                if code:
                    return code
        return self._extract_code_from_status(payload)

    def _extract_code_from_sms_payload(self, payload: Any) -> Optional[str]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None
        for item in reversed(data):
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                return code
            text = str(item.get("text") or "").strip()
            extracted = self._safe_extract(text)
            if extracted:
                return extracted
        return None

    def _status_text(self, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("status", "state", "title", "details", "message"):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value.upper()
            return json.dumps(payload, ensure_ascii=False).upper()
        return str(payload or "").strip().upper()

    def wait_for_code(
        self, entry: PhoneLease, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        wait_seconds = _to_positive_int(
            timeout, self.otp_timeout_seconds, minimum=10
        )
        error_state: dict[str, str | None] = {"last": None}

        def poll_once() -> Optional[str]:
            try:
                status_v2_payload = self._get_status_v2_payload(entry.activation_id)
                code = self._extract_code_from_status_v2(status_v2_payload)
                if code:
                    self._log(f"[HeroSMS] 收到验证码: {code}")
                    return code

                status_v2_text = self._status_text(status_v2_payload)
                if status_v2_text in {"STATUS_CANCEL", "NO_ACTIVATION"}:
                    self._log(f"[HeroSMS] 激活已终止: {status_v2_payload}")
                    return None

                status_payload = self._get_status_payload(entry.activation_id)
                code = self._extract_code_from_status(status_payload)
                if code:
                    self._log(f"[HeroSMS] 收到验证码: {code}")
                    return code

                status_text = str(status_payload or "").strip().upper()
                if status_text in {"STATUS_CANCEL", "NO_ACTIVATION"}:
                    self._log(f"[HeroSMS] 激活已终止: {status_payload}")
                    return None

                sms_payload = self._get_all_sms_payload(entry.activation_id)
                code = self._extract_code_from_sms_payload(sms_payload)
                if code:
                    self._log(f"[HeroSMS] 收到验证码: {code}")
                    return code
            except Exception as error:
                summary = str(error or "").strip() or type(error).__name__
                if error_state.get("last") != summary:
                    error_state["last"] = summary
                    self._log(f"[HeroSMS] 拉取短信失败: {summary}")
            return None

        return self._run_polling_wait(
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            poll_once=poll_once,
            timeout_message=f"HeroSMS 等待验证码超时 ({wait_seconds}s)",
            raise_on_timeout=False,
        )

    def _set_status(self, activation_id: str, status: int) -> Any:
        return self._stub_request(
            {"action": "setStatus", "id": activation_id, "status": status}
        )

    def _fire_lifecycle_action(self, action: str, activation_id: str) -> Any:
        return self._stub_request({"action": action, "id": activation_id})

    def _mark_ready(self, activation_id: str, *, stage: str) -> None:
        payload = self._set_status(activation_id, 1)
        summary = self._error_summary(payload) or str(payload or "").strip() or "ok"
        self._log(f"[HeroSMS] {stage}: {summary}")

    def report_code_requested(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        self._mark_ready(entry.activation_id, stage="发码后标记号码就绪")

    def finish_activation(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        try:
            payload = self._set_status(entry.activation_id, 6)
            self._log(f"[HeroSMS] 完成激活: {self._error_summary(payload)}")
        except Exception:
            try:
                self._fire_lifecycle_action("finishActivation", entry.activation_id)
                self._log("[HeroSMS] 完成激活: finishActivation")
            except Exception as error:
                self._log(f"[HeroSMS] 完成激活失败: {error}")

    def cancel_activation(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        try:
            payload = self._set_status(entry.activation_id, 8)
            self._log(f"[HeroSMS] 取消激活: {self._error_summary(payload)}")
            return
        except Exception:
            pass
        try:
            self._fire_lifecycle_action("cancelActivation", entry.activation_id)
            self._log("[HeroSMS] 取消激活: cancelActivation")
        except Exception as error:
            self._log(f"[HeroSMS] 取消激活失败: {error}")


class FreeSmsToolPhoneService(BasePhoneService):
    provider_key = "free_sms_tool"
    provider_label = "Free SMS Tool"
    DEFAULT_BASE_URL = "http://127.0.0.1:18000"

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(config, log_fn=log_fn)
        self.base_url = str(
            self.config.get("free_sms_tool_base_url", self.DEFAULT_BASE_URL)
            or self.DEFAULT_BASE_URL
        ).strip().rstrip("/")
        self.api_key = str(self.config.get("free_sms_tool_api_key", "") or "").strip()
        self.app_slug = str(
            self.config.get("free_sms_tool_app_slug", "chatgpt") or "chatgpt"
        ).strip()
        self.app_name = str(
            self.config.get("free_sms_tool_app_name", "ChatGPT") or "ChatGPT"
        ).strip()
        self.country_name = str(self.config.get("free_sms_tool_country_name", "") or "").strip()
        self.provider_id = str(self.config.get("free_sms_tool_provider_id", "") or "").strip()
        self.claim_ttl_minutes = _to_positive_int(
            self.config.get("free_sms_tool_claim_ttl_minutes"), 10
        )
        self.include_cooling = _to_bool(
            self.config.get("free_sms_tool_include_cooling"), False
        )
        self._max_attempts = _to_positive_int(
            self.config.get("free_sms_tool_phone_attempts"), 3
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("free_sms_tool_otp_timeout_seconds"), 120, minimum=10
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("free_sms_tool_poll_interval_seconds"), 5, minimum=1
        )
        self._requests_session = create_mailbox_requests_session(
            build_mailbox_proxy_config(None)
        )
        self._leases_by_phone: dict[str, dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _parse_http_response(self, response) -> Any:
        text = str(response.text or "").strip()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "json" in content_type or text.startswith("{") or text.startswith("["):
            try:
                return response.json()
            except Exception:
                pass
        return text

    def _error_summary(self, payload: Any) -> str:
        if isinstance(payload, dict):
            detail = str(
                payload.get("detail")
                or payload.get("message")
                or payload.get("error")
                or payload.get("status")
                or ""
            ).strip()
            if detail:
                return detail
            return json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, list):
            return json.dumps(payload, ensure_ascii=False)
        return str(payload or "").strip()

    def _api_get_json(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> Any:
        response = self._requests_session.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=20,
        )
        payload = self._parse_http_response(response)
        if response.status_code >= 400:
            raise RuntimeError(self._error_summary(payload))
        return payload

    def _api_post_json(self, path: str, payload: dict[str, Any]) -> Any:
        response = self._requests_session.post(
            f"{self.base_url}{path}",
            json=payload,
            headers={**self._headers(), "Content-Type": "application/json"},
            timeout=20,
        )
        parsed = self._parse_http_response(response)
        if response.status_code >= 400:
            raise RuntimeError(self._error_summary(parsed))
        return parsed

    def _remember_lease(self, lease: PhoneLease) -> None:
        if not lease.phone:
            return
        self._leases_by_phone[lease.phone] = {
            "claim_token": lease.activation_id,
            "number_id": lease.extra.get("number_id"),
        }

    def _forget_lease(self, lease: PhoneLease) -> None:
        if lease.phone:
            self._leases_by_phone.pop(lease.phone, None)

    def _build_claim_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "app_slug": self.app_slug or None,
            "app_name": self.app_name,
            "country_name": self.country_name or None,
            "provider_id": self.provider_id or None,
            "purpose": "phone verification",
            "include_cooling": self.include_cooling,
            "ttl_minutes": self.claim_ttl_minutes,
        }
        return payload

    def _parse_claim_payload(self, payload: Any) -> PhoneLease:
        if not isinstance(payload, dict):
            raise RuntimeError(f"Free SMS Tool 返回了非 JSON 响应: {payload}")

        claim_token = str(payload.get("claim_token") or "").strip()
        phone = str(payload.get("e164") or "").strip()
        if not claim_token or not phone:
            raise RuntimeError(f"Free SMS Tool claim 缺少关键信息: {payload}")

        country_name = str(payload.get("country_name") or self.country_name or "").strip()
        lease = PhoneLease(
            phone=phone,
            activation_id=claim_token,
            country_name=country_name,
            country_slug=_slugify_label(country_name),
            provider=self.provider_key,
            extra={
                "claim": dict(payload),
                "claim_token": claim_token,
                "number_id": payload.get("number_id"),
                "claim_created_at": payload.get("created_at"),
            },
        )
        self._remember_lease(lease)
        return lease

    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneLease]:
        if not self.enabled:
            raise RuntimeError("Free SMS Tool 未配置 base_url/api_key")

        excluded = _normalize_phone_exception_prefixes(exclude_prefixes)
        last_error = ""
        for _attempt in range(self.max_attempts):
            payload = self._api_post_json("/api/claims", self._build_claim_payload())
            lease = self._parse_claim_payload(payload)
            phone_digits = re.sub(r"\D+", "", lease.phone)
            if excluded and any(phone_digits.startswith(prefix) for prefix in excluded):
                last_error = f"Free SMS Tool 返回了被排除前缀的号码: {lease.phone}"
                self._log(last_error)
                self.cancel_activation(lease)
                continue
            self._log(
                f"[FreeSmsTool] 认领成功: {lease.phone} claim_token={lease.activation_id}"
            )
            return lease

        if last_error:
            raise RuntimeError(last_error)
        return None

    def report_code_requested(self, entry: PhoneLease) -> None:
        requested_at = datetime.now(timezone.utc).isoformat()
        entry.extra["requested_at"] = requested_at
        self._log(
            f"[FreeSmsTool] 已记录发码时间: phone={entry.phone} requested_at={requested_at}"
        )

    def _message_threshold(self, entry: PhoneLease) -> Optional[datetime]:
        requested_at = entry.extra.get("requested_at")
        if requested_at:
            return _parse_iso_datetime(requested_at)
        return _parse_iso_datetime(entry.extra.get("claim_created_at"))

    def _extract_code_from_message(self, message: Any) -> Optional[str]:
        if not isinstance(message, dict):
            return None
        code = str(message.get("otp_code") or "").strip()
        if code:
            return code
        return self._safe_extract(str(message.get("body") or ""))

    def wait_for_code(
        self, entry: PhoneLease, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        wait_seconds = _to_positive_int(
            timeout, self.otp_timeout_seconds, minimum=10
        )
        threshold = self._message_threshold(entry)
        error_state: dict[str, str | None] = {"last": None}

        def poll_once() -> Optional[str]:
            try:
                payload = self._api_get_json(
                    f"/api/claims/{entry.activation_id}/messages",
                    {"limit": 20},
                )
                messages = payload if isinstance(payload, list) else []
                for item in messages:
                    received_at = _parse_iso_datetime(
                        item.get("received_at") if isinstance(item, dict) else None
                    )
                    if threshold and received_at and received_at < threshold:
                        continue
                    code = self._extract_code_from_message(item)
                    if code:
                        self._log(f"[FreeSmsTool] 收到验证码: {code}")
                        return code
            except Exception as error:
                summary = str(error or "").strip() or type(error).__name__
                if error_state.get("last") != summary:
                    error_state["last"] = summary
                    self._log(f"[FreeSmsTool] 拉取短信失败: {summary}")
            return None

        return self._run_polling_wait(
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            poll_once=poll_once,
            timeout_message=f"Free SMS Tool 等待验证码超时 ({wait_seconds}s)",
            raise_on_timeout=False,
        )

    def mark_blacklisted(self, phone: str) -> None:
        lease_state = self._leases_by_phone.get(str(phone or "").strip()) or {}
        number_id = lease_state.get("number_id")
        if not number_id:
            return
        payload = {
            "reason": "provider rejected during automated phone verification"
        }
        self._api_post_json(f"/api/numbers/{int(number_id)}/blacklist", payload)
        self._log(f"[FreeSmsTool] 已拉黑号码: phone={phone} number_id={number_id}")

    def finish_activation(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        payload = self._api_post_json(
            f"/api/claims/{entry.activation_id}/complete",
            {"result": "success", "app_state": "success"},
        )
        self._log(
            f"[FreeSmsTool] 完成 claim: {self._error_summary(payload) or entry.activation_id}"
        )
        self._forget_lease(entry)

    def cancel_activation(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        payload = self._api_post_json(
            f"/api/claims/{entry.activation_id}/release",
            {"result": "cancelled", "app_state": "available"},
        )
        self._log(
            f"[FreeSmsTool] 释放 claim: {self._error_summary(payload) or entry.activation_id}"
        )
        self._forget_lease(entry)


class FiveSimPhoneService(BasePhoneService):
    provider_key = "five_sim"
    provider_label = "5sim"
    BASE_URL = "https://5sim.net/v1"
    _COUNTRY_ALIASES = {
        "uk": "england",
        "gb": "england",
        "greatbritain": "england",
        "unitedkingdom": "england",
        "britain": "england",
        "us": "usa",
        "unitedstates": "usa",
        "unitedstatesofamerica": "usa",
        "holland": "netherlands",
    }
    _TERMINAL_STATES = {"BANNED", "CANCELED", "TIMEOUT", "FINISHED"}

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(config, log_fn=log_fn)
        self.api_key = str(self.config.get("five_sim_api_key", "") or "").strip()
        self.product = _slugify_label(
            str(self.config.get("five_sim_product", "other") or "other")
        )
        self.requested_country = str(
            self.config.get("five_sim_country", "") or ""
        ).strip()
        self.requested_operator = str(
            self.config.get("five_sim_operator", "") or ""
        ).strip()
        self.configured_max_price = _to_optional_float(
            self.config.get("five_sim_max_price")
        )
        self._max_attempts = _to_positive_int(
            self.config.get("five_sim_phone_attempts"), 3
        )
        self.otp_timeout_seconds = _to_positive_int(
            self.config.get("five_sim_otp_timeout_seconds"), 120, minimum=10
        )
        self.poll_interval_seconds = _to_positive_int(
            self.config.get("five_sim_poll_interval_seconds"), 5, minimum=1
        )
        self._requests_session = create_mailbox_requests_session(
            build_mailbox_proxy_config(None)
        )
        self._country_catalog: dict[str, Any] | None = None
        self._price_catalog: dict[str, Any] | None = None
        self._leases_by_phone: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _parse_http_response(self, response) -> Any:
        text = str(response.text or "").strip()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "json" in content_type or text.startswith("{") or text.startswith("["):
            try:
                return response.json()
            except Exception:
                pass
        return text

    def _error_summary(self, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "status", "detail"):
                detail = str(payload.get(key) or "").strip()
                if detail:
                    return detail
            return json.dumps(payload, ensure_ascii=False)
        if isinstance(payload, list):
            return json.dumps(payload, ensure_ascii=False)
        return str(payload or "").strip()

    def _api_get_json(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
    ) -> Any:
        response = self._requests_session.get(
            f"{self.BASE_URL}{path}",
            params=params,
            headers=self._headers() if auth else {"Accept": "application/json"},
            timeout=20,
        )
        payload = self._parse_http_response(response)
        if response.status_code >= 400:
            raise RuntimeError(self._error_summary(payload))
        return payload

    def _remember_lease(self, lease: PhoneLease) -> None:
        activation_id = str(lease.activation_id or "").strip()
        if not activation_id:
            return
        for key in {
            str(lease.phone or "").strip(),
            re.sub(r"\D+", "", str(lease.phone or "")),
        }:
            if key:
                self._leases_by_phone[key] = activation_id

    def _forget_lease(self, lease: PhoneLease) -> None:
        for key in {
            str(lease.phone or "").strip(),
            re.sub(r"\D+", "", str(lease.phone or "")),
        }:
            if key:
                self._leases_by_phone.pop(key, None)

    def _load_country_catalog(self) -> dict[str, Any]:
        if self._country_catalog is None:
            payload = self._api_get_json("/guest/countries", auth=False)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"5sim 国家列表返回异常: {self._error_summary(payload)}"
                )
            self._country_catalog = payload
        return self._country_catalog

    def _lookup_country_meta(self, slug: str) -> dict[str, Any]:
        catalog = self._load_country_catalog()
        payload = catalog.get(slug)
        if isinstance(payload, dict):
            return payload
        return {}

    def _country_display_name(self, slug: str) -> str:
        meta = self._lookup_country_meta(slug)
        for key in ("text_en", "text_ru"):
            value = str(meta.get(key) or "").strip()
            if value:
                return value
        return slug.replace("-", " ").title()

    def _resolve_country_slug(self, requested: str) -> str:
        raw = str(requested or "").strip()
        if not raw or raw.lower() in {"any", "auto"}:
            return ""

        catalog = self._load_country_catalog()
        normalized = _normalize_lookup_key(raw)
        alias = self._COUNTRY_ALIASES.get(normalized, normalized)

        if raw in catalog:
            return raw
        if alias in catalog:
            return alias

        for slug, meta in catalog.items():
            candidates = {
                _normalize_lookup_key(slug),
                _normalize_lookup_key(meta.get("text_en")),
                _normalize_lookup_key(meta.get("text_ru")),
            }
            candidates.update(
                _normalize_lookup_key(item) for item in (meta.get("iso") or {}).keys()
            )
            candidates.update(
                _normalize_lookup_key(item)
                for item in (meta.get("prefix") or {}).keys()
            )
            if alias in candidates or normalized in candidates:
                return slug

        raise RuntimeError(f"5sim 无法识别国家: {requested}")

    def _resolve_operator_slug(self, requested: str) -> str:
        raw = str(requested or "").strip()
        if not raw or raw.lower() in {"any", "auto"}:
            return ""
        return _normalize_lookup_key(raw)

    def _load_price_catalog(self) -> dict[str, Any]:
        if self._price_catalog is None:
            payload = self._api_get_json("/guest/prices", auth=False)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"5sim 价格列表返回异常: {self._error_summary(payload)}"
                )
            self._price_catalog = payload
        return self._price_catalog

    def _build_candidates(self) -> list[dict[str, Any]]:
        if not self.product:
            raise RuntimeError("5sim 未配置 product")

        requested_country = self._resolve_country_slug(self.requested_country)
        requested_operator = self._resolve_operator_slug(self.requested_operator)
        prices = self._load_price_catalog()

        candidates: list[dict[str, Any]] = []
        product_seen = False
        for country_slug, product_map in prices.items():
            if requested_country and country_slug != requested_country:
                continue
            if not isinstance(product_map, dict):
                continue
            operator_map = product_map.get(self.product)
            if not isinstance(operator_map, dict):
                continue
            product_seen = True
            for operator_slug, detail in operator_map.items():
                if requested_operator and operator_slug != requested_operator:
                    continue
                if not isinstance(detail, dict):
                    continue
                count = _to_positive_int(detail.get("count"), 0, minimum=0)
                if count <= 0:
                    continue
                cost = _to_optional_float(detail.get("cost"))
                if cost is None:
                    continue
                if (
                    self.configured_max_price is not None
                    and cost > self.configured_max_price
                ):
                    continue
                candidates.append(
                    {
                        "country_slug": country_slug,
                        "country_name": self._country_display_name(country_slug),
                        "operator": operator_slug,
                        "cost": cost,
                        "count": count,
                    }
                )

        if candidates:
            candidates.sort(
                key=lambda item: (
                    float(item["cost"]),
                    -int(item["count"]),
                    str(item["country_slug"]),
                    str(item["operator"]),
                )
            )
            return candidates

        if requested_country and requested_operator and self.configured_max_price is None:
            return [
                {
                    "country_slug": requested_country,
                    "country_name": self._country_display_name(requested_country),
                    "operator": requested_operator,
                    "cost": None,
                    "count": 0,
                }
            ]

        scope: list[str] = [f"product={self.product}"]
        if requested_country:
            scope.append(f"country={requested_country}")
        if requested_operator:
            scope.append(f"operator={requested_operator}")
        if self.configured_max_price is not None:
            scope.append(f"max_price={self.configured_max_price}")
        if not product_seen:
            raise RuntimeError(
                f"5sim 产品 {self.product} 不存在或当前没有价格数据 ({', '.join(scope)})"
            )
        raise RuntimeError(
            f"5sim 没有可用号码 ({', '.join(scope)})"
        )

    def _parse_order_payload(self, payload: Any) -> PhoneLease:
        if not isinstance(payload, dict):
            raise RuntimeError(f"5sim 返回了非 JSON 响应: {payload}")

        order_id = str(payload.get("id") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        country_slug = str(payload.get("country") or "").strip()
        if not order_id or not phone or not country_slug:
            raise RuntimeError(f"5sim order 缺少关键信息: {payload}")

        lease = PhoneLease(
            phone=phone,
            activation_id=order_id,
            country_name=self._country_display_name(country_slug),
            country_slug=country_slug,
            provider=self.provider_key,
            service_code=self.product,
            extra={
                "order": dict(payload),
                "operator": payload.get("operator"),
                "product": payload.get("product") or self.product,
                "created_at": payload.get("created_at"),
                "status": payload.get("status"),
            },
        )
        self._remember_lease(lease)
        return lease

    def _buy_candidate(self, candidate: dict[str, Any]) -> PhoneLease:
        path = (
            "/user/buy/activation/"
            f"{quote(str(candidate['country_slug']), safe='')}/"
            f"{quote(str(candidate['operator']), safe='')}/"
            f"{quote(str(self.product), safe='')}"
        )
        payload = self._api_get_json(path)
        return self._parse_order_payload(payload)

    def acquire_phone(
        self, *, exclude_prefixes: Optional[Iterable[str]] = None
    ) -> Optional[PhoneLease]:
        if not self.enabled:
            raise RuntimeError("5sim 未配置 API Key")

        excluded = _normalize_phone_exception_prefixes(exclude_prefixes)
        candidates = self._build_candidates()
        last_error = ""
        attempts_left = self.max_attempts

        for candidate in candidates:
            if attempts_left <= 0:
                break
            attempts_left -= 1
            try:
                lease = self._buy_candidate(candidate)
            except Exception as error:
                last_error = str(error)
                continue

            phone_digits = re.sub(r"\D+", "", lease.phone)
            if excluded and any(phone_digits.startswith(prefix) for prefix in excluded):
                last_error = f"5sim 返回了被排除前缀的号码: {lease.phone}"
                self._log(last_error)
                self.cancel_activation(lease)
                continue

            self._log(
                "[5sim] 取号成功: "
                f"{lease.phone} order_id={lease.activation_id} "
                f"country={candidate['country_slug']} operator={candidate['operator']} "
                f"price={candidate.get('cost')}"
            )
            return lease

        if last_error:
            raise RuntimeError(last_error)
        return None

    def report_code_requested(self, entry: PhoneLease) -> None:
        requested_at = datetime.now(timezone.utc).isoformat()
        entry.extra["requested_at"] = requested_at
        self._log(
            f"[5sim] 已记录发码时间: phone={entry.phone} requested_at={requested_at}"
        )

    def _extract_code_from_sms(self, item: Any) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        for key in ("code", "otp", "otp_code"):
            code = str(item.get(key) or "").strip()
            if code:
                return code
        for key in ("text", "sms", "body", "message"):
            code = self._safe_extract(str(item.get(key) or ""))
            if code:
                return code
        return None

    def _sms_received_at(self, item: Any) -> Optional[datetime]:
        if not isinstance(item, dict):
            return None
        for key in ("created_at", "date", "received_at"):
            parsed = _parse_iso_datetime(item.get(key))
            if parsed is not None:
                return parsed
        return None

    def wait_for_code(
        self, entry: PhoneLease, *, timeout: Optional[int] = None
    ) -> Optional[str]:
        wait_seconds = _to_positive_int(
            timeout, self.otp_timeout_seconds, minimum=10
        )
        threshold = _parse_iso_datetime(entry.extra.get("requested_at")) or _parse_iso_datetime(
            entry.extra.get("created_at")
        )
        error_state: dict[str, str | None] = {"last": None}

        def poll_once() -> Optional[str]:
            try:
                payload = self._api_get_json(f"/user/check/{entry.activation_id}")
                if not isinstance(payload, dict):
                    return None

                messages = payload.get("sms") if isinstance(payload.get("sms"), list) else []
                for item in reversed(messages):
                    received_at = self._sms_received_at(item)
                    if threshold and received_at and received_at < threshold:
                        continue
                    code = self._extract_code_from_sms(item)
                    if code:
                        self._log(f"[5sim] 收到验证码: {code}")
                        return code

                status = str(payload.get("status") or "").strip().upper()
                if status in self._TERMINAL_STATES:
                    self._log(f"[5sim] 订单已终止: {status}")
                    return None
            except Exception as error:
                summary = str(error or "").strip() or type(error).__name__
                if error_state.get("last") != summary:
                    error_state["last"] = summary
                    self._log(f"[5sim] 拉取短信失败: {summary}")
            return None

        return self._run_polling_wait(
            timeout=wait_seconds,
            poll_interval=self.poll_interval_seconds,
            poll_once=poll_once,
            timeout_message=f"5sim 等待验证码超时 ({wait_seconds}s)",
            raise_on_timeout=False,
        )

    def mark_blacklisted(self, phone: str) -> None:
        raw = str(phone or "").strip()
        activation_id = self._leases_by_phone.get(raw) or self._leases_by_phone.get(
            re.sub(r"\D+", "", raw)
        )
        if not activation_id:
            return
        try:
            payload = self._api_get_json(f"/user/ban/{activation_id}")
            self._log(
                f"[5sim] 已封禁号码: phone={phone} status={self._error_summary(payload) or activation_id}"
            )
        except Exception as error:
            self._log(f"[5sim] 封禁号码失败: {error}")

    def finish_activation(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        try:
            payload = self._api_get_json(f"/user/finish/{entry.activation_id}")
            self._log(
                f"[5sim] 完成订单: {self._error_summary(payload) or entry.activation_id}"
            )
        except Exception as error:
            self._log(f"[5sim] 完成订单失败: {error}")
        finally:
            self._forget_lease(entry)

    def cancel_activation(self, entry: PhoneLease) -> None:
        if not entry.activation_id:
            return
        try:
            payload = self._api_get_json(f"/user/cancel/{entry.activation_id}")
            self._log(
                f"[5sim] 取消订单: {self._error_summary(payload) or entry.activation_id}"
            )
        except Exception as error:
            self._log(f"[5sim] 取消订单失败: {error}")
        finally:
            self._forget_lease(entry)
