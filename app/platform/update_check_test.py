import unittest
from unittest.mock import AsyncMock, patch

from app.platform import update_check


class UpdateCheckTest(unittest.IsolatedAsyncioTestCase):
    def test_select_latest_release_uses_version_order_and_skips_drafts(self):
        releases = [
            {"tag_name": "v1.9.9", "draft": False},
            {"tag_name": "v2.0.0", "draft": True},
            {"tag_name": "v1.10.0", "draft": False},
            {"tag_name": "nightly", "draft": False},
        ]

        selected = update_check._select_latest_release(releases)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["tag_name"], "v1.10.0")

    def test_build_payload_exposes_github_changelog(self):
        release = {
            "tag_name": "v1.0.3",
            "name": "v1.0.3",
            "html_url": "https://github.com/AuuCoder/gptGrok2api/releases/tag/v1.0.3",
            "published_at": "2026-07-18T11:31:23Z",
            "body": "release notes",
            "changelog": "# Changelog\n\n## 1.0.3",
        }

        with patch.object(update_check, "get_project_version", return_value="1.0.2"):
            payload = update_check._build_payload(release=release)

        self.assertEqual(payload["current_version"], "1.0.2")
        self.assertEqual(payload["latest_version"], "1.0.3")
        self.assertEqual(payload["changelog"], release["changelog"])
        self.assertTrue(payload["update_available"])

    def test_success_cache_uses_short_ttl_while_release_is_pending(self):
        payload = {
            "current_version": "1.0.4",
            "latest_version": "1.0.3",
            "changelog": "# Changelog\n\n## 1.0.3 - 2026-07-18\n",
        }

        self.assertEqual(update_check._success_cache_ttl(payload), update_check._ERROR_TTL_SECONDS)

    def test_success_cache_uses_short_ttl_for_unreleased_changelog(self):
        payload = {
            "current_version": "1.0.4",
            "latest_version": "1.0.4",
            "changelog": "# Changelog\n\n## Unreleased\n",
        }

        self.assertEqual(update_check._success_cache_ttl(payload), update_check._ERROR_TTL_SECONDS)

    def test_success_cache_uses_normal_ttl_for_published_release(self):
        payload = {
            "current_version": "1.0.4",
            "latest_version": "1.0.4",
            "changelog": "# Changelog\n\n## 1.0.4 - 2026-07-19\n",
        }

        self.assertEqual(update_check._success_cache_ttl(payload), update_check._CACHE_TTL_SECONDS)

    async def test_fetch_latest_release_combines_github_release_and_changelog(self):
        releases = [
            {"tag_name": "v1.0.2", "draft": False},
            {"tag_name": "v1.0.3", "draft": False, "body": "notes"},
        ]
        with patch.object(
            update_check,
            "_fetch_github_releases",
            new=AsyncMock(return_value=releases),
        ), patch.object(
            update_check,
            "_fetch_github_changelog",
            new=AsyncMock(return_value="# Changelog"),
        ):
            release = await update_check._fetch_latest_release()

        self.assertEqual(release["tag_name"], "v1.0.3")
        self.assertEqual(release["changelog"], "# Changelog")


if __name__ == "__main__":
    unittest.main()
