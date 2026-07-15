"""Bounded-concurrency batch processing utility."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def run_batch(
    items: Iterable[T],
    handler: Callable[[T], Awaitable[R]],
    *,
    concurrency: int = 10,
    pause_sec: float = 0.0,
    batch_size: int = 0,
) -> list[R]:
    """Process *items* with bounded concurrency.

    Args:
        items: Input sequence.
        handler: Async callable applied to each item.
        concurrency: Maximum simultaneous tasks.
        pause_sec: Sleep between batches (only when *batch_size* > 0).
        batch_size: Group size for inter-batch pauses; 0 = no grouping.

    Returns:
        Results in the same order as *items*.
    """
    item_list = list(items)
    if not item_list:
        return []

    worker_count = max(1, concurrency)

    async def _run_chunk(chunk: list[T]) -> list[Any]:
        results: list[Any] = [None] * len(chunk)
        next_index = 0

        async def _worker() -> None:
            nonlocal next_index
            while True:
                index = next_index
                if index >= len(chunk):
                    return
                next_index += 1
                results[index] = await handler(chunk[index])

        tasks = [asyncio.create_task(_worker()) for _ in range(min(worker_count, len(chunk)))]
        try:
            await asyncio.gather(*tasks)
        except Exception:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results

    if not batch_size or batch_size >= len(item_list):
        return list(await _run_chunk(item_list))

    results: list[Any] = []
    for start in range(0, len(item_list), batch_size):
        chunk = item_list[start : start + batch_size]
        chunk_results = await _run_chunk(chunk)
        results.extend(chunk_results)
        if pause_sec > 0 and start + batch_size < len(item_list):
            await asyncio.sleep(pause_sec)
    return results
