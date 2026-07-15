"""Media transport — create post, create link, upscale video.

All three endpoints are simple JSON POST calls with proxy lifecycle.
"""

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind, ProxyScope, RequestKind
from app.dataplane.reverse.transport._proxy_feedback import upstream_feedback
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.reverse.protocol.xai_video import (
    MEDIA_LINK_URL,
    MEDIA_POST_URL,
    VIDEO_UPSCALE_URL,
    build_media_link_payload,
    build_media_post_payload,
    build_upscale_payload,
)
from app.dataplane.reverse.transport.http import post_json


async def _post_with_proxy(
    url:     str,
    token:   str,
    payload: dict,
    *,
    label:     str,
    timeout_key: str = "video.timeout",
    referer:   str   = "https://grok.com",
) -> dict:
    """Shared helper: acquire proxy → POST JSON → feedback → return body."""
    cfg       = get_config()
    timeout_s = cfg.get_float(timeout_key, 60.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire(scope=ProxyScope.APP, kind=RequestKind.HTTP)

    try:
        result = await post_json(
            url,
            token,
            orjson.dumps(payload),
            lease     = lease,
            timeout_s = timeout_s,
            origin    = "https://grok.com",
            referer   = referer,
        )
    except UpstreamError as exc:
        await proxy.feedback(
            lease,
            upstream_feedback(exc),
        )
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        raise UpstreamError(f"{label}: transport error: {exc}") from exc

    await proxy.feedback(
        lease,
        ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
    )
    logger.debug("media request completed: operation={}", label)
    return result


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

async def create_media_post(
    token:      str,
    media_type: str,
    media_url:  str = "",
    prompt:     str = "",
    referer:    str = "https://grok.com/imagine",
) -> dict:
    """POST /rest/media/post/create — create a media post."""
    payload = build_media_post_payload(
        media_type = media_type,
        media_url  = media_url,
        prompt     = prompt,
    )
    return await _post_with_proxy(
        MEDIA_POST_URL, token, payload,
        label = "create_media_post",
        referer = referer,
    )


async def create_media_link(token: str, post_id: str) -> dict:
    """POST /rest/media/post/create-link — get a shareable link for a post."""
    payload = build_media_link_payload(post_id)
    return await _post_with_proxy(
        MEDIA_LINK_URL, token, payload,
        label = "create_media_link",
    )


async def upscale_video(token: str, video_id: str) -> dict:
    """POST /rest/media/video/upscale — upscale a video."""
    payload = build_upscale_payload(video_id)
    return await _post_with_proxy(
        VIDEO_UPSCALE_URL, token, payload,
        label = "upscale_video",
    )


__all__ = ["create_media_post", "create_media_link", "upscale_video"]
