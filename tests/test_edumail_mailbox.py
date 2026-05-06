import json
import unittest
from unittest import mock
from unittest.mock import patch

import httpx

from core.base_mailbox import EduMailMailbox, ImailMailbox, MailboxAccount
from core.edumail_client import EduMailSessionClient


def _wire_html(components: list[dict], csrf_token: str = "csrf-token") -> str:
    parts = [f'<html><head><meta name="csrf-token" content="{csrf_token}"></head><body>']
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
        parts.append(f'<div wire:id="{component["fingerprint"]["id"]}" wire:initial-data="{encoded}"></div>')
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
                "domain": "edumail.edu.rs",
                "domains": ["edumail.edu.rs", "inboxmail.biz"],
                "email": email,
                "emails": [email] if email else [],
                "captcha": None,
            },
            "dataMeta": [],
            "checksum": checksum,
        },
    }


def _app_component(email: str, checksum: str = "app-checksum", initial: bool = False):
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
                "initial": initial,
                "overflow": False,
            },
            "dataMeta": [],
            "checksum": checksum,
        },
    }


class _FakeResponse:
    def __init__(self, *, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

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


class EduMailSessionClientTests(unittest.TestCase):
    @patch("time.sleep", return_value=None)
    @patch("core.edumail_client.httpx.Client")
    def test_generate_random_email_uses_livewire_random_then_mailbox_page(
        self,
        mock_client_cls,
        _sleep,
    ):
        email = "demo123@inboxmail.biz"
        fake_client = _FakeHttpxClient(
            [
                _FakeResponse(
                    text=_wire_html([_actions_component(path="/")]),
                ),
                _FakeResponse(
                    json_data={
                        "effects": {"html": None, "dirty": ["email"], "redirect": "https://edumail.su/mailbox"},
                        "serverMemo": {"checksum": "actions-random"},
                    }
                ),
                _FakeResponse(
                    text=_wire_html(
                        [
                            _actions_component(email=email, checksum="actions-after", path="mailbox"),
                            _app_component(email=email, checksum="app-after"),
                        ]
                    ),
                ),
            ]
        )
        mock_client_cls.return_value = fake_client

        client = EduMailSessionClient(base_url="https://edumail.su")
        result = client.generate_random_email()

        self.assertEqual(result, email)
        self.assertEqual(fake_client.get_calls[0]["url"], "https://edumail.su/")
        self.assertEqual(fake_client.get_calls[1]["url"], "https://edumail.su/mailbox")
        random_update = fake_client.post_calls[0]["json"]["updates"][0]
        self.assertEqual(random_update["type"], "callMethod")
        self.assertEqual(random_update["payload"]["method"], "random")

    @patch("time.sleep", return_value=None)
    @patch("core.edumail_client.httpx.Client")
    def test_get_messages_refreshes_mailbox_state_before_fetch(
        self,
        mock_client_cls,
        _sleep,
    ):
        email = "demo123@inboxmail.biz"
        fake_client = _FakeHttpxClient(
            [
                _FakeResponse(
                    text=_wire_html(
                        [
                            _actions_component(email=email, checksum="actions-initial", path="mailbox"),
                            _app_component(email=email, checksum="app-initial"),
                        ]
                    ),
                ),
                _FakeResponse(
                    json_data={
                        "effects": {"html": None, "dirty": []},
                        "serverMemo": {"checksum": "actions-synced"},
                    }
                ),
                _FakeResponse(
                    text=_wire_html(
                        [
                            _actions_component(email=email, checksum="actions-after-sync", path="mailbox"),
                            _app_component(email=email, checksum="app-after-sync", initial=True),
                        ]
                    ),
                ),
                _FakeResponse(
                    json_data={
                        "effects": {"html": None, "dirty": ["messages"]},
                        "serverMemo": {
                            "data": {
                                "messages": [
                                    {
                                        "id": 1001,
                                        "subject": "EduMail probe 654321",
                                        "sender_email": "postmaster@gmail.com",
                                        "content": "Verification code: 654321<br/>",
                                    }
                                ]
                            }
                        },
                    }
                ),
            ]
        )
        mock_client_cls.return_value = fake_client

        client = EduMailSessionClient(base_url="https://edumail.su")
        messages = client.get_messages(email)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], 1001)
        self.assertEqual(fake_client.get_calls[0]["url"], "https://edumail.su/mailbox")
        self.assertEqual(fake_client.get_calls[1]["url"], "https://edumail.su/mailbox")
        action_updates = fake_client.post_calls[0]["json"]["updates"]
        self.assertEqual(action_updates[0]["payload"]["event"], "syncEmail")
        app_updates = fake_client.post_calls[1]["json"]["updates"]
        self.assertEqual(app_updates[0]["payload"]["event"], "syncEmail")
        self.assertEqual(app_updates[1]["payload"]["event"], "fetchMessages")
        self.assertEqual(
            fake_client.post_calls[1]["json"]["serverMemo"]["checksum"],
            "app-after-sync",
        )

    @patch("time.sleep", return_value=None)
    @patch("core.edumail_client.httpx.Client")
    def test_generate_random_email_can_lock_specific_domain_via_setdomain_and_create(
        self,
        mock_client_cls,
        _sleep,
    ):
        email = "customname@inboxmail.biz"
        fake_client = _FakeHttpxClient(
            [
                _FakeResponse(
                    text=_wire_html([_actions_component(path="/")]),
                ),
                _FakeResponse(
                    json_data={
                        "effects": {"html": None, "dirty": ["domain"]},
                        "serverMemo": {
                            "data": {"domain": "inboxmail.biz", "captcha": None},
                            "checksum": "actions-set-domain",
                        },
                    }
                ),
                _FakeResponse(
                    json_data={
                        "effects": {"html": None, "dirty": ["email"], "redirect": "https://edumail.su/mailbox"},
                        "serverMemo": {"checksum": "actions-created"},
                    }
                ),
                _FakeResponse(
                    text=_wire_html(
                        [
                            _actions_component(email=email, checksum="actions-after", path="mailbox"),
                            _app_component(email=email, checksum="app-after"),
                        ]
                    ),
                ),
            ]
        )
        mock_client_cls.return_value = fake_client

        client = EduMailSessionClient(base_url="https://edumail.su")
        with patch.object(client, "_generate_local_part", return_value="customname"):
            result = client.generate_random_email(domain="inboxmail.biz")

        self.assertEqual(result, email)
        set_domain_update = fake_client.post_calls[0]["json"]["updates"][0]
        self.assertEqual(set_domain_update["payload"]["method"], "setDomain")
        self.assertEqual(set_domain_update["payload"]["params"], ["inboxmail.biz"])
        create_updates = fake_client.post_calls[1]["json"]["updates"]
        self.assertEqual(create_updates[0]["type"], "syncInput")
        self.assertEqual(create_updates[0]["payload"]["value"], "customname")
        self.assertEqual(create_updates[1]["payload"]["method"], "create")
        self.assertEqual(
            fake_client.post_calls[1]["json"]["serverMemo"]["checksum"],
            "actions-set-domain",
        )


