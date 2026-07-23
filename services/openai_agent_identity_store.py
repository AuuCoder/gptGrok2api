from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from services.config import DATA_DIR


class AgentIdentityStore:
    """Encrypted, process-safe archive for Codex Agent Identity credentials."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._data_dir = Path(data_dir)
        self._db_path = self._data_dir / "openai_agent_identities.db"
        self._key_path = self._data_dir / "openai_agent_identities.key"
        self._lock = threading.RLock()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._cipher = Fernet(self._load_or_create_key())
        self._initialize()

    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            key = self._key_path.read_bytes().strip()
            Fernet(key)
            try:
                os.chmod(self._key_path, 0o600)
            except OSError:
                pass
            return key

        key = Fernet.generate_key()
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._key_path.name}.",
            dir=str(self._key_path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "wb")
            fd = -1
            with stream:
                stream.write(key + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Publishing with a hard link is atomic and never replaces a
                # key another worker has already created.
                os.link(temporary, self._key_path)
                return key
            except FileExistsError:
                existing = self._key_path.read_bytes().strip()
                Fernet(existing)
                return existing
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                connection.close()
                raise
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_identities (
                    account_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    plan_type TEXT NOT NULL DEFAULT 'free',
                    agent_runtime_id TEXT NOT NULL UNIQUE,
                    private_key_ciphertext TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_identities_email_idx
                    ON agent_identities(email);
                """
            )
        try:
            os.chmod(self._db_path, 0o600)
        except OSError:
            pass

    def _encrypt(self, value: str) -> str:
        return self._cipher.encrypt(str(value or "").encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self._cipher.decrypt(str(value or "").encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise RuntimeError("Agent Identity archive key does not match the database") from exc

    def save(self, auth_json: dict[str, Any]) -> dict[str, Any]:
        identity = auth_json.get("agent_identity") if isinstance(auth_json, dict) else None
        if not isinstance(identity, dict):
            raise ValueError("agent_identity is required")
        account_id = str(identity.get("account_id") or "").strip()
        runtime_id = str(identity.get("agent_runtime_id") or "").strip()
        private_key = str(identity.get("agent_private_key") or "").strip()
        if not account_id or not runtime_id or not private_key:
            raise ValueError("Agent Identity is incomplete")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO agent_identities(
                    account_id, user_id, email, plan_type, agent_runtime_id,
                    private_key_ciphertext, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    email = excluded.email,
                    plan_type = excluded.plan_type,
                    agent_runtime_id = excluded.agent_runtime_id,
                    private_key_ciphertext = excluded.private_key_ciphertext,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    str(identity.get("chatgpt_user_id") or ""),
                    str(identity.get("email") or ""),
                    str(identity.get("plan_type") or "free"),
                    runtime_id,
                    self._encrypt(private_key),
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get(account_id) or {}

    @staticmethod
    def _auth_json(row: sqlite3.Row, private_key: str) -> dict[str, Any]:
        return {
            "auth_mode": "agent_identity",
            "agent_identity": {
                "agent_runtime_id": str(row["agent_runtime_id"]),
                "agent_private_key": private_key,
                "account_id": str(row["account_id"]),
                "chatgpt_user_id": str(row["user_id"] or ""),
                "email": str(row["email"] or ""),
                "plan_type": str(row["plan_type"] or "free"),
                "chatgpt_account_is_fedramp": False,
            },
        }

    def get(self, account_id: str) -> dict[str, Any] | None:
        key = str(account_id or "").strip()
        if not key:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM agent_identities WHERE account_id = ?", (key,)
            ).fetchone()
        return self._auth_json(row, self._decrypt(str(row["private_key_ciphertext"]))) if row else None

    def list_auth(self, account_ids: list[str] | None = None) -> list[dict[str, Any]]:
        targets = {str(item or "").strip() for item in (account_ids or []) if str(item or "").strip()}
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM agent_identities ORDER BY updated_at DESC"
            ).fetchall()
        return [
            self._auth_json(row, self._decrypt(str(row["private_key_ciphertext"])))
            for row in rows
            if not targets or str(row["account_id"]) in targets
        ]

    def summary(self) -> list[dict[str, str]]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT account_id, user_id, email, plan_type, agent_runtime_id,
                       created_at, updated_at
                FROM agent_identities ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]


openai_agent_identity_store = AgentIdentityStore()


__all__ = ["AgentIdentityStore", "openai_agent_identity_store"]
