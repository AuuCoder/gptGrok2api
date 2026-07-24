from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from services.xai_cli_oauth_service import XaiCliOAuthService
from services.xai_cli_oauth_store import XaiCliOAuthAccountStore


def _jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{encoded}.signature"


class XaiCliOAuthServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = XaiCliOAuthAccountStore(Path(self.temp_dir.name) / "accounts.json")
        self.service = XaiCliOAuthService(self.store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _account(self, *, expires_at: str = "2030-01-01T00:00:00+00:00") -> dict[str, object]:
        return self.store.upsert(
            {
                "email": "person@example.com",
                "subject": "subject-person",
                "access_token": "old-access",
                "refresh_token": "refresh-token",
                "expires_at": expires_at,
                "models": ["grok-4.5"],
            }
        )["item"]

    async def test_import_validates_models_and_gates_catalog(self) -> None:
        access = _jwt({"sub": "subject-import", "email": "import@example.com", "exp": int(time.time()) + 3600})
        with patch.object(self.service, "_fetch_models", new=AsyncMock(return_value=["grok-4.5"])), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(return_value={"status": "valid"}),
        ):
            result = await self.service.import_credentials(access_token=access, refresh_token="refresh-import")

        self.assertEqual(result["account"]["email"], "im***t@example.com")
        self.assertTrue(self.service.supports_model("grok-4.5"))
        self.assertEqual(self.service.model_items()[0]["id"], "grok-4.5")

    async def test_import_keeps_permission_denied_for_delayed_retry(self) -> None:
        access = _jwt({"sub": "subject-pending", "email": "pending@example.com", "exp": int(time.time()) + 3600})
        with patch.object(self.service, "_fetch_models", new=AsyncMock(return_value=["grok-4.5"])), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(
                return_value={
                    "status": "invalid",
                    "http_status": 403,
                    "code": "permission-denied",
                    "error": "permission pending",
                }
            ),
        ):
            result = await self.service.import_credentials(
                access_token=access,
                refresh_token="refresh-pending",
            )

        self.assertEqual(result["probe"]["code"], "permission-denied")
        self.assertEqual(len(self.store.list_accounts(redacted=False)), 1)

    async def test_protocol_job_imports_credentials_without_exposing_source_password(self) -> None:
        source = {
            "id": "grok-source-one",
            "email": "source@example.com",
            "password": "source-password",
            "sso": "source-sso",
            "status": "active",
        }
        credential = {
            "access_token": _jwt({"sub": "subject-protocol", "email": "source@example.com", "exp": int(time.time()) + 3600}),
            "refresh_token": "protocol-refresh",
            "id_token": "",
            "expires_in": 3600,
        }
        authorize = MagicMock(return_value=credential)
        protocol = SimpleNamespace(authorize=authorize)
        events: list[dict[str, object]] = []
        self.service.protocol_event_sink = events.append

        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch(
            "services.register_service.register_service.get",
            return_value={"grok": {}, "proxy": "direct"},
        ), patch(
            "services.xai_device_oauth_protocol.XaiDeviceOAuthProtocol",
            return_value=protocol,
        ), patch.object(
            self.service,
            "_fetch_models",
            new=AsyncMock(return_value=["grok-4.5"]),
        ), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(return_value={"status": "valid"}),
        ):
            started = await self.service.start_protocol_authorization()
            job_id = started["job"]["id"]
            for _ in range(50):
                await asyncio.sleep(0)
                job = self.service.get_protocol_authorization_job(job_id)
                if job and job["status"] not in {"pending", "running"}:
                    break

        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "authorized")
        self.assertEqual(job["models"], ["grok-4.5"])
        self.assertNotIn("source-password", repr(job))
        self.assertNotIn("source-sso", repr(job))
        self.assertNotIn("protocol-refresh", repr(job))
        authorize.assert_called_once_with(
            email="source@example.com",
            password="source-password",
            sso="source-sso",
        )
        self.assertEqual(self.store.list_accounts()[0]["source_type"], "registered_account_protocol")
        self.assertEqual(events[-1]["job_id"], job_id)
        self.assertEqual(events[-1]["oauth_account_id"], self.store.list_accounts()[0]["id"])

    async def test_protocol_spending_limit_waits_without_external_delivery(self) -> None:
        source = {
            "id": "grok-permission-pending",
            "email": "pending@example.com",
            "password": "source-password",
        }
        credential = {
            "access_token": _jwt({"sub": "pending-subject", "email": "pending@example.com", "exp": int(time.time()) + 3600}),
            "refresh_token": "pending-refresh",
            "id_token": "",
            "expires_in": 3600,
        }
        protocol = SimpleNamespace(authorize=lambda **_kwargs: credential)
        events: list[dict[str, object]] = []
        self.service.protocol_event_sink = events.append

        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch(
            "services.register_service.register_service.get",
            return_value={"grok": {"oauth_delivery": {"sub2api": {"enabled": True}}}, "proxy": "direct"},
        ), patch(
            "services.xai_device_oauth_protocol.XaiDeviceOAuthProtocol",
            return_value=protocol,
        ), patch.object(
            self.service,
            "_fetch_models",
            new=AsyncMock(return_value=["grok-4.5"]),
        ), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(
                return_value={
                    "status": "invalid",
                    "http_status": 402,
                    "code": "personal-team-blocked:spending-limit",
                    "error": "subscription or credits required",
                }
            ),
        ), patch(
            "services.xai_oauth_delivery_service.deliver_xai_oauth_account",
        ) as deliver:
            started = await self.service.start_protocol_authorization("grok-permission-pending")
            job_id = started["job"]["id"]
            for _ in range(50):
                await asyncio.sleep(0)
                job = self.service.get_protocol_authorization_job(job_id)
                if job and job["status"] not in {"pending", "running"}:
                    break

        self.assertEqual(job["status"], "authorized")
        self.assertEqual(job["stage"], "permission_pending")
        self.assertEqual(events[-1]["status"], "permission_pending")
        deliver.assert_not_called()

    async def test_protocol_can_use_reference_pkce_flow(self) -> None:
        source = {
            "id": "grok-pkce-source",
            "email": "pkce@example.com",
            "password": "source-password",
            "sso": "source-sso",
        }
        session_cookies = {"sso": "source-sso", "session": "live-cookie"}
        credential = {
            "access_token": _jwt({"sub": "pkce-subject", "email": "pkce@example.com", "exp": int(time.time()) + 3600}),
            "refresh_token": "pkce-refresh",
            "id_token": "",
            "expires_in": 3600,
        }
        authorize = MagicMock(return_value=credential)
        protocol = SimpleNamespace(authorize=authorize, progress=None)

        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch(
            "services.register_service.register_service.get",
            return_value={
                "grok": {
                    "xai_cli_oauth_flow": "pkce_reference",
                    "xai_cli_pkce_reference_dir": "/reference",
                },
                "proxy": "direct",
            },
        ), patch(
            "services.xai_reference_pkce_protocol.XaiReferencePkceProtocol",
            return_value=protocol,
        ), patch.object(
            self.service,
            "_fetch_models",
            new=AsyncMock(return_value=["grok-4.5"]),
        ), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(return_value={"status": "valid"}),
        ):
            started = await self.service.start_protocol_authorization(
                "grok-pkce-source",
                session_cookies=session_cookies,
            )
            job_id = started["job"]["id"]
            for _ in range(50):
                await asyncio.sleep(0)
                job = self.service.get_protocol_authorization_job(job_id)
                if job and job["status"] not in {"pending", "running"}:
                    break

        self.assertEqual(job["status"], "authorized")
        self.assertEqual(self.store.list_accounts()[0]["source_type"], "registered_account_pkce_reference")
        authorize.assert_called_once_with(
            email="pkce@example.com",
            password="source-password",
            sso="source-sso",
            session_cookies=session_cookies,
        )

    async def test_protocol_jobs_are_reused_per_source_account(self) -> None:
        first = {"id": "grok-one", "email": "one@example.com", "password": "password"}
        second = {"id": "grok-two", "email": "two@example.com", "password": "password"}
        sources = {"grok-one": first, "grok-two": second}

        with patch.object(
            self.service,
            "_select_protocol_source_account",
            side_effect=lambda account_id="": sources[account_id or "grok-one"],
        ), patch.object(self.service, "_run_protocol_authorization", new=AsyncMock()):
            one = await self.service.start_protocol_authorization("grok-one")
            one_reused = await self.service.start_protocol_authorization("grok-one")
            two = await self.service.start_protocol_authorization("grok-two")

        self.assertFalse(one["reused"])
        self.assertTrue(one_reused["reused"])
        self.assertEqual(one_reused["job"]["id"], one["job"]["id"])
        self.assertFalse(two["reused"])
        self.assertNotEqual(two["job"]["id"], one["job"]["id"])

    def test_protocol_source_accepts_saved_sso_without_password(self) -> None:
        source = {
            "id": "grok-sso-only",
            "email": "sso-only@example.com",
            "password": "",
            "sso": "saved-sso",
            "status": "active",
        }
        with patch(
            "services.register.grok_account_store.grok_account_store.get_accounts_by_ids",
            return_value=[source],
        ):
            selected = self.service._select_protocol_source_account("grok-sso-only")

        self.assertEqual(selected, source)

    async def test_protocol_authorization_stays_successful_when_one_delivery_target_fails(self) -> None:
        source = {
            "id": "grok-delivery-source",
            "email": "delivery@example.com",
            "password": "source-password",
            "sso": "delivery-sso",
        }
        credential = {
            "access_token": _jwt({"sub": "delivery-subject", "email": "delivery@example.com", "exp": int(time.time()) + 3600}),
            "refresh_token": "delivery-refresh",
            "id_token": "",
            "expires_in": 3600,
        }
        protocol = SimpleNamespace(authorize=lambda **_kwargs: credential)
        delivery = {
            "sub2api": {"status": "success", "target_id": "server-one", "at": "2030-01-01T00:00:00+00:00"},
            "cpa": {"status": "failed", "target_id": "pool-one", "at": "2030-01-01T00:00:00+00:00", "error": "HTTP 503"},
        }

        delivery_mock = MagicMock(return_value=delivery)
        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch(
            "services.register_service.register_service.get",
            return_value={"grok": {"oauth_delivery": {}}, "proxy": "direct"},
        ), patch(
            "services.xai_device_oauth_protocol.XaiDeviceOAuthProtocol",
            return_value=protocol,
        ), patch.object(
            self.service,
            "_fetch_models",
            new=AsyncMock(return_value=["grok-4.5"]),
        ), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(return_value={"status": "valid"}),
        ), patch(
            "services.xai_oauth_delivery_service.deliver_xai_oauth_account",
            delivery_mock,
        ):
            started = await self.service.start_protocol_authorization("grok-delivery-source")
            job_id = started["job"]["id"]
            for _ in range(50):
                await asyncio.sleep(0)
                job = self.service.get_protocol_authorization_job(job_id)
                if job and job["status"] not in {"pending", "running"}:
                    break

        self.assertEqual(job["status"], "authorized")
        self.assertIn("外部投递部分失败", job["message"])
        self.assertEqual(job["delivery"], delivery)
        account = self.store.list_accounts(redacted=False)[0]
        self.assertEqual(account["metadata"]["oauth_delivery"], delivery)
        self.assertNotIn("delivery-refresh", repr(job))
        delivered_account = delivery_mock.call_args.args[0]
        self.assertEqual(delivered_account["sso_token"], "delivery-sso")
        self.assertNotIn("sso_token", account)

    async def test_background_protocol_entry_runs_to_terminal_state(self) -> None:
        source = {"id": "grok-background", "email": "background@example.com", "password": "password"}
        completed = threading.Event()

        async def run(job_id: str, selected: dict[str, object], *, notify_failure: bool = True) -> None:
            self.assertEqual(selected, source)
            self.assertFalse(notify_failure)
            self.service._update_protocol_job(
                job_id,
                status="authorized",
                stage="completed",
                message="协议授权完成",
            )
            completed.set()

        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch.object(
            self.service,
            "_run_protocol_authorization",
            new=run,
        ):
            started = self.service.start_protocol_authorization_background("grok-background")
            finished = await asyncio.to_thread(completed.wait, 2)

        self.assertTrue(finished)
        job = self.service.get_protocol_authorization_job(started["job"]["id"])
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "authorized")
        self.assertTrue(started["queued"])
        for _ in range(20):
            if not self.service._protocol_threads:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.service._protocol_threads)

    async def test_background_protocol_retries_transient_consent_failure_once(self) -> None:
        source = {"id": "grok-retry", "email": "retry@example.com", "password": "password"}
        completed = threading.Event()
        attempts = 0

        async def run(job_id: str, _selected: dict[str, object], *, notify_failure: bool = True) -> None:
            nonlocal attempts
            self.assertFalse(notify_failure)
            attempts += 1
            if attempts == 1:
                self.service._update_protocol_job(
                    job_id,
                    status="failed",
                    stage="consent",
                    message="协议授权失败",
                    error="consent form missing",
                    retryable=True,
                )
                return
            self.service._update_protocol_job(
                job_id,
                status="authorized",
                stage="completed",
                message="协议授权完成",
                error="",
            )
            completed.set()

        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch.object(
            self.service,
            "_run_protocol_authorization",
            new=run,
        ), patch(
            "services.xai_cli_oauth_service.time.sleep",
            return_value=None,
        ):
            started = self.service.start_protocol_authorization_background("grok-retry")
            finished = await asyncio.to_thread(completed.wait, 2)

        self.assertTrue(finished)
        self.assertEqual(attempts, 2)
        job = self.service.get_protocol_authorization_job(started["job"]["id"])
        self.assertEqual(job["status"], "authorized")

    async def test_background_protocol_does_not_retry_permanent_failure(self) -> None:
        source = {"id": "grok-permanent", "email": "permanent@example.com", "password": "password"}
        completed = threading.Event()
        attempts = 0

        async def run(job_id: str, _selected: dict[str, object], *, notify_failure: bool = True) -> None:
            nonlocal attempts
            self.assertFalse(notify_failure)
            attempts += 1
            self.service._update_protocol_job(
                job_id,
                status="failed",
                stage="session",
                message="协议授权失败",
                error="invalid credentials",
                retryable=False,
            )

        def event_sink(payload: dict[str, object]) -> None:
            if payload.get("status") == "failed":
                completed.set()

        self.service.protocol_event_sink = event_sink
        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch.object(
            self.service,
            "_run_protocol_authorization",
            new=run,
        ):
            self.service.start_protocol_authorization_background("grok-permanent")
            finished = await asyncio.to_thread(completed.wait, 2)

        self.assertTrue(finished)
        self.assertEqual(attempts, 1)

    def test_oauth_keeps_configured_solver_limit_during_registration(self) -> None:
        active = self.service._oauth_grok_config(
            {
                "enabled": True,
                "target": "grok",
                "threads": 3,
                "stats": {"running": 3},
                "grok": {"provider": "local", "local_concurrency": 3},
            }
        )
        self.assertEqual(active["local_concurrency"], 3)
        self.assertEqual(active["captcha_timeout"], 180)

        idle = self.service._oauth_grok_config(
            {
                "enabled": False,
                "target": "grok",
                "threads": 3,
                "stats": {"running": 0},
                "grok": {"provider": "local", "local_concurrency": 3},
            }
        )
        self.assertEqual(idle["local_concurrency"], 3)
        self.assertEqual(idle["captcha_timeout"], 180)

    async def test_background_protocol_runs_while_registration_is_active(self) -> None:
        source = {"id": "grok-immediate", "email": "immediate@example.com", "password": "password"}
        completed = threading.Event()

        async def run(job_id: str, _selected: dict[str, object], *, notify_failure: bool = True) -> None:
            self.service._update_protocol_job(job_id, status="authorized", stage="completed")
            completed.set()

        with patch.object(self.service, "_select_protocol_source_account", return_value=source), patch.object(
            self.service,
            "_run_protocol_authorization",
            new=run,
        ):
            started = self.service.start_protocol_authorization_background("grok-immediate")
            finished = await asyncio.to_thread(completed.wait, 2)

        self.assertTrue(finished)
        self.assertEqual(self.service.get_protocol_authorization_job(started["job"]["id"])["status"], "authorized")

    def test_protocol_queue_orders_registration_then_backfill_then_retry(self) -> None:
        recovery = {"id": "grok-recovery", "email": "recovery@example.com", "password": "password"}
        backfill = {"id": "grok-backfill", "email": "backfill@example.com", "password": "password"}
        registered = {"id": "grok-registered", "email": "registered@example.com", "password": "password"}

        with patch.object(
            self.service,
            "_select_protocol_source_account",
            side_effect=[recovery, backfill, registered],
        ), patch.object(self.service, "_ensure_protocol_workers"):
            recovery_job = self.service.start_protocol_authorization_background("grok-recovery", retry=True)
            backfill_job = self.service.start_protocol_authorization_background("grok-backfill")
            registered_job = self.service.start_protocol_authorization_background(
                "grok-registered",
                prioritize=True,
            )

        first = self.service._protocol_queue.get_nowait()
        second = self.service._protocol_queue.get_nowait()
        third = self.service._protocol_queue.get_nowait()
        self.assertEqual(first[2], registered_job["job"]["id"])
        self.assertEqual(second[2], backfill_job["job"]["id"])
        self.assertEqual(third[2], recovery_job["job"]["id"])
        self.assertEqual(registered_job["priority"], "registration")
        self.assertEqual(backfill_job["priority"], "backfill")
        self.assertEqual(recovery_job["priority"], "retry")

    def test_protocol_queue_status_reports_priority_counts(self) -> None:
        self.service._protocol_queue.put((0, 1, "registration", {}))
        self.service._protocol_queue.put((10, 2, "backfill", {}))
        self.service._protocol_queue.put((20, 3, "retry", {}))

        status = self.service.protocol_queue_status()

        self.assertEqual(status["queued"], 3)
        self.assertEqual(status["registration"], 1)
        self.assertEqual(status["backfill"], 1)
        self.assertEqual(status["retry"], 1)

    def test_protocol_worker_limit_matches_default_local_solver_capacity(self) -> None:
        self.assertEqual(self.service._protocol_worker_limit(), 2)

    def test_oauth_reuses_registration_upstream_proxy(self) -> None:
        profile = SimpleNamespace(proxy_url="socks5h://proxy.example:1080")
        with patch("services.proxy_service.proxy_settings.get_profile", return_value=profile) as get_profile:
            resolved = self.service._resolve_registration_proxy("")

        self.assertEqual(resolved, "socks5h://proxy.example:1080")
        get_profile.assert_called_once_with(proxy="", upstream=True)

    async def test_refresh_rotates_token_without_returning_credentials(self) -> None:
        account = self._account(expires_at="2000-01-01T00:00:00+00:00")
        access = _jwt({"sub": "subject-person", "email": "person@example.com", "exp": int(time.time()) + 3600})
        response = httpx.Response(200, json={"access_token": access, "refresh_token": "rotated-refresh", "expires_in": 3600})
        with patch.object(self.service, "_form_post", new=AsyncMock(return_value=response)):
            result = await self.service.refresh_account(str(account["id"]))

        self.assertNotIn("rotated-refresh", repr(result))
        raw = self.store.get(str(account["id"]))
        self.assertEqual(raw["refresh_token"], "rotated-refresh")
        self.assertEqual(raw["access_token"], access)

    async def test_nonstream_request_records_success_and_never_uses_cookie_headers(self) -> None:
        account = self._account()
        selected_accounts: list[dict[str, str]] = []
        upstream = httpx.Response(
            200,
            json={"id": "resp_1", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]},
        )
        with patch.object(self.service, "_post_response", new=AsyncMock(return_value=upstream)):
            response = await self.service.create_response(
                {"model": "grok-4.5", "input": "hello", "stream": False},
                on_account_selected=selected_accounts.append,
            )

        self.assertEqual(response["id"], "resp_1")
        self.assertEqual(
            selected_accounts,
            [{"account_id": str(account["id"]), "account_email": "pe***n@example.com"}],
        )
        self.assertNotIn("old-access", repr(selected_accounts))
        self.assertNotIn("refresh-token", repr(selected_accounts))
        headers = self.service._cli_headers("access")
        self.assertEqual(headers["Authorization"], "Bearer access")
        self.assertNotIn("Cookie", headers)
        item = self.store.list_accounts()[0]
        self.assertEqual(item["use_count"], 1)

    async def test_account_probe_uses_only_the_requested_oauth_account(self) -> None:
        first = self._account()
        second = self.store.upsert(
            {
                "email": "second@example.com",
                "subject": "subject-second",
                "access_token": "second-access",
                "refresh_token": "second-refresh",
                "expires_at": "2030-01-01T00:00:00+00:00",
                "models": ["grok-4.5"],
            }
        )["item"]
        upstream = httpx.Response(
            200,
            json={"id": "resp_test", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}]},
        )

        with patch.object(self.service, "_post_response", new=AsyncMock(return_value=upstream)) as post_response:
            result = await self.service.test_account(str(second["id"]), model="grok-4.5", prompt="只回复 OK")

        self.assertEqual(result["account_id"], second["id"])
        self.assertEqual(result["content"], "OK")
        self.assertEqual(post_response.await_args.args[0]["id"], second["id"])
        by_id = {item["id"]: item for item in self.store.list_accounts()}
        self.assertEqual(by_id[first["id"]]["use_count"], 0)
        self.assertEqual(by_id[second["id"]]["use_count"], 1)

    async def test_background_probe_persists_grok_45_quota_without_counting_user_traffic(self) -> None:
        account = self._account()
        upstream = httpx.Response(
            200,
            headers={
                "x-ratelimit-limit-requests": "21",
                "x-ratelimit-remaining-requests": "20",
                "x-ratelimit-limit-tokens": "1000000",
                "x-ratelimit-remaining-tokens": "999786",
            },
            json={
                "id": "resp_probe",
                "usage": {"input_tokens": 196, "output_tokens": 18, "total_tokens": 214},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            },
        )

        with patch.object(self.service, "_post_response", new=AsyncMock(return_value=upstream)):
            result = await self.service.probe_account(str(account["id"]))

        self.assertEqual(result["status"], "valid")
        saved = self.store.get(str(account["id"]), redacted=True)
        self.assertEqual(saved["probe"]["model"], "grok-4.5")
        self.assertEqual(saved["probe"]["usage"]["total_tokens"], 214)
        self.assertEqual(saved["quota"]["requests"], {"limit": 21, "remaining": 20})
        self.assertEqual(saved["quota"]["tokens"], {"limit": 1000000, "remaining": 999786})
        self.assertEqual(saved["use_count"], 0)
        self.assertEqual(saved["fail_count"], 0)

    async def test_background_probe_marks_personal_team_blocked_oauth_invalid(self) -> None:
        account = self._account()
        upstream = httpx.Response(
            402,
            json={
                "error": {
                    "code": "personal-team-blocked:spending-limit",
                    "message": "Personal team has no credits",
                }
            },
        )

        with patch.object(self.service, "_post_response", new=AsyncMock(return_value=upstream)):
            result = await self.service.probe_account(str(account["id"]))

        self.assertEqual(result["status"], "invalid")
        saved = self.store.get(str(account["id"]), redacted=True)
        self.assertEqual(saved["status"], "invalid")
        self.assertEqual(saved["probe"]["http_status"], 402)
        self.assertEqual(saved["probe"]["code"], "personal-team-blocked:spending-limit")

    async def test_sync_models_delivers_account_after_delayed_probe_becomes_valid(self) -> None:
        account = self._account()
        delivery = {
            "sub2api": {"status": "success", "target_id": "server-one"},
            "cpa": {"status": "skipped", "target_id": ""},
        }
        with patch.object(
            self.service,
            "_fetch_models",
            new=AsyncMock(return_value=["grok-4.5"]),
        ), patch.object(
            self.service,
            "probe_account",
            new=AsyncMock(return_value={"status": "valid"}),
        ), patch(
            "services.register_service.register_service.get",
            return_value={
                "grok": {
                    "oauth_delivery": {
                        "sub2api": {"enabled": True, "server_id": "server-one"},
                        "cpa": {"enabled": False},
                    }
                }
            },
        ), patch(
            "services.xai_oauth_delivery_service.deliver_xai_oauth_account",
            return_value=delivery,
        ) as deliver:
            result = await self.service.sync_models(str(account["id"]))

        self.assertEqual(result["delivery"]["sub2api"]["status"], "success")
        saved = self.store.get(str(account["id"]), redacted=True)
        self.assertEqual(saved["metadata"]["oauth_delivery"]["sub2api"]["status"], "success")
        deliver.assert_called_once()

    async def test_delayed_delivery_skips_already_successful_target(self) -> None:
        account = self._account()
        self.store.update_metadata(
            str(account["id"]),
            {"oauth_delivery": {"sub2api": {"status": "success", "target_id": "server-one"}}},
        )
        with patch(
            "services.register_service.register_service.get",
            return_value={
                "grok": {
                    "oauth_delivery": {
                        "sub2api": {"enabled": True, "server_id": "server-one"},
                        "cpa": {"enabled": False},
                    }
                }
            },
        ), patch(
            "services.xai_oauth_delivery_service.deliver_xai_oauth_account",
        ) as deliver:
            result = await self.service._deliver_oauth_if_needed(str(account["id"]))

        self.assertEqual(result["sub2api"]["status"], "success")
        deliver.assert_not_called()

    async def test_stream_request_reports_selected_account_when_iteration_starts(self) -> None:
        account = self._account()
        selected_accounts: list[dict[str, str]] = []

        class FakeResponse:
            status_code = 200

            async def aiter_text(self):
                yield 'event: response.completed\ndata: {"response": {}}\n\n'

        class FakeStream:
            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *_args):
                return False

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return FakeStream()

        with patch.object(self.service, "_client", return_value=FakeClient()):
            stream = await self.service.create_response(
                {"model": "grok-4.5", "input": "hello", "stream": True},
                on_account_selected=selected_accounts.append,
            )
            self.assertEqual(selected_accounts, [])
            chunks = [chunk async for chunk in stream]

        self.assertTrue(chunks)
        self.assertEqual(
            selected_accounts,
            [{"account_id": str(account["id"]), "account_email": "pe***n@example.com"}],
        )

    async def test_standard_responses_sse_converts_to_chat_completion_chunks(self) -> None:
        async def response_stream():
            yield 'event: response.output_text.delta\ndata: {"delta":"Hi"}\n\n'
            yield 'event: response.completed\ndata: {"response":{"usage":{"input_tokens":2,"output_tokens":1}}}\n\n'

        chunks = [chunk async for chunk in self.service._chat_stream(model="grok-4.5", response_stream=response_stream())]
        self.assertTrue(any('"content":"Hi"' in chunk for chunk in chunks))
        self.assertEqual(chunks[-1], "data: [DONE]\n\n")


if __name__ == "__main__":
    unittest.main()
