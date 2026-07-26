"""平台插件基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import os
import time


class AccountStatus(str, Enum):
    REGISTERED   = "registered"
    TRIAL        = "trial"
    SUBSCRIBED   = "subscribed"
    EXPIRED      = "expired"
    INVALID      = "invalid"


@dataclass
class Account:
    platform: str
    email: str
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    trial_end_time: int = 0       # unix timestamp
    extra: dict = field(default_factory=dict)  # 平台自定义字段
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RegisterConfig:
    """注册任务配置"""
    executor_type: str = "protocol"   # protocol | headless | headed
    # yescaptcha | local_solver | manual | capsolver | ezcaptcha | vision | auto
    captcha_solver: str = "yescaptcha"
    proxy: Optional[str] = None
    extra: dict = field(default_factory=dict)


class BasePlatform(ABC):
    # 子类必须定义
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    # 子类声明支持的执行器类型，未列出的自动降级到 protocol
    supported_executors: list = ["protocol", "headless", "headed"]

    def __init__(self, config: RegisterConfig = None):
        self.config = config or RegisterConfig()
        self._task_control = None
        requested_executor = str(self.config.executor_type or "").strip() or "protocol"
        if requested_executor not in self.supported_executors:
            fallback = (
                "protocol"
                if "protocol" in self.supported_executors
                else (self.supported_executors[0] if self.supported_executors else "protocol")
            )
            print(
                f"[{self.display_name or self.name}] 执行器 '{requested_executor}' 不受支持，"
                f"自动切换为 '{fallback}' (支持: {self.supported_executors})"
            )
            self.config.executor_type = fallback
        else:
            self.config.executor_type = requested_executor

    @abstractmethod
    def register(self, email: str, password: str = None) -> Account:
        """执行注册流程，返回 Account"""
        ...

    @abstractmethod
    def check_valid(self, account: Account) -> bool:
        """检测账号是否有效"""
        ...

    def get_trial_url(self, account: Account) -> Optional[str]:
        """生成试用激活链接（可选实现）"""
        return None

    def get_platform_actions(self) -> list:
        """
        返回平台支持的额外操作列表，每项格式:
        {"id": str, "label": str, "params": [{"key": str, "label": str, "type": str}]}
        """
        return []

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """
        执行平台特定操作，返回 {"ok": bool, "data": any, "error": str}
        """
        raise NotImplementedError(f"平台 {self.name} 不支持操作: {action_id}")

    def get_quota(self, account: Account) -> dict:
        """查询账号配额（可选实现）"""
        return {}

    def bind_task_control(self, task_control) -> None:
        """绑定协作式任务控制器，供邮箱等待/人工跳过等场景复用。"""
        self._task_control = task_control
        mailbox = getattr(self, "mailbox", None)
        if mailbox is not None:
            mailbox._task_control = task_control

    def get_mailbox_otp_timeout(self, default: int = 120) -> int:
        """统一解析邮箱 OTP 等待秒数，避免平台内散落魔法值。"""
        extra = getattr(self.config, "extra", {}) or {}
        candidates = (
            extra.get("mailbox_otp_timeout_seconds"),
            extra.get("email_otp_timeout_seconds"),
            extra.get("otp_timeout"),
            default,
        )
        for value in candidates:
            if value in (None, ""):
                continue
            try:
                resolved = int(value)
            except (TypeError, ValueError):
                continue
            if resolved > 0:
                return resolved
        return default

    def _make_executor(self):
        """根据 config 创建执行器"""
        from .executors.protocol import ProtocolExecutor
        t = self.config.executor_type
        if t == "protocol":
            return ProtocolExecutor(proxy=self.config.proxy)
        elif t == "headless":
            from .executors.playwright import PlaywrightExecutor
            return PlaywrightExecutor(proxy=self.config.proxy, headless=True)
        elif t == "headed":
            from .executors.playwright import PlaywrightExecutor
            return PlaywrightExecutor(proxy=self.config.proxy, headless=False)
        raise ValueError(f"未知执行器类型: {t}")

    def _make_captcha(self, **kwargs):
        """根据 config 创建验证码解决器（同步）。

        合法 captcha_solver:
          yescaptcha | local_solver | manual | capsolver | ezcaptcha | vision | auto
        """
        from .base_captcha import (
            CapSolverCaptcha,
            CompositeCaptcha,
            EZCaptchaCaptcha,
            LocalSolverCaptcha,
            ManualCaptcha,
            VisionCaptcha,
            YesCaptcha,
        )
        from .flags import FEATURE_CAPSOLVER, FEATURE_VISION_CAPTCHA, flag_enabled

        extra = self.config.extra or {}

        def _cfg_get(*names: str, default: str = "") -> str:
            for name in names:
                if name in kwargs and kwargs.get(name) not in (None, ""):
                    return str(kwargs.get(name)).strip()
                if extra.get(name) not in (None, ""):
                    return str(extra.get(name)).strip()
            try:
                from core.config_store import config_store

                store = config_store.get_all()
            except Exception:
                store = {}
            for name in names:
                if store.get(name) not in (None, ""):
                    return str(store.get(name)).strip()
                env_val = os.getenv(str(name or "").upper())
                if env_val not in (None, ""):
                    return str(env_val).strip()
            return default

        def _store_all() -> dict:
            try:
                from core.config_store import config_store

                return config_store.get_all()
            except Exception:
                return {}

        t = str(self.config.captcha_solver or "yescaptcha").strip().lower()
        store = _store_all()

        if t == "capsolver" and not flag_enabled(FEATURE_CAPSOLVER, store):
            raise ValueError("feature_capsolver 未启用")
        if t == "vision" and not flag_enabled(FEATURE_VISION_CAPTCHA, store):
            raise ValueError("feature_vision_captcha 未启用")

        if t == "yescaptcha":
            key = _cfg_get("key", "yescaptcha_key")
            api_base = _cfg_get("yescaptcha_api_base") or None
            return YesCaptcha(key, api_base=api_base)
        if t == "manual":
            return ManualCaptcha()
        if t == "local_solver":
            url = (
                _cfg_get("solver_url")
                or os.getenv("LOCAL_SOLVER_URL")
                or f"http://127.0.0.1:{os.getenv('SOLVER_PORT', '8889')}"
            )
            return LocalSolverCaptcha(url)
        if t == "capsolver":
            return CapSolverCaptcha(_cfg_get("capsolver_key", "key"))
        if t == "ezcaptcha":
            return EZCaptchaCaptcha(
                _cfg_get("ezcaptcha_key", "key"),
                api_base=_cfg_get("ezcaptcha_api_base") or None,
            )
        if t == "vision":
            return VisionCaptcha(
                {
                    "vision_api_base": _cfg_get("vision_api_base"),
                    "vision_api_key": _cfg_get("vision_api_key"),
                    "vision_model": _cfg_get("vision_model"),
                }
            )
        if t == "auto":
            solvers = []
            labels = []
            yes_key = _cfg_get("yescaptcha_key", "key")
            if yes_key:
                solvers.append(YesCaptcha(yes_key, api_base=_cfg_get("yescaptcha_api_base") or None))
                labels.append("yescaptcha")
            if flag_enabled(FEATURE_CAPSOLVER, store):
                cap_key = _cfg_get("capsolver_key")
                if cap_key:
                    solvers.append(CapSolverCaptcha(cap_key))
                    labels.append("capsolver")
            ez_key = _cfg_get("ezcaptcha_key")
            if ez_key:
                solvers.append(
                    EZCaptchaCaptcha(ez_key, api_base=_cfg_get("ezcaptcha_api_base") or None)
                )
                labels.append("ezcaptcha")
            if not solvers:
                raise ValueError("captcha_solver=auto 但未配置任何可用 key")
            try:
                max_attempts = int(str(_cfg_get("captcha_max_provider_attempts", default="3") or "3"))
            except (TypeError, ValueError):
                max_attempts = 3
            return CompositeCaptcha(solvers, max_provider_attempts=max_attempts, labels=labels)
        raise ValueError(f"未知验证码解决器: {t}")
