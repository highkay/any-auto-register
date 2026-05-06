import unittest
from unittest import mock

from platforms.deepseek.ds2api_upload import upload_to_ds2api


class DummyAccount:
    def __init__(self, *, email="deepseek@example.com", password="Aa1!demoPass", extra=None):
        self.email = email
        self.password = password
        self.extra = dict(extra or {})

    def get_extra(self):
        return dict(self.extra)


class DeepSeekDs2ApiUploadTests(unittest.TestCase):
    def test_upload_uses_admin_accounts_endpoint_and_bearer_key(self):
        account = DummyAccount()
        session = mock.Mock()
        add_response = mock.Mock()
        add_response.status_code = 200
        add_response.json.return_value = {"success": True, "total_accounts": 6}
        test_response = mock.Mock()
        test_response.status_code = 200
        test_response.json.return_value = {
            "account": "deepseek@example.com",
            "success": True,
            "message": "Token 刷新成功（登录与会话创建成功）",
            "session_count": 0,
        }
        session.post.side_effect = [add_response, test_response]

        ok, msg, detail = upload_to_ds2api(
            account,
            api_url="http://ds2api.local/admin",
            admin_key="highkay1844",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已导入并刷新 token", msg)
        self.assertEqual(detail["identifier"], "deepseek@example.com")
        self.assertTrue(detail["refresh_success"])
        self.assertEqual(detail["refresh_status_code"], 200)
        session.post.assert_has_calls(
            [
                mock.call(
                    "http://ds2api.local/admin/accounts",
                    headers={
                        "Accept": "application/json",
                        "Authorization": "Bearer highkay1844",
                        "Content-Type": "application/json",
                    },
                    json={"email": "deepseek@example.com", "password": "Aa1!demoPass"},
                    timeout=15,
                ),
                mock.call(
                    "http://ds2api.local/admin/accounts/test",
                    headers={
                        "Accept": "application/json",
                        "Authorization": "Bearer highkay1844",
                        "Content-Type": "application/json",
                    },
                    json={"identifier": "deepseek@example.com"},
                    timeout=15,
                ),
            ]
        )

    def test_duplicate_email_is_treated_as_success(self):
        account = DummyAccount()
        session = mock.Mock()
        add_response = mock.Mock()
        add_response.status_code = 400
        add_response.json.return_value = {"detail": "邮箱已存在"}
        test_response = mock.Mock()
        test_response.status_code = 200
        test_response.json.return_value = {
            "account": "deepseek@example.com",
            "success": True,
            "message": "Token 刷新成功（登录与会话创建成功）",
            "session_count": 0,
        }
        session.post.side_effect = [add_response, test_response]

        ok, msg, detail = upload_to_ds2api(
            account,
            api_url="http://ds2api.local/admin",
            admin_key="highkay1844",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已存在并刷新 token", msg)
        self.assertEqual(detail["add_status_code"], 400)
        self.assertTrue(detail["refresh_success"])

    def test_missing_password_fails_fast(self):
        account = DummyAccount(password="")

        ok, msg, detail = upload_to_ds2api(
            account,
            api_url="http://ds2api.local/admin",
            admin_key="highkay1844",
        )

        self.assertFalse(ok)
        self.assertIn("缺少 password", msg)
        self.assertEqual(detail["accounts_url"], "http://ds2api.local/admin/accounts")

    def test_refresh_failure_makes_whole_upload_fail(self):
        account = DummyAccount()
        session = mock.Mock()
        add_response = mock.Mock()
        add_response.status_code = 200
        add_response.json.return_value = {"success": True, "total_accounts": 6}
        test_response = mock.Mock()
        test_response.status_code = 200
        test_response.json.return_value = {
            "account": "deepseek@example.com",
            "success": False,
            "message": "登录失败: PASSWORD_OR_USER_NAME_IS_WRONG",
            "session_count": 0,
        }
        session.post.side_effect = [add_response, test_response]

        ok, msg, detail = upload_to_ds2api(
            account,
            api_url="http://ds2api.local/admin",
            admin_key="highkay1844",
            session=session,
        )

        self.assertFalse(ok)
        self.assertIn("刷新 token 失败", msg)
        self.assertFalse(detail["refresh_success"])


if __name__ == "__main__":
    unittest.main()
