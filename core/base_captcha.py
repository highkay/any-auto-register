"""验证码解决器基类"""
from abc import ABC, abstractmethod
import os


def _default_solver_url() -> str:
    return os.getenv("LOCAL_SOLVER_URL") or f"http://127.0.0.1:{os.getenv('SOLVER_PORT', '8889')}"


def _default_yescaptcha_api() -> str:
    return os.getenv("YESCAPTCHA_API_BASE") or "https://api.yescaptcha.com"


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

    def classify_hcaptcha(self, prompt: str, images_b64: list[str], **kwargs):
        """返回 hCaptcha 分类结果"""
        raise NotImplementedError


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
        while True:
            _checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            d = requests.post(
                f"{self.api}/getTaskResult",
                json={"clientKey": self.client_key, "taskId": task_id},
                timeout=min(request_timeout, max(1.0, remaining)),
                verify=False,
            ).json()
            if d.get("status") == "ready":
                solution = d.get("solution")
                if isinstance(solution, dict):
                    return solution
                raise RuntimeError(f"YesCaptcha 返回缺少 solution: {d}")
            if d.get("errorId", 0) != 0:
                raise RuntimeError(f"YesCaptcha 错误: {d}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _sleep_with_interrupt(min(poll_interval, remaining))
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


class ManualCaptcha(BaseCaptcha):
    """人工打码，阻塞等待用户输入"""
    def solve_turnstile(self, page_url: str, site_key: str, **kwargs) -> str:
        return input(f"请手动获取 Turnstile token ({page_url}): ").strip()

    def solve_hcaptcha(self, page_url: str, site_key: str, **kwargs) -> str:
        return input(f"请手动获取 hCaptcha token ({page_url}): ").strip()

    def solve_recaptcha_v2(self, page_url: str, site_key: str, **kwargs) -> str:
        return input(f"请手动获取 reCAPTCHA v2 token ({page_url}): ").strip()

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
