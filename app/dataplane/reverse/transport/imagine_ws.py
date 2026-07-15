"""Imagine WebSocket reverse transport.

Protocol (per round):
  Client → reset message
  Client → prompt message
  Server → N × (start_stage json → image frames → completed json)
  Server → [may close WS, or keep open for next round]

One WS connection is reused across rounds until the server closes it or n
images have been collected. This avoids a TLS handshake per round when
requesting more images than a single round produces (speed=6, quality=4).
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

import aiohttp
import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind, ProxyScope, RequestKind
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.reverse.transport._proxy_feedback import upstream_feedback
from app.dataplane.proxy.adapters.headers import build_ws_headers
from app.dataplane.reverse.protocol.xai_image import (
    WS_IMAGINE_URL,
    build_reset_message,
    build_request_message,
    parse_image_url,
    parse_json_frame,
)
from .websocket import WebSocketClient

_client = WebSocketClient()
_INTER_ROUND_WAIT_S = 2.0


# ---------------------------------------------------------------------------
# Slot state
# ---------------------------------------------------------------------------

@dataclass
class _Slot:
    """Tracks one in-flight image generation slot."""
    image_id:  str
    order:     int
    width:     int
    height:    int
    last_blob: str  = field(default="", repr=False)
    last_url:  str  = ""
    done:      bool = False
    progress:  int  = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _final_event(slot: _Slot, r_rated: bool = False) -> dict[str, Any]:
    return {
        "type":      "image",
        "image_id":  slot.image_id,
        "order":     slot.order,
        "stage":     "final",
        "blob":      slot.last_blob,
        "url":       slot.last_url,
        "width":     slot.width,
        "height":    slot.height,
        "is_final":  True,
        "moderated": False,
        "r_rated":   r_rated,
    }


async def _probe_ws_closed(ws: aiohttp.ClientWebSocketResponse, wait_s: float) -> bool:
    """Wait up to *wait_s* seconds for a CLOSE frame.

    Returns True if the server closed the connection, False if the connection
    appears to still be alive (timeout expired with no CLOSE received).
    """
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=wait_s)
        return msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Single-round generator
# ---------------------------------------------------------------------------

async def _stream_round(
    ws:                  aiohttp.ClientWebSocketResponse,
    prompt:              str,
    *,
    aspect_ratio:        str,
    enable_nsfw:         bool,
    enable_pro:          bool,
    needed:              int,
    stream_timeout_s:    float,
    round_timeout_s:     float,
    inter_round_wait_s:  float,
) -> AsyncGenerator[dict[str, Any], None]:
    """Drive one round of image generation on an already-open WS.

    Sends reset + prompt, then processes frames until all slots are completed
    or the WS closes.  Always yields a ``{type: "_meta", ws_closed: bool}``
    sentinel as the very last item so the caller knows whether to reconnect.
    """
    request_id = str(uuid.uuid4())
    try:
        await ws.send_json(build_reset_message())
        await ws.send_json(build_request_message(
            request_id, prompt, aspect_ratio, enable_nsfw, enable_pro,
        ))
    except Exception as exc:
        yield {"type": "error", "error_code": "send_failed", "error": str(exc)}
        yield {"type": "_meta", "ws_closed": True}
        return

    slots:           dict[str, _Slot] = {}
    round_completed: int               = 0
    round_start                        = time.monotonic()

    while True:
        elapsed = time.monotonic() - round_start
        if elapsed >= round_timeout_s:
            logger.warning("imagine round timed out: elapsed_s={:.1f}", elapsed)
            for slot in slots.values():
                if not slot.done:
                    if slot.last_blob:
                        yield _final_event(slot)
                    else:
                        yield {
                            "type":       "error",
                            "error_code": "slot_incomplete",
                            "error":      f"slot {slot.image_id[:8]} timed out",
                        }
            yield {"type": "_meta", "ws_closed": False}
            return

        recv_timeout = min(stream_timeout_s, round_timeout_s - elapsed)
        try:
            ws_msg = await asyncio.wait_for(ws.receive(), timeout=recv_timeout)
        except asyncio.TimeoutError:
            # No frame arrived — check if all known slots are already done.
            if slots and all(s.done for s in slots.values()):
                ws_closed = await _probe_ws_closed(ws, inter_round_wait_s)
                yield {"type": "_meta", "ws_closed": ws_closed}
                return
            continue

        # ── TEXT frames ──────────────────────────────────────────────────────
        if ws_msg.type == aiohttp.WSMsgType.TEXT:
            try:
                msg = orjson.loads(ws_msg.data)
            except Exception:
                continue

            msg_type = msg.get("type")

            # JSON control frames (start_stage / completed)
            if msg_type == "json":
                parsed = parse_json_frame(msg)
                if parsed is None:
                    continue

                if parsed["status"] == "start_stage":
                    iid = parsed["image_id"]
                    slots[iid] = _Slot(
                        image_id = iid,
                        order    = parsed["order"],
                        width    = parsed["width"],
                        height   = parsed["height"],
                    )
                    logger.debug(
                        "imagine slot started: image_id={} order={} width={} height={}",
                        iid[:8], parsed["order"], parsed["width"], parsed["height"],
                    )
                    yield {
                        "type": "progress",
                        "image_id": iid,
                        "order": parsed["order"],
                        "progress": 10,
                    }

                elif parsed["status"] == "completed":
                    iid  = parsed["image_id"]
                    slot = slots.get(iid)
                    if slot is None or slot.done:
                        continue

                    slot.done = True

                    if parsed["moderated"]:
                        logger.warning("imagine slot moderated: image_id={}", iid[:8])
                        yield {"type": "moderated", "image_id": iid, "order": slot.order}
                    else:
                        logger.debug("imagine slot completed: image_id={} order={}", iid[:8], slot.order)
                        yield _final_event(slot, r_rated=parsed["r_rated"])
                        round_completed += 1

                    all_done = slots and all(s.done for s in slots.values())
                    if all_done:
                        ws_closed = await _probe_ws_closed(ws, inter_round_wait_s)
                        yield {"type": "_meta", "ws_closed": ws_closed}
                        return

                    if round_completed >= needed:
                        # Have enough this round; leave remaining slots in flight.
                        yield {"type": "_meta", "ws_closed": False}
                        return

            # Image blob frames (intermediate previews)
            elif msg_type == "image":
                url   = msg.get("url", "")
                blob  = msg.get("blob", "")
                iid, _ext = parse_image_url(url)
                slot  = slots.get(iid)
                if slot and not slot.done:
                    slot.last_blob = blob
                    slot.last_url  = url
                    progress = msg.get("percentage_complete")
                    try:
                        parsed_progress = int(float(progress)) if progress is not None else 50
                    except (TypeError, ValueError):
                        parsed_progress = 50
                    parsed_progress = max(10, min(99, parsed_progress))
                    if parsed_progress > slot.progress:
                        slot.progress = parsed_progress
                        yield {
                            "type": "progress",
                            "image_id": iid,
                            "order": slot.order,
                            "progress": parsed_progress,
                        }

            # Server-side error
            elif msg_type == "error":
                err_code = msg.get("err_code") or "upstream_error"
                err_msg  = msg.get("err_msg")  or str(msg)
                logger.warning("imagine websocket server error: code={} message={}", err_code, err_msg)
                yield {"type": "error", "error_code": err_code, "error": err_msg}
                yield {"type": "_meta", "ws_closed": True}
                return

        # ── WS closed / error ────────────────────────────────────────────────
        elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            # Yield best-effort finals for any slots that received a blob but
            # never got a completed frame.
            for slot in slots.values():
                if not slot.done:
                    if slot.last_blob:
                        logger.debug(
                            "imagine websocket closed with best-effort final: image_id={}",
                            slot.image_id[:8],
                        )
                        yield _final_event(slot)
                    else:
                        logger.warning(
                            "imagine websocket closed before image data arrived: image_id={}",
                            slot.image_id[:8],
                        )
            yield {"type": "_meta", "ws_closed": True}
            return


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def stream_images(
    token:        str,
    prompt:       str,
    *,
    aspect_ratio: str  = "2:3",
    n:            int  = 1,
    enable_nsfw:  bool = True,
    enable_pro:   bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream image events, collecting *n* final images.

    Reuses a single WS connection across multiple rounds when the server keeps
    the connection open.  Reconnects transparently if the server closes it.

    Yields:
        ``{type: "image",      is_final: True,  ...}``  — final image per slot
        ``{type: "moderated",  ...}``                   — censored slot
        ``{type: "error",      ...}``                   — fatal error (stops iteration)
    """
    cfg                = get_config()
    timeout_s          = cfg.get_float("image.timeout",            120.0)
    stream_timeout_s   = cfg.get_float("image.stream_timeout",      10.0)
    inter_round_wait_s = _INTER_ROUND_WAIT_S

    collected = 0

    while collected < n:
        needed = n - collected

        # ── Establish connection ──────────────────────────────────────────────
        proxy   = await get_proxy_runtime()
        lease   = await proxy.acquire(scope=ProxyScope.APP, kind=RequestKind.WEBSOCKET)
        headers = build_ws_headers(token=token, lease=lease)

        try:
            conn = await _client.connect(
                WS_IMAGINE_URL,
                headers   = headers,
                timeout   = timeout_s,
                ws_kwargs = {"heartbeat": 20, "receive_timeout": stream_timeout_s},
                lease     = lease,
            )
        except Exception as exc:
            status = getattr(exc, "status", None)
            logger.error("imagine websocket connect failed: error={}", exc)
            from app.platform.errors import UpstreamError as _UE
            fb = upstream_feedback(_UE("connect failed", status=status)) \
                if status else ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR)
            await proxy.feedback(lease, fb)
            yield {
                "type":       "error",
                "error_code": "rate_limit_exceeded" if status == 429 else "connection_failed",
                "error":      str(exc),
            }
            return

        # ── Run rounds on this connection ─────────────────────────────────────
        try:
            async with conn as ws:
                while collected < n:
                    needed = n - collected
                    async for ev in _stream_round(
                        ws, prompt,
                        aspect_ratio       = aspect_ratio,
                        enable_nsfw        = enable_nsfw,
                        enable_pro         = enable_pro,
                        needed             = needed,
                        stream_timeout_s   = stream_timeout_s,
                        round_timeout_s    = timeout_s,
                        inter_round_wait_s = inter_round_wait_s,
                    ):
                        if ev["type"] == "_meta":
                            ws_closed = ev["ws_closed"]
                            break   # exit inner for-loop; handle ws_closed below
                        if ev.get("is_final"):
                            collected += 1
                        yield ev
                        if ev["type"] == "error":
                            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
                            return
                    else:
                        # _stream_round exhausted without a _meta — shouldn't happen
                        ws_closed = True

                    if ws_closed or collected >= n:
                        break   # exit inner while; reconnect or finish

        except aiohttp.ClientError as exc:
            logger.error("imagine websocket connection failed: error={}", exc)
            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
            yield {"type": "error", "error_code": "connection_failed", "error": str(exc)}
            return

        if collected >= n:
            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200))
            return

        # Server closed the connection but we still need more images → reconnect.
        # Give back the current lease before acquiring a new one on the next iteration.
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200))
        logger.info("imagine websocket reconnecting: remaining_images={} requested_images={}", n - collected, n)


__all__ = ["stream_images"]
