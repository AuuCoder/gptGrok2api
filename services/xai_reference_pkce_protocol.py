"""Adapter for testing the exact PKCE flow from a local reference checkout."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services.xai_reference_pkce_worker import RESULT_PREFIX


_REFERENCE_IMPORT_LOCK = threading.Lock()


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
        turnstile_config: dict[str, Any] | None = None,
    ) -> None:
        self.reference_dir = Path(reference_dir).expanduser().resolve()
        self.proxy = "" if str(proxy or "").strip() == "direct" else str(proxy or "").strip()
        self.timeout = max(30.0, float(timeout))
        self.progress = progress
        self.turnstile_config = dict(turnstile_config or {})

    def _emit(self, stage: str, message: str) -> None:
        if self.progress is not None:
            self.progress(stage, message)

    def _validate_reference(self) -> None:
        protocol_file = self.reference_dir / "xconsole_client" / "oauth_protocol.py"
        if not protocol_file.is_file():
            raise XaiReferencePkceProtocolError(
                "PKCE reference checkout is incomplete",
                stage="pkce_setup",
            )

    @staticmethod
    def _credential(result: Any) -> dict[str, Any]:
        token = result.token if isinstance(getattr(result, "token", None), dict) else {}
        credential = {
            "access_token": str(token.get("access_token") or ""),
            "refresh_token": str(token.get("refresh_token") or ""),
            "id_token": str(token.get("id_token") or ""),
            "expires_in": int(token.get("expires_in") or 21_600),
            "token_type": str(token.get("token_type") or "Bearer"),
        }
        if not credential["access_token"] or not credential["refresh_token"]:
            raise XaiReferencePkceProtocolError(
                "PKCE reference returned incomplete OAuth credentials",
                stage="pkce",
            )
        return credential

    def authorize_live_session(
        self,
        *,
        email: str,
        password: str,
        sso: str = "",
        session: Any,
        session_cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run the reference PKCE flow on the registration curl_cffi session."""
        self._validate_reference()
        self._emit("pkce", "复用注册会话执行 Authorization Code + PKCE")
        system_solver = None
        try:
            with _REFERENCE_IMPORT_LOCK:
                reference_path = str(self.reference_dir)
                if reference_path not in sys.path:
                    sys.path.insert(0, reference_path)
                from curl_cffi.requests.impersonate import BrowserType
                from xconsole_client.oauth_protocol import ProtocolOAuthClient

            supported = {item.value for item in BrowserType}
            impersonate = "chrome146" if "chrome146" in supported else "chrome136"
            client = ProtocolOAuthClient(
                proxy=self.proxy,
                impersonate=impersonate,
                debug=False,
            )
            if self.turnstile_config:
                from services.register.grok_protocol import TurnstileSolver

                solver_config = dict(self.turnstile_config)
                if self.proxy:
                    solver_config["proxy"] = self.proxy
                system_solver = TurnstileSolver(solver_config)

                class SystemSolverAdapter:
                    def solve_turnstile(
                        self,
                        website_url: str,
                        website_key: str,
                        *,
                        premium: bool = False,
                    ) -> str:
                        del premium
                        return system_solver.solve(
                            website_url=website_url,
                            sitekey=website_key,
                            action=str(solver_config.get("action") or ""),
                        )

                client.solver = SystemSolverAdapter()
            unused_session = client._s
            client._s = session
            try:
                unused_session.close()
            except Exception:
                pass
            cookies = dict(session_cookies or {})
            if sso:
                cookies.setdefault("sso", sso)
                cookies.setdefault("sso-rw", sso)
            with tempfile.TemporaryDirectory(prefix="xai-pkce-auth-") as auth_dir:
                result = client.login(
                    str(email or "").strip(),
                    str(password or "").strip(),
                    proxy=self.proxy,
                    cliproxyapi_auth_dir=auth_dir,
                    output_dir=None,
                    session_cookies=cookies or None,
                )
        except XaiReferencePkceProtocolError:
            raise
        except Exception as exc:
            detail = " ".join(str(exc or type(exc).__name__).split()) or type(exc).__name__
            raise XaiReferencePkceProtocolError(
                f"PKCE live-session authorization failed: {detail[:400]}",
                stage="pkce",
                retryable=True,
            ) from exc
        finally:
            if system_solver is not None:
                system_solver.close()
        credential = self._credential(result)
        self._emit("pkce", "Authorization Code + PKCE 已完成")
        return credential

    def authorize(
        self,
        *,
        email: str,
        password: str,
        sso: str = "",
        session_cookies: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        python = self.reference_dir / ".venv" / "bin" / "python"
        self._validate_reference()
        if not python.is_file():
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
