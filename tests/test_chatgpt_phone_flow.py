import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.base_phone import FiveSimPhoneService, FreeSmsToolPhoneService, HeroSMSPhoneService, PhoneLease
from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.phone_service import (
    PhoneEntry,
    SMSToMePhoneService,
    create_phone_service,
    parse_country_slugs,
    resolve_phone_verification_provider,
)
from platforms.chatgpt.utils import FlowState


class OAuthCookieDecodeTests(unittest.TestCase):
    def test_decode_signed_cookie_payload(self):
        payload = {
            "email": "demo@example.com",
            "phone_number": "+447456344799",
            "phone_verification_channel": "whatsapp",
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
        cookie_value = f"{encoded}.sig-a.sig-b"

        self.assertEqual(OAuthClient._decode_cookie_json_value(cookie_value), payload)

    def test_decode_invalid_cookie_payload(self):
        self.assertIsNone(OAuthClient._decode_cookie_json_value("not-a-valid-cookie"))


class SMSToMeConfigTests(unittest.TestCase):
    def test_parse_country_slugs_accepts_csv_and_iterables(self):
        self.assertEqual(
            parse_country_slugs("united-kingdom, poland;finland"),
            ["united-kingdom", "poland", "finland"],
        )
        self.assertEqual(
            parse_country_slugs(["united-kingdom", "poland", "united_kingdom"]),
            ["united-kingdom", "poland"],
        )

    def test_resolve_phone_verification_provider_supports_auto_mode(self):
        self.assertEqual(
            resolve_phone_verification_provider({"phone_verification_provider": "auto"}),
            "smstome",
        )

    def test_phone_service_enabled_when_pool_file_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            pool_path.write_text("+447456344799\tunited-kingdom\thttps://example.com\n", encoding="utf-8")

            service = SMSToMePhoneService({"smstome_global_file": str(pool_path)})
            self.assertTrue(service.enabled)

    def test_phone_service_disabled_for_empty_pool_without_cookie(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            pool_path.write_text("", encoding="utf-8")

            service = SMSToMePhoneService({"smstome_global_file": str(pool_path)})
            self.assertFalse(service.enabled)

    def test_phone_service_uses_configured_pool_and_task_paths(self):
        service = SMSToMePhoneService(
            {
                "smstome_global_file": "custom_pool.txt",
                "smstome_used_numbers_dir": "custom_used",
                "smstome_task_name": "custom_task",
            }
        )
        self.assertEqual(service.global_file, Path("custom_pool.txt"))
        self.assertEqual(service.used_numbers_dir, Path("custom_used"))
        self.assertEqual(service.task_name, "custom_task")

    def test_wait_for_code_forwards_cookie_timeout_and_poll_interval(self):
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447456344799",
            detail_url="https://example.com/phone/1",
        )
        service = SMSToMePhoneService(
            {
                "smstome_cookie": "cf_clearance=demo",
                "smstome_otp_timeout_seconds": "66",
                "smstome_poll_interval_seconds": "7",
            }
        )

        with mock.patch("platforms.chatgpt.phone_service.wait_for_otp", return_value="123456") as mocked:
            code = service.wait_for_code(entry)

        self.assertEqual(code, "123456")
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "cf_clearance=demo")
        self.assertEqual(kwargs["timeout"], 66)
        self.assertEqual(kwargs["poll_interval"], 7)
        self.assertFalse(kwargs["raise_on_timeout"])

    def test_ensure_pool_ready_syncs_with_configured_page_limit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            pool_path = Path(tmp_dir) / "phones.txt"
            service = SMSToMePhoneService(
                {
                    "smstome_cookie": "cf_clearance=demo",
                    "smstome_country_slugs": "united-kingdom",
                    "smstome_global_file": str(pool_path),
                    "smstome_sync_max_pages_per_country": "9",
                }
            )

            with mock.patch("platforms.chatgpt.phone_service.update_global_phone_list", return_value=3) as mocked:
                service.ensure_pool_ready()

        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["cookie_header"], "cf_clearance=demo")
        self.assertEqual(kwargs["countries"], ["united-kingdom"])
        self.assertEqual(kwargs["output_path"], pool_path)
        self.assertEqual(kwargs["max_pages_per_country"], 9)


