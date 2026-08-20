"""Cross-platform discovery for optional local document-processing tools."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

try:
    from .local_secrets import runtime_setting
except ImportError:  # Direct script execution from ``web_app``.
    from local_secrets import runtime_setting


def _first_existing(paths: Iterable[Path]) -> str | None:
    for path in paths:
        try:
            if path.is_file():
                return str(path)
        except OSError:
            continue
    return None


def _configured_or_path(variable: str, commands: tuple[str, ...]) -> str | None:
    configured = runtime_setting(variable)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        discovered = shutil.which(configured)
        if discovered:
            return discovered
    for command in commands:
        discovered = shutil.which(command)
        if discovered:
            return discovered
    return None


def _is_windows() -> bool:
    return os.name == "nt"


def libreoffice_executable(explicit: str | None = None) -> str | None:
    if explicit:
        path = Path(explicit).expanduser()
        return str(path) if path.is_file() else shutil.which(explicit)
    discovered = _configured_or_path(
        "LIBREOFFICE_PATH", ("soffice", "libreoffice", "soffice.exe"),
    )
    if discovered or not _is_windows():
        return discovered
    roots = [
        Path(value) for value in (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ) if value
    ]
    return _first_existing(root / "LibreOffice" / "program" / "soffice.exe" for root in roots)


def ghostscript_executable() -> str | None:
    discovered = _configured_or_path(
        "GHOSTSCRIPT_PATH", ("gs", "gswin64c", "gswin32c", "gswin64c.exe", "gswin32c.exe"),
    )
    if discovered or not _is_windows():
        return discovered
    roots = [
        Path(value) for value in (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ) if value
    ]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(sorted((root / "gs").glob("gs*/bin/gswin64c.exe"), reverse=True))
        candidates.extend(sorted((root / "gs").glob("gs*/bin/gswin32c.exe"), reverse=True))
    return _first_existing(candidates)


def tesseract_executable() -> str | None:
    discovered = _configured_or_path("TESSERACT_PATH", ("tesseract", "tesseract.exe"))
    if discovered or not _is_windows():
        return discovered
    roots = [
        Path(value) for value in (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ) if value
    ]
    return _first_existing(root / "Tesseract-OCR" / "tesseract.exe" for root in roots)
