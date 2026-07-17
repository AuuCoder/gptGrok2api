"""Credential store for the xAI CLI OAuth provider.

This store is deliberately separate from both ``grok_accounts.json`` (the
registration archive) and the embedded Grok runtime account repository.  The
latter accepts grok.com SSO cookies, whereas Grok CLI uses a renewable OAuth
refresh token for ``cli-chat-proxy.grok.com``.

Only service code should request unredacted records.  API and UI handlers must
use :meth:`list_accounts` with its default ``redacted=True``.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.json_file import read_json_file


XAI_CLI_OAUTH_ACCOUNTS_FILE = DATA_DIR / "xai_cli_oauth_accounts.json"
XAI_CLI_OAUTH_PROVIDER = "xai_cli_oauth"
XAI_CLI_OAUTH_SCHEMA_VERSION = 1

_STATUSES = frozenset({"active", "disabled", "expired", "invalid"})
_SECRET_METADATA_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "cookies",
        "device_code",
        "id_token",
        "password",
        "private_key",
        "raw",
        "refresh_token",
        "secret",
        "session",
        "set_cookie",
        "token",
        "user_code",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _as_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _mask_email(value: object) -> str:
    local, separator, domain = _clean_text(value).partition("@")
    if not separator:
        return ""
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1:]}@{domain}"


def _mask_subject(value: object) -> str:
    subject = _clean_text(value)
    if len(subject) <= 8:
        return "***" if subject else ""
    return f"{subject[:4]}...{subject[-4:]}"


def _normalize_status(value: object, *, default: str = "active") -> str:
    status = _clean_text(value).lower() or default
    if status not in _STATUSES:
        choices = ", ".join(sorted(_STATUSES))
        raise ValueError(f"xAI CLI OAuth status must be one of: {choices}")
    return status


def _identity_key(item: dict[str, Any]) -> tuple[str, str]:
    """Return the stable (subject, normalized-email) identity tuple."""
    return _clean_text(item.get("subject")), _clean_text(item.get("email")).lower()


def _metadata_key_is_secret(key: object) -> bool:
    normalized = _clean_text(key).lower().replace("-", "_")
    return (
        normalized in _SECRET_METADATA_KEYS
        or normalized.endswith(("_token", "_secret", "_password", "_cookie", "_key"))
    )


def _safe_metadata(value: object) -> Any:
    """Copy metadata while dropping values that could accidentally be secrets."""
    if isinstance(value, dict):
        return {
            str(key): _safe_metadata(child)
            for key, child in value.items()
            if not _metadata_key_is_secret(key)
        }
    if isinstance(value, list):
        return [_safe_metadata(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Request bodies are JSON in normal use.  Ignore arbitrary objects rather
    # than persisting their repr, which could contain a token.
    return None


def _first_text(*values: object) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _expires_at_from_seconds(value: object) -> str:
    try:
        seconds = max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return ""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _model_ids(value: object) -> list[str]:
    source = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(_clean_text(item) for item in source if _clean_text(item)))


def _non_negative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_error(value: object, item: dict[str, Any]) -> str:
    """Keep a compact diagnostic without accidentally persisting a credential."""
    text = _clean_text(value)[:500]
    for key in ("access_token", "refresh_token", "id_token"):
        secret = _clean_text(item.get(key))
        if secret:
            text = text.replace(secret, "***")
    return text


def account_log_identity(item: dict[str, Any]) -> dict[str, str]:
    """Return the non-secret identity allowed in a call log."""
    return {
        "account_id": _clean_text(item.get("id")),
        "account_email": _mask_email(item.get("email")),
    }


class XaiCliOAuthAccountStore:
    """Small, file-backed OAuth credential pool for the future xAI CLI provider.

    Persisted records have a provider-specific schema instead of sharing the
    SSO token schema used by the existing Grok runtime.  The only deduplication
    keys are OAuth ``subject`` (preferred) and email.  Refresh-token rotation
    therefore updates the existing account rather than creating a new one.
    """

    def __init__(self, file_path: Path = XAI_CLI_OAUTH_ACCOUNTS_FILE):
        self.file_path = Path(file_path)
        self._lock = threading.RLock()
        self._rotation_after_id = ""

    def _load_unlocked(self) -> list[dict[str, Any]]:
        data = read_json_file(
            self.file_path,
            name=self.file_path.name,
            default_factory=dict,
            expected_types=(dict, list),
        )
        if isinstance(data, dict):
            data = data.get("items")
        return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    @staticmethod
    def _secure_write(path: Path, payload: dict[str, Any]) -> None:
        """Atomically write JSON credential data with owner-only permissions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
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
            # Existing files may have been created by an older release with a
            # broader mode; retain the credential-store boundary on updates.
            os.chmod(path, 0o600)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _save_unlocked(self, items: list[dict[str, Any]]) -> None:
        payload = {"schema_version": XAI_CLI_OAUTH_SCHEMA_VERSION, "items": items}
        self._secure_write(self.file_path, payload)
        self._secure_write(self.file_path.with_suffix(self.file_path.suffix + ".bak"), payload)

    @staticmethod
    def _account_payload(item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("xAI CLI OAuth account must be an object")

        credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
        profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
        email = _first_text(item.get("email"), credentials.get("email"), auth.get("email"), profile.get("email"))
        subject = _first_text(
            item.get("subject"),
            item.get("sub"),
            credentials.get("subject"),
            credentials.get("sub"),
            auth.get("subject"),
            auth.get("sub"),
        )
        access_token = _first_text(item.get("access_token"), credentials.get("access_token"), auth.get("access_token"))
        refresh_token = _first_text(
            item.get("refresh_token"),
            credentials.get("refresh_token"),
            auth.get("refresh_token"),
        )
        id_token = _first_text(item.get("id_token"), credentials.get("id_token"), auth.get("id_token"))
        if not email and not subject:
            raise ValueError("xAI CLI OAuth account requires email or subject")
        if not refresh_token:
            raise ValueError("xAI CLI OAuth account requires refresh_token")

        expires_at = _first_text(
            item.get("expires_at"),
            item.get("expired"),
            credentials.get("expires_at"),
            credentials.get("expired"),
            auth.get("expires_at"),
            auth.get("expired"),
        )
        if not expires_at:
            expires_at = _expires_at_from_seconds(
                item.get("expires_in")
                if item.get("expires_in") is not None
                else credentials.get("expires_in", auth.get("expires_in"))
            )
        disabled = _as_bool(item.get("disabled", credentials.get("disabled", auth.get("disabled", False))))
        status_value = item.get("status")
        status = _normalize_status(status_value, default="disabled" if disabled else "active")
        metadata = _safe_metadata(item.get("metadata", item.get("profile", {})))
        return {
            "id": f"xai-cli-oauth-{uuid.uuid4().hex}",
            "provider": XAI_CLI_OAUTH_PROVIDER,
            "auth_kind": "oauth",
            "email": email,
            "subject": subject,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "token_type": _first_text(item.get("token_type"), credentials.get("token_type"), auth.get("token_type")) or "Bearer",
            "expires_at": expires_at,
            "last_refresh_at": _first_text(
                item.get("last_refresh_at"),
                item.get("last_refresh"),
                credentials.get("last_refresh_at"),
                credentials.get("last_refresh"),
            )
            or _now(),
            "status": status,
            "source_type": _clean_text(item.get("source_type")) or "oauth_import",
            "metadata": metadata if isinstance(metadata, dict) else {},
            "models": _model_ids(item.get("models", item.get("available_models", []))),
            "use_count": 0,
            "fail_count": 0,
            "last_used_at": "",
            "last_error": "",
            "created_at": _clean_text(item.get("created_at")) or _now(),
            "updated_at": _now(),
        }

    @staticmethod
    def _merge_account(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """Merge a refresh-token rotation while preserving stable identity fields."""
        merged = dict(existing)
        for key, value in incoming.items():
            if key in {"id", "created_at", "use_count", "fail_count", "last_used_at", "last_error"}:
                continue
            if key in {"email", "subject", "access_token", "id_token", "expires_at", "metadata", "models"} and not value:
                continue
            merged[key] = copy.deepcopy(value)
        merged["id"] = _clean_text(existing.get("id")) or incoming["id"]
        merged["provider"] = XAI_CLI_OAUTH_PROVIDER
        merged["auth_kind"] = "oauth"
        # Disabled is an operator decision.  Credential re-imports must not
        # silently put the account back into the rotation; explicit
        # ``set_disabled(..., False)`` is the recovery action.
        if _normalize_status(existing.get("status")) == "disabled" and incoming["status"] == "active":
            merged["status"] = "disabled"
        merged["created_at"] = _clean_text(existing.get("created_at")) or incoming["created_at"]
        merged["models"] = _model_ids(merged.get("models"))
        merged["use_count"] = _non_negative_int(existing.get("use_count"))
        merged["fail_count"] = _non_negative_int(existing.get("fail_count"))
        merged["last_used_at"] = _clean_text(existing.get("last_used_at"))
        merged["last_error"] = _safe_error(existing.get("last_error"), existing)
        merged["updated_at"] = _now()
        return merged

    def upsert(self, item: dict[str, Any]) -> dict[str, Any]:
        """Insert or update one OAuth credential without exposing it in the result."""
        incoming = self._account_payload(item)
        incoming_subject, incoming_email = _identity_key(incoming)
        with self._lock:
            items = self._load_unlocked()
            matched_indexes = [
                index
                for index, current in enumerate(items)
                if (
                    incoming_subject
                    and _clean_text(current.get("subject")) == incoming_subject
                )
                or (
                    incoming_email
                    and _clean_text(current.get("email")).lower() == incoming_email
                )
            ]
            if matched_indexes:
                first_index = matched_indexes[0]
                merged: dict[str, Any] = {}
                for index in matched_indexes:
                    merged.update(items[index])
                account = self._merge_account(merged, incoming)
                matched_set = set(matched_indexes)
                next_items = [current for index, current in enumerate(items) if index not in matched_set]
                next_items.insert(min(first_index, len(next_items)), account)
                added = False
            else:
                account = incoming
                next_items = [*items, account]
                added = True
            self._save_unlocked(next_items)
            return {"added": added, "count": len(next_items), "item": self._redacted(account)}

    @staticmethod
    def _redacted(item: dict[str, Any]) -> dict[str, Any]:
        """Project an account into a safe response suitable for API handlers."""
        return {
            "id": _clean_text(item.get("id")),
            "provider": XAI_CLI_OAUTH_PROVIDER,
            "auth_kind": "oauth",
            "email": _mask_email(item.get("email")),
            "subject_preview": _mask_subject(item.get("subject")),
            "has_access_token": bool(_clean_text(item.get("access_token"))),
            "has_refresh_token": bool(_clean_text(item.get("refresh_token"))),
            "has_id_token": bool(_clean_text(item.get("id_token"))),
            "token_type": _clean_text(item.get("token_type")) or "Bearer",
            "expires_at": _clean_text(item.get("expires_at")),
            "last_refresh_at": _clean_text(item.get("last_refresh_at")),
            "status": _normalize_status(item.get("status")),
            "source_type": _clean_text(item.get("source_type")) or "oauth_import",
            "metadata": _safe_metadata(item.get("metadata")) if isinstance(item.get("metadata"), dict) else {},
            "models": _model_ids(item.get("models")),
            "use_count": _non_negative_int(item.get("use_count")),
            "fail_count": _non_negative_int(item.get("fail_count")),
            "last_used_at": _clean_text(item.get("last_used_at")),
            "last_error": _safe_error(item.get("last_error"), item),
            "created_at": _clean_text(item.get("created_at")),
            "updated_at": _clean_text(item.get("updated_at")),
        }

    def list_accounts(
        self,
        *,
        redacted: bool = True,
        keyword: str = "",
        status: str = "all",
    ) -> list[dict[str, Any]]:
        status_filter = _clean_text(status).lower()
        if status_filter and status_filter != "all":
            _normalize_status(status_filter)
        needle = _clean_text(keyword).lower()
        with self._lock:
            result = []
            for current in self._load_unlocked():
                current_status = _normalize_status(current.get("status"))
                if status_filter and status_filter != "all" and current_status != status_filter:
                    continue
                if needle and not any(
                    needle in _clean_text(value).lower()
                    for value in (
                        current.get("id"),
                        current.get("email"),
                        current.get("subject"),
                        current_status,
                        current.get("source_type"),
                    )
                ):
                    continue
                result.append(copy.deepcopy(current))
        return [self._redacted(item) for item in result] if redacted else result

    def get_accounts_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Return unredacted credentials in requested ID order for provider code only."""
        ordered_ids = list(dict.fromkeys(_clean_text(value) for value in ids if _clean_text(value)))
        if not ordered_ids:
            return []
        with self._lock:
            by_id = {
                _clean_text(item.get("id")): item
                for item in self._load_unlocked()
                if _clean_text(item.get("id"))
            }
            return [copy.deepcopy(by_id[account_id]) for account_id in ordered_ids if account_id in by_id]

    def get(self, account_id: str, *, redacted: bool = False) -> dict[str, Any] | None:
        """Return one record by stable ID.

        ``redacted=False`` is for the in-process OAuth provider only.  API
        handlers should always pass ``redacted=True`` or use
        :meth:`list_accounts`.
        """
        records = self.get_accounts_by_ids([account_id])
        if not records:
            return None
        return self._redacted(records[0]) if redacted else records[0]

    def update_metadata(self, account_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Merge non-secret operational metadata into one stored account."""
        clean_id = _clean_text(account_id)
        safe_updates = _safe_metadata(updates)
        if not clean_id or not isinstance(safe_updates, dict):
            return None
        with self._lock:
            items = self._load_unlocked()
            for index, current in enumerate(items):
                if _clean_text(current.get("id")) != clean_id:
                    continue
                metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
                updated = dict(current)
                updated["metadata"] = {**metadata, **safe_updates}
                updated["updated_at"] = _now()
                items[index] = updated
                self._save_unlocked(items)
                return self._redacted(updated)
        return None

    def select_next_account(self, *, exclude_ids: list[str] | None = None) -> dict[str, Any] | None:
        """Return the next active account for internal provider use.

        This only advances an in-memory round-robin cursor and does not log or
        persist credentials.  Expired access tokens stay eligible because the
        caller can refresh them with the stored refresh token before dispatch.
        """
        excluded = {_clean_text(value) for value in (exclude_ids or []) if _clean_text(value)}
        with self._lock:
            items = self._load_unlocked()
            candidates = [
                (index, item)
                for index, item in enumerate(items)
                if _normalize_status(item.get("status")) == "active"
                and _clean_text(item.get("id")) not in excluded
                and _clean_text(item.get("refresh_token"))
            ]
            if not candidates:
                return None
            last_index = next(
                (
                    index
                    for index, item in enumerate(items)
                    if _clean_text(item.get("id")) == self._rotation_after_id
                ),
                -1,
            )
            _index, selected = next(
                (candidate for candidate in candidates if candidate[0] > last_index),
                candidates[0],
            )
            self._rotation_after_id = _clean_text(selected.get("id"))
            return copy.deepcopy(selected)

    def select(self, exclude_ids: list[str] | None = None) -> dict[str, Any] | None:
        """Short alias used by the xAI CLI provider's account-rotation loop."""
        return self.select_next_account(exclude_ids=exclude_ids)

    def update_tokens(
        self,
        account_id: str,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: str | None = None,
        email: str | None = None,
        *,
        id_token: str | None = None,
        expires_in: int | None = None,
        models: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Persist a successful OAuth refresh, including refresh-token rotation."""
        target_id = _clean_text(account_id)
        next_access_token = _clean_text(access_token)
        if not target_id:
            raise ValueError("account_id is required")
        if not next_access_token:
            raise ValueError("access_token is required")
        explicit_expiry = _clean_text(expires_at)
        calculated_expiry = _expires_at_from_seconds(expires_in) if expires_in is not None else ""
        with self._lock:
            items = self._load_unlocked()
            for item in items:
                if _clean_text(item.get("id")) != target_id:
                    continue
                item["access_token"] = next_access_token
                if refresh_token is not None and _clean_text(refresh_token):
                    item["refresh_token"] = _clean_text(refresh_token)
                if id_token is not None:
                    item["id_token"] = _clean_text(id_token)
                if explicit_expiry or calculated_expiry:
                    item["expires_at"] = explicit_expiry or calculated_expiry
                if email is not None and _clean_text(email):
                    item["email"] = _clean_text(email)
                if models is not None:
                    item["models"] = _model_ids(models)
                item["last_refresh_at"] = _now()
                item["updated_at"] = _now()
                self._save_unlocked(items)
                return self._redacted(item)
        return None

    def set_available_models(self, account_id: str, models: list[str]) -> dict[str, Any] | None:
        """Cache model IDs discovered by a successful CLI proxy probe.

        The method is deliberately local-only: discovery and any network call
        live in the provider transport, not in this storage component.
        """
        target_id = _clean_text(account_id)
        if not target_id:
            raise ValueError("account_id is required")
        with self._lock:
            items = self._load_unlocked()
            for item in items:
                if _clean_text(item.get("id")) != target_id:
                    continue
                item["models"] = _model_ids(models)
                item["updated_at"] = _now()
                self._save_unlocked(items)
                return self._redacted(item)
        return None

    def available_models(self) -> list[str]:
        """Return the union of cached model IDs for selectable OAuth accounts."""
        with self._lock:
            return list(
                dict.fromkeys(
                    model
                    for item in self._load_unlocked()
                    if _normalize_status(item.get("status")) == "active"
                    for model in _model_ids(item.get("models"))
                )
            )

    def record_result(self, account_id: str, success: bool, error: str = "") -> dict[str, Any] | None:
        """Record one local dispatch result without retaining token-bearing errors."""
        target_id = _clean_text(account_id)
        if not target_id:
            raise ValueError("account_id is required")
        with self._lock:
            items = self._load_unlocked()
            for item in items:
                if _clean_text(item.get("id")) != target_id:
                    continue
                item["last_used_at"] = _now()
                if success:
                    item["use_count"] = _non_negative_int(item.get("use_count")) + 1
                    item["last_error"] = ""
                else:
                    item["fail_count"] = _non_negative_int(item.get("fail_count")) + 1
                    item["last_error"] = _safe_error(error, item)
                item["updated_at"] = _now()
                self._save_unlocked(items)
                return self._redacted(item)
        return None

    def set_status(self, ids: list[str], status: str) -> dict[str, int]:
        """Set a lifecycle status by stable store IDs only."""
        target_ids = {_clean_text(value) for value in ids if _clean_text(value)}
        normalized_status = _normalize_status(status)
        if not target_ids:
            return {"updated": 0, "count": self.count()}
        with self._lock:
            items = self._load_unlocked()
            updated = 0
            for item in items:
                if _clean_text(item.get("id")) not in target_ids:
                    continue
                if _normalize_status(item.get("status")) != normalized_status:
                    item["status"] = normalized_status
                    item["updated_at"] = _now()
                    updated += 1
            if updated:
                self._save_unlocked(items)
            return {"updated": updated, "count": len(items)}

    def set_disabled(self, ids: str | list[str], disabled: bool) -> dict[str, int]:
        account_ids = [ids] if isinstance(ids, str) else ids
        return self.set_status(account_ids, "disabled" if disabled else "active")

    def delete_accounts(self, ids: list[str]) -> dict[str, int]:
        target_ids = {_clean_text(value) for value in ids if _clean_text(value)}
        if not target_ids:
            return {"removed": 0, "count": self.count()}
        with self._lock:
            items = self._load_unlocked()
            next_items = [item for item in items if _clean_text(item.get("id")) not in target_ids]
            removed = len(items) - len(next_items)
            if removed:
                self._save_unlocked(next_items)
            return {"removed": removed, "count": len(next_items)}

    def delete(self, account_id: str) -> bool:
        """Delete one account by its stable ID and return whether it existed."""
        return bool(self.delete_accounts([account_id]).get("removed"))

    def count(self) -> int:
        with self._lock:
            return len(self._load_unlocked())


xai_cli_oauth_store = XaiCliOAuthAccountStore()


__all__ = [
    "XAI_CLI_OAUTH_ACCOUNTS_FILE",
    "XAI_CLI_OAUTH_PROVIDER",
    "XAI_CLI_OAUTH_SCHEMA_VERSION",
    "XaiCliOAuthAccountStore",
    "xai_cli_oauth_store",
]