class EduMailMailboxTests(unittest.TestCase):
    @patch("time.sleep", return_value=None)
    def test_wait_for_code_skips_excluded_code_and_returns_next(self, _sleep):
        mailbox = EduMailMailbox(api_url="https://edumail.su")
        fake_client = mock.Mock()
        fake_client.get_messages.side_effect = [
            [
                {
                    "id": 1,
                    "subject": "Your code 111111",
                    "sender_email": "postmaster@gmail.com",
                    "content": "Verification code: 111111<br/>",
                }
            ],
            [
                {
                    "id": 1,
                    "subject": "Your code 111111",
                    "sender_email": "postmaster@gmail.com",
                    "content": "Verification code: 111111<br/>",
                },
                {
                    "id": 2,
                    "subject": "Your code 222222",
                    "sender_email": "postmaster@gmail.com",
                    "content": "Verification code: 222222<br/>",
                },
            ],
        ]

        with patch.object(mailbox, "_get_client", return_value=fake_client):
            code = mailbox.wait_for_code(
                MailboxAccount(email="demo123@inboxmail.biz"),
                timeout=5,
                exclude_codes={"111111"},
            )

        self.assertEqual(code, "222222")

    @patch("time.sleep", return_value=None)
    def test_wait_for_code_logs_deduplicated_poll_errors_before_success(self, _sleep):
        mailbox = EduMailMailbox(api_url="https://edumail.su")
        fake_client = mock.Mock()
        fake_client.get_messages.side_effect = [
            RuntimeError("mailbox 500"),
            RuntimeError("mailbox 500"),
            [
                {
                    "id": 2,
                    "subject": "Your code 222222",
                    "sender_email": "postmaster@gmail.com",
                    "content": "Verification code: 222222<br/>",
                },
            ],
        ]
        logs: list[str] = []
        mailbox._log_fn = logs.append

        with patch.object(mailbox, "_get_client", return_value=fake_client):
            code = mailbox.wait_for_code(
                MailboxAccount(email="demo123@inboxmail.biz"),
                timeout=5,
            )

        self.assertEqual(code, "222222")
        self.assertEqual(
            [entry for entry in logs if "拉取收件箱失败" in entry],
            ["[EduMail] 拉取收件箱失败: RuntimeError: mailbox 500"],
        )

    def test_get_current_ids_hashes_message_ids_from_client(self):
        mailbox = EduMailMailbox(api_url="https://edumail.su")
        fake_client = mock.Mock()
        fake_client.get_messages.return_value = [
            {"id": 1001, "subject": "one"},
            {"message_id": "m-2", "subject": "two"},
        ]

        with patch.object(mailbox, "_get_client", return_value=fake_client):
            ids = mailbox.get_current_ids(MailboxAccount(email="demo123@inboxmail.biz"))

        self.assertEqual(ids, {"1001", "m-2"})

    def test_imail_mailbox_uses_default_blocked_domains_when_domain_not_specified(self):
        mailbox = ImailMailbox(api_url="https://imail.edu.vn")
        fake_client = mock.Mock()
        fake_client.generate_random_email.return_value = "demo123@mailer.edu.pl"

        with patch.object(mailbox, "_get_client", return_value=fake_client):
            account = mailbox.get_email()

        self.assertEqual(account.email, "demo123@mailer.edu.pl")
        fake_client.generate_random_email.assert_called_once_with(
            domain="",
            blocked_domains={"apple.edu.pl", "imail.edu.vn"},
        )


if __name__ == "__main__":
    unittest.main()
