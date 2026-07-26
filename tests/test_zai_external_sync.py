import unittest
from unittest import mock

from services.external_sync import sync_account


class DummyAccount:
    def __init__(self, *, platform="zai", email="user@example.com", token="zai-token", extra=None):
        self.platform = platform
        self.email = email
        self.token = token
        self.extra = dict(extra or {})
        self.id = None

    def get_extra(self):
        return dict(self.extra)


def _config_getter(values: dict[str, str]):
    def _get(key: str, default: str = "") -> str:
        return values.get(key, default)

    return _get


class ZaiExternalSyncTests(unittest.TestCase):
    def test_zai2api_enabled_uploads_and_persists_sync_status(self):
        account = DummyAccount()
        cfg = {
            "zai_zai2api_enabled": "1",
            "zai_zai2api_url": "http://192.168.1.18:18082",
            "zai_zai2api_auth_token": "auth-token-123",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch(
                "services.external_sync.upload_to_zai2api",
                return_value=(True, "ok", {"tokens_url": "http://192.168.1.18:18082/v1/tokens"}),
            ) as upload_mock:
                with mock.patch("services.external_sync.persist_zai2api_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "zai2api")
        self.assertTrue(result[0]["ok"])
        upload_mock.assert_called_once_with(
            account,
            api_url="http://192.168.1.18:18082",
            auth_token="auth-token-123",
        )
        persist_mock.assert_called_once_with(
            account,
            ok=True,
            msg="ok",
            detail={"tokens_url": "http://192.168.1.18:18082/v1/tokens"},
        )

    def test_zai2api_disabled_skips_auto_upload(self):
        account = DummyAccount()
        cfg = {
            "zai_zai2api_enabled": "0",
            "zai_zai2api_url": "http://192.168.1.18:18082",
            "zai_zai2api_auth_token": "auth-token-123",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch("services.external_sync.upload_to_zai2api") as upload_mock:
                with mock.patch("services.external_sync.persist_zai2api_sync_result") as persist_mock:
                    result = sync_account(account)

        self.assertEqual(result, [])
        upload_mock.assert_not_called()
        persist_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
