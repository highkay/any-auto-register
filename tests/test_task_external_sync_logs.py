import unittest
from unittest import mock

from api.tasks import _auto_upload_integrations


class _InlineThread:
    def __init__(self, *, target, daemon):
        self._target = target

    def start(self):
        self._target()


class TaskExternalSyncLogTests(unittest.TestCase):
    def test_oauth_progress_is_written_to_task_log(self):
        def fake_sync_account(account, *, log=print):
            log("[xAI CPA] 已打开本机授权浏览器")
            return [{"name": "xAI CPA", "ok": True, "msg": "凭据已上传"}]

        with mock.patch("api.tasks.threading.Thread", _InlineThread):
            with mock.patch("api.tasks._log") as task_log:
                with mock.patch("services.external_sync.sync_account", side_effect=fake_sync_account):
                    _auto_upload_integrations("task-1", object())

        task_log.assert_has_calls(
            [
                mock.call("task-1", "  [xAI CPA] 已打开本机授权浏览器"),
                mock.call("task-1", "  [xAI CPA] [OK] 凭据已上传"),
            ]
        )


if __name__ == "__main__":
    unittest.main()
