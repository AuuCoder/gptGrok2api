from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.image_task_service import ImageTaskService


class ImageTaskServiceGrokTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "image_tasks.json"
        self.identity = {"id": "test-user", "name": "test-key", "role": "admin"}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _wait_for_task(self, service: ImageTaskService, task_id: str) -> dict:
        deadline = time.time() + 2
        while time.time() < deadline:
            items = service.list_tasks(self.identity, [task_id])["items"]
            if items and items[0]["status"] in {"success", "error"}:
                return items[0]
            time.sleep(0.01)
        self.fail(f"task {task_id} did not finish")

    def test_grok_generation_uses_runtime_handler_and_keeps_unified_log_fields(self) -> None:
        gpt_handler = Mock(side_effect=AssertionError("GPT handler must not run"))
        captured: dict = {}

        def grok_handler(payload: dict, trace: dict) -> dict:
            captured.update(payload)
            trace.update(
                {
                    "account_token": "secret-sso-token",
                    "metrics": {"account_wait_ms": 12, "egress_acquire_ms": 18},
                    "events": [
                        {"event": "image_getting_account", "account_wait_ms": 12},
                        {
                            "event": "image_egress_ready",
                            "egress_acquire_ms": 18,
                            "proxy_source": "grok_runtime",
                            "has_proxy": True,
                        },
                    ],
                    "proxy": {
                        "proxy_source": "grok_runtime",
                        "proxy_hash": "abcdef123456",
                        "has_proxy": True,
                        "egress_mode": "single_proxy",
                        "egress_key": "grok:app:abcdef123456",
                        "egress_label": "Grok app 代理 abcdef123456",
                    },
                }
            )
            return {
                "created": 1,
                "data": [{"url": "https://pro.muyuai.top/v1/files/image?id=grok-task"}],
            }

        service = ImageTaskService(
            self.path,
            generation_handler=gpt_handler,
            grok_generation_handler=grok_handler,
        )
        with patch(
            "services.image_task_service.grok_account_store.runtime_identity_for_token",
            return_value={
                "account_id": "grok-account-1",
                "account_email": "gr***k@example.com",
            },
        ), patch("services.image_task_service.log_service.add") as add_log:
            service.submit_generation(
                self.identity,
                client_task_id="grok-generation",
                prompt="draw a test image",
                model="grok-imagine-image-lite",
                n=1,
                base_url="https://pro.muyuai.top",
            )
            task = self._wait_for_task(service, "grok-generation")

        self.assertEqual(task["status"], "success")
        self.assertEqual(task["model"], "grok-imagine-image-lite")
        self.assertEqual(captured["base_url"], "https://pro.muyuai.top")
        gpt_handler.assert_not_called()
        _, summary, detail = add_log.call_args.args
        self.assertEqual(summary, "文生图调用完成")
        self.assertEqual(detail["account_email"], "gr***k@example.com")
        self.assertEqual(detail["provider"], "grok_sso")
        self.assertEqual(detail["provider_account_id"], "grok-account-1")
        self.assertEqual(detail["proxy_source"], "grok_runtime")
        self.assertEqual(detail["perf"]["account_wait_ms"], 12)
        self.assertEqual(detail["perf"]["egress_acquire_ms"], 18)
        self.assertEqual(detail["urls"], ["https://pro.muyuai.top/v1/files/image?id=grok-task"])
        self.assertNotIn("secret-sso-token", repr(detail))

    def test_grok_generation_model_maps_reference_edits_to_grok_edit(self) -> None:
        captured: dict = {}

        def grok_edit_handler(payload: dict, _trace: dict) -> dict:
            captured.update(payload)
            return {
                "created": 1,
                "data": [{"url": "https://pro.muyuai.top/v1/files/image?id=grok-edit"}],
            }

        service = ImageTaskService(
            self.path,
            edit_handler=Mock(side_effect=AssertionError("GPT edit handler must not run")),
            grok_edit_handler=grok_edit_handler,
        )
        with patch("services.image_task_service.log_service.add"):
            service.submit_edit(
                self.identity,
                client_task_id="grok-edit",
                prompt="edit this image",
                model="grok-imagine-image-lite",
                n=4,
                size="1280x720",
                images=[(b"image", "input.png", "image/png")],
                base_url="https://pro.muyuai.top",
            )
            task = self._wait_for_task(service, "grok-edit")

        self.assertEqual(task["status"], "success")
        self.assertEqual(task["model"], "grok-imagine-image-edit")
        self.assertEqual(task["n"], 2)
        self.assertEqual(task["size"], "1024x1024")
        self.assertEqual(captured["model"], "grok-imagine-image-edit")
        self.assertEqual(captured["n"], 2)
        self.assertEqual(captured["size"], "1024x1024")

    def test_gpt_generation_still_uses_existing_handler(self) -> None:
        gpt_handler = Mock(
            return_value={
                "created": 1,
                "data": [{"url": "https://pro.muyuai.top/images/gpt-task.png"}],
            }
        )
        grok_handler = Mock(side_effect=AssertionError("Grok handler must not run"))
        service = ImageTaskService(
            self.path,
            generation_handler=gpt_handler,
            grok_generation_handler=grok_handler,
        )
        with patch("services.image_task_service.log_service.add"):
            service.submit_generation(
                self.identity,
                client_task_id="gpt-generation",
                prompt="draw a GPT image",
                model="gpt-image-2",
            )
            task = self._wait_for_task(service, "gpt-generation")

        self.assertEqual(task["status"], "success")
        gpt_handler.assert_called_once()
        grok_handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
