#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_mailbox import create_mailbox
from core.browser_backend import sync_playwright
from core.config_store import config_store
from platforms.deepseek.core import (
    _collect_deepseek_form_state,
    _collect_deepseek_send_code_page_state,
    _fill_deepseek_input,
    _open_deepseek_sign_up_browser_page,
    _parse_deepseek_playwright_json_response,
    _request_deepseek_guest_pow_response_via_browser,
    _wait_for_deepseek_sign_up_form,
    random_password,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a headed DeepSeek send-code handoff flow for manual challenge solving."
    )
    parser.add_argument("--proxy", default="socks5://192.168.1.18:1083")
    parser.add_argument("--ui-locale", default="ja-JP")
    parser.add_argument("--region", default="US")
    parser.add_argument("--tz-offset-seconds", default="32400")
    parser.add_argument("--mail-provider", default="outlookemail")
    parser.add_argument("--mail-domain", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--flaresolverr-url", default="http://127.0.0.1:8191/v1")
    parser.add_argument("--user-data-dir", default="")
    parser.add_argument("--manual-timeout-seconds", type=int, default=600)
    parser.add_argument("--mailbox-timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--artifact",
        default="docs/artifacts/deepseek-manual-send-code-handoff.json",
    )
    return parser.parse_args()


def _print(payload: Any) -> None:
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return
    print(str(payload), flush=True)


def _mask_email(email: str) -> str:
    value = str(email or "").strip()
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    if len(local) <= 4:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-2:]}@{domain}"


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if "password" in lowered:
                out[key] = "***"
            elif any(marker in lowered for marker in ("token", "captcha", "pow")):
                text = str(child or "").strip()
                if text:
                    out[key] = {
                        "present": True,
                        "length": len(text),
                        "prefix": text[:24],
                    }
                else:
                    out[key] = child
            else:
                out[key] = _sanitize(child)
        return out
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_extra(args: argparse.Namespace) -> dict[str, Any]:
    extra = config_store.get_all().copy()
    if str(args.mail_provider or "").strip():
        extra["mail_provider"] = str(args.mail_provider).strip()
    extra["deepseek_ui_locale"] = args.ui_locale
    extra["deepseek_region"] = args.region
    extra["deepseek_tz_offset_seconds"] = args.tz_offset_seconds
    if str(args.flaresolverr_url or "").strip():
        extra["deepseek_flaresolverr_url"] = str(args.flaresolverr_url).strip()
    if str(args.mail_domain or "").strip():
        domain_override = str(args.mail_domain).strip().lstrip("@")
        for key in (
            "imail_domain",
            "edumail_domain",
            "boomlify_domain",
            "nullsto_domain",
            "gptmail_domain",
            "maliapi_domain",
            "duckmail_domain",
            "skymail_domain",
            "cloudmail_domain",
            "freemail_domain",
            "opentrashmail_domain",
            "cfrouting_domain",
            "cfworker_domain",
            "cfworker_domain_override",
            "cfworker_domains",
            "cfworker_enabled_domains",
        ):
            extra[key] = domain_override
    return extra


