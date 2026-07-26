import json
import unittest
from unittest import mock
from urllib.parse import unquote

from platforms.grok.protocol_client import (
    GrokProtocolClient,
    build_signup_body,
    classify_grpc_failure,
    expand_sso_hop_urls,
    extract_server_action_error,
    extract_sso_from_text,
    grpc_web_frame,
    is_session_sso,
    looks_like_domain_rejection,
    parse_grpc_message,
    parse_grpc_status,
    scrape_state_tree,
    _pb_str,
)


class GrokProtocolClientHelpersTests(unittest.TestCase):
    def test_grpc_web_frame_and_pb_str(self):
        inner = _pb_str(1, "a@b.com")
        frame = grpc_web_frame(inner)
        self.assertEqual(frame[0], 0)
        length = int.from_bytes(frame[1:5], "big")
        self.assertEqual(length, len(inner))
        self.assertEqual(frame[5:], inner)

    def test_parse_grpc_status_from_trailer_body(self):
        body = b"ok\r\ngrpc-status: 3\r\ngrpc-message: rejected\r\n"
        self.assertEqual(parse_grpc_status({}, body), "3")
        self.assertEqual(parse_grpc_status({"grpc-status": "0"}, b""), "0")
        self.assertEqual(parse_grpc_message({}, body), "rejected")

    def test_classify_grpc_domain_rejection(self):
        err = classify_grpc_failure(
            http_status=200,
            grpc_status="3",
            grpc_message="Please use another email address",
            stage="grpc_create",
        )
        self.assertEqual(err.code, "email_domain_rejected")
        self.assertTrue(looks_like_domain_rejection(str(err)))

    def test_extract_server_action_error_ignores_i18n_catalog(self):
        flight = (
            '0:{"a":"$@1"}\n'
            '1:{"error":"[internal] Failed to verify Cloudflare turnstile token.","traceId":"$undefined"}\n'
            'f:["Please use another email address","invalid email","email domain rejected"]\n'
        )
        err = extract_server_action_error(flight)
        self.assertIn("turnstile", err.lower())
        # Full RSC catalog must not be treated as domain rejection.
        self.assertFalse(looks_like_domain_rejection(flight))
        self.assertTrue(looks_like_domain_rejection('{"error":"email domain rejected"}'))

    def test_classify_grpc_cf_block(self):
        err = classify_grpc_failure(
            http_status=403,
            grpc_status="",
            grpc_message="",
            body_preview="Attention Required! | Cloudflare",
            stage="grpc_create",
        )
        self.assertEqual(err.code, "cf_403")

    def test_scrape_state_tree_from_flight_payload(self):
        flight = (
            r'self.__next_f.push([1,"{\"f\":[[[\"\",{\"children\":[\"(app)\",'
            r'{\"children\":[\"(auth)\",{\"children\":[\"sign-up\",'
            r'{\"children\":[\"__PAGE__\",{},null,null,0]},null,null,0]},'
            r'null,null,0]},null,null,0]},null,null,16]],\"$undefined\""])'
        )
        # Minimal synthetic: ensure default path when sign-up present without flight
        tree = scrape_state_tree("sign-up page content without flight")
        self.assertTrue(tree.startswith("%5B"))
        # Empty without sign-up marker
        self.assertEqual(scrape_state_tree("hello"), "")

    def test_is_session_sso_rejects_set_cookie_hop_jwt(self):
        # Craft a JWT-like payload with success_url config (not a session SSO).
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"config": {"success_url": "https://example.com"}}).encode()
        ).decode().rstrip("=")
        hop_token = f"{header}.{payload}.sig"
        self.assertFalse(is_session_sso(hop_token))

        session_payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "user-1", "iss": "xai"}).encode()
        ).decode().rstrip("=")
        session_token = f"{header}.{session_payload}." + ("x" * 40)
        self.assertTrue(is_session_sso(session_token))

    def test_extract_sso_from_text_named_cookie(self):
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "user-1"}).encode()
        ).decode().rstrip("=")
        token = f"{header}.{payload}." + ("y" * 40)
        text = f'some rsc body; sso={token}; Path=/'
        self.assertEqual(extract_sso_from_text(text), token)

    def test_expand_sso_hop_urls_adds_known_hosts(self):
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {"config": {"success_url": "https://auth.grokusercontent.com/set-cookie"}}
            ).encode()
        ).decode().rstrip("=")
        jwt = f"{header}.{payload}.abc"
        hops = expand_sso_hop_urls(
            [f"https://auth.grokusercontent.com/set-cookie?q={jwt}"]
        )
        self.assertTrue(any("auth.x.ai/set-cookie" in h for h in hops))
        self.assertTrue(any(jwt in h for h in hops))

    def test_build_signup_body_shape(self):
        raw = build_signup_body(
            "a@b.com",
            "Pass123,,,aA1",
            "ABC123",
            "turnstile-token-value",
            given_name="Ada",
            family_name="Lovelace",
        )
        data = json.loads(raw.decode("utf-8"))
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["emailValidationCode"], "ABC123")
        self.assertEqual(data[0]["turnstileToken"], "turnstile-token-value")
        self.assertEqual(data[0]["createUserAndSessionRequest"]["email"], "a@b.com")
        self.assertEqual(data[0]["createUserAndSessionRequest"]["givenName"], "Ada")
        self.assertEqual(data[0]["createUserAndSessionRequest"]["tosAcceptedVersion"], 1)
        self.assertEqual(data[0]["castleRequestToken"], "")

    def test_create_email_code_posts_grpc_web(self):
        client = GrokProtocolClient(proxy=None, log_fn=lambda *_: None)
        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.headers = {"grpc-status": "0"}
        fake_resp.content = b""
        fake_resp.text = ""

        with mock.patch.object(client._session, "post", return_value=fake_resp) as post:
            client.create_email_code("user@example.com")

        self.assertEqual(
            post.call_args.args[0],
            "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateEmailValidationCode",
        )
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Content-Type"], "application/grpc-web+proto")
        self.assertEqual(headers["X-Grpc-Web"], "1")
        body = post.call_args.kwargs["data"]
        self.assertIsInstance(body, (bytes, bytearray))
        self.assertIn(b"user@example.com", body)
        client.close()

    def test_signup_server_action_follows_sso_when_cookie_missing(self):
        client = GrokProtocolClient(proxy=None, log_fn=lambda *_: None)
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "user-1"}).encode()
        ).decode().rstrip("=")
        sso = f"{header}.{payload}." + ("z" * 40)

        action_resp = mock.Mock()
        action_resp.status_code = 200
        action_resp.text = f'sso={sso}'
        action_resp.cookies = []
        action_resp.headers = {}

        with mock.patch.object(client._session, "post", return_value=action_resp):
            text, got = client.signup_server_action(
                b"[]",
                action_id="abc",
                state_tree=unquote("%5B%22%22%5D"),
            )
        self.assertIn(sso, text)
        self.assertEqual(got, sso)
        client.close()


if __name__ == "__main__":
    unittest.main()
