import json
import unittest
from unittest.mock import patch

import httpx

from core.edumail_client import EduMailSessionClient
from core.web_mailbox_clients import (
    BoomlifySessionClient,
    NullstoSessionClient,
)


def _wire_html(components: list[dict], csrf_input: str = "csrf-token") -> str:
    parts = [f'<html><head></head><body><input type="hidden" name="_token" value="{csrf_input}">']
    for component in components:
        payload = {
            "fingerprint": component["fingerprint"],
            "serverMemo": component["serverMemo"],
            "effects": component.get("effects", {"listeners": []}),
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
        )
        parts.append(
            f'<div wire:id="{component["fingerprint"]["id"]}" wire:initial-data="{encoded}"></div>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def _actions_component(email: str | None = None, checksum: str = "actions-checksum", path: str = "/"):
    return {
        "fingerprint": {
            "id": "actions-id",
            "name": "frontend.actions",
            "locale": "en",
            "path": path,
            "method": "GET",
            "v": "acj",
        },
        "serverMemo": {
            "children": [],
            "errors": [],
            "htmlHash": "actions-hash",
            "data": {
                "in_app": email is not None,
                "user": None,
                "domain": "imail.edu.vn",
                "domains": ["imail.edu.vn", "mailer.edu.pl"],
                "email": email,
                "emails": [email] if email else [],
                "captcha": None,
            },
            "dataMeta": [],
            "checksum": checksum,
        },
    }


def _app_component(email: str, checksum: str = "app-checksum"):
    return {
        "fingerprint": {
            "id": "app-id",
            "name": "frontend.app",
            "locale": "en",
            "path": "mailbox",
            "method": "GET",
            "v": "acj",
        },
        "serverMemo": {
            "children": [],
            "errors": [],
            "htmlHash": "app-hash",
            "data": {
                "messages": [],
                "deleted": [],
                "error": "",
                "email": email,
                "initial": False,
                "overflow": False,
            },
            "dataMeta": [],
            "checksum": checksum,
        },
    }


class _FakeResponse:
    def __init__(self, *, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class _FakeHttpxClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append({"url": url, **kwargs})
        return self._responses.pop(0)

    def close(self):
        return None


class LivewireMailboxClientCompatibilityTests(unittest.TestCase):
    def test_livewire_client_accepts_hidden_token_input_for_imail(self):
        html_text = _wire_html([_actions_component(path="/")], csrf_input="csrf-imail")
        self.assertEqual(
            EduMailSessionClient._extract_csrf(html_text),
            "csrf-imail",
        )

    @patch("time.sleep", return_value=None)
    @patch("core.edumail_client.httpx.Client")
    def test_livewire_client_skips_blocked_default_domain_when_generating_random_email(
        self,
        mock_client_cls,
        _sleep,
    ):
        fake_client = _FakeHttpxClient(
            [
                _FakeResponse(
                    text=_wire_html([_actions_component(path="/")]),
                ),
                _FakeResponse(
                    json_data={
                        "effects": {"html": None, "dirty": ["domain"]},
                        "serverMemo": {
                            "data": {"domain": "mailer.edu.pl", "captcha": None},
                            "checksum": "actions-set-domain",
                        },
                    }
                ),
                _FakeResponse(
                    json_data={
                        "effects": {
                            "html": None,
                            "dirty": ["email"],
                            "redirect": "https://imail.edu.vn/mailbox",
                        },
                        "serverMemo": {"checksum": "actions-created"},
                    }
                ),
                _FakeResponse(
                    text=_wire_html(
                        [
                            _actions_component(
                                email="probe777@mailer.edu.pl",
                                checksum="actions-after",
                                path="mailbox",
                            ),
                            _app_component(email="probe777@mailer.edu.pl", checksum="app-after"),
                        ]
                    ),
                ),
            ]
        )
        mock_client_cls.return_value = fake_client

        client = EduMailSessionClient(base_url="https://imail.edu.vn")
        with patch("random.choice", return_value="mailer.edu.pl"):
            with patch.object(client, "_generate_local_part", return_value="probe777"):
                result = client.generate_random_email(blocked_domains={"imail.edu.vn"})

        self.assertEqual(result, "probe777@mailer.edu.pl")
        set_domain_update = fake_client.post_calls[0]["json"]["updates"][0]
        self.assertEqual(set_domain_update["payload"]["method"], "setDomain")
        self.assertEqual(set_domain_update["payload"]["params"], ["mailer.edu.pl"])
        create_updates = fake_client.post_calls[1]["json"]["updates"]
        self.assertEqual(create_updates[0]["type"], "syncInput")
        self.assertEqual(create_updates[1]["payload"]["method"], "create")


class BoomlifySessionClientTests(unittest.TestCase):
    @patch("core.web_mailbox_clients.httpx.Client")
    def test_generate_random_email_uses_public_domain_pool_without_provider_defaults(self, mock_client_cls):
        fake_client = _FakeHttpxClient(
            [
                _FakeResponse(
                    json_data=[
                        {"id": "domain-1", "domain": "bscse.okcx.edu.rs", "is_active": 1},
                    ]
                ),
                _FakeResponse(
                    json_data={"email": {"address": "abc123@bscse.okcx.edu.rs"}}
                ),
            ]
        )
        mock_client_cls.return_value = fake_client

        client = BoomlifySessionClient(api_base="https://v1.boomlify.com")
        with patch("core.web_mailbox_clients._generate_local_part", return_value="abc123"):
            result = client.generate_random_email()

        self.assertEqual(result, "abc123@bscse.okcx.edu.rs")
        self.assertEqual(
            fake_client.post_calls[0]["json"],
            {"email": "abc123@bscse.okcx.edu.rs", "domainId": "domain-1"},
        )


class NullstoSessionClientTests(unittest.TestCase):
    @patch("core.web_mailbox_clients.httpx.Client")
    def test_create_and_fetch_messages_via_supabase(self, mock_client_cls):
        fake_client = _FakeHttpxClient(
            [
                _FakeResponse(
                    text=(
                        '<html><head><script type="module" crossorigin '
                        'src="/assets/index-test.js"></script></head></html>'
                    )
                ),
                _FakeResponse(
                    text=(
                        'const SUPABASE_URL="https://yasbrindfmevbxysrgqd.supabase.co";'
                        'const SUPABASE_ANON_KEY="'
                        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                        "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inlhc2JyaW5kZm1ldmJ4eXNyZ3FkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njc3MjczNDMsImV4cCI6MjA4MzMwMzM0M30."
                        "K3dqUww7BkMDlDz4UxO5797Qp6wM87o0X6F0Pec41Jg"
                        '";'
                    )
                ),
                _FakeResponse(
                    json_data=[
                        {
                            "id": "domain-1",
                            "name": "@nullsto.edu.pl",
                            "is_active": True,
                        }
                    ]
                ),
                _FakeResponse(
                    json_data={
                        "success": True,
                        "email": {
                            "id": "email-1",
                            "address": "probe1@nullsto.edu.pl",
                            "secret_token": "secret-1",
                        },
                    }
                ),
                _FakeResponse(
                    json_data={
                        "emails": [
                            {
                                "id": "msg-1",
                                "subject": "Verification code 654321",
                                "from_email": "postmaster@gmail.com",
                            }
                        ]
                    }
                ),
            ]
        )
        mock_client_cls.return_value = fake_client

        client = NullstoSessionClient(base_url="https://nullsto.edu.pl")
        with patch("core.web_mailbox_clients._generate_local_part", return_value="probe1"):
            email_addr = client.generate_random_email()
        messages = client.get_messages(email_addr)

        self.assertEqual(email_addr, "probe1@nullsto.edu.pl")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "msg-1")
        self.assertIn("/rest/v1/rpc/create_temp_email", fake_client.post_calls[0]["url"])
        self.assertIn("/functions/v1/secure-email-access", fake_client.post_calls[1]["url"])


if __name__ == "__main__":
    unittest.main()
