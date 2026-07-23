from __future__ import annotations

import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor

from services.openai_agent_identity_store import AgentIdentityStore


def test_agent_identity_private_key_is_encrypted_at_rest(tmp_path) -> None:
    store = AgentIdentityStore(tmp_path)
    auth_json = {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": "runtime-one",
            "agent_private_key": "private-key-must-not-be-plaintext",
            "account_id": "account-one",
            "chatgpt_user_id": "user-one",
            "email": "one@example.test",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
        },
    }

    store.save(auth_json)

    raw_database = (tmp_path / "openai_agent_identities.db").read_bytes()
    assert b"private-key-must-not-be-plaintext" not in raw_database
    assert stat.S_IMODE((tmp_path / "openai_agent_identities.key").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "openai_agent_identities.db").stat().st_mode) == 0o600
    assert store.get("account-one") == auth_json
    with sqlite3.connect(tmp_path / "openai_agent_identities.db") as connection:
        ciphertext = connection.execute(
            "SELECT private_key_ciphertext FROM agent_identities WHERE account_id = ?",
            ("account-one",),
        ).fetchone()[0]
    assert ciphertext != "private-key-must-not-be-plaintext"
    summary = store.summary()
    assert summary[0]["account_id"] == "account-one"
    assert "agent_private_key" not in summary[0]
    assert "private_key_ciphertext" not in summary[0]


def test_concurrent_stores_share_one_archive_key(tmp_path) -> None:
    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _: AgentIdentityStore(tmp_path), range(8)))

    auth_json = {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": "runtime-shared",
            "agent_private_key": "private-shared",
            "account_id": "account-shared",
            "chatgpt_user_id": "user-shared",
            "email": "shared@example.test",
            "plan_type": "free",
            "chatgpt_account_is_fedramp": False,
        },
    }
    stores[0].save(auth_json)
    assert all(store.get("account-shared") == auth_json for store in stores)
