from __future__ import annotations

import io
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import services.backup_service as backup_module


class GrokRuntimeBackupTest(unittest.TestCase):
    def test_register_backup_contains_consistent_grok_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            runtime_dir = data_dir / "grok_runtime"
            runtime_dir.mkdir(parents=True)
            (data_dir / "register.json").write_text("{}\n", encoding="utf-8")
            (data_dir / "grok_accounts.json").write_text("[]\n", encoding="utf-8")
            (runtime_dir / "config.toml").write_text('[proxy.egress]\nmode = "direct"\n', encoding="utf-8")

            database_path = runtime_dir / "accounts.db"
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("create table accounts (token text primary key, status text not null)")
                database.execute("insert into accounts values (?, ?)", ("test-token", "disabled"))
                database.commit()

            with patch.object(backup_module, "DATA_DIR", data_dir):
                payload = backup_module.BackupService()._build_backup_archive(
                    {"include": {"register": True}},
                    trigger="test",
                )

            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                names = {member.name for member in archive.getmembers() if member.isfile()}
                self.assertIn("data/grok_runtime/config.toml", names)
                self.assertIn("data/grok_runtime/accounts.db", names)
                extracted = archive.extractfile("data/grok_runtime/accounts.db")
                self.assertIsNotNone(extracted)
                database_bytes = extracted.read() if extracted is not None else b""

            restored_path = Path(directory) / "restored.db"
            restored_path.write_bytes(database_bytes)
            with closing(sqlite3.connect(restored_path)) as restored:
                self.assertEqual(
                    restored.execute("select token, status from accounts").fetchone(),
                    ("test-token", "disabled"),
                )


if __name__ == "__main__":
    unittest.main()
