from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from stat import S_IMODE

from services.register.grok_account_store import GrokAccountStore


class GrokAccountStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = GrokAccountStore(Path(self.temp_dir.name) / "grok_accounts.json")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upsert_deduplicates_by_email_or_sso(self) -> None:
        first = self.store.upsert(
            {"email": "User@example.com", "password": "first-password", "sso": "sso=first-token"}
        )
        first_id = first["item"]["id"]
        first_created_at = first["item"]["created_at"]
        second = self.store.upsert(
            {"email": "user@example.com", "password": "second-password", "sso": "second-token"}
        )
        third = self.store.upsert(
            {"email": "other@example.com", "password": "third-password", "sso": "second-token"}
        )

        self.assertTrue(first["added"])
        self.assertFalse(second["added"])
        self.assertFalse(third["added"])
        items = self.store.list_accounts(redacted=False)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["email"], "other@example.com")
        self.assertEqual(items[0]["password"], "third-password")
        self.assertEqual(items[0]["sso"], "second-token")
        self.assertEqual(items[0]["id"], first_id)
        self.assertEqual(items[0]["created_at"], first_created_at)

    def test_redacted_list_never_exposes_credentials(self) -> None:
        self.store.upsert(
            {"email": "secret@example.com", "password": "plain-password", "sso": "plain-sso-token"}
        )

        redacted = self.store.list_accounts(redacted=True)

        self.assertEqual(len(redacted), 1)
        self.assertEqual(redacted[0]["email"], "se***t@example.com")
        self.assertTrue(redacted[0]["has_password"])
        self.assertTrue(redacted[0]["has_sso"])
        self.assertNotIn("plain-password", repr(redacted))
        self.assertNotIn("plain-sso-token", repr(redacted))

    def test_concurrent_writes_are_not_lost(self) -> None:
        def save(index: int) -> None:
            self.store.upsert(
                {
                    "email": f"user-{index}@example.com",
                    "password": f"password-{index}",
                    "sso": f"token-{index}",
                }
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(save, range(40)))

        self.assertEqual(self.store.count(), 40)

    def test_text_export_matches_grok_account_format(self) -> None:
        self.store.upsert({"email": "user@example.com", "password": "password", "sso": "sso=token"})

        self.assertEqual(self.store.export_text(), "user@example.com----password----token\n")

    def test_login_credentials_excludes_sso(self) -> None:
        saved = self.store.upsert({"email": "user@example.com", "password": "password", "sso": "token"})

        credentials = self.store.get_login_credentials(saved["item"]["id"])

        self.assertEqual(
            credentials,
            {"id": saved["item"]["id"], "email": "user@example.com", "password": "password"},
        )
        self.assertNotIn("sso", credentials)

    def test_runtime_identity_for_token_returns_only_log_safe_metadata(self) -> None:
        saved = self.store.upsert(
            {
                "email": "secret@example.com",
                "password": "plain-password",
                "sso": "plain-sso-token",
            }
        )

        identity = self.store.runtime_identity_for_token("sso=plain-sso-token; Path=/")

        self.assertEqual(
            identity,
            {
                "account_id": saved["item"]["id"],
                "account_email": "se***t@example.com",
            },
        )
        self.assertNotIn("plain-password", repr(identity))
        self.assertNotIn("plain-sso-token", repr(identity))

    def test_runtime_identity_for_unknown_token_uses_non_secret_stable_id(self) -> None:
        first = self.store.runtime_identity_for_token("unknown-token")
        second = self.store.runtime_identity_for_token("sso=unknown-token")

        self.assertEqual(first, second)
        self.assertTrue(first["account_id"].startswith("grok-sso-"))
        self.assertNotIn("unknown-token", repr(first))

    def test_credential_files_use_owner_only_permissions(self) -> None:
        self.store.upsert({"email": "user@example.com", "password": "password", "sso": "token"})

        self.assertEqual(S_IMODE(self.store.file_path.stat().st_mode), 0o600)
        backup = self.store.file_path.with_suffix(self.store.file_path.suffix + ".bak")
        self.assertEqual(S_IMODE(backup.stat().st_mode), 0o600)

    def test_pending_account_upgrades_to_active_without_changing_identity(self) -> None:
        pending = self.store.upsert(
            {
                "email": "user@example.com",
                "password": "password",
                "sso": "",
                "status": "pending_sso",
                "profile": {"session_state": "missing"},
            }
        )

        active = self.store.upsert(
            {
                "email": "user@example.com",
                "password": "password",
                "sso": "token",
                "status": "active",
                "profile": {"session_state": "ready"},
            }
        )

        self.assertEqual(active["item"]["id"], pending["item"]["id"])
        self.assertEqual(active["item"]["created_at"], pending["item"]["created_at"])
        self.assertEqual(active["item"]["status"], "active")
        self.assertEqual(active["item"]["sso"], "token")

    def test_active_account_is_not_downgraded_by_late_pending_snapshot(self) -> None:
        active = self.store.upsert(
            {
                "email": "user@example.com",
                "password": "active-password",
                "sso": "token",
                "status": "active",
                "profile": {"session_state": "ready"},
            }
        )

        late = self.store.upsert(
            {
                "email": "user@example.com",
                "password": "stale-password",
                "sso": "",
                "status": "pending_sso",
                "profile": {"session_state": "missing"},
            }
        )

        self.assertEqual(late["item"]["id"], active["item"]["id"])
        self.assertEqual(late["item"]["status"], "active")
        self.assertEqual(late["item"]["password"], "active-password")
        self.assertEqual(late["item"]["profile"], {"session_state": "ready"})

    def test_list_filters_before_redacting_credentials(self) -> None:
        self.store.upsert(
            {
                "email": "search-target@example.com",
                "password": "plain-password",
                "sso": "plain-sso-token",
                "status": "active",
            }
        )
        self.store.upsert(
            {
                "email": "pending@example.com",
                "password": "pending-password",
                "sso": "",
                "status": "pending_sso",
            }
        )

        active = self.store.list_accounts(keyword="SEARCH-TARGET", status="ACTIVE")

        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["email"], "se***t@example.com")
        self.assertEqual(active[0]["status"], "active")
        self.assertNotIn("plain-password", repr(active))
        self.assertNotIn("plain-sso-token", repr(active))
        self.assertEqual(len(self.store.list_accounts(status="pending_sso")), 1)
        self.assertEqual(len(self.store.list_accounts(status="all")), 2)

    def test_active_filter_matches_legacy_items_without_status(self) -> None:
        saved = self.store.upsert(
            {"email": "legacy@example.com", "password": "password", "sso": "token"}
        )
        items = self.store.list_accounts(redacted=False)
        items[0].pop("status", None)
        self.store._save_unlocked(items)

        active = self.store.list_accounts(status="active")

        self.assertEqual([item["id"] for item in active], [saved["item"]["id"]])
        self.assertEqual(active[0]["status"], "active")

    def test_delete_accounts_uses_stable_ids_only(self) -> None:
        first = self.store.upsert({"email": "first@example.com", "password": "one", "sso": "token-one"})
        second = self.store.upsert({"email": "second@example.com", "password": "two", "sso": "token-two"})

        by_email = self.store.delete_accounts(["first@example.com"])
        deleted = self.store.delete_accounts([first["item"]["id"], first["item"]["id"], "missing-id"])

        self.assertEqual(by_email, {"removed": 0, "count": 2})
        self.assertEqual(deleted, {"removed": 1, "count": 1})
        remaining = self.store.list_accounts(redacted=False)
        self.assertEqual([item["id"] for item in remaining], [second["item"]["id"]])

    def test_get_accounts_by_ids_returns_raw_accounts_in_requested_order(self) -> None:
        first = self.store.upsert({"email": "first@example.com", "password": "one", "sso": "token-one"})
        second = self.store.upsert({"email": "second@example.com", "password": "two", "sso": "token-two"})

        accounts = self.store.get_accounts_by_ids(
            [second["item"]["id"], "missing-id", first["item"]["id"], second["item"]["id"]]
        )

        self.assertEqual(
            [item["id"] for item in accounts],
            [second["item"]["id"], first["item"]["id"]],
        )
        self.assertEqual([item["sso"] for item in accounts], ["token-two", "token-one"])

    def test_runtime_reconcile_adds_runtime_only_accounts_without_erasing_registration_fields(self) -> None:
        registered = self.store.upsert(
            {
                "email": "registered@example.com",
                "password": "registered-password",
                "sso": "registered-token",
                "source_type": "protocol",
                "profile": {"mail_provider": "icloud_api", "mailbox_id": "mailbox-1"},
            }
        )

        first = self.store.reconcile_runtime_accounts(
            [
                {
                    "token": "registered-token",
                    "pool": "super",
                    "status": "disabled",
                    "quota": {"fast": {"remaining": 2, "total": 5}},
                    "use_count": 7,
                    "fail_count": 1,
                    "tags": ["registered", "nsfw"],
                    "refresh_status": "failed",
                    "refresh_at": 123456789,
                    "refresh_error": "上游未返回真实额度数据",
                },
                {
                    "token": "runtime-only-token",
                    "pool": "basic",
                    "status": "cooling",
                    "quota": {"fast": {"remaining": 1, "total": 3}},
                },
            ]
        )

        self.assertEqual(first, {"added": 1, "updated": 1, "missing": 0, "count": 2})
        items = {item["sso"]: item for item in self.store.list_accounts(redacted=False)}
        saved = items["registered-token"]
        self.assertEqual(saved["id"], registered["item"]["id"])
        self.assertEqual(saved["email"], "registered@example.com")
        self.assertEqual(saved["password"], "registered-password")
        self.assertEqual(saved["source_type"], "protocol")
        self.assertEqual(saved["profile"], {"mail_provider": "icloud_api", "mailbox_id": "mailbox-1"})
        self.assertEqual(saved["runtime"]["status"], "disabled")
        self.assertEqual(saved["runtime"]["pool"], "super")
        self.assertEqual(saved["runtime"]["use_count"], 7)
        self.assertEqual(saved["runtime"]["refresh_status"], "failed")
        self.assertEqual(saved["runtime"]["refresh_at"], 123456789)
        self.assertEqual(saved["runtime"]["refresh_error"], "上游未返回真实额度数据")
        self.assertEqual(items["runtime-only-token"]["source_type"], "runtime")
        self.assertEqual(items["runtime-only-token"]["email"], "")
        self.assertEqual(items["runtime-only-token"]["runtime"]["status"], "cooling")

    def test_runtime_reconcile_marks_removed_without_deleting_registration_archive(self) -> None:
        saved = self.store.upsert(
            {
                "email": "registered@example.com",
                "password": "registered-password",
                "sso": "registered-token",
                "source_type": "protocol",
                "profile": {"mail_provider": "icloud_api"},
            }
        )
        self.store.reconcile_runtime_accounts(
            [{"token": "registered-token", "pool": "basic", "status": "active"}]
        )

        removed = self.store.reconcile_runtime_accounts([])

        self.assertEqual(removed, {"added": 0, "updated": 0, "missing": 1, "count": 1})
        account = self.store.get_accounts_by_ids([saved["item"]["id"]])[0]
        self.assertEqual(account["email"], "registered@example.com")
        self.assertEqual(account["password"], "registered-password")
        self.assertEqual(account["source_type"], "protocol")
        self.assertEqual(account["profile"], {"mail_provider": "icloud_api"})
        self.assertEqual(account["runtime"]["present"], False)
        self.assertEqual(account["runtime"]["status"], "removed")


if __name__ == "__main__":
    unittest.main()
