"""Load local runtime secrets without adding a dotenv dependency."""

from __future__ import annotations

import os
from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV_FILES = (
    WORKSPACE_ROOT / ".env.local",
    WORKSPACE_ROOT / ".env",
)


def _read_local_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in LOCAL_ENV_FILES:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
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


def gemini_api_key() -> str:
    """Return the shared Gemini key without logging or mutating the environment."""
    # Test and offline-retrieval runs must be able to opt out even when a
    # developer .env file is present.  An empty GEMINI_API_KEY used to fall
    # through to that file, which made supposedly local regression requests
    # unexpectedly call Gemini and appear to hang.
    if os.environ.get("PERMIT_DISABLE_GEMINI", "").casefold() in {"1", "true", "yes"}:
        return ""
    environment_value = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    if environment_value:
        return environment_value
    local = _read_local_env()
    return (
        local.get("GEMINI_API_KEY")
        or local.get("GOOGLE_API_KEY")
        or ""
    ).strip()
