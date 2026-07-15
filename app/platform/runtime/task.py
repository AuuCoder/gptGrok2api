"""Async batch task model + in-memory store for SSE progress streaming."""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional


class AsyncTask:
    """Tracks progress of an async batch operation with fan-out SSE support."""

    __slots__ = (
        "id", "total", "processed", "ok", "fail", "status",
        "warning", "result", "error", "created_at",
        "cancelled", "_queues", "_final_event",
    )

    def __init__(self, total: int) -> None:
        self.id = uuid.uuid4().hex
        self.total = int(total)
        self.processed = 0
        self.ok = 0
        self.fail = 0
        self.status = "running"
        self.warning: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.cancelled = False
        self._queues: List[asyncio.Queue] = []
        self._final_event: Optional[Dict[str, Any]] = None

    # -- Fan-out pub/sub ---------------------------------------------------

    def _publish(self, event: Dict[str, Any]) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.append(q)
        return q

    def detach(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    # -- Recording ---------------------------------------------------------

    def record(
        self,
        success: bool,
        *,
        item: Any = None,
        detail: Any = None,
        error: str = "",
    ) -> None:
        self.processed += 1
        if success:
            self.ok += 1
        else:
            self.fail += 1
        event: Dict[str, Any] = {
            "type": "progress",
            "task_id": self.id,
            "total": self.total,
            "processed": self.processed,
            "ok": self.ok,
            "fail": self.fail,
        }
        if item is not None:
            event["item"] = item
        if detail is not None:
            event["detail"] = detail
        if error:
            event["error"] = error
        self._publish(event)

    def finish(self, result: Dict[str, Any], *, warning: Optional[str] = None) -> None:
        self.status = "done"
        self.result = result
        self.warning = warning
        event = {
            "type": "done",
            "task_id": self.id,
            "total": self.total,
            "processed": self.processed,
            "ok": self.ok,
            "fail": self.fail,
            "warning": self.warning,
            "result": result,
        }
        self._final_event = event
        self._publish(event)

    def fail_task(self, msg: str) -> None:
        self.status = "error"
        self.error = msg
        event = {
            "type": "error",
            "task_id": self.id,
            "total": self.total,
            "processed": self.processed,
            "ok": self.ok,
            "fail": self.fail,
            "error": msg,
        }
        self._final_event = event
        self._publish(event)

    def cancel(self) -> None:
        self.cancelled = True

    def finish_cancelled(self) -> None:
        self.status = "cancelled"
        event = {
            "type": "cancelled",
            "task_id": self.id,
            "total": self.total,
            "processed": self.processed,
            "ok": self.ok,
            "fail": self.fail,
        }
        self._final_event = event
        self._publish(event)

    # -- Snapshots ---------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        return {
            "task_id": self.id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "ok": self.ok,
            "fail": self.fail,
            "warning": self.warning,
        }

    def final_event(self) -> Optional[Dict[str, Any]]:
        return self._final_event


# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------

_TASKS: Dict[str, AsyncTask] = {}


def create_task(total: int) -> AsyncTask:
    task = AsyncTask(total)
    _TASKS[task.id] = task
    return task


def get_task(task_id: str) -> Optional[AsyncTask]:
    return _TASKS.get(task_id)


async def expire_task(task_id: str, ttl_s: int = 300) -> None:
    await asyncio.sleep(ttl_s)
    _TASKS.pop(task_id, None)


__all__ = ["AsyncTask", "create_task", "get_task", "expire_task"]
