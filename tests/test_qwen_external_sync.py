import unittest
from unittest import mock

from services.external_sync import sync_account


def _config_getter(values: dict[str, str]):
    def _get(key: str, default: str = "") -> str:
        return values.get(key, default)

    return _get


class DummyQwenAccount:
    def __init__(self):
        self.platform = "qwen"
        self.email = "demo@example.com"
        self.password = "Secret123!"
        self.token = "web-token"
        self.extra = {
            "oauth_access_token": "oa",
            "refresh_token": "rt",
            "resource_url": "portal.qwen.ai",
        }

    def get_extra(self):
        return dict(self.extra)


class QwenExternalSyncTests(unittest.TestCase):
    def test_sync_uploads_to_opengate_when_enabled(self):
        account = DummyQwenAccount()
        cfg = {
            "qwen_cpa_enabled": "0",
            "opengate_enabled": "1",
            "opengate_api_url": "http://192.168.1.18:7860",
            "opengate_api_key": "sk-test",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch(
                "platforms.qwen.opengate_upload.upload_to_opengate",
                return_value=(True, "已导入 OpenGate: demo@example.com", {"status_code": 201}),
            ) as upload_mock:
                with mock.patch(
                    "platforms.qwen.opengate_upload.persist_opengate_sync_result"
                ) as persist_mock:
                    results = sync_account(account)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "OpenGate")
        self.assertTrue(results[0]["ok"])
        upload_mock.assert_called_once()
        persist_mock.assert_called_once()

    def test_sync_can_upload_cpa_and_opengate_together(self):
        account = DummyQwenAccount()
        cfg = {
            "qwen_cpa_enabled": "1",
            "qwen_cpa_api_url": "http://cpa.local",
            "qwen_cpa_api_key": "k",
            "opengate_enabled": "1",
            "opengate_api_url": "http://192.168.1.18:7860",
            "opengate_api_key": "sk-test",
        }

        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
            with mock.patch(
                "platforms.qwen.cpa_upload.generate_token_json",
                return_value={"email": "demo@example.com"},
            ):
                with mock.patch(
                    "platforms.qwen.cpa_upload.upload_to_cpa",
                    return_value=(True, "cpa-ok"),
                ):
                    with mock.patch(
                        "platforms.qwen.opengate_upload.upload_to_opengate",
                        return_value=(True, "opengate-ok", {}),
                    ):
                        with mock.patch(
                            "platforms.qwen.opengate_upload.persist_opengate_sync_result"
                        ):
                            results = sync_account(account)

        names = [item["name"] for item in results]
        self.assertIn("Qwen CPA", names)
        self.assertIn("OpenGate", names)


if __name__ == "__main__":
    unittest.main()
