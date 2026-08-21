"""Load local runtime secrets without adding a dotenv dependency."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV_FILES = (
    WORKSPACE_ROOT / ".env.local",
    WORKSPACE_ROOT / ".env",
)


@lru_cache(maxsize=1)
def _read_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in LOCAL_ENV_FILES:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            values.setdefault(key, value)
    return values


def runtime_setting(name: str, default: str = "", *aliases: str) -> str:
    """Return a non-mutating runtime setting from the OS or local env files.

    Environment variables take precedence.  Keeping this lookup centralized
    makes ``.env.local`` behave the same on Windows, macOS, and Linux without a
    third-party dotenv dependency.
    """
    names = (name, *aliases)
    for key in names:
        value = os.environ.get(key)
        if value is not None and value.strip():
            return value.strip()
    local = _read_local_env()
    for key in names:
        value = local.get(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def gemini_api_key() -> str:
    """Return the shared Gemini key without logging or mutating the environment."""
    # Test and offline-retrieval runs must be able to opt out even when a
    # developer .env file is present.  An empty GEMINI_API_KEY used to fall
    # through to that file, which made supposedly local regression requests
    # unexpectedly call Gemini and appear to hang.
    if runtime_setting("PERMIT_DISABLE_GEMINI").casefold() in {"1", "true", "yes"}:
        return ""
    return runtime_setting("GEMINI_API_KEY", "", "GOOGLE_API_KEY")
