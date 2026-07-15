"""In-process request cancellation flags.

This is intentionally lightweight: it does not try to kill Python threads.
Long-running request paths poll the flag at safe boundaries and exit by
raising a normal exception, allowing account/egress cleanup to run.
"""

from __future__ import annotations

import time
from threading import Lock


class RequestCancelledError(RuntimeError):
    """Raised when an in-flight request is cancelled from the admin UI."""


class RequestCancelService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled: dict[str, float] = {}

    def cancel(self, call_id: str) -> bool:
        call_id = str(call_id or "").strip()
        if not call_id:
            return False
        with self._lock:
            self._cancelled[call_id] = time.time()
        return True

    def is_cancelled(self, call_id: str) -> bool:
        call_id = str(call_id or "").strip()
        if not call_id:
            return False
        with self._lock:
            return call_id in self._cancelled

    def raise_if_cancelled(self, call_id: str) -> None:
        if self.is_cancelled(call_id):
            raise RequestCancelledError("request cancelled by administrator")

    def clear(self, call_id: str) -> None:
        call_id = str(call_id or "").strip()
        if not call_id:
            return
        with self._lock:
            self._cancelled.pop(call_id, None)


request_cancel_service = RequestCancelService()
