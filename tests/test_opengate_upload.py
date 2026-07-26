import unittest
from unittest import mock

from platforms.qwen.opengate_upload import upload_to_opengate


class DummyAccount:
    def __init__(self, *, email="user@example.com", password="Secret123!", extra=None):
        self.email = email
        self.password = password
        self.extra = dict(extra or {})

    def get_extra(self):
        return dict(self.extra)


class OpenGateUploadTests(unittest.TestCase):
    def test_upload_posts_email_password_with_bearer_auth(self):
        account = DummyAccount()
        session = mock.Mock()
        response = mock.Mock()
        response.status_code = 201
        response.text = ""
        response.json.return_value = {
            "success": True,
            "email": "user@example.com",
            "loginSucceeded": True,
            "loginError": None,
        }
        session.post.return_value = response

        ok, msg, detail = upload_to_opengate(
            account,
            api_url="http://192.168.1.18:7860",
            api_key="sk-test",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("OpenGate", msg)
        self.assertIn("登录成功", msg)
        self.assertEqual(detail["accounts_url"], "http://192.168.1.18:7860/api/accounts")
        session.post.assert_called_once_with(
            "http://192.168.1.18:7860/api/accounts",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer sk-test",
            },
            json={"email": "user@example.com", "password": "Secret123!"},
            timeout=60,
        )

    def test_dashboard_url_normalized_to_api_accounts(self):
        account = DummyAccount()
        session = mock.Mock()
        response = mock.Mock()
        response.status_code = 201
        response.text = ""
        response.json.return_value = {"success": True, "loginSucceeded": False}
        session.post.return_value = response

        ok, msg, detail = upload_to_opengate(
            account,
            api_url="http://192.168.1.18:7860/dashboard",
            api_key="sk-test",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已导入 OpenGate", msg)
        self.assertEqual(detail["accounts_url"], "http://192.168.1.18:7860/api/accounts")

    def test_conflict_treated_as_success(self):
        account = DummyAccount()
        session = mock.Mock()
        response = mock.Mock()
        response.status_code = 409
        response.text = "already exists"
        response.json.return_value = {"error": {"message": "Account already exists"}}
        session.post.return_value = response

        ok, msg, detail = upload_to_opengate(
            account,
            api_url="http://192.168.1.18:7860",
            api_key="sk-test",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已存在", msg)
        self.assertEqual(detail["status_code"], 409)

    def test_missing_password_fails_fast(self):
        account = DummyAccount(password="")
        ok, msg, detail = upload_to_opengate(
            account,
            api_url="http://192.168.1.18:7860",
            api_key="sk-test",
        )
        self.assertFalse(ok)
        self.assertIn("password", msg.lower())
        self.assertEqual(detail["accounts_url"], "http://192.168.1.18:7860/api/accounts")


if __name__ == "__main__":
    unittest.main()
