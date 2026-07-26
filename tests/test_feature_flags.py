"""Feature flag helpers and platform gating."""
from __future__ import annotations

import unittest

from core.flags import (
    FEATURE_CAPSOLVER,
    FEATURE_GITHUB_REGISTER,
    assert_platform_allowed,
    flag_enabled,
    normalize_flag_value,
    platform_feature_flag,
    require_flag,
)


class FeatureFlagsTest(unittest.TestCase):
    def test_normalize_and_truthy(self):
        self.assertEqual(normalize_flag_value("1"), "1")
        self.assertEqual(normalize_flag_value("TRUE"), "1")
        self.assertEqual(normalize_flag_value("on"), "1")
        self.assertEqual(normalize_flag_value(""), "0")
        self.assertEqual(normalize_flag_value("no"), "0")
        self.assertTrue(flag_enabled(FEATURE_GITHUB_REGISTER, {FEATURE_GITHUB_REGISTER: "yes"}))
        self.assertFalse(flag_enabled(FEATURE_GITHUB_REGISTER, {FEATURE_GITHUB_REGISTER: "0"}))
        self.assertFalse(flag_enabled(FEATURE_GITHUB_REGISTER, {}))

    def test_platform_gate(self):
        self.assertEqual(platform_feature_flag("github"), FEATURE_GITHUB_REGISTER)
        self.assertIsNone(platform_feature_flag("chatgpt"))
        assert_platform_allowed("chatgpt", {})
        with self.assertRaises(ValueError):
            assert_platform_allowed("github", {FEATURE_GITHUB_REGISTER: "0"})
        assert_platform_allowed("github", {FEATURE_GITHUB_REGISTER: "1"})

    def test_require_flag(self):
        require_flag(FEATURE_CAPSOLVER, cfg={FEATURE_CAPSOLVER: "on"})
        with self.assertRaises(ValueError):
            require_flag(FEATURE_CAPSOLVER, cfg={})


if __name__ == "__main__":
    unittest.main()
