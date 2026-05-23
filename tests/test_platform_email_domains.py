import unittest

from core.base_mailbox import BaseMailbox, MailboxAccount, PlatformAwareMailbox
from core.platform_email_domains import (
    is_email_domain_blocked,
    resolve_platform_blocked_email_domains,
)


class DummyMailbox(BaseMailbox):
    def __init__(self, emails: list[str]):
        self._emails = list(emails)

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._emails.pop(0))

    def wait_for_code(self, account: MailboxAccount, *args, **kwargs):
        return "123456"

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()


class PlatformEmailDomainPolicyTests(unittest.TestCase):
    def test_resolve_platform_blocked_email_domains_preserves_deepseek_defaults(self):
        domains = resolve_platform_blocked_email_domains("deepseek", {})

        self.assertIn("apple.edu.pl", domains)
        self.assertIn("imail.edu.vn", domains)
        self.assertIn("bscse.okcx.edu.rs", domains)
        self.assertIn("mail.highkay.com", domains)
        self.assertIn("highkay.qzz.io", domains)
        self.assertIn("highlu.de", domains)
        self.assertIn("20210513.xyz", domains)
        self.assertIn("highkay.com", domains)
        self.assertIn("edumail.edu.rs", domains)
        self.assertIn("oxfor.edu.pl", domains)
        self.assertIn("zikzak.site", domains)
        self.assertIn("nondon.store", domains)
        self.assertIn("nullsto.edu.pl", domains)
        self.assertIn("io.vn", domains)
        self.assertIn("nik.edu.pl", domains)
        self.assertIn("mailer.edu.pl", domains)
        self.assertIn("gddp2018.edu.vn", domains)

    def test_resolve_platform_blocked_email_domains_preserves_grok_defaults(self):
        domains = resolve_platform_blocked_email_domains("grok", {})

        self.assertIn("nik.edu.pl", domains)
        self.assertIn("mailo.edu.pl", domains)
        self.assertIn("oxfor.edu.pl", domains)
        self.assertTrue(
            is_email_domain_blocked(
                "first@contact.oxfor.edu.pl",
                domains,
            )
        )

    def test_resolve_platform_blocked_email_domains_merges_configured_values(self):
        domains = resolve_platform_blocked_email_domains(
            "grok",
            {"grok_blocked_email_domains": " Bad.Example.com \nfoo.test,bar.test "},
        )

        self.assertEqual(
            domains,
            [
                "nik.edu.pl",
                "mailo.edu.pl",
                "oxfor.edu.pl",
                "bad.example.com",
                "foo.test",
                "bar.test",
            ],
        )

    def test_platform_aware_mailbox_skips_blocked_domains(self):
        mailbox = PlatformAwareMailbox(
            DummyMailbox(["first@sub.blocked.example", "second@allowed.example"]),
            platform="grok",
            blocked_domains=["blocked.example"],
        )

        account = mailbox.get_email()

        self.assertEqual(account.email, "second@allowed.example")

    def test_platform_aware_mailbox_blacklist_domain_applies_to_following_attempts(self):
        mailbox = PlatformAwareMailbox(
            DummyMailbox(
                [
                    "first@blocked.example",
                    "second@blocked.example",
                    "third@allowed.example",
                ]
            ),
            platform="grok",
        )
        mailbox.blacklist_domain("blocked.example", reason="Grok 注册页拒绝该邮箱域名")

        account = mailbox.get_email()

        self.assertEqual(account.email, "third@allowed.example")

    def test_email_domain_blocked_uses_suffix_match(self):
        self.assertTrue(
            is_email_domain_blocked(
                "first@mail.highkay.qzz.io",
                ["highkay.qzz.io"],
            )
        )
        self.assertTrue(
            is_email_domain_blocked(
                "first@sub.blocked.example",
                ["blocked.example"],
            )
        )
        self.assertFalse(
            is_email_domain_blocked(
                "first@notblocked.example",
                ["blocked.example"],
            )
        )


if __name__ == "__main__":
    unittest.main()
