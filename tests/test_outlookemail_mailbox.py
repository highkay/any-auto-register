import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import SQLModel, create_engine

from core.base_mailbox import MailboxAccount, OutlookEmailMailbox, create_mailbox


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, handler):
        self._handler = handler
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self._handler("GET", url, params=params, headers=headers)

    def post(self, url, json=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "json": json,
                "timeout": timeout,
            }
        )
        return self._handler("POST", url, json=json)


class OutlookEmailMailboxTests(unittest.TestCase):
    def setUp(self):
        OutlookEmailMailbox._leased_emails_by_pool.clear()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self._test_engine = create_engine(
            f"sqlite:///{Path(self._tmp_dir.name) / 'outlookemail.db'}"
        )
        SQLModel.metadata.create_all(self._test_engine)
        self._engine_patcher = mock.patch("core.db.engine", self._test_engine)
        self._engine_patcher.start()
        self.addCleanup(self._engine_patcher.stop)
        self.addCleanup(self._test_engine.dispose)

    def _config(self):
        return {
            "outlookemail_base_url": "http://outlookemail.example",
            "outlookemail_password": "admin123",
            "outlookemail_api_key": "api-key",
            "outlookemail_group_id": "1",
        }

    def test_release_current_account_allows_reuse(self):
        accounts_payload = {
            "success": True,
            "accounts": [
                {"id": 1, "email": "first@example.com"},
                {"id": 2, "email": "second@example.com"},
            ],
        }

        def handler(method, url, **kwargs):
            if method == "GET" and url.endswith("/api/external/accounts"):
                return _FakeResponse(accounts_payload)
            raise AssertionError(f"unexpected call: {method} {url}")

        session = _FakeSession(handler)
        with mock.patch("core.base_mailbox.create_mailbox_requests_session", return_value=session):
            mailbox = create_mailbox("outlookemail", extra=self._config(), platform="grok")
            account = mailbox.get_email()
            self.assertEqual(account.email, "first@example.com")

            mailbox.release_current_account()

            another_mailbox = create_mailbox("outlookemail", extra=self._config(), platform="grok")
            another_account = another_mailbox.get_email()
            self.assertEqual(another_account.email, "first@example.com")

    def test_mark_current_account_registered_persists_platform_blacklist_only_for_same_platform(self):
        accounts_payload = {
            "success": True,
            "accounts": [
                {"id": 1, "email": "first@example.com"},
                {"id": 2, "email": "second@example.com"},
            ],
        }

        def handler(method, url, **kwargs):
            if method == "GET" and url.endswith("/api/external/accounts"):
                return _FakeResponse(accounts_payload)
            raise AssertionError(f"unexpected call: {method} {url}")

        session = _FakeSession(handler)
        with mock.patch("core.base_mailbox.create_mailbox_requests_session", return_value=session):
            mailbox = create_mailbox("outlookemail", extra=self._config(), platform="grok")
            account = mailbox.get_email()
            self.assertEqual(account.email, "first@example.com")
            mailbox.mark_current_account_registered()

            same_platform = create_mailbox("outlookemail", extra=self._config(), platform="grok")
            same_platform_account = same_platform.get_email()
            self.assertEqual(same_platform_account.email, "second@example.com")

            other_platform = create_mailbox("outlookemail", extra=self._config(), platform="deepseek")
            other_platform_account = other_platform.get_email()
            self.assertEqual(other_platform_account.email, "first@example.com")

    @mock.patch("time.sleep", return_value=None)
    def test_wait_for_code_fetches_detail_and_skips_excluded_codes(self, _sleep):
        def handler(method, url, **kwargs):
            if method == "POST" and url.endswith("/login"):
                return _FakeResponse({"success": True, "message": "登录成功"})
            if method == "GET" and url.endswith("/api/external/emails"):
                return _FakeResponse(
                    {
                        "success": True,
                        "emails": [
                            {
                                "id": "m1",
                                "subject": "Your verification code",
                                "body_preview": "111111",
                                "from": "first@example.com",
                                "date": "2026-05-23T12:00:00Z",
                            },
                            {
                                "id": "m2",
                                "subject": "Your verification code",
                                "body_preview": "",
                                "from": "second@example.com",
                                "date": "2026-05-23T12:01:00Z",
                            },
                        ],
                    }
                )
            if method == "GET" and url.endswith("/api/email/demo@example.com/m1"):
                return _FakeResponse(
                    {
                        "success": True,
                        "email": {
                            "subject": "Your verification code",
                            "from": "first@example.com",
                            "body": "<div>111111</div>",
                        },
                    }
                )
            if method == "GET" and url.endswith("/api/email/demo@example.com/m2"):
                return _FakeResponse(
                    {
                        "success": True,
                        "email": {
                            "subject": "Your verification code",
                            "from": "second@example.com",
                            "body": "<div>Use 222222 to finish sign in</div>",
                        },
                    }
                )
            raise AssertionError(f"unexpected call: {method} {url}")

        session = _FakeSession(handler)
        with mock.patch("core.base_mailbox.create_mailbox_requests_session", return_value=session):
            mailbox = create_mailbox("outlookemail", extra=self._config(), platform="grok")
            code = mailbox.wait_for_code(
                MailboxAccount(email="demo@example.com"),
                timeout=5,
                exclude_codes={"111111"},
            )

        self.assertEqual(code, "222222")
        self.assertEqual(len(session.post_calls), 1)

    def test_get_current_ids_reads_external_emails(self):
        def handler(method, url, **kwargs):
            if method == "GET" and url.endswith("/api/external/emails"):
                return _FakeResponse(
                    {
                        "success": True,
                        "emails": [
                            {"id": "m1"},
                            {"id": "m2"},
                        ],
                    }
                )
            raise AssertionError(f"unexpected call: {method} {url}")

        session = _FakeSession(handler)
        with mock.patch("core.base_mailbox.create_mailbox_requests_session", return_value=session):
            mailbox = create_mailbox("outlookemail", extra=self._config(), platform="grok")
            ids = mailbox.get_current_ids(MailboxAccount(email="demo@example.com"))

        self.assertEqual(ids, {"m1", "m2"})


if __name__ == "__main__":
    unittest.main()
