#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_phone import HeroSMSPhoneService, PhoneLease
from core.browser_runtime import with_chrome_executable
from core.config_store import config_store

KIMI_HOME_URL = "https://www.kimi.com/"
KIMI_VERIFY_CODE_FRAGMENT = "/api/user/sms/verify-code"
LOGIN_MODAL_TITLE = "手机号快捷登录"
DEFAULT_OUTPUT_DIR = ROOT / ".tmp" / "kimi_e2e"
KIMI_COUNTRY_CODE_URL = "https://www.kimi.com/api/user/sms/country-code"
DESKTOP_EDGE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
)
COUNTRY_NAME_ALIASES: dict[str, list[str]] = {
    "canada": ["Canada", "加拿大"],
    "chile": ["Chile", "智利"],
    "indonesia": ["Indonesia", "印度尼西亚"],
    "hong kong": ["Hong Kong", "香港", "中国香港"],
    "united kingdom": ["United Kingdom", "英国"],
    "thailand": ["Thailand", "泰国"],
    "usa": ["USA", "United States", "美国"],
    "united states": ["USA", "United States", "美国"],
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _text_dump(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text or ""), encoding="utf-8")


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _mask_phone(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 6:
        return raw
    return f"{raw[:4]}***{raw[-3:]}"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    return slug.strip("-") or "attempt"


def _trim_text(value: Any, *, limit: int = 3000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _normalize_phone_variants(phone: str, phone_code: str) -> list[str]:
    full_digits = _digits(phone)
    code_digits = _digits(phone_code)
    variants: list[str] = []

    def add(candidate: str) -> None:
        cleaned = re.sub(r"\s+", "", str(candidate or ""))
        if len(cleaned) < 6 or cleaned in variants:
            return
        variants.append(cleaned)

    if full_digits and code_digits and full_digits.startswith(code_digits):
        local_digits = full_digits[len(code_digits) :]
        add(local_digits)
        if local_digits.startswith("0"):
            add(local_digits.lstrip("0"))
        else:
            add("0" + local_digits)
    add(full_digits)
    if full_digits.startswith("0"):
        add(full_digits.lstrip("0"))
    return variants


def _parse_json_text(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _normalize_lookup_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _resolve_country_display_hints(country_name: str, phone_code: str) -> list[str]:
    target_name = _normalize_lookup_text(country_name)
    target_code = str(phone_code or "").strip()
    hints: list[str] = []
    if not target_name and not target_code:
        return hints
    alias_hints = COUNTRY_NAME_ALIASES.get(target_name, [])
    for item in alias_hints:
        if item and item not in hints:
            hints.append(item)
    try:
        response = requests.get(KIMI_COUNTRY_CODE_URL, timeout=20)
        payload = response.json()
    except Exception:
        return hints
    if not isinstance(payload, list):
        return hints
    for item in payload:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("country_name") or "").strip()
        item_code = str(item.get("phone_code") or "").strip()
        normalized_item_name = _normalize_lookup_text(item_name)
        if target_name and normalized_item_name == target_name:
            if item_name and item_name not in hints:
                hints.append(item_name)
            continue
        if target_name and any(_normalize_lookup_text(alias) == normalized_item_name for alias in alias_hints):
            if item_name and item_name not in hints:
                hints.append(item_name)
            continue
        if target_name and target_name in normalized_item_name:
            if item_name and item_name not in hints:
                hints.append(item_name)
            continue
        if target_code and item_code == target_code and not target_name:
            if item_name and item_name not in hints:
                hints.append(item_name)
    return hints


def _build_hero_config(args: argparse.Namespace) -> dict[str, Any]:
    all_config = dict(config_store.get_all() or {})
    if args.hero_sms_api_key:
        all_config["hero_sms_api_key"] = args.hero_sms_api_key
    all_config["hero_sms_service"] = args.service
    all_config["hero_sms_country"] = args.country
    all_config["hero_sms_phone_attempts"] = str(args.hero_phone_attempts)
    all_config["hero_sms_otp_timeout_seconds"] = str(args.otp_timeout_seconds)
    all_config["hero_sms_poll_interval_seconds"] = str(args.poll_interval_seconds)
    if args.hero_max_price is not None:
        all_config["hero_sms_max_price"] = str(args.hero_max_price)
    return all_config


def _read_windows_system_proxy() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except Exception:
        return ""

    if not enabled:
        return ""
    return str(server or "").strip()


def _normalize_proxy_server(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "=" in value and ";" in value:
        parts = {}
        for item in value.split(";"):
            if "=" not in item:
                continue
            key, server = item.split("=", 1)
            parts[str(key or "").strip().lower()] = str(server or "").strip()
        value = (
            parts.get("https")
            or parts.get("http")
            or parts.get("socks")
            or parts.get("all")
            or ""
        )
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return value


def _resolve_browser_proxy(setting: str) -> dict[str, Any]:
    mode = str(setting or "auto").strip().lower()
    env_candidates = [
        os.environ.get("https_proxy", ""),
        os.environ.get("HTTPS_PROXY", ""),
        os.environ.get("http_proxy", ""),
        os.environ.get("HTTP_PROXY", ""),
    ]
    env_proxy = next((item for item in env_candidates if str(item or "").strip()), "")
    system_proxy = _read_windows_system_proxy()

    if mode in {"", "auto", "system"}:
        resolved = _normalize_proxy_server(env_proxy) or _normalize_proxy_server(system_proxy)
    elif mode == "none":
        resolved = ""
    else:
        resolved = _normalize_proxy_server(setting)

    return {
        "mode": mode,
        "env_proxy": _normalize_proxy_server(env_proxy),
        "system_proxy": _normalize_proxy_server(system_proxy),
        "resolved_proxy": resolved,
    }


def _launch_browser(playwright, *, headless: bool, proxy_server: str = "") -> Browser:
    last_error: Exception | None = None
    for kwargs in (
        with_chrome_executable(headless=headless),
        {"channel": "msedge", "headless": headless},
        {"headless": headless},
    ):
        if proxy_server:
            kwargs["proxy"] = {"server": proxy_server}
        kwargs["args"] = ["--disable-blink-features=AutomationControlled"]
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Kimi browser launch failed: {last_error}") from last_error


def _create_context(browser: Browser) -> BrowserContext:
    context = browser.new_context(
        locale="zh-CN",
        viewport={"width": 1600, "height": 1200},
        timezone_id="Asia/Shanghai",
        user_agent=DESKTOP_EDGE_UA,
    )
    context.add_init_script(
        """
        () => {
          try {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
            window.chrome = window.chrome || { runtime: {} };
          } catch (err) {}
        }
        """
    )
    return context


def _extract_request_headers(headers: dict[str, str]) -> dict[str, str]:
    keep = {
        "authorization",
        "content-type",
        "referer",
        "user-agent",
        "x-language",
        "x-msh-device-id",
        "x-msh-platform",
        "x-msh-session-id",
        "x-msh-version",
        "x-traffic-id",
        "r-timezone",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
    }
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key or "").strip().lower() in keep
    }


def _browser_state(context: BrowserContext, page: Page) -> dict[str, Any]:
    cookies = context.cookies()
    cookie_header = "; ".join(
        f"{item.get('name')}={item.get('value')}" for item in cookies if item.get("name")
    )
    local_storage = page.evaluate(
        "() => Object.fromEntries(Object.entries(window.localStorage || {}))"
    )
    session_storage = page.evaluate(
        "() => Object.fromEntries(Object.entries(window.sessionStorage || {}))"
    )
    return {
        "url": str(page.url or ""),
        "cookies": cookies,
        "cookie_names": [str(item.get("name") or "") for item in cookies if item.get("name")],
        "cookie_header": cookie_header,
        "local_storage": local_storage,
        "session_storage": session_storage,
        "storage_state": context.storage_state(),
    }


def _write_browser_bootstrap_artifacts(
    *,
    context: BrowserContext,
    page: Page,
    attempt_dir: Path,
    proxy_info: dict[str, Any],
) -> None:
    payload = {
        "proxy_info": proxy_info,
        "user_agent": page.evaluate("() => navigator.userAgent"),
        "webdriver": page.evaluate("() => navigator.webdriver"),
        "language": page.evaluate("() => navigator.language"),
    }
    try:
        probe_page = context.new_page()
        probe_page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=30000)
        payload["ipify_text"] = probe_page.locator("body").inner_text(timeout=5000)
        probe_page.close()
    except Exception as exc:
        payload["ipify_error"] = str(exc)
    _json_dump(attempt_dir / "browser_bootstrap.json", payload)


