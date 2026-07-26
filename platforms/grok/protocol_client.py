"""Grok / x.ai 协议注册客户端。

对齐 Charles-0509/Grok-Register 的协议腿：
  TLS 指纹 (curl_cffi chrome131)
  → gRPC-Web 发/验码
  → 独立 Turnstile token
  → Next.js Server Action 建号
  → SSO hop 提取会话 cookie

浏览器只负责 Turnstile；注册主链走 HTTP，降低整页 UI 被 CF 403 的概率。
"""

from __future__ import annotations

import base64
import json
import random
import re
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

from curl_cffi import requests as curl_requests

from core.proxy_utils import build_requests_proxy_config

SITE_URL = "https://accounts.x.ai"
CONNECT_CREATE = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
CONNECT_VERIFY = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
CONNECT_PASS = f"{SITE_URL}/auth_mgmt.AuthManagement/ValidatePassword"
SIGNUP_URL_GROK = f"{SITE_URL}/sign-up?redirect=grok-com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_ROUTER_STATE_TREE_JSON = (
    '["",{"children":["(app)",{"children":["(auth)",{"children":["sign-up",'
    '{"children":["__PAGE__",{},null,null,0]},null,null,0]},null,null,0]},'
    "null,null,0]},null,null,16]"
)
# Runtime scrape preferred; this is only a last-resort fallback.
# Observed 2026-07 on accounts.x.ai signup chunk (42-char Next action id).
DEFAULT_NEXT_ACTION = "7fe62086186e534f952cbaf993efbf7ba7e61ed8e8"
DEFAULT_SITE_KEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_IMPERSONATE = "chrome131"

_SITEKEY_RE = re.compile(r"0x4AAAAAAA[a-zA-Z0-9_-]+")
_JS_SRC_RE = re.compile(r'src="(/_next/static/[^"]+\.js)"')
_HEX40_RE = re.compile(r"[a-fA-F0-9]{40,50}")
_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)')
_JWT_RE = re.compile(r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+")
_DOMAIN_REJECT_MARKERS = (
    "disposable",
    "email domain",
    "use another email",
    "please use another",
    "not allowed",
    "invalid email",
    "blocked domain",
    "unsupported email",
    "邮箱域名",
    "其他邮箱",
    "临时邮箱",
    "不可用",
)
_SET_COOKIE_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+set-cookie/?\?q=eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"
)
_SET_COOKIE_REL_RE = re.compile(
    r"(/[A-Za-z0-9_./-]*set-cookie/?\?q=eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)"
)
_SSO_NAMED_RE = re.compile(
    r"(?i)(?:^|[;,\s'\"\\])sso=(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)"
)
_SSO_NEAR_RE = re.compile(
    r"(?i)(?:sso|session)[^e]{0,40}(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)"
)

_GIVEN_NAMES = [
    "James",
    "John",
    "Robert",
    "Michael",
    "William",
    "David",
    "Richard",
    "Joseph",
    "Thomas",
    "Charles",
]
_FAMILY_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
]


class GrokProtocolError(RuntimeError):
    """Protocol-layer registration failure with optional machine code."""

    def __init__(self, message: str, *, code: str = "protocol"):
        super().__init__(message)
        self.code = code


@dataclass
class SignupConfig:
    site_key: str = ""
    action_id: str = ""
    state_tree: str = ""
    source: str = ""


@dataclass
class ProtocolResult:
    email: str
    password: str
    given_name: str
    family_name: str
    sso: str
    sso_rw: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)


def _pb_varint(n: int) -> bytes:
    parts = bytearray()
    while n > 0x7F:
        parts.append((n & 0x7F) | 0x80)
        n >>= 7
    parts.append(n & 0x7F)
    return bytes(parts)


