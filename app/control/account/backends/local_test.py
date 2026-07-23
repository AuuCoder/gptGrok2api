from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import QuotaSource


class LocalAccountRepositoryTokenPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_connection_initialization_is_serialized(self) -> None:
        class FakeConnection:
            row_factory = None

            def execute(self, _statement: str):
                return self

        active = 0
        max_active = 0
        guard = threading.Lock()

        def connect(*_args, **_kwargs):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with guard:
                active -= 1
            return FakeConnection()

        repo = LocalAccountRepository(Path("accounts.db"))
        with patch("app.control.account.backends.local.sqlite3.connect", side_effect=connect):
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda _: repo._connect(), range(8)))

        self.assertEqual(max_active, 1)

    async def test_connection_lifecycles_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = LocalAccountRepository(Path(temp_dir) / "accounts.db")
            active = 0
            max_active = 0
            guard = threading.Lock()

            def hold_connection(_index: int) -> None:
                nonlocal active, max_active
                with repo._connection():
                    with guard:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.01)
                    with guard:
                        active -= 1

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(hold_connection, range(8)))

        self.assertEqual(max_active, 1)

    async def test_console_quota_keeps_source_and_reset_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = LocalAccountRepository(Path(temp_dir) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="test-token", pool="basic")])
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token="test-token",
                        ext_merge={
                            "refresh_status": "failed",
                            "refresh_at": 1_800_000_000_100,
                            "refresh_error": "上游未返回真实额度数据",
                        },
                        quota_console={
                            "remaining": 0,
                            "total": 20,
                            "window_seconds": 3600,
                            "reset_at": 1_800_000_000_000,
                            "synced_at": None,
                            "source": int(QuotaSource.ESTIMATED),
                        },
                    )
                ]
            )

            payload = (await repo.list_token_payloads())[0]

        self.assertEqual(
            payload["quota"]["console"],
            {
                "remaining": 0,
                "total": 20,
                "reset_at": 1_800_000_000_000,
                "source": int(QuotaSource.ESTIMATED),
            },
        )
        self.assertEqual(payload["refresh_status"], "failed")
        self.assertEqual(payload["refresh_at"], 1_800_000_000_100)
        self.assertEqual(payload["refresh_error"], "上游未返回真实额度数据")


if __name__ == "__main__":
    unittest.main()
