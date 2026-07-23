from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.register as register_api


class RegisterGrokAccountsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(register_api.create_router())
        self.client = TestClient(app)

    def test_list_grok_accounts_supports_filters_and_pagination(self) -> None:
        items = [
            {
                "id": f"grok-{index}",
                "platform": "grok",
                "email": f"us***{index}@example.com",
                "has_password": True,
                "has_sso": True,
                "status": "active",
            }
            for index in range(3)
        ]
        view = {
            "items": items,
            "all_total": 8,
            "summary": {"total": 8, "synced": 3},
            "runtime_available": True,
            "runtime_error": "",
        }
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "grok_accounts_view",
            return_value=view,
        ) as list_accounts, patch.object(
            register_api.register_service,
            "grok_probe_scheduler_status",
            return_value={"enabled": False, "running": False, "next_run_at": ""},
        ):
            response = self.client.get(
                "/api/register/grok/accounts",
                params={
                    "page": 2,
                    "page_size": 2,
                    "keyword": "example",
                    "status": "active",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "count": 3,
                "total": 3,
                "all_total": 8,
                "page": 2,
                "page_size": 2,
                "items": [items[2]],
                "summary": {"total": 8, "synced": 3},
                "runtime_available": True,
                "runtime_error": "",
                "probe_scheduler": {"enabled": False, "running": False, "next_run_at": ""},
            },
        )
        list_accounts.assert_called_once_with(keyword="example", status="active")

    def test_grok_probe_polling_toggle_uses_persistent_service_setting(self) -> None:
        status = {"enabled": True, "running": False, "next_run_at": "2030-01-01T00:00:00+00:00"}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "set_grok_probe_scheduler_enabled",
            return_value=status,
        ) as toggle:
            response = self.client.post("/api/register/grok/probe-polling", json={"enabled": True})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"probe_scheduler": status})
        toggle.assert_called_once_with(True)

    def test_stop_checkout_retries_uses_dedicated_service_action(self) -> None:
        response_payload = {"enabled": False, "checkout_tasks": [{"status": "cancelled"}]}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "stop_checkout_retries",
            return_value=response_payload,
        ) as stop_retries:
            response = self.client.post("/api/register/checkout-retries/stop")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"register": response_payload})
        stop_retries.assert_called_once_with()

    def test_clear_checkout_history_uses_dedicated_service_action(self) -> None:
        response_payload = {"removed": 2, "register": {"checkout_tasks": []}}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "clear_checkout_history",
            return_value=response_payload,
        ) as clear_history:
            response = self.client.post("/api/register/checkout-history/clear")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), response_payload)
        clear_history.assert_called_once_with()

    def test_retry_selected_outlook_mailboxes_passes_exact_selection(self) -> None:
        response_payload = {"enabled": True, "stats": {"retry_selected": 2}}
        mailbox_ids = ["mailbox-1", "mailbox-4"]
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "retry_outlook_failed",
            return_value=response_payload,
        ) as retry_selected:
            response = self.client.post(
                "/api/register/outlook-pool/retry-selected",
                json={"provider_id": "outlook-main", "mailbox_ids": mailbox_ids},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"register": response_payload})
        retry_selected.assert_called_once_with("outlook-main", mailbox_ids)

    def test_retry_selected_outlook_mailboxes_returns_bad_request_for_stale_selection(self) -> None:
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "retry_outlook_failed",
            side_effect=ValueError("所选邮箱已不在本次失败列表中"),
        ):
            response = self.client.post(
                "/api/register/outlook-pool/retry-selected",
                json={
                    "provider_id": "outlook-main",
                    "mailbox_ids": ["stale-mailbox"],
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("本次失败列表", response.json()["detail"])

    def test_list_grok_accounts_returns_200_when_runtime_is_unavailable(self) -> None:
        view = {
            "items": [{"id": "grok-one", "platform": "grok", "email": "us***r@example.com", "sync_state": "unknown"}],
            "all_total": 1,
            "summary": {"total": 1, "runtime_total": 0},
            "runtime_available": False,
            "runtime_error": "runtime unavailable",
        }
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "grok_accounts_view",
            return_value=view,
        ):
            response = self.client.get("/api/register/grok/accounts", params={"page": 1, "page_size": 20})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["runtime_available"])
        self.assertEqual(response.json()["runtime_error"], "runtime unavailable")
        self.assertEqual(response.json()["items"], view["items"])

    def test_refresh_grok_runtime_snapshot_uses_separate_endpoint(self) -> None:
        payload = {"ok": True, "refreshed": True, "refreshing": False, "error": ""}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "refresh_grok_runtime_snapshot",
            return_value=payload,
        ) as refresh_snapshot:
            response = self.client.post("/api/register/grok/accounts/runtime/snapshot")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        refresh_snapshot.assert_called_once_with()

    def test_grok_account_login_credentials_are_no_store_and_exclude_sso(self) -> None:
        credentials = {"id": "grok-one", "email": "user@example.com", "password": "generated-password"}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "grok_account_login_credentials",
            return_value=credentials,
        ) as get_credentials:
            response = self.client.get("/api/register/grok/accounts/grok-one/credentials")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), credentials)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("sso", response.json())
        get_credentials.assert_called_once_with("grok-one")

    def test_grok_account_login_credentials_return_404_for_missing_account(self) -> None:
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "grok_account_login_credentials",
            return_value=None,
        ):
            response = self.client.get("/api/register/grok/accounts/missing/credentials")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], {"error": "Grok 账号不存在或已删除"})

    def test_export_grok_accounts_supports_sub2api_format(self) -> None:
        payload = {"exported_at": "2030-01-01T00:00:00+00:00", "proxies": [], "accounts": []}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "export_grok_accounts_sub2api",
            return_value=payload,
        ):
            response = self.client.get("/api/register/grok/accounts/export", params={"format": "sub2api"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertIn("grok-accounts-sub2api-", response.headers["content-disposition"])

    def test_export_grok_accounts_supports_cpa_zip_format(self) -> None:
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "export_grok_accounts_cpa",
            return_value=b"zip-content",
        ):
            response = self.client.get("/api/register/grok/accounts/export", params={"format": "cpa"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"zip-content")
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("grok-accounts-cpa-", response.headers["content-disposition"])

    def test_export_selected_grok_accounts_supports_scope_and_format(self) -> None:
        payload = {"exported_at": "2030-01-01T00:00:00+00:00", "proxies": [], "accounts": []}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "export_grok_accounts_sub2api",
            return_value=payload,
        ) as export_accounts:
            response = self.client.post(
                "/api/register/grok/accounts/export",
                json={"ids": [" grok-one ", "grok-one", "grok-two"], "format": "sub2api"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        export_accounts.assert_called_once_with(["grok-one", "grok-two"])

    def test_delete_grok_accounts_deduplicates_stable_ids(self) -> None:
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "delete_grok_accounts",
            return_value={"removed": 2, "count": 1, "upstream_deleted": 2},
        ) as delete_accounts:
            response = self.client.request(
                "DELETE",
                "/api/register/grok/accounts",
                json={"ids": [" grok-one ", "grok-one", "", "grok-two"], "delete_upstream": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"removed": 2, "count": 1, "upstream_deleted": 2})
        delete_accounts.assert_called_once_with(["grok-one", "grok-two"], delete_upstream=True)

    def test_delete_grok_accounts_rejects_empty_ids(self) -> None:
        with patch.object(register_api, "require_admin"):
            response = self.client.request(
                "DELETE",
                "/api/register/grok/accounts",
                json={"ids": ["", "  "]},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], {"error": "ids is required"})

    def test_grok_runtime_action_endpoints_use_stable_ids(self) -> None:
        cases = [
            (
                "/api/register/grok/accounts/oauth/authorize",
                {"ids": [" grok-one ", "grok-one", "grok-two"]},
                "authorize_grok_accounts_oauth",
                {
                    "summary": {"total": 2, "queued": 1, "reused": 1, "skipped": 0, "failed": 0},
                    "results": [],
                },
                (["grok-one", "grok-two"],),
            ),
            (
                "/api/register/grok/accounts/sync",
                {"ids": [" grok-one ", "grok-one", "grok-two"]},
                "sync_grok_accounts",
                {"summary": {"total": 2, "ok": 2, "fail": 0}, "results": []},
                (["grok-one", "grok-two"],),
            ),
            (
                "/api/register/grok/accounts/runtime/refresh",
                {"ids": ["grok-one"]},
                "refresh_grok_accounts_runtime",
                {"summary": {"total": 1, "ok": 1, "fail": 0}},
                (["grok-one"],),
            ),
            (
                "/api/register/grok/accounts/runtime/verify",
                {"ids": [" grok-one ", "grok-one", "grok-two"]},
                "verify_grok_accounts_runtime",
                {
                    "summary": {"total": 2, "valid": 1, "invalid": 0, "unknown": 1},
                    "results": [
                        {"id": "grok-one", "status": "valid", "quota": {"remaining": 3, "total": 5}},
                        {"id": "grok-two", "status": "unknown", "error": "未确认登录态"},
                    ],
                },
                (["grok-one", "grok-two"],),
            ),
            (
                "/api/register/grok/accounts/runtime/disabled",
                {"ids": ["grok-one"], "disabled": True},
                "set_grok_accounts_disabled",
                {"disabled": True, "summary": {"total": 1, "ok": 1, "fail": 0}},
                (["grok-one"], True),
            ),
        ]
        for path, body, method_name, payload, expected_args in cases:
            with self.subTest(path=path), patch.object(register_api, "require_admin"), patch.object(
                register_api.register_service,
                method_name,
                return_value=payload,
            ) as action:
                response = self.client.post(path, json=body)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), payload)
            action.assert_called_once_with(*expected_args)

    def test_grok_account_chat_test_uses_one_stable_id_and_returns_safe_error(self) -> None:
        payload = {
            "id": "grok-one",
            "model": "grok-4.3-console",
            "content": "pong",
            "elapsed_ms": 12,
        }
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "chat_test_grok_account",
            return_value=payload,
        ) as action:
            response = self.client.post(
                "/api/register/grok/accounts/grok-one/runtime/chat-test",
                json={"prompt": "ping"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        action.assert_called_once_with("grok-one", prompt="ping", model=None)

        error = register_api.GrokAccountChatTestError("Console 权限被拒绝", status_code=403)
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "chat_test_grok_account",
            side_effect=error,
        ):
            denied = self.client.post(
                "/api/register/grok/accounts/grok-one/runtime/chat-test",
                json={"prompt": "ping"},
            )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"], {"error": "Console 权限被拒绝"})

    def test_delete_grok_accounts_preserves_local_record_when_upstream_fails(self) -> None:
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "delete_grok_accounts",
            side_effect=RuntimeError("upstream unavailable"),
        ):
            response = self.client.request(
                "DELETE",
                "/api/register/grok/accounts",
                json={"ids": ["grok-one"], "delete_upstream": True},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], {"error": "upstream unavailable"})


if __name__ == "__main__":
    unittest.main()