def _pb_str(field: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    tag = bytes([((field << 3) | 2) & 0xFF])
    return tag + _pb_varint(len(raw)) + raw


def grpc_web_frame(inner: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(inner)) + inner


def _body_text(body: bytes | str) -> str:
    if isinstance(body, (bytes, bytearray)):
        return body.decode("utf-8", errors="ignore")
    return str(body or "")


def parse_grpc_status(headers: dict[str, Any], body: bytes | str) -> str:
    for key in ("grpc-status", "Grpc-Status", "GRPC-STATUS"):
        value = headers.get(key)
        if value not in (None, ""):
            return str(value).strip()
    text = _body_text(body)
    idx = text.rfind("grpc-status:")
    if idx < 0:
        return ""
    rest = text[idx + len("grpc-status:") :].lstrip(" \t")
    end = len(rest)
    for i, ch in enumerate(rest):
        if ch in "\r\n ":
            end = i
            break
    return rest[:end].strip()


def parse_grpc_message(headers: dict[str, Any], body: bytes | str) -> str:
    for key in ("grpc-message", "Grpc-Message", "GRPC-MESSAGE"):
        value = headers.get(key)
        if value not in (None, ""):
            return unquote(str(value).strip())
    text = _body_text(body)
    idx = text.rfind("grpc-message:")
    if idx < 0:
        return ""
    rest = text[idx + len("grpc-message:") :].lstrip(" \t")
    end = len(rest)
    for i, ch in enumerate(rest):
        if ch in "\r\n":
            end = i
            break
    return unquote(rest[:end].strip())


def extract_server_action_error(text: str) -> str:
    """Extract Next.js server-action error payload from RSC flight text.

    Full flight payloads embed i18n catalogs that contain words like
    "invalid email" / "email domain", so naive substring checks false-positive.
    Real action failures look like: 1:{"error":"...","traceId":...}
    """
    raw = str(text or "")
    if not raw:
        return ""
    for match in re.finditer(
        r'(?m)^\d+:\{"error":"((?:\\.|[^"\\])*)"',
        raw,
    ):
        err = match.group(1)
        err = (
            err.replace(r"\"", '"')
            .replace(r"\\n", " ")
            .replace(r"\\", "")
            .strip()
        )
        if err:
            return err
    # Compact / single-line fallback
    match = re.search(r'"error"\s*:\s*"((?:\\.|[^"\\]){8,240})"', raw)
    if match:
        err = match.group(1).replace(r"\"", '"').strip()
        # Ignore catalog-like long sentences without internal markers unless explicit.
        if err and (
            err.startswith("[internal]")
            or "turnstile" in err.lower()
            or "castle" in err.lower()
            or "validation" in err.lower()
            or "failed" in err.lower()
        ):
            return err
    return ""


def looks_like_domain_rejection(text: str) -> bool:
    # Prefer explicit action error first.
    action_error = extract_server_action_error(text)
    if action_error:
        lowered = action_error.lower()
        return any(marker in lowered for marker in _DOMAIN_REJECT_MARKERS)
    # Fallback: only inspect short bodies (not full RSC catalogs).
    raw = str(text or "")
    if len(raw) > 4000:
        return False
    lowered = raw.lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _DOMAIN_REJECT_MARKERS)


def classify_grpc_failure(
    *,
    http_status: int,
    grpc_status: str,
    grpc_message: str,
    body_preview: str = "",
    stage: str = "grpc",
) -> GrokProtocolError:
    merged = f"{grpc_message} {body_preview}"
    if http_status in {403, 503} or is_cloudflare_block(http_status, body_preview):
        code = "cf_403" if http_status == 403 else "cf_blocked"
        return GrokProtocolError(
            f"{stage} blocked http={http_status} grpc={grpc_status} msg={grpc_message[:160]}",
            code=code,
        )
    if looks_like_domain_rejection(merged):
        return GrokProtocolError(
            f"邮箱域名被拒绝: {grpc_message or body_preview[:160] or 'please use another email'}",
            code="email_domain_rejected",
        )
    return GrokProtocolError(
        f"{stage} http={http_status} grpc={grpc_status} msg={grpc_message[:200]}",
        code=stage,
    )


def normalize_rsc(text: str) -> str:
    value = str(text or "")
    return (
        value.replace(r"\u0026", "&")
        .replace(r"\u003d", "=")
        .replace(r"\u002F", "/")
        .replace(r"\/", "/")
    )


def jwt_payload_map(token: str) -> Optional[dict[str, Any]]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    pad = "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_session_sso(token: str) -> bool:
    tok = str(token or "").strip()
    if not tok.startswith("eyJ") or tok.count(".") != 2:
        return False
    payload = jwt_payload_map(tok)
    if payload is None:
        return len(tok) > 80
    config = payload.get("config")
    if isinstance(config, dict):
        if "success_url" in config or "token" in config:
            return False
    if "success_url" in payload:
        return False
    return len(tok) > 40


def extract_sso_from_text(text: str) -> str:
    body = normalize_rsc(text)
    match = _SSO_NAMED_RE.search(body)
    if match and is_session_sso(match.group(1)):
        return match.group(1)
    match = _SSO_NEAR_RE.search(body)
    if match and is_session_sso(match.group(1)):
        return match.group(1)
    for candidate in _JWT_RE.findall(body):
        if is_session_sso(candidate):
            return candidate
    return ""


