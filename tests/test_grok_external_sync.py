import unittest
from unittest import mock

from services.external_sync import sync_account


class DummyAccount:
    platform = "grok"
    email = "user@example.com"
    password = "example-password"
    token = "sso-token"
    extra = {"sso": "sso-token"}
    id = None

    def get_extra(self):
        return dict(self.extra)


def _config_getter(values: dict[str, str]):
    def _get(key: str, default: str = "") -> str:
        return values.get(key, default)

    return _get


class GrokExternalSyncTests(unittest.TestCase):
    def test_hub_mode_skips_auto_upload_by_default(self):
        config = {
            "grok2api_url": "http://grok2api.test",
            "grok_cpa_enabled": "1",
        }
        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(config)):
            with mock.patch(
                "platforms.grok.grok2api_upload.upload_to_grok2api"
            ) as grok2_mock:
                with mock.patch(
                    "platforms.grok.cpa_xai.mint_and_upload_xai_cpa"
                ) as cpa_mock:
                    logs: list[str] = []
                    results = sync_account(DummyAccount(), log=logs.append)

        self.assertEqual(results, [])
        grok2_mock.assert_not_called()
        cpa_mock.assert_not_called()
        self.assertTrue(any("跳过自动推送" in line for line in logs))

    def test_xai_cpa_runs_when_grok2api_is_unavailable(self):
        config = {
            "grok_auto_upload": "1",
            "grok2api_url": "http://grok2api.test",
            "grok_cpa_enabled": "1",
        }
        with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(config)):
            with mock.patch(
                "services.grok2api_runtime.ensure_grok2api_ready",
                return_value=(False, "grok2api 未就绪"),
            ):
                with mock.patch(
                    "platforms.grok.cpa_xai.mint_and_upload_xai_cpa",
                    return_value=(True, "uploaded", {"uploaded": True}),
                ) as cpa_mock:
                    logs: list[str] = []
                    results = sync_account(DummyAccount(), log=logs.append)

        self.assertEqual([item["name"] for item in results], ["grok2api", "xAI CPA"])
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])
        cpa_mock.assert_called_once()
        cpa_log = cpa_mock.call_args.kwargs["log"]
        cpa_log("已打开本机授权浏览器")
        self.assertEqual(logs, ["[xAI CPA] 已打开本机授权浏览器"])


if __name__ == "__main__":
    unittest.main()
