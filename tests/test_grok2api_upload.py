import unittest
from unittest import mock

from core.db import AccountModel
from platforms.grok.grok2api_upload import (
    _extract_sso,
    _normalize_pool_name,
    upload_to_grok2api,
)


class Grok2ApiUploadTests(unittest.TestCase):
    def test_extract_sso_reads_account_model_extra_json(self):
        account = AccountModel(
            platform="grok",
            email="user@example.com",
            password="pw",
            token="",
            extra_json='{"sso":"sso-value","sso_rw":"rw-value"}',
        )

        self.assertEqual(_extract_sso(account), "sso-value")

    def test_upload_uses_admin_api_tokens_add(self):
        account = AccountModel(
            platform="grok",
            email="user@example.com",
            password="pw",
            token="",
            extra_json='{"sso":"sso-value"}',
        )
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"count": 1, "skipped": 0}

        with mock.patch(
            "platforms.grok.grok2api_upload.cffi_requests.post",
            return_value=response,
        ) as post_mock:
            ok, msg = upload_to_grok2api(
                account,
                api_url="http://grok2api.test",
                app_key="admin-key",
                pool_name="ssoBasic",
            )

        self.assertTrue(ok)
        self.assertIn("导入成功", msg)
        post_mock.assert_called_once()
        self.assertEqual(
            post_mock.call_args.args[0],
            "http://grok2api.test/admin/api/tokens/add",
        )
        self.assertEqual(
            post_mock.call_args.kwargs["json"],
            {"pool": "basic", "tokens": ["sso-value"]},
        )

    def test_upload_retries_admin_key_without_api_key_prefix(self):
        account = AccountModel(
            platform="grok",
            email="user@example.com",
            password="pw",
            token="",
            extra_json='{"sso":"sso-value"}',
        )
        auth_failed = mock.Mock()
        auth_failed.status_code = 401
        auth_failed.json.return_value = {"detail": "Invalid authentication token."}
        ok_response = mock.Mock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"count": 1, "skipped": 0}

        with mock.patch(
            "platforms.grok.grok2api_upload.cffi_requests.post",
            side_effect=[auth_failed, ok_response],
        ) as post_mock:
            ok, msg = upload_to_grok2api(
                account,
                api_url="http://grok2api.test",
                app_key="sk-admin-key",
                pool_name="basic",
            )

        self.assertTrue(ok)
        self.assertIn("导入成功", msg)
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(
            post_mock.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer sk-admin-key",
        )
        self.assertEqual(
            post_mock.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer admin-key",
        )

    def test_normalize_legacy_pool_names(self):
        self.assertEqual(_normalize_pool_name("ssoBasic"), "basic")
        self.assertEqual(_normalize_pool_name("ssoSuper"), "super")
        self.assertEqual(_normalize_pool_name("heavy"), "heavy")


if __name__ == "__main__":
    unittest.main()