def jwt_from_set_cookie_url(url: str) -> str:
    raw = unquote(str(url or ""))
    idx = raw.find("q=")
    if idx >= 0:
        rest = raw[idx + 2 :]
        for sep in ("&", '"', "'", " "):
            cut = rest.find(sep)
            if cut >= 0:
                rest = rest[:cut]
                break
        if rest.startswith("eyJ"):
            return rest
    match = _JWT_RE.search(raw)
    return match.group(0) if match else ""


def extract_all_set_cookie_urls(text: str) -> list[str]:
    body = normalize_rsc(text)
    found: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        value = str(url or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        found.append(value)

    for match in _SET_COOKIE_URL_RE.findall(body):
        _add(match)
    for match in _SET_COOKIE_REL_RE.findall(body):
        _add(SITE_URL + match)
    if not found:
        lower = body.lower()
        idx = lower.find("set-cookie")
        if idx >= 0:
            window = body[idx : idx + 400]
            jwt = _JWT_RE.search(window)
            if jwt:
                _add(f"https://auth.grokusercontent.com/set-cookie?q={jwt.group(0)}")
    return found


def expand_sso_hop_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        value = str(url or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        out.append(value)

    for url in urls:
        _add(url)
        jwt = jwt_from_set_cookie_url(url)
        if not jwt:
            continue
        payload = jwt_payload_map(jwt) or {}
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        for key in ("success_url",):
            success = ""
            if isinstance(config, dict):
                success = str(config.get(key) or "")
            if not success:
                success = str(payload.get(key) or "")
            if success.startswith("https://"):
                _add(success)
                if "set-cookie" in success and "q=" not in success:
                    _add(success.rstrip("/") + "?q=" + jwt)
        for host in (
            "https://auth.grokusercontent.com/set-cookie?q=",
            "https://auth.grokipedia.com/set-cookie?q=",
            "https://auth.grok.com/set-cookie?q=",
            "https://auth.x.ai/set-cookie?q=",
        ):
            _add(host + jwt)
    return out


def build_signup_body(
    email: str,
    password: str,
    code: str,
    turnstile_token: str,
    *,
    given_name: str = "",
    family_name: str = "",
    castle_token: str = "",
) -> bytes:
    given = given_name or random.choice(_GIVEN_NAMES)
    family = family_name or random.choice(_FAMILY_NAMES)
    payload = [
        {
            "emailValidationCode": code,
            "createUserAndSessionRequest": {
                "email": email,
                "givenName": given,
                "familyName": family,
                "clearTextPassword": password,
                "tosAcceptedVersion": 1,
            },
            "turnstileToken": turnstile_token,
            "conversionId": str(uuid.uuid4()),
            "castleRequestToken": castle_token or "",
        }
    ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def scrape_state_tree(html: str) -> str:
    """Extract Next-Router-State-Tree from RSC flight payloads when possible."""
    text = str(html or "")
    for match in _FLIGHT_RE.finditer(text):
        chunk = match.group(1) or ""
        decoded = chunk.replace(r"\"", '"')
        if "sign-up" not in decoded:
            continue
        idx = decoded.find('"f":[[[')
        if idx < 0:
            continue
        f_start = idx + 5  # points at [[[...
        end_rel = decoded[f_start:].find('"$undefined"')
        if end_rel < 0:
            continue
        raw = decoded[f_start : f_start + end_rel]
        raw = raw.replace(r'\\"', '"').replace("\\", "")
        raw = raw.strip().rstrip(",")
        if raw.startswith("[") and "sign-up" in raw:
            return quote(raw, safe="")
    if "sign-up" in text:
        return quote(DEFAULT_ROUTER_STATE_TREE_JSON, safe="")
    return ""


def is_cloudflare_block(status: int, body: str, headers: Optional[dict[str, Any]] = None) -> bool:
    if status in {403, 503, 429}:
        lower = (body or "").lower()
        markers = (
            "just a moment",
            "cf-browser-verification",
            "attention required",
            "cloudflare",
            "cf-ray",
            "sorry, you have been blocked",
            "enable javascript and cookies",
        )
        if any(m in lower for m in markers):
            return True
        if status == 403 and len((body or "").strip()) < 80:
            return True
    if headers:
        server = str(headers.get("Server") or headers.get("server") or "").lower()
        if "cloudflare" in server and status >= 400:
            return True
    return False


class GrokProtocolClient:
    def __init__(
        self,
        proxy: Optional[str] = None,
        *,
        impersonate: str = DEFAULT_IMPERSONATE,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 45.0,
        log_fn: Callable[[str], None] = print,
        task_control=None,
    ):
        self.proxy = (proxy or "").strip() or None
        self.impersonate = impersonate or DEFAULT_IMPERSONATE
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.log = log_fn
        self._task_control = task_control
        self.cfg = SignupConfig()
        self._session = self._new_session()

    def _checkpoint(self) -> None:
        if self._task_control is not None:
            self._task_control.checkpoint()

    def _new_session(self):
        session = curl_requests.Session(impersonate=self.impersonate)
        if self.proxy:
            session.proxies = build_requests_proxy_config(self.proxy)
        return session

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def recreate(self, impersonate: Optional[str] = None) -> None:
        cookies = self.export_cookie_pairs()
        if impersonate:
            self.impersonate = impersonate
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._new_session()
        self.apply_cookie_pairs(cookies)

    def export_cookie_pairs(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        jar = getattr(self._session.cookies, "jar", None) or self._session.cookies
        try:
            for cookie in jar:
                name = str(getattr(cookie, "name", "") or "")
                value = str(getattr(cookie, "value", "") or "")
                domain = str(getattr(cookie, "domain", "") or "")
                if name:
                    out.append((name, value, domain))
        except TypeError:
            # Mapping-like cookie jar fallback
            try:
                for name, value in dict(self._session.cookies).items():
                    out.append((str(name), str(value), ""))
            except Exception:
                pass
        return out

    def apply_cookie_pairs(self, cookies: list[tuple[str, str, str]] | list[dict[str, Any]]) -> None:
        for item in cookies or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "")
                domain = str(item.get("domain") or "").strip() or ".x.ai"
            else:
                name, value, domain = item
                name = str(name or "").strip()
                value = str(value or "")
                domain = str(domain or "").strip() or ".x.ai"
            if not name:
                continue
            try:
                self._session.cookies.set(name, value, domain=domain)
            except Exception:
                try:
                    self._session.cookies.set(name, value)
                except Exception:
                    pass

    def apply_clearance_cookies(self, cookies: list[dict[str, Any]], user_agent: str = "") -> None:
        if user_agent:
            self.user_agent = user_agent
        self.apply_cookie_pairs(cookies)

    def _browser_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    def _grpc_headers(self) -> dict[str, str]:
        headers = self._browser_headers()
        headers.update(
            {
                "Content-Type": "application/grpc-web+proto",
                "X-Grpc-Web": "1",
                "X-User-Agent": "connect-es/2.1.1",
                "Origin": SITE_URL,
                "Referer": SIGNUP_URL_GROK,
                "Accept": "*/*",
            }
        )
        return headers

    def warm_signup(self) -> tuple[int, str, dict[str, Any]]:
        self._checkpoint()
        headers = self._browser_headers()
        headers.update(
            {
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://grok.com/",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "cross-site",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        resp = self._session.get(
            SIGNUP_URL_GROK,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=True,
        )
        return int(resp.status_code), str(resp.text or ""), dict(resp.headers or {})

    def fetch_config(self) -> SignupConfig:
        status, html, headers = self.warm_signup()
        cfg = SignupConfig(source=f"http status={status} profile={self.impersonate}")
        if status != 200 or is_cloudflare_block(status, html, headers):
            cfg.source += " (blocked_or_empty)"
            code = "cf_403" if status == 403 else "cf_blocked"
            preview = " ".join(html.split())[:180]
            raise GrokProtocolError(
                f"signup page blocked status={status} profile={self.impersonate} body={preview}",
                code=code,
            )
        match = _SITEKEY_RE.search(html)
        if match:
            cfg.site_key = match.group(0)
        scraped_tree = scrape_state_tree(html)
        if scraped_tree:
            cfg.state_tree = scraped_tree
            if scraped_tree != quote(DEFAULT_ROUTER_STATE_TREE_JSON, safe=""):
                cfg.source += "+scrape_tree"
            else:
                cfg.source += "+default_tree"
        js_paths = []
        for item in _JS_SRC_RE.findall(html):
            if item not in js_paths:
                js_paths.append(item)
        # Prefer chunks that mention createUser/registerUser.
        # Scan broadly: the signup action id often lives past the first 24 chunks.
        signup_keys = (
            "createUser",
            "registerUser",
            "emailValidation",
            "createUserAndSession",
            "emailValidationCode",
            "turnstileToken",
            "castleRequestToken",
        )
        for path in js_paths[:80]:
            if cfg.action_id:
                break
            js = self._fetch_js(path)
            if not js:
                continue
            if not any(k in js for k in signup_keys):
                continue
            # Prefer quoted action ids (Next embeds them as string literals).
            quoted = re.findall(r"""['"]([a-f0-9]{40,42})['"]""", js)
            hexes = quoted or _HEX40_RE.findall(js)
            if not hexes:
                continue
            # Prefer 40-42 char hashes; avoid longer accidental matches.
            preferred = next(
                (h for h in hexes if 40 <= len(h) <= 42),
                hexes[0],
            )
            cfg.action_id = preferred
            cfg.source += "+scrape_action"
        if not cfg.action_id:
            cfg.action_id = DEFAULT_NEXT_ACTION
            cfg.source += "+default_action"
        if not cfg.site_key:
            cfg.site_key = DEFAULT_SITE_KEY
            cfg.source += "+default_sitekey"
        if not cfg.state_tree:
            cfg.state_tree = quote(DEFAULT_ROUTER_STATE_TREE_JSON, safe="")
            cfg.source += "+default_tree"
        self.cfg = cfg
        self.log(
            f"协议配置 site_key={cfg.site_key[:16]}... action={cfg.action_id[:12]}... src={cfg.source}"
        )
        return cfg

    def _fetch_js(self, path: str) -> str:
        self._checkpoint()
        headers = self._browser_headers()
        headers["Referer"] = SIGNUP_URL_GROK
        try:
            resp = self._session.get(
                SITE_URL + path,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                return ""
            return str(resp.text or "")
        except Exception:
            return ""

    def _raise_if_grpc_failed(
        self,
        resp,
        *,
        stage: str,
    ) -> None:
        body = resp.content if hasattr(resp, "content") else (resp.text or "").encode("utf-8", "ignore")
        headers = dict(resp.headers or {})
        status = parse_grpc_status(headers, body)
        if status == "":
            status = "0"
        message = parse_grpc_message(headers, body)
        preview = " ".join(_body_text(body).split())[:220]
        http_status = int(getattr(resp, "status_code", 0) or 0)
        if http_status != 200 or status not in {"0", ""}:
            raise classify_grpc_failure(
                http_status=http_status,
                grpc_status=status,
                grpc_message=message,
                body_preview=preview,
                stage=stage,
            )

    def create_email_code(self, email: str, castle_token: str = "") -> None:
        self._checkpoint()
        inner = _pb_str(1, email)
        if castle_token:
            inner += _pb_str(3, castle_token)
        frame = grpc_web_frame(inner)
        resp = self._session.post(
            CONNECT_CREATE,
            data=frame,
            headers=self._grpc_headers(),
            timeout=self.timeout,
        )
        self._raise_if_grpc_failed(resp, stage="grpc_create")
        self.log(f"协议发码成功: {email}")

    def verify_email_code(self, email: str, code: str) -> None:
        self._checkpoint()
        clean = str(code or "").replace("-", "").replace(" ", "").strip()
        inner = _pb_str(1, email) + _pb_str(2, clean)
        frame = grpc_web_frame(inner)
        resp = self._session.post(
            CONNECT_VERIFY,
            data=frame,
            headers=self._grpc_headers(),
            timeout=self.timeout,
        )
        self._raise_if_grpc_failed(resp, stage="grpc_verify")
        self.log("协议验码成功")

    def validate_password(self, email: str, password: str) -> None:
        self._checkpoint()
        inner = _pb_str(4, email) + _pb_str(5, password)
        frame = grpc_web_frame(inner)
        resp = self._session.post(
            CONNECT_PASS,
            data=frame,
            headers=self._grpc_headers(),
            timeout=self.timeout,
        )
        self._raise_if_grpc_failed(resp, stage="grpc_password")

    def _cookie_value(self, name: str) -> str:
        for cname, value, _domain in self.export_cookie_pairs():
            if cname == name and is_session_sso(value):
                return value
        # Fallback: plain mapping may hold non-session tokens
        try:
            value = str(self._session.cookies.get(name) or "")
            if name == "sso" and is_session_sso(value):
                return value
            if name != "sso":
                return value
        except Exception:
            pass
        return ""

    def jar_sso(self) -> str:
        value = self._cookie_value("sso")
        if value:
            return value
        return ""

    def follow_sso_hop(self, start: str) -> str:
        hops = expand_sso_hop_urls([start])
        seen: set[str] = set()
        i = 0
        while i < len(hops) and i < 10:
            self._checkpoint()
            hop = hops[i]
            i += 1
            if not hop or hop in seen:
                continue
            seen.add(hop)
            headers = self._browser_headers()
            headers.update(
                {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": SITE_URL + "/",
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
            try:
                resp = self._session.get(
                    hop,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except Exception:
                continue
            body = str(resp.text or "")
            for cookie in getattr(resp, "cookies", []) or []:
                name = str(getattr(cookie, "name", "") or "")
                value = str(getattr(cookie, "value", "") or "")
                if name == "sso" and is_session_sso(value):
                    return value
            # Also check jar after response
            sso = self.jar_sso()
            if sso:
                return sso
            extracted = extract_sso_from_text(body)
            if extracted:
                return extracted
            loc = ""
            try:
                loc = str(resp.headers.get("Location") or resp.headers.get("location") or "")
            except Exception:
                loc = ""
            if loc.startswith("/"):
                if "grokusercontent" in hop:
                    loc = "https://auth.grokusercontent.com" + loc
                else:
                    loc = SITE_URL + loc
            if 300 <= int(getattr(resp, "status_code", 0) or 0) < 400 and loc.startswith("http"):
                for expanded in expand_sso_hop_urls([loc]):
                    if expanded not in seen:
                        hops.append(expanded)
        return self.jar_sso()

    def signup_server_action(
        self,
        body: bytes,
        action_id: str,
        state_tree: str,
    ) -> tuple[str, str]:
        self._checkpoint()
        headers = self._browser_headers()
        headers.update(
            {
                "Accept": "text/x-component",
                "Content-Type": "text/plain;charset=UTF-8",
                "Next-Action": action_id,
                "Next-Router-State-Tree": state_tree,
                "Origin": SITE_URL,
                "Referer": SIGNUP_URL_GROK,
            }
        )
        resp = self._session.post(
            SIGNUP_URL_GROK,
            data=body,
            headers=headers,
            timeout=self.timeout,
            allow_redirects=True,
        )
        text = str(resp.text or "")
        sso = ""
        for cookie in getattr(resp, "cookies", []) or []:
            name = str(getattr(cookie, "name", "") or "")
            value = str(getattr(cookie, "value", "") or "")
            if name == "sso" and is_session_sso(value):
                sso = value
                break
        if not sso:
            for hop in expand_sso_hop_urls(extract_all_set_cookie_urls(text)):
                value = self.follow_sso_hop(hop)
                if is_session_sso(value):
                    sso = value
                    break
        if not is_session_sso(sso):
            sso = self.jar_sso()
        if not is_session_sso(sso):
            extracted = extract_sso_from_text(text)
            if is_session_sso(extracted):
                sso = extracted
        if not is_session_sso(sso):
            sso = ""
        preview = " ".join(text.split())[:220]
        http_status = int(resp.status_code or 0)
        action_error = extract_server_action_error(text)
        if http_status >= 400:
            if is_cloudflare_block(http_status, text, dict(resp.headers or {})):
                code = "cf_403" if http_status == 403 else "cf_blocked"
            elif looks_like_domain_rejection(text):
                code = "email_domain_rejected"
            else:
                code = "signup_http"
            raise GrokProtocolError(
                f"signup http={http_status} body={preview}",
                code=code,
            )
        if action_error and not sso:
            lowered = action_error.lower()
            if "turnstile" in lowered:
                code = "turnstile"
            elif looks_like_domain_rejection(action_error):
                code = "email_domain_rejected"
            elif "castle" in lowered:
                code = "castle"
            else:
                code = "signup_action_error"
            raise GrokProtocolError(
                f"signup action error: {action_error}",
                code=code,
            )
        if looks_like_domain_rejection(text) and not sso:
            raise GrokProtocolError(
                f"邮箱域名被拒绝: {preview or 'please use another email'}",
                code="email_domain_rejected",
            )
        if not sso:
            raise GrokProtocolError(
                f"signup ok but no session sso hops={len(extract_all_set_cookie_urls(text))} body={preview}",
                code="signup_no_sso",
            )
        return text, sso

    def export_playwright_cookies(self) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for name, value, domain in self.export_cookie_pairs():
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain or ".x.ai",
                    "path": "/",
                    "secure": True,
                    "httpOnly": name.lower().startswith("sso") or name in {"cf_clearance", "__cf_bm"},
                }
            )
        return cookies
