import unittest
from pathlib import Path
from unittest import mock

from core.executors.playwright import PlaywrightExecutor


class PlaywrightExecutorTests(unittest.TestCase):
    @mock.patch("core.executors.playwright.build_playwright_proxy_config")
    @mock.patch("core.executors.playwright.resolve_browser_headless")
    @mock.patch("core.executors.playwright.ensure_browser_display_available")
    @mock.patch("core.executors.playwright.browser_backend.sync_playwright")
    def test_executor_uses_unified_browser_backend(
        self,
        sync_playwright_mock,
        _display_mock,
        resolve_headless_mock,
        build_proxy_mock,
    ):
        resolve_headless_mock.return_value = (True, "test")
        build_proxy_mock.return_value = {"server": "socks5://127.0.0.1:1080"}

        playwright = sync_playwright_mock.return_value.start.return_value
        browser = mock.Mock()
        context = mock.Mock()
        page = mock.Mock()
        browser.new_context.return_value = context
        context.new_page.return_value = page
        playwright.chromium.launch.return_value = browser

        executor = PlaywrightExecutor(proxy="socks5h://127.0.0.1:1080", headless=True)

        self.assertIs(executor.page, page)
        build_proxy_mock.assert_called_once_with("socks5h://127.0.0.1:1080")
        launch_kwargs = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(
            launch_kwargs["proxy"],
            {"server": "socks5://127.0.0.1:1080"},
        )
        # Prefer portable Chrome when present on this machine.
        if Path(r"F:\chrome\chrome.exe").is_file():
            self.assertEqual(
                launch_kwargs.get("executable_path"),
                str(Path(r"F:\chrome\chrome.exe").resolve()),
            )


        executor.close()
        browser.close.assert_called_once()
        playwright.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
