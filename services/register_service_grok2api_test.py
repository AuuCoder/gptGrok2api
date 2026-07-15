from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import services.register_service as register_service_module
from app.platform.errors import UpstreamError
from services.register.grok_account_store import GrokAccountStore
from services.register_service import GrokAccountChatTestError, RegisterService, _normalize
from services.xai_cli_oauth_store import XaiCliOAuthAccountStore


class FakeGrok2APIClient:
    def __init__(self) -> None:
        self.enabled = True
        self.verify_on_import = True
        self.pool = "auto"
        self.list_result: dict = {"tokens": []}
        self.add_result: dict = {"status": "success", "count": 1, "skipped": 0}
        self.refresh_result: dict = {"status": "done", "summary": {"total": 1, "ok": 1, "fail": 0}}
        self.verify_result: dict = {
            "results": [{"token": "token", "status": "valid", "quota": {"remaining": 1, "total": 1}}]
        }
        self.disabled_result: dict = {"status": "done", "summary": {"total": 1, "ok": 1, "fail": 0}}
        self.delete_result: dict = {"deleted": 1}
        self.list = MagicMock(side_effect=lambda: self.list_result)
        self.add = MagicMock(side_effect=lambda _tokens: self.add_result)
        self.refresh = MagicMock(side_effect=lambda _tokens: self.refresh_result)
        self.verify = MagicMock(side_effect=lambda _tokens: self.verify_result)
        self.set_disabled = MagicMock(side_effect=lambda _tokens, _disabled: self.disabled_result)
        self.delete = MagicMock(side_effect=lambda _tokens: self.delete_result)
        self.chat_test = MagicMock(
            return_value={"model": "grok-4.3-console", "content": "pong", "elapsed_ms": 12}
        )


