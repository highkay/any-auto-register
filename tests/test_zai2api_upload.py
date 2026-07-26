import unittest
from unittest import mock

from platforms.zai.zai2api_upload import upload_to_zai2api


class DummyAccount:
    def __init__(self, *, email="user@example.com", token="zai-token", extra=None):
        self.email = email
        self.token = token
        self.extra = dict(extra or {})

    def get_extra(self):
        return dict(self.extra)


class Zai2ApiUploadTests(unittest.TestCase):
    def test_upload_posts_token_to_v1_tokens_with_bearer_auth(self):
        account = DummyAccount()
        session = mock.Mock()
        response = mock.Mock()
        response.status_code = 201
        response.json.return_value = {"ok": True}
        session.post.return_value = response

        ok, msg, detail = upload_to_zai2api(
            account,
            api_url="http://192.168.1.18:18082",
            auth_token="auth-token-123",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已导入 zai2api", msg)
        self.assertEqual(detail["tokens_url"], "http://192.168.1.18:18082/v1/tokens")
        session.post.assert_called_once_with(
            "http://192.168.1.18:18082/v1/tokens",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer auth-token-123",
                "Content-Type": "application/json",
            },
            json={"token": "zai-token"},
            timeout=15,
        )

    def test_missing_account_token_fails_fast(self):
        account = DummyAccount(token="")

        ok, msg, detail = upload_to_zai2api(
            account,
            api_url="http://192.168.1.18:18082",
            auth_token="auth-token-123",
        )

        self.assertFalse(ok)
        self.assertIn("缺少 bearer token", msg)
        self.assertEqual(detail["tokens_url"], "http://192.168.1.18:18082/v1/tokens")

    def test_upload_preserves_explicit_v1_tokens_url(self):
        account = DummyAccount()
        session = mock.Mock()
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        session.post.return_value = response

        ok, _msg, detail = upload_to_zai2api(
            account,
            api_url="http://192.168.1.18:18082/v1/tokens",
            auth_token="auth-token-123",
            session=session,
        )

        self.assertTrue(ok)
        self.assertEqual(detail["tokens_url"], "http://192.168.1.18:18082/v1/tokens")


if __name__ == "__main__":
    unittest.main()
