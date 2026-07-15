"""Tool call parser — extract structured tool calls from model text output.

Tries multiple formats in priority order:
  1. <tool_calls> XML  (canonical format we inject)
  2. JSON envelope {"tool_calls": [...]}
  3. JSON array  [{"name": ..., "input": ...}]
  4. Alternative XML tags (<function_call>, <invoke>)

Returns a list of ParsedToolCall dataclasses.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParsedToolCall:
    call_id: str
    name: str
    arguments: str          # always a JSON string

    @staticmethod
    def make(name: str, arguments: Any) -> "ParsedToolCall":
        call_id = f"call_{int(time.time() * 1000)}{os.urandom(3).hex()}"
        if isinstance(arguments, str):
            args_str = arguments
        else:
            try:
                args_str = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                args_str = "{}"
        return ParsedToolCall(call_id=call_id, name=name, arguments=args_str)


@dataclass
class ParseResult:
    calls: list[ParsedToolCall] = field(default_factory=list)
    saw_tool_syntax: bool = False   # detected XML/JSON envelope even if parsing failed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_tool_calls(
    text: str,
    available_tools: list[str] | None = None,
) -> ParseResult:
    """Parse tool calls from model-generated text.

    Args:
        text: Full or partial model output text.
        available_tools: If provided, only calls whose name appears in this
                         list are accepted (case-sensitive).
    """
    result = ParseResult()
    if not text or not text.strip():
        return result

    # Fast path: check whether tool-call syntax is present at all
    if not _has_tool_syntax(text):
        return result
    result.saw_tool_syntax = True

    # Try parsers in priority order
    calls = (
        _parse_xml_tool_calls(text)
        or _parse_json_envelope(text)
        or _parse_json_array(text)
        or _parse_alt_xml(text)
    )

    if calls and available_tools:
        calls = [c for c in calls if c.name in available_tools]

    result.calls = calls or []
    return result


# ---------------------------------------------------------------------------
# Syntax detection
# ---------------------------------------------------------------------------

_TOOL_SYNTAX_PATTERNS = re.compile(
    r"<tool_calls|<tool_call|<function_call|<invoke\s|"
    r'"tool_calls"\s*:|\btool_calls\b',
    re.IGNORECASE,
)

def _has_tool_syntax(text: str) -> bool:
    return bool(_TOOL_SYNTAX_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# Parser 1: <tool_calls> XML (canonical)
# ---------------------------------------------------------------------------

_XML_ROOT_RE    = re.compile(r"<tool_calls\s*>(.*?)</tool_calls\s*>", re.DOTALL | re.IGNORECASE)
_XML_CALL_RE    = re.compile(r"<tool_call\s*>(.*?)</tool_call\s*>",   re.DOTALL | re.IGNORECASE)
_XML_NAME_RE    = re.compile(r"<tool_name\s*>(.*?)</tool_name\s*>",   re.DOTALL | re.IGNORECASE)
_XML_PARAMS_RE  = re.compile(r"<parameters\s*>(.*?)</parameters\s*>", re.DOTALL | re.IGNORECASE)


def _parse_xml_tool_calls(text: str) -> list[ParsedToolCall]:
    root_m = _XML_ROOT_RE.search(text)
    if not root_m:
        return []
    calls: list[ParsedToolCall] = []
    for call_m in _XML_CALL_RE.finditer(root_m.group(1)):
        inner = call_m.group(1)
        name_m   = _XML_NAME_RE.search(inner)
        params_m = _XML_PARAMS_RE.search(inner)
        if not name_m:
            continue
        name   = name_m.group(1).strip()
        params = params_m.group(1).strip() if params_m else "{}"
        parsed_args = _parse_json_tolerant(params)
        if parsed_args is None:
            continue
        calls.append(ParsedToolCall.make(name, parsed_args))
    return calls


# ---------------------------------------------------------------------------
# Parser 2: {"tool_calls": [...]} JSON envelope
# ---------------------------------------------------------------------------

def _parse_json_envelope(text: str) -> list[ParsedToolCall]:
    # Only attempt if the text literally contains "tool_calls" key
    if '"tool_calls"' not in text:
        return []
    obj = _extract_outermost_json_obj(text)
    if not isinstance(obj, dict):
        return []
    raw_calls = obj.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    return _extract_from_call_list(raw_calls)


_JSON_DECODER = json.JSONDecoder()


def _extract_outermost_json_obj(text: str) -> Any:
    """Find and parse the first top-level JSON object in *text*.

    Uses JSONDecoder.raw_decode which handles the object boundary correctly
    without a manual bracket-depth tracker.
    """
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = _JSON_DECODER.raw_decode(text, start)
        return obj
    except (json.JSONDecodeError, ValueError):
        # Attempt repair on the substring from first '{' onward
        end = text.rfind("}") + 1
        return _try_repair_json(text[start:end]) if end > start else None


# ---------------------------------------------------------------------------
# Parser 3: bare JSON array [{"name":..., "input":...}]
# ---------------------------------------------------------------------------

_JSON_ARR_RE = re.compile(r"\[[\s\S]+\]", re.DOTALL)

def _parse_json_array(text: str) -> list[ParsedToolCall]:
    m = _JSON_ARR_RE.search(text)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(arr, list):
        return []
    return _extract_from_call_list(arr)


def _extract_from_call_list(items: list[Any]) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("tool_name") or "").strip()
        args = item.get("input") or item.get("arguments") or item.get("parameters") or {}
        if not name:
            continue
        calls.append(ParsedToolCall.make(name, args))
    return calls


# ---------------------------------------------------------------------------
# Parser 4: alternative XML tags (<function_call>, <invoke name="...">)
# ---------------------------------------------------------------------------

_FC_RE      = re.compile(r"<function_call\s*>(.*?)</function_call\s*>", re.DOTALL | re.IGNORECASE)
_INVOKE_RE  = re.compile(r'<invoke\s+name=["\']?(\w+)["\']?\s*>(.*?)</invoke\s*>',  re.DOTALL | re.IGNORECASE)
_FC_NAME_RE = re.compile(r"<name\s*>(.*?)</name\s*>",                  re.DOTALL | re.IGNORECASE)
_FC_ARGS_RE = re.compile(r"<arguments\s*>(.*?)</arguments\s*>",        re.DOTALL | re.IGNORECASE)


def _parse_alt_xml(text: str) -> list[ParsedToolCall]:
    calls: list[ParsedToolCall] = []

    # <function_call><name>...</name><arguments>...</arguments></function_call>
    for m in _FC_RE.finditer(text):
        inner  = m.group(1)
        name_m = _FC_NAME_RE.search(inner)
        args_m = _FC_ARGS_RE.search(inner)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        args = _parse_json_tolerant(args_m.group(1).strip() if args_m else "{}")
        if args is None:
            continue
        calls.append(ParsedToolCall.make(name, args))

    # <invoke name="tool_name">...</invoke>
    for m in _INVOKE_RE.finditer(text):
        name  = m.group(1).strip()
        inner = m.group(2)
        args  = _parse_json_tolerant(inner.strip())
        if args is None:
            args = {}
        calls.append(ParsedToolCall.make(name, args))

    return calls


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _parse_json_tolerant(s: str) -> Any:
    """Try to parse JSON; attempt light repair on failure."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        repaired = _try_repair_json(s)
        return repaired


def _try_repair_json(s: str) -> Any:
    """Very lightweight JSON repair: fix unescaped newlines inside strings."""
    try:
        # Replace literal newlines inside strings (common model output issue)
        fixed = re.sub(r'(?<!\\)\n', r'\\n', s)
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        return None
