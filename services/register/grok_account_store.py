from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.json_file import read_json_file


GROK_ACCOUNTS_FILE = DATA_DIR / "grok_accounts.json"
_SECRET_PROFILE_KEYS = {
    "access_token",
    "cookies",
    "password",
    "refresh_token",
    "sso",
    "sso_token",
    "token",
}
_STATUS_RANK = {
    "submitting": 30,
    "pending_submit": 30,
    "submission_failed": 40,
    "submission_unknown": 45,
    "submission_unconfirmed": 50,
    "pending_sso": 60,
    "active": 100,
}

_RUNTIME_ACCOUNT_FIELDS = (
    "status",
    "pool",
    "quota",
    "use_count",
    "fail_count",
    "last_used_at",
    "tags",
    "refresh_status",
    "refresh_at",
    "refresh_error",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_sso(value: object) -> str:
    token = _clean_text(value)
    if token.startswith("sso="):
        token = token[4:].split(";", 1)[0].strip()
    return token


def _mask_email(value: object) -> str:
    local, sep, domain = _clean_text(value).partition("@")
    if not sep:
        return "***"
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1:]}@{domain}"


def _matches_filters(item: dict[str, Any], *, keyword: str, status: str) -> bool:
    status_filter = _clean_text(status).lower()
    if status_filter and status_filter != "all":
        item_status = _clean_text(item.get("status")).lower() or "active"
        if item_status != status_filter:
            return False

    needle = _clean_text(keyword).lower()
    if not needle:
        return True
    return any(
        needle in _clean_text(value).lower()
        for value in (
            item.get("id"),
            item.get("email"),
            item.get("source_type"),
            item.get("status"),
        )
    )


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _runtime_snapshot(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Keep the host archive's runtime mirror deliberately free of credentials."""
    token = _normalize_sso(item.get("token") or item.get("sso"))
    if not token:
        return None

    last_used_at = item.get("last_used_at")
    if not isinstance(last_used_at, (str, int, float)):
        last_used_at = None
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    refresh_at = item.get("refresh_at")
    if not isinstance(refresh_at, (str, int, float)):
        refresh_at = None
    return token, {
        "present": True,
        "status": _clean_text(item.get("status")).lower() or "active",
        "pool": _clean_text(item.get("pool")) or "basic",
        "quota": copy.deepcopy(item.get("quota")) if isinstance(item.get("quota"), dict) else {},
        "use_count": _non_negative_int(item.get("use_count")),
        "fail_count": _non_negative_int(item.get("fail_count")),
        "last_used_at": last_used_at,
        "tags": list(dict.fromkeys(_clean_text(value) for value in tags if _clean_text(value))),
        "refresh_status": _clean_text(item.get("refresh_status")).lower(),
        "refresh_at": refresh_at,
        "refresh_error": _clean_text(item.get("refresh_error"))[:300],
    }


def _runtime_values_match(current: object, expected: dict[str, Any]) -> bool:
    source = current if isinstance(current, dict) else {}
    return all(source.get(key) == expected.get(key) for key in ("present", *_RUNTIME_ACCOUNT_FIELDS))


class GrokAccountStore:
    def __init__(self, file_path: Path = GROK_ACCOUNTS_FILE):
        self.file_path = file_path
        self._lock = threading.RLock()
        self._secure_existing_files()

    def _load_unlocked(self) -> list[dict[str, Any]]:
        data = read_json_file(
            self.file_path,
            name=self.file_path.name,
            default_factory=list,
            expected_types=(dict, list),
        )
        if isinstance(data, dict):
            data = data.get("items")
        return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _secure_existing_files(self) -> None:
        for path in (self.file_path, self.file_path.with_suffix(self.file_path.suffix + ".bak")):
            try:
                if path.is_file():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _secure_write(path: Path, data: list[dict[str, Any]]) -> None:
        """Atomically persist account credentials with owner-only permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _save_unlocked(self, items: list[dict[str, Any]]) -> None:
        self._secure_write(self.file_path, items)
        self._secure_write(self.file_path.with_suffix(self.file_path.suffix + ".bak"), items)

    @staticmethod
    def _account_payload(item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Grok 注册结果必须是对象")

        credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
        profile_source = item.get("profile") if isinstance(item.get("profile"), dict) else {}
        email = _clean_text(item.get("email") or credentials.get("email") or profile_source.get("email"))
        password = _clean_text(item.get("password") or credentials.get("password") or profile_source.get("password"))
        sso = _normalize_sso(
            item.get("sso")
            or item.get("sso_token")
            or credentials.get("sso")
            or credentials.get("sso_token")
            or auth.get("sso")
            or auth.get("token")
        )
        if not email and not sso:
            raise ValueError("Grok 注册结果缺少 email 或 sso")

        profile = {
            str(key): copy.deepcopy(value)
            for key, value in profile_source.items()
            if str(key).lower() not in _SECRET_PROFILE_KEYS
        }
        created_at = _clean_text(item.get("created_at")) or _now()
        payload: dict[str, Any] = {
            "id": f"grok-{uuid.uuid4().hex}",
            "platform": "grok",
            "email": email,
            "password": password,
            "sso": sso,
            "profile": profile,
            "source_type": _clean_text(item.get("source_type")) or "protocol",
            "status": _clean_text(item.get("status")) or "active",
            "created_at": created_at,
            "updated_at": _now(),
        }
        return payload

    def upsert(self, item: dict[str, Any]) -> dict[str, Any]:
        incoming = self._account_payload(item)
        email_key = incoming["email"].lower()
        sso_key = incoming["sso"]

        with self._lock:
            items = self._load_unlocked()
            match_indexes = [
                index
                for index, current in enumerate(items)
                if (email_key and _clean_text(current.get("email")).lower() == email_key)
                or (sso_key and _normalize_sso(current.get("sso")) == sso_key)
            ]
            existing_id = _clean_text(items[match_indexes[0]].get("id")) if match_indexes else ""
            existing_created_at = _clean_text(items[match_indexes[0]].get("created_at")) if match_indexes else ""
            merged: dict[str, Any] = {}
            for index in match_indexes:
                merged.update(items[index])
            existing_rank = _STATUS_RANK.get(_clean_text(merged.get("status")).lower(), 0)
            incoming_rank = _STATUS_RANK.get(_clean_text(incoming.get("status")).lower(), 0)
            incoming_values = {key: value for key, value in incoming.items() if value not in (None, "", {})}
            if existing_rank > incoming_rank:
                for key in ("password", "profile", "source_type", "status"):
                    incoming_values.pop(key, None)
            merged.update(incoming_values)
            merged["id"] = existing_id or _clean_text(merged.get("id")) or incoming["id"]
            merged["platform"] = "grok"
            merged.setdefault("email", "")
            merged.setdefault("password", "")
            merged.setdefault("sso", "")
            merged.setdefault("profile", {})
            merged["created_at"] = existing_created_at or _clean_text(merged.get("created_at")) or incoming["created_at"]
            merged["updated_at"] = _now()

            if match_indexes:
                first_index = match_indexes[0]
                match_set = set(match_indexes)
                next_items = [current for index, current in enumerate(items) if index not in match_set]
                next_items.insert(min(first_index, len(next_items)), merged)
                added = False
            else:
                next_items = [*items, merged]
                added = True

            self._save_unlocked(next_items)
            return {"added": added, "count": len(next_items), "item": copy.deepcopy(merged)}

    def reconcile_runtime_accounts(self, runtime_items: list[dict[str, Any]]) -> dict[str, int]:
        """Mirror the runtime account pool without replacing registration provenance.

        ``grok_accounts.json`` is the host-side archive.  It owns registration
        fields such as email, password and source_type; the embedded Grok
        runtime owns only the non-secret operational fields stored under
        ``runtime``.  Keeping that boundary prevents a runtime-only token from
        erasing a registered account's mailbox details when both stores are
        reconciled.
        """
        snapshots: dict[str, dict[str, Any]] = {}
        for raw in runtime_items:
            if not isinstance(raw, dict):
                continue
            normalized = _runtime_snapshot(raw)
            if normalized is None:
                continue
            token, snapshot = normalized
            snapshots[token] = snapshot

        with self._lock:
            items = self._load_unlocked()
            now = _now()
            changed = False
            added = 0
            updated = 0
            missing = 0
            matched_tokens: set[str] = set()
            next_items: list[dict[str, Any]] = []

            for item in items:
                current = dict(item)
                token = _normalize_sso(current.get("sso"))
                snapshot = snapshots.get(token) if token else None
                if snapshot is not None:
                    matched_tokens.add(token)
                    if not _runtime_values_match(current.get("runtime"), snapshot):
                        current["runtime"] = {**snapshot, "synced_at": now}
                        current["updated_at"] = now
                        changed = True
                        updated += 1
                elif token:
                    # Do not delete a registration archive just because an
                    # operator removed its runtime token.  It can be added
                    # back explicitly, while the preserved mailbox/provenance
                    # remains available for diagnosis or export.
                    absent = {"present": False, "status": "removed"}
                    if not _runtime_values_match(current.get("runtime"), absent):
                        current["runtime"] = {**absent, "synced_at": now}
                        current["updated_at"] = now
                        changed = True
                        missing += 1
                next_items.append(current)

            for token, snapshot in snapshots.items():
                if token in matched_tokens:
                    continue
                next_items.append(
                    {
                        "id": f"grok-{uuid.uuid4().hex}",
                        "platform": "grok",
                        "email": "",
                        "password": "",
                        "sso": token,
                        "profile": {},
                        "source_type": "runtime",
                        "status": "active",
                        "runtime": {**snapshot, "synced_at": now},
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                added += 1
                changed = True

            if changed:
                self._save_unlocked(next_items)
            return {
                "added": added,
                "updated": updated,
                "missing": missing,
                "count": len(next_items),
            }

    def list_accounts(
        self,
        *,
        redacted: bool = True,
        keyword: str = "",
        status: str = "all",
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                copy.deepcopy(item)
                for item in self._load_unlocked()
                if _matches_filters(item, keyword=keyword, status=status)
            ]
        if not redacted:
            return items
        return [
            {
                "id": _clean_text(item.get("id")),
                "platform": "grok",
                "email": _mask_email(item.get("email")) if _clean_text(item.get("email")) else "",
                "has_password": bool(_clean_text(item.get("password"))),
                "has_sso": bool(_normalize_sso(item.get("sso"))),
                "source_type": _clean_text(item.get("source_type")) or "protocol",
                "status": _clean_text(item.get("status")) or "active",
                "oauth_authorization": copy.deepcopy(item.get("oauth_authorization"))
                if isinstance(item.get("oauth_authorization"), dict)
                else {},
                "created_at": _clean_text(item.get("created_at")),
                "updated_at": _clean_text(item.get("updated_at")),
            }
            for item in items
        ]

    def delete_accounts(self, ids: list[str]) -> dict[str, int]:
        target_ids = {
            account_id
            for value in ids
            if (account_id := _clean_text(value))
        }
        if not target_ids:
            return {"removed": 0, "count": self.count()}

        with self._lock:
            items = self._load_unlocked()
            next_items = [item for item in items if _clean_text(item.get("id")) not in target_ids]
            removed = len(items) - len(next_items)
            if removed:
                self._save_unlocked(next_items)
            return {"removed": removed, "count": len(next_items)}

    def get_accounts_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        ordered_ids = list(
            dict.fromkeys(
                account_id
                for value in ids
                if (account_id := _clean_text(value))
            )
        )
        if not ordered_ids:
            return []

        with self._lock:
            by_id = {
                _clean_text(item.get("id")): item
                for item in self._load_unlocked()
                if _clean_text(item.get("id"))
            }
            return [copy.deepcopy(by_id[account_id]) for account_id in ordered_ids if account_id in by_id]

    def update_probe_results(self, results: list[dict[str, Any]], *, probed_at: str) -> dict[str, int]:
        """Persist credential-free login probe outcomes by stable account ID."""
        normalized: dict[str, dict[str, Any]] = {}
        for raw in results:
            if not isinstance(raw, dict):
                continue
            account_id = _clean_text(raw.get("id"))
            status = _clean_text(raw.get("status")).lower()
            if not account_id or status not in {"valid", "invalid", "unknown"}:
                continue
            probe: dict[str, Any] = {
                "status": status,
                "at": _clean_text(probed_at) or _now(),
            }
            quota = raw.get("quota") if isinstance(raw.get("quota"), dict) else None
            if quota is not None:
                probe["quota"] = {
                    "remaining": _non_negative_int(quota.get("remaining")),
                    "total": _non_negative_int(quota.get("total")),
                }
            error = _clean_text(raw.get("error"))
            if error:
                probe["error"] = error[:300]
            normalized[account_id] = probe

        if not normalized:
            return {"updated": 0, "missing": 0}

        with self._lock:
            items = self._load_unlocked()
            updated = 0
            matched: set[str] = set()
            now = _now()
            next_items: list[dict[str, Any]] = []
            for item in items:
                current = dict(item)
                account_id = _clean_text(current.get("id"))
                probe = normalized.get(account_id)
                if probe is not None:
                    current["probe"] = probe
                    current["updated_at"] = now
                    matched.add(account_id)
                    updated += 1
                next_items.append(current)
            if updated:
                self._save_unlocked(next_items)
            return {"updated": updated, "missing": len(normalized) - len(matched)}

    def update_recovery_state(
        self,
        account_id: str,
        *,
        status: str,
        last_attempt_at: str | None = None,
        last_success_at: str | None = None,
        next_attempt_at: str | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> bool:
        """Persist non-secret automatic recovery state for one stable account ID."""
        target_id = _clean_text(account_id)
        normalized_status = _clean_text(status).lower()
        if not target_id or normalized_status not in {"pending", "running", "success", "failed"}:
            return False

        with self._lock:
            items = self._load_unlocked()
            now = _now()
            changed = False
            next_items: list[dict[str, Any]] = []
            for item in items:
                current = dict(item)
                if _clean_text(current.get("id")) != target_id:
                    next_items.append(current)
                    continue

                recovery = dict(current.get("recovery")) if isinstance(current.get("recovery"), dict) else {}
                recovery["status"] = normalized_status
                if last_attempt_at is not None:
                    recovery["last_attempt_at"] = _clean_text(last_attempt_at)
                if last_success_at is not None:
                    recovery["last_success_at"] = _clean_text(last_success_at)
                if next_attempt_at is not None:
                    recovery["next_attempt_at"] = _clean_text(next_attempt_at)
                if error is not None:
                    recovery["error"] = _clean_text(error)[:300]
                if attempts is not None:
                    recovery["attempts"] = _non_negative_int(attempts)
                current["recovery"] = recovery
                current["updated_at"] = now
                changed = True
                next_items.append(current)

            if changed:
                self._save_unlocked(next_items)
            return changed

    def update_oauth_authorization_state(
        self,
        account_id: str,
        *,
        status: str,
        attempted_at: str | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> bool:
        """Persist the credential-free result of OAuth authorization."""
        target_id = _clean_text(account_id)
        normalized_status = _clean_text(status).lower()
        if not target_id or normalized_status not in {"pending", "success", "failed", "denied"}:
            return False

        with self._lock:
            items = self._load_unlocked()
            now = _now()
            changed = False
            next_items: list[dict[str, Any]] = []
            for item in items:
                current = dict(item)
                if _clean_text(current.get("id")) != target_id:
                    next_items.append(current)
                    continue
                state = dict(current.get("oauth_authorization")) if isinstance(
                    current.get("oauth_authorization"), dict
                ) else {}
                state["status"] = normalized_status
                state["attempted_at"] = _clean_text(attempted_at) or now
                if error is not None:
                    state["error"] = _clean_text(error)[:300]
                if attempts is not None:
                    state["attempts"] = _non_negative_int(attempts)
                current["oauth_authorization"] = state
                current["updated_at"] = now
                changed = True
                next_items.append(current)
            if changed:
                self._save_unlocked(next_items)
            return changed

    def replace_sso_after_recovery(
        self,
        account_id: str,
        *,
        expected_sso: str,
        new_sso: str,
        recovered_at: str,
        quota: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically replace one account's SSO and absorb its runtime-only mirror."""
        target_id = _clean_text(account_id)
        expected = _normalize_sso(expected_sso)
        replacement = _normalize_sso(new_sso)
        if not target_id or not replacement:
            raise ValueError("Grok 自动恢复缺少账号 ID 或新 SSO")

        with self._lock:
            items = self._load_unlocked()
            target_index = next(
                (index for index, item in enumerate(items) if _clean_text(item.get("id")) == target_id),
                None,
            )
            if target_index is None:
                raise ValueError("Grok 自动恢复账号不存在")

            target = dict(items[target_index])
            if _normalize_sso(target.get("sso")) != expected:
                raise RuntimeError("Grok 账号登录态已被其他操作更新")

            duplicate_indexes: set[int] = set()
            for index, item in enumerate(items):
                if index == target_index or _normalize_sso(item.get("sso")) != replacement:
                    continue
                is_runtime_only = (
                    _clean_text(item.get("source_type")).lower() == "runtime"
                    and not _clean_text(item.get("email"))
                    and not _clean_text(item.get("password"))
                )
                if not is_runtime_only:
                    raise RuntimeError("新 Grok SSO 已属于其他已保存账号")
                duplicate_indexes.add(index)
                if isinstance(item.get("runtime"), dict):
                    target["runtime"] = copy.deepcopy(item["runtime"])

            timestamp = _clean_text(recovered_at) or _now()
            probe: dict[str, Any] = {"status": "valid", "at": timestamp}
            if isinstance(quota, dict):
                probe["quota"] = {
                    "remaining": _non_negative_int(quota.get("remaining")),
                    "total": _non_negative_int(quota.get("total")),
                }
            recovery = dict(target.get("recovery")) if isinstance(target.get("recovery"), dict) else {}
            recovery.update(
                {
                    "status": "success",
                    "last_attempt_at": timestamp,
                    "last_success_at": timestamp,
                    "next_attempt_at": "",
                    "error": "",
                    "attempts": 0,
                }
            )
            target.update(
                {
                    "sso": replacement,
                    "status": "active",
                    "probe": probe,
                    "recovery": recovery,
                    "updated_at": timestamp,
                }
            )

            next_items: list[dict[str, Any]] = []
            for index, item in enumerate(items):
                if index in duplicate_indexes:
                    continue
                next_items.append(target if index == target_index else item)
            self._save_unlocked(next_items)
            return copy.deepcopy(target)

    def get_login_credentials(self, account_id: str) -> dict[str, str] | None:
        """Return only the email/password pair for one explicit admin action."""
        target_id = _clean_text(account_id)
        if not target_id:
            return None
        with self._lock:
            for item in self._load_unlocked():
                if _clean_text(item.get("id")) != target_id:
                    continue
                return {
                    "id": target_id,
                    "email": _clean_text(item.get("email")),
                    "password": _clean_text(item.get("password")),
                }
        return None

    def runtime_identity_for_token(self, token: str) -> dict[str, str]:
        """Resolve a runtime SSO token to log-safe account metadata."""
        normalized = _normalize_sso(token)
        if not normalized:
            return {}
        fallback_id = f"grok-sso-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]}"
        with self._lock:
            for item in self._load_unlocked():
                if _normalize_sso(item.get("sso")) != normalized:
                    continue
                email = _clean_text(item.get("email"))
                return {
                    "account_id": _clean_text(item.get("id")) or fallback_id,
                    "account_email": _mask_email(email) if email else "",
                }
        return {"account_id": fallback_id, "account_email": ""}

    def count(self) -> int:
        with self._lock:
            return len(self._load_unlocked())

    def export_text(self) -> str:
        lines = []
        for item in self.list_accounts(redacted=False):
            email = _clean_text(item.get("email")).replace("\n", " ").replace("\r", " ")
            password = _clean_text(item.get("password")).replace("\n", " ").replace("\r", " ")
            sso = _normalize_sso(item.get("sso")).replace("\n", " ").replace("\r", " ")
            if email or sso:
                lines.append(f"{email}----{password}----{sso}")
        return "\n".join(lines) + ("\n" if lines else "")


grok_account_store = GrokAccountStore()
