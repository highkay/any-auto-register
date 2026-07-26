"""验证码解决器基类"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import os
import time
from typing import Any, Callable


InterruptChecker = Callable[[], None] | None

_LOCAL_YESCAPTCHA_API = "http://127.0.0.1:38010"
_LOCAL_YESCAPTCHA_HEALTH = f"{_LOCAL_YESCAPTCHA_API}/api/v1/health"


def _probe_local_yescaptcha_api() -> str | None:
    try:
        import requests

        resp = requests.get(_LOCAL_YESCAPTCHA_HEALTH, timeout=0.8)
        resp.raise_for_status()
        payload = resp.json() if resp.text else {}
    except Exception:
        return None
    if isinstance(payload, dict) and str(payload.get("status") or "").strip().lower() == "ok":
        return _LOCAL_YESCAPTCHA_API
    return None


def _default_solver_url() -> str:
    return os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{os.getenv('SOLVER_PORT', '8889')}"


def _default_yescaptcha_api() -> str:
    configured = os.getenv("YESCAPTCHA_API_BASE")
    if configured:
        return configured
    return _probe_local_yescaptcha_api() or "https://api.yescaptcha.com"


class BaseCaptcha(ABC):
    @abstractmethod
    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        """返回 Turnstile token"""
        ...

    @abstractmethod
    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        """返回 hCaptcha token"""
        ...

    @abstractmethod
    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        """返回 reCAPTCHA v2 token"""
        ...

    @abstractmethod
    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        """返回图片验证码文字"""
        ...

    def solve_turnstile_session(
        self,
        page_url: str,
        site_key: str,
        *,
        session_state=None,
        widget_hints=None,
        runtime_hints=None,
        browser_proxy=None,
        options=None,
        **kwargs,
    ):
        """返回同会话 Turnstile 求解结果"""
        raise NotImplementedError

    def classify_hcaptcha(self, prompt: str, images_b64: list[str], **kwargs):
        """返回 hCaptcha 分类结果"""
        raise NotImplementedError

    def solve_aliyun(self, page_url: str, **kwargs):
        """返回 Aliyun captcha solution"""
        raise NotImplementedError

    def solve_aliyun_slide_action(self, image_b64: str, **kwargs):
        """返回 Aliyun 滑块动作结果"""
        raise NotImplementedError

    def solve_aliyun_click_start(self, image_b64: str, **kwargs):
        """返回 Aliyun 开始点击结果"""
        raise NotImplementedError

    def solve_funcaptcha(
        self,
        page_url: str,
        public_key: str,
        *,
        subdomain: str | None = None,
        blob: str | None = None,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker: InterruptChecker = None,
        **kwargs,
    ) -> str:
        """返回 Arkose / FunCaptcha token。"""
        raise NotImplementedError

    def solve_perimeterx(
        self,
        page_url: str,
        app_id: str,
        *,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker: InterruptChecker = None,
        **kwargs,
    ) -> "PerimeterXSolution":
        """返回 PerimeterX cookie 解。"""
        raise NotImplementedError

    def solve_vision_challenge(
        self,
        *,
        prompt: str,
        image_b64: str | None = None,
        images_b64: list[str] | None = None,
        answer_format: str = "ANSWER_INDEX",
        n_options: int | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker: InterruptChecker = None,
        **kwargs,
    ) -> dict:
        """多模型投票：{answer, votes, raw_texts, model_used}。不驱动 page。"""
        raise NotImplementedError


@dataclass
class PerimeterXSolution:
    """PerimeterX / HUMAN 求解结果。"""

    ok: bool
    cookies: dict[str, str] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    method: str = "none"  # capsolver | ezcaptcha | human_hold | none | yescaptcha | manual


def _default_capsolver_api() -> str:
    return (os.getenv("CAPSOLVER_API_BASE") or "https://api.capsolver.com").rstrip("/")


def _default_ezcaptcha_api() -> str:
    return (os.getenv("EZCAPTCHA_API_BASE") or "https://api.ez-captcha.com").rstrip("/")


def _run_interrupt(checker: InterruptChecker) -> None:
    if checker is not None:
        checker()


def _sleep_with_interrupt(seconds: float, interrupt_checker: InterruptChecker = None) -> None:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        _run_interrupt(interrupt_checker)
        chunk = min(0.5, remaining)
        time.sleep(chunk)
        remaining -= chunk
    _run_interrupt(interrupt_checker)


class _YesCaptchaStyleClient:
    """Shared createTask / getTaskResult polling for YesCaptcha-compatible APIs."""

    def __init__(self, client_key: str, api_base: str, *, provider_label: str = "Captcha"):
        self.client_key = str(client_key or "").strip()
        self.api = str(api_base or "").rstrip("/")
        self.provider_label = provider_label

    def create_task(self, task: dict) -> str:
        import requests
        import urllib3

        urllib3.disable_warnings()
        if not self.client_key:
            raise RuntimeError(f"{self.provider_label} 未配置 client key")
        resp = requests.post(
            f"{self.api}/createTask",
            json={"clientKey": self.client_key, "task": task},
            timeout=30,
            verify=False,
        )
        payload = resp.json() if resp.text else {}
        if isinstance(payload, dict) and int(payload.get("errorId") or 0) != 0:
            raise RuntimeError(f"{self.provider_label} 创建任务失败: {payload}")
        task_id = payload.get("taskId") if isinstance(payload, dict) else None
        if not task_id:
            raise RuntimeError(f"{self.provider_label} 创建任务失败: {resp.text}")
        return str(task_id)

    def wait_task_result(
        self,
        task_id: str,
        *,
        timeout_label: str,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker: InterruptChecker = None,
    ) -> dict:
        import requests
        import urllib3

        urllib3.disable_warnings()
        deadline = time.monotonic() + max(1.0, float(timeout_seconds or 180.0))
        poll_interval = max(0.2, float(poll_interval_seconds or 3.0))
        request_timeout = max(1.0, float(request_timeout_seconds or 30.0))
        last_payload: dict | None = None

        while True:
            _run_interrupt(interrupt_checker)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            last_payload = requests.post(
                f"{self.api}/getTaskResult",
                json={"clientKey": self.client_key, "taskId": task_id},
                timeout=min(request_timeout, max(1.0, remaining)),
                verify=False,
            ).json()
            if not isinstance(last_payload, dict):
                _sleep_with_interrupt(
                    min(poll_interval, max(0.0, deadline - time.monotonic())),
                    interrupt_checker,
                )
                continue
            if last_payload.get("status") == "ready":
                solution = last_payload.get("solution")
                if isinstance(solution, dict):
                    return solution
                raise RuntimeError(f"{self.provider_label} 返回缺少 solution: {last_payload}")
            if last_payload.get("status") == "failed" or int(last_payload.get("errorId") or 0) != 0:
                raise RuntimeError(f"{self.provider_label} 错误: {last_payload}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _sleep_with_interrupt(min(poll_interval, remaining), interrupt_checker)

        raise TimeoutError(timeout_label)


def _funcaptcha_task(
    *,
    page_url: str,
    public_key: str,
    subdomain: str | None,
    blob: str | None,
    task_type: str,
) -> dict:
    task: dict[str, Any] = {
        "type": task_type,
        "websiteURL": page_url,
        "websitePublicKey": public_key,
    }
    if subdomain:
        sub = str(subdomain).strip()
        if sub and not sub.startswith("http"):
            sub = f"https://{sub}"
        if sub:
            task["funcaptchaApiJSSubdomain"] = sub
    if blob:
        task["data"] = json.dumps({"blob": str(blob)})
    return task


def _extract_funcaptcha_token(solution: dict) -> str:
    token = solution.get("token") or solution.get("gRecaptchaResponse")
    if token:
        return str(token)
    raise RuntimeError(f"FunCaptcha 返回缺少 token: {solution}")


def _extract_perimeterx_cookies(solution: dict) -> dict[str, str]:
    cookies: dict[str, str] = {}
    raw_cookies = solution.get("cookies")
    if isinstance(raw_cookies, dict):
        for key, value in raw_cookies.items():
            if value is not None and str(value).strip():
                cookies[str(key)] = str(value)
    for key in ("_px2", "_px3", "_pxhd", "_pxvid", "px2", "px3", "pxhd", "pxvid"):
        value = solution.get(key)
        if value is not None and str(value).strip():
            name = key if key.startswith("_") else (f"_{key}" if key.startswith("px") else key)
            cookies[name] = str(value)
    return cookies


class YesCaptcha(BaseCaptcha):
    def __init__(self, client_key: str, api_base: str | None = None):
        self.client_key = client_key
        self.api = str(api_base or _default_yescaptcha_api()).rstrip("/")

    def _create_task(self, task: dict) -> str:
        import requests, urllib3

        urllib3.disable_warnings()
        r = requests.post(
            f"{self.api}/createTask",
            json={
                "clientKey": self.client_key,
                "task": task,
            },
            timeout=30,
            verify=False,
        )
        task_id = r.json().get("taskId")
        if not task_id:
            raise RuntimeError(f"YesCaptcha 创建任务失败: {r.text}")
        return str(task_id)

    def _wait_task_result(
        self,
        task_id: str,
        *,
        timeout_label: str,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> dict:
        import requests, time, urllib3

        def _checkpoint() -> None:
            if interrupt_checker is not None:
                interrupt_checker()

        def _fetch_result(timeout_seconds_value: float) -> dict:
            return requests.post(
                f"{self.api}/getTaskResult",
                json={"clientKey": self.client_key, "taskId": task_id},
                timeout=timeout_seconds_value,
                verify=False,
            ).json()

        def _coerce_result(payload: dict) -> dict | None:
            if payload.get("status") == "ready":
                solution = payload.get("solution")
                if isinstance(solution, dict):
                    return solution
                raise RuntimeError(f"YesCaptcha 返回缺少 solution: {payload}")
            if payload.get("errorId", 0) != 0:
                raise RuntimeError(f"YesCaptcha 错误: {payload}")
            return None

        def _sleep_with_interrupt(seconds: float) -> None:
            remaining = max(0.0, float(seconds))
            while remaining > 0:
                _checkpoint()
                chunk = min(0.5, remaining)
                time.sleep(chunk)
                remaining -= chunk
            _checkpoint()

        urllib3.disable_warnings()
        deadline = time.monotonic() + max(1.0, float(timeout_seconds or 180.0))
        poll_interval = max(0.2, float(poll_interval_seconds or 3.0))
        request_timeout = max(1.0, float(request_timeout_seconds or 30.0))
        last_payload: dict | None = None
        while True:
            _checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            last_payload = _fetch_result(min(request_timeout, max(1.0, remaining)))
            solution = _coerce_result(last_payload)
            if solution is not None:
                return solution
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _sleep_with_interrupt(min(poll_interval, remaining))

        _checkpoint()
        final_timeout = min(request_timeout, max(1.0, float(poll_interval_seconds or 3.0)))
        try:
            final_payload = _fetch_result(final_timeout)
        except Exception:
            final_payload = last_payload
        if isinstance(final_payload, dict):
            solution = _coerce_result(final_payload)
            if solution is not None:
                return solution
        raise TimeoutError(timeout_label)

    def solve_turnstile(
        self,
        page_url: str,
        site_key: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> str:
        task_id = self._create_task(
            {
                "type": "TurnstileTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
        )
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha Turnstile 超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        token = solution.get("token")
        if token:
            return str(token)
        raise RuntimeError(f"YesCaptcha Turnstile 返回缺少 token: {solution}")

    def solve_turnstile_session(
        self,
        page_url: str,
        site_key: str,
        *,
        session_state=None,
        widget_hints=None,
        runtime_hints=None,
        browser_proxy=None,
        options=None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> dict:
        task = {
            "type": "TurnstileTaskSessionProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        if session_state:
            task["sessionState"] = session_state
        if widget_hints:
            task["widgetHints"] = widget_hints
        if runtime_hints:
            task["runtimeHints"] = runtime_hints
        if browser_proxy:
            task["browserProxy"] = browser_proxy
        if options:
            task["options"] = options

        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha Turnstile 同会话超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        token = solution.get("token")
        if token:
            return solution
        raise RuntimeError(f"YesCaptcha Turnstile 同会话返回缺少 token: {solution}")

    def solve_hcaptcha(
        self,
        page_url: str,
        site_key: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> str:
        task_id = self._create_task(
            {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
        )
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha hCaptcha 超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        token = solution.get("gRecaptchaResponse")
        if token:
            return str(token)
        raise RuntimeError(f"YesCaptcha hCaptcha 返回缺少 gRecaptchaResponse: {solution}")

    def solve_recaptcha_v2(
        self,
        page_url: str,
        site_key: str,
        *,
        enterprise: bool = False,
        is_invisible: bool = False,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> str:
        task = {
            "type": "RecaptchaV2EnterpriseTaskProxyless"
            if enterprise
            else "NoCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        if is_invisible and not enterprise:
            task["isInvisible"] = True
        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha reCAPTCHA v2 超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        token = solution.get("gRecaptchaResponse")
        if token:
            return str(token)
        raise RuntimeError(f"YesCaptcha reCAPTCHA v2 返回缺少 gRecaptchaResponse: {solution}")

    def classify_hcaptcha(
        self,
        prompt: str,
        images_b64: list[str],
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ):
        task_id = self._create_task(
            {
                "type": "HCaptchaClassification",
                "question": prompt,
                "images": images_b64,
            }
        )
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha hCaptcha 分类超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        if "answer" in solution and solution.get("answer") is not None:
            return solution.get("answer")
        if "objects" in solution and solution.get("objects") is not None:
            return solution.get("objects")
        raise RuntimeError(f"YesCaptcha hCaptcha 分类返回缺少 answer/objects: {solution}")

    def solve_image(
        self,
        image_b64: str,
        prompt: str = "",
        *,
        schema_mode: str | None = None,
        timeout_s: float | None = None,
        model_candidates: list[str] | tuple[str, ...] | str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> str:
        task = {
            "type": "ImageToTextTask",
            "body": image_b64,
        }
        if prompt:
            task["question"] = prompt
        if schema_mode:
            task["schema_mode"] = str(schema_mode).strip()
        if timeout_s is not None:
            task["timeout_s"] = float(timeout_s)
        if model_candidates:
            if isinstance(model_candidates, str):
                task["model_candidates"] = model_candidates
            else:
                task["model_candidates"] = [str(item).strip() for item in model_candidates if str(item).strip()]
        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha 图片识别超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        text = solution.get("text")
        if text is not None:
            return str(text)
        raise RuntimeError(f"YesCaptcha 图片识别返回缺少 text: {solution}")

    def solve_funcaptcha(
        self,
        page_url: str,
        public_key: str,
        *,
        subdomain: str | None = None,
        blob: str | None = None,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 6.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
        **kwargs,
    ) -> str:
        task = _funcaptcha_task(
            page_url=page_url,
            public_key=public_key,
            subdomain=subdomain,
            blob=blob,
            task_type="FunCaptchaTaskProxyless",
        )
        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha FunCaptcha 超时",
            timeout_seconds=timeout_seconds or 200.0,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        return _extract_funcaptcha_token(solution)

    @staticmethod
    def _set_question_or_queries(task: dict, *, question: str = "", queries=None) -> None:
        if question:
            task["question"] = str(question)
            return
        if not queries:
            return
        if isinstance(queries, str):
            task["queries"] = [str(queries)]
            return
        normalized = [str(item) for item in queries if str(item)]
        if normalized:
            task["queries"] = normalized

    @staticmethod
    def _set_model_candidates(task: dict, model_candidates) -> None:
        if not model_candidates:
            return
        if isinstance(model_candidates, str):
            task["model_candidates"] = model_candidates
            return
        normalized = [str(item).strip() for item in model_candidates if str(item).strip()]
        if normalized:
            task["model_candidates"] = normalized

    def solve_aliyun_slide_action(
        self,
        image_b64: str,
        *,
        question: str = "",
        queries=None,
        background: str | None = None,
        piece: str | None = None,
        timeout_s: float | None = None,
        model_candidates: list[str] | tuple[str, ...] | str | None = None,
        project_name: str | None = None,
        schema_mode: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> dict:
        task = {
            "type": "AliyunSlideActionTask",
            "body": image_b64,
        }
        self._set_question_or_queries(task, question=question, queries=queries)
        if background:
            task["background"] = str(background)
        if piece:
            task["piece"] = str(piece)
        if timeout_s is not None:
            task["timeout_s"] = float(timeout_s)
        self._set_model_candidates(task, model_candidates)
        if project_name:
            task["project_name"] = str(project_name).strip()
        if schema_mode:
            task["schema_mode"] = str(schema_mode).strip()

        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha Aliyun slide action 超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        if isinstance(solution.get("slider"), dict) and isinstance(solution.get("gap"), dict):
            return solution
        raise RuntimeError(f"YesCaptcha Aliyun slide action 返回缺少 slider/gap: {solution}")

    def solve_aliyun_click_start(
        self,
        image_b64: str,
        *,
        question: str = "",
        queries=None,
        timeout_s: float | None = None,
        model_candidates: list[str] | tuple[str, ...] | str | None = None,
        project_name: str | None = None,
        schema_mode: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> dict:
        task = {
            "type": "AliyunClickStartTask",
            "body": image_b64,
        }
        self._set_question_or_queries(task, question=question, queries=queries)
        if timeout_s is not None:
            task["timeout_s"] = float(timeout_s)
        self._set_model_candidates(task, model_candidates)
        if project_name:
            task["project_name"] = str(project_name).strip()
        if schema_mode:
            task["schema_mode"] = str(schema_mode).strip()

        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha Aliyun click start 超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        if isinstance(solution.get("clicks"), list):
            return solution
        raise RuntimeError(f"YesCaptcha Aliyun click start 返回缺少 clicks: {solution}")

    def solve_aliyun(
        self,
        page_url: str,
        *,
        website_key: str | None = None,
        captcha_selector: str | None = None,
        trigger_selector: str | None = None,
        success_selector: str | None = None,
        mode_hint: str | None = None,
        callback_path: str | None = None,
        project_name: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 3.0,
        request_timeout_seconds: float = 30.0,
        interrupt_checker=None,
    ) -> dict:
        task = {
            "type": "AliyunCaptchaTaskProxyless",
            "websiteURL": page_url,
        }
        if website_key:
            task["websiteKey"] = str(website_key).strip()
        if captcha_selector:
            task["captchaSelector"] = str(captcha_selector).strip()
        if trigger_selector:
            task["triggerSelector"] = str(trigger_selector).strip()
        if success_selector:
            task["successSelector"] = str(success_selector).strip()
        if mode_hint:
            task["modeHint"] = str(mode_hint).strip()
        if callback_path:
            task["callbackPath"] = str(callback_path).strip()
        if project_name:
            task["project_name"] = str(project_name).strip()

        task_id = self._create_task(task)
        solution = self._wait_task_result(
            task_id,
            timeout_label="YesCaptcha Aliyun 超时",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            request_timeout_seconds=request_timeout_seconds,
            interrupt_checker=interrupt_checker,
        )
        captcha_verify_param = solution.get("captchaVerifyParam")
        if captcha_verify_param not in (None, "", {}, []):
            return solution
        token = solution.get("token")
        if token not in (None, ""):
            return solution
        raise RuntimeError(f"YesCaptcha Aliyun 返回缺少 captchaVerifyParam: {solution}")


class CapSolverCaptcha(BaseCaptcha):
    """CapSolver.com — FunCaptcha / PerimeterX / Turnstile 兜底。"""

    def __init__(self, client_key: str, api_base: str | None = None):
        self.client_key = client_key
        self.api = str(api_base or _default_capsolver_api()).rstrip("/")
        self._client = _YesCaptchaStyleClient(client_key, self.api, provider_label="CapSolver")

    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        task_id = self._client.create_task(
            {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
            }
        )
        solution = self._client.wait_task_result(
            task_id,
            timeout_label="CapSolver Turnstile 超时",
            timeout_seconds=kwargs.get("timeout_seconds"),
            interrupt_checker=kwargs.get("interrupt_checker"),
        )
        token = solution.get("token")
        if token:
            return str(token)
        raise RuntimeError(f"CapSolver Turnstile 返回缺少 token: {solution}")

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("CapSolver hCaptcha 未实现")

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("CapSolver reCAPTCHA 未实现")

    def solve_funcaptcha(
        self,
        page_url: str,
        public_key: str,
        *,
        subdomain: str | None = None,
        blob: str | None = None,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker=None,
        **kwargs,
    ) -> str:
        task = _funcaptcha_task(
            page_url=page_url,
            public_key=public_key,
            subdomain=subdomain,
            blob=blob,
            task_type="FunCaptchaTaskProxyLess",
        )
        task_id = self._client.create_task(task)
        solution = self._client.wait_task_result(
            task_id,
            timeout_label="CapSolver FunCaptcha 超时",
            timeout_seconds=timeout_seconds or 180.0,
            poll_interval_seconds=6.0,
            interrupt_checker=interrupt_checker,
        )
        return _extract_funcaptcha_token(solution)

    def solve_perimeterx(
        self,
        page_url: str,
        app_id: str,
        *,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker=None,
        **kwargs,
    ) -> PerimeterXSolution:
        task = {
            "type": "AntiPerimeterXTaskProxyLess",
            "websiteURL": page_url,
            "appId": app_id,
            "websiteKey": app_id,
        }
        task_id = self._client.create_task(task)
        solution = self._client.wait_task_result(
            task_id,
            timeout_label="CapSolver PerimeterX 超时",
            timeout_seconds=timeout_seconds or 120.0,
            poll_interval_seconds=5.0,
            interrupt_checker=interrupt_checker,
        )
        cookies = _extract_perimeterx_cookies(solution)
        return PerimeterXSolution(
            ok=bool(cookies) or bool(solution),
            cookies=cookies,
            raw=solution if isinstance(solution, dict) else {},
            method="capsolver",
        )

    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        raise NotImplementedError("CapSolver 图片识别未实现")


class EZCaptchaCaptcha(BaseCaptcha):
    """EZ-Captcha — FunCaptcha / PerimeterX。"""

    def __init__(self, client_key: str, api_base: str | None = None):
        self.client_key = client_key
        self.api = str(api_base or _default_ezcaptcha_api()).rstrip("/")
        self._client = _YesCaptchaStyleClient(client_key, self.api, provider_label="EZCaptcha")

    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("EZCaptcha Turnstile 未实现")

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("EZCaptcha hCaptcha 未实现")

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("EZCaptcha reCAPTCHA 未实现")

    def solve_funcaptcha(
        self,
        page_url: str,
        public_key: str,
        *,
        subdomain: str | None = None,
        blob: str | None = None,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker=None,
        **kwargs,
    ) -> str:
        task = _funcaptcha_task(
            page_url=page_url,
            public_key=public_key,
            subdomain=subdomain,
            blob=blob,
            task_type="FunCaptchaTaskProxyless",
        )
        task_id = self._client.create_task(task)
        solution = self._client.wait_task_result(
            task_id,
            timeout_label="EZCaptcha FunCaptcha 超时",
            timeout_seconds=timeout_seconds or 180.0,
            poll_interval_seconds=5.0,
            interrupt_checker=interrupt_checker,
        )
        return _extract_funcaptcha_token(solution)

    def solve_perimeterx(
        self,
        page_url: str,
        app_id: str,
        *,
        proxy: str | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker=None,
        **kwargs,
    ) -> PerimeterXSolution:
        task = {
            "type": "PerimeterX",
            "websiteURL": page_url,
            "websiteKey": app_id,
        }
        task_id = self._client.create_task(task)
        solution = self._client.wait_task_result(
            task_id,
            timeout_label="EZCaptcha PerimeterX 超时",
            timeout_seconds=timeout_seconds or 120.0,
            poll_interval_seconds=5.0,
            interrupt_checker=interrupt_checker,
        )
        cookies = _extract_perimeterx_cookies(solution)
        return PerimeterXSolution(
            ok=bool(cookies) or bool(solution),
            cookies=cookies,
            raw=solution if isinstance(solution, dict) else {},
            method="ezcaptcha",
        )

    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        raise NotImplementedError("EZCaptcha 图片识别未实现")


class VisionCaptcha(BaseCaptcha):
    """Multi-model vision vote only (page driving in services.vision_solver)."""

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})

    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("VisionCaptcha 不支持 Turnstile token")

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("VisionCaptcha 不支持 hCaptcha token")

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("VisionCaptcha 不支持 reCAPTCHA token")

    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        from services.vision_solver.vision import ask_vision

        text = ask_vision(
            prompt or "Read the captcha text in the image. Reply with only the text.",
            image_b64,
            interrupt_checker=kwargs.get("interrupt_checker"),
            timeout_seconds=float(kwargs.get("timeout_seconds") or 60),
        )
        if not text:
            raise RuntimeError("VisionCaptcha 图片识别失败")
        return str(text).strip().splitlines()[-1].strip()

    def solve_vision_challenge(
        self,
        *,
        prompt: str,
        image_b64: str | None = None,
        images_b64: list[str] | None = None,
        answer_format: str = "ANSWER_INDEX",
        n_options: int | None = None,
        timeout_seconds: float | None = None,
        interrupt_checker=None,
        **kwargs,
    ) -> dict:
        from services.vision_solver.vision import vote_answer

        img = image_b64
        if not img and images_b64:
            img = images_b64[0]
        if not img:
            raise ValueError("solve_vision_challenge 需要 image_b64")
        return vote_answer(
            prompt,
            img,
            n_options=n_options,
            rounds=int(kwargs.get("rounds") or 3),
            answer_format=answer_format,
            timeout_seconds=float(timeout_seconds or 55),
            interrupt_checker=interrupt_checker,
        )


class ManualCaptcha(BaseCaptcha):
    """人工打码，阻塞等待用户输入"""
    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        return input(f"请手动获取 Turnstile token ({page_url}): ").strip()

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        return input(f"请手动获取 hCaptcha token ({page_url}): ").strip()

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        return input(f"请手动获取 reCAPTCHA v2 token ({page_url}): ").strip()

    def solve_funcaptcha(self, page_url: str, public_key: str, **kwargs) -> str:
        return input(f"请手动获取 FunCaptcha token ({page_url} / {public_key}): ").strip()

    def solve_perimeterx(self, page_url: str, app_id: str, **kwargs) -> PerimeterXSolution:
        raw = input(f"请手动完成 PerimeterX 后按回车继续 ({page_url} / {app_id}): ").strip()
        return PerimeterXSolution(ok=True, cookies={}, raw={"note": raw}, method="manual")

    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        return input("请输入图片验证码: ").strip()


class LocalSolverCaptcha(BaseCaptcha):
    """调用本地 api_solver 服务解 Turnstile（Camoufox/patchright）"""

    def __init__(self, solver_url: str | None = None):
        self.solver_url = (solver_url or _default_solver_url()).rstrip("/")

    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        import requests, time
        # 提交任务
        r = requests.get(
            f"{self.solver_url}/turnstile",
            params={"url": page_url, "sitekey": site_key},
            timeout=15,
        )
        r.raise_for_status()
        task_id = r.json().get("taskId")
        if not task_id:
            raise RuntimeError(f"LocalSolver 未返回 taskId: {r.text}")
        # 轮询结果
        for _ in range(60):
            time.sleep(2)
            res = requests.get(
                f"{self.solver_url}/result",
                params={"id": task_id},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                status = data.get("status")
                if status == "ready":
                    token = data.get("solution", {}).get("token")
                    if token:
                        return token
                elif status == "CAPTCHA_FAIL":
                    raise RuntimeError("LocalSolver Turnstile 失败")
        raise TimeoutError("LocalSolver Turnstile 超时")

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("LocalSolver 暂不支持 hCaptcha")

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        raise NotImplementedError("LocalSolver 暂不支持 reCAPTCHA v2")

    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        raise NotImplementedError

    @staticmethod
    def start_solver(headless: bool = True, browser_type: str = "camoufox",
                     port: int = 8889) -> None:
        """在后台线程启动本地 solver 服务"""
        import subprocess, sys, os
        solver_path = os.path.join(
            os.path.dirname(__file__), "..", "services", "turnstile_solver", "start.py"
        )
        cmd = [
            sys.executable, solver_path,
            "--port", str(port),
            "--browser_type", browser_type,
        ]
        if not headless:
            cmd.append("--no-headless")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等待服务启动
        import time, requests
        for _ in range(20):
            time.sleep(1)
            try:
                requests.get(f"http://localhost:{port}/", timeout=2)
                return
            except Exception:
                pass
        raise RuntimeError("LocalSolver 启动超时")


class CompositeCaptcha(BaseCaptcha):
    """按挑战类型依次尝试多个 solver；节点失败则尝试下一个。"""

    def __init__(
        self,
        solvers: list[BaseCaptcha],
        *,
        max_provider_attempts: int = 3,
        labels: list[str] | None = None,
    ):
        self.solvers = list(solvers or [])
        self.max_provider_attempts = max(1, int(max_provider_attempts or 3))
        self.labels = list(labels or [])

    def _label(self, index: int, solver: BaseCaptcha) -> str:
        if index < len(self.labels) and self.labels[index]:
            return self.labels[index]
        return type(solver).__name__

    def _chain(self, method: str, *args, **kwargs):
        errors: list[str] = []
        attempts = 0
        for index, solver in enumerate(self.solvers):
            if attempts >= self.max_provider_attempts:
                break
            fn = getattr(solver, method, None)
            if not callable(fn):
                continue
            attempts += 1
            try:
                return fn(*args, **kwargs)
            except NotImplementedError:
                attempts -= 1
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{self._label(index, solver)}: {exc}")
                continue
        detail = "; ".join(errors) if errors else "无可用 solver"
        raise RuntimeError(f"CompositeCaptcha.{method} 全部失败: {detail}")

    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        return self._chain("solve_turnstile", page_url, site_key, **kwargs)

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        return self._chain("solve_hcaptcha", page_url, site_key, **kwargs)

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        return self._chain("solve_recaptcha_v2", page_url, site_key, **kwargs)

    def solve_funcaptcha(self, page_url: str, public_key: str, **kwargs) -> str:
        return self._chain("solve_funcaptcha", page_url, public_key, **kwargs)

    def solve_perimeterx(self, page_url: str, app_id: str, **kwargs) -> PerimeterXSolution:
        return self._chain("solve_perimeterx", page_url, app_id, **kwargs)

    def solve_image(self, image_b64: str, prompt: str = "", **kwargs) -> str:
        return self._chain("solve_image", image_b64, prompt, **kwargs)

    def solve_vision_challenge(self, **kwargs) -> dict:
        return self._chain("solve_vision_challenge", **kwargs)

    def solve_turnstile_session(self, page_url: str, site_key: str, **kwargs):
        return self._chain("solve_turnstile_session", page_url, site_key, **kwargs)

    def classify_hcaptcha(self, prompt: str, images_b64: list[str], **kwargs):
        return self._chain("classify_hcaptcha", prompt, images_b64, **kwargs)