class HeroSMSPhoneServiceTests(unittest.TestCase):
    def test_resolve_phone_verification_provider_prefers_explicit_value(self):
        self.assertEqual(
            resolve_phone_verification_provider(
                {
                    "phone_verification_provider": "hero_sms",
                    "smstome_cookie": "cf_clearance=demo",
                }
            ),
            "hero_sms",
        )
        self.assertEqual(
            resolve_phone_verification_provider(
                {
                    "hero_sms_api_key": "demo",
                }
            ),
            "hero_sms",
        )
        self.assertEqual(
            resolve_phone_verification_provider({"smstome_cookie": "cf_clearance=demo"}),
            "smstome",
        )

    def test_create_phone_service_returns_hero_sms_when_selected(self):
        service = create_phone_service(
            {
                "phone_verification_provider": "hero_sms",
                "hero_sms_api_key": "demo",
            }
        )
        self.assertIsInstance(service, HeroSMSPhoneService)

    def test_hero_sms_resolves_kimi_service_code_by_name(self):
        service = HeroSMSPhoneService(
            {"hero_sms_api_key": "demo", "hero_sms_service": "Kimi"}
        )
        service._service_catalog = [
            {"code": "ayz", "name": "Kimi"},
            {"code": "go", "name": "Google"},
        ]
        self.assertEqual(service._resolve_service_code(), "ayz")

    def test_hero_sms_acquire_phone_uses_selected_country_price_and_prefix_exclusions(self):
        service = HeroSMSPhoneService(
            {"hero_sms_api_key": "demo", "hero_sms_service": "ayz"}
        )
        candidate = {
            "country_id": 16,
            "country_name": "United Kingdom",
            "country_slug": "united-kingdom",
            "price": 0.065,
            "min_price": 0.065,
            "total": 100,
            "physical": 90,
            "default_count": 80,
        }
        with mock.patch.object(service, "_resolve_service_code", return_value="ayz"):
            with mock.patch.object(service, "_build_offer_candidates", return_value=[candidate]):
                with mock.patch.object(
                    service,
                    "_stub_request",
                    side_effect=[
                        {
                            "activationId": "123456",
                            "phoneNumber": "+447456344799",
                            "activationCost": 0.065,
                        },
                        "ACCESS_READY",
                    ],
                ) as mocked:
                    lease = service.acquire_phone(exclude_prefixes=["+447000", "+668000"])

        self.assertEqual(lease.activation_id, "123456")
        self.assertEqual(lease.phone, "+447456344799")
        self.assertEqual(lease.country_id, 16)
        self.assertEqual(lease.country_slug, "united-kingdom")
        self.assertEqual(mocked.call_count, 2)
        query = mocked.call_args_list[0].args[0]
        self.assertEqual(query["action"], "getNumberV2")
        self.assertEqual(query["service"], "ayz")
        self.assertEqual(query["country"], 16)
        self.assertEqual(query["maxPrice"], 0.065)
        self.assertEqual(query["fixedPrice"], "true")
        self.assertEqual(query["phoneException"], "447000,668000")
        ready_query = mocked.call_args_list[1].args[0]
        self.assertEqual(ready_query, {"action": "setStatus", "id": "123456", "status": 1})

    def test_hero_sms_wait_for_code_prefers_status_v2_sms_code(self):
        service = HeroSMSPhoneService({"hero_sms_api_key": "demo"})
        entry = PhoneLease(phone="+447456344799", activation_id="123456")

        with mock.patch.object(
            service,
            "_get_status_v2_payload",
            return_value={"sms": {"code": "246810"}},
        ):
            with mock.patch.object(service, "_get_status_payload") as mocked_status:
                with mock.patch.object(service, "_get_all_sms_payload") as mocked_sms:
                    code = service.wait_for_code(entry, timeout=10)

        self.assertEqual(code, "246810")
        mocked_status.assert_not_called()
        mocked_sms.assert_not_called()

    def test_hero_sms_wait_for_code_can_extract_from_sms_list(self):
        service = HeroSMSPhoneService({"hero_sms_api_key": "demo"})
        entry = PhoneLease(phone="+447456344799", activation_id="123456")
        with mock.patch.object(service, "_get_status_v2_payload", return_value="STATUS_WAIT_CODE"):
            with mock.patch.object(service, "_get_status_payload", return_value="STATUS_WAIT_CODE"):
                with mock.patch.object(
                    service,
                    "_get_all_sms_payload",
                    return_value={"data": [{"id": "1", "text": "Kimi verification code 654321"}]},
                ):
                    code = service.wait_for_code(entry, timeout=10)

        self.assertEqual(code, "654321")

    def test_hero_sms_report_code_requested_marks_activation_ready(self):
        service = HeroSMSPhoneService({"hero_sms_api_key": "demo"})
        entry = PhoneLease(phone="+447456344799", activation_id="123456")

        with mock.patch.object(service, "_set_status", return_value="ACCESS_READY") as mocked:
            service.report_code_requested(entry)

        mocked.assert_called_once_with("123456", 1)


