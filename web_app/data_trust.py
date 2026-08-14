"""Central trust rules for searchable and displayable extracted text."""

from __future__ import annotations

import re
from typing import Any

try:
    from .text_reconstruction import reconstruction_text
except ImportError:  # Direct ``python3 web_app/server.py`` execution.
    from text_reconstruction import reconstruction_text


def verified_text(record: dict[str, Any]) -> str:
    reconstructed = reconstruction_text(record)
    if reconstructed and (
        record.get("text_trust_status") == "verified"
        or (isinstance(record.get("reconstruction"), dict) and record["reconstruction"].get("verified") is True)
    ):
        return reconstructed
    if record.get("text_trust_status") == "verified" and str(record.get("verified_text", "")).strip():
        return str(record["verified_text"])
    return str(record.get("original_text", ""))


def is_reference_note(record: dict[str, Any]) -> bool:
    """Identify source-document notices that are not review issues.

    Some review packages repeat a reviewer directory/header on every round.
    It is useful audit evidence, but it is not a government requirement and
    must not become a searchable comment or a recurring issue timeline.
    Keep the raw record in the dataset; this is only a production-view guard.
    """
    text = re.sub(r"\s+", " ", verified_text(record)).strip().casefold()
    if not text:
        return False
    reviewer_directory = (
        "building dept reviewers" in text
        and ("please contact reviewers" in text or "office phone" in text)
    )
    reference_only = (
        "for reference use only" in text
        and ("don't copy" in text or "do not copy" in text)
    )
    # San Jose prescreen exports repeat this administrative fee/payment
    # notification at the beginning of every submission.  It is source
    # evidence, but not a design-review requirement, so it must not create a
    # recurring issue or become the first event in every timeline.
    fee_payment_notice = (
        (
            "official notification" in text
            and "plan check fee" in text
        )
        or (
            "plan check fee" in text
            and ("permit payment" in text or "payment option" in text)
            and (
                "permit specialist" in text
                or "chandler ramirez" in text
                or "408-535-3555" in text
            )
        )
    )
    return reviewer_directory or reference_only or fee_payment_notice


def is_malformed_rollup_comment(record: dict[str, Any]) -> bool:
    """Hide response-letter rows that concatenate several PC rounds.

    Some response-letter exports repeat the original ``A. PC1`` requirement
    and append ``PC2:``, ``PC3:``, and later follow-ups into one extracted
    comment cell.  That cell is not a new government comment; the individual
    round records are the authoritative searchable history.  Keep the raw
    row in the dataset for audit, but do not let it create a duplicate list
    item or recurring issue.
    """
    text = re.sub(r"\s+", " ", verified_text(record)).strip().casefold()
    if not text or not re.match(r"^a\.?\s*pc\s*1\b", text):
        return False
    source = str(record.get("source_document", "")).casefold()
    if "response" not in source:
        return False
    return len(re.findall(r"\bpc\s*[2-9]\s*:", text)) >= 2


def is_general_review_text(value: Any) -> bool:
    """Return whether text is boilerplate rather than a design issue.

    Review exports frequently repeat workflow instructions (``noted``, a
    generic code-review disclaimer, or a pointer to another checklist) beside
    the real row comment.  Those lines remain in the source record and can be
    searched, but they should not create a recurring-issue timeline or become
    its first event.

    The rules intentionally match complete, well-known boilerplate phrases;
    they do not classify every short comment as generic because short design
    requirements such as ``Provide brace at ridge`` are valid issues.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    if not text:
        return True
    text = text.strip(" -*_:")
    if text in {"noted", "noted.", "no changes, comment remains"}:
        return True
    if re.fullmatch(
        r"this comment to remain open until all other comments are resolved\.?",
        text,
    ):
        return True
    if re.fullmatch(
        r"please see comments at special inspection and testing form\.?",
        text,
    ):
        return True
    if (
        "planning reserves the right to provide additional comments" in text
        and len(text) < 260
    ):
        return True
    if (
        "building division review is limited to general compliance" in text
        and "not be construed as a comprehensive plan check" in text
    ):
        return True
    if (
        text.startswith("comment verify all requirements of crc r302")
        and "when submitting for full building permit" in text
    ):
        return True
    if (
        text.startswith("please obtain an electronic copy of the city of menlo park")
        and "special inspection and testing form" in text
        and "completely filled-out" in text
    ):
        return True
    return False


def searchable_comment(record: dict[str, Any], link: dict[str, Any] | None = None) -> bool:
    if (
        record.get("search_eligible") is False
        or record.get("text_trust_status") == "quarantined"
        or is_malformed_rollup_comment(record)
    ):
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
