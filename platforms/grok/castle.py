"""Castle.io request-token mint for xAI signup trust.

Browser SPA uses Castle CDN v2 (`window._castle`). Missing tokens can still yield
SSO cookies, but Device OAuth often fails later with `invalid_grant`.

Reference (user notes / frontend parity):
  _castle('setAppId', pk)
  _castle('createRequestToken')  -> castleRequestToken
"""

from __future__ import annotations

import os
from typing import Callable, Optional

# Public publishable key observed on accounts.x.ai frontend.
DEFAULT_CASTLE_PK = "pk_p8GGwD3TmFJZRsX3BQcqAv9aFVispNz"
CASTLE_CDN_V2 = "https://cdn.castle.io/v2/castle.js"


def resolve_castle_pk(extra: Optional[dict] = None) -> str:
    extra = extra or {}
    return str(
        extra.get("grok_castle_pk")
        or os.getenv("GROK_CASTLE_PK")
        or DEFAULT_CASTLE_PK
    ).strip() or DEFAULT_CASTLE_PK


def mint_castle_request_token(
    *,
    proxy: Optional[str] = None,
    pk: str = "",
    page_url: str = "https://accounts.x.ai/sign-up",
    user_agent: str = "",
    log_fn: Callable[[str], None] = print,
    task_control=None,
    timeout: float = 30.0,
) -> str:
    """Mint a Castle request token via Patchright + CDN v2.

    Uses (0, eval)(src) after fetching the script (CDN v2 pattern), not
    script.textContent assignment.
    """
    from patchright.sync_api import sync_playwright

    from core.browser_runtime import with_chrome_executable
    from core.proxy_utils import build_playwright_proxy_config
    from curl_cffi import requests as curl_requests

    app_id = (pk or DEFAULT_CASTLE_PK).strip() or DEFAULT_CASTLE_PK
    log_fn(f"Castle mint: pk={app_id[:12]}... page={page_url}")

    # Fetch script out-of-band (more reliable than page fetch under CSP).
    sess = curl_requests.Session(impersonate="chrome131")
    if proxy:
        from core.proxy_utils import build_requests_proxy_config

        sess.proxies = build_requests_proxy_config(proxy)
    try:
        src = sess.get(CASTLE_CDN_V2, timeout=20).text
    finally:
        try:
            sess.close()
        except Exception:
            pass
    if not src or "_castle" not in src:
        raise RuntimeError("Castle CDN v2 脚本下载失败或内容异常")

    playwright = browser = context = None
    try:
        playwright = sync_playwright().start()
        launch: dict = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--window-position=120,80",
                "--window-size=900,700",
            ],
        }
        proxy_cfg = build_playwright_proxy_config(proxy) if proxy else None
        if proxy_cfg:
            launch["proxy"] = proxy_cfg
        try:
            browser = playwright.chromium.launch(
                **with_chrome_executable(launch, channel="chrome")
            )
        except Exception:
            browser = playwright.chromium.launch(**with_chrome_executable(launch))

        ctx_kwargs: dict = {"viewport": {"width": 900, "height": 700}}
        if user_agent:
            ctx_kwargs["user_agent"] = user_agent
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()

        # Prefer real origin when reachable; fall back to blank content.
        try:
            if task_control is not None:
                task_control.checkpoint()
            page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            log_fn(f"Castle mint: 打开 {page_url} 失败，改用 about:blank ({exc})")
            page.set_content("<!doctype html><title>castle</title><body></body>")

        page.wait_for_timeout(400)
        if task_control is not None:
            task_control.checkpoint()

        page.evaluate(
            """(src) => {
              try { (0, eval)(src); } catch (e) { window.__castleEvalErr = String(e); }
            }""",
            src,
        )
        page.wait_for_timeout(300)

        token = page.evaluate(
            """async (pk) => {
              const c = window._castle;
              if (typeof c !== 'function') {
                return { ok: false, err: 'window._castle missing type=' + (typeof c) };
              }
              try { c('setAppId', pk); } catch (e) {
                return { ok: false, err: 'setAppId: ' + (e && e.message || e) };
              }
              let t;
              try { t = c('createRequestToken'); } catch (e) {
                return { ok: false, err: 'createRequestToken: ' + (e && e.message || e) };
              }
              if (t && typeof t.then === 'function') {
                try { t = await t; } catch (e) {
                  return { ok: false, err: 'createRequestToken async: ' + (e && e.message || e) };
                }
              }
              const s = (t == null ? '' : String(t)).trim();
              return { ok: s.length > 20, token: s, len: s.length };
            }""",
            app_id,
        )
        if not isinstance(token, dict) or not token.get("ok"):
            err = token.get("err") if isinstance(token, dict) else repr(token)
            raise RuntimeError(f"Castle createRequestToken 失败: {err}")
        value = str(token.get("token") or "").strip()
        log_fn(f"Castle mint 成功 len={len(value)}")
        return value
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


def ensure_castle_on_page(page, *, pk: str = "", log_fn: Callable[[str], None] = print) -> str:
    """Ensure Castle is present on an existing page and return a request token."""
    app_id = (pk or DEFAULT_CASTLE_PK).strip() or DEFAULT_CASTLE_PK
    ready = False
    try:
        ready = bool(page.evaluate("typeof window._castle === 'function'"))
    except Exception:
        ready = False
    if not ready:
        # Prefer in-page script tag (avoids curl TLS issues on some Windows hosts).
        loaded = False
        try:
            page.add_script_tag(url=CASTLE_CDN_V2)
            page.wait_for_timeout(400)
            loaded = bool(page.evaluate("typeof window._castle === 'function'"))
        except Exception as exc:
            log_fn(f"Castle add_script_tag: {exc}")
        if not loaded:
            src = ""
            try:
                from curl_cffi import requests as curl_requests

                src = curl_requests.get(
                    CASTLE_CDN_V2, timeout=20, impersonate="chrome131"
                ).text
            except Exception as exc:
                log_fn(f"Castle CDN curl 失败: {exc}")
                try:
                    # Playwright request as last resort (same browser network stack).
                    src = page.context.request.get(CASTLE_CDN_V2, timeout=20000).text()
                except Exception as exc2:
                    log_fn(f"Castle CDN page.request 失败: {exc2}")
            if src and "_castle" in src:
                page.evaluate(
                    """(src) => { try { (0, eval)(src); } catch (e) { window.__castleEvalErr = String(e); } }""",
                    src,
                )
                page.wait_for_timeout(300)
    result = page.evaluate(
        """async (pk) => {
          const c = window._castle;
          if (typeof c !== 'function') return { ok:false, err:'no _castle type=' + (typeof c) + ' evalErr=' + (window.__castleEvalErr||'') };
          try { c('setAppId', pk); } catch (e) {}
          let t = c('createRequestToken');
          if (t && t.then) t = await t;
          const s = String(t || '').trim();
          return { ok: s.length > 20, token: s, len: s.length };
        }""",
        app_id,
    )
    if not isinstance(result, dict) or not result.get("ok"):
        err = result.get("err") if isinstance(result, dict) else repr(result)
        log_fn(f"Castle on-page mint 失败: {err}")
        return ""
    tok = str(result.get("token") or "").strip()
    log_fn(f"Castle on-page mint 成功 len={len(tok)}")
    return tok
