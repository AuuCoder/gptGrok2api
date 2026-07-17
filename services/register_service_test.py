from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import services.register_service as register_service_module
from services.register import grok_register
from services.register.grok_account_store import GrokAccountStore
from services.openai_checkout_service import CheckoutSessionError
from services.register_service import RegisterService, _normalize


class RegisterServiceGrokTest(unittest.TestCase):
    def test_checkout_config_update_refreshes_active_retry_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "retry-run"
            stop_event = service._checkout_retry_stop_event
            job = {
                "key": "retry-run:checkout-1",
                "run_id": "retry-run",
                "task_id": "checkout-1",
                "index": 1,
                "email": "registered@example.test",
                "access_token": "opaque-access-token",
                "checkout": {"checkout_proxy_url": "proxy.example:1080:user:pass"},
                "stop_event": stop_event,
                "next_retry_monotonic": 0.0,
                "in_flight": False,
            }
            service._checkout_retry_jobs[job["key"]] = job

            service.update({
                "checkout": {
                    **service._config["checkout"],
                    "checkout_proxy_enabled": True,
                    "checkout_proxy_url": "socks5h://user:pass@proxy.example:1080",
                }
            })

            self.assertEqual(
                job["checkout"]["checkout_proxy_url"],
                "socks5h://user:pass@proxy.example:1080",
            )
            self.assertEqual(
                job["checkout"]["provider_proxy_url"],
                "socks5h://user:pass@proxy.example:1080",
            )

    def test_checkout_task_preserves_detailed_protocol_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "progress-run"

            service._upsert_checkout_task(
                {
                    "task_id": "checkout-progress",
                    "run_id": "progress-run",
                    "index": 1,
                    "email": "progress@example.test",
                    "channel": "upi",
                    "status": "running",
                    "stage": "stripe_provider",
                    "progress_detail": "IN Provider：刷新 Stripe 并复核 UPI 资格",
                    "attempt": 2,
                }
            )

            task = service.get()["checkout_tasks"][0]
            self.assertEqual(task["stage"], "stripe_provider")
            self.assertEqual(
                task["progress_detail"],
                "IN Provider：刷新 Stripe 并复核 UPI 资格",
            )

    def test_checkout_retry_queue_uses_its_independent_thread_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config["threads"] = 1
            service._config["checkout"]["threads"] = 3
            service._checkout_task_run_id = "retry-run"
            stop_event = service._checkout_retry_stop_event
            started: set[str] = set()
            started_lock = threading.Lock()
            all_started = threading.Event()
            release = threading.Event()

            for index in range(1, 4):
                key = f"retry-run:checkout-{index}"
                service._checkout_retry_jobs[key] = {
                    "key": key,
                    "run_id": "retry-run",
                    "task_id": f"checkout-{index}",
                    "index": index,
                    "email": f"parallel-{index}@example.test",
                    "access_token": f"parallel-token-{index}",
                    "attempt": 0,
                    "stop_event": stop_event,
                    "next_retry_monotonic": 0.0,
                    "in_flight": False,
                }

            def hold_attempt(job: dict, _event: threading.Event) -> None:
                with started_lock:
                    started.add(job["key"])
                    if len(started) == 3:
                        all_started.set()
                release.wait(2)
                with service._lock:
                    service._checkout_retry_jobs.pop(job["key"], None)

            with patch.object(service, "_run_checkout_retry_attempt", side_effect=hold_attempt):
                with service._lock:
                    service._ensure_checkout_retry_workers_locked()
                self.assertTrue(all_started.wait(2), "三个 Checkout 任务应并发被 worker 领取")
                self.assertEqual(len(started), 3)
                release.set()
                with service._lock:
                    service._cancel_checkout_retries_locked()
                for worker in service._checkout_retry_runners:
                    worker.join(2)

            self.assertEqual(service._checkout_retry_jobs, {})

    def test_upi_retry_attempt_rotates_the_full_country_specific_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "retry-run"
            stop_event = service._checkout_retry_stop_event
            job = {
                "key": "retry-run:checkout-1",
                "run_id": "retry-run",
                "task_id": "checkout-1",
                "index": 1,
                "email": "registered@example.test",
                "access_token": "opaque-access-token",
                "checkout": {
                    "checkout_proxy_url": "http://in-checkout-0.example.test:8000\nhttp://in-checkout-1.example.test:8000",
                    "promotion_proxy_url": "http://vn-0.example.test:8001\nhttp://vn-1.example.test:8001",
                    "provider_proxy_url": "http://in-provider-0.example.test:8002\nhttp://in-provider-1.example.test:8002",
                },
                "attempt": 1,
                "next_proxy_rotation": 1,
                "next_retry_monotonic": 0.0,
                "stop_event": stop_event,
            }
            service._checkout_retry_jobs[job["key"]] = job

            with (
                patch.object(register_service_module.account_service, "update_account"),
                patch.object(
                    register_service_module.openai_checkout_service,
                    "extract_and_store_checkout_link",
                    side_effect=CheckoutSessionError("UPI 最终支付链接未生成"),
                ) as extract,
            ):
                service._run_checkout_retry_attempt(job, stop_event)

            extract.assert_called_once_with(
                "opaque-access-token",
                checkout_channel="upi",
                checkout_proxy="http://in-checkout-0.example.test:8000\nhttp://in-checkout-1.example.test:8000",
                promotion_proxy="http://vn-0.example.test:8001\nhttp://vn-1.example.test:8001",
                provider_proxy="http://in-checkout-0.example.test:8000\nhttp://in-checkout-1.example.test:8000",
                proxy_rotation=1,
                progress=ANY,
            )
            self.assertIn(job["key"], service._checkout_retry_jobs)
            self.assertEqual(job["attempt"], 2)
            self.assertEqual(job["next_proxy_rotation"], 2)
            task = service.get()["checkout_tasks"][0]
            self.assertEqual(task["status"], "retrying")
            self.assertEqual(task["attempt"], 2)
            self.assertIn("轮", task["error_short"])

    def test_upi_non_free_trial_immediately_rotates_without_dropping_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "retry-run"
            stop_event = service._checkout_retry_stop_event
            job = {
                "key": "retry-run:checkout-1",
                "run_id": "retry-run",
                "task_id": "checkout-1",
                "index": 1,
                "email": "registered@example.test",
                "access_token": "opaque-access-token",
                "checkout": {
                    "checkout_proxy_url": "http://in-checkout.example.test:8000",
                    "promotion_proxy_url": "http://vn.example.test:8001",
                    "provider_proxy_url": "http://in-provider.example.test:8002",
                },
                "attempt": 1,
                "next_proxy_rotation": 9,
                "next_retry_monotonic": 0.0,
                "stop_event": stop_event,
            }
            error = CheckoutSessionError(
                "UPI 最终支付链接协议请求失败: 当前 Checkout 不是 0 元试用资格（amount=1000）",
                status_code=422,
                code="checkout_amount_mismatch",
            )
            service._checkout_retry_jobs[job["key"]] = job

            with (
                patch.object(register_service_module.account_service, "update_account") as update_account,
                patch.object(
                    register_service_module.openai_checkout_service,
                    "extract_and_store_checkout_link",
                    side_effect=error,
                ),
            ):
                service._run_checkout_retry_attempt(job, stop_event)

            self.assertFalse(service._checkout_retry_error_is_terminal(error))
            self.assertTrue(service._checkout_retry_error_is_trial_ineligible(error))
            self.assertEqual(service._checkout_retry_delay_seconds(error), 0)
            self.assertIn(job["key"], service._checkout_retry_jobs)
            self.assertEqual(job["next_proxy_rotation"], 10)
            self.assertEqual(job["attempt"], 2)
            task = service.get()["checkout_tasks"][0]
            self.assertEqual(task["status"], "retrying")
            self.assertEqual(task["error_short"], "第 2 轮：当前代理无 0 元试用资格，立即切换 IN / VN / IN 代理")
            self.assertEqual(task["next_retry_at"], "")
            self.assertEqual(
                update_account.call_args_list[-1].args,
                ("opaque-access-token", {"checkout_link_status": "pending"}),
            )

    def test_unsupported_proxy_country_format_is_terminal(self) -> None:
        error = CheckoutSessionError(
            "代理未包含可改写的 country/region 选择器或 Kookeey 地区段: proxy#test"
        )

        self.assertTrue(RegisterService._checkout_retry_error_is_terminal(error))
        self.assertEqual(
            register_service_module.openai_register._checkout_failure_short(error),
            "代理地区格式不支持",
        )

    def test_generic_decline_uses_risk_cooldown(self) -> None:
        error = CheckoutSessionError(
            "Stripe 风控拒绝（generic_decline）：SetupIntent 创建失败"
        )

        self.assertEqual(RegisterService._checkout_retry_delay_seconds(error), 30)

    def test_cloudflare_challenge_uses_longer_cooldown(self) -> None:
        error = CheckoutSessionError(
            "cloudflare_challenge: checkout/update HTTP 403",
            code="cloudflare_challenge",
        )

        self.assertEqual(RegisterService._checkout_retry_delay_seconds(error), 60)

    def test_existing_accounts_append_while_registration_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config["enabled"] = True
            service._config["checkout"] = {
                "channel": "upi",
                "country": "IN",
                "currency": "INR",
                "checkout_ui_mode": "custom",
                "continuous_retry": True,
            }
            service._checkout_task_run_id = "existing-checkout-run"
            existing_job = {
                "key": "existing-checkout-run:existing-task",
                "run_id": "existing-checkout-run",
                "task_id": "existing-task",
                "access_token": "other-token",
                "stop_event": service._checkout_retry_stop_event,
            }
            service._checkout_retry_jobs[existing_job["key"]] = existing_job
            account = {"access_token": "live-token", "email": "existing@example.test"}
            with (
                patch.object(register_service_module.account_service, "resolve_access_token", return_value="live-token"),
                patch.object(register_service_module.account_service, "get_account", return_value=account),
                patch.object(register_service_module.account_service, "update_account") as update_account,
                patch.object(service, "_enqueue_checkout_retry") as enqueue,
                patch.object(register_service_module.random, "randrange", return_value=123),
            ):
                result = service.enqueue_checkout_retries_for_accounts(["live-token", "live-token"])

            self.assertEqual(result, {"queued": 1, "skipped": 0})
            update_account.assert_called_once_with("live-token", {"checkout_link_status": "pending"}, quiet=True)
            payload = enqueue.call_args.args[0]
            self.assertEqual(payload["access_token"], "live-token")
            self.assertEqual(payload["email"], "existing@example.test")
            self.assertEqual(payload["attempt"], 0)
            self.assertEqual(payload["next_proxy_rotation"], 123)
            self.assertEqual(payload["run_id"], "existing-checkout-run")
            self.assertIs(service._checkout_retry_jobs[existing_job["key"]], existing_job)

    def test_existing_ready_pix_account_is_not_enqueued_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config["checkout"] = {
                "channel": "pix",
                "country": "BR",
                "currency": "BRL",
                "checkout_ui_mode": "custom",
                "continuous_retry": True,
            }
            service._checkout_task_run_id = "existing-checkout-run"
            account = {
                "access_token": "live-token",
                "email": "ready@example.test",
                "checkout_link_status": "ready",
                "checkout_final_url": "https://payments.stripe.com/qr/instructions/pix-ready",
            }
            with (
                patch.object(register_service_module.account_service, "resolve_access_token", return_value="live-token"),
                patch.object(register_service_module.account_service, "get_account", return_value=account),
                patch.object(register_service_module.account_service, "update_account") as update_account,
                patch.object(service, "_enqueue_checkout_retry") as enqueue,
            ):
                result = service.enqueue_checkout_retries_for_accounts(["live-token"])

            self.assertEqual(result, {"queued": 0, "skipped": 1})
            update_account.assert_not_called()
            enqueue.assert_not_called()

    def test_existing_ready_upi_account_is_not_enqueued_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config["checkout"] = {
                "channel": "upi",
                "continuous_retry": True,
            }
            service._checkout_task_run_id = "existing-checkout-run"
            account = {
                "access_token": "live-token",
                "email": "ready@example.test",
                "checkout_link_status": "ready",
                "checkout_final_url": (
                    "https://payments.stripe.com/upi/instructions/upi-ready?client_secret=secret"
                ),
            }
            with (
                patch.object(register_service_module.account_service, "resolve_access_token", return_value="live-token"),
                patch.object(register_service_module.account_service, "get_account", return_value=account),
                patch.object(register_service_module.account_service, "update_account") as update_account,
                patch.object(service, "_enqueue_checkout_retry") as enqueue,
            ):
                result = service.enqueue_checkout_retries_for_accounts(["live-token"])

            self.assertEqual(result, {"queued": 0, "skipped": 1})
            update_account.assert_not_called()
            enqueue.assert_not_called()

    def test_retry_worker_drops_existing_ready_pix_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "retry-run"
            stop_event = service._checkout_retry_stop_event
            final_url = "https://payments.stripe.com/qr/instructions/pix-ready"
            job = {
                "key": "retry-run:checkout-1",
                "run_id": "retry-run",
                "task_id": "checkout-1",
                "index": 1,
                "email": "ready@example.test",
                "access_token": "live-token",
                "checkout": {"channel": "pix"},
                "channel": "pix",
                "attempt": 3,
                "next_proxy_rotation": 4,
                "next_retry_monotonic": 0.0,
                "stop_event": stop_event,
            }
            service._checkout_retry_jobs[job["key"]] = job
            account = {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": final_url,
            }

            with (
                patch.object(register_service_module.account_service, "get_account", return_value=account),
                patch.object(register_service_module.account_service, "update_account") as update_account,
                patch.object(
                    register_service_module.openai_checkout_service,
                    "extract_and_store_checkout_link",
                ) as extract,
            ):
                service._run_checkout_retry_attempt(job, stop_event)

            self.assertNotIn(job["key"], service._checkout_retry_jobs)
            update_account.assert_not_called()
            extract.assert_not_called()
            task = service.get()["checkout_tasks"][0]
            self.assertEqual(task["status"], "success")
            self.assertEqual(task["payment_link"], final_url)

    def test_retry_worker_drops_existing_ready_upi_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "retry-run"
            stop_event = service._checkout_retry_stop_event
            final_url = "https://payments.stripe.com/upi/instructions/upi-ready?client_secret=secret"
            job = {
                "key": "retry-run:checkout-1",
                "run_id": "retry-run",
                "task_id": "checkout-1",
                "index": 1,
                "email": "ready@example.test",
                "access_token": "live-token",
                "checkout": {"channel": "upi"},
                "channel": "upi",
                "attempt": 3,
                "next_proxy_rotation": 4,
                "next_retry_monotonic": 0.0,
                "stop_event": stop_event,
            }
            service._checkout_retry_jobs[job["key"]] = job
            account = {
                "access_token": "live-token",
                "checkout_link_status": "ready",
                "checkout_final_url": final_url,
            }

            with (
                patch.object(register_service_module.account_service, "get_account", return_value=account),
                patch.object(register_service_module.account_service, "update_account") as update_account,
                patch.object(
                    register_service_module.openai_checkout_service,
                    "extract_and_store_checkout_link",
                ) as extract,
            ):
                service._run_checkout_retry_attempt(job, stop_event)

            self.assertNotIn(job["key"], service._checkout_retry_jobs)
            update_account.assert_not_called()
            extract.assert_not_called()
            task = service.get()["checkout_tasks"][0]
            self.assertEqual(task["status"], "success")
            self.assertEqual(task["payment_link"], final_url)

    def test_checkout_tasks_are_structured_scoped_and_preserved_by_registration_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "run-current"

            service._upsert_checkout_task(
                {
                    "task_id": "checkout-1-a",
                    "run_id": "run-current",
                    "index": 1,
                    "email": "registered@example.test",
                    "status": "running",
                    "stage": "stripe",
                    "channel": "upi",
                }
            )
            service._upsert_checkout_task(
                {
                    "task_id": "checkout-1-a",
                    "run_id": "run-current",
                    "status": "success",
                    "stage": "completed",
                    "payment_link": "https://payments.stripe.com/upi/instructions/upi_test",
                    "error_short": "",
                }
            )
            # A task from an earlier run, and a URL with proxy credentials,
            # must not become visible in the runtime snapshot.
            service._upsert_checkout_task(
                {
                    "task_id": "checkout-stale",
                    "run_id": "run-stale",
                    "index": 2,
                    "email": "old@example.test",
                    "status": "success",
                    "stage": "提链完成",
                    "payment_link": "https://user:proxy-secret@payments.example.test/checkout",
                }
            )

            tasks = service.get()["checkout_tasks"]
            self.assertEqual(len(tasks), 1)
            task = tasks[0]
            self.assertEqual(task["task_id"], "checkout-1-a")
            self.assertEqual(task["index"], 1)
            self.assertEqual(task["email"], "registered@example.test")
            self.assertEqual(task["status"], "success")
            self.assertEqual(task["stage"], "completed")
            self.assertEqual(task["payment_link"], "https://payments.stripe.com/upi/instructions/upi_test")
            self.assertEqual(task["error_short"], "")
            self.assertTrue(task["created_at"])
            self.assertTrue(task["updated_at"])
            self.assertTrue(task["finished_at"])
            self.assertNotIn("proxy-secret", json.dumps(tasks, ensure_ascii=False))

            service.reset()
            self.assertEqual(service.get()["checkout_tasks"], tasks)

    def test_starting_and_stopping_registration_preserves_checkout_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config["target"] = "openai"
            service._checkout_task_run_id = "existing-checkout-run"
            service._upsert_checkout_task(
                {
                    "task_id": "existing-task",
                    "run_id": "existing-checkout-run",
                    "index": 1,
                    "email": "existing@example.test",
                    "status": "retrying",
                    "stage": "retrying",
                    "channel": "pix",
                }
            )
            stop_event = service._checkout_retry_stop_event
            existing_job = {
                "key": "existing-checkout-run:existing-task",
                "run_id": "existing-checkout-run",
                "task_id": "existing-task",
                "access_token": "existing-token",
                "stop_event": stop_event,
            }
            service._checkout_retry_jobs[existing_job["key"]] = existing_job

            with (
                patch.object(service, "_sync_backend_config", return_value=SimpleNamespace()),
                patch.object(service, "_run"),
            ):
                service.start()
                service._runner.join(2)

            self.assertIs(service._checkout_retry_stop_event, stop_event)
            self.assertFalse(stop_event.is_set())
            self.assertIs(service._checkout_retry_jobs[existing_job["key"]], existing_job)
            self.assertEqual(len(service.get()["checkout_tasks"]), 1)
            self.assertEqual(
                register_service_module.openai_register.register_checkout_task_run_id,
                "existing-checkout-run",
            )

            service._upsert_checkout_task(
                {
                    "task_id": "new-registration-task",
                    "run_id": "existing-checkout-run",
                    "index": 2,
                    "email": "new@example.test",
                    "status": "running",
                    "stage": "checkout",
                    "channel": "upi",
                }
            )
            self.assertEqual(len(service.get()["checkout_tasks"]), 2)

            service.stop()
            self.assertFalse(stop_event.is_set())
            self.assertIn(existing_job["key"], service._checkout_retry_jobs)

    def test_clear_checkout_history_preserves_active_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._checkout_task_run_id = "run-current"
            for task_id, status in (
                ("queued-task", "queued"),
                ("running-task", "running"),
                ("retrying-task", "retrying"),
                ("success-task", "success"),
                ("failed-task", "failed"),
                ("cancelled-task", "cancelled"),
            ):
                service._upsert_checkout_task(
                    {
                        "task_id": task_id,
                        "run_id": "run-current",
                        "index": 1,
                        "email": f"{task_id}@example.test",
                        "status": status,
                        "stage": status,
                        "channel": "pix",
                    }
                )

            result = service.clear_checkout_history()

            self.assertEqual(result["removed"], 3)
            self.assertEqual(
                [task["task_id"] for task in result["register"]["checkout_tasks"]],
                ["queued-task", "running-task", "retrying-task"],
            )

    def test_invalid_checkout_channel_is_normalized_to_upi(self) -> None:
        config = _normalize(
            {
                "target": "openai",
                "checkout": {
                    "channel": "other",
                    "country": "us",
                    "currency": "usd",
                    "checkout_ui_mode": "hosted",
                    "threads": 7,
                },
            }
        )

        self.assertEqual(config["checkout"]["channel"], "upi")
        self.assertEqual(config["checkout"]["country"], "IN")
        self.assertEqual(config["checkout"]["currency"], "INR")
        self.assertEqual(config["checkout"]["checkout_ui_mode"], "custom")
        self.assertEqual(config["checkout"]["threads"], 7)
        self.assertEqual(RegisterService._checkout_retry_proxy_plan("upi"), "IN / VN / IN")
        self.assertEqual(RegisterService._checkout_retry_channel({}), "upi")

    def test_pix_checkout_channel_sets_br_market_and_retry_plan(self) -> None:
        config = _normalize(
            {
                "checkout": {
                    "channel": "pix",
                    "country": "IN",
                    "currency": "INR",
                }
            }
        )

        self.assertEqual(config["checkout"]["channel"], "pix")
        self.assertEqual(config["checkout"]["country"], "BR")
        self.assertEqual(config["checkout"]["currency"], "BRL")
        self.assertEqual(RegisterService._checkout_retry_proxy_plan("pix"), "BR 共享出口")
        self.assertEqual(
            RegisterService._checkout_retry_channel({"checkout": {"channel": "pix"}}),
            "pix",
        )

    def test_checkout_threads_default_independently_of_registration_threads(self) -> None:
        config = _normalize({"threads": 11, "checkout": {}})
        checkout_defaults = register_service_module.openai_register.config.get("checkout") or {}
        expected_checkout_threads = max(1, int(checkout_defaults.get("threads") or 5))

        self.assertEqual(config["threads"], 11)
        self.assertEqual(config["checkout"]["threads"], expected_checkout_threads)

    def test_sub2api_sync_config_is_normalized_independently_of_import_group_filter(self) -> None:
        config = _normalize(
            {
                "sub2api_sync": {
                    "enabled": "true",
                    "server_id": "  sub2api-primary  ",
                    "group_mode": "custom",
                    "group_id": " should-not-be-used-as-import-filter ",
                    "group_name": "  新注册账号  ",
                }
            }
        )

        self.assertEqual(
            config["sub2api_sync"],
            {
                "enabled": True,
                "server_id": "sub2api-primary",
                "group_mode": "custom",
                "group_id": "should-not-be-used-as-import-filter",
                "group_name": "新注册账号",
            },
        )

    def test_sub2api_sync_update_is_saved_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store_file = Path(temp_dir) / "register.json"
            service = RegisterService(store_file)
            expected = {
                "enabled": True,
                "server_id": "sub2api-primary",
                "group_mode": "custom",
                "group_id": "",
                "group_name": "新注册账号",
            }

            saved = service.update(
                {
                    "sub2api_sync": {
                        "enabled": "true",
                        "server_id": "  sub2api-primary  ",
                        "group_mode": "custom",
                        "group_name": "  新注册账号  ",
                    }
                }
            )

            self.assertEqual(saved["sub2api_sync"], expected)
            self.assertEqual(json.loads(store_file.read_text(encoding="utf-8"))["sub2api_sync"], expected)
            self.assertEqual(RegisterService(store_file).get()["sub2api_sync"], expected)

    def test_grok_oauth_delivery_targets_are_independent_and_default_off(self) -> None:
        defaults = _normalize({"target": "grok"})["grok"]["oauth_delivery"]
        self.assertFalse(defaults["sub2api"]["enabled"])
        self.assertFalse(defaults["cpa"]["enabled"])

        config = _normalize({
            "target": "grok",
            "grok": {
                "oauth_delivery": {
                    "sub2api": {
                        "enabled": True,
                        "server_id": " sub-server ",
                        "group_mode": "custom",
                        "group_id": "ignored",
                        "group_name": " Grok OAuth ",
                    },
                    "cpa": {
                        "enabled": False,
                        "pool_id": " cpa-pool ",
                    },
                }
            },
        })

        delivery = config["grok"]["oauth_delivery"]
        self.assertEqual(delivery["sub2api"], {
            "enabled": True,
            "server_id": "sub-server",
            "group_mode": "custom",
            "group_id": "ignored",
            "group_name": "Grok OAuth",
        })
        self.assertEqual(delivery["cpa"], {"enabled": False, "pool_id": "cpa-pool"})

    def test_checkout_channel_falls_back_to_upi_when_invalid(self) -> None:
        config = _normalize({"checkout": {"channel": "unsupported"}})

        self.assertEqual(config["checkout"]["channel"], "upi")

    def test_checkout_provider_proxy_is_derived_from_checkout_proxy(self) -> None:
        config = _normalize(
            {
                "checkout": {
                    "checkout_proxy_enabled": True,
                    "checkout_proxy_url": "  http://checkout-user:checkout-password@checkout.example.test:8888  ",
                    "promotion_proxy_enabled": True,
                    "promotion_proxy_url": "  http://promotion-user:promotion-password@promotion.example.test:9898  ",
                    "provider_proxy_enabled": True,
                    "provider_proxy_url": "  http://provider-user:provider-password@provider.example.test:9999  ",
                }
            }
        )

        self.assertTrue(config["checkout"]["checkout_proxy_enabled"])
        self.assertEqual(
            config["checkout"]["checkout_proxy_url"],
            "http://checkout-user:checkout-password@checkout.example.test:8888",
        )
        self.assertTrue(config["checkout"]["promotion_proxy_enabled"])
        self.assertEqual(
            config["checkout"]["promotion_proxy_url"],
            "http://promotion-user:promotion-password@promotion.example.test:9898",
        )
        self.assertTrue(config["checkout"]["provider_proxy_enabled"])
        self.assertEqual(
            config["checkout"]["provider_proxy_url"],
            "http://checkout-user:checkout-password@checkout.example.test:8888",
        )

        disabled = _normalize(
            {
                "checkout": {
                    "checkout_proxy_enabled": False,
                    "checkout_proxy_url": "http://ignored.example.test:8888",
                    "promotion_proxy_enabled": False,
                    "promotion_proxy_url": "http://ignored-promotion.example.test:8888",
                    "provider_proxy_enabled": False,
                    "provider_proxy_url": "http://ignored-provider.example.test:8888",
                }
            }
        )
        self.assertFalse(disabled["checkout"]["checkout_proxy_enabled"])
        self.assertEqual(disabled["checkout"]["checkout_proxy_url"], "")
        self.assertFalse(disabled["checkout"]["promotion_proxy_enabled"])
        self.assertEqual(disabled["checkout"]["promotion_proxy_url"], "")
        self.assertFalse(disabled["checkout"]["provider_proxy_enabled"])
        self.assertEqual(disabled["checkout"]["provider_proxy_url"], "")

        legacy = _normalize(
            {
                "checkout": {
                    "residential_proxy_enabled": True,
                    "residential_proxy_url": "  http://legacy-user:legacy-password@legacy.example.test:8888  ",
                }
            }
        )
        self.assertTrue(legacy["checkout"]["checkout_proxy_enabled"])
        self.assertEqual(
            legacy["checkout"]["checkout_proxy_url"],
            "http://legacy-user:legacy-password@legacy.example.test:8888",
        )
        self.assertFalse(legacy["checkout"]["promotion_proxy_enabled"])
        self.assertEqual(legacy["checkout"]["promotion_proxy_url"], "")
        self.assertTrue(legacy["checkout"]["provider_proxy_enabled"])
        self.assertEqual(
            legacy["checkout"]["provider_proxy_url"],
            "http://legacy-user:legacy-password@legacy.example.test:8888",
        )
        self.assertNotIn("residential_proxy_enabled", legacy["checkout"])

    def test_checkout_stage_proxies_accept_vendor_compact_formats(self) -> None:
        config = _normalize(
            {
                "checkout": {
                    "checkout_proxy_enabled": True,
                    "checkout_proxy_url": "checkout-user:checkout-password:checkout.example.test:8888",
                    "promotion_proxy_enabled": True,
                    "promotion_proxy_url": "promotion-user:promotion-password@promotion.example.test:9898",
                    "provider_proxy_enabled": True,
                    "provider_proxy_url": "provider.example.test:9999@provider-user:provider-password",
                }
            }
        )

        self.assertEqual(
            config["checkout"]["checkout_proxy_url"],
            "checkout-user:checkout-password:checkout.example.test:8888",
        )
        self.assertEqual(
            config["checkout"]["promotion_proxy_url"],
            "promotion-user:promotion-password@promotion.example.test:9898",
        )
        self.assertEqual(
            config["checkout"]["provider_proxy_url"],
            "checkout-user:checkout-password:checkout.example.test:8888",
        )

    def test_checkout_stage_proxy_lists_preserve_normalized_lines(self) -> None:
        config = _normalize(
            {
                "checkout": {
                    "checkout_proxy_enabled": True,
                    "checkout_proxy_url": "first.example.test:8000:checkout-user:checkout-password\ncheckout-user:checkout-password@second.example.test:8001",
                    "promotion_proxy_enabled": True,
                    "promotion_proxy_url": "promotion.example.test:8500:promotion-user:promotion-password\npromotion-user:promotion-password@promo-backup.example.test:8501",
                    "provider_proxy_enabled": True,
                    "provider_proxy_url": "provider.example.test:9000@provider-user:provider-password\nhttp://provider-user:provider-password@backup.example.test:9001",
                }
            }
        )

        self.assertEqual(
            config["checkout"]["checkout_proxy_url"],
            "first.example.test:8000:checkout-user:checkout-password\ncheckout-user:checkout-password@second.example.test:8001",
        )
        self.assertEqual(
            config["checkout"]["promotion_proxy_url"],
            "promotion.example.test:8500:promotion-user:promotion-password\npromotion-user:promotion-password@promo-backup.example.test:8501",
        )
        self.assertEqual(
            config["checkout"]["provider_proxy_url"],
            "first.example.test:8000:checkout-user:checkout-password\ncheckout-user:checkout-password@second.example.test:8001",
        )

    def test_scheme3_pix_protocol_is_preserved(self) -> None:
        config = _normalize({"checkout": {"channel": "pix", "pix_protocol": "standalone"}})

        self.assertEqual(config["checkout"]["pix_protocol"], "standalone")
        self.assertEqual(RegisterService._checkout_retry_proxy_plan("pix", "standalone"), "BR / VN / BR")

    def test_grok_normalization_forces_total_and_preserves_protocol_options(self) -> None:
        config = _normalize(
            {
                "target": "grok",
                "mode": "quota",
                "grok": {
                    "provider": "custom",
                    "api_base": "https://captcha.example.test",
                    "api_key": "secret",
                    "max_mail_retry": 7,
                    "custom_option": "kept",
                },
            }
        )

        self.assertEqual(config["target"], "grok")
        self.assertEqual(config["mode"], "total")
        self.assertEqual(config["grok"]["provider"], "custom")
        self.assertEqual(config["grok"]["max_mail_retries"], 7)
        self.assertTrue(config["grok"]["xai_cli_oauth_enabled"])
        self.assertEqual(config["grok"]["custom_option"], "kept")

        disabled = _normalize({"target": "grok", "grok": {"xai_cli_oauth_enabled": False}})
        self.assertFalse(disabled["grok"]["xai_cli_oauth_enabled"])

    def test_successful_grok_registration_starts_default_oauth_protocol(self) -> None:
        protocol_calls: list[str] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = SimpleNamespace(
                config={},
                register_log_sink=None,
                account_result_sink=None,
                worker=lambda index: {
                    "ok": True,
                    "index": index,
                    "result": {
                        "email": "oauth-default@example.com",
                        "password": "password",
                        "sso": "oauth-default-sso",
                    },
                },
            )
            account_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")

            def start_protocol(account_id: str) -> dict[str, object]:
                protocol_calls.append(account_id)
                return {"reused": False, "job": {"id": "xai-protocol-test"}}

            service = RegisterService(
                Path(temp_dir) / "register.json",
                grok_oauth_protocol_sink=start_protocol,
            )
            with patch.object(register_service_module, "_registration_backend", return_value=backend), patch.object(
                register_service_module, "grok_account_store", account_store
            ):
                service.update({"target": "grok", "total": 1, "threads": 1})
                service.start()
                self.assertIsNotNone(service._runner)
                service._runner.join(timeout=5)

            accounts = account_store.list_accounts(redacted=False)
            self.assertEqual(protocol_calls, [accounts[0]["id"]])
            self.assertTrue(
                any("Grok OAuth 授权已启动" in entry["text"] for entry in service.get()["logs"])
            )

    def test_grok_runtime_overrides_icloud_without_mutating_saved_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config = _normalize(
                {
                    "target": "grok",
                    "mail": {
                        "providers": [
                            {
                                "id": "icloud-primary",
                                "enable": True,
                                "type": "icloud_api",
                                "api_base": "https://mail.example.test",
                                "api_key": "mail-secret",
                                "project": "openai",
                                "keyword": "OpenAI",
                            }
                        ]
                    },
                }
            )

            runtime = service._runtime_config("grok")

            self.assertEqual(runtime["mail"]["providers"][0]["project"], "grok")
            self.assertEqual(runtime["mail"]["providers"][0]["keyword"], "xAI")
            self.assertEqual(service._config["mail"]["providers"][0]["project"], "openai")
            self.assertEqual(service._config["mail"]["providers"][0]["keyword"], "OpenAI")

    def test_openai_runtime_overrides_stale_grok_project_for_every_icloud_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RegisterService(Path(temp_dir) / "register.json")
            service._config = _normalize(
                {
                    "target": "grok",
                    "mail": {
                        "providers": [
                            {
                                "id": "icloud-external",
                                "enable": True,
                                "type": "icloud_api",
                                "api_base": "https://mail.example.test",
                                "api_key": "mail-secret",
                                "project": "grok",
                                "keyword": "xAI",
                            },
                            {
                                "id": "icloud-local",
                                "enable": True,
                                "type": "icloud_local",
                                "project": "grok",
                                "keyword": "xAI",
                            },
                        ]
                    },
                }
            )

            runtime = service._runtime_config("openai")

            self.assertEqual(
                [(item["project"], item["keyword"]) for item in runtime["mail"]["providers"]],
                [("openai", "OpenAI"), ("openai", "OpenAI")],
            )
            self.assertEqual(
                [(item["project"], item["keyword"]) for item in service._config["mail"]["providers"]],
                [("grok", "xAI"), ("grok", "xAI")],
            )

    def test_grok_worker_is_dispatched_and_result_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = SimpleNamespace(
                config={},
                register_log_sink=None,
                worker=lambda index: {
                    "ok": True,
                    "index": index,
                    "result": {
                        "email": "grok@example.com",
                        "password": "password",
                        "sso": "sso-token",
                    },
                },
            )
            account_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = RegisterService(Path(temp_dir) / "register.json")

            with patch.object(register_service_module, "_registration_backend", return_value=backend), patch.object(
                register_service_module, "grok_account_store", account_store
            ):
                service.update({"target": "grok", "mode": "available", "total": 1, "threads": 1})
                service.start()
                self.assertIsNotNone(service._runner)
                service._runner.join(timeout=5)

            self.assertFalse(service._runner.is_alive())
            self.assertEqual(service.get()["stats"]["success"], 1)
            self.assertEqual(service.get()["stats"]["fail"], 0)
            accounts = account_store.list_accounts(redacted=False)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["email"], "grok@example.com")
            self.assertEqual(accounts[0]["sso"], "sso-token")
            self.assertNotIn("sso-token", json.dumps(service.get(), ensure_ascii=False))
            self.assertEqual(backend.config["target"], "grok")
            self.assertEqual(backend.config["grok"]["max_mail_retry"], 3)

    def test_failed_grok_worker_persists_pending_account_without_counting_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = SimpleNamespace(
                config={},
                register_log_sink=None,
                account_result_sink=None,
                worker=lambda index: {
                    "ok": False,
                    "index": index,
                    "error": "missing sso",
                    "account_persisted": False,
                    "account": {
                        "email": "pending@example.com",
                        "password": "password",
                        "sso": "",
                        "status": "pending_sso",
                        "profile": {"session_state": "missing"},
                    },
                },
            )
            account_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = RegisterService(Path(temp_dir) / "register.json")

            with patch.object(register_service_module, "_registration_backend", return_value=backend), patch.object(
                register_service_module, "grok_account_store", account_store
            ):
                service.update({"target": "grok", "total": 1, "threads": 1})
                service.start()
                self.assertIsNotNone(service._runner)
                service._runner.join(timeout=5)

            self.assertFalse(service._runner.is_alive())
            self.assertEqual(service.get()["stats"]["success"], 0)
            self.assertEqual(service.get()["stats"]["fail"], 1)
            accounts = account_store.list_accounts(redacted=False)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["email"], "pending@example.com")
            self.assertEqual(accounts[0]["status"], "pending_sso")
            self.assertEqual(accounts[0]["sso"], "")

    def test_active_grok_results_are_imported_without_duplicate_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = SimpleNamespace(
                config={},
                register_log_sink=None,
                account_result_sink=None,
                worker=lambda index: {
                    "ok": True,
                    "index": index,
                    "result": {
                        "email": "duplicate@example.com",
                        "password": "password",
                        "sso": "same-sso-token",
                        "status": "active",
                    },
                },
            )
            account_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = RegisterService(Path(temp_dir) / "register.json")

            with patch.object(register_service_module, "_registration_backend", return_value=backend), patch.object(
                register_service_module, "grok_account_store", account_store
            ):
                service.update({"target": "grok", "total": 2, "threads": 1})
                service.start()
                self.assertIsNotNone(service._runner)
                service._runner.join(timeout=5)

            self.assertFalse(service._runner.is_alive())
            self.assertEqual(service.get()["stats"]["success"], 2)
            accounts = account_store.list_accounts(redacted=False)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["email"], "duplicate@example.com")
            self.assertEqual(accounts[0]["status"], "active")
            self.assertTrue(
                any("Grok 账号已保存" in entry["text"] for entry in service.get()["logs"])
            )

    def test_real_grok_worker_contract_integrates_with_service(self) -> None:
        protocol_calls: list[tuple[str, object]] = []

        class FakeProtocolClient:
            def __init__(self, config, *, proxy="", log=None):
                self.config = config
                self.proxy = proxy
                self.log = log

            def bootstrap(self):
                return None

            def send_email_validation_code(self, email):
                protocol_calls.append(("send", email))
                return email

            def verify_email_validation_code(self, email, code):
                protocol_calls.append(("verify", (email, code)))
                return "verification-token"

            def solve_turnstile(self):
                return "turnstile-token"

            def create_user_and_session(self, **kwargs):
                protocol_calls.append(("create", kwargs))
                return {"sso": "real-wrapper-sso", "redirect_url": "https://grok.com/"}

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            account_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = RegisterService(Path(temp_dir) / "register.json")
            create_mailbox = MagicMock(
                return_value={
                    "provider": "icloud_api",
                    "provider_ref": "icloud_api:primary",
                    "address": "wrapper@example.com",
                    "label": "Grok",
                }
            )
            original_config = dict(grok_register.config)
            try:
                with patch.object(register_service_module, "grok_account_store", account_store), patch.object(
                    grok_register, "GrokProtocolClient", FakeProtocolClient
                ), patch.object(grok_register.mail_provider, "create_mailbox", create_mailbox), patch.object(
                    grok_register.mail_provider, "wait_for_code", return_value="ABC-123"
                ), patch.object(grok_register.mail_provider, "mark_mailbox_result"):
                    service.update(
                        {
                            "target": "grok",
                            "total": 1,
                            "threads": 1,
                            "mail": {
                                "providers": [
                                    {
                                        "id": "icloud-primary",
                                        "enable": True,
                                        "type": "icloud_api",
                                        "api_base": "https://mail.example.test",
                                        "api_key": "mail-secret",
                                        "project": "openai",
                                        "keyword": "OpenAI",
                                    }
                                ]
                            },
                        }
                    )
                    service.start()
                    self.assertIsNotNone(service._runner)
                    service._runner.join(timeout=5)
            finally:
                grok_register.config.clear()
                grok_register.config.update(original_config)

            self.assertFalse(service._runner.is_alive())
            self.assertEqual(service.get()["stats"]["success"], 1)
            accounts = account_store.list_accounts(redacted=False)
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["email"], "wrapper@example.com")
            self.assertEqual(accounts[0]["sso"], "real-wrapper-sso")
            runtime_mail = create_mailbox.call_args.args[0]
            self.assertEqual(runtime_mail["providers"][0]["project"], "grok")
            self.assertEqual(runtime_mail["providers"][0]["keyword"], "xAI")
            self.assertEqual([name for name, _value in protocol_calls], ["send", "verify", "create"])
            self.assertEqual(protocol_calls[1][1], ("wrapper@example.com", "ABC-123"))


if __name__ == "__main__":
    unittest.main()
