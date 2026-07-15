from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from services.xai_cli_oauth_store import XaiCliOAuthAccountStore


class XaiCliOAuthAccountStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "xai_cli_oauth_accounts.json"
        self.store = XaiCliOAuthAccountStore(self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _payload(email: str, subject: str, suffix: str, **extra: object) -> dict[str, object]:
        return {
            "type": "xai",
            "auth_kind": "oauth",
            "email": email,
            "sub": subject,
            "access_token": f"access-{suffix}",
            "refresh_token": f"refresh-{suffix}",
            "id_token": f"id-{suffix}",
            "expires_in": 3600,
            **extra,
        }

    def test_upsert_accepts_cpa_shape_and_writes_owner_only_files(self) -> None:
        saved = self.store.upsert(
            self._payload(
                "person@example.com",
                "subject-one",
                "one",
                metadata={"display_name": "Person", "refresh_token": "must-not-persist"},
            )
        )

        self.assertTrue(saved["added"])
        self.assertEqual(saved["item"]["provider"], "xai_cli_oauth")
        self.assertEqual(saved["item"]["email"], "pe***n@example.com")
        self.assertNotIn("access-one", repr(saved))
        self.assertNotIn("refresh-one", repr(saved))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.path.with_suffix(".json.bak")).st_mode & 0o777, 0o600)

        raw = self.store.get_accounts_by_ids([saved["item"]["id"]])[0]
        self.assertEqual(raw["access_token"], "access-one")
        self.assertEqual(raw["refresh_token"], "refresh-one")
        self.assertEqual(raw["metadata"], {"display_name": "Person"})
        self.assertTrue(raw["expires_at"])

    def test_redacted_list_never_exposes_oauth_credentials(self) -> None:
        self.store.upsert(self._payload("secret@example.com", "top-secret-subject", "secret"))

        listed = self.store.list_accounts()

        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["has_access_token"])
        self.assertTrue(listed[0]["has_refresh_token"])
        self.assertTrue(listed[0]["has_id_token"])
        encoded = repr(listed)
        self.assertNotIn("access-secret", encoded)
        self.assertNotIn("refresh-secret", encoded)
        self.assertNotIn("id-secret", encoded)
        self.assertNotIn("top-secret-subject", encoded)

    def test_subject_deduplicates_refresh_token_rotation_and_preserves_identity(self) -> None:
        first = self.store.upsert(self._payload("first@example.com", "stable-subject", "one"))
        first_created_at = self.store.get(first["item"]["id"])["created_at"]
        second = self.store.upsert(
            self._payload("renamed@example.com", "stable-subject", "two", expires_at="2030-01-01T00:00:00+00:00")
        )

        self.assertFalse(second["added"])
        self.assertEqual(second["item"]["id"], first["item"]["id"])
        raw = self.store.get_accounts_by_ids([first["item"]["id"]])[0]
        self.assertEqual(raw["email"], "renamed@example.com")
        self.assertEqual(raw["access_token"], "access-two")
        self.assertEqual(raw["refresh_token"], "refresh-two")
        self.assertEqual(raw["created_at"], first_created_at)
        self.assertEqual(self.store.count(), 1)

    def test_round_robin_skips_disabled_accounts_and_honors_exclusions(self) -> None:
        first = self.store.upsert(self._payload("one@example.com", "subject-one", "one"))["item"]
        second = self.store.upsert(self._payload("two@example.com", "subject-two", "two"))["item"]
        third = self.store.upsert(self._payload("three@example.com", "subject-three", "three"))["item"]

        self.assertEqual(self.store.select_next_account()["id"], first["id"])
        self.assertEqual(self.store.select_next_account()["id"], second["id"])
        self.store.set_disabled([second["id"]], True)
        self.assertEqual(self.store.select_next_account()["id"], third["id"])
        self.assertEqual(
            self.store.select_next_account(exclude_ids=[first["id"]])["id"],
            third["id"],
        )

    def test_update_tokens_rotates_refresh_token_and_status_controls_selection(self) -> None:
        saved = self.store.upsert(self._payload("person@example.com", "subject-one", "one"))["item"]

        updated = self.store.update_tokens(
            saved["id"],
            access_token="access-two",
            refresh_token="refresh-two",
            id_token="id-two",
            expires_at="2032-01-01T00:00:00+00:00",
        )
        self.assertIsNotNone(updated)
        self.assertTrue(updated["has_access_token"])
        raw = self.store.get_accounts_by_ids([saved["id"]])[0]
        self.assertEqual(raw["access_token"], "access-two")
        self.assertEqual(raw["refresh_token"], "refresh-two")
        self.assertEqual(raw["id_token"], "id-two")
        self.assertEqual(raw["expires_at"], "2032-01-01T00:00:00+00:00")

        self.assertEqual(self.store.set_disabled([saved["id"]], True)["updated"], 1)
        self.assertIsNone(self.store.select_next_account())
        self.assertEqual(self.store.set_disabled([saved["id"]], False)["updated"], 1)
        self.assertEqual(self.store.select_next_account()["id"], saved["id"])
        self.assertEqual(self.store.set_status([saved["id"]], "invalid")["updated"], 1)
        self.assertIsNone(self.store.select_next_account())

    def test_record_result_and_available_models_are_publicly_safe(self) -> None:
        first = self.store.upsert(
            self._payload("one@example.com", "subject-one", "one", models=["grok-4.5", "grok-4.5"])
        )["item"]
        second = self.store.upsert(
            self._payload("two@example.com", "subject-two", "two", models=["grok-4.5-fast"])
        )["item"]

        self.assertEqual(self.store.available_models(), ["grok-4.5", "grok-4.5-fast"])
        self.store.record_result(first["id"], False, "401 access-one refresh-one")
        self.store.record_result(first["id"], True)
        account = self.store.get(first["id"], redacted=True)
        self.assertEqual(account["use_count"], 1)
        self.assertEqual(account["fail_count"], 1)
        self.assertEqual(account["last_error"], "")
        self.store.set_disabled(second["id"], True)
        self.assertEqual(self.store.available_models(), ["grok-4.5"])

    def test_reimport_preserves_operator_disable_and_cached_models(self) -> None:
        saved = self.store.upsert(
            self._payload("person@example.com", "subject-one", "one", models=["grok-4.5"])
        )["item"]
        self.store.set_disabled(saved["id"], True)

        self.store.upsert(self._payload("person@example.com", "subject-one", "two"))

        account = self.store.get(saved["id"], redacted=True)
        self.assertEqual(account["status"], "disabled")
        self.assertEqual(account["models"], ["grok-4.5"])
        self.assertIsNone(self.store.select())

    def test_validation_and_delete_use_safe_stable_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "email or subject"):
            self.store.upsert({"refresh_token": "dummy"})
        with self.assertRaisesRegex(ValueError, "refresh_token"):
            self.store.upsert({"email": "missing@example.com"})

        first = self.store.upsert(self._payload("one@example.com", "subject-one", "one"))["item"]
        second = self.store.upsert(self._payload("two@example.com", "subject-two", "two"))["item"]
        self.assertEqual(self.store.delete_accounts(["one@example.com"]), {"removed": 0, "count": 2})
        self.assertEqual(self.store.delete_accounts([first["id"], first["id"]]), {"removed": 1, "count": 1})
        self.assertEqual(self.store.get_accounts_by_ids([second["id"]])[0]["email"], "two@example.com")


if __name__ == "__main__":
    unittest.main()
