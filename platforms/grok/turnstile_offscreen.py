"""Browser-native Turnstile mint for Grok protocol registration.

No third-party captcha API required. Strategies (in order):

1. Origin sandbox: fulfill a minimal HTML document on accounts.x.ai (correct
   sitekey host) **without** the production CSP that blocks injected api.js.
2. Live page: open the real sign-up page with clearance cookies, CDP CSP bypass,
   inject/render widget, click if needed.

Aligned with Charles-0509/Grok-Register turnstile_mint.py and the project's
original same-browser Turnstile handling — not YesCaptcha.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from core.browser_runtime import with_chrome_executable
from core.proxy_utils import build_playwright_proxy_config

DEFAULT_PAGE_URL = "https://accounts.x.ai/sign-up"
DEFAULT_SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
TOKEN_MIN_LENGTH = 20
TURNSTILE_API = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"

_TURNSTILE_MOUSE_PATCH = r"""
(() => {
  function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }
  const screenX = getRandomInt(800, 1200);
  const screenY = getRandomInt(400, 600);
  try {
    Object.defineProperty(MouseEvent.prototype, 'screenX', {
      configurable: true, value: screenX,
    });
  } catch (_) {}
  try {
    Object.defineProperty(MouseEvent.prototype, 'screenY', {
      configurable: true, value: screenY,
    });
  } catch (_) {}
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  } catch (_) {}
  window.chrome = window.chrome || { runtime: {} };
})();
"""


def _has_display() -> bool:
    if os.name == "nt":
        return True
    return bool(
        (os.environ.get("DISPLAY") or "").strip()
        or (os.environ.get("WAYLAND_DISPLAY") or "").strip()
    )


def resolve_launch_mode(mode: str = "offscreen") -> tuple[str, bool]:
    """Return (label, headless).

    Labels:
      - headed: on-screen Chrome (best for managed Turnstile / manual click)
      - offscreen: headed but moved off-screen
      - headless / headless-no-display: true headless
    """
    mode = (mode or "offscreen").strip().lower()
    if mode in {"", "auto"}:
        mode = "offscreen"
    if mode in {"headed", "visible", "manual", "on-screen", "onscreen"}:
        if _has_display():
            return "headed", False
        return "headless-no-display", True
    if mode == "headless":
        return "headless", True
    if mode == "offscreen":
        if _has_display():
            return "offscreen", False
        return "headless-no-display", True
    # Unknown → prefer visible when possible
    if _has_display():
        return "offscreen", False
    return "headless-no-display", True


def launch_args(label: str) -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
        "--disable-dev-shm-usage",
        "--lang=en-US",
    ]
    if label == "headed":
        # On-screen: managed Turnstile often fails when parked at -2400.
        args.extend(
            [
                "--window-position=80,60",
                "--window-size=1100,800",
            ]
        )
    elif label == "offscreen":
        # Visible-size headed window moved off-screen (true headless often gets 600010).
        args.extend(
            [
                "--window-position=-2400,-2400",
                "--window-size=900,700",
            ]
        )
    return args


def _sandbox_html(site_key: str) -> str:
    """Minimal page on accounts.x.ai origin with explicit Turnstile render.

    Note: route.fulfill often skips *inline* script execution under Patchright.
    Keep a data-sitekey widget + external api.js so Turnstile can still boot;
    Python-side evaluate re-drives render after navigation.
    """
    key = site_key.replace("\\", "\\\\").replace("'", "\\'").replace('"', "&quot;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Grok Turnstile Mint</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; background: #111; color: #eee; }}
    #wrap {{ padding: 24px; }}
    #cf-box {{
      background: #fff; padding: 12px; border-radius: 8px;
      width: 320px; min-height: 70px;
    }}
  </style>
  <script src="{TURNSTILE_API}" async defer></script>
</head>
<body>
  <div id="wrap">
    <div id="cf-box" class="cf-turnstile" data-sitekey="{key}"></div>
    <input type="hidden" name="cf-turnstile-response" id="cf-response" value=""/>
  </div>
</body>
</html>
"""


