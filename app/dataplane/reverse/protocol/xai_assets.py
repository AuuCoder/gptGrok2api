"""XAI asset management protocol — list / delete / download endpoints.

URL reference (from production reverse analysis):
  list    GET  https://grok.com/rest/assets
  delete  DELETE https://grok.com/rest/assets-metadata/{asset_id}
  download GET https://assets.grok.com/{path}    (streaming)

The app-chat upload endpoint is handled separately in transport/asset_upload.py.
"""

import pathlib
import urllib.parse
from urllib.parse import urlparse

ASSETS_LIST_URL      = "https://grok.com/rest/assets"
ASSETS_DELETE_URL    = "https://grok.com/rest/assets-metadata"   # append /{asset_id}
ASSETS_DOWNLOAD_BASE = "https://assets.grok.com"

# app-chat file management (used by asset_upload.py).
APP_CHAT_UPLOAD_URL = "https://grok.com/rest/app-chat/upload-file"

# MIME type mapping used for download content-type inference.
_EXTENSION_MIME: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".mp4":  "video/mp4",
    ".webm": "video/webm",
}

def asset_delete_url(asset_id: str) -> str:
    return f"{ASSETS_DELETE_URL}/{asset_id}"


def resolve_download_url(file_path: str) -> tuple[str, str, str]:
    """Resolve *file_path* to ``(url, origin, referer)``.

    *file_path* may be:
    - a full ``https://assets.grok.com/...`` URL
    - an absolute path  ``/foo/bar.png``
    - a relative path   ``foo/bar.png``
    """
    parsed = urlparse(file_path)
    if parsed.scheme and parsed.netloc:
        url    = file_path
        origin = f"{parsed.scheme}://{parsed.netloc}"
    else:
        path   = file_path if file_path.startswith("/") else f"/{file_path}"
        url    = f"{ASSETS_DOWNLOAD_BASE}{path}"
        origin = ASSETS_DOWNLOAD_BASE
    return url, origin, f"{origin}/"


def infer_content_type(url: str) -> str | None:
    """Return a best-guess MIME type for *url* based on file extension."""
    path = pathlib.Path(urllib.parse.urlparse(url).path)
    return _EXTENSION_MIME.get(path.suffix.lower())


def resolve_asset_reference(
    file_id: str,
    file_uri: str,
    *,
    user_id: str | None = None,
) -> str | None:
    """Return the absolute asset content URL used by image-edit requests."""
    if file_uri:
        url, _, _ = resolve_download_url(file_uri)
        return url
    if file_id and user_id:
        return f"{ASSETS_DOWNLOAD_BASE}/users/{user_id}/{file_id}/content"
    return None


__all__ = [
    "ASSETS_LIST_URL", "ASSETS_DELETE_URL", "ASSETS_DOWNLOAD_BASE",
    "APP_CHAT_UPLOAD_URL",
    "asset_delete_url", "resolve_download_url", "infer_content_type",
    "resolve_asset_reference",
]