def _apply_hero_candidate_preference(
    phone_service: HeroSMSPhoneService,
    *,
    prefer_expensive: bool,
) -> None:
    if not prefer_expensive:
        return

    original = phone_service._build_offer_candidates

    def _wrapped(service_code: str) -> list[dict[str, Any]]:
        rows = list(original(service_code))
        rows.sort(
            key=lambda item: (
                -float(item["price"]),
                -int(item["default_count"]),
                -int(item["physical"]),
                -int(item["total"]),
                int(item["country_id"]),
            )
        )
        return rows

    phone_service._build_offer_candidates = _wrapped


def _wait_enabled(locator, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if locator.is_enabled():
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("等待按钮可点击超时")


def _open_login_modal(page: Page) -> None:
    page.goto(KIMI_HOME_URL, wait_until="domcontentloaded", timeout=120000)
    page.wait_for_timeout(3000)
    for selector in (
        ".user-info",
        ".not-login-container",
        ".user-info-container",
        ".nav-item.more-history",
    ):
        locator = page.locator(selector)
        if locator.count() == 0:
            continue
        try:
            locator.first.click(force=True, timeout=5000)
            break
        except Exception:
            continue
    page.get_by_text(LOGIN_MODAL_TITLE).wait_for(timeout=30000)


def _check_terms(page: Page) -> None:
    checkbox = page.get_by_role("checkbox").first
    try:
        if checkbox.is_checked():
            return
    except Exception:
        pass
    checkbox.click(force=True)
    page.wait_for_timeout(300)


def _select_country(page: Page, *, phone_code: str) -> dict[str, Any]:
    return _select_country_with_name(page, phone_code=phone_code, country_name="")


def _select_country_with_name(
    page: Page,
    *,
    phone_code: str,
    country_name: str,
) -> dict[str, Any]:
    trigger = page.locator(".phone-login-region-code").first
    trigger.click(force=True)
    list_box = page.locator("ul.select-region").first
    list_box.wait_for(state="visible", timeout=10000)
    all_items = list_box.locator("li.select-region-item")
    try:
        count = all_items.count()
    except Exception:
        count = 0
    if count == 0:
        raise RuntimeError("Kimi 区号列表为空")

    display_hints = _resolve_country_display_hints(country_name, phone_code)
    matched_index = -1
    fallback_index = -1
    seen_texts: list[str] = []
    for idx in range(count):
        text = all_items.nth(idx).inner_text(timeout=2000).strip()
        if text:
            seen_texts.append(text)
        if phone_code and phone_code not in text:
            continue
        if fallback_index < 0:
            fallback_index = idx
        if any(hint and hint in text for hint in display_hints):
            matched_index = idx
            break
    target_index = matched_index if matched_index >= 0 else fallback_index
    if target_index < 0:
        preview = seen_texts[:20]
        raise RuntimeError(
            f"Kimi 区号列表未找到 {phone_code}"
            f" country={country_name or '-'}"
            f" hints={display_hints}"
            f" items={preview}"
        )
    item = all_items.nth(target_index)
    item_text = item.inner_text().strip()
    item.click(force=True)
    page.wait_for_timeout(500)
    selected_value = page.locator("input.region-code").first.input_value().strip()
    return {
        "selected_text": item_text,
        "selected_value": selected_value,
        "display_hints": display_hints,
    }


def _submit_send_code(
    page: Page,
    *,
    phone_variant: str,
    attempt_dir: Path,
) -> dict[str, Any]:
    phone_input = page.get_by_placeholder("请输入手机号").first
    send_button = page.get_by_role("button", name="发送验证码").first
    phone_input.fill("")
    phone_input.fill(phone_variant)
    _check_terms(page)
    _wait_enabled(send_button)

    with page.expect_response(
        lambda resp: KIMI_VERIFY_CODE_FRAGMENT in resp.url
        and resp.request.method.upper() == "POST",
        timeout=60000,
    ) as pending:
        send_button.click(force=True)
    response = pending.value
    request = response.request
    response_text = response.text()
    request_body = request.post_data or ""
    request_headers = _extract_request_headers(request.headers)
    payload = {
        "request_url": request.url,
        "request_method": request.method,
        "request_headers": request_headers,
        "request_body_raw": request_body,
        "request_body_json": _parse_json_text(request_body),
        "response_status": response.status,
        "response_text": response_text,
        "response_json": _parse_json_text(response_text),
    }
    _json_dump(attempt_dir / "send_code_exchange.json", payload)
    return payload


def _collect_user_api_events(page: Page) -> tuple[list[dict[str, Any]], Any]:
    events: list[dict[str, Any]] = []

    def _capture(response) -> None:
        try:
            request = response.request
            if "/api/user/" not in request.url:
                return
            events.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "status": response.status,
                    "request_body": request.post_data or "",
                    "request_headers": _extract_request_headers(request.headers),
                    "response_text": _trim_text(response.text()),
                }
            )
        except Exception:
            return

    page.on("response", _capture)
    return events, _capture


