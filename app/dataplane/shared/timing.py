"""Dataplane clock — re-exports from platform.runtime.clock.

Provides a canonical import path for hot-path code that needs wall-clock
timestamps without pulling in the full platform package.
"""

from app.platform.runtime.clock import now_ms, now_s, ms_to_s, s_to_ms

__all__ = ["now_ms", "now_s", "ms_to_s", "s_to_ms"]