def mint_turnstile_token(
    site_key: str = DEFAULT_SITE_KEY,
    *,
    page_url: str = DEFAULT_PAGE_URL,
    proxy: Optional[str] = None,
    cookies: Optional[list[dict[str, Any]]] = None,
    user_agent: str = "",
    timeout: float = 90.0,
    mode: str = "offscreen",
    log_fn: Callable[[str], None] = print,
    task_control=None,
) -> str:
    """Mint a Cloudflare Turnstile token via Patchright (no captcha API)."""
    from patchright.sync_api import sync_playwright

    key = str(site_key or DEFAULT_SITE_KEY).strip() or DEFAULT_SITE_KEY
    url = str(page_url or DEFAULT_PAGE_URL).strip() or DEFAULT_PAGE_URL
    label, use_headless = resolve_launch_mode(mode)
    log_fn(f"Turnstile mint: mode={label} headless={use_headless} sitekey={key[:18]}...")

    launch_kwargs: dict[str, Any] = with_chrome_executable(
        {
            "headless": use_headless,
            "args": launch_args(label),
        },
        channel="chrome" if os.name == "nt" else None,
    )
    # Drop explicit None channel so Playwright does not reject the launch kwargs.
    if launch_kwargs.get("channel") is None:
        launch_kwargs.pop("channel", None)

    proxy_cfg = build_playwright_proxy_config(proxy) if proxy else None
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg

    playwright = browser = context = None
    try:
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(**launch_kwargs)
        except Exception:
            fallback = dict(launch_kwargs)
            fallback.pop("channel", None)
            fallback.pop("executable_path", None)
            browser = playwright.chromium.launch(
                **with_chrome_executable(fallback)
            )

        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": 900, "height": 700},
            "locale": "en-US",
        }
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent
        context = browser.new_context(**ctx_kwargs)
        context.add_init_script(_TURNSTILE_MOUSE_PATCH)

        for cookie in cookies or []:
            try:
                payload = {
                    "name": str(cookie.get("name") or ""),
                    "value": str(cookie.get("value") or ""),
                    "domain": str(cookie.get("domain") or ".x.ai"),
                    "path": str(cookie.get("path") or "/"),
                }
                if not payload["name"]:
                    continue
                context.add_cookies([payload])
            except Exception:
                try:
                    context.add_cookies(
                        [
                            {
                                "name": str(cookie.get("name") or ""),
                                "value": str(cookie.get("value") or ""),
                                "url": "https://accounts.x.ai/",
                                "path": "/",
                            }
                        ]
                    )
                except Exception:
                    pass

        page = context.new_page()

        def _checkpoint() -> None:
            if task_control is not None:
                task_control.checkpoint()

        def _read_token() -> str:
            try:
                token = page.evaluate(
                    """() => {
                      const a = document.querySelector('input[name="cf-turnstile-response"]')
                        || document.querySelector('#cf-response')
                        || document.querySelector('textarea[name="cf-turnstile-response"]');
                      if (a && a.value && a.value.length > 10) return a.value;
                      if (window.__grokTsToken && String(window.__grokTsToken).length > 10) {
                        return String(window.__grokTsToken);
                      }
                      try {
                        if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
                          const r = window.turnstile.getResponse();
                          if (r) return String(r);
                        }
                      } catch (_) {}
                      return '';
                    }"""
                )
                return str(token or "").strip()
            except Exception:
                return ""

        def _diag() -> dict[str, Any]:
            try:
                return page.evaluate(
                    """() => {
                      const ifr = [...document.querySelectorAll('iframe')].filter(f => {
                        const s = f.src || '';
                        return s.includes('turnstile') || s.includes('challenges.cloudflare.com');
                      }).length;
                      return {
                        title: document.title || '',
                        href: location.href || '',
                        status: window.__grokTsStatus || '',
                        turnstile: !!(window.turnstile && window.turnstile.render),
                        iframes: ifr,
                        all_ifr: document.querySelectorAll('iframe').length,
                        widget: !!document.querySelector('.cf-turnstile,[data-sitekey],#cf-box'),
                        tokLen: (document.querySelector('input[name="cf-turnstile-response"]')||{}).value
                          ? document.querySelector('input[name="cf-turnstile-response"]').value.length
                          : 0,
                      };
                    }"""
                )
            except Exception as exc:
                return {"error": str(exc)}

        def _click_widget() -> None:
            box = page.evaluate(
                """() => {
                  const e = document.querySelector(
                    '#cf-box, .cf-turnstile, iframe[src*="turnstile"], iframe[src*="challenges.cloudflare.com"]'
                  );
                  if (!e) return null;
                  const r = e.getBoundingClientRect();
                  if (!r.width || !r.height) return null;
                  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                }"""
            )
            if not box:
                return
            x, y = float(box["x"]), float(box["y"])
            page.mouse.move(max(0, x - 30), max(0, y - 10))
            page.mouse.move(x, y, steps=10)
            page.mouse.down()
            time.sleep(0.05)
            page.mouse.up()

        def _wait_token(deadline: float, *, clicks: bool = True) -> str:
            attempt = 0
            while time.monotonic() < deadline:
                _checkpoint()
                token = _read_token()
                if len(token) >= TOKEN_MIN_LENGTH:
                    return token
                if clicks and attempt in (3, 8, 15, 25, 40):
                    try:
                        _click_widget()
                    except Exception:
                        pass
                page.wait_for_timeout(400)
                attempt += 1
            return _read_token()

        def _enable_cdp_csp_bypass() -> None:
            try:
                cdp = context.new_cdp_session(page)
                cdp.send("Page.setBypassCSP", {"enabled": True})
                log_fn("Turnstile: CDP Page.setBypassCSP=on")
            except Exception as exc:
                log_fn(f"Turnstile: CDP CSP bypass skip: {exc}")

        # ---------- Strategy 1: route.fulfill origin sandbox ----------
        # set_content on the real SPA often no-ops; fulfill the document instead.
        # Verified: direct egress can mint tokens after ~10-15s; proxy may fail.
        deadline = time.monotonic() + max(float(timeout or 90), 20.0)
        log_fn("Turnstile strategy=origin_sandbox_fulfill")
        sandbox_body = _sandbox_html(key)
        origin_url = (
            url
            if "accounts.x.ai" in (url or "")
            else "https://accounts.x.ai/sign-up?redirect=grok-com"
        )
        try:
            def _fulfill_signup(route):
                req = route.request
                if req.resource_type == "document" and "accounts.x.ai" in (req.url or ""):
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=sandbox_body,
                    )
                else:
                    route.continue_()

            page.route("https://accounts.x.ai/**", _fulfill_signup)
            _enable_cdp_csp_bypass()
            page.goto(origin_url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(600)
            _enable_cdp_csp_bypass()
            try:
                origin_now = page.evaluate("() => location.origin || ''")
                title_now = page.evaluate("() => document.title || ''")
                log_fn(f"Turnstile sandbox origin={origin_now} title={title_now}")
            except Exception:
                pass

            # Drive render from Python (inline scripts in fulfilled HTML may not run).
            try:
                status = page.evaluate(
                    f"""async () => {{
                      window.__grokTsStatus = window.__grokTsStatus || 'py_boot';
                      function setTok(t) {{
                        let i = document.querySelector('input[name="cf-turnstile-response"]')
                          || document.getElementById('cf-response');
                        if (!i) {{
                          i = document.createElement('input');
                          i.type = 'hidden';
                          i.name = 'cf-turnstile-response';
                          i.id = 'cf-response';
                          document.body.appendChild(i);
                        }}
                        i.value = t || '';
                        window.__grokTsToken = t || '';
                        window.__grokTsStatus = 'token';
                      }}
                      window.__grokSetToken = setTok;
                      function tryRender() {{
                        if (!(window.turnstile && window.turnstile.render)) return 'no_api';
                        try {{
                          const host = document.getElementById('cf-box')
                            || document.querySelector('.cf-turnstile');
                          if (!host) return 'no_host';
                          // Avoid double-render if widget already present.
                          if (host.querySelector('iframe')) return 'has_iframe';
                          window.turnstile.render(host, {{
                            sitekey: {key!r},
                            callback: setTok,
                            'error-callback': function(c) {{
                              window.__grokTsStatus = 'error:' + c;
                            }},
                          }});
                          window.__grokTsStatus = 'rendered_py';
                          return 'rendered';
                        }} catch (e) {{
                          return 'render_err:' + (e && e.message ? e.message : e);
                        }}
                      }}
                      let r = tryRender();
                      if (r !== 'no_api') return r;
                      await new Promise((resolve) => {{
                        const s = document.createElement('script');
                        s.src = {TURNSTILE_API!r};
                        s.async = true;
                        s.onload = () => resolve('loaded');
                        s.onerror = () => resolve('script_error');
                        document.head.appendChild(s);
                        setTimeout(() => resolve('timeout_load'), 12000);
                      }});
                      r = tryRender();
                      if (r === 'no_api') window.__grokTsStatus = 'no_api_after_load';
                      return r;
                    }}"""
                )
                log_fn(f"Turnstile sandbox boot={status} diag={_diag()}")
            except Exception as exc:
                log_fn(f"Turnstile sandbox boot err: {exc} diag={_diag()}")

            # Direct path may need 10-20s for managed challenge.
            token = _wait_token(min(deadline, time.monotonic() + 55.0), clicks=True)
            if len(token) >= TOKEN_MIN_LENGTH:
                log_fn(f"Turnstile mint 成功(sandbox) len={len(token)}")
                try:
                    page.unroute("https://accounts.x.ai/**")
                except Exception:
                    pass
                return token
            log_fn(f"Turnstile sandbox 未出 token diag={_diag()}")
            try:
                page.unroute("https://accounts.x.ai/**")
            except Exception:
                pass
        except Exception as exc:
            log_fn(f"Turnstile sandbox 失败: {exc}")
            try:
                page.unroute("https://accounts.x.ai/**")
            except Exception:
                pass

        # ---------- Strategy 2: live sign-up page + inject ----------
        if time.monotonic() >= deadline:
            raise RuntimeError("turnstile timeout (sandbox)")

        log_fn("Turnstile strategy=live_page_inject")
        _enable_cdp_csp_bypass()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Early native token?
        token = _read_token()
        if len(token) >= TOKEN_MIN_LENGTH:
            log_fn(f"Turnstile mint 成功(native) len={len(token)}")
            return token

        # Inject widget + explicit API (reference mint script style)
        page.evaluate(
            f"""() => {{
              const key = {key!r};
              let host = document.querySelector('.cf-turnstile,[data-sitekey]');
              if (!host) {{
                host = document.createElement('div');
                host.className = 'cf-turnstile';
                host.id = 'cf-box';
                host.setAttribute('data-sitekey', key);
                host.style.cssText = 'position:fixed;top:16px;left:16px;z-index:99999;'
                  + 'background:#fff;padding:12px;width:320px;min-height:70px;'
                  + 'border:1px solid #ccc;border-radius:8px;';
                document.body.appendChild(host);
              }}
              if (!document.querySelector('input[name="cf-turnstile-response"]')) {{
                const i = document.createElement('input');
                i.type = 'hidden';
                i.name = 'cf-turnstile-response';
                i.id = 'cf-response';
                document.body.appendChild(i);
              }}
              window.__grokSetToken = function(t) {{
                const i = document.querySelector('input[name="cf-turnstile-response"]');
                if (i) i.value = t || '';
                window.__grokTsToken = t || '';
                window.__grokTsStatus = 'token';
              }};
              window.__grokRender = function() {{
                if (!window.turnstile || !window.turnstile.render) {{
                  window.__grokTsStatus = 'no_api';
                  return;
                }}
                try {{
                  window.turnstile.render(host, {{
                    sitekey: key,
                    callback: window.__grokSetToken,
                  }});
                  window.__grokTsStatus = 'rendered';
                }} catch (e) {{
                  window.__grokTsStatus = 'render_err:' + (e && e.message ? e.message : e);
                }}
              }};
              if (window.turnstile && window.turnstile.render) {{
                window.__grokRender();
              }} else {{
                const s = document.createElement('script');
                s.src = {TURNSTILE_API!r};
                s.async = true;
                s.onload = function() {{ setTimeout(window.__grokRender, 300); }};
                s.onerror = function() {{ window.__grokTsStatus = 'script_error'; }};
                document.head.appendChild(s);
                window.__grokTsStatus = 'loading_api';
              }}
            }}"""
        )
        try:
            page.add_script_tag(url=TURNSTILE_API)
        except Exception as exc:
            log_fn(f"Turnstile add_script_tag: {exc}")

        # Poll for API then force render
        for _ in range(30):
            _checkpoint()
            ready = page.evaluate(
                "!!(window.turnstile && typeof window.turnstile.render === 'function')"
            )
            if ready:
                page.evaluate(
                    "typeof window.__grokRender === 'function' && window.__grokRender()"
                )
                break
            page.wait_for_timeout(300)

        token = _wait_token(deadline, clicks=True)
        if len(token) >= TOKEN_MIN_LENGTH:
            log_fn(f"Turnstile mint 成功(live) len={len(token)}")
            return token

        # ---------- Strategy 3: headed manual handoff ----------
        # User clicks the visible checkbox when automation cannot mint.
        if label == "headed" and time.monotonic() < deadline:
            log_fn(
                "Turnstile strategy=manual_wait "
                f"(请在打开的窗口中点击验证码，剩余 {max(0, int(deadline - time.monotonic()))}s)"
            )
            try:
                # Bring widget into view / click host once.
                _click_widget()
            except Exception:
                pass
            while time.monotonic() < deadline:
                _checkpoint()
                token = _read_token()
                if len(token) >= TOKEN_MIN_LENGTH:
                    log_fn(f"Turnstile mint 成功(manual) len={len(token)}")
                    return token
                page.wait_for_timeout(500)

        log_fn(f"Turnstile mint 超时 diag={_diag()}")
        raise RuntimeError(
            "turnstile timeout (no token); browser mint failed without captcha API"
        )
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if playwright:
                playwright.stop()
        except Exception:
            pass