def _submit_login_and_capture(
    context: BrowserContext,
    page: Page,
    *,
    otp_code: str,
    attempt_dir: Path,
) -> dict[str, Any]:
    before_state = _browser_state(context, page)
    code_input = page.get_by_placeholder("请输入验证码").first
    code_input.fill("")
    code_input.fill(otp_code)
    _check_terms(page)

    api_events, handler = _collect_user_api_events(page)
    login_button = page.get_by_role("button", name="登录").first
    _wait_enabled(login_button)
    login_button.click()

    success_markers: list[str] = []
    deadline = time.monotonic() + 40
    after_state = before_state
    while time.monotonic() < deadline:
        after_state = _browser_state(context, page)
        if page.get_by_text(LOGIN_MODAL_TITLE).count() == 0:
            success_markers.append("modal_closed")
        if after_state["local_storage"] != before_state["local_storage"]:
            success_markers.append("local_storage_changed")
        if after_state["session_storage"] != before_state["session_storage"]:
            success_markers.append("session_storage_changed")
        if after_state["cookie_header"] != before_state["cookie_header"]:
            success_markers.append("cookie_header_changed")
        if success_markers:
            break
        time.sleep(0.5)

    try:
        page.remove_listener("response", handler)
    except Exception:
        pass

    _json_dump(attempt_dir / "login_api_events.json", api_events)
    _json_dump(attempt_dir / "browser_state_before_login.json", before_state)
    _json_dump(attempt_dir / "browser_state_after_login.json", after_state)
    return {
        "success_markers": sorted(set(success_markers)),
        "before_state": before_state,
        "after_state": after_state,
        "api_events": api_events,
    }


