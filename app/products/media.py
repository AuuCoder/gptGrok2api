"""Shared capability dispatcher for Grok Imagine media models.

The public product adapters use different response envelopes, but the upstream
work is the same: Imagine image generation, image editing, or a queued video
job.  Keeping the decision here prevents a media model from falling through to
the regular text-chat transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.control.model.spec import ModelSpec
from app.platform.errors import ValidationError


@dataclass(slots=True, frozen=True)
class MediaResult:
    """Normalised result consumed by the OpenAI and Anthropic adapters."""

    kind: Literal["image", "image_edit", "video"]
    prompt: str
    images: list[dict[str, Any]] | None = None
    video: dict[str, Any] | None = None


def _option(options: object | None, key: str, default: Any) -> Any:
    if isinstance(options, dict):
        value = options.get(key)
    else:
        value = getattr(options, key, None)
    return default if value is None else value


def _last_text_prompt(messages: list[dict[str, Any]], *, param: str) -> str:
    """Return the final usable text instruction from normalised messages."""
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type in {"text", "input_text", "output_text"}:
                text = str(block.get("text") or "").strip()
                if text:
                    text_parts.append(text)
        if text_parts:
            return " ".join(text_parts)
    raise ValidationError("A non-empty text prompt is required", param=param)


def _validate_image_count(model: str, n: int, *, image_edit: bool) -> None:
    max_n = 2 if image_edit else (4 if model == "grok-imagine-image-lite" else 10)
    if not 1 <= n <= max_n:
        raise ValidationError(
            f"n must be between 1 and {max_n} for model {model!r}",
            param="image_config.n",
        )


async def run_media(
    *,
    spec: ModelSpec,
    model: str,
    messages: list[dict[str, Any]],
    image_options: object | None = None,
    video_options: object | None = None,
    image_response_format: str = "url",
) -> MediaResult:
    """Execute the operation represented by *spec* and normalise its result.

    ``messages`` must already use the internal OpenAI-style text/image block
    representation.  Response formatting deliberately lives in the caller so
    each public protocol can remain valid for its own SDK.
    """
    if spec.is_image():
        from app.products.openai.images import generate

        prompt = _last_text_prompt(messages, param="input")
        n = int(_option(image_options, "n", 1))
        _validate_image_count(model, n, image_edit=False)
        result = await generate(
            model=model,
            prompt=prompt,
            n=n,
            size=str(_option(image_options, "size", "1024x1024")),
            response_format=image_response_format,
            stream=False,
            chat_format=False,
        )
        if not isinstance(result, dict):
            raise ValidationError("Image generation did not return a completed result")
        return MediaResult(
            kind="image",
            prompt=prompt,
            images=[item for item in result.get("data", []) if isinstance(item, dict)],
        )

    if spec.is_image_edit():
        from app.products.openai.images import edit

        n = int(_option(image_options, "n", 1))
        _validate_image_count(model, n, image_edit=True)
        result = await edit(
            model=model,
            messages=messages,
            n=n,
            size=str(_option(image_options, "size", "1024x1024")),
            response_format=image_response_format,
            stream=False,
            chat_format=False,
        )
        if not isinstance(result, dict):
            raise ValidationError("Image edit did not return a completed result")
        return MediaResult(
            kind="image_edit",
            prompt=_last_text_prompt(messages, param="input"),
            images=[item for item in result.get("data", []) if isinstance(item, dict)],
        )

    if spec.is_video():
        from app.products.openai.video import _extract_video_prompt_and_reference, create_video

        prompt, input_references = _extract_video_prompt_and_reference(messages)
        result = await create_video(
            model=model,
            prompt=prompt,
            seconds=_option(video_options, "seconds", 6),
            size=str(_option(video_options, "size", "720x1280")),
            resolution_name=_option(video_options, "resolution_name", None),
            preset=_option(video_options, "preset", None),
            input_references=input_references,
        )
        return MediaResult(kind="video", prompt=prompt, video=result)

    raise ValidationError(f"Model {model!r} is not a media model", param="model")


__all__ = ["MediaResult", "run_media"]
