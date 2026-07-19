"""CLIProxyAPI integration for browsing remote auth files and importing selected tokens."""

from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from curl_cffi.requests import Session

from services.account_service import account_service
from services.config import DATA_DIR
from services.json_file import read_json_file, write_json_file
from services.proxy_service import proxy_settings


CPA_CONFIG_FILE = DATA_DIR / "cpa_config.json"
DEFAULT_CPA_DELIVERY_CONFIG = {"enabled": False, "pool_id": ""}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_import_job(raw: object, *, fail_unfinished: bool) -> dict | None:
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "failed").strip() or "failed"
    if fail_unfinished and status in {"pending", "running"}:
        status = "failed"
    return {
        "job_id": str(raw.get("job_id") or uuid.uuid4().hex).strip(),
        "status": status,
        "created_at": str(raw.get("created_at") or _now_iso()).strip() or _now_iso(),
        "updated_at": str(raw.get("updated_at") or raw.get("created_at") or _now_iso()).strip() or _now_iso(),
        "total": int(raw.get("total") or 0),
        "completed": int(raw.get("completed") or 0),
        "added": int(raw.get("added") or 0),
        "skipped": int(raw.get("skipped") or 0),
        "refreshed": int(raw.get("refreshed") or 0),
        "failed": int(raw.get("failed") or 0),
        "errors": raw.get("errors") if isinstance(raw.get("errors"), list) else [],
    }


def _normalize_pool(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or _new_id()).strip(),
        "name": str(raw.get("name") or "").strip(),
        "base_url": str(raw.get("base_url") or "").strip(),
        "secret_key": str(raw.get("secret_key") or "").strip(),
        "import_job": _normalize_import_job(raw.get("import_job"), fail_unfinished=True),
    }


def _management_headers(secret_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret_key}",
        "Accept": "application/json",
    }


def normalize_cpa_delivery_config(raw: object) -> dict[str, object]:
    source = raw if isinstance(raw, dict) else {}
    enabled = source.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    return {
        "enabled": bool(enabled),
        "pool_id": str(source.get("pool_id") or "").strip(),
    }


class CPAConfig:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = Lock()
        self._pools: list[dict] = self._load()

    def _load(self) -> list[dict]:
        raw = read_json_file(
            self._store_file,
            name="cpa_config.json",
            default_factory=list,
            expected_types=(dict, list),
        )
        if isinstance(raw, dict) and "base_url" in raw:
            pool = _normalize_pool(raw)
            return [pool] if pool["base_url"] else []
        if isinstance(raw, list):
            return [_normalize_pool(item) for item in raw if isinstance(item, dict)]
        return []

    def _save(self) -> None:
        write_json_file(self._store_file, self._pools)

    def list_pools(self) -> list[dict]:
        with self._lock:
            return [dict(pool) for pool in self._pools]

    def get_pool(self, pool_id: str) -> dict | None:
        with self._lock:
            for pool in self._pools:
                if pool["id"] == pool_id:
                    return dict(pool)
        return None

    def add_pool(self, name: str, base_url: str, secret_key: str) -> dict:
        pool = _normalize_pool({"id": _new_id(), "name": name, "base_url": base_url, "secret_key": secret_key})
        with self._lock:
            self._pools.append(pool)
            self._save()
        return dict(pool)

    def update_pool(self, pool_id: str, updates: dict) -> dict | None:
        with self._lock:
            for index, pool in enumerate(self._pools):
                if pool["id"] != pool_id:
                    continue
                merged = {**pool, **{key: value for key, value in updates.items() if value is not None}, "id": pool_id}
                self._pools[index] = _normalize_pool(merged)
                self._save()
                return dict(self._pools[index])
        return None

    def delete_pool(self, pool_id: str) -> bool:
        with self._lock:
            before = len(self._pools)
            self._pools = [pool for pool in self._pools if pool["id"] != pool_id]
            if len(self._pools) < before:
                self._save()
                return True
        return False

    def set_import_job(self, pool_id: str, import_job: dict | None) -> dict | None:
        with self._lock:
            for index, pool in enumerate(self._pools):
                if pool["id"] != pool_id:
                    continue
                next_pool = dict(pool)
                next_pool["import_job"] = _normalize_import_job(import_job, fail_unfinished=False)
                self._pools[index] = next_pool
                self._save()
                return dict(next_pool)
        return None

    def get_import_job(self, pool_id: str) -> dict | None:
        with self._lock:
            for pool in self._pools:
                if pool["id"] == pool_id:
                    job = pool.get("import_job")
                    return dict(job) if isinstance(job, dict) else None
        return None


