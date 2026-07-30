"""Central trust rules for searchable and displayable extracted text."""

from __future__ import annotations

from typing import Any


def verified_text(record: dict[str, Any]) -> str:
    if record.get("text_trust_status") == "verified" and str(record.get("verified_text", "")).strip():
        return str(record["verified_text"])
    return str(record.get("original_text", ""))


def searchable_comment(record: dict[str, Any], link: dict[str, Any] | None = None) -> bool:
    if record.get("search_eligible") is False or record.get("text_trust_status") == "quarantined":
        return False
    link = link or {}
    if link.get("provenance") == "document_structure_rematch":
        confirmed = link.get("match_status") == "confirmed" or link.get("review_status") == "confirmed"
        if confirmed:
            return bool(str(record.get("verified_text") or record.get("original_text") or "").strip())
        return record.get("text_trust_status") == "verified" and bool(str(record.get("verified_text", "")).strip())
    if record.get("extraction_method") == "gemini_visual_two_pass":
        return record.get("text_trust_status") == "verified" and bool(str(record.get("verified_text", "")).strip())
    return True
