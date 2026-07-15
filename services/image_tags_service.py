from __future__ import annotations

from pathlib import Path
from threading import Lock

from services.config import DATA_DIR
from services.json_file import read_json_object, write_json_file

TAGS_FILE = DATA_DIR / "image_tags.json"
TAGS_LOCK = Lock()


def _ensure_file() -> None:
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not TAGS_FILE.exists():
        write_json_file(TAGS_FILE, {})


def load_tags() -> dict[str, list[str]]:
    with TAGS_LOCK:
        _ensure_file()
        data = read_json_object(TAGS_FILE, name="image_tags.json")
        return data if isinstance(data, dict) else {}


def save_tags(data: dict[str, list[str]]) -> None:
    with TAGS_LOCK:
        _ensure_file()
        write_json_file(TAGS_FILE, data)


def get_tags(image_rel: str) -> list[str]:
    return load_tags().get(image_rel, [])


def set_tags(image_rel: str, tags: list[str]) -> list[str]:
    with TAGS_LOCK:
        _ensure_file()
        data = read_json_object(TAGS_FILE, name="image_tags.json")
        cleaned = list(dict.fromkeys(t.strip() for t in tags if t.strip()))
        if cleaned:
            data[image_rel] = cleaned
        else:
            data.pop(image_rel, None)
        write_json_file(TAGS_FILE, data)
        return cleaned


def remove_tags(image_rel: str) -> None:
    with TAGS_LOCK:
        _ensure_file()
        data = read_json_object(TAGS_FILE, name="image_tags.json")
        if data.pop(image_rel, None) is not None:
            write_json_file(TAGS_FILE, data)


def delete_tag(tag: str) -> int:
    """从所有图片中删除指定标签，返回受影响的图片数。"""
    with TAGS_LOCK:
        _ensure_file()
        data = read_json_object(TAGS_FILE, name="image_tags.json")
        count = 0
        for rel in list(data):
            if tag in data[rel]:
                data[rel] = [t for t in data[rel] if t != tag]
                if not data[rel]:
                    del data[rel]
                count += 1
        if count > 0:
            write_json_file(TAGS_FILE, data)
        return count


def get_all_tags() -> list[str]:
    data = load_tags()
    seen: set[str] = set()
    result: list[str] = []
    for tags in data.values():
        for t in tags:
            if t not in seen:
                seen.add(t)
                result.append(t)
    return result
