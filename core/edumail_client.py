from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

_DEFAULT_BASE_URL = "https://edumail.su"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)


@dataclass
class _LivewireComponent:
    fingerprint: dict[str, Any]
    name: str
    server_memo: dict[str, Any]


class EduMailSessionClient:
    """EduMail.su mailbox session driven by Livewire HTTP requests."""

    def __init__(self, *, base_url: str = _DEFAULT_BASE_URL):
        self.base_url = (str(base_url or _DEFAULT_BASE_URL).strip() or _DEFAULT_BASE_URL).rstrip("/")
        self.home_url = f"{self.base_url}/"
        self.mailbox_url = f"{self.base_url}/mailbox"
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
                    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
        return self._client

    @staticmethod
    def _extract_csrf(html_text: str) -> str:
        match = re.search(
            r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
            str(html_text or ""),
            flags=re.IGNORECASE,
        )
        if match:
            return html.unescape(match.group(1))

        fallback = re.search(
            r'<input[^>]+name="_token"[^>]+value="([^"]+)"',
            str(html_text or ""),
            flags=re.IGNORECASE,
        )
        if fallback:
            return html.unescape(fallback.group(1))

        raise RuntimeError("EduMail 页面中未找到 csrf token")

    @staticmethod
    def _extract_email(text: str) -> str:
        match = re.search(
            r"\b([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})\b",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        return str(match.group(1) or "").strip().lower() if match else ""

    @staticmethod
    def _extract_components(html_text: str) -> dict[str, _LivewireComponent]:
        pattern = re.compile(
            r'wire:id="[^"]+"[^>]*wire:initial-data="(?P<data>[^"]+)"',
            flags=re.IGNORECASE,
        )
        components: dict[str, _LivewireComponent] = {}
        for match in pattern.finditer(str(html_text or "")):
            raw = html.unescape(match.group("data"))
            payload = json.loads(raw)
            fingerprint = dict(payload.get("fingerprint") or {})
            server_memo = dict(payload.get("serverMemo") or {})
            name = str(fingerprint.get("name") or "").strip()
            if not name:
                continue
            components[name] = _LivewireComponent(
                fingerprint=fingerprint,
                name=name,
                server_memo=server_memo,
            )
        return components

    def _livewire_headers(self, csrf_token: str, referer: str) -> dict[str, str]:
        return {
            "accept": "text/html, application/xhtml+xml",
            "content-type": "application/json",
            "origin": self.base_url,
            "referer": referer,
            "x-csrf-token": csrf_token,
            "x-livewire": "true",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    def _post_livewire(
        self,
        component: _LivewireComponent,
        csrf_token: str,
        referer: str,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        response = self._ensure_client().post(
            f"{self.base_url}/livewire/message/{component.name}",
            headers=self._livewire_headers(csrf_token, referer),
            json={
                "fingerprint": component.fingerprint,
                "serverMemo": component.server_memo,
                "updates": updates,
            },
        )
        if response.status_code >= 400:
            preview = (response.text or "")[:400]
            raise RuntimeError(
                f"EduMail Livewire {component.name} 失败: HTTP {response.status_code} {preview}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"EduMail Livewire {component.name} 返回异常: {payload}")
        server_memo = payload.get("serverMemo")
        if isinstance(server_memo, dict):
            component.server_memo = self._merge_server_memo(component.server_memo, server_memo)
        return payload

    @staticmethod
    def _merge_server_memo(
        current: dict[str, Any],
        update: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(current or {})
        for key, value in dict(update or {}).items():
            if (
                key == "data"
                and isinstance(value, dict)
                and isinstance(merged.get("data"), dict)
            ):
                next_data = dict(merged.get("data") or {})
                next_data.update(value)
                merged["data"] = next_data
            else:
                merged[key] = value
        return merged

    def _load_page_state(
        self,
        url: str,
    ) -> tuple[str, dict[str, _LivewireComponent], str]:
        response = self._ensure_client().get(url)
        response.raise_for_status()
        html_text = response.text
        csrf_token = self._extract_csrf(html_text)
        components = self._extract_components(html_text)
        return csrf_token, components, html_text

    @staticmethod
    def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
        server_data = dict((payload.get("serverMemo") or {}).get("data") or {})
        messages = server_data.get("messages") or []
        return [item for item in messages if isinstance(item, dict)]

    @staticmethod
    def _generate_local_part() -> str:
        import random
        import string

        alphabet = string.ascii_lowercase + string.digits
        prefix = random.choice(string.ascii_lowercase)
        suffix = "".join(random.choices(alphabet, k=9))
        return f"{prefix}{suffix}"

    def generate_random_email(
        self,
        *,
        domain: str = "",
        blocked_domains: set[str] | None = None,
    ) -> str:
        csrf_token, components, _ = self._load_page_state(self.home_url)
        actions = components.get("frontend.actions")
        if actions is None:
            raise RuntimeError(
                f"EduMail 首页缺少 frontend.actions 组件: {sorted(components)}"
            )

        known_domains = [
            str(item or "").strip().lower()
            for item in ((actions.server_memo.get("data") or {}).get("domains") or [])
            if str(item or "").strip()
        ]
        blocked = {
            str(item or "").strip().lower().lstrip("@")
            for item in (blocked_domains or set())
            if str(item or "").strip()
        }
        target_domain = str(domain or "").strip().lower().lstrip("@")
        if not target_domain and blocked:
            allowed_domains = [item for item in known_domains if item not in blocked]
            if allowed_domains:
                import random

                target_domain = random.choice(allowed_domains)

        if target_domain:
            if target_domain not in known_domains:
                raise RuntimeError(f"EduMail 不支持指定域名: {target_domain}")
            current_domain = str(
                ((actions.server_memo.get("data") or {}).get("domain") or "")
            ).strip().lower()
            if target_domain != current_domain:
                self._post_livewire(
                    actions,
                    csrf_token,
                    self.home_url,
                    [
                        {
                            "type": "callMethod",
                            "payload": {
                                "id": "set-domain",
                                "method": "setDomain",
                                "params": [target_domain],
                            },
                        }
                    ],
                )

            for _ in range(3):
                local_part = self._generate_local_part()
                self._post_livewire(
                    actions,
                    csrf_token,
                    self.home_url,
                    [
                        {
                            "type": "syncInput",
                            "payload": {
                                "id": "set-user",
                                "name": "user",
                                "value": local_part,
                            },
                        },
                        {
                            "type": "callMethod",
                            "payload": {
                                "id": "create-email",
                                "method": "create",
                                "params": [],
                            },
                        },
                    ],
                )
                time.sleep(0.25)
                _, mailbox_components, mailbox_html = self._load_page_state(self.mailbox_url)
                app = mailbox_components.get("frontend.app")
                actions_after = mailbox_components.get("frontend.actions")
                email_addr = ""
                if app is not None:
                    email_addr = self._extract_email(
                        ((app.server_memo.get("data") or {}).get("email") or "")
                    )
                if not email_addr and actions_after is not None:
                    email_addr = self._extract_email(
                        ((actions_after.server_memo.get("data") or {}).get("email") or "")
                    )
                if not email_addr:
                    email_addr = self._extract_email(mailbox_html)
                if email_addr.endswith(f"@{target_domain}"):
                    return email_addr
            raise RuntimeError(f"EduMail 指定域名创建失败: {target_domain}")

        self._post_livewire(
            actions,
            csrf_token,
            self.home_url,
            [
                {
                    "type": "callMethod",
                    "payload": {"id": "generate-random", "method": "random", "params": []},
                }
            ],
        )

        time.sleep(0.25)
        _, mailbox_components, mailbox_html = self._load_page_state(self.mailbox_url)
        app = mailbox_components.get("frontend.app")
        actions = mailbox_components.get("frontend.actions")

        email_addr = ""
        if app is not None:
            email_addr = self._extract_email(
                ((app.server_memo.get("data") or {}).get("email") or "")
            )
        if not email_addr and actions is not None:
            email_addr = self._extract_email(
                ((actions.server_memo.get("data") or {}).get("email") or "")
            )
        if not email_addr:
            email_addr = self._extract_email(mailbox_html)
        if not email_addr:
            raise RuntimeError("EduMail 随机邮箱生成成功后未能解析邮箱地址")
        return email_addr

    def get_messages(self, email_addr: str) -> list[dict[str, Any]]:
        email_addr = self._extract_email(email_addr)
        if not email_addr:
            raise RuntimeError("EduMail 缺少邮箱地址，无法拉取收件箱")

        csrf_token, components, _ = self._load_page_state(self.mailbox_url)
        actions = components.get("frontend.actions")
        if actions is None:
            raise RuntimeError(
                f"EduMail mailbox 页面缺少 frontend.actions 组件: {sorted(components)}"
            )

        self._post_livewire(
            actions,
            csrf_token,
            self.mailbox_url,
            [
                {
                    "type": "fireEvent",
                    "payload": {
                        "id": "sync-email-actions",
                        "event": "syncEmail",
                        "params": [email_addr],
                    },
                }
            ],
        )

        time.sleep(0.15)
        refreshed_csrf, refreshed_components, _ = self._load_page_state(self.mailbox_url)
        app = refreshed_components.get("frontend.app")
        if app is None:
            raise RuntimeError(
                f"EduMail mailbox 页面缺少 frontend.app 组件: {sorted(refreshed_components)}"
            )

        payload = self._post_livewire(
            app,
            refreshed_csrf,
            self.mailbox_url,
            [
                {
                    "type": "fireEvent",
                    "payload": {
                        "id": "sync-email-app",
                        "event": "syncEmail",
                        "params": [email_addr],
                    },
                },
                {
                    "type": "fireEvent",
                    "payload": {
                        "id": "fetch-messages",
                        "event": "fetchMessages",
                        "params": [],
                    },
                },
            ],
        )
        return self._extract_messages(payload)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
