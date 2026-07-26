"""Magic-link mailbox adapters (do not strip URLs)."""
from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

from core import mailbox_links as ml


@dataclass
class FakeAccount:
    email: str = "user@example.com"
    account_id: str = ""


class FakeCFWorker:
    provider = "cfworker"

    def __init__(self, mails):
        self._mails = mails

    def _get_mails(self, email: str):
        return list(self._mails)

    def _decode_raw_content(self, raw):
        return str(raw)


class FakeMaliAPI:
    provider = "maliapi"

    def __init__(self, messages, details=None):
        self._messages = messages
        self._details = details or {}

    def _list_messages(self, account):
        return list(self._messages)

    def _get_message_detail(self, message_id):
        return self._details.get(str(message_id), {})


class FakeNoHooks:
    provider = "temp-inline-only"


class MailboxLinksTest(unittest.TestCase):
    def test_find_link_keeps_url(self):
        body = "Click https://claude.ai/magic-link#abc_DEF-123:=+/zz to continue"
        link = ml.find_magic_link_in_texts([body], ml.CLAUDE_MAGIC_LINK_REGEX)
        self.assertEqual(link, "https://claude.ai/magic-link#abc_DEF-123:=+/zz")

    def test_adapter_a_cfworker(self):
        mailbox = FakeCFWorker(
            [
                {
                    "id": "1",
                    "subject": "Verify",
                    "raw": "open https://claude.ai/magic-link#tok123 end",
                }
            ]
        )
        views = ml.iter_mail_message_views(mailbox, FakeAccount(), limit=5)
        self.assertEqual(len(views), 1)
        joined = "\n".join(views[0].texts)
        self.assertIn("https://claude.ai/magic-link#tok123", joined)

    def test_adapter_c_maliapi_detail(self):
        mailbox = FakeMaliAPI(
            messages=[{"id": "m1", "subject": "hi"}],
            details={
                "m1": {
                    "body": "go https://claude.ai/magic-link#from-detail now",
                }
            },
        )
        views = ml.iter_mail_message_views(mailbox, FakeAccount(), limit=5)
        self.assertTrue(any("from-detail" in t for t in views[0].texts))

    def test_unsupported_raises(self):
        with self.assertRaises(ml.UnsupportedMailboxForLinksError):
            ml.iter_mail_message_views(FakeNoHooks(), FakeAccount())

    def test_supports_magic_link(self):
        self.assertTrue(ml.supports_magic_link(FakeCFWorker([])))
        self.assertFalse(ml.supports_magic_link(FakeNoHooks()))
        self.assertFalse(ml.supports_magic_link(None))

    def test_wait_for_magic_link(self):
        mailbox = FakeCFWorker(
            [
                {
                    "id": "new-1",
                    "subject": "Claude",
                    "raw": "https://claude.ai/magic-link#waited",
                }
            ]
        )
        # before_ids empty set → scan all
        link = ml.wait_for_magic_link(
            mailbox,
            FakeAccount(),
            link_regex=ml.CLAUDE_MAGIC_LINK_REGEX,
            timeout=2,
            before_ids=set(),
            poll_interval=0.1,
            log=lambda *_: None,
        )
        self.assertIn("waited", link)

    def test_wait_timeout(self):
        mailbox = FakeCFWorker([{"id": "1", "subject": "x", "raw": "no link here"}])
        with self.assertRaises(TimeoutError):
            ml.wait_for_magic_link(
                mailbox,
                FakeAccount(),
                link_regex=ml.CLAUDE_MAGIC_LINK_REGEX,
                timeout=1,
                before_ids=set(),
                poll_interval=0.2,
                log=lambda *_: None,
            )

    def test_checkpoint_called(self):
        control = MagicMock()
        mailbox = FakeCFWorker(
            [{"id": "1", "raw": "https://claude.ai/magic-link#cp"}]
        )
        ml.wait_for_magic_link(
            mailbox,
            FakeAccount(),
            link_regex=ml.CLAUDE_MAGIC_LINK_REGEX,
            timeout=2,
            before_ids=set(),
            poll_interval=0.1,
            task_control=control,
            log=lambda *_: None,
        )
        self.assertTrue(control.checkpoint.called)

    def test_hook_f(self):
        class Hooked:
            provider = "custom"

            def iter_mail_message_views(self, account):
                return [
                    ml.MailMessageView(
                        id="h1",
                        texts=["https://claude.ai/magic-link#hooked"],
                    )
                ]

        views = ml.iter_mail_message_views(Hooked(), FakeAccount())
        self.assertEqual(views[0].id, "h1")


if __name__ == "__main__":
    unittest.main()
