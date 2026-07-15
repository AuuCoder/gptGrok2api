from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse


PromptRecord = dict[str, Any]


class PromptSourceParseError(ValueError):
    pass


@dataclass(frozen=True)
class PromptSourceAdapter:
    name: str
    label: str
    extensions: tuple[str, ...]
    content_markers: tuple[str, ...]
    parse: Callable[[bytes, dict[str, Any]], list[PromptRecord]]


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r\n", "\n")).strip()


def _clean_multiline(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _decode_payload(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _strip_tags(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<img\b[^>\n]*(?:>|$)", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _strip_markdown(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", "", text)
    text = re.sub(r"<img\b[^>\n]*(?:>|$)", "", text, flags=re.I)
    text = re.sub(r"\[([^\]]+)]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_~>#]+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _normalize_title(value: str) -> str:
    text = _strip_markdown(value)
    text = re.sub(r"^No\.\s*\d+\s*[:\uff1a\-\s]*", "", text, flags=re.I)
    text = re.sub(r"^[\d\s.\-\u3001:\uff1a]+", "", text)
    return text.strip(" -:\u3001\uff1a")


def _markdown_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    urls.extend(re.findall(r"!\[[^\]]*]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", text))
    urls.extend(re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", text, flags=re.I))
    seen: set[str] = set()
    result: list[str] = []
    for raw in urls:
        url = html.unescape(raw).strip()
        if not url or url in seen:
            continue
        lower = url.lower()
        if "img.shields.io" in lower or "badge.svg" in lower or "awesome.re" in lower:
            continue
        seen.add(url)
        result.append(url)
    return result


def parse_json_prompt_source(payload: bytes, source: dict[str, Any]) -> list[PromptRecord]:
    try:
        data = json.loads(_decode_payload(payload))
    except json.JSONDecodeError as exc:
        raise PromptSourceParseError(f"JSON parse failed: {exc}") from exc

    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        for key in ("items", "prompts", "data"):
            value = data.get(key)
            if isinstance(value, list):
                raw_items = value
                break
        else:
            raw_items = []
    else:
        raw_items = []

    items: list[PromptRecord] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        images = raw.get("images")
        image_urls = [item for item in images if isinstance(item, str)] if isinstance(images, list) else []
        preview = image_urls[0] if image_urls else ""
        items.append(
            {
                **raw,
                "title": raw.get("title") or raw.get("title_cn") or raw.get("title_zh") or raw.get("title_en") or raw.get("name"),
                "description": raw.get("description") or raw.get("description_cn") or raw.get("note") or raw.get("summary"),
                "preview": raw.get("preview") or raw.get("preview_url") or raw.get("image") or raw.get("image_url") or preview,
                "reference_image_urls": raw.get("reference_image_urls") or raw.get("reference_images") or image_urls,
                "category": raw.get("category_cn") or raw.get("category_zh") or raw.get("category"),
                "sub_category": raw.get("sub_category_cn") or raw.get("sub_category"),
                "link": raw.get("link") or raw.get("source_url"),
            }
        )
    return items


def parse_markdown_prompt_source(payload: bytes, source: dict[str, Any]) -> list[PromptRecord]:
    text = _decode_payload(payload)
    lines = text.splitlines()
    sections: list[tuple[str, str, str]] = []
    current_category = ""
    current_title = ""
    current_start = -1

    for index, line in enumerate(lines):
        match = re.match(r"^(#{2,3})\s+(.+?)\s*$", line)
        if not match:
            continue
        level, heading = match.groups()
        if level == "##":
            if current_title and current_start >= 0:
                sections.append((current_category, current_title, "\n".join(lines[current_start:index])))
                current_title = ""
                current_start = -1
            current_category = _normalize_title(heading)
        elif level == "###":
            if current_title and current_start >= 0:
                sections.append((current_category, current_title, "\n".join(lines[current_start:index])))
            current_title = _normalize_title(heading)
            current_start = index

    if current_title and current_start >= 0:
        sections.append((current_category, current_title, "\n".join(lines[current_start:])))

    items: list[PromptRecord] = []
    fence_re = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)(?:\n```|$)", flags=re.S)
    for order, (category, title, section) in enumerate(sections):
        code_blocks = list(fence_re.finditer(section))
        if not title or not code_blocks:
            continue
        prompt = ""
        for block in code_blocks:
            body = _clean_multiline(block.group("body"))
            if len(body) >= 10:
                prompt = body
                break
        if not prompt:
            continue
        image_urls = _markdown_image_urls(section)
        description = ""
        for raw in section.splitlines()[1:]:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!") or line.startswith("```") or re.match(r"^</?img\b", line, flags=re.I):
                continue
            stripped = _strip_markdown(line)
            if stripped and stripped != prompt:
                description = stripped[:500]
                break
        items.append(
            {
                "id": f"md-{order + 1}",
                "title": title,
                "description": description,
                "prompt": prompt,
                "preview": image_urls[0] if image_urls else "",
                "reference_image_urls": image_urls,
                "category": category,
                "tags": [tag for tag in [category, "Markdown"] if tag],
                "mode": "image",
                "image_mode": "generate",
                "sort_order": order,
            }
        )
    return items


def parse_html_prompt_source(payload: bytes, source: dict[str, Any]) -> list[PromptRecord]:
    text = _decode_payload(payload)
    articles = re.findall(r"<article\b[^>]*>(.*?)</article>", text, flags=re.S | re.I)
    if not articles:
        articles = re.findall(r"<section\b[^>]*>(.*?)</section>", text, flags=re.S | re.I)
    if not articles:
        articles = re.findall(r"<div\b[^>]*class=[\"'][^\"']*(?:prompt|card|item)[^\"']*[\"'][^>]*>(.*?)</div>", text, flags=re.S | re.I)

    items: list[PromptRecord] = []
    for order, article in enumerate(articles):
        title_match = re.search(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]>", article, flags=re.S | re.I)
        title = _clean(_strip_tags(title_match.group(1))) if title_match else ""
        paragraphs = [
            _clean_multiline(_strip_tags(match))
            for match in re.findall(r"<p\b[^>]*>(.*?)</p>", article, flags=re.S | re.I)
        ]
        paragraphs = [item for item in paragraphs if item]
        prompt = max(paragraphs, key=len) if paragraphs else ""
        description = next((item for item in paragraphs if item != prompt), "")
        image_urls = re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", article, flags=re.I)
        if title and prompt:
            items.append(
                {
                    "id": f"html-{order + 1}",
                    "title": title,
                    "description": description,
                    "prompt": prompt,
                    "preview": image_urls[0] if image_urls else "",
                    "reference_image_urls": image_urls,
                    "category": "",
                    "tags": [],
                    "mode": "image",
                    "image_mode": "generate",
                    "sort_order": order,
                }
            )
    return items


ADAPTERS: dict[str, PromptSourceAdapter] = {
    "json": PromptSourceAdapter("json", "JSON", (".json",), ("application/json", "text/json"), parse_json_prompt_source),
    "markdown": PromptSourceAdapter("markdown", "Markdown", (".md", ".markdown"), ("text/markdown", "text/plain"), parse_markdown_prompt_source),
    "html": PromptSourceAdapter("html", "HTML", (".html", ".htm"), ("text/html",), parse_html_prompt_source),
}


def adapter_label(adapter_name: str) -> str:
    return ADAPTERS.get(adapter_name, ADAPTERS["json"]).label


def normalize_adapter_name(value: object, url: str = "") -> str:
    raw = str(value or "").strip().lower()
    if raw in ADAPTERS:
        return raw
    parsed_path = urlparse(url).path.lower()
    for adapter in ADAPTERS.values():
        if any(parsed_path.endswith(extension) for extension in adapter.extensions):
            return adapter.name
    return "json"


def infer_adapter_name(url: str, content_type: str = "", explicit: object = "") -> str:
    raw = str(explicit or "").strip().lower()
    if raw in ADAPTERS:
        return raw
    content_type = content_type.lower()
    for adapter in ADAPTERS.values():
        if any(marker in content_type for marker in adapter.content_markers):
            return adapter.name
    return normalize_adapter_name("", url)


def parse_prompt_source_payload(
    payload: bytes,
    source: dict[str, Any],
    *,
    content_type: str = "",
) -> tuple[str, list[PromptRecord]]:
    explicit_adapter = str(source.get("adapter") or "").strip().lower()
    allow_adapter_fallback = bool(source.get("built_in")) or bool(source.get("allow_adapter_fallback"))
    adapter_name = infer_adapter_name(str(source.get("url") or ""), content_type, explicit_adapter)
    attempts = [adapter_name]
    if allow_adapter_fallback or not explicit_adapter:
        attempts.extend(name for name in ADAPTERS if name != adapter_name)

    last_error: Exception | None = None
    for name in attempts:
        try:
            items = ADAPTERS[name].parse(payload, source)
        except Exception as exc:
            last_error = exc
            continue
        if items:
            return name, items
    if last_error is not None:
        raise PromptSourceParseError(str(last_error)) from last_error
    return adapter_name, []
