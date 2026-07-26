import unittest
from unittest import mock
from unittest.mock import patch

from fastapi import HTTPException

from api.tasks import (
    RegisterTaskRequest,
    _create_task_record,
    _effective_register_concurrency,
    _run_register,
    _task_store,
    enqueue_register_task,
)
from core.base_mailbox import BaseMailbox, MailboxAccount
from core.base_platform import Account, BasePlatform


class _FakeMailbox(BaseMailbox):
    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        def poll_once():
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=0.01,
            poll_once=poll_once,
        )


class _FakePlatform(BasePlatform):
    name = "fake"
    display_name = "Fake"

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        account = self.mailbox.get_email()
        self.mailbox.wait_for_code(account, timeout=1)
        return Account(
            platform="fake",
            email=account.email,
            password=password or "pw",
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _FakeChatGPTWorkspacePlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"

    _counter = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    @classmethod
    def reset_counter(cls):
        cls._counter = 0

    def register(self, email: str, password: str = None) -> Account:
        type(self)._counter += 1
        index = type(self)._counter
        return Account(
            platform="chatgpt",
            email=f"user{index}@example.com",
            password=password or "pw",
            extra={"workspace_id": f"ws-{index}"},
        )

    def check_valid(self, account: Account) -> bool:
        return True


class _RetryableProxyBlockPlatform(BasePlatform):
    name = "grok"
    display_name = "Grok"

    attempts: list[str | None] = []

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    @classmethod
    def reset_attempts(cls):
        cls.attempts = []

    def register(self, email: str, password: str = None) -> Account:
        type(self).attempts.append(self.config.proxy)
        if "bad-proxy" in str(self.config.proxy or ""):
            raise RuntimeError(
                "Grok 注册页被 Cloudflare/WAF 封禁，当前代理不可用: title=Attention Required! | Cloudflare"
            )
        return Account(
            platform="grok",
            email="retry-success@example.com",
            password=password or "pw",
        )

    def check_valid(self, account: Account) -> bool:
        return True


class RegisterTaskControlFlowTests(unittest.TestCase):
    def _build_request(self, **overrides):
        payload = {
            "platform": "fake",
            "count": 1,
            "concurrency": 1,
            "proxy": "http://proxy.local:8080",
            "extra": {"mail_provider": "fake"},
        }
        payload.update(overrides)
        return RegisterTaskRequest(**payload)

    def _run_with_control(self, task_id: str, *, stop: bool = False, skip: bool = False):
        req = self._build_request()
        _create_task_record(task_id, req, "manual", None)
        if stop:
            _task_store.request_stop(task_id)
        if skip:
            _task_store.request_skip_current(task_id)

        with (
            patch("core.registry.get", return_value=_FakePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id)

    def test_skip_current_marks_attempt_as_skipped(self):
        snapshot = self._run_with_control("task-control-skip", skip=True)

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], [])

    def test_stop_marks_task_as_stopped(self):
        snapshot = self._run_with_control("task-control-stop", stop=True)

        self.assertEqual(snapshot["status"], "stopped")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["skipped"], 0)
        self.assertEqual(snapshot["errors"], [])

    def test_chatgpt_logs_success_and_final_summary(self):
        task_id = "task-chatgpt-workspace-progress"
        req = self._build_request(platform="chatgpt", count=2, concurrency=1)
        _create_task_record(task_id, req, "manual", None)
        _FakeChatGPTWorkspacePlatform.reset_counter()

        with (
            patch("core.registry.get", return_value=_FakeChatGPTWorkspacePlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        joined_logs = "\n".join(snapshot["logs"])

        self.assertIn("[OK] 注册成功: user1@example.com", joined_logs)
        self.assertIn("[OK] 注册成功: user2@example.com", joined_logs)
        self.assertIn("完成: 成功 2 个, 跳过 0 个, 失败 0 个", joined_logs)

    def test_deepseek_forces_serial_concurrency(self):
        req = self._build_request(platform="deepseek", count=10, concurrency=3)

        self.assertEqual(_effective_register_concurrency(req), 1)

    def test_other_platforms_keep_requested_concurrency(self):
        req = self._build_request(platform="chatgpt", count=10, concurrency=3)

        self.assertEqual(_effective_register_concurrency(req), 3)

    def test_deepseek_rejects_second_manual_task_while_active(self):
        task_id = "task-deepseek-active-guard"
        req = self._build_request(platform="deepseek", count=1, concurrency=1)
        _create_task_record(task_id, req, "manual", None)
        background_tasks = mock.Mock()

        try:
            with self.assertRaises(HTTPException) as ctx:
                enqueue_register_task(req, background_tasks=background_tasks)

            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("DeepSeek", ctx.exception.detail)
            background_tasks.add_task.assert_not_called()
        finally:
            _task_store._records.pop(task_id, None)

    def test_grok_retries_next_proxy_when_cloudflare_blocks_current_proxy(self):
        task_id = "task-grok-proxy-retry"
        req = self._build_request(platform="grok", count=1, concurrency=1, proxy=None)
        _create_task_record(task_id, req, "manual", None)
        _RetryableProxyBlockPlatform.reset_attempts()

        with (
            patch("core.registry.get", return_value=_RetryableProxyBlockPlatform),
            patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            patch("core.db.save_account", side_effect=lambda account: account),
            patch("api.tasks._save_task_log"),
            patch(
                "core.proxy_pool.proxy_pool.get_next",
                side_effect=["http://bad-proxy:8080", "http://good-proxy:8080"],
            ),
            patch("core.proxy_pool.proxy_pool.report_fail") as report_fail_mock,
            patch("core.proxy_pool.proxy_pool.report_success") as report_success_mock,
        ):
            _run_register(task_id, req)

        snapshot = _task_store.snapshot(task_id)
        joined_logs = "\n".join(snapshot["logs"])

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        self.assertIn("当前代理被目标站封禁", joined_logs)
        self.assertEqual(
            _RetryableProxyBlockPlatform.attempts,
            ["http://bad-proxy:8080", "http://good-proxy:8080"],
        )
        report_fail_mock.assert_called_once_with("http://bad-proxy:8080")
        report_success_mock.assert_called_once_with("http://good-proxy:8080")


if __name__ == "__main__":
    unittest.main()
