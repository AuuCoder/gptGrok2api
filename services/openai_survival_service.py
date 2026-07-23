from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.account_service import account_service
from services.config import DATA_DIR
from services.json_file import read_json_object, write_json_file
from utils.log import logger


DEFAULT_CONFIG = {
    "enabled": True,
    "interval_minutes": 60,
    "concurrency": 4,
    "refresh_codex_rt": True,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


class OpenAISurvivalService:
    """Periodic OpenAI account liveness tracking with conservative verdicts."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self._data_dir = Path(data_dir)
        self._config_path = self._data_dir / "openai_survival.json"
        self._lease_path = self._data_dir / "openai_survival.db"
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._runner: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None
        self._config = self._load_config()
        self._status: dict[str, Any] = {
            "running": False,
            "last_started_at": "",
            "last_finished_at": "",
            "last_error": "",
            "last_summary": {},
            "next_run_at": "",
        }
        self._initialize_lease_db()

    def _load_config(self) -> dict[str, Any]:
        raw = read_json_object(self._config_path, name="openai_survival.json")
        return self._normalize_config(raw)

    @staticmethod
    def _normalize_config(raw: object) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        enabled = source.get("enabled", DEFAULT_CONFIG["enabled"])
        refresh = source.get("refresh_codex_rt", DEFAULT_CONFIG["refresh_codex_rt"])
        if type(enabled) is not bool or type(refresh) is not bool:
            raise ValueError("enabled and refresh_codex_rt must be boolean")
        try:
            interval = int(source.get("interval_minutes", DEFAULT_CONFIG["interval_minutes"]))
            concurrency = int(source.get("concurrency", DEFAULT_CONFIG["concurrency"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_minutes and concurrency must be integers") from exc
        if not 15 <= interval <= 1440:
            raise ValueError("interval_minutes must be between 15 and 1440")
        if not 1 <= concurrency <= 8:
            raise ValueError("concurrency must be between 1 and 8")
        return {
            "enabled": enabled,
            "interval_minutes": interval,
            "concurrency": concurrency,
            "refresh_codex_rt": refresh,
        }

    def _initialize_lease_db(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._lease_path, timeout=15)) as connection:
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_lease (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
        try:
            os.chmod(self._lease_path, 0o600)
        except OSError:
            pass

    def _claim_run(self, ttl_seconds: int = 3600) -> bool:
        now = time.time()
        with closing(sqlite3.connect(self._lease_path, timeout=15)) as connection:
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM scheduler_lease WHERE name = 'openai_survival' AND expires_at <= ?",
                (now,),
            )
            try:
                connection.execute(
                    "INSERT INTO scheduler_lease(name, owner, expires_at) VALUES('openai_survival', ?, ?)",
                    (self._owner, now + ttl_seconds),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
            connection.commit()
        return True

    def _release_run(self) -> None:
        with closing(sqlite3.connect(self._lease_path, timeout=15)) as connection:
            connection.execute(
                "DELETE FROM scheduler_lease WHERE name = 'openai_survival' AND owner = ?",
                (self._owner,),
            )
            connection.commit()

    def _renew_run(self, ttl_seconds: int = 3600) -> bool:
        with closing(sqlite3.connect(self._lease_path, timeout=15)) as connection:
            cursor = connection.execute(
                """
                UPDATE scheduler_lease SET expires_at = ?
                WHERE name = 'openai_survival' AND owner = ?
                """,
                (time.time() + ttl_seconds, self._owner),
            )
            connection.commit()
            return cursor.rowcount == 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._config, **self._status}

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._config = self._normalize_config({**self._config, **dict(patch or {})})
            write_json_file(self._config_path, self._config)
            self._wake.set()
            return self.status()

    def start_scheduler(self, stop_event: threading.Event) -> threading.Thread:
        with self._lock:
            if self._scheduler and self._scheduler.is_alive():
                return self._scheduler
            self._scheduler = threading.Thread(
                target=self._scheduler_loop,
                args=(stop_event,),
                name="openai-survival-scheduler",
                daemon=True,
            )
            self._scheduler.start()
            return self._scheduler

    def stop_scheduler(self) -> None:
        self._wake.set()
        thread = self._scheduler
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _scheduler_loop(self, stop_event: threading.Event) -> None:
        # Give startup migrations and account loading time to settle.
        if stop_event.wait(60):
            return
        while not stop_event.is_set():
            config = self.status()
            if bool(config["enabled"]):
                self.run_now()
            interval = int(config["interval_minutes"]) * 60
            next_at = datetime.fromtimestamp(time.time() + interval, tz=timezone.utc).isoformat()
            with self._lock:
                self._status["next_run_at"] = next_at if bool(config["enabled"]) else ""
            self._wake.clear()
            deadline = time.monotonic() + interval
            while not stop_event.is_set() and time.monotonic() < deadline:
                if self._wake.wait(timeout=min(5, max(0.1, deadline - time.monotonic()))):
                    break

    def run_now(self) -> bool:
        with self._lock:
            if self._runner and self._runner.is_alive():
                return False
            self._runner = threading.Thread(
                target=self.run_once,
                name="openai-survival-run",
                daemon=True,
            )
            self._runner.start()
            return True

    def _probe_one(self, account: dict[str, Any], refresh_first: bool) -> dict[str, Any]:
        token = str(account.get("access_token") or "").strip()
        if not token:
            return {"status": "no_token", "error": "missing access_token", "tier": 0}
        active_token = token
        tier = 3
        before_refresh = str(account.get("last_token_refresh_at") or "")
        source_type = str(account.get("source_type") or "").strip().lower()
        if refresh_first and source_type != "chatgpt_web" and str(account.get("refresh_token") or "").strip():
            active_token = account_service.refresh_access_token(
                token,
                force=True,
                event="openai_survival",
            ) or token
            refreshed_account = account_service.get_account(active_token) or {}
            if str(refreshed_account.get("last_token_refresh_at") or "") != before_refresh:
                tier = 1

        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

            with OpenAIBackendAPI(active_token) as backend:
                remote = backend.get_user_info()
        except InvalidAccessTokenError as exc:
            return {
                "access_token": active_token,
                "status": "token_dead",
                "error": f"at_probe_failed: {exc}",
                "tier": tier,
            }
        except Exception as exc:
            message = str(exc or "")
            lowered = message.lower()
            if "account_deactivated" in lowered or "deleted or deactivated" in lowered:
                return {
                    "access_token": active_token,
                    "status": "account_deactivated",
                    "error": message[:300],
                    "tier": tier,
                }
            return {
                "access_token": active_token,
                "status": "error",
                "error": message[:300],
                "tier": tier,
            }

        claims = _jwt_claims(active_token)
        auth = claims.get("https://api.openai.com/auth") if isinstance(claims, dict) else {}
        auth = auth if isinstance(auth, dict) else {}
        plan_type = str(remote.get("type") or auth.get("chatgpt_plan_type") or "free").strip().lower()
        status = plan_type if plan_type in {"free", "plus", "pro", "team", "k12"} else "free"
        return {
            "access_token": active_token,
            "status": status,
            "plan_type": plan_type or "free",
            "active_until": str(auth.get("chatgpt_subscription_active_until") or ""),
            "active_start": str(auth.get("chatgpt_subscription_active_start") or ""),
            "tier": tier,
            "remote": remote,
        }

    def _persist_probe(self, original: dict[str, Any], result: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        token = str(result.get("access_token") or original.get("access_token") or "").strip()
        probe_status = str(result.get("status") or "error")
        previous_status = str(original.get("survival_status") or "").strip().lower()
        healthy_statuses = {"free", "plus", "pro", "team", "k12"}
        inconclusive_statuses = {"error", "token_dead", "no_token"}
        status = probe_status
        if probe_status in inconclusive_statuses and previous_status in healthy_statuses | {"account_deactivated"}:
            status = previous_status
        updates: dict[str, Any] = {
            "survival_status": status,
            "survival_last_probe_status": probe_status,
            "survival_last_checked_at": now_iso,
            "survival_check_tier": int(result.get("tier") or 0),
            "survival_check_error": str(result.get("error") or "")[:300] or None,
        }
        if probe_status in healthy_statuses:
            first = _parse_time(original.get("survival_first_confirmed_at")) or now
            observed_from = _parse_time(original.get("created_at")) or first
            updates.update(
                {
                    "survival_alive": True,
                    "survival_first_confirmed_at": first.isoformat(),
                    "survival_last_confirmed_at": now_iso,
                    "survival_observed_seconds": max(0, int((now - observed_from).total_seconds())),
                    "survival_plan_type": str(result.get("plan_type") or probe_status),
                    "survival_active_until": str(result.get("active_until") or ""),
                    "survival_active_start": str(result.get("active_start") or ""),
                }
            )
        elif probe_status == "account_deactivated":
            updates["survival_alive"] = False
            updates["survival_deactivated_at"] = now_iso
        elif previous_status not in healthy_statuses | {"account_deactivated"}:
            updates["survival_alive"] = None
        account_service.update_account(token, updates, quiet=True)
        return probe_status

    def run_once(self) -> dict[str, Any]:
        if not self._claim_run():
            return {"skipped": True, "reason": "another process is polling"}
        started = _now_iso()
        with self._lock:
            self._status.update({"running": True, "last_started_at": started, "last_error": ""})
            config = dict(self._config)
        try:
            accounts = account_service.list_accounts()
            counts: dict[str, int] = {}
            errors = 0
            with ThreadPoolExecutor(max_workers=int(config["concurrency"])) as executor:
                futures = {
                    executor.submit(self._probe_one, account, bool(config["refresh_codex_rt"])): account
                    for account in accounts
                    if str(account.get("access_token") or "").strip()
                }
                for future in as_completed(futures):
                    account = futures[future]
                    self._renew_run()
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"status": "error", "error": str(exc)[:300], "tier": 0}
                    status = self._persist_probe(account, result)
                    counts[status] = counts.get(status, 0) + 1
                    errors += int(status in {"error", "token_dead", "no_token"})
            summary = {
                "total": sum(counts.values()),
                "confirmed": sum(counts.get(key, 0) for key in ("free", "plus", "pro", "team", "k12")),
                "errors": errors,
                "statuses": counts,
            }
            with self._lock:
                self._status.update(
                    {
                        "running": False,
                        "last_finished_at": _now_iso(),
                        "last_summary": summary,
                        "last_error": "",
                    }
                )
            return summary
        except Exception as exc:
            with self._lock:
                self._status.update(
                    {
                        "running": False,
                        "last_finished_at": _now_iso(),
                        "last_error": f"{type(exc).__name__}: {exc}",
                    }
                )
            logger.error({"event": "openai_survival_poll_failed", "error": str(exc)})
            return {"error": str(exc)}
        finally:
            self._release_run()


openai_survival_service = OpenAISurvivalService()


__all__ = ["OpenAISurvivalService", "openai_survival_service"]
