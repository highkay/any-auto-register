import unittest

from platforms.deepseek.core import (
    _classify_deepseek_sign_up_state,
    _summarize_deepseek_sign_up_state,
)


class DeepSeekCoreTests(unittest.TestCase):
    def test_classify_deepseek_sign_up_state_detects_email_form(self):
        state = {
            "body": "DeepSeek sign up",
            "inputs": [
                {"type": "email"},
                {"type": "password"},
                {"type": "password"},
                {"type": "tel"},
            ],
            "buttons": [
                {"className": "ds-link-button ds-verify-code-input-countdown"},
            ],
        }

        self.assertEqual(_classify_deepseek_sign_up_state(state), "email_form")

    def test_classify_deepseek_sign_up_state_detects_phone_only_branch(self):
        state = {
            "title": "DeepSeek - Into the Unknown",
            "url": "https://chat.deepseek.com/sign_up?locale=en-US",
            "body": (
                "Only phone number registration is supported in your region.\n"
                "+86\nSend code\nSign up"
            ),
            "inputs": [
                {"type": "tel"},
                {"type": "password"},
                {"type": "password"},
                {"type": "tel"},
            ],
            "buttons": [
                {"className": "ds-link-button ds-verify-code-input-countdown"},
            ],
        }

        self.assertEqual(_classify_deepseek_sign_up_state(state), "phone_only")
        summary = _summarize_deepseek_sign_up_state(state, classification="phone_only")
        self.assertIn("classification=phone_only", summary)
        self.assertIn("Only phone number registration is supported", summary)


if __name__ == "__main__":
    unittest.main()
