import unittest

from core.base_mailbox import BaseMailbox, MailboxAccount


class _HelperMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        raise NotImplementedError

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()


class MailboxTextHelperTests(unittest.TestCase):
    def setUp(self):
        self.mailbox = _HelperMailbox()

    def test_safe_extract_ignores_url_digits_for_simple_six_digit_pattern(self):
        text = (
            "Track https://example.com/u20216706?ref=654321 before you continue. "
            "Use 112233 to finish sign in."
        )

        code = self.mailbox._safe_extract(text, r"\d{6}")

        self.assertEqual(code, "112233")

    def test_decode_raw_content_keeps_plain_text_without_mail_headers(self):
        raw = "Line one\n\nVerification code: 654321"

        decoded = self.mailbox._decode_raw_content(raw)

        self.assertEqual(decoded, "Line one Verification code: 654321")


if __name__ == "__main__":
    unittest.main()
