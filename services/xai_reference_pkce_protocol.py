"""Adapter for testing the exact PKCE flow from a local reference checkout."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services.xai_reference_pkce_worker import RESULT_PREFIX


class XaiReferencePkceProtocolError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "pkce", retryable: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


class XaiReferencePkceProtocol:
    def __init__(
        self,
        reference_dir: str,
        *,
        proxy: str = "",
        timeout: float = 240.0,
        progress: Callable[[str, str], None] | None = None,
    ) -> None:
        self.reference_dir = Path(reference_dir).expanduser().resolve()
        self.proxy = "" if str(proxy or "").strip() == "direct" else str(proxy or "").strip()
        self.timeout = max(30.0, float(timeout))
        self.progress = progress

    def _emit(self, stage: str, message: str) -> None:
        if self.progress is not None:
            self.progress(stage, message)

    def authorize(
        self,
        *,
        email: str,
        password: str,
        sso: str = "",
        session_cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        python = self.reference_dir / ".venv" / "bin" / "python"
        protocol_file = self.reference_dir / "xconsole_client" / "oauth_protocol.py"
        if not python.is_file() or not protocol_file.is_file():
            raise XaiReferencePkceProtocolError(
                "PKCE reference checkout or its .venv is incomplete",
                stage="pkce_setup",
            )

        worker = Path(__file__).with_name("xai_reference_pkce_worker.py")
        payload = json.dumps(
            {
                "email": str(email or "").strip(),
                "password": str(password or "").strip(),
                "sso": str(sso or "").strip(),
                "session_cookies": dict(session_cookies or {}),
                "proxy": self.proxy,
            },
            ensure_ascii=True,
        )
        self._emit("pkce", "使用参考实现执行 Authorization Code + PKCE")
        try:
            completed = subprocess.run(
                [str(python), str(worker), "--reference-dir", str(self.reference_dir)],
                input=payload,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise XaiReferencePkceProtocolError(
                "PKCE reference authorization timed out",
                stage="pkce",
                retryable=True,
            ) from exc

        result_line = next(
            (line for line in reversed(completed.stdout.splitlines()) if line.startswith(RESULT_PREFIX)),
            "",
        )
        if completed.returncode != 0 or not result_line:
            detail = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "no result returned"
            raise XaiReferencePkceProtocolError(
                f"PKCE reference authorization failed: {detail[:300]}",
                stage="pkce",
                retryable=True,
            )
        try:
            credential = json.loads(result_line[len(RESULT_PREFIX):])
        except json.JSONDecodeError as exc:
            raise XaiReferencePkceProtocolError("PKCE reference returned invalid JSON", stage="pkce") from exc
        if not isinstance(credential, dict) or not credential.get("access_token") or not credential.get("refresh_token"):
            raise XaiReferencePkceProtocolError("PKCE reference returned incomplete OAuth credentials", stage="pkce")
        self._emit("pkce", "Authorization Code + PKCE 已完成")
        return credential


__all__ = ["XaiReferencePkceProtocol", "XaiReferencePkceProtocolError"]