def list_remote_files(pool: dict) -> list[dict]:
    base_url = str(pool.get("base_url") or "").strip()
    secret_key = str(pool.get("secret_key") or "").strip()
    if not base_url or not secret_key:
        return []

    url = f"{base_url.rstrip('/')}/v0/management/auth-files"
    session = Session(**proxy_settings.build_session_kwargs(verify=True))
    try:
        response = session.get(url, headers=_management_headers(secret_key), timeout=30)
        if not response.ok:
            raise RuntimeError(f"remote list failed: HTTP {response.status_code}")
        payload = response.json()
    finally:
        session.close()

    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise RuntimeError("remote list payload is invalid")

    items: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        email = str(item.get("email") or item.get("account") or "").strip()
        if not name:
            continue
        items.append({"name": name, "email": email})
    return items


def fetch_remote_access_token(pool: dict, file_name: str) -> tuple[str | None, str | None]:
    base_url = str(pool.get("base_url") or "").strip()
    secret_key = str(pool.get("secret_key") or "").strip()
    file_name = str(file_name or "").strip()
    if not base_url or not secret_key or not file_name:
        return None, "invalid request"

    url = f"{base_url.rstrip('/')}/v0/management/auth-files/download"
    session = Session(**proxy_settings.build_session_kwargs(verify=True))
    try:
        response = session.get(url, headers=_management_headers(secret_key), params={"name": file_name}, timeout=30)
        if not response.ok:
            return None, f"HTTP {response.status_code}"
        payload = response.json()
    except Exception as exc:
        return None, str(exc)
    finally:
        session.close()

    if not isinstance(payload, dict):
        return None, "invalid payload"

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return None, "missing access_token"
    return access_token, None


class CPAUploadError(RuntimeError):
    """A credential-free error raised while uploading one CPA auth file."""


def _xai_auth_file_name(account: dict) -> str:
    identity = str(account.get("email") or account.get("subject") or "oauth").strip()
    safe_identity = re.sub(r"[^A-Za-z0-9@._-]+", "-", identity).strip("-._")[:120] or "oauth"
    return f"xai-{safe_identity}.json"


