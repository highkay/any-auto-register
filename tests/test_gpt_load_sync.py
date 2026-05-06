import unittest
from unittest import mock

from services.gpt_load_sync import (
    upload_account_to_gpt_load,
    upload_cerebras_account_to_gpt_load,
    upload_key_to_group,
    upload_nvidia_account_to_gpt_load,
)


class DummyAccount:
    def __init__(self, *, token="", extra=None):
        self.token = token
        self.extra = dict(extra or {})

    def get_extra(self):
        return dict(self.extra)


class GptLoadSyncTests(unittest.TestCase):
    def test_upload_key_to_group_added_count_success(self):
        session = mock.Mock()
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {
            "code": 0,
            "message": "操作成功",
            "data": {"added_count": 1, "ignored_count": 0},
        }
        session.post.return_value = response

        ok, msg, detail = upload_key_to_group(
            api_url="http://gpt-load.local",
            api_key="sk-admin",
            group_id=7,
            key_value="nv-key",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已导入 1 个 key", msg)
        self.assertEqual(detail["added_count"], 1)

    def test_upload_key_to_group_ignored_count_treated_as_success(self):
        session = mock.Mock()
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {
            "code": 0,
            "message": "操作成功",
            "data": {"added_count": 0, "ignored_count": 1},
        }
        session.post.return_value = response

        ok, msg, detail = upload_key_to_group(
            api_url="http://gpt-load.local",
            api_key="sk-admin",
            group_id=7,
            key_value="nv-key",
            session=session,
        )

        self.assertTrue(ok)
        self.assertIn("已存在", msg)
        self.assertEqual(detail["ignored_count"], 1)

    def test_upload_account_to_gpt_load_missing_key_fails_fast(self):
        account = DummyAccount(token="", extra={})

        ok, msg, detail = upload_account_to_gpt_load(
            account,
            api_url="http://gpt-load.local",
            api_key="sk-admin",
            group_name="nvidia",
            provider_label="Cerebras",
        )

        self.assertFalse(ok)
        self.assertIn("缺少 Cerebras API key", msg)
        self.assertEqual(detail, {})

    def test_upload_nvidia_account_to_gpt_load_resolves_group_and_uploads(self):
        account = DummyAccount(token="nv-key", extra={"api_key": "nv-key"})

        with mock.patch(
            "services.gpt_load_sync.resolve_group",
            return_value={"id": 11, "name": "nvidia"},
        ) as resolve_mock:
            with mock.patch(
                "services.gpt_load_sync.upload_key_to_group",
                return_value=(True, "已导入 1 个 key", {"added_count": 1}),
            ) as upload_mock:
                ok, msg, detail = upload_nvidia_account_to_gpt_load(
                    account,
                    api_url="http://gpt-load.local",
                    api_key="sk-admin",
                    group_name="nvidia",
                )

        self.assertTrue(ok)
        self.assertEqual(msg, "已导入 1 个 key")
        self.assertEqual(detail["group_id"], 11)
        self.assertEqual(detail["group_name"], "nvidia")
        resolve_mock.assert_called_once()
        upload_mock.assert_called_once_with(
            api_url="http://gpt-load.local",
            api_key="sk-admin",
            group_id=11,
            key_value="nv-key",
            timeout=15,
            session=None,
        )

    def test_upload_cerebras_account_to_gpt_load_resolves_group_and_uploads(self):
        account = DummyAccount(token="cb-key", extra={"api_key": "cb-key"})

        with mock.patch(
            "services.gpt_load_sync.resolve_group",
            return_value={"id": 12, "name": "cerebras"},
        ) as resolve_mock:
            with mock.patch(
                "services.gpt_load_sync.upload_key_to_group",
                return_value=(True, "已导入 1 个 key", {"added_count": 1}),
            ) as upload_mock:
                ok, msg, detail = upload_cerebras_account_to_gpt_load(
                    account,
                    api_url="http://gpt-load.local",
                    api_key="sk-admin",
                    group_name="cerebras",
                )

        self.assertTrue(ok)
        self.assertEqual(msg, "已导入 1 个 key")
        self.assertEqual(detail["group_id"], 12)
        self.assertEqual(detail["group_name"], "cerebras")
        resolve_mock.assert_called_once()
        upload_mock.assert_called_once_with(
            api_url="http://gpt-load.local",
            api_key="sk-admin",
            group_id=12,
            key_value="cb-key",
            timeout=15,
            session=None,
        )


if __name__ == "__main__":
    unittest.main()
