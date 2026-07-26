import unittest
from unittest import mock

from platforms.grok.castle import DEFAULT_CASTLE_PK, resolve_castle_pk
from platforms.grok.protocol_client import build_signup_body, grpc_web_frame, _pb_str


class GrokCastleTests(unittest.TestCase):
    def test_resolve_castle_pk_default(self):
        self.assertTrue(resolve_castle_pk({}).startswith("pk_"))
        self.assertEqual(resolve_castle_pk({"grok_castle_pk": "pk_test"}), "pk_test")

    def test_signup_body_includes_castle_token(self):
        raw = build_signup_body(
            "a@b.com",
            "Pass1,,,aA1",
            "ABC123",
            "turnstile-token",
            castle_token="castle-tok-value-123456",
        )
        import json

        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data[0]["castleRequestToken"], "castle-tok-value-123456")
        self.assertTrue(data[0]["conversionId"])

    def test_create_email_grpc_frame_includes_field_3_when_castle_set(self):
        inner = _pb_str(1, "a@b.com") + _pb_str(3, "castle-token-xyz")
        frame = grpc_web_frame(inner)
        self.assertIn(b"a@b.com", frame)
        self.assertIn(b"castle-token-xyz", frame)

    def test_default_pk_matches_frontend_publishable_key(self):
        self.assertEqual(DEFAULT_CASTLE_PK, "pk_p8GGwD3TmFJZRsX3BQcqAv9aFVispNz")


if __name__ == "__main__":
    unittest.main()
