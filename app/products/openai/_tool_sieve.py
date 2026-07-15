"""Tool Sieve — streaming tool-call detector and buffer.

Sits between the raw SSE text stream and the response formatter.
Accumulates chunks, detects when the model starts emitting a <tool_calls>
XML block, buffers the entire block, then parses it once complete.

Usage pattern (streaming path in chat.py):

    sieve = ToolSieve(tool_names)
    async for text_chunk in model_stream:
        safe_text, tool_calls = sieve.feed(text_chunk)
        if safe_text:
            yield make_stream_chunk(safe_text)
        if tool_calls:
            yield make_tool_call_chunk(tool_calls)
            break   # nothing more to send

    # After the stream ends, flush any remaining buffer
    tool_calls = sieve.flush()
    if tool_calls:
        yield make_tool_call_chunk(tool_calls)
"""

from __future__ import annotations

import re

from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall, parse_tool_calls


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

# We start buffering as soon as we see the opening of a <tool_calls> tag.
# Using a prefix match so we catch it even before the `>` arrives.
_OPEN_TAG_RE  = re.compile(r"<tool_calls[\s>]?", re.IGNORECASE)
_CLOSE_TAG    = "</tool_calls>"
_CLOSE_TAG_RE = re.compile(r"</tool_calls\s*>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# ToolSieve
# ---------------------------------------------------------------------------

class ToolSieve:
    """Stateful per-request sieve.

    Call :meth:`feed` for every text chunk from the model stream.
    Call :meth:`flush` once the stream ends to handle any buffered remainder.
    """

    __slots__ = ("_tool_names", "_buf", "_capturing", "_done")

    def __init__(self, tool_names: list[str]) -> None:
        self._tool_names = tool_names
        self._buf: str = ""
        self._capturing: bool = False
        self._done: bool = False          # already emitted tool calls once

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """Process one text chunk.

        Returns:
            (safe_text, tool_calls)
            - safe_text: text safe to forward immediately to the client
            - tool_calls: non-None (possibly empty list) once a complete
              XML block has been parsed; None while still accumulating
        """
        if self._done or not chunk:
            return chunk if not self._capturing else "", None

        if self._capturing:
            return self._feed_capturing(chunk)
        else:
            return self._feed_scanning(chunk)

    def flush(self) -> list[ParsedToolCall] | None:
        """Call after the stream ends.  Attempts to parse anything remaining
        in the buffer.  Returns None if no tool-call syntax was present."""
        if self._done or not self._buf:
            return None
        self._done = True
        result = parse_tool_calls(self._buf, self._tool_names)
        self._buf = ""
        if result.saw_tool_syntax:
            return result.calls
        return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _feed_scanning(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """Not yet in capture mode — look for the opening tag."""
        combined = self._buf + chunk
        self._buf = ""

        m = _OPEN_TAG_RE.search(combined)
        if m is None:
            # No opening tag; safe to forward.  Keep the last few chars in
            # the buffer in case the tag straddles a chunk boundary.
            safe, leftover = _split_at_boundary(combined, "<tool_calls")
            self._buf = leftover
            return safe, None

        # Opening tag found → emit everything before it, start capturing.
        # Then immediately attempt to consume the rest of this chunk as the
        # capture phase (the closing tag may already be present).
        safe_part = combined[: m.start()]
        self._buf = combined[m.start():]
        self._capturing = True
        cap_safe, calls = self._feed_capturing("")
        return safe_part + cap_safe, calls

    def _feed_capturing(self, chunk: str) -> tuple[str, list[ParsedToolCall] | None]:
        """In capture mode — accumulate until closing tag."""
        self._buf += chunk

        close_m = _CLOSE_TAG_RE.search(self._buf)
        if close_m is None:
            # Not complete yet — keep buffering, emit nothing
            return "", None

        # Complete block found
        xml_block = self._buf[: close_m.end()]
        self._buf = ""
        self._capturing = False
        self._done = True

        result = parse_tool_calls(xml_block, self._tool_names)
        return "", result.calls if result.saw_tool_syntax else None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _split_at_boundary(text: str, prefix: str) -> tuple[str, str]:
    """Split text so that any partial match of *prefix* at the end stays in
    the leftover buffer (to be checked again on the next chunk)."""
    # Check if any suffix of text could be the start of prefix
    for i in range(min(len(prefix) - 1, len(text)), 0, -1):
        if text.endswith(prefix[:i]):
            return text[: -i], text[-i:]
    return text, ""
