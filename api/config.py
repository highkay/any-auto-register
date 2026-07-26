from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config_store import config_store
from core.platform_email_domains import resolve_platform_blocked_email_domains
from services.mail_imports import MailImportExecuteRequest, MailImportSnapshotRequest, mail_import_registry

router = APIRouter(prefix="/config", tags=["config"])

CONFIG_KEYS = [
    "email_domain_rule_enabled",
    "email_domain_level_count",
    "laoudo_auth",
    "laoudo_email",
    "laoudo_account_id",
    "yescaptcha_key",
    "yescaptcha_api_base",
    "twocaptcha_key",
    "default_executor",
    "default_captcha_solver",
    # Experimental feature flags (default off)
    "feature_claude_register",
    "feature_github_register",
    "feature_outlook_producer",
    "feature_vision_captcha",
    "feature_capsolver",
    # Extended captcha backends
    "capsolver_key",
    "ezcaptcha_key",
    "ezcaptcha_api_base",
    "captcha_max_provider_attempts",
    # Vision
    "vision_api_base",
    "vision_api_key",
    "vision_model",
    "vision_shot_dir",
    "vision_shot_retention_days",
    "vision_max_rounds",
    "vision_review_enabled",
    # Outlook producer / GitHub
    "outlook_px_app_id",
    "outlook_px_mode",
    "outlook_extract_graph_token",
    "outlook_require_graph_token",
    "github_skip_captcha_variants",
    "github_puzzle_max_rounds",
    "duckmail_api_url",
    "duckmail_provider_url",
    "duckmail_bearer",
    "duckmail_domain",
    "duckmail_api_key",
    "freemail_api_url",
    "freemail_admin_token",
    "freemail_username",
    "freemail_password",
    "freemail_domain",
    "moemail_api_url",
    "moemail_api_key",
    "skymail_api_base",
    "skymail_token",
    "skymail_domain",
    "cloudmail_api_base",
    "cloudmail_admin_email",
    "cloudmail_admin_password",
    "cloudmail_domain",
    "cloudmail_subdomain",
    "cloudmail_timeout",
    "mail_provider",
    "outlook_backend",
    "mailbox_otp_timeout_seconds",
    "maliapi_base_url",
    "maliapi_api_key",
    "maliapi_domain",
    "maliapi_auto_domain_strategy",
    "applemail_base_url",
    "applemail_pool_dir",
    "applemail_pool_file",
    "applemail_mailboxes",
    "gptmail_base_url",
    "gptmail_api_key",
    "gptmail_mode",
    "gptmail_domain",
    "edumail_base_url",
    "edumail_domain",
    "imail_base_url",
    "imail_domain",
    "boomlify_base_url",
    "boomlify_api_base",
    "boomlify_domain",
    "nullsto_base_url",
    "nullsto_domain",
    "opentrashmail_api_url",
    "opentrashmail_domain",
    "opentrashmail_password",
    "cfrouting_domain",
    "cfrouting_imap_server",
    "cfrouting_imap_port",
    "cfrouting_username",
    "cfrouting_password",
    "cfrouting_mailboxes",
    "cfrouting_poll_interval_seconds",
    "cfworker_api_url",
    "cfworker_admin_token",
    "cfworker_custom_auth",
    "cfworker_domain",
    "cfworker_domains",
    "cfworker_enabled_domains",
    "cfworker_subdomain",
    "cfworker_random_subdomain",
    "cfworker_random_name_subdomain",
    "cfworker_fingerprint",
    "outlookemail_base_url",
    "outlookemail_password",
    "outlookemail_api_key",
    "outlookemail_group_id",
    "phone_verification_provider",
    "smstome_global_file",
    "smstome_used_numbers_dir",
    "smstome_task_name",
    "smstome_cookie",
    "smstome_country_slugs",
    "smstome_phone_attempts",
    "smstome_otp_timeout_seconds",
    "smstome_poll_interval_seconds",
    "smstome_sync_max_pages_per_country",
    "five_sim_api_key",
    "five_sim_product",
    "five_sim_country",
    "five_sim_operator",
    "five_sim_max_price",
    "five_sim_phone_attempts",
    "five_sim_otp_timeout_seconds",
    "five_sim_poll_interval_seconds",
    "hero_sms_api_key",
    "hero_sms_service",
    "hero_sms_country",
    "hero_sms_operator",
    "hero_sms_max_price",
    "hero_sms_phone_attempts",
    "hero_sms_otp_timeout_seconds",
    "hero_sms_poll_interval_seconds",
    "free_sms_tool_base_url",
    "free_sms_tool_api_key",
    "free_sms_tool_app_slug",
    "free_sms_tool_app_name",
    "free_sms_tool_country_name",
    "free_sms_tool_provider_id",
    "free_sms_tool_claim_ttl_minutes",
    "free_sms_tool_include_cooling",
    "free_sms_tool_phone_attempts",
    "free_sms_tool_otp_timeout_seconds",
    "free_sms_tool_poll_interval_seconds",
    "luckmail_base_url",
    "luckmail_api_key",
    "luckmail_email_type",
    "luckmail_domain",
    "cpa_enabled",
    "cpa_api_url",
    "cpa_api_key",
    "cpa_cleanup_enabled",
    "cpa_cleanup_interval_minutes",
    "cpa_cleanup_threshold",
    "cpa_cleanup_concurrency",
    "cpa_cleanup_register_delay_seconds",
    "sub2api_enabled",
    "sub2api_api_url",
    "sub2api_api_key",
    "sub2api_group_ids",
    "team_manager_url",
    "team_manager_key",
    "codex_proxy_url",
    "codex_proxy_key",
    "codex_proxy_upload_type",
    "cliproxyapi_base_url",
    "cliproxyapi_management_key",
    "gpt_load_enabled",
    "gpt_load_url",
    "gpt_load_admin_key",
    "gpt_load_group_name",
    "gpt_load_cerebras_group_name",
    "cerebras_full_name",
    "cerebras_use_case",
    "cerebras_mailbox_attempts",
    "deepseek_ui_locale",
    "deepseek_region",
    "deepseek_tz_offset_seconds",
    "deepseek_pow_worker_url",
    "deepseek_ds2api_enabled",
    "deepseek_ds2api_url",
    "deepseek_ds2api_admin_key",
    "zai_mailbox_attempts",
    "zai_zai2api_enabled",
    "zai_zai2api_url",
    "zai_zai2api_auth_token",
    "grok2api_url",
    "grok2api_app_key",
    "grok2api_pool",
    "grok2api_quota",
    "grok_cpa_enabled",
    "grok_cpa_management_url",
    "grok_cpa_management_token",
    "grok_cpa_auth_dir",
    "grok_cpa_proxy",
    "grok_cpa_headless",
    "grok_cpa_timeout_seconds",
    "grok_register_mode",
    "grok_browser_fallback",
    "grok_clearance_mode",
    "grok_flaresolverr_url",
    "grok_flaresolverr_attempts",
    "grok_turnstile_mode",
    "grok_turnstile_timeout",
    "grok_manual_turnstile",
    "grok_manual_turnstile_timeout",
    "grok_browser_mode",
    "grok_castle_pk",
    "grok_signup_attempts",
    "grok_cf_impersonate",
    "grok_cf_impersonate_fallback",
    "grok_mailbox_attempts",
    "grok_blocked_email_domains",
    "kiro_manager_path",
    "kiro_manager_exe",
    "qwen_cpa_enabled",
    "qwen_cpa_api_url",
    "qwen_cpa_api_key",
    "qwen_captcha_mode",
    "opengate_enabled",
    "opengate_api_url",
    "opengate_api_key",
    "qwen_blocked_email_domains",
    "chatgpt_blocked_email_domains",
    "deepseek_blocked_email_domains",
    "external_apps_update_mode",
    "contribution_enabled",
    "contribution_server_url",
    "contribution_key",
    "contribution_mode",
    "custom_contribution_url",
    "custom_contribution_token",
]


