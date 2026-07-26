import unittest
from unittest import mock

from core.proxy_utils import build_playwright_proxy_config


class ProxyUtilsTests(unittest.TestCase):
    @mock.patch(
        "core.proxy_utils._get_or_start_browser_proxy_bridge",
        return_value="http://127.0.0.1:43123",
    )
    def test_build_playwright_proxy_config_wraps_authenticated_socks_proxy(
        self,
        bridge_mock,
    ):
        config = build_playwright_proxy_config(
            "socks5://highkay_1:1844@gate.rola.vip:2000"
        )

        self.assertEqual(config, {"server": "http://127.0.0.1:43123"})
        bridge_mock.assert_called_once_with(
            "socks5://highkay_1:1844@gate.rola.vip:2000"
        )

    def test_build_playwright_proxy_config_keeps_plain_socks_proxy_shape(self):
        config = build_playwright_proxy_config("socks5h://127.0.0.1:7890")

        self.assertEqual(config, {"server": "socks5://127.0.0.1:7890"})


if __name__ == "__main__":
    unittest.main()
