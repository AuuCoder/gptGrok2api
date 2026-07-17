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
        ) as list_accounts:
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
            },
        )
        list_accounts.assert_called_once_with(keyword="example", status="active")

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

    def test_grok_account_batch_chat_test_uses_admin_route_and_prompt(self) -> None:
        job = {
            "id": "batch-job-one",
            "status": "running",
            "total": 2,
            "current": 1,
            "current_id": "grok-one",
            "cancel_requested": False,
            "summary": {
                "total": 2,
                "success": 0,
                "blocked": 0,
                "invalid": 0,
                "limited": 0,
                "permission": 0,
                "failed": 0,
                "skipped": 1,
                "pending": 1,
            },
            "results": [
                {"id": "grok-one", "status": "pending", "error": "", "elapsed_ms": 0},
                {"id": "grok-two", "status": "skipped", "error": "账号未保存 SSO 登录态", "elapsed_ms": 0},
            ],
        }
        with patch.object(register_api, "require_admin") as require_admin, patch.object(
            register_api.register_service,
            "start_grok_accounts_chat_test_job",
            return_value={"reused": False, "job": job},
        ) as start:
            response = self.client.post(
                "/api/register/grok/accounts/runtime/chat-test/batch",
                json={"prompt": "ping", "model": "grok-4.3-console"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"job": job})
        require_admin.assert_called_once()
        start.assert_called_once_with(prompt="ping", model="grok-4.3-console")

        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "get_grok_accounts_chat_test_job",
            return_value=job,
        ) as get_job:
            status = self.client.get("/api/register/grok/accounts/runtime/chat-test/batch/batch-job-one")

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json(), {"job": job})
        get_job.assert_called_once_with("batch-job-one")

        cancelled_job = {**job, "cancel_requested": True}
        with patch.object(register_api, "require_admin"), patch.object(
            register_api.register_service,
            "cancel_grok_accounts_chat_test_job",
            return_value=cancelled_job,
        ) as cancel_job:
            cancelled = self.client.post(
                "/api/register/grok/accounts/runtime/chat-test/batch/batch-job-one/cancel"
            )

        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json(), {"job": cancelled_job})
        cancel_job.assert_called_once_with("batch-job-one")

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