class ConfigUpdate(BaseModel):
    data: dict


class AppleMailImportRequest(BaseModel):
    content: str
    filename: str = ""
    pool_dir: str = ""
    bind_to_config: bool = True


@router.get("")
def get_config():
    all_cfg = config_store.get_all()
    if all_cfg.get("mail_provider") == "outlook":
        all_cfg["mail_provider"] = "microsoft"
    if not all_cfg.get("mail_provider"):
        all_cfg["mail_provider"] = "luckmail"
    if not all_cfg.get("applemail_base_url"):
        all_cfg["applemail_base_url"] = "https://www.appleemail.top"
    if not all_cfg.get("applemail_pool_dir"):
        all_cfg["applemail_pool_dir"] = "mail"
    if not all_cfg.get("applemail_mailboxes"):
        all_cfg["applemail_mailboxes"] = "INBOX,Junk"
    if not all_cfg.get("outlook_backend"):
        all_cfg["outlook_backend"] = "graph"
    if not all_cfg.get("cfrouting_imap_port"):
        all_cfg["cfrouting_imap_port"] = "993"
    if not all_cfg.get("cfrouting_mailboxes"):
        all_cfg["cfrouting_mailboxes"] = "INBOX"
    if not all_cfg.get("gptmail_base_url"):
        all_cfg["gptmail_base_url"] = "https://mail.chatgpt.org.uk"
    if not all_cfg.get("gptmail_mode"):
        all_cfg["gptmail_mode"] = "api"
    if not all_cfg.get("edumail_base_url"):
        all_cfg["edumail_base_url"] = "https://edumail.su"
    if not all_cfg.get("imail_base_url"):
        all_cfg["imail_base_url"] = "https://imail.edu.vn"
    if not all_cfg.get("boomlify_base_url"):
        all_cfg["boomlify_base_url"] = "https://boomlify.com/en/edu-temp-mail"
    if not all_cfg.get("boomlify_api_base"):
        all_cfg["boomlify_api_base"] = "https://v1.boomlify.com"
    if not all_cfg.get("nullsto_base_url"):
        all_cfg["nullsto_base_url"] = "https://nullsto.edu.pl"
    if not all_cfg.get("luckmail_base_url"):
        all_cfg["luckmail_base_url"] = "https://mails.luckyous.com/"
    if not all_cfg.get("outlookemail_group_id"):
        all_cfg["outlookemail_group_id"] = "1"
    if not str(all_cfg.get("contribution_enabled", "") or "").strip():
        all_cfg["contribution_enabled"] = "0"
    if not all_cfg.get("contribution_server_url"):
        all_cfg["contribution_server_url"] = "http://new.xem8k5.top:7317/"
    if not all_cfg.get("contribution_mode"):
        all_cfg["contribution_mode"] = "codex"
    if not all_cfg.get("custom_contribution_url"):
        all_cfg["custom_contribution_url"] = "http://127.0.0.1:5000"
    if not all_cfg.get("external_apps_update_mode"):
        all_cfg["external_apps_update_mode"] = "tag"
    if not all_cfg.get("yescaptcha_api_base"):
        all_cfg["yescaptcha_api_base"] = "https://api.yescaptcha.com"
    if not all_cfg.get("free_sms_tool_base_url"):
        all_cfg["free_sms_tool_base_url"] = "http://127.0.0.1:18000"
    if not all_cfg.get("phone_verification_provider"):
        all_cfg["phone_verification_provider"] = "auto"
    if not all_cfg.get("smstome_global_file"):
        all_cfg["smstome_global_file"] = "smstome_all_numbers.txt"
    if not all_cfg.get("smstome_used_numbers_dir"):
        all_cfg["smstome_used_numbers_dir"] = "smstome_used"
    if not all_cfg.get("smstome_task_name"):
        all_cfg["smstome_task_name"] = "chatgpt_add_phone"
    if not all_cfg.get("gpt_load_group_name"):
        all_cfg["gpt_load_group_name"] = "nvidia"
    if not all_cfg.get("gpt_load_cerebras_group_name"):
        all_cfg["gpt_load_cerebras_group_name"] = "cerebras"
    if not all_cfg.get("cerebras_use_case"):
        all_cfg["cerebras_use_case"] = "hobbyist"
    if not all_cfg.get("cerebras_mailbox_attempts"):
        all_cfg["cerebras_mailbox_attempts"] = "3"
    if not all_cfg.get("deepseek_ui_locale"):
        all_cfg["deepseek_ui_locale"] = "ja-JP"
    if not all_cfg.get("deepseek_region"):
        all_cfg["deepseek_region"] = "US"
    if not all_cfg.get("deepseek_tz_offset_seconds"):
        all_cfg["deepseek_tz_offset_seconds"] = "32400"
    if not all_cfg.get("deepseek_pow_worker_url"):
        all_cfg["deepseek_pow_worker_url"] = (
            "https://fe-static.deepseek.com/chat/static/33614.570c5fac7d.js"
        )
    if not str(all_cfg.get("deepseek_blocked_email_domains", "") or "").strip():
        all_cfg["deepseek_blocked_email_domains"] = ",".join(
            resolve_platform_blocked_email_domains("deepseek")
        )
    if not str(all_cfg.get("deepseek_ds2api_enabled", "") or "").strip():
        all_cfg["deepseek_ds2api_enabled"] = "0"
    if not all_cfg.get("zai_mailbox_attempts"):
        all_cfg["zai_mailbox_attempts"] = "3"
    if not str(all_cfg.get("zai_zai2api_enabled", "") or "").strip():
        all_cfg["zai_zai2api_enabled"] = "0"
    if not str(all_cfg.get("email_domain_rule_enabled", "") or "").strip():
        all_cfg["email_domain_rule_enabled"] = "0"
    if not str(all_cfg.get("email_domain_level_count", "") or "").strip():
        all_cfg["email_domain_level_count"] = "2"
    for flag_key in (
        "feature_claude_register",
        "feature_github_register",
        "feature_outlook_producer",
        "feature_vision_captcha",
        "feature_capsolver",
    ):
        if not str(all_cfg.get(flag_key, "") or "").strip():
            all_cfg[flag_key] = "0"
    if not str(all_cfg.get("captcha_max_provider_attempts", "") or "").strip():
        all_cfg["captcha_max_provider_attempts"] = "3"
    if not str(all_cfg.get("vision_review_enabled", "") or "").strip():
        all_cfg["vision_review_enabled"] = "0"
    if not str(all_cfg.get("vision_max_rounds", "") or "").strip():
        all_cfg["vision_max_rounds"] = "3"
    if not str(all_cfg.get("vision_shot_retention_days", "") or "").strip():
        all_cfg["vision_shot_retention_days"] = "3"
    # 只返回已知 key，未设置的返回空字符串
    return {k: all_cfg.get(k, "") for k in CONFIG_KEYS}


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key
    safe = {k: v for k, v in body.data.items() if k in CONFIG_KEYS}
    if safe.get("mail_provider") == "outlook":
        safe["mail_provider"] = "microsoft"
    if "phone_verification_provider" in safe:
        provider = str(safe.get("phone_verification_provider", "")).strip().lower()
        safe["phone_verification_provider"] = provider or "auto"
    if "email_domain_rule_enabled" in safe:
        enabled = str(safe.get("email_domain_rule_enabled", "")).strip().lower()
        safe["email_domain_rule_enabled"] = (
            "1" if enabled in {"1", "true", "yes", "on"} else "0"
        )
    if "email_domain_level_count" in safe:
        try:
            level_count = int(str(safe.get("email_domain_level_count", "")).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="域名级数必须是整数") from exc
        if level_count < 2:
            raise HTTPException(status_code=400, detail="域名级数不能小于 2")
        safe["email_domain_level_count"] = str(level_count)
    from core.flags import FEATURE_FLAG_KEYS, normalize_flag_value

    for flag_key in FEATURE_FLAG_KEYS:
        if flag_key in safe:
            safe[flag_key] = normalize_flag_value(safe.get(flag_key))
    if "captcha_max_provider_attempts" in safe:
        try:
            attempts = int(str(safe.get("captcha_max_provider_attempts", "")).strip() or "3")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="captcha_max_provider_attempts 必须是整数") from exc
        if attempts < 1:
            raise HTTPException(status_code=400, detail="captcha_max_provider_attempts 不能小于 1")
        safe["captcha_max_provider_attempts"] = str(attempts)
    if "vision_review_enabled" in safe:
        safe["vision_review_enabled"] = normalize_flag_value(safe.get("vision_review_enabled"))
    for bool_key in (
        "outlook_extract_graph_token",
        "outlook_require_graph_token",
    ):
        if bool_key in safe:
            safe[bool_key] = normalize_flag_value(safe.get(bool_key))
    config_store.set_many(safe)
    return {"ok": True, "updated": list(safe.keys())}


@router.post("/applemail/import")
def import_applemail_pool(body: AppleMailImportRequest):
    try:
        strategy = mail_import_registry.get("applemail")
        result = strategy.execute(
            MailImportExecuteRequest(
                type="applemail",
                content=body.content,
                filename=body.filename,
                pool_dir=body.pool_dir,
                bind_to_config=body.bind_to_config,
            )
        )
        snapshot = result.snapshot.model_dump()
        return {
            "filename": snapshot["filename"],
            "path": result.meta.get("path", ""),
            "count": snapshot["count"],
            "pool_dir": snapshot["pool_dir"],
            "bound_to_config": bool(result.meta.get("bound_to_config")),
            "items": snapshot["items"],
            "truncated": snapshot["truncated"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/applemail/pool")
def get_applemail_pool_snapshot(
    pool_dir: str = "",
    pool_file: str = "",
):
    try:
        strategy = mail_import_registry.get("applemail")
        snapshot = strategy.get_snapshot(
            MailImportSnapshotRequest(
                type="applemail",
                pool_dir=pool_dir,
                pool_file=pool_file,
            )
        )
        return snapshot.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