def _maybe_upload_kimi2api(
    *,
    args: argparse.Namespace,
    attempt_dir: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    api_url = str(args.kimi2api_url or "").strip()
    auth_token = str(args.kimi2api_auth_token or "").strip()
    if not api_url:
        return {"skipped": True, "reason": "kimi2api_url_not_configured"}

    payload = {
        "source": "kimi_phone_e2e_probe",
        "captured_at": _utc_now_iso(),
        "result": result,
    }
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        response = requests.post(api_url, json=payload, headers=headers, timeout=30)
        response_text = response.text.strip()
        upload_result = {
            "skipped": False,
            "status_code": response.status_code,
            "response_text": _trim_text(response_text),
            "response_json": _parse_json_text(response_text),
            "ok": response.status_code < 400,
        }
    except Exception as exc:
        upload_result = {
            "skipped": False,
            "ok": False,
            "error": str(exc),
        }
    _json_dump(attempt_dir / "kimi2api_upload.json", upload_result)
    return upload_result


def _attempt_summary(
    *,
    attempt_index: int,
    lease: PhoneLease,
    send_exchange: dict[str, Any] | None = None,
    otp_code: str | None = None,
    login_result: dict[str, Any] | None = None,
    upload_result: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    response_json = (send_exchange or {}).get("response_json")
    return {
        "attempt": attempt_index,
        "phone_masked": _mask_phone(lease.phone),
        "phone_full": lease.phone,
        "country_name": lease.country_name,
        "country_slug": lease.country_slug,
        "activation_id": lease.activation_id,
        "send_response_status": (send_exchange or {}).get("response_status"),
        "send_response_json": response_json,
        "otp_received": bool(otp_code),
        "otp_code": otp_code or "",
        "login_success_markers": (login_result or {}).get("success_markers") or [],
        "upload_result": upload_result or {},
        "error": error,
    }


def run_attempt(
    *,
    args: argparse.Namespace,
    attempt_index: int,
    phone_service: HeroSMSPhoneService,
    output_dir: Path,
    exclude_prefixes: set[str],
) -> dict[str, Any]:
    lease = phone_service.acquire_phone(exclude_prefixes=sorted(exclude_prefixes))
    if not lease:
        raise RuntimeError("HeroSMS 未返回号码")

    attempt_dir = output_dir / f"{attempt_index:02d}_{_safe_slug(lease.country_slug or lease.country_name)}_{_digits(lease.phone)[-6:]}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    _json_dump(
        attempt_dir / "lease.json",
        {
            "phone": lease.phone,
            "activation_id": lease.activation_id,
            "country_id": lease.country_id,
            "country_name": lease.country_name,
            "country_slug": lease.country_slug,
            "provider": lease.provider,
            "service_code": lease.service_code,
            "extra": lease.extra,
        },
    )

    send_exchange: dict[str, Any] | None = None
    otp_code = ""
    login_result: dict[str, Any] | None = None
    upload_result: dict[str, Any] | None = None
    error = ""

    try:
        phone_variants = _normalize_phone_variants(lease.phone, args.phone_code)
        if not phone_variants:
            raise RuntimeError(f"无法从号码派生 Kimi 输入格式: {lease.phone}")
        _json_dump(attempt_dir / "phone_variants.json", phone_variants)
        proxy_info = _resolve_browser_proxy(args.browser_proxy)
        _json_dump(attempt_dir / "proxy_resolution.json", proxy_info)

        with sync_playwright() as playwright:
            browser = _launch_browser(
                playwright,
                headless=not args.headed,
                proxy_server=str(proxy_info.get("resolved_proxy") or ""),
            )
            context = _create_context(browser)
            page = context.new_page()
            try:
                _write_browser_bootstrap_artifacts(
                    context=context,
                    page=page,
                    attempt_dir=attempt_dir,
                    proxy_info=proxy_info,
                )
                _open_login_modal(page)
                country_result = _select_country_with_name(
                    page,
                    phone_code=args.phone_code,
                    country_name=args.country,
                )
                _json_dump(attempt_dir / "country_selection.json", country_result)

                last_send_error = ""
                selected_variant = ""
                for variant in phone_variants:
                    try:
                        send_exchange = _submit_send_code(
                            page,
                            phone_variant=variant,
                            attempt_dir=attempt_dir / _safe_slug(variant),
                        )
                        selected_variant = variant
                        response_status = int(send_exchange.get("response_status") or 0)
                        if response_status >= 400:
                            response_json = send_exchange.get("response_json")
                            last_send_error = (
                                f"HTTP {response_status}: "
                                f"{response_json if response_json is not None else send_exchange.get('response_text')}"
                            )
                            continue
                        response_json = send_exchange.get("response_json")
                        if isinstance(response_json, dict) and response_json.get("error_type"):
                            last_send_error = (
                                str(response_json.get("message") or response_json.get("detail") or response_json)
                            )
                            continue
                        break
                    except Exception as exc:
                        last_send_error = str(exc)
                        continue
                else:
                    raise RuntimeError(last_send_error or "Kimi 发码失败")

                _text_dump(attempt_dir / "selected_phone_variant.txt", selected_variant)
                phone_service.report_code_requested(lease)
                otp_code = str(
                    phone_service.wait_for_code(lease, timeout=args.otp_timeout_seconds) or ""
                ).strip()
                hero_sms_status_snapshot: dict[str, Any] = {}
                try:
                    hero_sms_status_snapshot = {
                        "status_v2": phone_service._get_status_v2_payload(lease.activation_id),
                        "status": phone_service._get_status_payload(lease.activation_id),
                    }
                except Exception as exc:
                    hero_sms_status_snapshot = {"error": str(exc)}
                _json_dump(
                    attempt_dir / "hero_sms_status_after_wait.json",
                    hero_sms_status_snapshot,
                )
                if not otp_code:
                    raise RuntimeError("HeroSMS 未收到短信验证码")
                _text_dump(attempt_dir / "otp_code.txt", otp_code)

                login_result = _submit_login_and_capture(
                    context,
                    page,
                    otp_code=otp_code,
                    attempt_dir=attempt_dir,
                )
                if not login_result.get("success_markers"):
                    raise RuntimeError("Kimi 登录后未观察到明确的登录态变化")

                result_payload = {
                    "lease": {
                        "phone": lease.phone,
                        "activation_id": lease.activation_id,
                        "country_name": lease.country_name,
                        "country_slug": lease.country_slug,
                    },
                    "send_exchange": send_exchange,
                    "otp_code": otp_code,
                    "login_result": login_result,
                }
                upload_result = _maybe_upload_kimi2api(
                    args=args,
                    attempt_dir=attempt_dir,
                    result=result_payload,
                )
                phone_service.finish_activation(lease)
            finally:
                try:
                    page.screenshot(path=str(attempt_dir / "final_page.png"), full_page=False)
                except Exception:
                    pass
                context.close()
                browser.close()
    except Exception as exc:
        error = str(exc)
        phone_service.cancel_activation(lease)

    exclude_prefixes.add(phone_service.prefix_hint(lease.phone))
    summary = _attempt_summary(
        attempt_index=attempt_index,
        lease=lease,
        send_exchange=send_exchange,
        otp_code=otp_code,
        login_result=login_result,
        upload_result=upload_result,
        error=error,
    )
    _json_dump(attempt_dir / "attempt_summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Kimi phone-route E2E validation with HeroSMS."
    )
    parser.add_argument("--hero-sms-api-key", default="", help="HeroSMS API key")
    parser.add_argument("--country", default="Chile", help="HeroSMS country name")
    parser.add_argument("--phone-code", default="+56", help="Kimi phone code")
    parser.add_argument("--service", default="Kimi", help="HeroSMS service name/code")
    parser.add_argument("--attempts", type=int, default=5, help="Number of real phone attempts")
    parser.add_argument("--hero-phone-attempts", type=int, default=3, help="HeroSMS internal acquire retries")
    parser.add_argument("--hero-max-price", type=float, default=None, help="Optional HeroSMS max price")
    parser.add_argument("--prefer-expensive", action="store_true", help="Prefer higher-price HeroSMS candidates")
    parser.add_argument("--otp-timeout-seconds", type=int, default=120, help="OTP wait timeout")
    parser.add_argument("--poll-interval-seconds", type=int, default=5, help="HeroSMS OTP polling interval")
    parser.add_argument("--pause-seconds", type=float, default=2.0, help="Pause between attempts")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Artifact directory")
    parser.add_argument("--headed", action="store_true", help="Run headed browser")
    parser.add_argument(
        "--browser-proxy",
        default="auto",
        help="Browser proxy mode/url: auto, system, none, or explicit proxy url",
    )
    parser.add_argument("--kimi2api-url", default="", help="Optional kimi2api upload endpoint")
    parser.add_argument("--kimi2api-auth-token", default="", help="Optional kimi2api bearer token")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    phone_service = HeroSMSPhoneService(_build_hero_config(args))
    if not phone_service.enabled:
        raise SystemExit("HeroSMS API key not configured")
    _apply_hero_candidate_preference(
        phone_service,
        prefer_expensive=bool(args.prefer_expensive),
    )

    excluded_prefixes: set[str] = set()
    results: list[dict[str, Any]] = []
    for attempt_index in range(1, max(int(args.attempts or 1), 1) + 1):
        started_at = _utc_now_iso()
        summary = run_attempt(
            args=args,
            attempt_index=attempt_index,
            phone_service=phone_service,
            output_dir=output_dir,
            exclude_prefixes=excluded_prefixes,
        )
        summary["started_at"] = started_at
        summary["finished_at"] = _utc_now_iso()
        results.append(summary)
        print(
            json.dumps(
                {
                    "attempt": summary["attempt"],
                    "phone_masked": summary["phone_masked"],
                    "send_response_status": summary["send_response_status"],
                    "otp_received": summary["otp_received"],
                    "login_success_markers": summary["login_success_markers"],
                    "error": summary["error"],
                },
                ensure_ascii=False,
            )
        )
        if attempt_index < args.attempts:
            time.sleep(max(float(args.pause_seconds or 0), 0.0))

    final_summary = {
        "country": args.country,
        "phone_code": args.phone_code,
        "service": args.service,
        "attempts": args.attempts,
        "browser_proxy": args.browser_proxy,
        "hero_max_price": args.hero_max_price,
        "prefer_expensive": bool(args.prefer_expensive),
        "generated_at": _utc_now_iso(),
        "results": results,
    }
    _json_dump(output_dir / "summary.json", final_summary)
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
