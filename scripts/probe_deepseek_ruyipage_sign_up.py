#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import winreg
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ruyipage import FirefoxOptions, FirefoxPage  # noqa: E402
from ruyipage._runtime.resolver import get_executable_path  # noqa: E402

from platforms.deepseek.core import (  # noqa: E402
    _classify_deepseek_sign_up_state,
    _summarize_deepseek_sign_up_state,
    build_deepseek_page_url,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe DeepSeek sign_up with ruyipage via the current system proxy."
    )
    parser.add_argument(
        "--proxy",
        default="",
        help="Optional explicit proxy URL. If omitted, resolve from the local system proxy.",
    )
    parser.add_argument(
        "--ui-locale",
        default="en-US",
        help="Browser locale hint used for DeepSeek sign_up probing.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Launch a visible Firefox window instead of headless mode.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=45,
        help="How long to wait for DeepSeek sign_up to settle into a known branch.",
    )
    parser.add_argument(
        "--artifact",
        default="docs/artifacts/deepseek-ruyipage-sign-up-probe.json",
        help="Artifact JSON output path, relative to repo root.",
    )
    return parser.parse_args()


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _mask_ip(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text[:8] + "***"
    parts = text.split(".")
    if len(parts) != 4:
        return text[:6] + "***"
    return ".".join(parts[:2] + ["*", "*"])


def _normalize_proxy_value(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if ";" in value and "=" in value:
        mapping: dict[str, str] = {}
        for item in value.split(";"):
            if "=" not in item:
                continue
            key, candidate = item.split("=", 1)
            mapping[str(key).strip().lower()] = str(candidate).strip()
        for key in ("https", "http", "socks", "socks5"):
            candidate = str(mapping.get(key) or "").strip()
            if not candidate:
                continue
            if key in {"socks", "socks5"}:
                return f"socks5://{candidate}"
            return f"http://{candidate}"
    return f"http://{value}"


def _resolve_system_proxy() -> tuple[str, str]:
    for env_name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        candidate = _normalize_proxy_value(os.environ.get(env_name, ""))
        if candidate:
            return candidate, f"env:{env_name}"

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except OSError:
        enabled = 0
        server = ""

    if enabled and server:
        candidate = _normalize_proxy_value(server)
        if candidate:
            return candidate, "registry:Internet Settings"

    return "", "none"


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_body_text(page: FirefoxPage) -> str:
    value = page.run_js("document.body ? document.body.innerText : ''")
    return str(value or "").strip()


def _read_text_endpoint(page: FirefoxPage, url: str, *, timeout_seconds: int = 30) -> str:
    page.get(url, timeout=timeout_seconds)
    time.sleep(1)
    body = _read_body_text(page)
    if not body:
        return ""
    return body.splitlines()[0].strip()


def _probe_egress(page: FirefoxPage) -> dict[str, Any]:
    ip_text = _read_text_endpoint(page, "https://api.ipify.org")
    country_text = _read_text_endpoint(page, "https://ipinfo.io/country")
    region_text = _read_text_endpoint(page, "https://ipinfo.io/region")
    city_text = _read_text_endpoint(page, "https://ipinfo.io/city")
    return {
        "ip": ip_text,
        "country": country_text,
        "region": region_text,
        "city": city_text,
    }


def _configure_page_locale(page: FirefoxPage, *, ui_locale: str) -> dict[str, Any]:
    script = """
(() => {
  const locale = __CODEX_UI_LOCALE__;
  const storageLocale = String(locale || '').replace(/-/g, '_');
  const result = { locale, storageLocale, setNavigator: false, setStorage: false };
  try {
    Object.defineProperty(navigator, 'language', { get: () => locale, configurable: true });
    Object.defineProperty(navigator, 'languages', { get: () => [locale, 'en'], configurable: true });
    result.setNavigator = true;
  } catch (err) {
    result.navigatorError = String(err);
  }
  try {
    localStorage.setItem('webLocalePreference', storageLocale);
    localStorage.setItem('webLocale', storageLocale);
    result.setStorage = true;
  } catch (err) {
    result.storageError = String(err);
  }
  return result;
})()
"""
    script = script.replace("__CODEX_UI_LOCALE__", json.dumps(ui_locale))
    payload = page.run_js(script)
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {"result": str(payload)}


def _accept_cookie_banner_if_present(page: FirefoxPage) -> bool:
    script = """
(() => {
  const labels = [
    'Accept all cookies',
    'Accept All',
    'Allow all',
    '必要なクッキーのみ',
    'すべてのCookieを受け入れる'
  ];
  const buttons = Array.from(document.querySelectorAll('button'));
  for (const button of buttons) {
    const text = String(button.textContent || '').trim();
    if (labels.includes(text)) {
      button.click();
      return { clicked: true, text };
    }
  }
  return { clicked: false };
})()
"""
    payload = page.run_js(script)
    return isinstance(payload, dict) and bool(payload.get("clicked"))


def _collect_form_state(page: FirefoxPage) -> dict[str, Any]:
    script = """
(() => {
  const bodyText = String(document.body?.innerText || '').slice(0, 1200);
  const inputs = Array.from(document.querySelectorAll('input.ds-input__input')).map((node, index) => ({
    index,
    type: node.getAttribute('type') || '',
    placeholder: node.getAttribute('placeholder') || '',
    value: node.value || ''
  }));
  const buttons = Array.from(document.querySelectorAll('button')).map((node, index) => ({
    index,
    text: String(node.textContent || '').trim(),
    className: node.className || ''
  }));
  const scriptSrcs = Array.from(document.querySelectorAll('script[src]'))
    .map(node => String(node.getAttribute('src') || ''))
    .filter(src => /hcaptcha|turnstile|cloudflare|captcha|fengkongcloud/i.test(src))
    .slice(0, 20);
  const iframeSrcs = Array.from(document.querySelectorAll('iframe[src]'))
    .map(node => String(node.getAttribute('src') || ''))
    .filter(src => /hcaptcha|turnstile|cloudflare|captcha|fengkongcloud/i.test(src))
    .slice(0, 20);
  const challengeResources = performance.getEntriesByType('resource')
    .map(entry => String(entry.name || ''))
    .filter(name => /hcaptcha|turnstile|cloudflare|captcha|fengkongcloud/i.test(name))
    .slice(0, 40);
  return {
    url: location.href,
    title: document.title || '',
    body: bodyText,
    inputs,
    buttons,
    readyState: document.readyState || '',
    hasWindowHcaptcha: typeof window.hcaptcha !== 'undefined',
    hasWindowTurnstile: typeof window.turnstile !== 'undefined',
    scriptSrcs,
    iframeSrcs,
    challengeResources
  };
})()
"""
    payload = page.run_js(script)
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {
        "url": str(page.url or ""),
        "title": str(page.title or ""),
        "body": str(payload or "")[:1200],
        "inputs": [],
        "buttons": [],
        "readyState": "",
        "hasWindowHcaptcha": False,
        "hasWindowTurnstile": False,
        "scriptSrcs": [],
        "iframeSrcs": [],
        "challengeResources": [],
    }


def _probe_deepseek_sign_up(
    page: FirefoxPage,
    *,
    ui_locale: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    sign_up_url = build_deepseek_page_url("/sign_up", ui_locale)
    first_pass_url = sign_up_url

    page.get(first_pass_url, timeout=120)
    time.sleep(5)
    locale_setup = _configure_page_locale(page, ui_locale=ui_locale)
    cookie_banner_clicked = _accept_cookie_banner_if_present(page)
    page.get(sign_up_url, timeout=120)
    time.sleep(5)

    snapshots: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(timeout_seconds, 5)
    final_state: dict[str, Any] = {}
    classification = "unknown"

    while True:
        state = _collect_form_state(page)
        classification = _classify_deepseek_sign_up_state(state)
        snapshots.append(
            {
                "ts": round(time.time(), 3),
                "classification": classification,
                "summary": _summarize_deepseek_sign_up_state(
                    state,
                    classification=classification,
                ),
                "hasWindowHcaptcha": bool(state.get("hasWindowHcaptcha")),
                "hasWindowTurnstile": bool(state.get("hasWindowTurnstile")),
                "scriptSrcs": list(state.get("scriptSrcs") or [])[:5],
                "iframeSrcs": list(state.get("iframeSrcs") or [])[:5],
                "challengeResources": list(state.get("challengeResources") or [])[:8],
            }
        )
        final_state = state
        if classification in {"email_form", "phone_only"}:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(2)

    return {
        "sign_up_url": sign_up_url,
        "initial_url": first_pass_url,
        "locale_setup": locale_setup,
        "cookie_banner_clicked": cookie_banner_clicked,
        "classification": classification,
        "summary": _summarize_deepseek_sign_up_state(
            final_state,
            classification=classification,
        ),
        "state": final_state,
        "snapshots": snapshots[-10:],
    }


def main() -> int:
    args = _parse_args()
    artifact_path = ROOT / args.artifact

    explicit_proxy = _normalize_proxy_value(args.proxy)
    if explicit_proxy:
        proxy = explicit_proxy
        proxy_source = "arg:proxy"
    else:
        proxy, proxy_source = _resolve_system_proxy()

    if not proxy:
        raise RuntimeError("No proxy resolved from --proxy or the local system proxy.")

    browser_path = get_executable_path()
    screenshot_path = artifact_path.with_suffix(".png")

    result: dict[str, Any] = {
        "ok": False,
        "browser_backend": "ruyipage",
        "browser_path": browser_path,
        "proxy": proxy,
        "proxy_source": proxy_source,
        "ui_locale": args.ui_locale,
        "headed": bool(args.headed),
        "artifact": str(artifact_path.relative_to(ROOT)),
        "screenshot": str(screenshot_path.relative_to(ROOT)),
    }

    _print_json(
        {
            "phase": "launch",
            "browser_backend": result["browser_backend"],
            "proxy": result["proxy"],
            "proxy_source": result["proxy_source"],
            "ui_locale": result["ui_locale"],
            "headed": result["headed"],
        }
    )

    page: FirefoxPage | None = None
    try:
        options = FirefoxOptions()
        options.set_browser_path(browser_path)
        options.set_proxy(proxy)
        options.set_pref("intl.accept_languages", f"{args.ui_locale},en")
        options.set_pref("javascript.use_us_english_locale", True)
        options.set_window_size(1440, 1080)
        options.headless(not args.headed)
        page = FirefoxPage(options)

        egress = _probe_egress(page)
        result["egress"] = egress
        _print_json(
            {
                "phase": "egress",
                "ip": _mask_ip(str(egress.get("ip") or "")),
                "country": str(egress.get("country") or ""),
                "region": str(egress.get("region") or ""),
                "city": str(egress.get("city") or ""),
            }
        )

        deepseek = _probe_deepseek_sign_up(
            page,
            ui_locale=args.ui_locale,
            timeout_seconds=args.timeout_seconds,
        )
        page.screenshot(path=str(screenshot_path), full_page=True)

        result["deepseek"] = deepseek
        result["ok"] = True
        _print_json(
            {
                "phase": "deepseek",
                "classification": str(deepseek.get("classification") or ""),
                "summary": str(deepseek.get("summary") or ""),
                "hasWindowHcaptcha": bool(
                    ((deepseek.get("state") or {})).get("hasWindowHcaptcha")
                ),
                "hasWindowTurnstile": bool(
                    ((deepseek.get("state") or {})).get("hasWindowTurnstile")
                ),
            }
        )
        return 0
    except Exception as exc:
        result["error"] = str(exc)
        raise
    finally:
        _write_artifact(artifact_path, result)
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
