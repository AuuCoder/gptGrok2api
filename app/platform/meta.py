"""Project metadata helpers sourced from pyproject.toml."""

from functools import lru_cache
from pathlib import Path
import tomllib


_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "pyproject.toml"


@lru_cache(maxsize=1)
def get_project_meta() -> dict[str, str]:
    data: dict[str, str] = {"name": "grok2api", "version": "0.0.0"}
    if not _PYPROJECT.exists():
        return data
    with _PYPROJECT.open("rb") as fh:
        parsed = tomllib.load(fh)
    project = parsed.get("project") or {}
    name = str(project.get("name") or data["name"]).strip() or data["name"]
    version = str(project.get("version") or data["version"]).strip() or data["version"]
    return {"name": name, "version": version}


def get_project_version() -> str:
    return get_project_meta()["version"]


__all__ = ["get_project_meta", "get_project_version"]