class FreeSmsToolPhoneServiceTests(unittest.TestCase):
    def test_resolve_phone_verification_provider_supports_free_sms_tool(self):
        self.assertEqual(
            resolve_phone_verification_provider(
                {
                    "phone_verification_provider": "free_sms_tool",
                    "free_sms_tool_api_key": "demo",
                }
            ),
            "free_sms_tool",
        )
        self.assertEqual(
            resolve_phone_verification_provider({"free_sms_tool_api_key": "demo"}),
            "free_sms_tool",
        )

    def test_create_phone_service_returns_free_sms_tool_when_selected(self):
        service = create_phone_service(
            {
                "phone_verification_provider": "free_sms_tool",
                "free_sms_tool_api_key": "demo",
            }
        )
        self.assertIsInstance(service, FreeSmsToolPhoneService)

    def test_free_sms_tool_acquire_phone_creates_claim(self):
        service = FreeSmsToolPhoneService(
            {
                "free_sms_tool_base_url": "http://127.0.0.1:18000",
                "free_sms_tool_api_key": "demo",
                "free_sms_tool_app_slug": "kimi",
                "free_sms_tool_app_name": "Kimi",
                "free_sms_tool_country_name": "United Kingdom",
                "free_sms_tool_claim_ttl_minutes": "5",
            }
        )
        payload = {
            "claim_token": "clm_demo",
            "number_id": 15,
            "e164": "+447949338055",
            "country_name": "United Kingdom",
            "created_at": "2026-05-06T12:53:35.660635+00:00",
        }
        with mock.patch.object(service, "_api_post_json", return_value=payload) as mocked:
            lease = service.acquire_phone()

        self.assertEqual(lease.phone, "+447949338055")
        self.assertEqual(lease.activation_id, "clm_demo")
        self.assertEqual(lease.extra["number_id"], 15)
        mocked.assert_called_once_with(
            "/api/claims",
            {
                "app_slug": "kimi",
                "app_name": "Kimi",
                "country_name": "United Kingdom",
                "provider_id": None,
                "purpose": "phone verification",
                "include_cooling": False,
                "ttl_minutes": 5,
            },
        )

    def test_free_sms_tool_wait_for_code_ignores_stale_messages_before_requested_at(self):
        service = FreeSmsToolPhoneService(
            {
                "free_sms_tool_base_url": "http://127.0.0.1:18000",
                "free_sms_tool_api_key": "demo",
            }
        )
        entry = PhoneLease(
            phone="+447949338055",
            activation_id="clm_demo",
            extra={
                "claim_created_at": "2026-05-06T12:53:35.660635+00:00",
                "requested_at": "2026-05-06T12:54:00+00:00",
            },
        )
        with mock.patch.object(
            service,
            "_api_get_json",
            return_value=[
                {
                    "id": 1,
                    "body": "old code 111111",
                    "otp_code": "111111",
                    "received_at": "2026-05-06T12:53:50+00:00",
                },
                {
                    "id": 2,
                    "body": "new code 222222",
                    "otp_code": "222222",
                    "received_at": "2026-05-06T12:54:20+00:00",
                },
            ],
        ):
            code = service.wait_for_code(entry, timeout=10)

        self.assertEqual(code, "222222")


