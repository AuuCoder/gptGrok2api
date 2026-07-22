from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import services.register_service as register_service_module
from app.platform.errors import UpstreamError
from services.register.grok_account_store import GrokAccountStore
from services.register_service import GrokAccountChatTestError, RegisterService, _grok_oauth_display_status, _normalize
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
        self.readiness = MagicMock(return_value=(True, ""))


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
        self.assertEqual(
            defaults["probe_scheduler"],
            {
                "interval_minutes": 60,
                "batch_size": 50,
                "last_started_at": "",
                "last_finished_at": "",
                "oauth_recovery_last_sweep_at": "",
                "last_result": {},
                "last_error": "",
                "events": [],
            },
        )
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

    def test_grok_oauth_display_status_matches_account_table(self) -> None:
        self.assertEqual(_grok_oauth_display_status(None), "unauthorized")
        self.assertEqual(_grok_oauth_display_status({"status": "active"}), "normal")
        self.assertEqual(
            _grok_oauth_display_status({"status": "active", "probe": {"status": "limited"}}),
            "limited",
        )
        self.assertEqual(
            _grok_oauth_display_status({"status": "expired", "probe": {"status": "valid"}}),
            "expired",
        )
        self.assertEqual(
            _grok_oauth_display_status({"status": "invalid", "probe": {"status": "valid"}}),
            "invalid",
        )

    def test_manual_oauth_authorization_queues_only_eligible_unlinked_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            queued = grok_store.upsert(
                {
                    "email": "queued@example.com",
                    "password": "password",
                    "sso": "queued-sso",
                    "status": "active",
                }
            )["item"]
            reused = grok_store.upsert(
                {
                    "email": "reused@example.com",
                    "password": "password",
                    "sso": "reused-sso",
                    "status": "active",
                }
            )["item"]
            missing_password = grok_store.upsert(
                {
                    "email": "missing@example.com",
                    "sso": "missing-sso",
                    "status": "active",
                }
            )["item"]
            linked = grok_store.upsert(
                {
                    "email": "linked@example.com",
                    "password": "password",
                    "sso": "linked-sso",
                    "status": "active",
                }
            )["item"]
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            oauth_store.upsert(
                {
                    "email": "linked@example.com",
                    "subject": "linked-subject",
                    "access_token": "linked-access",
                    "refresh_token": "linked-refresh",
                    "expires_in": 3600,
                }
            )
            sink = MagicMock(
                side_effect=[
                    {"queued": True, "job": {"id": "queued-job"}},
                    {"reused": True, "job": {"id": "reused-job"}},
                ]
            )
            service = self._service(temp_dir)
            service._grok_oauth_protocol_sink = sink

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                result = service.authorize_grok_accounts_oauth(
                    [queued["id"], reused["id"], missing_password["id"], linked["id"]]
                )

            self.assertEqual(
                result["summary"],
                {"total": 4, "queued": 1, "reused": 1, "skipped": 1, "failed": 1},
            )
            self.assertEqual([item["status"] for item in result["results"]], [
                "queued",
                "reused",
                "failed",
                "already_authorized",
            ])
            self.assertEqual(
                [(call.args, call.kwargs) for call in sink.call_args_list],
                [((queued["id"],), {"prioritize": True}), ((reused["id"],), {"prioritize": True})],
            )

    def test_grok_oauth_log_masks_email(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir)

            service.handle_grok_oauth_protocol_event(
                {
                    "status": "failed",
                    "email": "complete-address@example.com",
                    "error": "authorization failed",
                }
            )

            text = service.get()["grok_oauth_logs"][-1]["text"]
            self.assertIn("co***s@example.com", text)
            self.assertNotIn("complete-address@example.com", text)

    def test_grok_probe_scheduler_runs_in_background_even_with_legacy_disabled_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(
                temp_dir,
                probe_scheduler={"enabled": False, "interval_minutes": 15, "batch_size": 25},
            )

            status = service.grok_probe_scheduler_status()

            self.assertNotIn("enabled", status)
            self.assertNotIn("queued", status)
            self.assertEqual(status["interval_minutes"], 15)
            self.assertEqual(status["batch_size"], 25)
            self.assertTrue(status["next_run_at"])

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
            self.assertTrue(any("Grok 账号已保存并加入账号池" in item["text"] for item in service.get()["logs"]))

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
                        "refresh_status": "failed",
                        "refresh_at": 123460,
                        "refresh_error": "上游未返回真实额度数据",
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
            store.reconcile_runtime_accounts(client.list_result["tokens"])

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                view = service.grok_accounts_view()
                active_filter = service.grok_accounts_view(status="active")
                normal_filter = service.grok_accounts_view(status="normal")
                refresh_failed_filter = service.grok_accounts_view(status="refresh_failed")

            client.list.assert_not_called()

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
            self.assertEqual(merged["refresh_status"], "failed")
            self.assertEqual(merged["refresh_at"], 123460)
            self.assertEqual(merged["refresh_error"], "上游未返回真实额度数据")
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
            self.assertEqual([item["id"] for item in refresh_failed_filter["items"]], [active["item"]["id"]])

    def test_runtime_view_attaches_redacted_oauth_metadata_by_email(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "oauth-user@example.com", "password": "password", "sso": "sso-secret", "status": "active"}
            )
            store.upsert(
                {"email": "no-oauth@example.com", "password": "password", "sso": "other-sso-secret", "status": "active"}
            )
            blocked = store.upsert(
                {"email": "blocked@example.com", "password": "password", "sso": "blocked-sso-secret", "status": "active"}
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
            oauth_store.upsert(
                {
                    "email": "blocked@example.com",
                    "refresh_token": "blocked-refresh-secret",
                    "status": "invalid",
                    "probe": {
                        "status": "invalid",
                        "http_status": 402,
                        "code": "personal-team-blocked:spending-limit",
                        "error": "spending limit reached",
                    },
                }
            )
            service = self._service(temp_dir, grok2api_enabled=False)

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                view = service.grok_accounts_view()
                unauthorized_view = service.grok_accounts_view(status="oauth_unauthorized")
                normal_view = service.grok_accounts_view(status="oauth_normal")
                no_quota_view = service.grok_accounts_view(status="oauth_no_quota")
                invalid_view = service.grok_accounts_view(status="oauth_invalid")

            item = next(entry for entry in view["items"] if entry["id"] == saved["item"]["id"])
            self.assertEqual(item["oauth"]["id"], linked["item"]["id"])
            self.assertEqual(item["oauth"]["status"], "active")
            self.assertEqual(item["oauth"]["models"], ["grok-4.5"])
            self.assertEqual(view["summary"]["oauth_total"], 3)
            self.assertEqual(view["summary"]["oauth_linked"], 2)
            self.assertEqual(
                view["summary"]["oauth_status"],
                {"unauthorized": 1, "normal": 1, "limited": 0, "no_quota": 1, "expired": 0, "invalid": 0},
            )
            self.assertEqual(
                [entry["email"] for entry in unauthorized_view["items"]],
                ["no***h@example.com"],
            )
            self.assertEqual([entry["id"] for entry in normal_view["items"]], [saved["item"]["id"]])
            self.assertEqual([entry["id"] for entry in no_quota_view["items"]], [blocked["item"]["id"]])
            self.assertEqual(invalid_view["items"], [])
            encoded = json.dumps(view, ensure_ascii=False)
            self.assertNotIn("oauth-access-secret", encoded)
            self.assertNotIn("oauth-refresh-secret", encoded)
            self.assertNotIn("oauth-subject-secret", encoded)

    def test_runtime_list_view_does_not_wait_for_runtime_snapshot(self) -> None:
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

            client.list.assert_not_called()
            self.assertTrue(view["runtime_available"])
            self.assertEqual(view["runtime_error"], "")
            self.assertEqual(len(view["items"]), 1)
            self.assertEqual(view["items"][0]["sync_state"], "unknown")
            self.assertNotIn("never-expose", json.dumps(view, ensure_ascii=False))

    def test_runtime_readiness_failure_degrades_to_local_redacted_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            store.upsert({"email": "user@example.com", "password": "password", "sso": "never-expose", "status": "active"})
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.readiness.return_value = (False, "runtime unavailable")

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                view = service.grok_accounts_view()

            self.assertFalse(view["runtime_available"])
            self.assertEqual(view["runtime_error"], "runtime unavailable")
            self.assertEqual(len(view["items"]), 1)
            self.assertEqual(view["items"][0]["sync_state"], "unknown")
            self.assertNotIn("never-expose", json.dumps(view, ensure_ascii=False))

    def test_runtime_snapshot_refresh_updates_cached_account_view(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "user@example.com", "password": "password", "sso": "snapshot-token", "status": "active"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.list_result = {
                "tokens": [
                    {
                        "token": "snapshot-token",
                        "pool": "super",
                        "status": "active",
                        "quota": {"auto": {"remaining": 9, "total": 10}},
                    }
                ]
            }

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                before = service.grok_accounts_view()
                refreshed = service.refresh_grok_runtime_snapshot()
                after = service.grok_accounts_view()

            self.assertEqual(before["items"][0]["sync_state"], "unknown")
            self.assertTrue(refreshed["ok"])
            self.assertTrue(refreshed["refreshed"])
            client.list.assert_called_once_with()
            updated = next(item for item in after["items"] if item["id"] == saved["item"]["id"])
            self.assertEqual(updated["sync_state"], "synced")
            self.assertEqual(updated["pool"], "super")
            self.assertEqual(updated["quota"]["auto"], {"remaining": 9, "total": 10})

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

    def test_scheduled_probe_checks_only_synced_non_disabled_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            active = store.upsert(
                {"email": "active@example.com", "password": "password", "sso": "active-secret", "status": "active"}
            )
            disabled = store.upsert(
                {"email": "disabled@example.com", "password": "password", "sso": "disabled-secret", "status": "active"}
            )
            local_only = store.upsert(
                {"email": "local@example.com", "password": "password", "sso": "local-secret", "status": "active"}
            )
            service = self._service(
                temp_dir,
                probe_scheduler={"interval_minutes": 15, "batch_size": 1},
            )
            client = FakeGrok2APIClient()
            client.list_result = {
                "tokens": [
                    {"token": "active-secret", "status": "active", "pool": "auto"},
                    {"token": "disabled-secret", "status": "disabled", "pool": "auto"},
                ]
            }
            client.verify_result = {
                "results": [
                    {
                        "token": "active-secret",
                        "status": "valid",
                        "quota": {"remaining": 7, "total": 10},
                    }
                ]
            }

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ):
                summary = service._run_grok_probe_once()
                status = service.grok_probe_scheduler_status()

            self.assertEqual(
                summary,
                {
                    "eligible": 1,
                    "skipped": 2,
                    "total": 1,
                    "valid": 1,
                    "invalid": 0,
                    "unknown": 0,
                    "batches": 1,
                    "recovery_attempted": 0,
                    "recovery_succeeded": 0,
                    "recovery_failed": 0,
                    "recovery_deferred": 0,
                    "oauth_eligible": 0,
                    "oauth_skipped": 0,
                    "oauth_total": 0,
                    "oauth_valid": 0,
                    "oauth_limited": 0,
                    "oauth_invalid": 0,
                    "oauth_unknown": 0,
                    "oauth_batches": 0,
                    "oauth_recovery_attempted": 0,
                    "oauth_recovery_queued": 0,
                    "oauth_recovery_failed": 0,
                    "oauth_recovery_deferred": 0,
                    "oauth_backfill_eligible": 0,
                    "oauth_backfill_attempted": 0,
                    "oauth_backfill_queued": 0,
                    "oauth_backfill_reused": 0,
                    "oauth_backfill_failed": 0,
                    "oauth_backfill_deferred": 0,
                    "oauth_backfill_missing_credentials": 0,
                },
            )
            client.verify.assert_called_once_with(["active-secret"])
            active_item = store.get_accounts_by_ids([active["item"]["id"]])[0]
            self.assertEqual(active_item["probe"]["status"], "valid")
            self.assertEqual(active_item["probe"]["quota"], {"remaining": 7, "total": 10})
            self.assertNotIn("probe", store.get_accounts_by_ids([disabled["item"]["id"]])[0])
            self.assertNotIn("probe", store.get_accounts_by_ids([local_only["item"]["id"]])[0])
            self.assertEqual(status["last_result"], summary)
            self.assertFalse(status["running"])
            self.assertTrue(status["next_run_at"])
            self.assertIn("Grok 账号探测完成", status["events"][-1]["message"])

    def test_scheduled_probe_checks_oauth_with_grok_45_and_persists_quota(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            active = oauth_store.upsert(
                {
                    "email": "oauth@example.com",
                    "subject": "oauth-subject",
                    "access_token": "oauth-access",
                    "refresh_token": "oauth-refresh",
                    "expires_in": 3600,
                    "models": ["grok-4.5"],
                }
            )["item"]
            disabled = oauth_store.upsert(
                {
                    "email": "disabled@example.com",
                    "subject": "disabled-subject",
                    "access_token": "disabled-access",
                    "refresh_token": "disabled-refresh",
                    "expires_in": 3600,
                    "models": ["grok-4.5"],
                }
            )["item"]
            oauth_store.set_disabled(disabled["id"], True)
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            service = self._service(temp_dir, probe_scheduler={"interval_minutes": 15, "batch_size": 50})
            client = FakeGrok2APIClient()
            upstream = httpx.Response(
                200,
                headers={
                    "x-ratelimit-limit-requests": "21",
                    "x-ratelimit-remaining-requests": "20",
                    "x-ratelimit-limit-tokens": "1000000",
                    "x-ratelimit-remaining-tokens": "999900",
                },
                json={"usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}},
            )

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ), patch.object(service, "_grok2api_client", return_value=client), patch(
                "services.xai_cli_oauth_service.XaiCliOAuthService._post_response",
                new=AsyncMock(return_value=upstream),
            ):
                summary = service._run_grok_probe_once()

            self.assertEqual(summary["oauth_eligible"], 1)
            self.assertEqual(summary["oauth_skipped"], 1)
            self.assertEqual(summary["oauth_valid"], 1)
            self.assertEqual(summary["oauth_batches"], 1)
            saved = oauth_store.get(active["id"], redacted=True)
            self.assertEqual(saved["probe"]["status"], "valid")
            self.assertEqual(saved["probe"]["model"], "grok-4.5")
            self.assertEqual(saved["quota"]["requests"]["remaining"], 20)
            self.assertEqual(saved["use_count"], 0)
            self.assertEqual(oauth_store.get(disabled["id"], redacted=True)["probe"], {})

    def test_scheduled_probe_queues_invalid_oauth_reauthorization_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            oauth = oauth_store.upsert(
                {
                    "email": "recover@example.com",
                    "subject": "recover-subject",
                    "access_token": "recover-access",
                    "refresh_token": "recover-refresh",
                    "expires_in": 3600,
                    "models": ["grok-4.5"],
                }
            )["item"]
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            source = grok_store.upsert(
                {"email": "recover@example.com", "password": "saved-password", "status": "active"}
            )["item"]
            service = self._service(temp_dir, probe_scheduler={"interval_minutes": 15, "batch_size": 50})
            client = FakeGrok2APIClient()
            protocol_sink = MagicMock(
                return_value={"reused": False, "queued": True, "job": {"id": "oauth-recovery-job-one"}}
            )
            service._grok_oauth_protocol_sink = protocol_sink
            blocked = httpx.Response(
                402,
                json={"error": {"code": "personal-team-blocked", "message": "Personal team is blocked"}},
            )

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ), patch.object(service, "_grok2api_client", return_value=client), patch(
                "services.xai_cli_oauth_service.XaiCliOAuthService._post_response",
                new=AsyncMock(return_value=blocked),
            ):
                first = service._run_grok_probe_once()
                second = service._run_grok_probe_once()

            self.assertEqual(first["oauth_invalid"], 1)
            self.assertEqual(first["oauth_recovery_attempted"], 1)
            self.assertEqual(first["oauth_recovery_queued"], 1)
            self.assertEqual(second["oauth_recovery_attempted"], 0)
            self.assertEqual(second["oauth_recovery_deferred"], 1)
            protocol_sink.assert_called_once_with(source["id"], retry=True)
            recovered = oauth_store.get(oauth["id"], redacted=True)
            self.assertEqual(recovered["status"], "invalid")
            self.assertEqual(recovered["recovery"]["status"], "pending")
            self.assertEqual(recovered["recovery"]["job_id"], "oauth-recovery-job-one")
            self.assertEqual(recovered["recovery"]["source_account_id"], source["id"])

    def test_permission_retry_only_probes_accounts_after_delay_and_counts_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            old = oauth_store.upsert(
                {
                    "email": "old-pending@example.com",
                    "subject": "old-pending-subject",
                    "access_token": "old-pending-access",
                    "refresh_token": "old-pending-refresh",
                    "expires_in": 3600,
                    "models": ["grok-4.5"],
                }
            )["item"]
            recent = oauth_store.upsert(
                {
                    "email": "recent-pending@example.com",
                    "subject": "recent-pending-subject",
                    "access_token": "recent-pending-access",
                    "refresh_token": "recent-pending-refresh",
                    "expires_in": 3600,
                    "models": ["grok-4.5"],
                }
            )["item"]
            now = datetime.now(timezone.utc)
            oauth_store.update_probe_result(
                old["id"],
                status="invalid",
                model="grok-4.5",
                http_status=403,
                code="permission-denied",
                error="permission pending",
                probed_at=(now - timedelta(minutes=16)).isoformat(),
            )
            oauth_store.update_probe_result(
                recent["id"],
                status="invalid",
                model="grok-4.5",
                http_status=403,
                code="permission-denied",
                error="permission pending",
                probed_at=(now - timedelta(minutes=14)).isoformat(),
            )
            service = self._service(temp_dir)
            probe_accounts = AsyncMock(
                return_value={
                    "results": [
                        {
                            "account_id": old["id"],
                            "status": "valid",
                            "code": "",
                            "delivery": {"sub2api": {"status": "success"}},
                        }
                    ],
                    "summary": {"delivery_success": 1},
                }
            )

            with patch.object(register_service_module, "xai_cli_oauth_store", oauth_store), patch(
                "services.xai_cli_oauth_service.xai_cli_oauth_service.probe_accounts",
                new=probe_accounts,
            ):
                summary = service._run_grok_permission_retry_once()

            self.assertEqual(
                summary,
                {"eligible": 1, "tested": 1, "valid": 1, "pending": 0, "failed": 0, "uploaded": 1},
            )
            probe_accounts.assert_awaited_once_with([old["id"]], concurrency=3)
            self.assertIn(
                "恢复 1，待生效 0，其他失败 0，已上传 1",
                service.get()["grok_oauth_logs"][-1]["text"],
            )
            self.assertEqual(service.get()["logs"], [])

    def test_scheduler_drains_due_permission_batches_without_idle_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir)
            stop_event = threading.Event()
            calls = 0

            def retry_batch(_stop_event: threading.Event) -> dict[str, int]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    stop_event.set()
                return {"tested": 3}

            with patch.object(
                service,
                "_grok_oauth_recovery_sweep_due",
                return_value=False,
            ), patch.object(
                service,
                "_grok_oauth_has_inflight_recovery",
                return_value=False,
            ), patch.object(
                service,
                "_grok_oauth_has_unlinked_accounts",
                return_value=False,
            ), patch.object(
                service,
                "_run_grok_permission_retry_once",
                side_effect=retry_batch,
            ), patch.object(
                service,
                "_grok_probe_due_locked",
                return_value=False,
            ), patch.object(service._grok_probe_wake_event, "wait") as wait:
                service._run_grok_probe_scheduler(stop_event)

            self.assertEqual(calls, 2)
            wait.assert_not_called()

    def test_oauth_protocol_events_complete_or_backoff_automatic_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            oauth = oauth_store.upsert(
                {
                    "email": "recover@example.com",
                    "subject": "recover-subject",
                    "access_token": "recover-access",
                    "refresh_token": "recover-refresh",
                    "expires_in": 3600,
                }
            )["item"]
            service = self._service(temp_dir)

            with patch.object(register_service_module, "xai_cli_oauth_store", oauth_store):
                oauth_store.update_recovery_state(
                    oauth["id"],
                    status="pending",
                    job_id="success-job",
                    last_attempt_at="2030-01-01T00:00:00+00:00",
                    attempts=1,
                )
                service.handle_grok_oauth_protocol_event(
                    {
                        "status": "authorized",
                        "job_id": "success-job",
                        "email": "recover@example.com",
                        "delivery": {
                            "sub2api": {
                                "status": "success",
                                "target_id": "nova-one",
                            }
                        },
                    }
                )
                successful = oauth_store.get(oauth["id"], redacted=True)
                success_log = service.get()["grok_oauth_logs"][-1]["text"]
                self.assertIn("自动恢复完成，已上传到 NovaApi", success_log)
                self.assertIn("re***r@example.com", success_log)
                self.assertNotIn("recover@example.com", success_log)
                self.assertEqual(service.get()["logs"], [])

                oauth_store.update_recovery_state(
                    oauth["id"],
                    status="pending",
                    job_id="failed-job",
                    last_attempt_at="2030-01-02T00:00:00+00:00",
                    attempts=2,
                )
                service.handle_grok_oauth_protocol_event(
                    {
                        "status": "failed",
                        "job_id": "failed-job",
                        "email": "recover@example.com",
                        "error": "authorization failed",
                    }
                )
                failed = oauth_store.get(oauth["id"], redacted=True)
                self.assertEqual(len(service.get()["grok_oauth_logs"]), 1)
                self.assertEqual(service.get()["logs"], [])

            self.assertEqual(successful["recovery"]["status"], "success")
            self.assertTrue(successful["recovery"]["last_success_at"])
            self.assertEqual(successful["recovery"]["attempts"], 0)
            self.assertEqual(failed["recovery"]["status"], "failed")
            self.assertTrue(failed["recovery"]["next_attempt_at"])
            self.assertEqual(failed["recovery"]["attempts"], 2)

    def test_permission_pending_event_is_not_reported_as_authorization_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(temp_dir)

            service.handle_grok_oauth_protocol_event(
                {
                    "status": "permission_pending",
                    "job_id": "permission-job",
                    "email": "pending@example.com",
                    "delivery": {},
                }
            )

            pending_log = service.get()["grok_oauth_logs"][-1]["text"]
            self.assertIn("权限待生效，已进入延迟复检", pending_log)
            self.assertIn("pe***g@example.com", pending_log)
            self.assertNotIn("pending@example.com", pending_log)
            self.assertEqual(service.get()["logs"], [])

    def test_startup_recovery_sweep_queues_unlinked_oauth_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            oauth_store.upsert(
                {
                    "email": "linked@example.com",
                    "subject": "linked-subject",
                    "access_token": "linked-access",
                    "refresh_token": "linked-refresh",
                    "expires_in": 3600,
                }
            )
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            grok_store.upsert(
                {
                    "email": "linked@example.com",
                    "password": "linked-password",
                    "sso": "linked-sso",
                    "status": "active",
                }
            )
            unlinked = grok_store.upsert(
                {
                    "email": "unlinked@example.com",
                    "password": "saved-password",
                    "sso": "unlinked-sso",
                    "status": "active",
                }
            )["item"]
            grok_store.upsert(
                {
                    "email": "missing-password@example.com",
                    "sso": "missing-password-sso",
                    "status": "active",
                }
            )
            grok_store.upsert(
                {
                    "email": "unknown@example.com",
                    "password": "saved-password",
                    "status": "submission_unknown",
                }
            )
            service = self._service(temp_dir)
            sink = MagicMock(return_value={"queued": True, "job": {"id": "backfill-job"}})
            service._grok_oauth_protocol_sink = sink

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                self.assertTrue(service._grok_oauth_has_unlinked_accounts())
                summary = service._run_grok_oauth_recovery_sweep()

            self.assertEqual(summary["backfill_eligible"], 1)
            self.assertEqual(summary["backfill_attempted"], 1)
            self.assertEqual(summary["backfill_queued"], 1)
            self.assertEqual(summary["backfill_missing_credentials"], 1)
            sink.assert_called_once_with(unlinked["id"])

    def test_startup_recovery_sweep_queues_previously_detected_invalid_oauth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            oauth = oauth_store.upsert(
                {
                    "email": "startup@example.com",
                    "subject": "startup-subject",
                    "access_token": "startup-access",
                    "refresh_token": "startup-refresh",
                    "expires_in": 3600,
                }
            )["item"]
            oauth_store.update_probe_result(
                oauth["id"],
                status="invalid",
                model="grok-4.5",
                http_status=402,
                code="personal-team-blocked",
            )
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            source = grok_store.upsert(
                {
                    "email": "startup@example.com",
                    "password": "saved-password",
                    "sso": "startup-sso",
                    "status": "active",
                }
            )["item"]
            service = self._service(temp_dir)
            service._config["grok"]["probe_scheduler"]["last_finished_at"] = "2020-01-01T00:00:00+00:00"
            sink = MagicMock(return_value={"queued": True, "job": {"id": "startup-recovery-job"}})
            service._grok_oauth_protocol_sink = sink

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                probe = service._grok_probe_config_locked()
                self.assertTrue(service._grok_oauth_recovery_sweep_due(probe))
                summary = service._run_grok_oauth_recovery_sweep()

            self.assertEqual(summary["eligible"], 1)
            self.assertEqual(summary["queued"], 1)
            sink.assert_called_once_with(source["id"], retry=True)
            status = service.grok_probe_scheduler_status()
            self.assertTrue(status["oauth_recovery_last_sweep_at"])
            self.assertFalse(service._grok_oauth_recovery_sweep_due(status))
            self.assertEqual(service.get()["logs"], [])

    def test_recovery_sweep_without_limit_queues_every_due_oauth_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            source_ids: list[str] = []
            for index in range(3):
                email = f"recover-{index}@example.com"
                oauth = oauth_store.upsert(
                    {
                        "email": email,
                        "subject": f"recover-subject-{index}",
                        "access_token": f"recover-access-{index}",
                        "refresh_token": f"recover-refresh-{index}",
                        "expires_in": 3600,
                    }
                )["item"]
                oauth_store.update_probe_result(
                    oauth["id"],
                    status="invalid",
                    model="grok-4.5",
                    code="invalid_credentials",
                )
                source = grok_store.upsert(
                    {
                        "email": email,
                        "password": "saved-password",
                        "sso": f"saved-sso-{index}",
                        "status": "active",
                    }
                )["item"]
                source_ids.append(source["id"])

            service = self._service(temp_dir, probe_scheduler={"batch_size": 1})
            sink = MagicMock(
                side_effect=lambda account_id, retry=False: {
                    "queued": True,
                    "job": {"id": f"job-{account_id}"},
                }
            )
            service._grok_oauth_protocol_sink = sink

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                summary = service._run_grok_oauth_recovery_sweep()

            self.assertEqual(summary["eligible"], 3)
            self.assertEqual(summary["attempted"], 3)
            self.assertEqual(summary["queued"], 3)
            self.assertEqual(summary["deferred"], 0)
            self.assertEqual(
                {call.args[0] for call in sink.call_args_list},
                set(source_ids),
            )
            self.assertTrue(all(call.kwargs == {"retry": True} for call in sink.call_args_list))

    def test_startup_scheduler_reclaims_orphaned_pending_oauth_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            oauth_store = XaiCliOAuthAccountStore(Path(temp_dir) / "oauth_accounts.json")
            oauth = oauth_store.upsert(
                {
                    "email": "orphan@example.com",
                    "subject": "orphan-subject",
                    "access_token": "orphan-access",
                    "refresh_token": "orphan-refresh",
                    "expires_in": 3600,
                }
            )["item"]
            oauth_store.update_probe_result(
                oauth["id"],
                status="invalid",
                model="grok-4.5",
                http_status=402,
                code="personal-team-blocked",
            )
            oauth_store.update_recovery_state(
                oauth["id"],
                status="pending",
                job_id="orphaned-job",
                source_account_id="old-source-id",
                last_attempt_at=register_service_module._now(),
                attempts=1,
            )
            grok_store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            source = grok_store.upsert(
                {
                    "email": "orphan@example.com",
                    "password": "saved-password",
                    "sso": "orphan-sso",
                    "status": "active",
                }
            )["item"]
            service = self._service(temp_dir)
            probe = service._config["grok"]["probe_scheduler"]
            probe["last_finished_at"] = "2020-01-01T00:00:00+00:00"
            probe["oauth_recovery_last_sweep_at"] = "2020-01-01T00:00:00+00:00"
            stop_event = threading.Event()

            def queue_replacement(_account_id: str, *, retry: bool = False) -> dict[str, object]:
                self.assertTrue(retry)
                stop_event.set()
                return {"queued": True, "job": {"id": "replacement-job"}}

            service._grok_oauth_protocol_sink = MagicMock(side_effect=queue_replacement)

            with patch.object(register_service_module, "grok_account_store", grok_store), patch.object(
                register_service_module, "xai_cli_oauth_store", oauth_store
            ):
                self.assertFalse(service._grok_oauth_recovery_sweep_due(probe))
                service._run_grok_probe_scheduler(stop_event)

            service._grok_oauth_protocol_sink.assert_called_once_with(source["id"], retry=True)
            recovered = oauth_store.get(oauth["id"], redacted=True)
            self.assertEqual(recovered["recovery"]["status"], "pending")
            self.assertEqual(recovered["recovery"]["job_id"], "replacement-job")
            self.assertEqual(recovered["recovery"]["attempts"], 2)
            self.assertEqual(service.get()["logs"], [])

    def test_scheduled_probe_recovers_invalid_account_with_saved_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "user@example.com", "password": "password", "sso": "old-secret", "status": "active"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.list_result = {
                "tokens": [{"token": "old-secret", "status": "active", "pool": "auto"}]
            }
            client.verify.side_effect = [
                {"results": [{"token": "old-secret", "status": "invalid", "error": "expired"}]},
                {
                    "results": [
                        {
                            "token": "new-secret",
                            "status": "valid",
                            "quota": {"remaining": 9, "total": 10},
                        }
                    ]
                },
            ]

            def delete_old(_tokens):
                client.list_result = {
                    "tokens": [{"token": "new-secret", "status": "active", "pool": "auto"}]
                }
                return {"deleted": 1}

            client.delete.side_effect = delete_old

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ), patch.object(service, "_authorize_grok_recovery_sso", return_value="new-secret") as authorize:
                summary = service._run_grok_probe_once()

            self.assertEqual(summary["invalid"], 1)
            self.assertEqual(summary["recovery_attempted"], 1)
            self.assertEqual(summary["recovery_succeeded"], 1)
            self.assertEqual(summary["recovery_failed"], 0)
            authorize.assert_called_once_with(email="user@example.com", password="password")
            client.add.assert_called_once_with(["new-secret"])
            client.delete.assert_called_once_with(["old-secret"])
            item = store.get_accounts_by_ids([saved["item"]["id"]])[0]
            self.assertEqual(item["sso"], "new-secret")
            self.assertEqual(item["probe"]["status"], "valid")
            self.assertEqual(item["probe"]["quota"], {"remaining": 9, "total": 10})
            self.assertEqual(item["recovery"]["status"], "success")
            self.assertTrue(item["recovery"]["last_success_at"])

    def test_failed_recovery_is_redacted_and_deferred_until_backoff_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GrokAccountStore(Path(temp_dir) / "grok_accounts.json")
            saved = store.upsert(
                {"email": "user@example.com", "password": "password", "sso": "old-secret", "status": "active"}
            )
            service = self._service(temp_dir)
            client = FakeGrok2APIClient()
            client.list_result = {
                "tokens": [{"token": "old-secret", "status": "active", "pool": "auto"}]
            }
            client.verify_result = {
                "results": [{"token": "old-secret", "status": "invalid", "error": "expired"}]
            }
            failure = RuntimeError("user@example.com password old-secret login failed")

            with patch.object(register_service_module, "grok_account_store", store), patch.object(
                service, "_grok2api_client", return_value=client
            ), patch.object(service, "_authorize_grok_recovery_sso", side_effect=failure) as authorize:
                first = service._run_grok_probe_once()
                second = service._run_grok_probe_once()

            self.assertEqual(first["recovery_attempted"], 1)
            self.assertEqual(first["recovery_failed"], 1)
            self.assertEqual(second["recovery_attempted"], 0)
            self.assertEqual(second["recovery_deferred"], 1)
            authorize.assert_called_once_with(email="user@example.com", password="password")
            item = store.get_accounts_by_ids([saved["item"]["id"]])[0]
            self.assertEqual(item["sso"], "old-secret")
            self.assertEqual(item["recovery"]["status"], "failed")
            self.assertEqual(item["recovery"]["attempts"], 1)
            self.assertTrue(item["recovery"]["next_attempt_at"])
            encoded = json.dumps(item["recovery"], ensure_ascii=False)
            self.assertNotIn("user@example.com", encoded)
            self.assertNotIn("password", encoded)
            self.assertNotIn("old-secret", encoded)

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
