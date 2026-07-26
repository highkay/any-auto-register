import unittest

from core.flags import FEATURE_CLAUDE_REGISTER, FEATURE_GITHUB_REGISTER
from core.registry import get, list_platforms, load_all


class PlatformLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all()

    def test_github_and_claude_registered(self):
        names = {p["name"] for p in list_platforms()}
        self.assertIn("github", names)
        self.assertIn("claude", names)
        gh = get("github")
        self.assertEqual(gh.name, "github")
        self.assertIn("headed", gh.supported_executors)

    def test_github_requires_flag(self):
        from core.base_platform import RegisterConfig
        from core.config_store import config_store

        Platform = get("github")
        inst = Platform(RegisterConfig())
        # ensure flag off
        try:
            config_store.set_many({FEATURE_GITHUB_REGISTER: "0"})
        except Exception:
            pass
        with self.assertRaises(ValueError):
            inst.register(email="a@b.com")


if __name__ == "__main__":
    unittest.main()