class FiveSimPhoneServiceTests(unittest.TestCase):
    def test_resolve_phone_verification_provider_supports_five_sim(self):
        self.assertEqual(
            resolve_phone_verification_provider(
                {
                    "phone_verification_provider": "five_sim",
                    "five_sim_api_key": "demo",
                }
            ),
            "five_sim",
        )
        self.assertEqual(
            resolve_phone_verification_provider({"five_sim_api_key": "demo"}),
            "five_sim",
        )

    def test_create_phone_service_returns_five_sim_when_selected(self):
        service = create_phone_service(
            {
                "phone_verification_provider": "five_sim",
                "five_sim_api_key": "demo",
            }
        )
        self.assertIsInstance(service, FiveSimPhoneService)

    def test_five_sim_acquire_phone_buys_lowest_price_candidate(self):
        service = FiveSimPhoneService(
            {
                "five_sim_api_key": "demo",
                "five_sim_product": "openai",
            }
        )
        service._country_catalog = {
            "netherlands": {"text_en": "Netherlands"},
            "england": {"text_en": "England"},
        }
        service._price_catalog = {
            "england": {
                "openai": {
                    "virtual59": {"cost": 0.0609, "count": 61694},
                }
            },
            "netherlands": {
                "openai": {
                    "virtual59": {"cost": 0.0385, "count": 1902},
                    "virtual60": {"cost": 0.0385, "count": 1200},
                }
            },
        }
        payload = {
            "id": 1004869420,
            "phone": "+31685183770",
            "operator": "virtual59",
            "product": "openai",
            "price": 0.0385,
            "status": "RECEIVED",
            "created_at": "2026-05-07T05:30:28.311351Z",
            "country": "netherlands",
        }
        with mock.patch.object(service, "_api_get_json", return_value=payload) as mocked:
            lease = service.acquire_phone()

        self.assertEqual(lease.phone, "+31685183770")
        self.assertEqual(lease.activation_id, "1004869420")
        self.assertEqual(lease.country_slug, "netherlands")
        mocked.assert_called_once_with(
            "/user/buy/activation/netherlands/virtual59/openai"
        )

    def test_five_sim_wait_for_code_extracts_sms_code(self):
        service = FiveSimPhoneService({"five_sim_api_key": "demo"})
        entry = PhoneLease(
            phone="+31685183770",
            activation_id="1004869420",
            extra={"created_at": "2026-05-07T05:30:28.311351Z"},
        )
        payload = {
            "status": "RECEIVED",
            "sms": [
                {"text": "old code 111111", "created_at": "2026-05-07T05:30:20Z"},
                {"code": "222222", "created_at": "2026-05-07T05:31:00Z"},
            ],
        }
        with mock.patch.object(service, "_api_get_json", return_value=payload):
            code = service.wait_for_code(entry, timeout=10)

        self.assertEqual(code, "222222")

    def test_five_sim_mark_blacklisted_bans_matching_activation(self):
        service = FiveSimPhoneService({"five_sim_api_key": "demo"})
        lease = PhoneLease(phone="+31685183770", activation_id="1004869420")
        service._remember_lease(lease)

        with mock.patch.object(service, "_api_get_json", return_value={"status": "BANNED"}) as mocked:
            service.mark_blacklisted(lease.phone)

        mocked.assert_called_once_with("/user/ban/1004869420")


