import unittest
from unittest import mock

from services.grok2api_runtime import verify_grok2api


class Grok2ApiRuntimeTests(unittest.TestCase):
    def test_verify_retries_admin_key_without_api_key_prefix(self):
        first = mock.Mock()
        first.status_code = 401
        first.text = '{"detail":"Invalid authentication token."}'
        second = mock.Mock()
        second.status_code = 200
        second.text = '{"status":"success"}'

        with mock.patch(
            "services.grok2api_runtime.requests.get",
            side_effect=[first, second],
        ) as get_mock:
            ok, msg = verify_grok2api(
                api_url="http://grok2api.test",
                app_key="sk-admin-key",
            )

        self.assertTrue(ok)
        self.assertEqual(msg, "grok2api 鉴权正常")
        self.assertEqual(get_mock.call_count, 2)
        self.assertEqual(
            get_mock.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer sk-admin-key",
        )
        self.assertEqual(
            get_mock.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer admin-key",
        )


if __name__ == "__main__":
    unittest.main()
