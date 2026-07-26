import unittest
from pathlib import Path
from unittest import mock

from platforms.grok.core import GrokRegister
from platforms.kiro.core import KiroRegister
from platforms.nvidia.core import NvidiaRegister


def _assert_prefers_f_chrome(launch_kwargs: dict) -> None:
    chrome = Path(r"F:\chrome\chrome.exe")
    if not chrome.is_file():
        return
    assert launch_kwargs.get("executable_path") == str(chrome.resolve())
    assert "channel" not in launch_kwargs


class PlaywrightProxyLauncherTests(unittest.TestCase):
    @mock.patch("platforms.nvidia.core.ensure_browser_display_available")
    @mock.patch("platforms.nvidia.core.resolve_browser_headless", return_value=(True, "test"))
    @mock.patch(
        "platforms.nvidia.core.build_playwright_proxy_config",
        return_value={"server": "socks5://127.0.0.1:1080"},
    )
    @mock.patch("patchright.sync_api.sync_playwright")
    def test_nvidia_launch_browser_uses_normalized_proxy(
        self,
        sync_playwright_mock,
        build_proxy_mock,
        _resolve_headless_mock,
        _display_mock,
    ):
        playwright = sync_playwright_mock.return_value.start.return_value
        browser = mock.Mock()
        playwright.chromium.launch.return_value = browser

        reg = NvidiaRegister(
            captcha_solver=None,
            proxy="socks5h://127.0.0.1:1080",
            log_fn=lambda *_: None,
            headless=True,
        )
        _, launched_browser = reg._launch_browser()

        self.assertIs(launched_browser, browser)
        build_proxy_mock.assert_called_once_with("socks5h://127.0.0.1:1080")
        launch_kwargs = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(
            launch_kwargs["proxy"],
            {"server": "socks5://127.0.0.1:1080"},
        )
        _assert_prefers_f_chrome(launch_kwargs)

    @mock.patch("platforms.grok.core.ensure_browser_display_available")
    @mock.patch("platforms.grok.core.resolve_browser_headless", return_value=(True, "test"))
    @mock.patch(
        "platforms.grok.core.build_playwright_proxy_config",
        return_value={"server": "socks5://127.0.0.1:1080"},
    )
    @mock.patch("patchright.sync_api.sync_playwright")
    def test_grok_launch_browser_uses_normalized_proxy(
        self,
        sync_playwright_mock,
        build_proxy_mock,
        _resolve_headless_mock,
        _display_mock,
    ):
        playwright = sync_playwright_mock.return_value.start.return_value
        browser = mock.Mock()
        playwright.chromium.launch.return_value = browser

        reg = GrokRegister(
            captcha_solver=None,
            yescaptcha_key="",
            proxy="socks5h://127.0.0.1:1080",
            log_fn=lambda *_: None,
            headless=True,
        )
        _, launched_browser = reg._launch_browser()

        self.assertIs(launched_browser, browser)
        build_proxy_mock.assert_called_once_with("socks5h://127.0.0.1:1080")
        launch_kwargs = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(
            launch_kwargs["proxy"],
            {"server": "socks5://127.0.0.1:1080"},
        )
        _assert_prefers_f_chrome(launch_kwargs)

    @mock.patch("platforms.kiro.core.ensure_browser_display_available")
    @mock.patch("platforms.kiro.core.resolve_browser_headless", return_value=(True, "test"))
    @mock.patch(
        "platforms.kiro.core.build_playwright_proxy_config",
        return_value={"server": "socks5://127.0.0.1:1080"},
    )
    @mock.patch("platforms.kiro.core.sync_playwright")
    def test_kiro_init_browser_uses_normalized_proxy(
        self,
        sync_playwright_mock,
        build_proxy_mock,
        _resolve_headless_mock,
        _display_mock,
    ):
        playwright = sync_playwright_mock.return_value.start.return_value
        browser = mock.Mock()
        context = mock.Mock()
        browser.new_context.return_value = context
        playwright.chromium.launch.return_value = browser

        reg = KiroRegister(proxy="socks5h://127.0.0.1:1080", headless=True)
        reg.log_fn = lambda *_: None
        reg._build_random_profile = mock.Mock(
            return_value={
                "name": "test-profile",
                "user_agent": "Mozilla/5.0",
                "locale": "en-US",
                "timezone_id": "UTC",
                "viewport": {"width": 1280, "height": 720},
            }
        )

        reg._init_browser()

        build_proxy_mock.assert_called_once_with("socks5h://127.0.0.1:1080")
        launch_kwargs = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(
            launch_kwargs["proxy"],
            {"server": "socks5://127.0.0.1:1080"},
        )
        _assert_prefers_f_chrome(launch_kwargs)


if __name__ == "__main__":
    unittest.main()
