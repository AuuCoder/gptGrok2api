from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.control.account.enums import AccountStatus, QuotaSource
from app.control.account.models import AccountRecord, QuotaWindow, RuntimeSnapshot
from app.control.account.quota_defaults import default_quota_set
from app.control.account.refresh import AccountRefreshService
from app.dataplane.reverse.protocol.xai_usage import FastQuotaProbeResult
from app.platform.errors import UpstreamError


class _Repository:
    def __init__(self, record: AccountRecord) -> None:
        self.record = record
        self.patches = []

    async def get_accounts(self, tokens: list[str]):
        return [self.record] if self.record.token in tokens else []

    async def patch_accounts(self, patches):
        self.patches.extend(patches)


class _RepairRepository:
    def __init__(self, records: list[AccountRecord]) -> None:
        self.records = records
        self.patch_calls: list[list] = []

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(revision=1, items=self.records)

    async def patch_accounts(self, patches):
        self.patch_calls.append(list(patches))


class AccountRefreshFastVerifyTest(unittest.IsolatedAsyncioTestCase):
    def _service(self) -> tuple[AccountRefreshService, _Repository]:
        repo = _Repository(
            AccountRecord(
                token="verify-token",
                pool="basic",
                quota=default_quota_set("basic").to_dict(),
            )
        )
        return AccountRefreshService(repo), repo

    async def test_valid_fast_probe_updates_only_fast_quota(self) -> None:
        service, repo = self._service()
        quota = QuotaWindow(
            remaining=3,
            total=10,
            window_seconds=3600,
            reset_at=123,
            synced_at=100,
            source=QuotaSource.REAL,
        )
        with patch(
            "app.dataplane.reverse.protocol.xai_usage.probe_fast_quota",
            AsyncMock(return_value=FastQuotaProbeResult(status="valid", quota=quota)),
        ) as probe:
            result = await service.verify_fast_token("verify-token")

        self.assertEqual(result, {"status": "valid", "quota": {"remaining": 3, "total": 10}})
        probe.assert_awaited_once_with("verify-token")
        self.assertEqual(len(repo.patches), 1)
        self.assertEqual(repo.patches[0].quota_fast["remaining"], 3)
        # The runtime keeps its existing basic-pool quota policy while the
        # verification response above reports the raw upstream probe values.
        self.assertEqual(repo.patches[0].quota_fast["total"], 30)
        self.assertIsNone(repo.patches[0].status)

    async def test_confirmed_invalid_probe_expires_runtime_account(self) -> None:
        service, repo = self._service()
        error = UpstreamError(
            "Upstream returned 401",
            status=401,
            body="invalid-credentials",
        )
        with patch(
            "app.dataplane.reverse.protocol.xai_usage.probe_fast_quota",
            AsyncMock(
                return_value=FastQuotaProbeResult(
                    status="invalid",
                    error="登录态已失效或账号不可用",
                    exception=error,
                )
            ),
        ):
            result = await service.verify_fast_token("verify-token")

        self.assertEqual(result, {"status": "invalid", "error": "登录态已失效或账号不可用"})
        self.assertEqual(len(repo.patches), 1)
        self.assertEqual(repo.patches[0].status, AccountStatus.EXPIRED)
        self.assertEqual(repo.patches[0].state_reason, "invalid_credentials")

    async def test_challenge_or_network_probe_does_not_patch_runtime_account(self) -> None:
        service, repo = self._service()
        with patch(
            "app.dataplane.reverse.protocol.xai_usage.probe_fast_quota",
            AsyncMock(
                return_value=FastQuotaProbeResult(
                    status="unknown",
                    error="上游访问被挑战或拒绝，未确认登录态",
                )
            ),
        ):
            result = await service.verify_fast_token("verify-token")

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(repo.patches, [])

    async def test_full_refresh_does_not_query_local_console_quota(self) -> None:
        service, _ = self._service()
        with patch(
            "app.dataplane.reverse.protocol.xai_usage.fetch_all_quotas",
            AsyncMock(return_value={}),
        ) as fetch:
            await service._fetch_all_quotas("verify-token", "basic")

        fetch.assert_awaited_once_with("verify-token", (1,))

    async def test_console_429_never_marks_account_expired(self) -> None:
        service, repo = self._service()
        await service.record_failure_async(
            "verify-token",
            5,
            UpstreamError("Console returned 429", status=429),
        )

        self.assertEqual(len(repo.patches), 1)
        patch = repo.patches[0]
        self.assertIsNone(patch.status)
        self.assertIsNone(patch.state_reason)
        self.assertEqual(patch.quota_console["remaining"], 0)
        self.assertEqual(patch.quota_console["source"], int(QuotaSource.ESTIMATED))

    async def test_manual_refresh_failure_records_visible_metadata(self) -> None:
        service, repo = self._service()
        with patch.object(service, "_fetch_all_quotas", AsyncMock(return_value=None)):
            result = await service._refresh_one(repo.record, track_result=True)

        self.assertEqual(result.failed, 1)
        self.assertEqual(len(repo.patches), 1)
        metadata = repo.patches[0].ext_merge
        self.assertEqual(metadata["refresh_status"], "failed")
        self.assertEqual(metadata["refresh_error"], "上游未返回真实额度数据")
        self.assertGreater(metadata["refresh_at"], 0)

    async def test_manual_refresh_success_clears_previous_failure(self) -> None:
        service, repo = self._service()
        quota = QuotaWindow(
            remaining=8,
            total=30,
            window_seconds=86_400,
            reset_at=123,
            synced_at=100,
            source=QuotaSource.REAL,
        )
        with patch.object(service, "_fetch_all_quotas", AsyncMock(return_value={1: quota})):
            result = await service._refresh_one(repo.record, track_result=True)

        self.assertEqual(result.refreshed, 1)
        self.assertEqual(repo.patches[0].ext_merge["refresh_status"], "success")
        self.assertEqual(repo.patches[0].ext_merge["refresh_error"], "")

    async def test_legacy_console_429_expiry_recovers_without_history_or_delay(self) -> None:
        now = 1_000_000

        def console_quota(*, reset_at: int | None) -> dict:
            quota = default_quota_set("basic").to_dict()
            quota["console"] = {
                "remaining": 0,
                "total": 20,
                "window_seconds": 3600,
                "reset_at": reset_at,
                "synced_at": now - 1,
                "source": int(QuotaSource.ESTIMATED),
            }
            return quota

        expired = AccountRecord(
            token="legacy-expired",
            pool="basic",
            status=AccountStatus.EXPIRED,
            state_reason="console_429_threshold_exceeded",
            quota=console_quota(reset_at=now - 1),
            ext={"expired_at": now - 1, "console_429_count": 3},
        )
        cooling = AccountRecord(
            token="legacy-cooling",
            pool="basic",
            status=AccountStatus.EXPIRED,
            state_reason="console_429_threshold_exceeded",
            quota=console_quota(reset_at=now + 60_000),
            ext={"expired_at": now - 1, "console_429_count": 3},
        )
        genuine_invalid = AccountRecord(
            token="real-invalid",
            pool="basic",
            status=AccountStatus.EXPIRED,
            state_reason="invalid_credentials",
            quota=console_quota(reset_at=now - 1),
            ext={"expired_at": now - 1},
        )
        repo = _RepairRepository([expired, cooling, genuine_invalid])
        service = AccountRefreshService(repo)

        with patch("app.control.account.refresh.now_ms", return_value=now):
            repaired = await service.recover_console_expired_accounts()

        self.assertEqual(repaired, 2)
        self.assertEqual(len(repo.patch_calls), 2)
        cleared_tokens = {item.token for item in repo.patch_calls[0]}
        self.assertEqual(cleared_tokens, {"legacy-expired", "legacy-cooling"})
        self.assertTrue(all(item.clear_failures for item in repo.patch_calls[0]))
        followups = {item.token: item for item in repo.patch_calls[1]}
        self.assertNotIn("real-invalid", followups)
        self.assertEqual(followups["legacy-expired"].quota_console["remaining"], 20)
        self.assertEqual(followups["legacy-cooling"].status, AccountStatus.COOLING)
        self.assertEqual(
            followups["legacy-cooling"].state_reason,
            "console_429_threshold_exceeded",
        )
        self.assertEqual(followups["legacy-cooling"].ext_merge["cooldown_until"], now + 60_000)


if __name__ == "__main__":
    unittest.main()
