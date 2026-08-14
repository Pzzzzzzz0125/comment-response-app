"""Verbatim text reconstruction helpers.

This module deliberately has no knowledge of canonical events or timelines.
It creates a readable representation while retaining the original extracted
text.  Relationship-level repair is intentionally handled elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable


RECONSTRUCTION_VERSION = "reconstruction-v1"
IDENTITY_NORMALIZATION_VERSION = "identity-v2"
SEARCH_NORMALIZATION_VERSION = "search-v2"

_EXPORT_NOISE = re.compile(r"(?:_x000[dDaA]_|\*x000[dDaA]_\*?)")
_SPACE = re.compile(r"[ \t\f\v]+")
_LIST_PREFIX = re.compile(r"^\s*(?:(?:\d+|[A-Za-z])[.)]|[-•‣▪◦])\s+")
_HEADING_PREFIX = re.compile(r"^\s*(?:[A-Z][A-Za-z0-9 /&()'-]{1,80})\s*:\s*$")
_LEXICAL_TOKEN = re.compile(r"[\w]+|[^\w\s]", re.UNICODE)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _clean_line(value: str) -> str:
    value = _EXPORT_NOISE.sub(" ", value)
    value = value.replace("\u00a0", " ")
    return _SPACE.sub(" ", value).strip()


def _looks_like_list(value: str) -> bool:
    return bool(_LIST_PREFIX.match(value))


def _join_lines(lines: list[str]) -> str:
    """Join artificial extraction line breaks but retain real list/paragraph boundaries."""
    paragraphs: list[str] = []
    current: list[str] = []
    for raw in lines:
        line = _clean_line(raw)
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if current and _looks_like_list(line):
            paragraphs.append(" ".join(current))
            current = []
        # A standalone heading is a structural boundary.  Keep it verbatim,
        # but do not infer headings from ordinary all-caps comments.
        if current and _HEADING_PREFIX.match(line):
            paragraphs.append(" ".join(current))
            current = []
        if current and current[-1].endswith("-") and line[:1].islower():
            current[-1] = current[-1][:-1] + line
        else:
            current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(item.strip() for item in paragraphs if item.strip())


def reconstruct_verbatim_text(value: Any) -> str:
    """Return lexical-preserving, human-readable text.

    This is intentionally deterministic.  It does not summarize, paraphrase,
    correct grammar, alter measurements, or remove repeated wording.
    """
    raw = unicodedata.normalize("NFKC", _text(value))
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return _join_lines(raw.split("\n"))


def _lexical_signature(value: Any) -> list[str]:
    """Return a strict lexical signature for candidate reconstruction output.

    A Gemini reconstruction is allowed to repair layout, but it is not
    allowed to rewrite wording.  Comparing words, numbers, and punctuation
    gives us a cheap safety gate before accepting an optional model-produced
    representation.  Whitespace and known export noise are intentionally
    excluded from the signature.
    """
    cleaned = _EXPORT_NOISE.sub(" ", unicodedata.normalize("NFKC", _text(value)))
    return [token.casefold() for token in _LEXICAL_TOKEN.findall(cleaned)]


def is_lexically_safe_reconstruction(raw: Any, candidate: Any) -> bool:
    """Whether a candidate changes only formatting, not source wording."""
    candidate_text = reconstruct_verbatim_text(candidate)
    if not candidate_text:
        return False
    return _lexical_signature(raw) == _lexical_signature(candidate_text)


def normalize_for_identity(value: Any) -> str:
    """Deterministic identity candidate text; never used to mutate old IDs."""
    text = reconstruct_verbatim_text(value).casefold()
    text = re.sub(r"\b(?:markup|comment)\s+[^\n]{0,100}?\bv\d+\s*[-/]?\s*c\d+\s+\d+(?:\.\d+)?\b", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_search(value: Any) -> str:
    text = reconstruct_verbatim_text(value).casefold()
    return re.sub(r"\s+", " ", text).strip()


def build_display_structure(text: str) -> list[dict[str, Any]]:
    """Build a small offset-based structure for paragraph/list rendering."""
    blocks: list[dict[str, Any]] = []
    cursor = 0
    for paragraph in str(text or "").split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            cursor += 2
            continue
        start = str(text).find(paragraph, cursor)
        if start < 0:
            start = cursor
        end = start + len(paragraph)
        lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
        if lines and all(_looks_like_list(line) for line in lines):
            blocks.append({"type": "list_item", "label": _LIST_PREFIX.match(lines[0]).group(0).strip(), "start": start, "end": end})
        elif _HEADING_PREFIX.match(paragraph):
            blocks.append({"type": "heading", "start": start, "end": end})
        else:
            blocks.append({"type": "paragraph", "start": start, "end": end})
        cursor = end + 2
    return blocks


def source_unit_id(record: dict[str, Any], role: str = "evidence", ordinal: int = 0) -> str:
    """Create a stable unit ID when an extractor did not provide one."""
    locator = record.get("source_locator_json")
    if not isinstance(locator, dict):
        locator = record.get("source_location") if isinstance(record.get("source_location"), dict) else {}
    key = {
        "sha": record.get("source_sha256", ""),
        "path": record.get("source_document", ""),
        "page": record.get("source_page") or record.get("source_page_start", ""),
        "page_end": record.get("source_page_end", ""),
        "sheet": record.get("source_sheet", ""),
        "row": record.get("source_row", ""),
        "cell": record.get("source_cell_range", ""),
        "paragraph": record.get("paragraph_index", ""),
        "locator": locator,
        "role": role,
        "ordinal": ordinal,
    }
    digest = hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return f"SU-{digest}"


def _existing_units(record: dict[str, Any], role: str) -> list[str]:
    keys = ("source_unit_ids", "comment_unit_ids" if role == "comment" else "response_unit_ids")
    for key in keys:
        value = record.get(key)
        if isinstance(value, list):
            units = [str(item) for item in value if str(item).strip()]
            if units:
                return list(dict.fromkeys(units))
    return []


def attach_reconstruction(
    record: dict[str, Any],
    *,
    role: str = "evidence",
    source_unit_ids: Iterable[str] | None = None,
    verified: bool | None = None,
    method: str | None = None,
    reconstructed_text: str | None = None,
) -> dict[str, Any]:
    """Return a copy with additive reconstruction fields only."""
    result = dict(record)
    # Keep the raw representation separate.  ``verified_text`` is the best
    # source for a trusted record, but it must never overwrite the immutable
    # extraction that was produced by the parser/OCR pass.
    raw_text = _text(
        record.get("text_raw")
        or record.get("raw_extracted_text")
        or record.get("original_text")
        or record.get("verified_text")
    )
    original = _text(record.get("verified_text") or record.get("original_text") or raw_text)
    reconstructed = reconstruct_verbatim_text(original)
    # Gemini may return an optional layout reconstruction alongside the exact
    # transcription.  Accept it only when the lexical safety gate proves it
    # did not add, remove, or alter words, numbers, codes, measurements,
    # punctuation, or negations.  Otherwise retain the deterministic local
    # representation and keep the model output available in the audit layer.
    candidate = _text(reconstructed_text)
    if candidate and is_lexically_safe_reconstruction(original, candidate):
        reconstructed = reconstruct_verbatim_text(candidate)
    units = list(dict.fromkeys(str(item) for item in (source_unit_ids or []) if str(item).strip()))
    units = units or _existing_units(record, role)
    if not units:
        units = [source_unit_id(record, role=role)] if original else []
    trusted = bool(
        verified if verified is not None else (
            str(record.get("verification_status", "")).casefold() == "confirmed"
            or str(record.get("text_trust_status", "")).casefold() == "verified"
        )
    )
    result.update({
        "text_raw": raw_text,
        "text_reconstructed": reconstructed,
        "display_structure": build_display_structure(reconstructed),
        "normalized_identity_text_v2": normalize_for_identity(reconstructed),
        "normalized_search_text_v2": normalize_for_search(reconstructed),
        "source_unit_ids": units,
        "reconstruction": {
            "version": RECONSTRUCTION_VERSION,
            "method": method or ("legacy_verified_text" if not reconstructed else "local_deterministic_cleanup"),
            "source_unit_ids": units,
            "verified": trusted,
            "verification_version": record.get("verification_prompt_version", ""),
            "raw_text_source": (
                "text_raw" if record.get("text_raw") else
                "raw_extracted_text" if record.get("raw_extracted_text") else
                "original_text" if record.get("original_text") else
                "verified_text"
            ),
            "uncertain": not trusted,
        },
    })
    return result


def reconstruction_text(record: dict[str, Any]) -> str:
    """Read reconstructed text only when it is trustworthy, with legacy fallback."""
    value = _text(record.get("text_reconstructed"))
    metadata = record.get("reconstruction")
    if value and (not isinstance(metadata, dict) or metadata.get("verified") is not False):
        return value
    return _text(record.get("verified_text") or record.get("original_text") or record.get("raw_extracted_text"))


__all__ = [
    "IDENTITY_NORMALIZATION_VERSION", "RECONSTRUCTION_VERSION", "SEARCH_NORMALIZATION_VERSION",
    "attach_reconstruction", "build_display_structure", "normalize_for_identity",
    "normalize_for_search", "reconstruct_verbatim_text", "reconstruction_text", "source_unit_id",
    "is_lexically_safe_reconstruction",
]
