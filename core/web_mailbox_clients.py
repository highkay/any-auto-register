from __future__ import annotations

import random
import re
from typing import Any
from urllib.parse import quote, urljoin

import httpx

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)
_EMAIL_RE = re.compile(
    r"\b([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})\b",
    flags=re.IGNORECASE,
)
_SUPABASE_URL_RE = re.compile(r"(https://[a-z0-9]+\.supabase\.co)", flags=re.IGNORECASE)
_JWT_RE = re.compile(
    r"(eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,})"
)
def _normalize_domain(value: Any) -> str:
    return str(value or "").strip().lower().lstrip("@").rstrip(".")


def _generate_local_part(length: int = 10) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    prefix = random.choice("abcdefghijklmnopqrstuvwxyz")
    suffix = "".join(random.choices(alphabet, k=max(length - 1, 5)))
    return f"{prefix}{suffix}"


def _extract_email(text: str) -> str:
    match = _EMAIL_RE.search(str(text or ""))
    return str(match.group(1) or "").strip().lower() if match else ""


class BoomlifySessionClient:
    def __init__(
        self,
        *,
        api_base: str = "https://v1.boomlify.com",
    ):
        self.api_base = (
            str(api_base or "https://v1.boomlify.com").strip() or "https://v1.boomlify.com"
        ).rstrip("/")
        self._client: httpx.Client | None = None

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                follow_redirects=True,
                http2=True,
                trust_env=False,
                timeout=20.0,
                headers={
                    "user-agent": _USER_AGENT,
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                    "accept": "application/json, text/plain, */*",
                },
            )
        return self._client

    def list_public_domains(self) -> list[dict[str, Any]]:
        response = self._ensure_client().get(f"{self.api_base}/domains/public")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Boomlify domains/public 返回异常: {payload!r}")
        return [item for item in payload if isinstance(item, dict) and _normalize_domain(item.get("domain"))]

    def _pick_domain(self, domain: str = "") -> dict[str, Any]:
        domains = self.list_public_domains()
        target_domain = _normalize_domain(domain)
        if target_domain:
            for item in domains:
                if _normalize_domain(item.get("domain")) == target_domain:
                    return item
            raise RuntimeError(f"Boomlify 不支持指定域名: {target_domain}")
        if not domains:
            raise RuntimeError("Boomlify 当前没有可用域名")
        return random.choice(domains)

    def generate_random_email(self, *, domain: str = "") -> str:
        target = self._pick_domain(domain)
        target_domain = _normalize_domain(target.get("domain"))
        email_addr = f"{_generate_local_part(8)}@{target_domain}"
        response = self._ensure_client().post(
            f"{self.api_base}/emails/public/create",
            json={
                "email": email_addr,
                "domainId": target.get("id"),
            },
        )
        response.raise_for_status()
        payload = response.json()
        email_payload = (payload or {}).get("email")
        if isinstance(email_payload, dict):
            candidate = email_payload.get("email") or email_payload.get("address") or ""
        else:
            candidate = email_payload or ""
        result = _extract_email(candidate)
        if not result:
            raise RuntimeError(f"Boomlify create 返回异常: {payload!r}")
        return result

    def get_messages(self, email_addr: str) -> list[dict[str, Any]]:
        target = _extract_email(email_addr)
        if not target:
            raise RuntimeError("Boomlify 缺少邮箱地址，无法拉取收件箱")
        response = self._ensure_client().get(
            f"{self.api_base}/emails/public/{quote(target, safe='@')}"
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Boomlify 收件箱返回异常: {payload!r}")
        return [item for item in payload if isinstance(item, dict)]

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None


class NullstoSessionClient:
    def __init__(self, *, base_url: str = "https://nullsto.edu.pl"):
        self.base_url = (str(base_url or "https://nullsto.edu.pl").strip() or "https://nullsto.edu.pl").rstrip("/")
        self.home_url = f"{self.base_url}/"
        self._client: httpx.Client | None = None
        self._supabase_base: str | None = None
        self._anon_key: str | None = None
        self._domains: list[dict[str, Any]] | None = None
        self._tokens_by_email: dict[str, tuple[str, str]] = {}

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                follow_redirects=True,
                http2=True,
                trust_env=False,
                timeout=30.0,
                headers={
                    "user-agent": _USER_AGENT,
                    "accept-language": "en-US,en;q=0.9",
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                },
            )
        return self._client

    def _ensure_bootstrap(self) -> tuple[str, str]:
        if self._supabase_base and self._anon_key:
            return self._supabase_base, self._anon_key

        response = self._ensure_client().get(self.home_url)
        response.raise_for_status()
        html_text = response.text
        script_match = re.search(
            r'<script[^>]+src="([^"]*/assets/index-[^"]+\.js)"',
            html_text,
            flags=re.IGNORECASE,
        )
        if not script_match:
            raise RuntimeError("Nullsto 页面中未找到前端 bundle")
        bundle_url = urljoin(self.home_url, script_match.group(1))
        bundle = self._ensure_client().get(bundle_url)
        bundle.raise_for_status()
        js_text = bundle.text

        supabase_match = _SUPABASE_URL_RE.search(js_text)
        anon_key_match = _JWT_RE.search(js_text)
        if not supabase_match or not anon_key_match:
            raise RuntimeError("Nullsto 前端 bundle 中未找到 Supabase 配置")

        self._supabase_base = str(supabase_match.group(1) or "").rstrip("/")
        self._anon_key = str(anon_key_match.group(1) or "").strip()
        return self._supabase_base, self._anon_key

    def _supabase_headers(self) -> dict[str, str]:
        _supabase_base, anon_key = self._ensure_bootstrap()
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "apikey": anon_key,
            "authorization": f"Bearer {anon_key}",
        }

    def list_domains(self) -> list[dict[str, Any]]:
        if self._domains is not None:
            return list(self._domains)
        supabase_base, _anon_key = self._ensure_bootstrap()
        response = self._ensure_client().get(
            f"{supabase_base}/rest/v1/domains?select=*&is_active=eq.true&order=is_premium.asc&limit=20",
            headers=self._supabase_headers(),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Nullsto domains 返回异常: {payload!r}")
        self._domains = [item for item in payload if isinstance(item, dict) and _normalize_domain(item.get("name"))]
        return list(self._domains)

    def _pick_domain(self, domain: str = "") -> dict[str, Any]:
        domains = self.list_domains()
        target_domain = _normalize_domain(domain)
        if target_domain:
            for item in domains:
                if _normalize_domain(item.get("name")) == target_domain:
                    return item
            raise RuntimeError(f"Nullsto 不支持指定域名: {target_domain}")
        if not domains:
            raise RuntimeError("Nullsto 当前没有可用域名")
        return domains[0]

    def generate_random_email(self, *, domain: str = "") -> str:
        target = self._pick_domain(domain)
        supabase_base, _anon_key = self._ensure_bootstrap()
        domain_name = _normalize_domain(target.get("name"))
        email_addr = f"{_generate_local_part(10)}@{domain_name}"
        response = self._ensure_client().post(
            f"{supabase_base}/rest/v1/rpc/create_temp_email",
            headers=self._supabase_headers(),
            json={
                "p_address": email_addr,
                "p_domain_id": target.get("id"),
                "p_user_id": None,
                "p_expires_at": None,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("success"):
            raise RuntimeError(f"Nullsto create_temp_email 返回异常: {payload!r}")
        email_payload = dict(payload.get("email") or {})
        result = _extract_email(
            email_payload.get("address") or email_payload.get("email") or ""
        )
        email_id = str(email_payload.get("id") or "").strip()
        secret_token = str(email_payload.get("secret_token") or "").strip()
        if not result or not email_id or not secret_token:
            raise RuntimeError(f"Nullsto create_temp_email 返回缺少字段: {payload!r}")
        self._tokens_by_email[result] = (email_id, secret_token)
        return result

    def get_messages(self, email_addr: str) -> list[dict[str, Any]]:
        target = _extract_email(email_addr)
        if not target:
            raise RuntimeError("Nullsto 缺少邮箱地址，无法拉取收件箱")
        token_info = self._tokens_by_email.get(target)
        if not token_info:
            raise RuntimeError("Nullsto 当前会话缺少邮箱访问令牌")
        email_id, secret_token = token_info
        supabase_base, _anon_key = self._ensure_bootstrap()
        response = self._ensure_client().post(
            f"{supabase_base}/functions/v1/secure-email-access",
            headers=self._supabase_headers(),
            json={
                "action": "get_emails",
                "tempEmailId": email_id,
                "token": secret_token,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"Nullsto secure-email-access 返回异常: {payload!r}")
        messages = payload.get("emails") or []
        return [item for item in messages if isinstance(item, dict)]

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