def _xai_auth_file_payload(account: dict) -> dict:
    access_token = str(account.get("access_token") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise CPAUploadError("本地 xAI 账号缺少 OAuth 凭据，无法上传")
    payload = {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": str(account.get("token_type") or "Bearer").strip() or "Bearer",
        "last_refresh": str(account.get("last_refresh_at") or _now_iso()).strip(),
    }
    for source_key, target_key in (
        ("id_token", "id_token"),
        ("email", "email"),
        ("subject", "sub"),
        ("expires_at", "expired"),
    ):
        value = str(account.get(source_key) or "").strip()
        if value:
            payload[target_key] = value
    return payload


def _codex_auth_file_name(account: dict) -> str:
    identity = str(account.get("email") or account.get("account_id") or "oauth").strip()
    safe_identity = re.sub(r"[^A-Za-z0-9@._-]+", "-", identity).strip("-._")[:120] or "oauth"
    return f"codex-{safe_identity}.json"


def _codex_auth_file_payload(account: dict) -> dict:
    payload = account_service.build_export_item(account)
    if payload is None:
        raise CPAUploadError("本地 OpenAI 账号缺少完整 OAuth 凭据，无法上传")
    payload.pop("password", None)
    payload["type"] = "codex"
    return payload


def _upload_auth_file(pool: dict, file_name: str, payload: dict, provider_label: str) -> dict:
    pool_id = str(pool.get("id") or "").strip()
    base_url = str(pool.get("base_url") or "").strip()
    secret_key = str(pool.get("secret_key") or "").strip()
    if not pool_id or not base_url or not secret_key:
        raise CPAUploadError("CPA 连接不完整，请重新保存连接")

    session = Session(**proxy_settings.build_session_kwargs(verify=True))
    try:
        response = session.post(
            f"{base_url.rstrip('/')}/v0/management/auth-files",
            headers=_management_headers(secret_key),
            files={
                "file": (
                    file_name,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json",
                )
            },
            timeout=30,
        )
        if not response.ok:
            raise CPAUploadError(f"CPA 上传 {provider_label} OAuth 文件失败（HTTP {response.status_code}）")
    except CPAUploadError:
        raise
    except Exception as exc:
        raise CPAUploadError(f"CPA 上传 {provider_label} OAuth 文件请求失败") from exc
    finally:
        session.close()

    return {
        "ok": True,
        "pool_id": pool_id,
        "pool_name": str(pool.get("name") or "").strip() or base_url,
        "file_name": file_name,
    }


def upload_xai_oauth_file(pool: dict, account: dict) -> dict:
    """Upload one xAI OAuth JSON file through CLIProxyAPI's management API."""
    file_name = _xai_auth_file_name(account)
    payload = _xai_auth_file_payload(account)
    return _upload_auth_file(pool, file_name, payload, "xAI")


def upload_openai_oauth_file(pool: dict, account: dict) -> dict:
    """Upload one OpenAI Codex OAuth JSON file through CLIProxyAPI's management API."""
    file_name = _codex_auth_file_name(account)
    payload = _codex_auth_file_payload(account)
    return _upload_auth_file(pool, file_name, payload, "OpenAI")


class CPAImportService:
    def __init__(self, cpa_config: CPAConfig):
        self._config = cpa_config

    def start_import(self, pool: dict, selected_files: list[str]) -> dict:
        names = list(dict.fromkeys(str(name or "").strip() for name in selected_files if str(name or "").strip()))
        if not names:
            raise ValueError("selected files is required")

        pool_id = str(pool.get("id") or "").strip()
        job = {
            "job_id": uuid.uuid4().hex,
            "status": "pending",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "total": len(names),
            "completed": 0,
            "added": 0,
            "skipped": 0,
            "refreshed": 0,
            "failed": 0,
            "errors": [],
        }
        saved_pool = self._config.set_import_job(pool_id, job)
        if saved_pool is None:
            raise ValueError("pool not found")

        thread = threading.Thread(
            target=self._run_import,
            args=(pool_id, pool, names),
            name=f"cpa-import-{pool_id}",
            daemon=True,
        )
        thread.start()
        return dict(saved_pool.get("import_job") or job)

    def _update_job(self, pool_id: str, **updates) -> dict | None:
        current = self._config.get_import_job(pool_id)
        if current is None:
            return None
        next_job = {**current, **updates, "updated_at": _now_iso()}
        pool = self._config.set_import_job(pool_id, next_job)
        if pool is None:
            return None
        job = pool.get("import_job")
        return dict(job) if isinstance(job, dict) else None

    def _append_error(self, pool_id: str, file_name: str, message: str) -> None:
        current = self._config.get_import_job(pool_id)
        if current is None:
            return
        errors = list(current.get("errors") or [])
        errors.append({"name": file_name, "error": message})
        self._update_job(pool_id, errors=errors, failed=len(errors))

    def _run_import(self, pool_id: str, pool: dict, names: list[str]) -> None:
        self._update_job(pool_id, status="running")

        tokens: list[str] = []
        max_workers = min(16, max(1, len(names)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(fetch_remote_access_token, pool, name): name for name in names}
            for future in as_completed(future_map):
                file_name = future_map[future]
                try:
                    token, error = future.result()
                except Exception as exc:
                    token, error = None, str(exc)

                if token:
                    tokens.append(token)
                else:
                    self._append_error(pool_id, file_name, error or "unknown error")

                current = self._config.get_import_job(pool_id) or {}
                failed = len(current.get("errors") or [])
                self._update_job(pool_id, completed=int(current.get("completed") or 0) + 1, failed=failed)

        if not tokens:
            current = self._config.get_import_job(pool_id) or {}
            self._update_job(
                pool_id,
                status="failed",
                completed=int(current.get("total") or 0),
                failed=len(current.get("errors") or []),
            )
            return

        add_result = account_service.add_accounts(tokens, source_type="codex")
        refresh_result = account_service.refresh_accounts(tokens)
        current = self._config.get_import_job(pool_id) or {}
        self._update_job(
            pool_id,
            status="completed",
            completed=len(names),
            added=int(add_result.get("added") or 0),
            skipped=int(add_result.get("skipped") or 0),
            refreshed=int(refresh_result.get("refreshed") or 0),
            failed=len(current.get("errors") or []),
        )


cpa_config = CPAConfig(CPA_CONFIG_FILE)
cpa_import_service = CPAImportService(cpa_config)