class OAuthPhoneBlacklistTests(unittest.TestCase):
    def test_should_blacklist_explicit_phone_rejection(self):
        state = FlowState(
            page_type="add_phone",
            payload={"error": {"message": "phone number is invalid"}},
        )
        self.assertTrue(
            OAuthClient._should_blacklist_phone_failure(
                "add-phone/send 失败: 400 - phone number is invalid",
                state,
            )
        )

    def test_should_not_blacklist_whatsapp_or_delivery_failures(self):
        self.assertFalse(
            OAuthClient._should_blacklist_phone_failure(
                "add_phone 已切到 whatsapp 通道，当前 SMSToMe 仅支持短信接码"
            )
        )
        self.assertFalse(
            OAuthClient._should_blacklist_phone_failure("手机号 +447000000001 未收到短信验证码")
        )

    def test_handle_add_phone_blacklists_explicitly_rejected_number(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000001",
            detail_url="https://example.com/phone/1",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.provider_label = "SMSToMe"

        with mock.patch("platforms.chatgpt.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(
                client,
                "_send_phone_number",
                return_value=(False, None, "add-phone/send 失败: 400 - phone number is invalid"),
            ):
                state = client._handle_add_phone_verification(
                    "device-id",
                    "Mozilla/5.0",
                    None,
                    None,
                    FlowState(page_type="add_phone"),
                )

        self.assertIsNone(state)
        phone_service.mark_blacklisted.assert_called_once_with(entry.phone)
        phone_service.cancel_activation.assert_called_once_with(entry)
        self.assertIn("add_phone 阶段失败", client.last_error)

    def test_handle_add_phone_does_not_blacklist_whatsapp_channel(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneEntry(
            country_slug="united-kingdom",
            phone="+447000000002",
            detail_url="https://example.com/phone/2",
        )
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.provider_label = "SMSToMe"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )

        with mock.patch("platforms.chatgpt.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={
                        "phone_verification_channel": "whatsapp",
                        "phone_number": entry.phone,
                    },
                ):
                    state = client._handle_add_phone_verification(
                        "device-id",
                        "Mozilla/5.0",
                        None,
                        None,
                        FlowState(page_type="add_phone"),
                    )

        self.assertIsNone(state)
        phone_service.mark_blacklisted.assert_not_called()
        phone_service.cancel_activation.assert_called_once_with(entry)
        self.assertIn("whatsapp", client.last_error)

    def test_handle_add_phone_finishes_activation_after_success(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneLease(phone="+447000000003", country_slug="united-kingdom")
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.wait_for_code.return_value = "123456"
        phone_service.provider_label = "HeroSMS"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        validated_state = FlowState(page_type="consent")

        with mock.patch("platforms.chatgpt.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={
                        "phone_verification_channel": "sms",
                        "phone_number": entry.phone,
                    },
                ):
                    with mock.patch.object(
                        client,
                        "_validate_phone_otp",
                        return_value=(True, validated_state, ""),
                    ):
                        state = client._handle_add_phone_verification(
                            "device-id",
                            "Mozilla/5.0",
                            None,
                            None,
                            FlowState(page_type="add_phone"),
                        )

        self.assertEqual(state, validated_state)
        phone_service.report_code_requested.assert_called_once_with(entry)
        phone_service.finish_activation.assert_called_once_with(entry)
        phone_service.cancel_activation.assert_not_called()

    def test_handle_add_phone_reports_ready_again_after_resend(self):
        client = OAuthClient(config={}, verbose=False)
        client._log = lambda _msg: None
        entry = PhoneLease(phone="+447000000004", country_slug="united-kingdom")
        phone_service = mock.Mock()
        phone_service.enabled = True
        phone_service.max_attempts = 1
        phone_service.acquire_phone.return_value = entry
        phone_service.prefix_hint.return_value = "+447000"
        phone_service.wait_for_code.side_effect = [None, "123456"]
        phone_service.provider_label = "HeroSMS"

        next_state = FlowState(
            page_type="phone_otp_verification",
            continue_url="https://auth.openai.com/phone-verification",
        )
        validated_state = FlowState(page_type="consent")

        with mock.patch("platforms.chatgpt.oauth_client.create_phone_service", return_value=phone_service):
            with mock.patch.object(client, "_send_phone_number", return_value=(True, next_state, "")):
                with mock.patch.object(
                    client,
                    "_decode_oauth_session_cookie",
                    return_value={
                        "phone_verification_channel": "sms",
                        "phone_number": entry.phone,
                    },
                ):
                    with mock.patch.object(client, "_resend_phone_otp", return_value=(True, "")):
                        with mock.patch.object(
                            client,
                            "_validate_phone_otp",
                            return_value=(True, validated_state, ""),
                        ):
                            state = client._handle_add_phone_verification(
                                "device-id",
                                "Mozilla/5.0",
                                None,
                                None,
                                FlowState(page_type="add_phone"),
                            )

        self.assertEqual(state, validated_state)
        self.assertEqual(phone_service.report_code_requested.call_args_list, [mock.call(entry), mock.call(entry)])
        self.assertEqual(phone_service.wait_for_code.call_args_list, [mock.call(entry), mock.call(entry)])
        phone_service.finish_activation.assert_called_once_with(entry)


if __name__ == "__main__":
    unittest.main()