class RegisterServiceGrok2APITest(unittest.TestCase):
    def setUp(self) -> None:
        self.oauth_accounts_patcher = patch.object(
            register_service_module.xai_cli_oauth_store,
            "list_accounts",
            return_value=[],
        )
        self.oauth_accounts_patcher.start()

    def tearDown(self) -> None:
        self.oauth_accounts_patcher.stop()

    @staticmethod
    def _service(temp_dir: str, **grok_updates) -> RegisterService:
        service = RegisterService(Path(temp_dir) / "register.json")
        service._config = _normalize(
            {
                "target": "grok",
                "grok": {
                    "grok2api_enabled": True,
                    "grok2api_api_base": "http://grok2api.test",
                    "grok2api_admin_key": "admin-secret",
                    **grok_updates,
                },
            }
        )
        return service

    def test_grok2api_config_defaults_and_nested_aliases(self) -> None:
        defaults = _normalize({"target": "grok"})["grok"]
        nested = _normalize(
            {
                "target": "grok",
                "grok": {
                    "grok2api": {
                        "enabled": "true",
                        "api_base": " http://runtime.test/ ",
                        "admin_key": " key ",
                        "pool": "AUTO",
                        "auto_nsfw": "1",
                        "verify_on_import": "false",
                        "timeout": "45",
                    }
                },
            }
        )["grok"]

        self.assertTrue(defaults["grok2api_enabled"])
        self.assertEqual(defaults["grok2api_pool"], "auto")
        self.assertTrue(defaults["grok2api_verify_on_import"])
        self.assertEqual(defaults["grok2api_timeout"], 30)
        self.assertTrue(nested["grok2api_enabled"])
        self.assertEqual(nested["grok2api_api_base"], "")
        self.assertEqual(nested["grok2api_admin_key"], "")
        self.assertEqual(nested["grok2api_pool"], "auto")
        self.assertTrue(nested["grok2api_auto_nsfw"])
        self.assertFalse(nested["grok2api_verify_on_import"])
        self.assertEqual(nested["grok2api_timeout"], 45)
        self.assertEqual(
            _normalize({"target": "grok", "grok": {"grok2api_pool": "unknown"}})["grok"]["grok2api_pool"],
            "auto",
        )

    def test_chat_test_resolves_one_id_and_redacts_console_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "user@example.com", "password": "password", "sso": "secret-sso", "status": "active"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                result = service.chat_test_grok_account(saved["item"]["id"], prompt=" ping ")

                client.chat_test.assert_called_once_with(
                    "secret-sso",
                    prompt="ping",
                    model="grok-4.3-console",
                )
                self.assertEqual(result, {
                    "id": saved["item"]["id"],
                    "model": "grok-4.3-console",
                    "content": "pong",
                    "elapsed_ms": 12,
                })

                client.chat_test.side_effect = UpstreamError(
                    "Console API returned 403 secret-sso",
                    status=403,
                    body="permission-denied secret-sso",
                )
                with self.assertRaises(GrokAccountChatTestError) as raised:
                    service.chat_test_grok_account(saved["item"]["id"], prompt="ping")

                self.assertEqual(raised.exception.status_code, 403)
                self.assertIn("Console 权限被拒绝", str(raised.exception))
                self.assertNotIn("secret-sso", str(raised.exception))

    def test_chat_test_rejects_known_exhausted_console_quota_without_upstream_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {
                    "email": "user@example.com",
                    "password": "password",
                    "sso": "secret-sso",
                    "status": "active",
                }
            )
            account_id = saved["item"]["id"]
            store.reconcile_runtime_accounts(
                [
                    {
                        "token": "secret-sso",
                        "status": "active",
                        "pool": "basic",
                        "quota": {
                            "console": {
                                "remaining": 0,
                                "total": 20,
                                "reset_at": 9_999_999_999_999,
                                "source": 2,
                            }
                        },
                    }
                ]
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                with self.assertRaises(GrokAccountChatTestError) as raised:
                    service.chat_test_grok_account(account_id, prompt="ping")

            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("Console 对话额度已耗尽", str(raised.exception))
            client.chat_test.assert_not_called()

    def test_batch_chat_test_classifies_results_without_leaking_ssos(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            success = store.upsert({"email": "success@example.com", "sso": "success-sso", "status": "active"})
            limited = store.upsert({"email": "limited@example.com", "sso": "limited-sso", "status": "active"})
            upstream_limited = store.upsert({"email": "upstream-limited@example.com", "sso": "upstream-limited-sso", "status": "active"})
            permission = store.upsert({"email": "permission@example.com", "sso": "permission-sso", "status": "active"})
            failed = store.upsert({"email": "failed@example.com", "sso": "failed-sso", "status": "active"})
            after_failed = store.upsert({"email": "after-failed@example.com", "sso": "after-failed-sso", "status": "active"})
            skipped = store.upsert({"email": "skipped@example.com", "sso": "", "status": "pending_sso"})
            store.reconcile_runtime_accounts(
                [
                    {
                        "token": "limited-sso",
                        "status": "active",
                        "pool": "basic",
                        "quota": {
                            "console": {
                                "remaining": 0,
                                "total": 20,
                                "reset_at": 9_999_999_999_999,
                                "source": 2,
                            }
                        },
                    }
                ]
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()

            def chat_test(token: str, **_kwargs):
                if token in {"success-sso", "after-failed-sso"}:
                    return {"model": "grok-4.3-console", "content": "pong", "elapsed_ms": 8}
                if token == "upstream-limited-sso":
                    raise UpstreamError("Console API returned 429 upstream-limited-sso", status=429)
                if token == "permission-sso":
                    raise UpstreamError("Console API returned 403 permission-sso", status=403, body="permission-denied")
                raise RuntimeError(f"unexpected {token}")

            client.chat_test.side_effect = chat_test
            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                started = service.start_grok_accounts_chat_test_job(prompt="ping")
                job_id = started["job"]["id"]
                deadline = time.monotonic() + 2
                result = service.get_grok_accounts_chat_test_job(job_id)
                while result is not None and result["status"] in {"queued", "running"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    result = service.get_grok_accounts_chat_test_job(job_id)

            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["summary"],
                {"total": 7, "success": 2, "limited": 2, "permission": 1, "failed": 1, "skipped": 1, "pending": 0},
            )
            by_id = {item["id"]: item for item in result["results"]}
            self.assertEqual(by_id[success["item"]["id"]]["status"], "success")
            self.assertEqual(by_id[limited["item"]["id"]]["status"], "limited")
            self.assertEqual(by_id[upstream_limited["item"]["id"]]["status"], "limited")
            self.assertEqual(by_id[permission["item"]["id"]]["status"], "permission")
            self.assertEqual(by_id[failed["item"]["id"]]["status"], "failed")
            self.assertEqual(by_id[after_failed["item"]["id"]]["status"], "success")
            self.assertEqual(by_id[skipped["item"]["id"]]["status"], "skipped")
            self.assertEqual(by_id[success["item"]["id"]]["elapsed_ms"], 8)
            self.assertEqual(by_id[limited["item"]["id"]]["elapsed_ms"], 0)
            self.assertEqual(client.chat_test.call_count, 5)
            encoded = json.dumps(result, ensure_ascii=False)
            for secret in (
                "success-sso",
                "limited-sso",
                "upstream-limited-sso",
                "permission-sso",
                "failed-sso",
                "after-failed-sso",
            ):
                self.assertNotIn(secret, encoded)

    def test_batch_chat_test_job_reuses_active_job_and_cancels_remaining_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            first = store.upsert({"email": "first@example.com", "sso": "first-sso", "status": "active"})
            second = store.upsert({"email": "second@example.com", "sso": "second-sso", "status": "active"})
            skipped = store.upsert({"email": "skipped@example.com", "sso": "", "status": "pending_sso"})
            service = self._service(temp_dir)
            entered = threading.Event()
            release = threading.Event()

            def chat_test(account_id: str, **_kwargs):
                entered.set()
                release.wait(timeout=2)
                return {"id": account_id, "model": "grok-4.3-console", "content": "pong", "elapsed_ms": 7}

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service,
                "chat_test_grok_account",
                side_effect=chat_test,
            ) as single_test:
                started = service.start_grok_accounts_chat_test_job(prompt="ping")
                job_id = started["job"]["id"]
                self.assertTrue(entered.wait(timeout=1))

                attached = service.start_grok_accounts_chat_test_job(prompt="different prompt")
                self.assertTrue(attached["reused"])
                self.assertEqual(attached["job"]["id"], job_id)
                self.assertEqual(attached["job"]["status"], "running")

                cancelling = service.cancel_grok_accounts_chat_test_job(job_id)
                self.assertIsNotNone(cancelling)
                self.assertTrue(cancelling["cancel_requested"])
                release.set()

                deadline = time.monotonic() + 2
                result = service.get_grok_accounts_chat_test_job(job_id)
                while result is not None and result["status"] in {"queued", "running"} and time.monotonic() < deadline:
                    time.sleep(0.01)
                    result = service.get_grok_accounts_chat_test_job(job_id)

            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(result["summary"], {
                "total": 3,
                "success": 1,
                "limited": 0,
                "permission": 0,
                "failed": 0,
                "skipped": 2,
                "pending": 0,
            })
            by_id = {item["id"]: item for item in result["results"]}
            self.assertEqual(by_id[first["item"]["id"]]["status"], "success")
            self.assertEqual(by_id[second["item"]["id"]]["status"], "skipped")
            self.assertEqual(by_id[second["item"]["id"]]["error"], "任务已取消")
            self.assertEqual(by_id[skipped["item"]["id"]]["status"], "skipped")
            single_test.assert_called_once_with(first["item"]["id"], prompt="ping", model="grok-4.3-console")

    def test_active_account_auto_imports_verifies_and_deduplicates_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.add_result = {"status": "success", "count": 0, "skipped": 1}
            payload = {
                "email": "active@example.com",
                "password": "password",
                "sso": "raw-sso-token",
                "status": "active",
            }

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                service._persist_grok_account_snapshot(payload)
                service._persist_grok_account_snapshot(payload)

            self.assertEqual(len(store.list_accounts(redacted=False)), 1)
            self.assertEqual(client.add.call_count, 2)
            self.assertEqual(client.refresh.call_count, 2)
            client.add.assert_called_with(["raw-sso-token"])
            client.refresh.assert_called_with(["raw-sso-token"])
            self.assertTrue(any("已导入内置 Grok 账号池" in item["text"] for item in service.get()["logs"]))

    def test_auto_import_refresh_failure_does_not_change_local_active_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.refresh_result = {"summary": {"total": 1, "ok": 0, "fail": 1}}

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                service._persist_grok_account_snapshot(
                    {"email": "active@example.com", "password": "password", "sso": "secret-token", "status": "active"}
                )

            account = store.list_accounts(redacted=False)[0]
            self.assertEqual(account["status"], "active")
            logs = service.get()["logs"]
            self.assertTrue(any(item["level"] == "red" and "导入内置 Grok 账号池失败" in item["text"] for item in logs))
            self.assertNotIn("secret-token", json.dumps(logs, ensure_ascii=False))

    def test_sync_can_skip_refresh_when_verify_on_import_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert({"email": "user@example.com", "password": "password", "sso": "token", "status": "active"})
            service = self._service(temp_dir, grok2api_verify_on_import=False)
            client = FakeGrok2APIClient()
            client.verify_on_import = False

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                result = service.sync_grok_accounts([saved["item"]["id"]])

            self.assertEqual(result["summary"], {"total": 1, "ok": 1, "fail": 0})
            client.add.assert_called_once_with(["token"])
            client.refresh.assert_not_called()

    def test_runtime_view_merges_fields_without_exposing_sso_and_builds_global_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            active = store.upsert(
                {"email": "active@example.com", "password": "password", "sso": "runtime-secret-token", "status": "active"}
            )
            store.upsert(
                {"email": "pending@example.com", "password": "password", "sso": "", "status": "pending_sso"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.list_result = {
                "tokens": [
                    {
                        "token": "runtime-secret-token",
                        "pool": "super",
                        "status": "cooling",
                        "quota": {
                            "auto": {"remaining": 8, "total": 10, "reset_at": 123},
                            "fast": {"remaining": 4, "total": 5},
                            "expert": {"remaining": 2, "total": 3},
                            "heavy": {"remaining": 1, "total": 2},
                            "console": {"remaining": 6, "total": 7, "reset_at": 456, "source": 2},
                        },
                        "use_count": 11,
                        "fail_count": 2,
                        "last_used_at": 123456,
                        "tags": ["nsfw"],
                    },
                    {
                        "token": "remote-only-token",
                        "pool": "basic",
                        "status": "disabled",
                        "quota": {"auto": {"remaining": 3, "total": 3}},
                        "use_count": 5,
                        "fail_count": 1,
                        "tags": [],
                    },
                ]
            }

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                view = service.grok_accounts_view()
                active_filter = service.grok_accounts_view(status="active")
                normal_filter = service.grok_accounts_view(status="normal")

            encoded = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("runtime-secret-token", encoded)
            self.assertNotIn("remote-only-token", encoded)
            self.assertTrue(view["runtime_available"])
            merged = next(item for item in view["items"] if item["id"] == active["item"]["id"])
            self.assertEqual(merged["sync_state"], "synced")
            self.assertEqual(merged["pool"], "super")
            self.assertEqual(merged["runtime_status"], "cooling")
            self.assertEqual(merged["quota"]["auto"], {"remaining": 8, "total": 10})
            self.assertEqual(
                merged["quota"]["console"],
                {"remaining": 6, "total": 7, "reset_at": 456, "source": 2},
            )
            self.assertEqual(merged["use_count"], 11)
            self.assertEqual(merged["tags"], ["nsfw"])
            runtime_only = next(item for item in view["items"] if item["source_type"] == "runtime")
            self.assertEqual(runtime_only["email"], "")
            self.assertFalse(runtime_only["has_password"])
            self.assertTrue(runtime_only["has_sso"])
            self.assertEqual(runtime_only["runtime_status"], "disabled")
            self.assertEqual(runtime_only["pool"], "basic")
            self.assertEqual(view["summary"]["runtime_total"], 2)
            self.assertEqual(view["summary"]["runtime_status"], {"active": 0, "cooling": 1, "invalid": 0, "disabled": 1})
            self.assertEqual(view["summary"]["calls_total"], 19)
            self.assertEqual(view["summary"]["quota"], {"auto": 11, "fast": 4, "expert": 2, "heavy": 1, "console": 6})

            self.assertEqual(len(active_filter["items"]), 2)
            self.assertEqual(normal_filter["items"], [])

    def test_runtime_view_attaches_redacted_oauth_metadata_by_email(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "oauth-user@example.com", "password": "password", "sso": "sso-secret", "status": "active"}
            )
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "xai_cli_oauth_accounts.json")
            linked = oauth_store.upsert(
                {
                    "email": "oauth-user@example.com",
                    "subject": "oauth-subject-secret",
                    "access_token": "oauth-access-secret",
                    "refresh_token": "oauth-refresh-secret",
                    "status": "active",
                    "models": ["grok-4.5"],
                }
            )
            oauth_store.upsert(
                {
                    "email": "unlinked@example.com",
                    "refresh_token": "unlinked-refresh-secret",
                    "status": "disabled",
                }
            )
            service = self._service(temp_dir, grok2api_enabled=False)

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                view = service.grok_accounts_view()

            item = next(entry for entry in view["items"] if entry["id"] == saved["item"]["id"])
            self.assertEqual(item["oauth"]["id"], linked["item"]["id"])
            self.assertEqual(item["oauth"]["status"], "active")
            self.assertEqual(item["oauth"]["models"], ["grok-4.5"])
            self.assertEqual(view["summary"]["oauth_total"], 2)
            self.assertEqual(view["summary"]["oauth_linked"], 1)
            encoded = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("oauth-access-secret", encoded)
            self.assertNotIn("oauth-refresh-secret", encoded)
            self.assertNotIn("oauth-subject-secret", encoded)

    def test_runtime_list_failure_degrades_to_local_redacted_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            store.upsert({"email": "user@example.com", "password": "password", "sso": "never-expose", "status": "active"})
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.list.side_effect = RuntimeError("runtime unavailable")

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                view = service.grok_accounts_view()

            self.assertFalse(view["runtime_available"])
            self.assertEqual(view["runtime_error"], "runtime unavailable")
            self.assertEqual(len(view["items"]), 1)
            self.assertEqual(view["items"][0]["sync_state"], "unknown")
            self.assertNotIn("never-expose", json.dumps(view, ensure_ascii=False))

    def test_runtime_actions_resolve_stable_ids_to_raw_sso(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert({"email": "user@example.com", "password": "password", "sso": "action-secret", "status": "active"})
            account_id = saved["item"]["id"]
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                synced = service.sync_grok_accounts([account_id])
                refreshed = service.refresh_grok_accounts_runtime([account_id])
                disabled = service.set_grok_accounts_disabled([account_id], True)
                deleted = service.delete_grok_accounts([account_id], delete_upstream=True)

            self.assertEqual(synced["summary"], {"total": 1, "ok": 1, "fail": 0})
            self.assertEqual(refreshed["summary"], {"total": 1, "ok": 1, "fail": 0})
            self.assertEqual(disabled["summary"], {"total": 1, "ok": 1, "fail": 0})
            self.assertEqual(deleted, {"removed": 1, "count": 0, "upstream_deleted": 1})
            client.add.assert_called_with(["action-secret"])
            client.refresh.assert_any_call(["action-secret"])
            client.set_disabled.assert_called_once_with(["action-secret"], True)
            client.delete.assert_called_once_with(["action-secret"])
            self.assertNotIn("action-secret", json.dumps([synced, refreshed, disabled, deleted], ensure_ascii=False))

    def test_runtime_verify_resolves_stable_ids_and_never_returns_sso(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            valid = store.upsert(
                {"email": "valid@example.com", "password": "password", "sso": "verify-secret", "status": "active"}
            )
            missing_sso = store.upsert(
                {"email": "missing@example.com", "password": "password", "sso": "", "status": "pending_sso"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.verify_result = {
                "results": [
                    {
                        "token": "verify-secret",
                        "status": "valid",
                        "quota": {"remaining": 4, "total": 10, "reset_at": 123},
                    }
                ]
            }

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                result = service.verify_grok_accounts_runtime(
                    [valid["item"]["id"], missing_sso["item"]["id"], "missing-id"]
                )

            self.assertEqual(client.verify.call_args.args, (["verify-secret"],))
            self.assertEqual(
                result["summary"],
                {"total": 3, "valid": 1, "invalid": 2, "unknown": 0},
            )
            self.assertEqual(
                result["results"],
                [
                    {"id": valid["item"]["id"], "status": "valid", "quota": {"remaining": 4, "total": 10}},
                    {"id": missing_sso["item"]["id"], "status": "invalid", "error": "账号未保存 SSO 登录态"},
                    {"id": "missing-id", "status": "invalid", "error": "本地账号不存在"},
                ],
            )
            encoded = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("verify-secret", encoded)
            self.assertNotIn("password", encoded)

    def test_runtime_verify_keeps_transient_failures_unknown_and_redacts_sso(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "user@example.com", "password": "password", "sso": "transient-secret", "status": "active"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.verify.side_effect = RuntimeError("upstream 503 transient-secret")

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                result = service.verify_grok_accounts_runtime([saved["item"]["id"]])

            self.assertEqual(result["summary"], {"total": 1, "valid": 0, "invalid": 0, "unknown": 1})
            self.assertEqual(result["results"][0]["status"], "unknown")
            self.assertNotIn("transient-secret", json.dumps(result, ensure_ascii=False))

    def test_upstream_delete_failure_keeps_local_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert({"email": "user@example.com", "password": "password", "sso": "delete-secret", "status": "active"})
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.delete.side_effect = RuntimeError("delete-secret upstream unavailable")

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                with self.assertRaisesRegex(RuntimeError, "upstream unavailable") as raised:
                    service.delete_grok_accounts([saved["item"]["id"]], delete_upstream=True)

            self.assertNotIn("delete-secret", str(raised.exception))
            self.assertEqual(store.count(), 1)


if __name__ == "__main__":
    unittest.main()