def _resolve_user_data_dir(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    else:
        path = path.resolve()
    return str(path)


def main() -> int:
    args = _parse_args()
    artifact_path = ROOT / args.artifact
    extra = _build_extra(args)
    proxy = str(args.proxy or "").strip() or None
    password = str(args.password or "").strip() or random_password()
    user_data_dir = _resolve_user_data_dir(args.user_data_dir)

    mailbox = create_mailbox(
        provider=str(extra.get("mail_provider") or "outlookemail").strip() or "outlookemail",
        extra=extra,
        proxy=proxy,
        platform="deepseek",
    )
    mail_account = mailbox.get_email()
    email = str(getattr(mail_account, "email", "") or "").strip()
    if not email:
        raise RuntimeError("未获取到可用邮箱")
    before_ids = mailbox.get_current_ids(mail_account)

    result: dict[str, Any] = {
        "ok": False,
        "email": _mask_email(email),
        "proxy": proxy or "",
        "ui_locale": args.ui_locale,
        "flaresolverr_url": str(args.flaresolverr_url or "").strip(),
        "mail_provider": str(extra.get("mail_provider") or ""),
        "user_data_dir": user_data_dir,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    _print(
        {
            "phase": "launch",
            "email": result["email"],
            "proxy": result["proxy"],
            "ui_locale": result["ui_locale"],
            "flaresolverr_url": result["flaresolverr_url"],
            "mail_provider": result["mail_provider"],
            "user_data_dir": result["user_data_dir"],
        }
    )

    browser = None
    context = None
    send_request_capture: dict[str, Any] = {}
    send_response_capture: dict[str, Any] = {}
    challenge_network: list[dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            try:
                browser, context, page, sign_up_url = _open_deepseek_sign_up_browser_page(
                    p,
                    proxy=proxy,
                    ui_locale=args.ui_locale,
                    headless=False,
                    flaresolverr_url=str(args.flaresolverr_url or "").strip() or None,
                    align_flaresolverr_identity=bool(str(args.flaresolverr_url or "").strip()),
                    user_data_dir=user_data_dir or None,
                    log_fn=lambda message: _print({"phase": "browser", "message": message}),
                )
                result["sign_up_url"] = sign_up_url

                def on_request(request) -> None:
                    url = str(request.url or "")
                    lowered = url.lower()
                    if any(
                        marker in lowered
                        for marker in (
                            "hcaptcha",
                            "turnstile",
                            "captcha",
                            "cloudflare",
                            "fengkongcloud",
                        )
                    ):
                        challenge_network.append(
                            {
                                "kind": "request",
                                "method": request.method,
                                "url": url[:500],
                                "resource_type": request.resource_type,
                            }
                        )

                def on_response(response) -> None:
                    url = str(response.url or "")
                    lowered = url.lower()
                    if any(
                        marker in lowered
                        for marker in (
                            "hcaptcha",
                            "turnstile",
                            "captcha",
                            "cloudflare",
                            "fengkongcloud",
                        )
                    ):
                        challenge_network.append(
                            {
                                "kind": "response",
                                "status": response.status,
                                "url": url[:500],
                            }
                        )

                page.on("request", on_request)
                page.on("response", on_response)

                _wait_for_deepseek_sign_up_form(page)
                email_input = page.locator(
                    'input.ds-input__input[type="text"], input.ds-input__input[type="email"]'
                ).first
                password_inputs = page.locator('input.ds-input__input[type="password"]')
                send_code_button = page.locator("button.ds-verify-code-input-countdown").first

                _fill_deepseek_input(email_input, email, field_name="email")
                _fill_deepseek_input(password_inputs.nth(0), password, field_name="password")
                _fill_deepseek_input(
                    password_inputs.nth(1),
                    password,
                    field_name="confirm_password",
                )

                result["before_state"] = _sanitize(_collect_deepseek_form_state(page))
                before_png = artifact_path.with_name(artifact_path.stem + "-before.png")
                page.screenshot(path=str(before_png), full_page=True)
                result["before_screenshot"] = str(before_png.relative_to(ROOT))

                banner_text = (
                    "Manual DeepSeek handoff ready.\\n"
                    f"Email: {email}\\n"
                    "Please click Send code and solve the challenge manually.\\n"
                    "This script is listening for the real send-code response."
                )
                page.evaluate(
                    """(bannerText) => {
                        const existing = document.getElementById('codex-manual-handoff-banner');
                        if (existing) existing.remove();
                        const banner = document.createElement('pre');
                        banner.id = 'codex-manual-handoff-banner';
                        banner.textContent = bannerText;
                        banner.style.position = 'fixed';
                        banner.style.right = '16px';
                        banner.style.top = '16px';
                        banner.style.zIndex = '2147483647';
                        banner.style.background = 'rgba(17, 24, 39, 0.92)';
                        banner.style.color = '#f9fafb';
                        banner.style.padding = '12px 14px';
                        banner.style.borderRadius = '12px';
                        banner.style.font = '12px/1.5 Consolas, monospace';
                        banner.style.maxWidth = '420px';
                        banner.style.whiteSpace = 'pre-wrap';
                        banner.style.boxShadow = '0 12px 32px rgba(0,0,0,0.35)';
                        document.body.appendChild(banner);
                    }""",
                    banner_text,
                )

                send_code_pow_response = ""
                try:
                    send_code_pow_response = _request_deepseek_guest_pow_response_via_browser(
                        page,
                        target_path="/api/v0/users/create_email_verification_code",
                        proxy=proxy,
                        ui_locale=args.ui_locale,
                        sign_up_url=sign_up_url,
                        tz_offset_seconds=args.tz_offset_seconds,
                        pow_worker_url=str(extra.get("deepseek_pow_worker_url") or ""),
                    )
                    if send_code_pow_response:
                        _print({"phase": "pow", "message": "已生成发码 PoW header"})
                except Exception as exc:
                    _print({"phase": "pow", "error": str(exc)})

                def route_handler(route) -> None:
                    try:
                        payload = json.loads(str(route.request.post_data or "").strip() or "{}")
                    except Exception:
                        payload = {"_raw": str(route.request.post_data or "")[:1200]}
                    headers = dict(route.request.headers)
                    send_request_capture["payload"] = _sanitize(payload)
                    send_request_capture["headers_before"] = _sanitize(
                        {
                            key: value
                            for key, value in headers.items()
                            if key.lower().startswith("x-")
                            or key.lower() in {
                                "accept-language",
                                "content-type",
                                "origin",
                                "referer",
                                "user-agent",
                            }
                        }
                    )
                    if send_code_pow_response and not str(headers.get("x-ds-guest-pow-response") or "").strip():
                        headers["x-ds-guest-pow-response"] = send_code_pow_response
                    send_request_capture["headers_after"] = _sanitize(
                        {
                            key: value
                            for key, value in headers.items()
                            if key.lower().startswith("x-")
                            or key.lower() in {
                                "accept-language",
                                "content-type",
                                "origin",
                                "referer",
                                "user-agent",
                            }
                        }
                    )
                    if send_code_pow_response:
                        route.continue_(headers=headers)
                        return
                    route.continue_()

                route_pattern = "**/api/v0/users/create_email_verification_code"
                page.route(route_pattern, route_handler)

                _print(
                    {
                        "phase": "handoff_ready",
                        "message": "浏览器已就绪，请在弹出的浏览器窗口里手动点击 Send code 并完成 challenge。",
                        "email": result["email"],
                    }
                )

                deadline = time.time() + max(args.manual_timeout_seconds, 30)
                send_response = None
                while time.time() < deadline:
                    try:
                        response = page.wait_for_response(
                            lambda resp: resp.request.method == "POST"
                            and "/api/v0/users/create_email_verification_code" in resp.url,
                            timeout=1000,
                        )
                        send_response = response
                        break
                    except Exception:
                        pass
                    if page.is_closed():
                        raise RuntimeError("浏览器页面已被关闭，未捕获到发码请求")

                if send_response is None:
                    raise TimeoutError(f"等待手动发码超时 ({args.manual_timeout_seconds}s)")

                send_data = _parse_deepseek_playwright_json_response(
                    send_response,
                    stage="手动浏览器发码",
                )
                result["send_code_response"] = _sanitize(send_data)
                send_response_capture["status"] = send_response.status
                send_response_capture["url"] = send_response.url
                result["send_code_request"] = send_request_capture
                result["send_code_response_meta"] = send_response_capture

                page.wait_for_timeout(1500)
                result["after_state"] = _sanitize(
                    _collect_deepseek_send_code_page_state(
                        page,
                        send_code_button=send_code_button,
                    )
                )
                result["after_form_state"] = _sanitize(_collect_deepseek_form_state(page))
                after_png = artifact_path.with_name(artifact_path.stem + "-after.png")
                page.screenshot(path=str(after_png), full_page=True)
                result["after_screenshot"] = str(after_png.relative_to(ROOT))
                result["challenge_network"] = challenge_network

                inner = send_data.get("data", {})
                if inner.get("biz_code") in (0, "0"):
                    sent_at = time.time()
                    code = mailbox.wait_for_code(
                        mail_account,
                        keyword="DeepSeek",
                        timeout=max(args.mailbox_timeout_seconds, 30),
                        before_ids=before_ids,
                        otp_sent_at=sent_at,
                    )
                    result["mailbox_code"] = code
                    result["ok"] = True
                    _print(
                        {
                            "phase": "mailbox",
                            "message": "已收到 DeepSeek 验证码",
                            "code": code,
                        }
                    )
                else:
                    result["ok"] = False

                _write_artifact(artifact_path, result)
                _print(
                    {
                        "phase": "done",
                        "artifact": str(artifact_path.relative_to(ROOT)),
                        "ok": result["ok"],
                    }
                )
                page.wait_for_timeout(3000)
                return 0 if result["ok"] else 1
            finally:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        result["challenge_network"] = challenge_network
        _write_artifact(artifact_path, result)
        _print(
            {
                "phase": "error",
                "artifact": str(artifact_path.relative_to(ROOT)),
                "error": str(exc),
            }
        )
        return 1
    finally:
        if not result.get("ok"):
            release_current = getattr(mailbox, "release_current_account", None)
            if callable(release_current):
                try:
                    release_current()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
