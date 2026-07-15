from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from services import provider_account_stats


class ProviderAccountStatsTest(unittest.IsolatedAsyncioTestCase):
    async def test_unified_stats_include_gpt_runtime_and_oauth_accounts(self) -> None:
        fake_runtime = type(
            "FakeRuntime",
            (),
            {
                "available": True,
                "account_stats": AsyncMock(
                    return_value={
                        "total": 3,
                        "active": 2,
                        "limited": 1,
                        "total_quota": 90,
                        "total_success": 7,
                        "total_fail": 2,
                        "by_type": {"basic": 3},
                    }
                ),
            },
        )()
        with (
            patch.object(
                provider_account_stats.account_service,
                "get_stats",
                return_value={
                    "total": 2,
                    "active": 1,
                    "disabled": 1,
                    "total_quota": 40,
                    "total_success": 4,
                    "total_fail": 1,
                    "by_type": {"Plus": 2},
                },
            ),
            patch.object(provider_account_stats, "grok_runtime", fake_runtime),
            patch.object(
                provider_account_stats.xai_cli_oauth_store,
                "list_accounts",
                return_value=[
                    {"status": "active", "use_count": 5, "fail_count": 1},
                    {"status": "invalid", "use_count": 1, "fail_count": 3},
                ],
            ),
        ):
            stats = await provider_account_stats.get_provider_account_stats()

        self.assertEqual(stats["total"], 7)
        self.assertEqual(stats["active"], 4)
        self.assertEqual(stats["limited"], 1)
        self.assertEqual(stats["abnormal"], 1)
        self.assertEqual(stats["disabled"], 1)
        self.assertEqual(stats["total_quota"], 130)
        self.assertEqual(stats["providers"]["gpt"]["total"], 2)
        self.assertEqual(stats["providers"]["grok"]["total"], 5)
        self.assertEqual(stats["providers"]["grok_oauth"]["total_quota"], 0)

    async def test_image_stats_exclude_text_only_oauth_accounts(self) -> None:
        fake_runtime = type(
            "FakeRuntime",
            (),
            {
                "available": True,
                "account_stats": AsyncMock(return_value={"total": 4, "active": 4, "total_quota": 120}),
            },
        )()
        with (
            patch.object(
                provider_account_stats.account_service,
                "get_stats",
                return_value={"total": 2, "active": 2, "total_quota": 20},
            ),
            patch.object(provider_account_stats, "grok_runtime", fake_runtime),
            patch.object(provider_account_stats.xai_cli_oauth_store, "list_accounts") as oauth_list,
        ):
            stats = await provider_account_stats.get_image_account_stats()

        self.assertEqual(stats["total"], 6)
        self.assertEqual(stats["total_quota"], 140)
        self.assertEqual(set(stats["providers"]), {"gpt", "grok"})
        oauth_list.assert_not_called()


if __name__ == "__main__":
    unittest.main()
