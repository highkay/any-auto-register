#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ruyipage import FirefoxOptions, FirefoxPage  # noqa: E402
from ruyipage._runtime.resolver import get_executable_path  # noqa: E402

from core.base_mailbox import create_mailbox  # noqa: E402
from core.config_store import config_store  # noqa: E402
from platforms.deepseek.core import (  # noqa: E402
    _POW_SOLVE_EVAL,
    DEEPSEEK_APP_VERSION,
    DEEPSEEK_CLIENT_VERSION,
    DEEPSEEK_DEFAULT_POW_WORKER_URL,
    DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS,
    DEEPSEEK_POW_WORKER_HOST_PAGE_URL,
    _classify_deepseek_sign_up_state,
    _extract_deepseek_guest_challenge,
    _summarize_deepseek_sign_up_state,
    build_deepseek_page_url,
    extract_deepseek_client_locale,
    random_password,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe the DeepSeek send-code path with ruyipage."
    )
    parser.add_argument("--proxy", default="socks5://192.168.1.18:1080")
    parser.add_argument("--ui-locale", default="en-US")
    parser.add_argument("--tz-offset-seconds", default=DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS)
    parser.add_argument("--mail-provider", default="")
    parser.add_argument("--mail-domain", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--send-timeout-seconds", type=int, default=18)
    parser.add_argument("--mailbox-timeout-seconds", type=int, default=120)
    parser.add_argument(
        "--artifact",
        default="docs/artifacts/deepseek-ruyipage-send-code-probe.json",
    )
    return parser.parse_args()


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
            elif any(marker in lowered for marker in ("token", "captcha", "pow", "authorization")):
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
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "..."
    return value


def _build_extra(args: argparse.Namespace) -> dict[str, Any]:
    extra = config_store.get_all().copy()
    if str(args.mail_provider or "").strip():
        extra["mail_provider"] = str(args.mail_provider).strip()
    extra["deepseek_ui_locale"] = args.ui_locale
    extra["deepseek_tz_offset_seconds"] = str(args.tz_offset_seconds or "").strip()
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
    return {
        "ip": _read_text_endpoint(page, "https://api.ipify.org"),
        "country": _read_text_endpoint(page, "https://ipinfo.io/country"),
        "region": _read_text_endpoint(page, "https://ipinfo.io/region"),
        "city": _read_text_endpoint(page, "https://ipinfo.io/city"),
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
  const bodyText = String(document.body?.innerText || '').slice(0, 1600);
  const inputs = Array.from(document.querySelectorAll('input.ds-input__input')).map((node, index) => ({
    index,
    type: node.getAttribute('type') || '',
    placeholder: node.getAttribute('placeholder') || '',
    value: node.value || ''
  }));
  const buttons = Array.from(document.querySelectorAll('button')).map((node, index) => ({
    index,
    text: String(node.textContent || '').trim(),
    className: node.className || '',
    disabled: Boolean(node.disabled)
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
        "title": "",
        "body": str(payload or "")[:1600],
        "inputs": [],
        "buttons": [],
        "readyState": "",
        "hasWindowHcaptcha": False,
        "hasWindowTurnstile": False,
        "scriptSrcs": [],
        "iframeSrcs": [],
        "challengeResources": [],
    }


def _wait_for_email_form(
    page: FirefoxPage,
    *,
    ui_locale: str,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    sign_up_url = build_deepseek_page_url("/sign_up", ui_locale)
    page.get(sign_up_url, timeout=120)
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

    if classification != "email_form":
        raise RuntimeError(
            "DeepSeek 注册页未进入邮箱表单: "
            + _summarize_deepseek_sign_up_state(
                final_state,
                classification=classification,
            )
        )

    return {
        "sign_up_url": sign_up_url,
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


def _install_send_code_hooks(page: FirefoxPage) -> dict[str, Any]:
    script = """
(() => {
  if (window.__codexSendCodeHooksInstalled) {
    return { installed: true, alreadyInstalled: true };
  }
  const TARGET = '/api/v0/users/create_email_verification_code';
  const originalFetch = window.fetch ? window.fetch.bind(window) : null;
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

  const toHeaderMap = (headers) => {
    const out = {};
    if (!headers) return out;
    try {
      if (headers instanceof Headers) {
        headers.forEach((value, key) => { out[String(key)] = String(value); });
        return out;
      }
      if (Array.isArray(headers)) {
        for (const item of headers) {
          if (Array.isArray(item) && item.length >= 2) {
            out[String(item[0])] = String(item[1]);
          }
        }
        return out;
      }
      for (const [key, value] of Object.entries(headers)) {
        out[String(key)] = String(value);
      }
    } catch (_) {}
    return out;
  };

  const pushLog = (entry) => {
    const logs = window.__codexSendCodeLog || [];
    logs.push({ ts: Date.now(), ...entry });
    if (logs.length > 30) {
      logs.splice(0, logs.length - 30);
    }
    window.__codexSendCodeLog = logs;
  };

  const normalizeUrl = (value) => {
    try {
      return String(new URL(String(value || ''), location.href));
    } catch (_) {
      return String(value || '');
    }
  };

  window.__codexSendCodeHooksInstalled = true;
  window.__codexSendCodeLog = [];

  if (originalFetch) {
    window.fetch = async (...args) => {
      const input = args[0];
      const init = args[1] || {};
      const url = normalizeUrl(input && input.url ? input.url : input);
      const method = String(
        init.method ||
        (input && input.method) ||
        'GET'
      ).toUpperCase();
      const isTarget = url.includes(TARGET);
      if (isTarget) {
        pushLog({
          kind: 'request',
          transport: 'fetch',
          url,
          method,
          headers: toHeaderMap(init.headers),
          body: typeof init.body === 'string' ? init.body : '',
        });
      }
      const response = await originalFetch(...args);
      if (isTarget) {
        let text = '';
        try {
          text = await response.clone().text();
        } catch (err) {
          text = `<clone_failed ${String(err)}>`;
        }
        pushLog({
          kind: 'response',
          transport: 'fetch',
          url,
          status: Number(response.status || 0),
          body: text,
        });
      }
      return response;
    };
  }

  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__codexUrl = normalizeUrl(url);
    this.__codexMethod = String(method || 'GET').toUpperCase();
    this.__codexHeaders = {};
    return originalOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    try {
      this.__codexHeaders[String(name)] = String(value);
    } catch (_) {}
    return originalSetRequestHeader.call(this, name, value);
  };

  XMLHttpRequest.prototype.send = function(body) {
    const url = String(this.__codexUrl || '');
    const isTarget = url.includes(TARGET);
    if (isTarget) {
      pushLog({
        kind: 'request',
        transport: 'xhr',
        url,
        method: String(this.__codexMethod || 'GET'),
        headers: this.__codexHeaders || {},
        body: typeof body === 'string' ? body : '',
      });
      this.addEventListener('loadend', () => {
        pushLog({
          kind: 'response',
          transport: 'xhr',
          url,
          status: Number(this.status || 0),
          body: String(this.responseText || ''),
        });
      });
    }
    return originalSend.call(this, body);
  };

  return { installed: true, alreadyInstalled: false };
})()
"""
    payload = page.run_js(script)
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {"result": str(payload)}


def _read_send_code_hooks(page: FirefoxPage) -> list[dict[str, Any]]:
    payload = page.run_js("window.__codexSendCodeLog || []")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _request_guest_challenge(
    page: FirefoxPage,
    *,
    target_path: str,
    ui_locale: str,
    tz_offset_seconds: str,
) -> dict[str, Any]:
    client_locale = extract_deepseek_client_locale(ui_locale)
    script = f"""
(async () => {{
  const response = await fetch('/api/v0/users/create_guest_challenge', {{
    method: 'POST',
    credentials: 'include',
    headers: {{
      'accept': '*/*',
      'content-type': 'application/json',
      'x-app-version': {json.dumps(DEEPSEEK_APP_VERSION)},
      'x-client-locale': {json.dumps(client_locale)},
      'x-client-platform': 'web',
      'x-client-timezone-offset': {json.dumps(str(tz_offset_seconds or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS))},
      'x-client-version': {json.dumps(DEEPSEEK_CLIENT_VERSION)}
    }},
    body: JSON.stringify({{ target_path: {json.dumps(target_path)} }})
  }});
  const text = await response.text();
  try {{
    return JSON.parse(text);
  }} catch (_) {{
    return {{ status: response.status, body: text }};
  }}
}})()
"""
    payload = page.run_js(script, timeout=45)
    if not isinstance(payload, dict):
        raise RuntimeError(f"DeepSeek guest challenge 响应异常: {payload!r}")
    return payload


def _encode_guest_pow_response(page: FirefoxPage, challenge: dict[str, Any], *, pow_worker_url: str) -> str:
    if not challenge:
        raise RuntimeError("DeepSeek guest challenge 为空")
    pow_tab = None
    pow_payload = {"challenge": challenge, "workerUrl": pow_worker_url}
    script = (
        "(async () => {"
        f"  const solve = {_POW_SOLVE_EVAL};"
        f"  return await solve({json.dumps(pow_payload, ensure_ascii=False)});"
        "})()"
    )
    try:
        pow_tab = page.new_tab("about:blank")
        pow_tab.get(DEEPSEEK_POW_WORKER_HOST_PAGE_URL, timeout=60)
        answer = pow_tab.run_js(script, timeout=45)
    finally:
        if pow_tab is not None:
            try:
                pow_tab.close()
            except Exception:
                pass
    if not isinstance(answer, dict):
        raise RuntimeError(f"DeepSeek PoW 返回异常: {answer!r}")
    salt = str(answer.get("salt") or challenge.get("salt") or "").strip()
    raw_answer = answer.get("answer")
    if not salt:
        raise RuntimeError(f"DeepSeek PoW 返回缺少 salt: {answer}")
    if raw_answer is None:
        raise RuntimeError(f"DeepSeek PoW 返回缺少 answer: {answer}")
    body = json.dumps({"salt": salt, "answer": int(raw_answer)}, separators=(",", ":"))
    return base64.b64encode(body.encode("utf-8")).decode("ascii")


def _fill_form(page: FirefoxPage, *, email: str, password: str) -> dict[str, Any]:
    script = f"""
(() => {{
  const setValue = (node, value) => {{
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setter) {{
      setter.call(node, value);
    }} else {{
      node.value = value;
    }}
    node.dispatchEvent(new Event('input', {{ bubbles: true }}));
    node.dispatchEvent(new Event('change', {{ bubbles: true }}));
    node.dispatchEvent(new Event('blur', {{ bubbles: true }}));
  }};
  const inputs = Array.from(document.querySelectorAll('input.ds-input__input'));
  const emailInput = inputs.find(
    (node) => ['text', 'email'].includes(String(node.getAttribute('type') || '').toLowerCase())
  );
  const passwordInputs = inputs.filter((node) => String(node.getAttribute('type') || '').toLowerCase() === 'password');
  const button = document.querySelector('button.ds-verify-code-input-countdown');
  if (!emailInput) {{
    return {{ ok: false, error: 'email_input_not_found' }};
  }}
  if (passwordInputs.length < 2) {{
    return {{ ok: false, error: 'password_inputs_not_found', passwordCount: passwordInputs.length }};
  }}
  setValue(emailInput, {json.dumps(email)});
  setValue(passwordInputs[0], {json.dumps(password)});
  setValue(passwordInputs[1], {json.dumps(password)});
  return {{
    ok: true,
    emailValue: emailInput.value || '',
    passwordValueLength: (passwordInputs[0].value || '').length,
    confirmValueLength: (passwordInputs[1].value || '').length,
    buttonText: String(button?.textContent || '').trim(),
    buttonDisabled: Boolean(button?.disabled),
    buttonClassName: String(button?.className || ''),
  }};
}})()
"""
    payload = page.run_js(script)
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {"ok": False, "result": str(payload)}


def _click_send_code(page: FirefoxPage) -> dict[str, Any]:
    script = """
(() => {
  const button = document.querySelector('button.ds-verify-code-input-countdown');
  if (!button) {
    return { clicked: false, error: 'send_code_button_not_found' };
  }
  button.scrollIntoView({ block: 'center', inline: 'center' });
  const payload = {
    text: String(button.textContent || '').trim(),
    disabled: Boolean(button.disabled),
    className: String(button.className || ''),
  };
  if (button.disabled) {
    return { clicked: false, ...payload, error: 'send_code_button_disabled' };
  }
  button.click();
  return { clicked: true, ...payload };
})()
"""
    payload = page.run_js(script)
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {"clicked": False, "result": str(payload)}


def _collect_send_code_page_state(page: FirefoxPage) -> dict[str, Any]:
    script = """
(() => {
  const button = document.querySelector('button.ds-verify-code-input-countdown');
  const bodyText = String(document.body?.innerText || '').slice(0, 1600);
  return {
    buttonText: String(button?.textContent || '').trim(),
    buttonDisabled: Boolean(button?.disabled),
    buttonClassName: String(button?.className || ''),
    bodyText,
    hasResendCountdown: /resend\\s+after\\s+\\d+\\s*s/i.test(bodyText + '\\n' + String(button?.textContent || '')),
    hasWindowHcaptcha: typeof window.hcaptcha !== 'undefined',
    hasWindowTurnstile: typeof window.turnstile !== 'undefined',
    challengeResources: performance.getEntriesByType('resource')
      .map(entry => String(entry.name || ''))
      .filter(name => /hcaptcha|turnstile|cloudflare|captcha|fengkongcloud/i.test(name))
      .slice(0, 40),
    iframeSrcs: Array.from(document.querySelectorAll('iframe[src]'))
      .map(node => String(node.getAttribute('src') || ''))
      .filter(src => /hcaptcha|turnstile|cloudflare|captcha|fengkongcloud/i.test(src))
      .slice(0, 20),
  };
})()
"""
    payload = page.run_js(script)
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()}
    return {"result": str(payload)}


def _serialize_packet(packet: Any) -> dict[str, Any]:
    response_text = ""
    try:
        response_text = str(packet.text or "")
    except Exception as exc:
        response_text = f"<read_failed {exc}>"
    return _sanitize(
        {
            "url": str(getattr(packet, "url", "") or ""),
            "method": str(getattr(packet, "method", "") or ""),
            "status": int(getattr(packet, "status", 0) or 0),
            "event_type": str(getattr(packet, "event_type", "") or ""),
            "is_failed": bool(getattr(packet, "is_failed", False)),
            "headers": dict(getattr(packet, "headers", {}) or {}),
            "request": getattr(packet, "request", {}) or {},
            "response": getattr(packet, "response", {}) or {},
            "text": response_text[:2000],
        }
    )


def _parse_send_code_json(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    args = _parse_args()
    artifact_path = ROOT / args.artifact
    screenshot_before = artifact_path.with_name(artifact_path.stem + "-before.png")
    screenshot_after = artifact_path.with_name(artifact_path.stem + "-after.png")
    extra = _build_extra(args)
    proxy = str(args.proxy or "").strip()
    password = str(args.password or "").strip() or random_password()
    pow_worker_url = str(extra.get("deepseek_pow_worker_url") or DEEPSEEK_DEFAULT_POW_WORKER_URL).strip()

    mailbox = create_mailbox(
        provider=str(extra.get("mail_provider") or "outlookemail").strip() or "outlookemail",
        extra=extra,
        proxy=proxy or None,
        platform="deepseek",
    )
    mail_account = mailbox.get_email()
    email = str(getattr(mail_account, "email", "") or "").strip()
    if not email:
        raise RuntimeError("未获取到可用邮箱")
    before_ids = mailbox.get_current_ids(mail_account)

    result: dict[str, Any] = {
        "ok": False,
        "browser_backend": "ruyipage",
        "browser_path": get_executable_path(),
        "proxy": proxy,
        "ui_locale": args.ui_locale,
        "headed": bool(args.headed),
        "mail_provider": str(extra.get("mail_provider") or ""),
        "email": _mask_email(email),
        "artifact": str(artifact_path.relative_to(ROOT)),
        "before_screenshot": str(screenshot_before.relative_to(ROOT)),
        "after_screenshot": str(screenshot_after.relative_to(ROOT)),
        "send_code_success": False,
    }

    _print_json(
        {
            "phase": "launch",
            "browser_backend": result["browser_backend"],
            "proxy": result["proxy"],
            "ui_locale": result["ui_locale"],
            "headed": result["headed"],
            "email": result["email"],
            "mail_provider": result["mail_provider"],
        }
    )

    page: FirefoxPage | None = None
    try:
        options = FirefoxOptions()
        options.set_browser_path(result["browser_path"])
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

        sign_up = _wait_for_email_form(
            page,
            ui_locale=args.ui_locale,
        )
        result["sign_up"] = _sanitize(sign_up)
        _print_json(
            {
                "phase": "sign_up",
                "classification": str(sign_up.get("classification") or ""),
                "summary": str(sign_up.get("summary") or ""),
                "hasWindowTurnstile": bool(((sign_up.get("state") or {})).get("hasWindowTurnstile")),
                "hasWindowHcaptcha": bool(((sign_up.get("state") or {})).get("hasWindowHcaptcha")),
            }
        )

        hook_status = _install_send_code_hooks(page)
        result["send_code_hooks"] = _sanitize(hook_status)

        fill_state = _fill_form(page, email=email, password=password)
        time.sleep(1.5)
        before_state = _collect_form_state(page)
        page.screenshot(path=str(screenshot_before), full_page=True)
        result["fill_state"] = _sanitize(fill_state)
        result["before_state"] = _sanitize(before_state)

        _print_json(
            {
                "phase": "fill",
                "buttonDisabled": bool(fill_state.get("buttonDisabled")),
                "buttonText": str(fill_state.get("buttonText") or ""),
            }
        )

        guest_challenge_response = _request_guest_challenge(
            page,
            target_path="/api/v0/users/create_email_verification_code",
            ui_locale=args.ui_locale,
            tz_offset_seconds=str(args.tz_offset_seconds or DEEPSEEK_DEFAULT_TZ_OFFSET_SECONDS),
        )
        guest_challenge = _extract_deepseek_guest_challenge(guest_challenge_response)
        guest_pow_response = _encode_guest_pow_response(
            page,
            guest_challenge,
            pow_worker_url=pow_worker_url,
        )
        page.network.set_extra_headers(
            {
                "x-ds-guest-pow-response": guest_pow_response,
            }
        )
        result["guest_challenge"] = _sanitize(guest_challenge_response)
        result["guest_pow"] = {
            "present": bool(guest_pow_response),
            "length": len(guest_pow_response),
            "prefix": guest_pow_response[:24],
        }
        _print_json(
            {
                "phase": "pow",
                "guest_challenge_ok": True,
                "guest_pow_length": len(guest_pow_response),
            }
        )

        page.listen.start(
            [
                "/api/v0/users/create_email_verification_code",
                "challenges.cloudflare.com/turnstile",
                "js.hcaptcha.com",
                "captcha",
                "fengkongcloud",
            ],
            method=None,
            collect_response=True,
        )
        click_state = _click_send_code(page)
        result["click_state"] = _sanitize(click_state)
        _print_json(
            {
                "phase": "click",
                "clicked": bool(click_state.get("clicked")),
                "disabled": bool(click_state.get("disabled")),
                "text": str(click_state.get("text") or ""),
                "error": str(click_state.get("error") or ""),
            }
        )

        time.sleep(2)
        packets = page.listen.wait(
            timeout=max(float(args.send_timeout_seconds), 3.0),
            count=8,
        )
        packet_list = packets if isinstance(packets, list) else ([packets] if packets else [])
        relevant_packets = [_serialize_packet(packet) for packet in packet_list if packet is not None]
        result["network_packets"] = relevant_packets

        hook_logs = _sanitize(_read_send_code_hooks(page))
        result["send_code_hook_logs"] = hook_logs

        send_packet = next(
            (
                item
                for item in relevant_packets
                if "/api/v0/users/create_email_verification_code" in str(item.get("url") or "")
            ),
            None,
        )
        send_response_text = str((send_packet or {}).get("text") or "")
        send_response_json = _parse_send_code_json(send_response_text)
        if send_packet is not None:
            result["send_code_response_meta"] = {
                "status": send_packet.get("status"),
                "event_type": send_packet.get("event_type"),
                "is_failed": send_packet.get("is_failed"),
            }
        if send_response_json is not None:
            result["send_code_response"] = _sanitize(send_response_json)
            inner = send_response_json.get("data", {})
            result["send_code_success"] = inner.get("biz_code") in (0, "0")
        else:
            result["send_code_response"] = send_response_text[:2000]
            result["send_code_success"] = False

        time.sleep(4)
        after_state = _collect_send_code_page_state(page)
        after_form_state = _collect_form_state(page)
        page.screenshot(path=str(screenshot_after), full_page=True)
        result["after_state"] = _sanitize(after_state)
        result["after_form_state"] = _sanitize(after_form_state)
        result["ok"] = True

        _print_json(
            {
                "phase": "after",
                "send_code_success": bool(result.get("send_code_success")),
                "buttonText": str(after_state.get("buttonText") or ""),
                "hasResendCountdown": bool(after_state.get("hasResendCountdown")),
                "hasWindowTurnstile": bool(after_state.get("hasWindowTurnstile")),
                "hasWindowHcaptcha": bool(after_state.get("hasWindowHcaptcha")),
                "packetCount": len(relevant_packets),
            }
        )

        if result["send_code_success"]:
            code = mailbox.wait_for_code(
                mail_account,
                keyword="DeepSeek",
                timeout=max(args.mailbox_timeout_seconds, 30),
                before_ids=before_ids,
                otp_sent_at=time.time(),
            )
            result["mailbox_code"] = code
            _print_json(
                {
                    "phase": "mailbox",
                    "received": True,
                    "code": code,
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
                page.network.clear_extra_headers()
            except Exception:
                pass
            try:
                page.quit()
            except Exception:
                pass
        if not result.get("send_code_success"):
            release_current = getattr(mailbox, "release_current_account", None)
            if callable(release_current):
                try:
                    release_current()
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
