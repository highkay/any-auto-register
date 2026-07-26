import os
import unittest
from pathlib import Path
from unittest import mock

from core import browser_runtime


class BrowserRuntimeChromeTests(unittest.TestCase):
    def test_get_chrome_executable_prefers_default_f_chrome(self):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in browser_runtime._CHROME_ENV_NAMES
        }
        with mock.patch.dict(os.environ, env, clear=True):
            path = browser_runtime.get_chrome_executable()
        self.assertIsNotNone(path)
        self.assertEqual(
            Path(path).resolve(),
            browser_runtime.DEFAULT_CHROME_EXECUTABLE.resolve(),
        )
        self.assertTrue(Path(path).is_file())

    def test_with_chrome_executable_replaces_chrome_channel(self):
        with mock.patch.object(
            browser_runtime,
            "get_chrome_executable",
            return_value=r"F:\chrome\chrome.exe",
        ):
            opts = browser_runtime.with_chrome_executable(
                headless=True, channel="chrome"
            )
        self.assertEqual(opts["executable_path"], r"F:\chrome\chrome.exe")
        self.assertNotIn("channel", opts)
        self.assertTrue(opts["headless"])

    def test_with_chrome_executable_keeps_msedge_channel(self):
        with mock.patch.object(
            browser_runtime,
            "get_chrome_executable",
            return_value=r"F:\chrome\chrome.exe",
        ):
            opts = browser_runtime.with_chrome_executable(
                headless=True, channel="msedge"
            )
        self.assertEqual(opts["channel"], "msedge")
        self.assertNotIn("executable_path", opts)

    def test_env_override_wins(self):
        fake = Path(r"F:\chrome\chrome.exe")
        with mock.patch.dict(
            os.environ,
            {"CHROME_EXECUTABLE": str(fake)},
            clear=False,
        ):
            path = browser_runtime.get_chrome_executable()
        self.assertEqual(Path(path).resolve(), fake.resolve())


if __name__ == "__main__":
    unittest.main()
