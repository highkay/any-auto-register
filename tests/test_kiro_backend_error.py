import unittest

from platforms.kiro.core import KiroRegister


class KiroBackendErrorTests(unittest.TestCase):
    def test_format_send_otp_blocked_error(self):
        reg = KiroRegister()

        message = reg._format_aws_backend_error(
            "https://profile.aws.amazon.com/api/send-otp",
            400,
            {"errorCode": "BLOCKED", "message": "Request was blocked by TES."},
        )

        self.assertEqual(
            message,
            "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
        )

    def test_format_nested_execute_error(self):
        reg = KiroRegister()

        message = reg._format_aws_backend_error(
            "https://us-east-1.signin.aws/platform/d-9067642ac7/api/execute",
            400,
            {
                "message": {
                    "heading": "An unexpected error has occurred",
                    "text": "Please try signing in again. If the error persists, please contact your administrator",
                    "errorCode": "ENTITY_DOES_NOT_EXIST",
                }
            },
        )

        self.assertEqual(
            message,
            "AWS api/execute 失败 [HTTP 400 / ENTITY_DOES_NOT_EXIST]: "
            "An unexpected error has occurred: Please try signing in again. "
            "If the error persists, please contact your administrator",
        )

    def test_merge_prefers_backend_error_over_generic_page_alert(self):
        merged = KiroRegister._merge_stage_error(
            "Sorry, there was an error processing your request. Please try again.",
            "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
        )

        self.assertEqual(
            merged,
            "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
        )

    def test_recent_backend_error_respects_since_index(self):
        reg = KiroRegister()
        reg._network_debug = [
            {
                "type": "response",
                "ts": 10.0,
                "url": "https://us-east-1.signin.aws/platform/d-9067642ac7/api/execute",
                "status": 400,
                "backend_error": "AWS api/execute 失败 [HTTP 400 / ENTITY_DOES_NOT_EXIST]: old",
            },
            {
                "type": "response",
                "ts": 20.0,
                "url": "https://profile.aws.amazon.com/api/send-otp",
                "status": 400,
                "backend_error": "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
            },
        ]

        self.assertEqual(
            reg._get_recent_aws_backend_error(since_index=1),
            "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
        )

    def test_recent_backend_error_falls_back_to_recent_last_error(self):
        reg = KiroRegister()
        reg._network_debug = []
        reg._last_aws_backend_error = {
            "message": "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
            "url": "https://profile.aws.amazon.com/api/send-otp",
            "status": 400,
            "ts": 30.0,
        }

        self.assertEqual(
            reg._get_recent_aws_backend_error(since_index=5, since_ts=25.0),
            "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
        )

    def test_recent_backend_error_ignores_stale_last_error(self):
        reg = KiroRegister()
        reg._network_debug = []
        reg._last_aws_backend_error = {
            "message": "AWS send-otp 失败 [HTTP 400 / BLOCKED]: Request was blocked by TES.",
            "url": "https://profile.aws.amazon.com/api/send-otp",
            "status": 400,
            "ts": 15.0,
        }

        self.assertEqual(
            reg._get_recent_aws_backend_error(since_index=0, since_ts=20.0),
            "",
        )


if __name__ == "__main__":
    unittest.main()
