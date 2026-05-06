from __future__ import annotations

import base64
import importlib.util
import json
import unittest
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "convert_cpa_chatgpt_token.py"
    spec = importlib.util.spec_from_file_location("convert_cpa_chatgpt_token", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _jwt(payload: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": "test"}
    segments = []
    for part in (header, payload, b"signature"):
        if isinstance(part, dict):
            raw = json.dumps(part, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        else:
            raw = part
        segments.append(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
    return ".".join(segments)


class ConvertCpaChatGPTTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def test_maps_core_fields_from_cpa_token(self):
        access_token = _jwt(
            {
                "sub": "auth0|demo-user",
                "iat": 1775795583,
                "exp": 1776659583,
                "https://api.openai.com/auth": {
                    "amr": ["otp", "urn:openai:amr:otp_email"],
                    "chatgpt_account_id": "a8546473-ed6d-4934-98e0-477277a4ea32",
                    "chatgpt_user_id": "user-XUKUqZRQPIgSsmy4Md1WWHvB",
                    "chatgpt_compute_residency": "no_constraint",
                    "chatgpt_plan_type": "free",
                    "user_id": "user-XUKUqZRQPIgSsmy4Md1WWHvB",
                },
                "https://api.openai.com/profile": {
                    "email": "tmpwnyqwu9387@mail.20210513.xyz",
                    "email_verified": True,
                },
            }
        )
        id_token = _jwt(
            {
                "email": "tmpwnyqwu9387@mail.20210513.xyz",
                "name": "Noah Anderson",
                "amr": ["otp", "urn:openai:amr:otp_email"],
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "a8546473-ed6d-4934-98e0-477277a4ea32",
                    "chatgpt_plan_type": "free",
                    "organization_id": "org-MlkdmK2DZdZ2eYuIYzczbcJG",
                    "project_id": "proj_8GBsP21cmhItGKev26cViyvz",
                },
            }
        )

        converted = self.module.convert_cpa_token(
            {
                "type": "codex",
                "email": "tmpwnyqwu9387@mail.20210513.xyz",
                "expired": "2026-04-20T12:33:03+08:00",
                "id_token": id_token,
                "account_id": "a8546473-ed6d-4934-98e0-477277a4ea32",
                "access_token": access_token,
                "refresh_token": "rt_demo",
                "session_token": "session_demo",
            }
        )

        self.assertEqual(
            converted["WARNING_BANNER"],
            self.module.WARNING_BANNER,
        )
        self.assertEqual(converted["accessToken"], access_token)
        self.assertEqual(converted["sessionToken"], "session_demo")
        self.assertEqual(converted["authProvider"], "openai")
        self.assertEqual(converted["user"]["id"], "user-XUKUqZRQPIgSsmy4Md1WWHvB")
        self.assertEqual(converted["user"]["name"], "Noah Anderson")
        self.assertEqual(converted["user"]["email"], "tmpwnyqwu9387@mail.20210513.xyz")
        self.assertEqual(converted["user"]["idp"], "auth0")
        self.assertEqual(converted["user"]["iat"], 1775795583)
        self.assertEqual(converted["account"]["id"], "a8546473-ed6d-4934-98e0-477277a4ea32")
        self.assertEqual(converted["account"]["planType"], "free")
        self.assertEqual(converted["account"]["computeResidency"], "no_constraint")
        self.assertEqual(converted["expires"], "2026-04-20T04:33:03.000Z")

    def test_derives_name_and_fallbacks_from_minimal_input(self):
        access_token = _jwt(
            {
                "sub": "auth0|min-user",
                "iat": 1700000000,
                "exp": 1700003600,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acc-min",
                    "chatgpt_user_id": "user-min",
                    "chatgpt_plan_type": "plus",
                    "chatgpt_compute_residency": "no_constraint",
                },
                "https://api.openai.com/profile": {
                    "email": "demo.user@example.com",
                    "email_verified": True,
                },
            }
        )

        converted = self.module.convert_cpa_token(
            {
                "access_token": access_token,
                "account_id": "acc-min",
                "email": "demo.user@example.com",
            }
        )

        self.assertEqual(converted["user"]["name"], "Demo User")
        self.assertEqual(converted["user"]["id"], "user-min")
        self.assertEqual(converted["account"]["planType"], "plus")
        self.assertEqual(converted["account"]["structure"], "personal")
        self.assertEqual(converted["expires"], "2023-11-14T23:13:20.000Z")
        self.assertNotIn("sessionToken", converted)


if __name__ == "__main__":
    unittest.main()
