#!/usr/bin/env python3
"""Local permit comment-response browser with deterministic precedent retrieval."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from .source_registry import SourceRegistry
    from .gemini_enrich import GeminiClient, record_digest
    from .rag_search import SearchIndex, normalize_analysis
    from .knowledge_chat import KnowledgeChat, enrich_query_plan, fallback_query_plan
    from .data_trust import is_general_review_text, is_malformed_rollup_comment, is_reference_note, searchable_comment, verified_text
    from .comment_dedup import find_duplicate_comments
    from .canonical_event import (
        NORMALIZATION_VERSION,
        classify_event_text_match,
        high_confidence_text_extension,
        normalize_actor,
        normalize_event_text,
    )
    from .document_identity import canonical_city_name, canonicalize_documents, topic_occurrence_key, topic_occurrence_allowed
    from .topic_taxonomy import TOPIC_TAXONOMY_VERSION, classify_topic
    from .progressive_retrieval import ValidatedTagIndex, progressive_retrieve
    from .local_secrets import gemini_api_key, runtime_setting
    from .ingestion_admin import IngestionAdmin
except ImportError:  # Direct `python3 web_app/server.py` execution.
    from source_registry import SourceRegistry
    from gemini_enrich import GeminiClient, record_digest
    from rag_search import SearchIndex, normalize_analysis
    from knowledge_chat import KnowledgeChat, enrich_query_plan, fallback_query_plan
    from data_trust import is_general_review_text, is_malformed_rollup_comment, is_reference_note, searchable_comment, verified_text
    from comment_dedup import find_duplicate_comments
    from canonical_event import (
        NORMALIZATION_VERSION,
        classify_event_text_match,
        high_confidence_text_extension,
        normalize_actor,
        normalize_event_text,
    )
    from document_identity import canonical_city_name, canonicalize_documents, topic_occurrence_key, topic_occurrence_allowed
    from topic_taxonomy import TOPIC_TAXONOMY_VERSION, classify_topic
    from progressive_retrieval import ValidatedTagIndex, progressive_retrieve
    from local_secrets import gemini_api_key, runtime_setting
    from ingestion_admin import IngestionAdmin


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for",
    "from", "has", "have", "in", "is", "it", "of", "on", "or", "that",
    "the", "this", "to", "was", "were", "will", "with", "your", "you",
    "please", "provide", "show", "shall", "per",
}

TECHNICAL_TERMS = {
    "access", "ada", "anchor", "bearing", "beam", "building", "calculation",
    "cbc", "cec", "code", "concrete", "connection", "construction", "cpc",
    "crc", "detail", "dimension", "door", "drain", "egress", "electrical",
    "elevation", "engineering", "fire", "floor", "footing", "foundation",
    "framing", "grading", "guardrail", "hvac", "irrigation", "lateral",
    "load", "mechanical", "plumbing", "rafter", "roof", "seismic", "sewer",
    "shear", "site", "slab", "soil", "stair", "stormwater", "structural",
    "tree", "ventilation", "wall", "window",
}

ADMINISTRATIVE_TERMS = {
    "application", "apply", "contact", "declaration", "email", "fee", "form",
    "invoice", "owner", "payment", "permit", "resubmit", "signature", "stamp",
    "submit", "submittal", "upload",
}

TOPIC_STOP_WORDS = STOP_WORDS | {
    "comment", "comments", "fullset", "general", "markup", "pdf", "plan",
    "plans", "review", "reviewed", "round", "sheet", "sheets",
}


def readable_text(text: str) -> str:
    """Return display-friendly text without changing the immutable source value."""
    value = html.unescape(unicodedata.normalize("NFKC", text or ""))
    value = value.replace("_x000D_", " ").replace("_x000A_", " ")
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(
        r"^(?:response\s*[_:-]\s*)+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value


def readable_evidence_text(text: str) -> str:
    """Format extracted evidence while keeping its wording intact.

    Spreadsheet/XML line-break markers are converted to paragraph breaks so a
    long requirement is readable in the app. This is presentation text only;
    the original extracted value remains immutable in the dataset.
    """
    value = html.unescape(unicodedata.normalize("NFKC", text or ""))
    value = re.sub(r"(?:_x000[dD]_|\*x000[dD]\*)", "\n", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u00a0", " ").replace("\t", " ")
    value = re.sub(r"(?m)^[ \t]*-{8,}[ \t]*$", "", value)
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in value.split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    value = "\n\n".join(paragraphs)
    value = re.sub(
        r"^(?:response\s*[_:-]\s*)+",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    return value.strip()


MARKUP_FILE_PATTERN = re.compile(
    r"^\s*(?P<kind>markup|comment)\s+(?P<filename>[^\s|]+\.(?:pdf|docx?|xlsx?|csv))\s+(?P<rest>.+)$",
    re.IGNORECASE | re.DOTALL,
)
MARKUP_IDENTIFIER_PATTERN = re.compile(
    r"\b(?P<version>V\s*\d+\s*[-/]?\s*C\s*\d+)\s+(?P<number>\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


def comment_display_parts(
    text: str,
    source_document: str = "",
    comment_number: str = "",
) -> tuple[str, str]:
    """Return ``(body, compact label)`` for common markup-prefixed comments.

    A few PDF extractions put the source filename and review identifier in
    front of the actual requirement, for example ``Markup ... V1-C1 39``.
    Detect only that well-formed prefix; all other text is displayed as-is.
    """
    formatted = readable_evidence_text(text)
    match = MARKUP_FILE_PATTERN.match(formatted)
    if not match:
        return formatted, ""
    rest = match.group("rest")
    identifier = MARKUP_IDENTIFIER_PATTERN.search(rest)
    if not identifier:
        number = str(comment_number or "").strip()
        if not number:
            return formatted, ""
        identifier = re.search(
            rf"\b{re.escape(number)}\b", rest[:160], re.IGNORECASE
        )
        if not identifier:
            return formatted, ""
    body = rest[identifier.end():].lstrip(" -:;|\n")
    if len(body) < 20:
        return formatted, ""
    version_value = identifier.groupdict().get("version") or ""
    version = re.sub(r"\s+", "", version_value)
    number_value = identifier.groupdict().get("number", "")
    label = f"{match.group('kind').title()} · {version} {number_value}".strip()
    return readable_evidence_text(body), label


EVENT_TIME_PATTERN = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{2,4}"
    r"(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM))?)\b",
    re.IGNORECASE,
)
WORKBOOK_EXPORT_PATTERN = re.compile(
    r"(?:^|[\s_-])RS[_\s-]*"
    r"(\d{1,2})-(\d{1,2})-(\d{4})"
    r"[_\s-]+(\d{1,2})_(\d{2})_(AM|PM)\b",
    re.IGNORECASE,
)
DOCUMENT_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})[-_](?P<day>\d{1,2})[-_](?P<year>20\d{2})(?!\d)"
    r"|(?<!\d)(?P<iso_year>20\d{2})[-_](?P<iso_month>\d{2})[-_](?P<iso_day>\d{2})(?!\d)",
    re.IGNORECASE,
)
COMPACT_DOCUMENT_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<compact_year>20\d{2})(?P<compact_month>\d{2})"
    r"(?P<compact_day>\d{2})(?:\d{4,})?(?!\d)",
    re.IGNORECASE,
)
SUBMISSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<number>\d+)(?:st|nd|rd|th)\s+submission\b",
    re.IGNORECASE,
)
ROUND_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<number>\d+)(?:st|nd|rd|th)\s+round"
    r"(?:\s+of\s+comments?)?\b",
    re.IGNORECASE,
)
PC_ROUND_PATTERN = re.compile(r"(?<![A-Za-z0-9])PC\s*-?\s*(?P<number>\d+)\b", re.IGNORECASE)
NUMERIC_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{2,4})(?!\d)"
)
MONTH_DATE_PATTERN = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
    r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(?P<day>\d{1,2})(?:,|\s)\s*(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
TRAILING_DATE_NOTE_PATTERN = re.compile(
    r"^(?P<body>.*?)(?:\s+|\n+)(?P<date>\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"\s*:\s*(?P<note>complete(?:d)?|resolved|closed)\.?\s*$",
    re.IGNORECASE,
)


def reviewer_event_identity(value: str) -> tuple[str, str]:
    """Return reviewer name and exact source timestamp from a reviewer cell."""
    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in str(value or "").splitlines()
        if line.strip()
    ]
    timestamp = ""
    actor = ""
    for index, line in enumerate(lines):
        match = EVENT_TIME_PATTERN.search(line)
        if not match:
            continue
        timestamp = match.group(1)
        if index:
            actor = lines[index - 1]
        break
    if not actor and lines:
        actor = lines[-1] if not timestamp else ""
    return actor, timestamp


def workbook_export_label(source_document: str) -> str:
    match = WORKBOOK_EXPORT_PATTERN.search(Path(source_document).name)
    if not match:
        return ""
    month, day, year, _hour, _minute, _meridiem = match.groups()
    return (
        f"By workbook export · {int(month):02d}/{int(day):02d}/"
        f"{year}"
    )


def document_date_label(source_document: str) -> str:
    """Extract a date embedded in a source filename or submission path.

    ProjectDox workbook exports commonly use ``RS_09-24-2025`` while other
    packages use an ISO-like date.  This is display metadata only; the source
    text and immutable record dates are never rewritten.
    """
    path_text = str(source_document or "")
    matches = [
        (match.start(), match, "separated")
        for match in DOCUMENT_DATE_PATTERN.finditer(path_text)
    ] + [
        (match.start(), match, "compact")
        for match in COMPACT_DOCUMENT_DATE_PATTERN.finditer(path_text)
    ]
    if not matches:
        return ""
    _position, match, kind = max(matches, key=lambda item: item[0])
    if kind == "compact":
        try:
            return (
                f"{int(match.group('compact_month')):02d}/"
                f"{int(match.group('compact_day')):02d}/"
                f"{match.group('compact_year')}"
            )
        except (TypeError, ValueError):
            return ""
    if match.group("month"):
        return (
            f"{int(match.group('month')):02d}/{int(match.group('day')):02d}/"
            f"{match.group('year')}"
        )
    return (
        f"{match.group('iso_month')}/{match.group('iso_day')}/"
        f"{match.group('iso_year')}"
    )


def document_submission_label(source_document: str) -> str:
    match = SUBMISSION_PATTERN.search(str(source_document or ""))
    if not match:
        return ""
    number = int(match.group("number"))
    suffix = (
        "th" if 10 <= number % 100 <= 20
        else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    )
    return f"{number}{suffix} submission"


def normalize_date_label(value: Any) -> str:
    """Return a stable MM/DD/YYYY label when ``value`` contains a date.

    Date metadata occasionally lands in ``review_round`` (for example
    ``May 4, 2026``).  It is valid date information, but it is never a
    review round.  Keeping this parser separate prevents the round parser
    from interpreting the day as a round number.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    iso_matches = list(re.finditer(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text))
    if iso_matches:
        match = iso_matches[-1]
        try:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).strftime("%m/%d/%Y")
        except ValueError:
            return ""
    numeric_matches = list(NUMERIC_DATE_PATTERN.finditer(text))
    if numeric_matches:
        match = numeric_matches[-1]
        month = int(match.group("month"))
        day = int(match.group("day"))
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%m/%d/%Y")
        except ValueError:
            return ""
    month_matches = list(MONTH_DATE_PATTERN.finditer(text))
    if not month_matches:
        return ""
    match = month_matches[-1]
    month_text = match.group("month").casefold().rstrip(".")
    month_names = {
        "jan": 1, "january": 1, "feb": 2, "february": 2,
        "mar": 3, "march": 3, "apr": 4, "april": 4,
        "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    month = month_names.get(month_text)
    if not month:
        return ""
    try:
        return datetime(
            int(match.group("year")), month, int(match.group("day"))
        ).strftime("%m/%d/%Y")
    except ValueError:
        return ""


def embedded_date_annotation(text: Any) -> tuple[str, str, str]:
    """Split a trailing ``3/16/2026: complete`` note from display text.

    The original extraction is never changed.  The timeline can show the
    date and completion note as metadata while presenting the requirement
    itself once, instead of repeating it as if it were a new comment.
    """
    formatted = readable_evidence_text(str(text or ""))
    match = TRAILING_DATE_NOTE_PATTERN.match(formatted)
    if not match:
        return formatted, "", ""
    return (
        match.group("body").strip(),
        normalize_date_label(match.group("date")),
        match.group("note").strip().rstrip("."),
    )


def event_document_date(raw: dict[str, Any], source_document: str = "") -> str:
    """Return the best date for one timeline event/document occurrence.

    Gemini now emits a structured file date. Older rows only have lineage or
    filename metadata, so all representations are accepted here. A date in
    an event header wins over a file date; the file date is the fallback that
    lets undated rows still deduplicate correctly.
    """
    candidates: list[Any] = [
        raw.get("occurred_at"), raw.get("occurred_at_label"),
        raw.get("event_date"), raw.get("source_document_date"),
        raw.get("document_date_iso"), raw.get("document_date"),
        raw.get("source_date"), raw.get("source_date_evidence"),
    ]
    for occurrence in raw.get("source_occurrences", []) or []:
        if isinstance(occurrence, dict):
            candidates.extend([
                occurrence.get("occurred_at"), occurrence.get("occurred_at_label"),
                occurrence.get("event_date"), occurrence.get("source_document_date"),
                occurrence.get("document_date_iso"), occurrence.get("document_date"),
                occurrence.get("source_date"), occurrence.get("source_date_evidence"),
            ])
    for candidate in candidates:
        value = candidate
        if isinstance(candidate, dict):
            value = candidate.get("iso") or candidate.get("raw") or candidate.get("value")
        normalized = normalize_date_label(value)
        if normalized:
            return normalized
    return document_date_label(source_document)


def timeline_event_date_key(event: dict[str, Any], source_document: str = "") -> str:
    """Stable date key used by both server-side timeline merges and display."""
    value = event_document_date(event, source_document)
    return value or embedded_date_annotation(
        event.get("exact_text") or event.get("text")
    )[1]


def timeline_event_dates_compatible(
    first: dict[str, Any], second: dict[str, Any],
) -> bool:
    """Return whether two same-text events may be one occurrence.

    A missing date is compatible with a known date because older exports did
    not always retain the reviewer-cell timestamp.  Two different known dates
    are never merged: that is a later response/follow-up or reissued comment,
    not another copy of the same event.
    """
    left = normalize_date_label(timeline_event_date_key(first))
    right = normalize_date_label(timeline_event_date_key(second))
    if not left or not right or left == right:
        return True
    # A legacy source-row container may expose its workbook/report date while
    # the indexed copy carries the reviewer-cell event date.  The container
    # date is provenance, not a second response/follow-up.  Do not apply this
    # exception when both sides have direct event dates.
    fallback_bases = {"document_date", "workbook_export", "event_metadata"}
    direct_bases = {"event_header", "reviewer_cell", "discussion_header", "response_date"}
    first_basis = str(first.get("time_basis", "")).casefold()
    second_basis = str(second.get("time_basis", "")).casefold()
    return (
        (first_basis in fallback_bases and second_basis in direct_bases)
        or (second_basis in fallback_bases and first_basis in direct_bases)
    )


def timeline_event_rounds_compatible(
    first: dict[str, Any], second: dict[str, Any],
) -> bool:
    """Treat a missing legacy round as metadata loss, not a new event.

    Older workbook discussion rows often omitted the effective PC round even
    though the canonical indexed copy contains it.  A missing round may join
    a known round when role, text, and date match.  When the visible text and
    date are the same, different PC labels are copied-container metadata and
    must also be allowed to join; a later date remains a distinct reissue.
    """
    left = canonical_round_label(
        first.get("effective_round") or first.get("review_round"),
    )
    right = canonical_round_label(
        second.get("effective_round") or second.get("review_round"),
    )
    unknown = {"", "unknown", "pcx"}
    if left.casefold() in unknown or right.casefold() in unknown:
        return True
    left_date = normalize_date_label(timeline_event_date_key(first))
    right_date = normalize_date_label(timeline_event_date_key(second))
    if left_date and right_date and left_date == right_date:
        return True
    return left == right


def source_round_number(source_document: Any) -> int | None:
    """Infer a plan-check round from a source path, never from a date.

    Folder names such as ``3rd Round of Comments`` are stronger evidence
    than a malformed extracted field.  ``PC2`` is used only as a fallback;
    submission numbers remain separate metadata and are not treated as the
    review round.
    """
    text = str(source_document or "")
    match = ROUND_PATH_PATTERN.search(text)
    if match:
        return int(match.group("number"))
    match = PC_ROUND_PATTERN.search(Path(text).name)
    return int(match.group("number")) if match else None


def canonical_round_number(value: Any, source_document: Any = "") -> int | None:
    number = round_number(value)
    if number is not None:
        return number
    return source_round_number(source_document)


def canonical_round_label(value: Any, source_document: Any = "") -> str:
    number = canonical_round_number(value, source_document)
    return str(number) if number is not None else ""


def date_only_label(value: str) -> str:
    normalized = normalize_date_label(value)
    if normalized:
        return normalized
    match = EVENT_TIME_PATTERN.search(str(value or ""))
    return match.group(1).split()[0] if match else str(value or "").strip()


def fallback_time_label(
    review_round: str,
    source_document_date: str = "",
) -> tuple[str, str, str]:
    if source_document_date:
        return (
            f"Document date · {source_document_date}",
            "document_date",
            "document",
        )
    if review_round:
        return (
            f"Exact time not recorded · Round {review_round}",
            "review_round",
            "round_only",
        )
    return ("Exact time not recorded", "missing", "unknown")


def normalized_comment(text: str) -> str:
    return readable_text(text).casefold()


def classify_comment(text: str, discipline: str = "") -> str:
    tokens = set(re.findall(r"[a-z]+", readable_text(f"{discipline} {text}").casefold()))
    technical_score = len(tokens & TECHNICAL_TERMS)
    administrative_score = len(tokens & ADMINISTRATIVE_TERMS)
    if administrative_score > technical_score:
        return "nontechnical"
    return "technical"


def topic_tokens(text: str) -> list[str]:
    value = readable_text(text).casefold()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"\b\d+(?:[./'-]\d+)*\b", " number ", value)
    return [
        token for token in re.findall(r"[a-z]+(?:-[a-z]+)?", value)
        if len(token) > 1 and token not in TOPIC_STOP_WORDS
    ]


def topic_label(text: str) -> str:
    value = readable_text(text)
    value = re.sub(r"^comment\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(
        r"^markup\s+\S+\.(?:pdf|docx?|xlsx?)(?:\s+\w+\s+review\s+\d+)?\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value


def recurring_issue_title(text: str) -> str:
    """Create a short, evidence-derived title for a review-history card.

    This is intentionally a display label only.  The immutable comment text
    remains on every timeline event and no semantic merge is performed here.
    """
    value = readable_evidence_text(text)
    # Remove source/list prefixes only when they are actually prefixes.  The
    # previous ``[AS]`` alternative also matched the first letter of words
    # such as ``Additional`` and left numbered titles as ``1.``/``2.``.
    value = re.sub(r"^\s*\(?[A-Z]\)?(?:[.:]|\s+)\s*", "", value)
    value = re.sub(r"^\s*PC\d+\s*[-:]\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^\s*#\d+\s*(?:continue)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^\s*\d+\s*[.)]\s+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" .:-")
    first = re.split(r"(?<=[.!?])\s+|\n+", value, maxsplit=1)[0].strip()
    if ":" in first and len(first.split(":", 1)[0]) <= 42:
        first = first.split(":", 1)[1].strip()
    if len(first) > 110:
        first = f"{first[:107].rsplit(' ', 1)[0]}…"
    return first or "Recurring review issue"


def issue_event_text_identity(text: Any) -> str:
    """Use the versioned normalizer shared by repair and live projections."""
    return normalize_event_text(text)


def _generic_timeline_text(text: Any) -> bool:
    normalized = normalize_event_text(text)
    return normalized in {
        "noted", "revised", "done", "addressed", "complete", "completed",
        "see plans", "see revised", "see updated", "ok", "okay",
    } or len(normalized.split()) <= 2


def _timeline_parent_context(event: dict[str, Any]) -> set[str]:
    return {
        str(event.get(field) or "").strip().casefold()
        for field in (
            "parent_event_id", "parent_comment_id", "linked_comment_id",
            "response_id", "printed_comment_id",
        )
        if str(event.get(field) or "").strip()
    }


def event_role_family(event: dict[str, Any]) -> str:
    """Use stable government/company roles for event identity.

    ``government_comment`` and ``reviewer_follow_up`` are two display labels
    for the same reviewer-side role in different exports.  The same applies
    to ``applicant_response`` and ``current_applicant_response``.  Keeping
    the original event type on the payload preserves the audit/display label;
    the role family prevents one logical event from being rendered twice.
    """
    role = re.sub(r"\s+", " ", str(event.get("actor_role", ""))).strip().casefold()
    event_type = str(event.get("event_type", "")).strip().casefold()
    if role in {"company", "applicant", "applicant_response"} or event_type in {
        "applicant_response", "current_applicant_response",
    }:
        return "company"
    if role in {"government", "reviewer", "city"} or event_type in {
        "government_comment", "reviewer_follow_up", "discussion_note",
    }:
        return "government"
    return role or event_type or "unknown"


def timeline_event_actors_compatible(
    first: dict[str, Any], second: dict[str, Any],
) -> bool:
    """Allow missing actor metadata, but never merge conflicting actors."""
    def actor_value(event: dict[str, Any]) -> str:
        return normalize_actor(
            event.get("actor")
            or event.get("reviewer_name")
            or event.get("author")
            or event.get("reviewer")
        )

    left = actor_value(first)
    right = actor_value(second)
    return not left or not right or left == right


def _timeline_event_type_rank(event: dict[str, Any]) -> int:
    event_type = str(event.get("event_type", "")).strip().casefold()
    return {
        "government_comment": 0,
        "applicant_response": 0,
        "reviewer_follow_up": 1,
        "discussion_note": 2,
        "current_applicant_response": 1,
    }.get(event_type, 3)


def _timeline_date_quality(event: dict[str, Any]) -> int:
    """Rank direct event dates above document/file fallback dates."""
    basis = str(event.get("time_basis", "")).strip().casefold()
    return {
        "event_header": 5,
        "reviewer_cell": 5,
        "discussion_header": 5,
        "response_date": 5,
        "embedded_text_date": 4,
        "event_metadata": 3,
        "document_date": 1,
        "workbook_export": 1,
        "review_round": 0,
        "round_key": 0,
        "unknown": 0,
        "missing": 0,
    }.get(basis, 2 if timeline_event_date_key(event) else 0)


def _merge_timeline_event_payloads(
    current: dict[str, Any], incoming: dict[str, Any],
) -> dict[str, Any]:
    """Merge duplicate timeline payloads without losing provenance."""
    left_text = issue_event_text_identity(current.get("text", ""))
    right_text = issue_event_text_identity(incoming.get("text", ""))
    # Prefer a clean body over a markup-prefixed extraction; otherwise keep
    # the longer text because it usually contains the complete requirement.
    if left_text != right_text or (
        str(current.get("text", "")).lstrip().casefold().startswith("markup ")
        and not str(incoming.get("text", "")).lstrip().casefold().startswith("markup ")
    ) or len(str(incoming.get("text", ""))) > len(str(current.get("text", ""))):
        current["text"] = incoming.get("text", current.get("text", ""))
    if _timeline_event_type_rank(incoming) < _timeline_event_type_rank(current):
        for field in ("event_type", "label", "record_label", "actor_role"):
            if incoming.get(field):
                current[field] = incoming[field]
    labels = [
        *(current.get("record_labels") or []),
        current.get("record_label"),
        *(incoming.get("record_labels") or []),
        incoming.get("record_label"),
    ]
    current["record_labels"] = list(dict.fromkeys(
        str(value).strip() for value in labels if str(value or "").strip()
    ))
    current["actor"] = current.get("actor") or incoming.get("actor", "")
    current["occurred_at"] = current.get("occurred_at") or incoming.get("occurred_at", "")
    current["occurred_at_label"] = current.get("occurred_at_label") or incoming.get("occurred_at_label", "")

    combined_sources = [*(current.get("sources") or []), *(incoming.get("sources") or [])]
    unique_sources: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    for source in combined_sources:
        if not isinstance(source, dict):
            continue
        source_key = source_reference_identity(source)
        if not source_key or source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        copied = dict(source)
        copied["relation"] = "Primary source" if not unique_sources else "Also appears in"
        unique_sources.append(copied)
    current["sources"] = unique_sources
    current["source"] = unique_sources[0] if unique_sources else current.get("source") or incoming.get("source")
    current["submissions"] = sorted(set(
        current.get("submissions") or []
    ) | set(incoming.get("submissions") or []) | {
        value for value in (current.get("submission"), incoming.get("submission")) if value
    })
    current["merged_event_ids"] = list(dict.fromkeys([
        *(current.get("merged_event_ids") or []),
        *(incoming.get("merged_event_ids") or []),
        str(incoming.get("event_id", "")),
    ]))
    # Prefer an exact event date over a document/file date.  If both are the
    # same quality, retain the earliest known date so the timeline starts at
    # the first observed occurrence.  Preserve all alternatives for audit.
    date_values = [
        timeline_event_date_key(current), timeline_event_date_key(incoming),
    ]
    variants = list(current.get("date_variants") or [])
    variants.extend(value for value in date_values if value)
    current["date_variants"] = list(dict.fromkeys(variants))
    current_quality = _timeline_date_quality(current)
    incoming_quality = _timeline_date_quality(incoming)
    if incoming_quality > current_quality or (
        incoming_quality == current_quality
        and timeline_event_date_key(incoming)
        and (
            not timeline_event_date_key(current)
            or timeline_event_date_key(incoming) < timeline_event_date_key(current)
        )
    ):
        for field in (
            "time_label", "time_basis", "time_precision", "source_date",
            "document_date", "document_date_iso", "document_date_source",
            "embedded_date", "embedded_date_note",
        ):
            if incoming.get(field) not in (None, "", {}):
                current[field] = incoming[field]
    else:
        for field in (
            "time_label", "time_basis", "time_precision", "source_date",
            "document_date", "document_date_iso", "document_date_source",
            "embedded_date", "embedded_date_note",
        ):
            if current.get(field) in (None, "", {}):
                current[field] = incoming.get(field, current.get(field))
    return current


def merge_timeline_event_occurrences(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-role, same-date, same-content timeline copies.

    Missing dates are treated as extraction/container-date variants, but two
    different known dates remain separate.  A different PC label on the same
    dated, highly similar text is a copied container label and is unioned into
    one event; government/company roles never merge.
    """
    merged: list[dict[str, Any]] = []
    # Bucket by role/text and check round separately. This lets legacy
    # discussion rows (which frequently lack a round) merge into their
    # indexed copies without merging known PC1/PC2 reissues.  A second role
    # bucket is used only for the conservative short-prefix enrichment case:
    # old exports sometimes store a truncated response beside a complete
    # response on the same dated row.  Source observations remain attached
    # to the surviving event; no source file is discarded.
    by_identity: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_role: dict[str, list[int]] = defaultdict(list)
    for event in events:
        if not isinstance(event, dict):
            continue
        text = issue_event_text_identity(event.get("text", ""))
        if not text:
            continue
        role = event_role_family(event)
        identity = (role, text)
        index = None
        for candidate in by_identity.get(identity, []):
            if (
                timeline_event_rounds_compatible(merged[candidate], event)
                and timeline_event_dates_compatible(merged[candidate], event)
                and timeline_event_actors_compatible(merged[candidate], event)
            ):
                if _generic_timeline_text(event.get("text", "")) and not (
                    _timeline_parent_context(merged[candidate])
                    & _timeline_parent_context(event)
                ):
                    continue
                index = candidate
                break
        # A short extraction and a complete extraction can have different
        # normalized text.  Only merge the strict prefix/enrichment pattern,
        # with compatible role, round, date, and actor.  Similar-looking
        # requirements, changed dimensions, or changed negations remain
        # separate events.
        if index is None:
            for candidate in by_role.get(role, []):
                existing = merged[candidate]
                if not (
                    timeline_event_rounds_compatible(existing, event)
                    and timeline_event_dates_compatible(existing, event)
                    and timeline_event_actors_compatible(existing, event)
                ):
                    continue
                match_class, _signals = classify_event_text_match(
                    existing.get("text", ""), event.get("text", ""),
                )
                if match_class == "HIGH_CONFIDENCE_DUPLICATE" or high_confidence_text_extension(existing.get("text", ""), event.get("text", "")) or high_confidence_text_extension(event.get("text", ""), existing.get("text", "")):
                    index = candidate
                    break
        if index is None:
            copied = dict(event)
            copied["sources"] = list(event.get("sources") or [])
            copied["merged_event_ids"] = list(event.get("merged_event_ids") or [])
            by_identity[identity].append(len(merged))
            by_role[role].append(len(merged))
            merged.append(copied)
            continue
        merged[index] = _merge_timeline_event_payloads(merged[index], event)
    return merged


def merge_duplicate_issue_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge same-date copies while retaining every source occurrence.

    When no reliable date exists, the effective round remains the fallback
    boundary.  A known same-date event wins over conflicting copied PC labels.
    """
    merged: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str], int] = {}
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event_type = str(raw.get("event_type", "discussion_note"))
        source_document = str(raw.get("source_document", "")).strip()
        if not source_document:
            source_document = next((
                str(occurrence.get("source_document", "")).strip()
                for occurrence in raw.get("source_occurrences", []) or []
                if isinstance(occurrence, dict)
                and str(occurrence.get("source_document", "")).strip()
            ), "")
        effective_round = canonical_round_label(
            raw.get("effective_round") or raw.get("review_round"),
            source_document,
        )
        event_date = event_document_date(raw, source_document)
        normalized_date = normalize_date_label(event_date)
        _display_text, embedded_date, _embedded_note = embedded_date_annotation(
            raw.get("exact_text") or raw.get("text")
        )
        event_date_key = normalized_date or embedded_date or event_date
        key = (
            event_role_family(raw),
            (f"date:{event_date_key}" if event_date_key else f"round:{effective_round}"),
            issue_event_text_identity(raw.get("exact_text") or raw.get("text")),
        )
        existing_index = by_key.get(key)
        if existing_index is None:
            copied = dict(raw)
            copied["source_occurrences"] = list(raw.get("source_occurrences", []) or [])
            copied["merged_event_ids"] = list(raw.get("merged_event_ids", []) or [])
            copied["merged_event_types"] = [event_type]
            by_key[key] = len(merged)
            merged.append(copied)
            continue
        existing = merged[existing_index]
        occurrences = [*(existing.get("source_occurrences") or []), *(raw.get("source_occurrences") or [])]
        unique_occurrences: list[dict[str, Any]] = []
        seen_occurrences: set[tuple[str, str, str]] = set()
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                continue
            occurrence_key = (
                str(occurrence.get("comment_id", "")),
                str(occurrence.get("source_document", "")).casefold(),
                str(occurrence.get("source_location", "")),
            )
            if occurrence_key in seen_occurrences:
                continue
            seen_occurrences.add(occurrence_key)
            unique_occurrences.append(occurrence)
        existing["source_occurrences"] = unique_occurrences
        existing["merged_event_types"] = list(dict.fromkeys([
            *(existing.get("merged_event_types") or [str(existing.get("event_type", ""))]),
            event_type,
        ]))
        if _timeline_event_type_rank(raw) < _timeline_event_type_rank(existing):
            existing["event_type"] = event_type
            existing["actor_role"] = raw.get("actor_role", existing.get("actor_role", ""))
            existing["actor"] = existing.get("actor") or raw.get("actor", "")
        existing["merged_event_ids"] = list(dict.fromkeys([
            *(existing.get("merged_event_ids") or []),
            *(raw.get("merged_event_ids") or []),
            str(raw.get("event_id", "")),
        ]))
    return merged


def recurring_issue_explanation(
    events: list[dict[str, Any]], status: str, round_count: int,
) -> str:
    """Explain persistence using only language present in the issue history."""
    history_span = (
        f"{round_count} review rounds"
        if round_count > 1
        else "multiple review events within one round"
    )
    comments = " ".join(str(event.get("comment_text", "")) for event in events)
    responses = [
        str(event.get("response_text", "")).strip()
        for event in events if str(event.get("response_text", "")).strip()
    ]
    combined = comments.casefold()
    generic_responses = sum(bool(re.match(
        r"^(?:noted\.?|updated\.?|revised\.?|see\s+(?:updated|revised)|"
        r"please\s+(?:see|refer)|refer\s+to\b)",
        response.casefold(),
    )) or len(response.split()) <= 4 for response in responses)
    if re.search(r"\bnot addressed\b|\bnot acceptable\b", combined):
        return (
            "Reviewer follow-up says the prior response did not fully address "
            "the requirement or did not identify the revision clearly."
        )
    if generic_responses:
        return (
            f"The issue continued across {history_span}; at least one "
            "recorded response was brief or referred generally to revised "
            "documents, and the requirement appeared again later."
        )
    if any(event.get("relationship_to_previous") == "response_rejected" for event in events):
        return (
            "A later reviewer follow-up kept the requirement open after an "
            "earlier response."
        )
    if any(event.get("relationship_to_previous") == "exact_reissue" for event in events):
        return (
            f"The same requirement was reissued across {history_span} "
            "without explicit resolution evidence."
        )
    if status == "resolved":
        return (
            f"The requirement appeared across {history_span} before the "
            "source record was explicitly marked responded."
        )
    if status == "open":
        return (
            f"The issue has {history_span}, and the latest "
            "stored history still contains a reviewer follow-up."
        )
    return (
        f"The issue has {history_span}, but the stored "
        "sources do not explicitly record a final resolution."
    )


def round_number(value: Any) -> int | None:
    text = str(value or "").strip().casefold()
    # A date accidentally stored in the review-round field is date metadata,
    # not a round.  In particular, ``May 4, 2026`` must never become round 4.
    if normalize_date_label(text):
        return None
    if text in {"initial", "initial review", "first", "first review", "first round"}:
        return 1
    match = PC_ROUND_PATTERN.search(text)
    if match:
        return int(match.group("number"))
    match = ROUND_PATH_PATTERN.search(text)
    if match:
        return int(match.group("number"))
    match = re.search(r"\b(?:round|review\s+round|plan\s*check)\s*[-:#]?\s*(\d+)\b", text)
    if match:
        return int(match.group(1))
    # Keep a plain numeric value supported for legacy rows, but do not scrape
    # arbitrary digits from filenames, dates, or prose.
    match = re.fullmatch(r"\d+", text)
    return int(match.group(0)) if match else None


def source_reference_identity(source: dict[str, Any]) -> str:
    """Return one display identity per original file, not per locator."""
    if source.get("kind") == "external":
        return f"url:{str(source.get('url', '')).strip()}"
    location = source.get("location", {})
    document_id = str(location.get("document_id", "")) if isinstance(location, dict) else ""
    if document_id:
        return f"document:{document_id}"
    filename = str(source.get("filename", "")).strip().casefold()
    return f"filename:{filename}" if filename else f"source:{source.get('source_id', '')}"


def topic_similarity(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    left_counts, right_counts = Counter(left), Counter(right)
    shared = sum(min(left_counts[token], right_counts[token]) for token in left_counts.keys() & right_counts.keys())
    total = sum(max(left_counts[token], right_counts[token]) for token in left_counts.keys() | right_counts.keys())
    return shared / total if total else 0.0


def tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", (text or "").casefold())
        if len(token) > 1 and token not in STOP_WORDS
    ]


def compact_path(path: str) -> str:
    names = [Path(part).name for part in re.split(r"\s+\|\s+", path or "") if part.strip()]
    return " + ".join(names)


def canonical_event_projection_key(record: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Build the runtime event identity used by the application list.

    Exact same-date/round text copies are one event; their physical source
    occurrences remain attached to that event.  If the date is unavailable we
    keep the round and site in the key, and if those are also unavailable we
    scope the fallback to the source path rather than guessing across files.
    """
    text = issue_event_text_identity(
        record.get("verified_text") or record.get("original_text") or ""
    )
    if not text:
        return None
    city = re.sub(r"\s+", " ", str(record.get("city") or "unknown")).strip().casefold()
    site = re.sub(r"\s+", " ", str(
        record.get("site_id") or record.get("project_id") or
        record.get("property_project") or record.get("site") or "unknown"
    )).strip().casefold()
    round_value = re.sub(r"\s+", " ", str(
        record.get("reviewed_plan_round") or record.get("review_round") or ""
    )).strip().casefold()
    date_value = str(
        record.get("event_date_iso") or record.get("event_date") or
        record.get("source_document_date") or record.get("document_date_iso") or
        record.get("source_date_evidence") or ""
    ).strip().casefold()
    if not city or not site or not round_value:
        source = str(record.get("source_document") or "").split(" | ", 1)[0].strip().casefold()
        return (city, site, round_value or source, text)
    return (city, site, f"{round_value}|{date_value or 'unknown'}", text)


def _projection_occurrence(record: dict[str, Any], owner_id: str) -> dict[str, Any]:
    """Return a compact source occurrence safe to expose through the API."""
    source = str(record.get("source_document") or "").split(" | ", 1)[0].strip()
    locator = record.get("source_locator_json")
    if not isinstance(locator, dict):
        locator = {}
    occurrence_key = "|".join((
        source,
        str(record.get("source_page") or record.get("source_page_start") or ""),
        str(record.get("source_row") or ""),
        json.dumps(locator, ensure_ascii=False, sort_keys=True),
    ))
    occurrence_id = "SO-" + hashlib.sha256(occurrence_key.encode("utf-8")).hexdigest()[:20]
    return {
        "source_occurrence_id": occurrence_id,
        "owner_id": owner_id,
        "source_document": source,
        "source_page": record.get("source_page") or record.get("source_page_start") or "",
        "source_page_end": record.get("source_page_end") or "",
        "source_sheet": record.get("source_sheet") or "",
        "source_row": record.get("source_row") or "",
        "source_cell_range": record.get("source_cell_range") or "",
        "source_locator_json": locator,
        "source_document_date": record.get("source_document_date") or record.get("document_date_iso") or "",
        "exact_text": str(record.get("verified_text") or record.get("original_text") or ""),
    }


class DatasetStore:
    def __init__(
        self,
        dataset_path: Path,
        categories_path: Path,
        source_root: Path,
        source_registry_path: Path | None = None,
        preview_root: Path | None = None,
        enrichment_path: Path | None = None,
        search_index_path: Path | None = None,
        document_authorizer: Any = None,
        gemini_client: GeminiClient | None = None,
        knowledge_gemini_client: GeminiClient | None = None,
        link_reviews_path: Path | None = None,
        workbook_reviews_path: Path | None = None,
        knowledge_router_client: GeminiClient | None = None,
    ):
        self.dataset_path = dataset_path.resolve()
        self.categories_path = categories_path.resolve()
        self.source_root = source_root.resolve()
        self.enrichment_path = (enrichment_path or self.categories_path.parent / "gemini_enrichment.json").resolve()
        self.link_reviews_path = (link_reviews_path or self.categories_path.parent / "link_review_decisions.json").resolve()
        self.workbook_reviews_path = (
            workbook_reviews_path
            or self.categories_path.parent / "workbook_review_decisions.json"
        ).resolve()
        # Tag classifications are a rebuildable projection, never part of the
        # immutable canonical dataset.  Administrative decisions live in a
        # small sidecar so deleting/rebuilding the index cannot alter facts.
        self.tag_suggestions_path = (
            self.categories_path.parent / "tag_suggestions.json"
        ).resolve()
        self.gemini_client = gemini_client
        self.knowledge_gemini_client = knowledge_gemini_client
        self.knowledge_router_client = knowledge_router_client
        self.search_index = SearchIndex(search_index_path or self.categories_path.parent / "search_index.json")
        self._search_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._dataset_mtime_ns = -1
        self._comments: list[dict[str, Any]] = []
        self._all_comments: list[dict[str, Any]] = []
        self._document_identity: dict[str, Any] = {}
        self._issue_event_index: dict[str, dict[str, Any]] = {}
        self._comments_by_id: dict[str, dict[str, Any]] = {}
        self._responses_by_id: dict[str, dict[str, Any]] = {}
        self._links_by_comment: dict[str, dict[str, Any]] = {}
        self._assignments: dict[str, str] = {}
        self._analysis_cache: dict[str, dict[str, Any]] = {}
        self._enrichment_entries: dict[str, dict[str, Any]] = {}
        self._link_review_decisions: dict[str, dict[str, Any]] = {}
        self._workbook_review_decisions: dict[str, dict[str, Any]] = {}
        self._tag_suggestions: dict[str, dict[str, Any]] = {}
        self._progressive_telemetry: list[dict[str, Any]] = []
        self.source_registry = SourceRegistry(
            self.dataset_path,
            self.source_root,
            source_registry_path or self.categories_path.parent / "source_registry.json",
            preview_root or self.categories_path.parent / "previews",
            authorizer=document_authorizer,
        )
        self.reload(force=True)
        self._load_categories()
        self._load_enrichment()
        self._load_link_reviews()
        self._load_workbook_reviews()
        self._load_tag_suggestions()
        self._sync_search_index()
        self.knowledge_chat = KnowledgeChat(self)

    def _load_link_reviews(self) -> None:
        with self._lock:
            if not self.link_reviews_path.is_file():
                self._link_review_decisions = {}
                return
            payload = json.loads(self.link_reviews_path.read_text(encoding="utf-8"))
            decisions = payload.get("decisions", {})
            known_link_ids = {str(row.get("link_id", "")) for row in self._links_by_comment.values()}
            self._link_review_decisions = {
                str(link_id): value for link_id, value in decisions.items()
                if link_id in known_link_ids and isinstance(value, dict)
                and value.get("decision") in {"confirmed", "rejected", "needs_followup"}
            } if isinstance(decisions, dict) else {}

    def _save_link_reviews(self) -> None:
        self.link_reviews_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": "1.0", "decisions": dict(sorted(self._link_review_decisions.items()))}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.link_reviews_path.parent,
            prefix="link-reviews-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.link_reviews_path)

    def _load_workbook_reviews(self) -> None:
        with self._lock:
            if not self.workbook_reviews_path.is_file():
                self._workbook_review_decisions = {}
                return
            payload = json.loads(
                self.workbook_reviews_path.read_text(encoding="utf-8")
            )
            decisions = payload.get("decisions", {})
            self._workbook_review_decisions = {
                str(source): value
                for source, value in decisions.items()
                if isinstance(value, dict)
                and value.get("decision") in {"confirmed", "needs_followup"}
            } if isinstance(decisions, dict) else {}

    def _save_workbook_reviews(self) -> None:
        self.workbook_reviews_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "decisions": dict(sorted(self._workbook_review_decisions.items())),
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.workbook_reviews_path.parent,
            prefix="workbook-reviews-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            json.dump(
                payload, stream, ensure_ascii=False, indent=2, sort_keys=True,
            )
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.workbook_reviews_path)

    def _load_tag_suggestions(self) -> None:
        """Load review decisions for the rebuildable validated tag index."""
        with self._lock:
            if not self.tag_suggestions_path.is_file():
                self._tag_suggestions = {}
                return
            try:
                payload = json.loads(self.tag_suggestions_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._tag_suggestions = {}
                return
            suggestions = payload.get("suggestions", {}) if isinstance(payload, dict) else {}
            self._tag_suggestions = {
                str(key): value for key, value in suggestions.items()
                if isinstance(value, dict)
                and value.get("status") in {"confirmed", "rejected", "suggested"}
            } if isinstance(suggestions, dict) else {}

    def _save_tag_suggestions(self) -> None:
        self.tag_suggestions_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "validated-tag-decisions-v1",
            "suggestions": dict(sorted(self._tag_suggestions.items())),
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.tag_suggestions_path.parent,
            prefix="tag-suggestions-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.tag_suggestions_path)

    def _tag_overlaid_rows(self) -> list[dict[str, Any]]:
        """Return canonical rows with only admin-confirmed tag overlays.

        The source rows are copied and never mutated.  This makes the tag
        index disposable: an index rebuild changes retrieval only, not the
        canonical event, timeline, response, or source-occurrence data.
        """
        rows: list[dict[str, Any]] = []
        for source in self._comments:
            row = dict(source)
            event_id = str(row.get("canonical_event_id") or row.get("comment_id") or "")
            accepted_event_ids = {
                event_id,
                str(row.get("comment_id") or ""),
                str(row.get("canonical_comment_id") or ""),
            }
            event_tags = list(row.get("event_tags") or []) if isinstance(row.get("event_tags"), list) else []
            issue_tags = list(row.get("issue_tags") or []) if isinstance(row.get("issue_tags"), list) else []
            for suggestion in self._tag_suggestions.values():
                if str(suggestion.get("status")) != "confirmed":
                    continue
                if str(suggestion.get("event_id") or "") not in accepted_event_ids:
                    continue
                tag = str(suggestion.get("suggested_tag") or suggestion.get("tag_id") or "").strip()
                level = str(suggestion.get("tag_level") or "issue").strip()
                if not tag:
                    continue
                target = issue_tags if level == "issue" else event_tags
                if tag not in target:
                    target.append(tag)
            if event_tags:
                row["event_tags"] = event_tags
            if issue_tags:
                row["issue_tags"] = issue_tags
            rows.append(row)
        return rows

    def tag_suggestions(self) -> dict[str, Any]:
        self.reload()
        return {
            "schema_version": "validated-tag-decisions-v1",
            "suggestions": [dict(value, suggestion_id=key) for key, value in sorted(self._tag_suggestions.items())],
        }

    def set_tag_suggestion(self, suggestion_id: str, decision: str) -> dict[str, Any]:
        self.reload()
        decision = str(decision or "").casefold().strip()
        if decision not in {"confirmed", "rejected"}:
            raise ValueError("Tag decision must be confirmed or rejected")
        suggestion_id = str(suggestion_id or "").strip()
        if not suggestion_id:
            raise ValueError("suggestion_id is required")
        current = dict(self._tag_suggestions.get(suggestion_id, {}))
        if not current:
            raise KeyError(suggestion_id)
        current["status"] = decision
        current["reviewed_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        self._tag_suggestions[suggestion_id] = current
        self._save_tag_suggestions()
        return {"suggestion_id": suggestion_id, **current}

    def _effective_link_status(self, link: dict[str, Any]) -> str:
        decision = self._link_review_decisions.get(str(link.get("link_id", "")), {})
        return str(decision.get("decision") or link.get("review_status", "not_applicable"))

    def _sync_search_index(self) -> dict[str, int]:
        return self.search_index.sync(
            self._comments,
            lambda row: (
                comment_display_parts(
                    verified_text(row),
                    str(row.get("source_document", "")),
                    str(row.get("comment_number", "")),
                )[0]
                if row.get("text_trust_status") == "verified"
                else (
                    self._enrichment_for(str(row["comment_id"]), row).get("display_text")
                    or readable_text(row.get("original_text", ""))
                )
            ),
            lambda comment_id: self._assignments.get(comment_id, "Uncategorized"),
            lambda comment_id: (
                self._effective_link_status(self._links_by_comment.get(comment_id, {})) == "confirmed"
                or self._responses_by_id.get(self._comments_by_id.get(comment_id, {}).get("response_id", ""), {}).get("human_review_status") == "confirmed"
            ),
        )

    def _attach_canonical_event_projection(
        self,
        all_comments: list[dict[str, Any]],
        canonical_comments: list[dict[str, Any]],
    ) -> None:
        """Annotate the runtime rows with one event and all source occurrences.

        The JSON dataset remains immutable at this point: this is an in-memory
        projection used by the list/detail APIs.  It closes the gap between
        the existing duplicate suppressor and the normalized evidence model,
        without making the frontend render one row per physical file.
        """
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        for row in all_comments:
            key = canonical_event_projection_key(row)
            if key is not None:
                groups.setdefault(key, []).append(row)
        canonical_ids = {str(row.get("comment_id", "")) for row in canonical_comments}
        for key, rows in groups.items():
            members = [row for row in rows if str(row.get("comment_id", "")) in canonical_ids]
            winner = members[0] if members else rows[0]
            key_json = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            event_id = "CE-" + hashlib.sha256(key_json.encode("utf-8")).hexdigest()[:20]
            occurrences: list[dict[str, Any]] = []
            seen: set[tuple[str, str, str, str]] = set()
            for row in rows:
                owner_id = str(row.get("comment_id", ""))
                if not owner_id:
                    continue
                candidate = _projection_occurrence(row, owner_id)
                identity = (
                    str(candidate.get("source_document", "")).casefold(),
                    str(candidate.get("source_page", "")),
                    str(candidate.get("source_row", "")),
                    json.dumps(candidate.get("source_locator_json", {}), sort_keys=True),
                )
                if identity not in seen:
                    seen.add(identity)
                    occurrences.append(candidate)
                for existing in row.get("source_occurrences", []) or []:
                    if not isinstance(existing, dict):
                        continue
                    merged = dict(row)
                    merged.update(existing)
                    occurrence = _projection_occurrence(merged, str(existing.get("owner_id") or owner_id))
                    identity = (
                        str(occurrence.get("source_document", "")).casefold(),
                        str(occurrence.get("source_page", "")),
                        str(occurrence.get("source_row", "")),
                        json.dumps(occurrence.get("source_locator_json", {}), sort_keys=True),
                    )
                    if identity not in seen:
                        seen.add(identity)
                        occurrences.append(occurrence)
            source_documents = sorted({
                str(item.get("source_document", "")).strip()
                for item in occurrences if str(item.get("source_document", "")).strip()
            })
            for row in members:
                row["canonical_event_id"] = event_id
                row["canonical_event_comment_count"] = 1
                row["canonical_event_source_count"] = len(source_documents)
                row["canonical_event_occurrence_count"] = len(occurrences)
                row["canonical_event_member_ids"] = [str(item.get("comment_id", "")) for item in rows if item.get("comment_id")]
                row["canonical_event_source_documents"] = source_documents
                row["canonical_event_source_occurrences"] = occurrences
                row["canonical_event_date"] = str(
                    winner.get("event_date_iso") or winner.get("event_date") or
                    winner.get("source_document_date") or winner.get("document_date_iso") or ""
                )
                row["canonical_event_date_provenance"] = winner.get("document_date_provenance") or winner.get("document_date") or {}
                row["canonical_event_round_provenance"] = winner.get("review_round_metadata") or {
                    "value": winner.get("reviewed_plan_round") or winner.get("review_round") or "",
                    "source": winner.get("review_round_source") or "record_field",
                    "confidence": winner.get("round_confidence", 0.0),
                }

    def reload(self, force: bool = False) -> None:
        with self._lock:
            stat = self.dataset_path.stat()
            if not force and stat.st_mtime_ns == self._dataset_mtime_ns:
                return
            data = json.loads(self.dataset_path.read_text(encoding="utf-8"))
            comments = data.get("comments", [])
            responses = data.get("responses", [])
            links = data.get("comment_response_links", [])
            # City spelling is a runtime projection.  Preserve the source
            # dataset on disk, but prevent case/accent variants from splitting
            # one municipality across selectors, summaries, search, and chat.
            for row in comments:
                row["city"] = canonical_city_name(row.get("city"))
            comment_ids = [row["comment_id"] for row in comments]
            if len(comment_ids) != len(set(comment_ids)):
                raise ValueError("Dataset contains duplicate comment IDs")
            links_by_comment = {row["comment_id"]: row for row in links}
            self._all_comments = comments
            issue_event_index = data.get("issue_event_index", {})
            self._issue_event_index = (
                issue_event_index if isinstance(issue_event_index, dict) else {}
            )
            # Document identity is persisted by the ingestion/repair tools.
            # Reusing it here avoids re-running the corpus-wide near-duplicate
            # comparison on every server start (which can take minutes for a
            # large source folder).  Fall back to the in-memory canonicalizer
            # for legacy or freshly imported datasets that do not have the
            # persisted identity fields yet.
            persisted_identity = {
                "source_files": data.get("source_files", {}),
                "canonical_documents": data.get("canonical_documents", {}),
                "source_file_aliases": data.get("source_file_aliases", []),
                "near_duplicate_review": data.get("near_duplicate_review", []),
                "canonical_document_count": len(data.get("canonical_documents", {}) or {}),
                "physical_source_file_count": len(data.get("source_files", {}) or {}),
            }
            has_persisted_identity = bool(
                persisted_identity["canonical_documents"]
                and persisted_identity["source_files"]
                and all(
                    row.get("source_file_id") and row.get("canonical_document_id")
                    for row in comments
                    if str(row.get("source_document", "")).strip()
                )
            )
            # Annotate physical files and extracted rows before any runtime
            # search/topic deduplication.  This is deliberately in-memory so
            # opening the app never mutates the production dataset; the
            # canonicalize_documents CLI persists the same registry during
            # ingestion or an explicit repair.
            self._document_identity = (
                persisted_identity
                if has_persisted_identity
                else canonicalize_documents(comments)
            )
            searchable = [
                row for row in comments
                if searchable_comment(row, links_by_comment.get(row["comment_id"]))
                and not is_reference_note(row)
            ]
            # Runtime guard: ingestion mistakes must never create repeated list,
            # summary, search-index, or knowledge-chat evidence.
            self._comments, self._duplicate_comments = find_duplicate_comments(searchable, links)
            self._attach_canonical_event_projection(self._all_comments, self._comments)
            self._comments_by_id = {row["comment_id"]: row for row in self._comments}
            self._responses_by_id = {row["response_id"]: row for row in responses}
            self._links_by_comment = links_by_comment
            self._dataset_mtime_ns = stat.st_mtime_ns
            self._analysis_cache = {}

    def _load_categories(self) -> None:
        with self._lock:
            if not self.categories_path.is_file():
                self._assignments = {}
                return
            payload = json.loads(self.categories_path.read_text(encoding="utf-8"))
            assignments = payload.get("assignments", {})
            self._assignments = {
                comment_id: str(category)
                for comment_id, category in assignments.items()
                if comment_id in self._comments_by_id and str(category).strip()
            }

    def _load_enrichment(self) -> None:
        with self._lock:
            if not self.enrichment_path.is_file():
                self._enrichment_entries = {}
                return
            payload = json.loads(self.enrichment_path.read_text(encoding="utf-8"))
            entries = payload.get("entries", {})
            self._enrichment_entries = entries if isinstance(entries, dict) else {}

    def _enrichment_for(self, record_id: str, record: dict[str, Any]) -> dict[str, Any]:
        entry = self._enrichment_entries.get(record_id, {})
        if entry.get("input_sha256") != record_digest(record):
            return {}
        return entry

    def _save_categories(self) -> None:
        self.categories_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1.0",
            "assignments": dict(sorted(self._assignments.items())),
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.categories_path.parent,
            prefix="categories-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.categories_path)

    def set_category(self, comment_ids: list[str], category: str) -> dict[str, Any]:
        category = re.sub(r"\s+", " ", category).strip()
        if len(category) > 80:
            raise ValueError("Category must be 80 characters or fewer")
        if not comment_ids or len(comment_ids) > 500:
            raise ValueError("Choose between 1 and 500 comments")
        unknown = [comment_id for comment_id in comment_ids if comment_id not in self._comments_by_id]
        if unknown:
            raise ValueError(f"Unknown comment ID: {unknown[0]}")
        with self._lock:
            for comment_id in comment_ids:
                if category:
                    self._assignments[comment_id] = category
                else:
                    self._assignments.pop(comment_id, None)
            self._save_categories()
            self._sync_search_index()
            self._search_cache.clear()
        return {"updated": len(comment_ids), "category": category}

    def cities(self) -> list[dict[str, Any]]:
        counts = Counter(canonical_city_name(row.get("city")) for row in self._comments)
        return [{"name": city, "count": counts[city]} for city in sorted(counts)]

    def categories(self, city: str = "") -> list[dict[str, Any]]:
        city = canonical_city_name(city) if city else ""
        counts: Counter[str] = Counter()
        for comment in self._comments:
            if city and comment["city"] != city:
                continue
            value = self._assignments.get(comment["comment_id"], "Uncategorized")
            counts[value] += 1
        return [{"name": name, "count": counts[name]} for name in sorted(counts)]

    def _event_source_references(
        self,
        event: dict[str, Any],
        occurrence: dict[str, Any],
        text: str,
    ) -> list[dict[str, Any]]:
        """Resolve a timeline event to the source that contains that event.

        A discussion event is stored on its parent comment for historical
        reasons, so looking up every event by ``comment_id`` makes an
        applicant response inherit the comment cell (for example C6).  The
        response record is the authoritative owner for an applicant response
        and normally points at the response cell (for example E6).  If an
        older workbook has no standalone response record, use its discussion
        cell (for example F6), never the government-comment cell.
        """
        event_type = str(event.get("event_type", "")).casefold().strip()
        owner_id = str(occurrence.get("comment_id", "")).strip()
        occurrence_filename = Path(
            str(occurrence.get("source_document", ""))
        ).name.casefold()
        event_location = event.get("source_location")
        occurrence_location = occurrence.get("source_location")
        target_cell_range = str(
            (event_location if isinstance(event_location, dict) else {}).get(
                "cell_range", ""
            )
            or (occurrence_location if isinstance(occurrence_location, dict) else {}).get(
                "cell_range", ""
            )
        ).strip().casefold()

        comment = getattr(self, "_comments_by_id", {}).get(owner_id)
        if comment is None:
            comment = next(
                (
                    row for row in getattr(self, "_all_comments", [])
                    if str(row.get("comment_id", "")) == owner_id
                ),
                {},
            )
        response_id = str(
            occurrence.get("response_id")
            or (comment.get("response_id", "") if isinstance(comment, dict) else "")
        ).strip()

        def references(owner: str) -> list[dict[str, Any]]:
            if not owner:
                return []
            if not hasattr(self, "source_registry"):
                return self._source_references(owner, text)
            # ``_source_references`` intentionally collapses all locators in
            # one physical document into one library link.  That is correct
            # for the source list, but not for event routing: C6 (comment),
            # E6 (response), and F6 (discussion) can all live in the same
            # workbook.  Keep the registry's source rows distinct here so a
            # structural locator can select the right cell.
            rows: list[dict[str, Any]] = []
            seen_source_ids: set[str] = set()
            for source in self.source_registry.sources_for_owner(owner):
                source_id = str(source.get("source_id", ""))
                if source_id and source_id in seen_source_ids:
                    continue
                if source_id:
                    seen_source_ids.add(source_id)
                copied = dict(source)
                copied["kind"] = "local"
                copied["filename"] = copied.get("document", {}).get("filename", "")
                rows.append(copied)
            return rows

        def filename_matches(source: dict[str, Any]) -> bool:
            return bool(occurrence_filename) and str(
                source.get("filename", "")
            ).casefold() == occurrence_filename

        def pick(
            rows: list[dict[str, Any]],
            *,
            exact_filename: bool = True,
            allowed_relations: set[str] | None = None,
            include_primary: bool = True,
        ) -> list[dict[str, Any]]:
            selected: list[dict[str, Any]] = []
            for source in rows:
                relation = str(source.get("relation", "")).casefold()
                if allowed_relations is not None and relation not in allowed_relations:
                    continue
                if not include_primary and relation == "primary source":
                    continue
                if exact_filename and not filename_matches(source):
                    continue
                selected.append(source)
            return selected

        # Response records carry their own precise E-column locator.  Prefer
        # that owner before looking at the parent comment's F-column history.
        if event_type in {"applicant_response", "current_applicant_response"}:
            response_sources = references(response_id)
            selected = pick(response_sources)
            if selected:
                return selected
            selected = pick(response_sources, exact_filename=False)
            if selected:
                return selected

            comment_sources = references(owner_id)
            discussion_relations = {
                "prior applicant response",
                "discussion history",
                "discussion",
            }
            selected = pick(
                comment_sources,
                allowed_relations=discussion_relations,
                include_primary=False,
            )
            if selected:
                return selected
            # Older issue rows can have a response relation even when the
            # registry did not preserve the relation spelling exactly.
            return pick(
                comment_sources,
                exact_filename=False,
                allowed_relations=discussion_relations,
                include_primary=False,
            )

        comment_sources = references(owner_id)
        if event_type == "reviewer_follow_up":
            # Legacy rows may have stored the follow-up relation under a
            # generic relation (or under the prior-response owner), but the
            # event locator still identifies the discussion cell.  Use that
            # structural locator before relation labels so F6 cannot fall
            # back to the government comment cell C6.
            if target_cell_range:
                selected = [
                    source for source in comment_sources
                    if str((source.get("location") or {}).get("cell_range", ""))
                    .casefold() == target_cell_range
                    and (not occurrence_filename or filename_matches(source))
                ]
                if selected:
                    return selected
            selected = pick(
                comment_sources,
                allowed_relations={"reviewer follow-up", "discussion history", "discussion"},
            )
            if selected:
                return selected
        elif event_type == "government_comment":
            selected = pick(
                comment_sources,
                allowed_relations={"government comment", "primary source"},
            )
            if selected:
                return selected
        selected = pick(comment_sources)
        if selected:
            return selected
        return pick(comment_sources, exact_filename=False)

    def _indexed_issue_events(
        self, comment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Render one canonical event union with every supporting source."""
        thread_id = str(comment.get("issue_thread_id", ""))
        thread = self._issue_event_index.get(thread_id, {})
        raw_events = thread.get("events", []) if isinstance(thread, dict) else []
        raw_events = merge_duplicate_issue_events(
            raw_events if isinstance(raw_events, list) else []
        )
        if not isinstance(raw_events, list) or not raw_events:
            return []
        rendered: list[dict[str, Any]] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            exact_text = str(raw.get("exact_text") or raw.get("text") or "").strip()
            if not exact_text:
                continue
            if is_general_review_text(exact_text):
                continue
            sources: list[dict[str, Any]] = []
            seen_sources: set[str] = set()
            seen_occurrence_documents: set[str] = set()
            for occurrence in raw.get("source_occurrences", []) or []:
                if not isinstance(occurrence, dict):
                    continue
                owner_id = str(occurrence.get("comment_id", ""))
                if not owner_id:
                    continue
                occurrence_document = str(occurrence.get("source_document", "")).strip()
                occurrence_document_key = occurrence_document.casefold()
                if occurrence_document_key and occurrence_document_key in seen_occurrence_documents:
                    continue
                references = self._event_source_references(
                    raw, occurrence, exact_text,
                )
                source = next((
                    reference for reference in references
                    if reference.get("kind") == "local"
                ), None)
                if not source:
                    continue
                source_key = source_reference_identity(source)
                if not source_key or source_key in seen_sources:
                    continue
                seen_sources.add(source_key)
                if occurrence_document_key:
                    seen_occurrence_documents.add(occurrence_document_key)
                source = dict(source)
                source["relation"] = (
                    "Primary source" if not sources else "Also appears in"
                )
                sources.append(source)
            event_timestamp = str(raw.get("occurred_at_label", ""))
            source_document = str(raw.get("source_document", ""))
            if not source_document:
                source_document = next((
                    str(occurrence.get("source_document", ""))
                    for occurrence in raw.get("source_occurrences", []) or []
                    if isinstance(occurrence, dict)
                    and str(occurrence.get("source_document", "")).strip()
                ), "")
            source_date = event_document_date(raw, source_document)
            review_round = canonical_round_label(
                raw.get("effective_round") or raw.get("review_round"),
                source_document,
            )
            marker = str(raw.get("event_round_marker", ""))
            display_text, embedded_date, embedded_note = embedded_date_annotation(
                exact_text
            )
            metadata_date = normalize_date_label(event_timestamp) or event_document_date(raw, source_document)
            event_time = (
                (date_only_label(event_timestamp), "event_header", "exact_date")
                if event_timestamp
                else (
                    (f"Event date · {metadata_date}", "event_metadata", "exact_date")
                    if metadata_date
                    else (
                        (f"Event date · {embedded_date}", "embedded_text_date", "exact_date")
                        if embedded_date
                        else fallback_time_label(review_round, source_date)
                    )
                )
            )
            event_type = str(raw.get("event_type", "discussion_note"))
            if not event_timestamp and not source_date and not metadata_date and not embedded_date:
                marker_key = marker or review_round
                event_time = (
                    (marker_key if marker_key.startswith("PC") else f"PC{marker_key}" if marker_key else "PCx"),
                    "round_key",
                    "round_only",
                )
            base_label = (
                "Government comment"
                if event_type == "government_comment"
                else "Reviewer follow-up"
                if event_type == "reviewer_follow_up"
                else "Applicant response"
                if event_type == "applicant_response"
                else "Discussion note"
            )
            event_id = str(raw.get("event_id", ""))
            actor = str(raw.get("actor", ""))
            text = display_text
            # Historic indexes could reuse one event_id for the same text in
            # PC1, PC2, and PC3. React requires sibling keys to be unique, so
            # expose an occurrence-specific ID even before a dataset repair
            # has been applied. The raw ID remains in merged_event_ids for
            # audit/provenance. Date-aware merging below decides whether two
            # rendered rows are copies or distinct dated attempts.
            event_identity = "|".join((
                event_role_family(raw),
                review_round or "unknown-round",
                issue_event_text_identity(text),
                timeline_event_date_key({
                    "occurred_at_label": event_timestamp,
                    "source_date": source_date,
                    "embedded_date": embedded_date,
                    "time_basis": event_time[1],
                }),
            ))
            identity_digest = hashlib.sha256(
                f"{thread_id}|{event_identity}".encode("utf-8")
            ).hexdigest()[:16]
            render_event_id = f"{event_id or 'E'}-{identity_digest}"
            event_payload = {
                "event_id": render_event_id,
                "event_type": event_type,
                "actor_role": str(raw.get("actor_role", "unknown")),
                "actor": actor,
                "occurred_at": str(raw.get("occurred_at", "")),
                "occurred_at_label": event_timestamp,
                "time_label": event_time[0],
                "time_basis": event_time[1],
                "time_precision": event_time[2],
                "source_date": source_date,
                "document_date": raw.get("document_date", {}),
                "document_date_iso": str(raw.get("document_date_iso", "")),
                "document_date_source": str(raw.get("document_date_source", "")),
                "embedded_date": embedded_date,
                "embedded_date_note": embedded_note,
                "submission": document_submission_label(source_document),
                "submissions": sorted({
                    document_submission_label(str(
                        occurrence.get("source_document", "")
                    ))
                    for occurrence in raw.get("source_occurrences", []) or []
                    if isinstance(occurrence, dict)
                    and document_submission_label(str(
                        occurrence.get("source_document", "")
                    ))
                }),
                "record_label": marker,
                "record_labels": list(dict.fromkeys([
                    *(raw.get("record_labels") or []),
                    *(raw.get("event_labels") or []),
                    marker,
                ])),
                "label": f"{base_label} · {marker}" if marker else base_label,
                "text": text,
                # ``review_round`` remains the effective round for backward
                # compatibility.  The observed document round is retained so
                # a PC1 quote inside a PC4 response letter is not mislabeled
                # as a new PC4 comment.
                "review_round": review_round,
                "effective_round": review_round,
                "observed_in_document_round": canonical_round_label(
                    raw.get("observed_in_document_round") or review_round,
                    source_document,
                ) or review_round,
                "source": sources[0] if sources else None,
                "sources": sources,
                "date_variants": [value for value in {
                    timeline_event_date_key({
                        "occurred_at_label": event_timestamp,
                        "source_date": source_date,
                        "embedded_date": embedded_date,
                        "time_basis": event_time[1],
                    }),
                    metadata_date,
                    embedded_date,
                } if value],
                "merged_event_ids": list(dict.fromkeys([
                    *(raw.get("merged_event_ids", []) or []),
                    *([event_id] if event_id else []),
                ])),
                "printed_comment_id": str(raw.get("printed_comment_id", "")),
                "parent_comment_id": str(raw.get("parent_comment_id", "")),
                "linked_comment_id": str(raw.get("linked_comment_id", "")),
            }
            rendered.append(event_payload)
        return merge_timeline_event_occurrences(rendered)

    def _view_comment(self, comment: dict[str, Any]) -> dict[str, Any]:
        comment_id = comment["comment_id"]
        response_id = comment.get("response_id", "")
        response = self._responses_by_id.get(response_id)
        link = self._links_by_comment.get(comment_id, {})
        comment_enrichment = self._enrichment_for(comment_id, comment)
        response_enrichment = self._enrichment_for(response_id, response) if response else {}
        comment_text = verified_text(comment)
        response_text = verified_text(response) if response else ""
        # Some cumulative response-letter rows contain a valid raw extraction
        # with earlier-round ``PC2:``/``PC3:`` material appended to it.  Keep
        # that immutable raw text for audit/source matching, but allow a
        # deterministic, reviewed display projection for the detail view.
        display_comment_text = str(
            comment.get("display_text_override") or comment_text
        )
        comment_display_text, comment_embedded_date, comment_embedded_note = embedded_date_annotation(
            display_comment_text
        )
        comment_body_text, comment_label = comment_display_parts(
            comment_display_text,
            str(comment.get("source_document", "")),
            str(comment.get("comment_number", "")),
        )
        comment_label = str(
            comment.get("display_label_override") or comment_label
        )
        response_body_text = readable_evidence_text(response_text)
        comment_sources = self._source_references(comment_id, comment_text)
        # A canonical event can have several physical source owners.  Merge
        # their registry links here so the UI exposes one event with multiple
        # clickable sources instead of repeated comment cards.
        for member_id in comment.get("canonical_event_member_ids", []) or []:
            if str(member_id) == comment_id:
                continue
            for source in self._source_references(str(member_id), comment_text):
                source_key = source_reference_identity(source)
                if not source_key or any(source_reference_identity(item) == source_key for item in comment_sources):
                    continue
                source = dict(source)
                source["relation"] = "Also appears in"
                comment_sources.append(source)
        response_sources = (
            self._source_references(response["response_id"], response_text)
            if response else []
        )
        primary_comment_source = next((
            source for source in comment_sources
            if source.get("relation") == "Primary source"
        ), None)
        primary_response_source = next((
            source for source in response_sources
            if source.get("relation") == "Primary source"
        ), None)
        event_sources = {
            str(
                ((source.get("location") or {}).get("metadata") or {}).get(
                    "issue_event_id", ""
                )
            ): source
            for source in comment_sources
            if isinstance(source.get("location"), dict)
        }
        discussion_sources = [
            source for source in comment_sources
            if str(source.get("relation", "")).casefold()
            in {"discussion history", "discussion"}
        ]
        discussion_events = [
            event for event in comment.get("issue_thread_events", []) or []
            if isinstance(event, dict) and str(
                event.get("exact_text", "")
            ).strip() and not is_general_review_text(event.get("exact_text", ""))
        ]
        discussion_events.sort(key=lambda event: (
            0 if event.get("occurred_at") else 1,
            str(event.get("occurred_at", "")),
            int(event.get("source_order") or 0),
        ))
        comment_actor, comment_timestamp = reviewer_event_identity(
            str(comment.get("reviewer", ""))
        )
        comment_source_date = event_document_date(
            comment, str(comment.get("source_document", ""))
        )
        comment_round = canonical_round_label(
            comment.get("review_round"), comment.get("source_document", "")
        )
        comment_submission = document_submission_label(
            str(comment.get("source_document", ""))
        )
        if comment_timestamp:
            comment_time = (
                date_only_label(comment_timestamp),
                "reviewer_cell",
                "exact_date",
            )
        else:
            metadata_date = normalize_date_label(comment.get("review_round"))
            if metadata_date:
                comment_time = (
                    f"Event date · {metadata_date}",
                    "event_metadata",
                    "exact_date",
                )
            elif comment_embedded_date:
                comment_time = (
                    f"Event date · {comment_embedded_date}",
                    "embedded_text_date",
                    "exact_date",
                )
            else:
                comment_time = fallback_time_label(
                    comment_round,
                    comment_source_date,
                )
        timeline_events: list[dict[str, Any]] = [{
            "event_id": f"{comment_id}-government-comment",
            "event_type": "government_comment",
            "actor_role": "government",
            "actor": comment_actor or str(comment.get("reviewer", "")),
            "occurred_at": "",
            "occurred_at_label": comment_timestamp,
            "time_label": comment_time[0],
            "time_basis": comment_time[1],
            "time_precision": comment_time[2],
            "source_date": comment_source_date,
            "document_date": comment.get("document_date", {}),
            "document_date_iso": str(comment.get("document_date_iso", "")),
            "document_date_source": str(comment.get("document_date_source", "")),
            "embedded_date": comment_embedded_date,
            "embedded_date_note": comment_embedded_note,
            "submission": comment_submission,
            "record_label": comment_label,
            "label": "Government comment",
            "text": str(
                comment.get("timeline_comment_text") or comment_body_text
            ),
            "review_round": comment_round,
            "effective_round": comment_round,
            "source": primary_comment_source,
            "sources": [primary_comment_source] if primary_comment_source else [],
        }]
        for event in discussion_events:
            event_type = str(event.get("event_type", "discussion_note"))
            raw_event_id = str(event.get("event_id") or "discussion")
            # Ingestion derives discussion IDs from the thread and event
            # ordinal.  The same ordinal can legitimately recur in a later
            # submission, so namespace it with the source comment row before
            # the frontend combines multiple records into one timeline.
            timeline_event_id = f"{comment_id}-{raw_event_id}"
            event_timestamp = str(
                event.get("occurred_at_label", "")
            )
            event_source_document = str(
                event.get("source_document")
                or comment.get("source_document", "")
            )
            event_round = canonical_round_label(
                event.get("effective_round")
                or event.get("review_round"),
                event_source_document,
            )
            event_display_text, event_embedded_date, event_embedded_note = embedded_date_annotation(
                event.get("exact_text", "")
            )
            event_metadata_date = normalize_date_label(event_timestamp) or event_document_date(
                event, event_source_document
            )
            event_time = (
                (
                    date_only_label(event_timestamp),
                    "discussion_header",
                    "exact_date",
                )
                if event_timestamp
                else (
                    (
                        f"Event date · {event_metadata_date}",
                        "event_metadata",
                        "exact_date",
                    )
                    if event_metadata_date
                    else (
                        (
                            f"Event date · {event_embedded_date}",
                            "embedded_text_date",
                            "exact_date",
                        )
                        if event_embedded_date
                        else fallback_time_label(event_round, comment_source_date)
                    )
                )
            )
            event_source_date = event_document_date(
                event, event_source_document
            ) or comment_source_date
            event_cell_range = str(
                (event.get("source_location") or {}).get("cell_range", "")
            )
            event_filename = Path(event_source_document).name.casefold()

            def same_event_file(source: dict[str, Any]) -> bool:
                return bool(event_filename) and str(
                    source.get("filename", "")
                ).casefold() == event_filename

            # An applicant response may be represented twice: the response
            # record has the precise response cell (E6), while the parent
            # comment's discussion history has the combined history cell
            # (F6).  Resolve that role before consulting comment-owned event
            # sources so the viewer cannot open the government comment cell
            # (C6) for a response citation.
            if event_type == "applicant_response":
                event_source = next(
                    (
                        source for source in response_sources
                        if same_event_file(source)
                    ),
                    next(
                        (
                            source for source in discussion_sources
                            if same_event_file(source)
                            and str(source.get("relation", "")).casefold()
                            in {"prior applicant response", "discussion history", "discussion"}
                        ),
                        next(
                            (
                                source for source in response_sources
                                if source.get("relation") == "Primary source"
                            ),
                            next(
                                (
                                    source for source in discussion_sources
                                    if str(source.get("relation", "")).casefold()
                                    in {"prior applicant response", "discussion history", "discussion"}
                                ),
                                None,
                            ),
                        ),
                    ),
                )
            else:
                # Reviewer follow-ups are discussion events, not government
                # comment cells.  Resolve them from the event's own source
                # occurrence so a missing/legacy registry entry cannot make
                # the viewer fall back to the parent comment cell (C6).
                if event_type == "reviewer_follow_up":
                    resolved_event_sources = self._event_source_references(
                        event,
                        {
                            "comment_id": comment_id,
                            "response_id": response_id,
                            "source_document": event_source_document,
                        },
                        str(event.get("exact_text", "")),
                    )
                    event_source = next(
                        (
                            source for source in resolved_event_sources
                            if source.get("kind") == "local"
                        ),
                        None,
                    )
                else:
                    event_source = None
                event_source = event_source or event_sources.get(raw_event_id)
                event_source = event_source or next(
                    (
                        source for source in discussion_sources
                        if (source.get("location") or {}).get("cell_range")
                        == event_cell_range
                    ),
                    discussion_sources[0] if discussion_sources else primary_comment_source,
                )
            timeline_events.append({
                "event_id": timeline_event_id,
                "event_type": event_type,
                "actor_role": str(event.get("actor_role", "unknown")),
                "actor": str(event.get("actor", "")),
                "occurred_at": str(event.get("occurred_at", "")),
                "occurred_at_label": str(
                    event.get("occurred_at_label", "")
                ),
                "time_label": event_time[0],
                "time_basis": event_time[1],
                "time_precision": event_time[2],
                "source_date": event_source_date,
                "document_date": event.get("document_date", {}),
                "document_date_iso": str(event.get("document_date_iso", "")),
                "document_date_source": str(event.get("document_date_source", "")),
                "submission": document_submission_label(event_source_document),
                "record_label": comment_label,
                "label": (
                    "Reviewer follow-up"
                    if event_type == "reviewer_follow_up"
                    else "Applicant response"
                    if event_type == "applicant_response"
                    else "Discussion note"
                ),
                "text": event_display_text,
                "embedded_date": event_embedded_date,
                "embedded_date_note": event_embedded_note,
                "review_round": str(
                    event_round
                ),
                "effective_round": event_round,
                "source": event_source,
                "sources": [event_source] if event_source else [],
            })

        def merge_indexed_timeline_events(
            indexed_events: list[dict[str, Any]],
        ) -> None:
            """Add cross-document history without discarding the source row.

            The source row contains the substantive government comment and
            its discussion cell. The global index adds occurrences from
            other rounds/files. Older code replaced the former with the
            latter, producing timelines that began with a response or
            follow-up and omitted the main requirement.
            """
            for indexed_event in indexed_events:
                indexed_text = issue_event_text_identity(
                    str(indexed_event.get("text", ""))
                )
                existing = next((
                    event for event in timeline_events
                    if issue_event_text_identity(str(event.get("text", "")))
                    == indexed_text
                    and timeline_event_rounds_compatible(event, indexed_event)
                    and event_role_family(event) == event_role_family(indexed_event)
                    and timeline_event_dates_compatible(event, indexed_event)
                ), None)
                if existing is None:
                    timeline_events.append(indexed_event)
                    continue
                _merge_timeline_event_payloads(existing, indexed_event)
        if response:
            response_source_document = str(
                response.get("source_document")
                or comment.get("source_document", "")
            )
            response_round = canonical_round_label(
                response.get("response_letter_round")
                or comment.get("review_round", ""),
                response_source_document,
            )
            response_source_date = event_document_date(
                response, response_source_document
            )
            # A response/status line can carry the actual response date even
            # when the physical workbook/PDF has a different report date.
            # Prefer that event date for the timeline, while retaining the
            # document date as provenance and as the fallback for old rows.
            response_event_date = normalize_date_label(
                response.get("event_date")
                or response.get("response_date_iso")
                or response.get("response_date_raw", "")
            )
            export_time = workbook_export_label(str(
                response_source_document
            ))
            response_time = (
                (
                    f"Response date · {response_event_date}",
                    "response_date",
                    "exact_date",
                )
                if response_event_date
                else (
                (
                    export_time,
                    "workbook_export",
                    "available_by",
                )
                if export_time
                else fallback_time_label(
                    response_round,
                    str(
                        response_source_date
                    ),
                )
                )
            )
            indexed_events = self._indexed_issue_events(comment)
            if indexed_events:
                merge_indexed_timeline_events(indexed_events)
            timeline_events.append({
                "event_id": f"{response_id}-current-response",
                "event_type": "current_applicant_response",
                "actor_role": "company",
                "actor": "",
                "occurred_at": "",
                # Use the response-cell date for timeline ordering.  The
                # workbook/report date remains in document_date as provenance
                # and must not make a late response appear at the start of
                # the history.
                "occurred_at_label": response_event_date,
                "time_label": response_time[0],
                "time_basis": response_time[1],
                "time_precision": response_time[2],
                "source_date": response_event_date or response_source_date,
                "event_date": response_event_date,
                "document_date": response.get("document_date", {}),
                "document_date_iso": str(response.get("document_date_iso", "")),
                "document_date_source": str(response.get("document_date_source", "")),
                "submission": document_submission_label(response_source_document),
                "record_label": comment_label,
                "label": (
                    "Current applicant response"
                    if discussion_events else "Company response"
                ),
                "text": response_body_text,
                "review_round": str(
                    response_round
                ),
                "effective_round": response_round,
                "source": primary_response_source,
                "sources": [primary_response_source] if primary_response_source else [],
            })
        else:
            indexed_events = self._indexed_issue_events(comment)
            if indexed_events:
                merge_indexed_timeline_events(indexed_events)

        # A source-row opening event and the persisted issue index can still
        # describe the same row with different container dates, labels, or
        # extraction prefixes.  Run one final role/round/content merge after
        # all synthetic and indexed events have been added.  This is the last
        # guard before the API payload reaches the browser.
        timeline_events = merge_timeline_event_occurrences(timeline_events)

        # Keep the source row that opened the issue at the top, then order
        # later attempts by review round and (when available) exact date.
        # Stable ordering for equal keys preserves the source's response /
        # reviewer-follow-up sequence.
        opening_event_id = f"{comment_id}-government-comment"
        original_order = {
            str(event.get("event_id", "")): index
            for index, event in enumerate(timeline_events)
        }

        def timeline_sort_key(event: dict[str, Any]) -> tuple[Any, ...]:
            event_id = str(event.get("event_id", ""))
            if event_id == opening_event_id:
                return (0, 0, 0, 0, original_order.get(event_id, 0))
            round_value = canonical_round_number(
                event.get("effective_round") or event.get("review_round")
            )
            round_key = round_value if round_value is not None else 10**9
            date_value = timeline_event_date_key(event)
            date_key = ""
            if date_value:
                try:
                    date_key = datetime.strptime(
                        normalize_date_label(date_value), "%m/%d/%Y"
                    ).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    date_key = str(date_value)
            # Once the opening comment is shown, chronology is primary.  A
            # known response/reviewer date must not be placed before a later
            # round merely because the older discussion row lost its round
            # label.  Round is only a fallback for undated legacy events.
            return (
                1,
                0 if date_key else 1,
                date_key,
                round_key,
                original_order.get(event_id, 0),
            )

        timeline_events.sort(key=timeline_sort_key)
        return {
            "comment_id": comment_id,
            "city_id": comment.get("city_id", ""),
            "site_id": comment.get("site_id", ""),
            "site_name": comment.get("site_name", ""),
            "project_id": comment.get("project_id", ""),
            "project_name": comment.get("project_name") or comment.get("site_name") or comment.get("property_project", "unknown"),
            "project_alias": comment.get("project_alias", ""),
            "source_file_id": comment.get("source_file_id", ""),
            "canonical_document_id": comment.get("canonical_document_id", ""),
            "canonical_comment_id": comment.get("canonical_comment_id", ""),
            "occurrence_type": comment.get("occurrence_type", "newly_issued"),
            "city": comment.get("city", "unknown"),
            "property_project": comment.get("site_name") or comment.get("property_project", "unknown"),
            "review_round": comment_round or "unknown",
            "discipline": comment.get("discipline", "unknown"),
            "comment_type": classify_comment(comment_text, comment.get("discipline", "")),
            "reviewer": comment.get("reviewer", ""),
            "comment_number": comment.get("comment_number", ""),
            "original_text": comment_text,
            "text_raw": str(comment.get("text_raw") or comment.get("raw_extracted_text") or comment.get("original_text", "")),
            "text_reconstructed": str(comment.get("text_reconstructed") or comment_text),
            "normalized_identity_text_v2": str(comment.get("normalized_identity_text_v2", "")),
            "normalized_search_text_v2": str(comment.get("normalized_search_text_v2", "")),
            "source_unit_ids": list(comment.get("source_unit_ids", []) or []),
            "reconstruction": comment.get("reconstruction", {}) if isinstance(comment.get("reconstruction"), dict) else {},
            "display_text": comment_body_text if comment.get("text_trust_status") == "verified" else (comment_enrichment.get("display_text") or comment_body_text),
            "comment_label": comment_label,
            "display_blocks": comment_enrichment.get("blocks", []),
            "source_filename": compact_path(comment.get("source_document", "")),
            "sources": comment_sources,
            "canonical_event": {
                "event_id": str(comment.get("canonical_event_id", "")),
                "comment_count": int(comment.get("canonical_event_comment_count", 1) or 1),
                "source_count": int(comment.get("canonical_event_source_count", len(comment_sources)) or 0),
                "occurrence_count": int(comment.get("canonical_event_occurrence_count", 0) or 0),
                "member_ids": list(comment.get("canonical_event_member_ids", []) or []),
                "source_documents": list(comment.get("canonical_event_source_documents", []) or []),
                "date": str(comment.get("canonical_event_date", "")),
                "date_provenance": comment.get("canonical_event_date_provenance", {}) or {},
                "round_provenance": comment.get("canonical_event_round_provenance", {}) or {},
                "source_occurrences": list(comment.get("canonical_event_source_occurrences", []) or []),
            },
            "source_location": comment.get("source_location", "unknown"),
            "extraction_method": comment.get("extraction_method", ""),
            "extraction_confidence": comment.get("extraction_confidence", ""),
            "match_status": comment.get("match_status", "unmatched"),
            "human_review_status": comment.get("human_review_status", "pending"),
            "category": self._assignments.get(comment_id, "Uncategorized"),
            "response": ({
                "response_id": response["response_id"],
                "original_text": response_text,
                "text_raw": str(response.get("text_raw") or response.get("raw_extracted_text") or response.get("original_text", "")),
                "text_reconstructed": str(response.get("text_reconstructed") or response_text),
                "normalized_identity_text_v2": str(response.get("normalized_identity_text_v2", "")),
                "normalized_search_text_v2": str(response.get("normalized_search_text_v2", "")),
                "source_unit_ids": list(response.get("source_unit_ids", []) or []),
                "reconstruction": response.get("reconstruction", {}) if isinstance(response.get("reconstruction"), dict) else {},
                "display_text": response_body_text if response.get("text_trust_status") == "verified" else (response_enrichment.get("display_text") or response_body_text),
                "display_blocks": response_enrichment.get("blocks", []),
                "source_filename": compact_path(response.get("source_document", "")),
                "sources": response_sources,
                "source_location": response.get("source_location", "unknown"),
                "human_review_status": response.get("human_review_status", "pending"),
                "source_document_date": response.get("source_document_date", ""),
                "document_date": response.get("document_date", {}),
                "document_date_iso": response.get("document_date_iso", ""),
                "event_date": response.get("event_date", ""),
                "event_date_source": response.get("event_date_source", ""),
            } if response else None),
            "link": {
                "link_id": link.get("link_id", ""),
                "match_confidence": link.get("match_confidence", ""),
                "matching_method": link.get("matching_method", ""),
                "review_status": self._effective_link_status(link),
            },
            "issue_thread": {
                "thread_id": str(
                    comment.get("issue_thread_id", comment_id)
                ),
                "grouping_status": str(
                    comment.get("issue_grouping_status", "single_record")
                ),
                "grouping_method": str(
                    comment.get(
                        "issue_grouping_method", "single_source_record",
                    )
                ),
                "status": str(comment.get("issue_status", "")),
                "event_count": len(timeline_events),
                "events": timeline_events,
            },
        }

    def link_review_queue(self, status: str = "pending", city: str = "", summary_only: bool = False) -> dict[str, Any]:
        self.reload()
        allowed_statuses = {"pending", "suggested", "confirmed", "rejected", "needs_review", "needs_followup", "all"}
        if status not in allowed_statuses:
            raise ValueError("Unknown link-review status")
        eligible: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for comment in self._comments:
            link = self._links_by_comment.get(str(comment.get("comment_id", "")), {})
            if not link.get("response_id"):
                continue
            base_status = str(link.get("review_status", ""))
            link_id = str(link.get("link_id", ""))
            if base_status not in {"suggested", "needs_review"} and link_id not in self._link_review_decisions:
                continue
            effective = self._effective_link_status(link)
            eligible.append((comment, link, effective))

        counts = Counter(effective for _, _, effective in eligible)
        count_payload = {
            "total": len(eligible), "suggested": counts["suggested"],
            "confirmed": counts["confirmed"], "rejected": counts["rejected"],
            "needs_review": counts["needs_review"], "needs_followup": counts["needs_followup"],
            "completed": counts["confirmed"] + counts["rejected"],
        }
        if summary_only:
            return {"items": [], "counts": count_payload}
        items: list[dict[str, Any]] = []
        for comment, link, effective in eligible:
            if city and comment.get("city") != city:
                continue
            if status == "pending" and effective not in {"suggested", "needs_review", "needs_followup"}:
                continue
            if status not in {"pending", "all"} and effective != status:
                continue
            view = self._view_comment(comment)
            decision = self._link_review_decisions.get(str(link.get("link_id", "")), {})
            items.append({
                "link_id": link.get("link_id", ""), "status": effective,
                "base_status": link.get("review_status", ""),
                "note": decision.get("note", ""), "updated_at": decision.get("updated_at"),
                "comment": view,
            })
        items.sort(key=lambda item: (
            str(item["comment"].get("city", "")), str(item["comment"].get("property_project", "")),
            str(item["comment"].get("review_round", "")), str(item["comment"].get("discipline", "")),
            str(item["comment"].get("comment_number", "")), str(item.get("link_id", "")),
        ))
        return {
            "items": items,
            "counts": count_payload,
        }

    def set_link_review(self, link_id: str, decision: str, note: str = "") -> dict[str, Any]:
        decision = decision.strip().casefold()
        note = re.sub(r"\s+", " ", note).strip()
        if decision not in {"", "confirmed", "rejected", "needs_followup"}:
            raise ValueError("Decision must be confirmed, rejected, needs_followup, or empty")
        if len(note) > 500:
            raise ValueError("Review note must be 500 characters or fewer")
        link = next((row for row in self._links_by_comment.values() if str(row.get("link_id", "")) == link_id), None)
        if not link or not link.get("response_id"):
            raise ValueError("Unknown response link")
        with self._lock:
            if decision:
                self._link_review_decisions[link_id] = {
                    "decision": decision, "note": note, "updated_at": int(time.time()),
                }
            else:
                self._link_review_decisions.pop(link_id, None)
            self._save_link_reviews()
            self._search_cache.clear()
            self._sync_search_index()
        return {"link_id": link_id, "decision": decision or str(link.get("review_status", "suggested"))}

    def _structured_workbook_groups(
        self,
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for comment in self._all_comments:
            if (
                comment.get("extraction_method")
                != "local_structured_spreadsheet"
            ):
                continue
            link = self._links_by_comment.get(
                str(comment.get("comment_id", "")), {},
            )
            if (
                link.get("provenance")
                != "local_structured_gemini_verified"
            ):
                continue
            source = str(comment.get("source_document", "")).strip()
            if (
                not source
                or Path(source).suffix.casefold() not in {".xlsx", ".csv"}
            ):
                continue
            groups.setdefault(source, []).append(comment)
        return groups

    def _workbook_completeness(
        self,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        artifact_ids = {
            str(
                (row.get("ingestion_audit") or {}).get("artifact_id", "")
            )
            for row in rows
            if isinstance(row.get("ingestion_audit"), dict)
        }
        if len(artifact_ids) != 1 or not next(iter(artifact_ids), ""):
            return {
                "can_confirm": False,
                "reason": "Rows do not share one ingestion artifact",
            }
        artifact_id = next(iter(artifact_ids))
        manifest_path = (
            self.dataset_path.parent
            / "ingestion_artifacts"
            / artifact_id
            / "completeness_manifest.json"
        )
        if not manifest_path.is_file():
            return {
                "can_confirm": False,
                "reason": "Structured completeness manifest is missing",
            }
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "can_confirm": False,
                "reason": "Structured completeness manifest is unreadable",
            }
        expected = int(manifest.get("candidate_comment_count") or 0)
        unresolved = int(manifest.get("unresolved_signal_count") or 0)
        can_confirm = (
            manifest.get("completion_status") == "complete"
            and manifest.get("requires_visual") is not True
            and unresolved == 0
            and expected == len(rows)
            and not manifest.get("duplicate_unit_ids")
            and not manifest.get("unassigned_unit_ids")
        )
        return {
            "can_confirm": can_confirm,
            "reason": (
                ""
                if can_confirm
                else "Local completeness checks did not pass"
            ),
            "artifact_id": artifact_id,
            "expected_comments": expected,
            "unresolved_signals": unresolved,
            "requires_visual": bool(manifest.get("requires_visual")),
        }

    def workbook_review_queue(
        self,
        status: str = "pending",
        city: str = "",
        summary_only: bool = False,
    ) -> dict[str, Any]:
        self.reload()
        if status not in {
            "pending", "confirmed", "needs_followup", "all",
        }:
            raise ValueError("Unknown workbook-review status")
        items: list[dict[str, Any]] = []
        for source, rows in self._structured_workbook_groups().items():
            rows.sort(key=lambda row: (
                int(row.get("source_row") or 0),
                str(row.get("comment_id", "")),
            ))
            if city and rows[0].get("city") != city:
                continue
            links = [
                self._links_by_comment.get(
                    str(row.get("comment_id", "")), {},
                )
                for row in rows
            ]
            dataset_confirmed = all(
                row.get("search_eligible") is True
                and row.get("text_trust_status") == "verified"
                for row in rows
            ) and all(
                link.get("review_status") in {
                    "confirmed", "not_required",
                }
                for link in links
            )
            decision = self._workbook_review_decisions.get(source, {})
            effective = (
                "confirmed"
                if dataset_confirmed
                else "needs_followup"
                if decision.get("decision") == "needs_followup"
                else "pending"
            )
            completeness = self._workbook_completeness(rows)
            row_views = [self._view_comment(row) for row in rows]
            first_source = next((
                reference
                for view in row_views
                for reference in view.get("sources", [])
                if reference.get("kind") == "local"
            ), None)
            comment_columns = sorted({
                re.sub(
                    r"\d.*$", "",
                    str(row.get("source_cell_range", "")),
                )
                for row in rows
                if str(row.get("source_cell_range", ""))
            })
            response_columns = sorted({
                re.sub(
                    r"\d.*$", "",
                    str(
                        self._responses_by_id.get(
                            str(row.get("response_id", "")), {},
                        ).get("source_cell_range", "")
                    ),
                )
                for row in rows
                if str(row.get("response_id", ""))
            } - {""})
            items.append({
                "source_document": source,
                "filename": Path(source).name,
                "status": effective,
                "note": str(decision.get("note", "")),
                "updated_at": decision.get("updated_at"),
                "city": str(rows[0].get("city", "")),
                "property_project": str(
                    rows[0].get("property_project", "")
                ),
                "review_rounds": sorted({
                    str(row.get("review_round", "")) for row in rows
                }),
                "comment_count": len(rows),
                "response_count": sum(
                    bool(row.get("response_id")) for row in rows
                ),
                "comment_columns": comment_columns,
                "response_columns": response_columns,
                "source": first_source,
                "rows": row_views if not summary_only else [],
                "structural_checks": completeness,
            })
        items.sort(key=lambda item: (
            item["city"], item["property_project"], item["filename"],
        ))
        counts = Counter(item["status"] for item in items)
        total = len(items)
        visible_items = (
            items
            if status == "all"
            else [item for item in items if item["status"] == status]
        )
        return {
            "items": [] if summary_only else visible_items,
            "counts": {
                "total": total,
                "pending": counts["pending"],
                "confirmed": counts["confirmed"],
                "needs_followup": counts["needs_followup"],
            },
        }

    def set_workbook_review(
        self,
        source_document: str,
        decision: str,
        note: str = "",
    ) -> dict[str, Any]:
        source_document = source_document.strip()
        decision = decision.strip().casefold()
        note = re.sub(r"\s+", " ", note).strip()
        if decision not in {"confirmed", "needs_followup"}:
            raise ValueError(
                "Decision must be confirmed or needs_followup"
            )
        if len(note) > 500:
            raise ValueError(
                "Workbook review note must be 500 characters or fewer"
            )
        groups = self._structured_workbook_groups()
        rows = groups.get(source_document)
        if not rows:
            raise ValueError("Unknown structured workbook")
        completeness = self._workbook_completeness(rows)
        if decision == "confirmed" and not completeness["can_confirm"]:
            raise ValueError(
                str(completeness.get("reason"))
                or "Workbook did not pass local completeness checks"
            )
        now = int(time.time())
        with self._lock:
            data = json.loads(
                self.dataset_path.read_text(encoding="utf-8")
            )
            target_ids = {
                str(row.get("comment_id", "")) for row in rows
            }
            if decision == "confirmed":
                comments_by_id = {
                    str(row.get("comment_id", "")): row
                    for row in data.get("comments", [])
                }
                responses_by_id = {
                    str(row.get("response_id", "")): row
                    for row in data.get("responses", [])
                }
                links_by_comment = {
                    str(row.get("comment_id", "")): row
                    for row in data.get("comment_response_links", [])
                }
                if not target_ids.issubset(comments_by_id):
                    raise RuntimeError(
                        "Dataset changed during workbook review; reload and retry"
                    )
                for comment_id in target_ids:
                    comment = comments_by_id[comment_id]
                    link = links_by_comment.get(comment_id, {})
                    if (
                        comment.get("source_document") != source_document
                        or comment.get("extraction_method")
                        != "local_structured_spreadsheet"
                        or link.get("provenance")
                        != "local_structured_gemini_verified"
                    ):
                        raise RuntimeError(
                            "Workbook rows changed during review; reload and retry"
                        )
                    comment.update({
                        "verified_text": str(
                            comment.get("original_text", "")
                        ),
                        "source_status": "confirmed",
                        "human_review_status": "confirmed",
                        "verification_status": "confirmed",
                        "text_trust_status": "verified",
                        "search_eligible": True,
                        "extraction_confidence": 1.0,
                    })
                    audit_payload = comment.setdefault(
                        "ingestion_audit", {},
                    )
                    if isinstance(audit_payload, dict):
                        audit_payload["human_workbook_verification"] = {
                            "decision": "confirmed",
                            "note": note,
                            "updated_at": now,
                            "artifact_id": completeness.get(
                                "artifact_id", ""
                            ),
                        }
                    response_id = str(comment.get("response_id", ""))
                    response = responses_by_id.get(response_id)
                    if response:
                        response.update({
                            "verified_text": str(
                                response.get("original_text", "")
                            ),
                            "human_review_status": "confirmed",
                            "verification_status": "confirmed",
                            "text_trust_status": "verified",
                            "search_eligible": True,
                            "extraction_confidence": 1.0,
                        })
                        response_audit = response.setdefault(
                            "ingestion_audit", {},
                        )
                        if isinstance(response_audit, dict):
                            response_audit[
                                "human_workbook_verification"
                            ] = {
                                "decision": "confirmed",
                                "note": note,
                                "updated_at": now,
                            }
                    link.update({
                        "review_status": (
                            "confirmed" if response else "not_required"
                        ),
                        "verification_status": "confirmed",
                        "match_confidence": 1.0 if response else 0.0,
                    })
                    link_audit = link.setdefault("ingestion_audit", {})
                    if isinstance(link_audit, dict):
                        link_audit["human_workbook_verification"] = {
                            "decision": "confirmed",
                            "note": note,
                            "updated_at": now,
                        }
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.dataset_path.parent,
                    prefix="dataset-workbook-review-",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    json.dump(
                        data,
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    temporary = Path(stream.name)
                os.replace(temporary, self.dataset_path)
            self._workbook_review_decisions[source_document] = {
                "decision": decision,
                "note": note,
                "updated_at": now,
                "comment_count": len(rows),
                "artifact_id": completeness.get("artifact_id", ""),
            }
            self._save_workbook_reviews()
            self.reload(force=True)
            self._load_link_reviews()
            self._sync_search_index()
            self._search_cache.clear()
        return {
            "source_document": source_document,
            "decision": decision,
            "updated": len(rows) if decision == "confirmed" else 0,
        }

    def _source_references(self, owner_id: str, text: str) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        seen: set[str] = set()
        local_sources = self.source_registry.sources_for_owner(owner_id)
        if not local_sources:
            record = self._responses_by_id.get(owner_id)
            if record is None:
                record = self._comments_by_id.get(owner_id)
            if record is None:
                record = next((
                    row for row in self._all_comments
                    if str(row.get("comment_id", "")) == owner_id
                ), None)
            if record is not None:
                local_sources = self.source_registry.recover_sources_for_record(
                    owner_id, record,
                )
        for source in local_sources:
            source["kind"] = "local"
            source["filename"] = source["document"]["filename"]
            key = source_reference_identity(source)
            if key in seen:
                continue
            references.append(source)
            seen.add(key)
        display = readable_text(text)
        for url in re.findall(r"https?://[^\s<>\"]+", display):
            clean_url = url.rstrip(".,);]")
            key = f"url:{clean_url}"
            if key in seen:
                continue
            seen.add(key)
            references.append({
                "kind": "external",
                "url": clean_url,
                "filename": "Referenced web resource",
                "location": "",
                "relation": "Referenced in text",
            })
        return references

    def _common_topics(self, comments: list[dict[str, Any]], limit: int = 6) -> tuple[int, list[dict[str, Any]]]:
        # A topic occurrence is one logical comment in one canonical document,
        # never one physical filename, extraction row, or later snapshot of
        # the same issue thread. Legacy rows that have not been canonicalized
        # use their comment id as a safe fallback.
        occurrence_rows: list[dict[str, Any]] = []
        seen_occurrences: set[tuple[str, str]] = set()
        seen_threads: set[str] = set()
        thread_members: dict[str, list[str]] = {}
        for row in comments:
            thread_id = str(row.get("issue_thread_id", ""))
            if not thread_id:
                continue
            members = thread_members.setdefault(thread_id, [])
            comment_id = str(row.get("comment_id", ""))
            if comment_id and comment_id not in members:
                members.append(comment_id)
        for row in comments:
            if not topic_occurrence_allowed(row):
                continue
            thread_id = str(row.get("issue_thread_id", ""))
            if thread_id:
                if thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
            key = topic_occurrence_key(row)
            if key in seen_occurrences:
                continue
            seen_occurrences.add(key)
            occurrence_rows.append(row)

        count = len(occurrence_rows)
        parents = list(range(count))
        tokenized = [topic_tokens(verified_text(row)) for row in occurrence_rows]
        signatures = [" ".join(tokens) for tokens in tokenized]

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for left in range(count):
            if not tokenized[left]:
                continue
            for right in range(left + 1, count):
                if signatures[left] == signatures[right]:
                    union(left, right)
                    continue
                shorter = min(len(tokenized[left]), len(tokenized[right]))
                threshold = 0.8 if shorter <= 5 else 0.7
                if topic_similarity(tokenized[left], tokenized[right]) >= threshold:
                    union(left, right)

        groups: dict[int, list[int]] = {}
        for index in range(count):
            groups.setdefault(find(index), []).append(index)

        common: list[dict[str, Any]] = []
        for indexes in groups.values():
            if len(indexes) < 2:
                continue
            representative_index = max(indexes, key=lambda item: (len(tokenized[item]), -item))
            representative = occurrence_rows[representative_index]
            document_ids = {
                str(occurrence_rows[item].get("canonical_document_id") or topic_occurrence_key(occurrence_rows[item])[0])
                for item in indexes
            }
            # A topic is common only across independent logical documents.  A
            # repeated row or a renamed/re-exported source is one occurrence.
            if len(document_ids) < 2:
                continue
            document_registry = getattr(self, "_document_identity", {}).get("canonical_documents", {}) if self is not None else {}
            duplicate_files_excluded = sum(
                max(0, int(document_registry.get(document_id, {}).get("duplicate_group_size", 1)) - 1)
                for document_id in document_ids
            )
            supporting_comment_ids: list[str] = []
            for item in indexes:
                row = occurrence_rows[item]
                thread_id = str(row.get("issue_thread_id", ""))
                member_ids = thread_members.get(
                    thread_id,
                    [str(row.get("comment_id", ""))],
                )
                for comment_id in member_ids:
                    if comment_id and comment_id not in supporting_comment_ids:
                        supporting_comment_ids.append(comment_id)
            common.append({
                "label": topic_label(verified_text(representative)),
                "occurrences": len(indexes),
                "independent_source_documents": len(document_ids),
                "physical_duplicate_files_excluded": duplicate_files_excluded,
                "projects": len({occurrence_rows[item].get("project_id") or occurrence_rows[item].get("property_project", "") for item in indexes}),
                "rounds": len({(occurrence_rows[item].get("project_id") or occurrence_rows[item].get("property_project", ""), occurrence_rows[item].get("review_round", "")) for item in indexes}),
                "cities": len({occurrence_rows[item].get("city", "") for item in indexes}),
                # Counts remain issue-level, while navigation receives every
                # member so the selected card can prefer the confirmed
                # response-bearing record and render the complete timeline.
                "comment_ids": supporting_comment_ids,
            })
        common.sort(key=lambda row: (-row["occurrences"], row["label"].casefold()))
        return len(groups), common[:limit]

    def _common_topics_by_aspect(self, comments: list[dict[str, Any]], limit: int = 6) -> tuple[int, list[dict[str, Any]]]:
        """Classify project-specific issues by reviewed object and aspect.

        This deliberately does not compare comment text similarity.  One
        issue thread is one unit; a topic becomes common only when it appears
        in at least two distinct projects.
        """
        issue_rows: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        for row in comments:
            if not topic_occurrence_allowed(row):
                continue
            thread_id = str(row.get("issue_thread_id") or row.get("comment_id", ""))
            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            thread = self._issue_event_index.get(thread_id, {})
            events = thread.get("events", []) if isinstance(thread, dict) else []
            event_text = " ".join(
                str(event.get("exact_text") or event.get("text") or "")
                for event in events if isinstance(event, dict)
            )
            topic = classify_topic(
                f"{verified_text(row)} {event_text}", row.get("discipline", "")
            )
            issue_rows.append({"row": row, "thread_id": thread_id, "topic": topic, "events": events})

        groups: dict[str, list[dict[str, Any]]] = {}
        for item in issue_rows:
            groups.setdefault(item["topic"]["topic_id"], []).append(item)

        common: list[dict[str, Any]] = []
        registry = getattr(self, "_document_identity", {}).get("canonical_documents", {})
        for topic_id, items in groups.items():
            project_ids = {
                str(item["row"].get("project_id") or item["row"].get("property_project", ""))
                for item in items
            }
            if len(project_ids) < 2:
                continue
            topic = items[0]["topic"]
            thread_ids = {item["thread_id"] for item in items}
            source_documents = {
                str(item["row"].get("canonical_document_id") or item["row"].get("source_document", ""))
                for item in items
            }
            event_count = sum(
                len(item["events"]) + int(bool(item["row"].get("response_id")))
                for item in items
            )
            source_count = sum(
                sum(len(event.get("source_occurrences", []) or []) for event in item["events"] if isinstance(event, dict))
                + int(bool(item["row"].get("response_id")))
                for item in items
            )
            supporting_ids: list[str] = []
            for item in items:
                member_ids = self._issue_event_index.get(item["thread_id"], {}).get("member_comment_ids", [])
                for comment_id in member_ids or [str(item["row"].get("comment_id", ""))]:
                    if comment_id and comment_id not in supporting_ids:
                        supporting_ids.append(comment_id)
            common.append({
                "topic_id": topic_id,
                "parent_topic": topic["parent"],
                "aspect": topic["aspect"],
                "label": topic["aspect"].title(),
                "taxonomy_version": TOPIC_TAXONOMY_VERSION,
                "occurrences": len(thread_ids),
                "issue_count": len(thread_ids),
                "project_count": len(project_ids),
                "event_count": event_count,
                "source_count": source_count,
                "independent_source_documents": len(source_documents),
                "physical_duplicate_files_excluded": sum(
                    max(0, int(registry.get(doc, {}).get("duplicate_group_size", 1)) - 1)
                    for doc in source_documents
                ),
                "projects": len(project_ids),
                "rounds": len({
                    (item["row"].get("project_id") or item["row"].get("property_project", ""), item["row"].get("review_round", ""))
                    for item in items
                }),
                "cities": len({item["row"].get("city", "") for item in items}),
                "comment_ids": supporting_ids,
            })
        common.sort(key=lambda item: (-item["issue_count"], item["label"].casefold()))
        return len(issue_rows), common[:limit]

    def _recurring_issues(self, comments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return project-specific review histories, separate from Common Topics.

        Common Topics intentionally groups broad design aspects.  This view
        instead keeps one concrete issue thread and its round-by-round events
        together.  Source occurrences remain nested under their event and are
        never counted as additional issues.
        """
        visible_ids = {str(row.get("comment_id", "")) for row in comments}
        all_by_id = {str(row.get("comment_id", "")): row for row in self._all_comments}
        all_by_id.update({str(row.get("comment_id", "")): row for row in comments})
        issues: list[dict[str, Any]] = []

        for thread_id, thread in self._issue_event_index.items():
            if not isinstance(thread, dict):
                continue
            member_ids = [
                str(value) for value in thread.get("member_comment_ids", []) or []
                if str(value)
            ]
            member_rows = [all_by_id[comment_id] for comment_id in member_ids if comment_id in all_by_id]
            visible_member_ids = [comment_id for comment_id in member_ids if comment_id in visible_ids]
            if not member_rows or not visible_member_ids:
                continue
            raw_events = [
                event for event in thread.get("events", []) or []
                if isinstance(event, dict) and str(event.get("exact_text") or event.get("text") or "").strip()
            ]
            raw_events = merge_duplicate_issue_events(raw_events)
            if not raw_events:
                continue
            # Keep workflow boilerplate out of the history while preserving
            # real design events in the same thread.  A cumulative export can
            # contain ``Noted`` and ``comment remains open`` beside the actual
            # government requirement; those lines must not make a timeline
            # look like a separate issue or inflate its event count.
            meaningful_events = [
                event for event in raw_events
                if not is_general_review_text(
                    event.get("exact_text") or event.get("text") or ""
                )
            ]
            if not meaningful_events:
                continue
            rounds = sorted({
                round_value
                for event in meaningful_events
                for round_value in [canonical_round_number(
                    event.get("effective_round") or event.get("review_round"),
                    event.get("source_document") or next((
                        occurrence.get("source_document", "")
                        for occurrence in event.get("source_occurrences", []) or []
                        if isinstance(occurrence, dict)
                    ), ""),
                )]
                if round_value is not None
            })
            if not rounds:
                rounds = sorted({
                    round_value
                    for row in member_rows
                    for round_value in [canonical_round_number(
                        row.get("review_round"), row.get("source_document", "")
                    )]
                    if round_value is not None
                })
            # Some legacy exports contain a valid issue history but omit the
            # review-round metadata from both the event and member row.  Keep
            # that history visible instead of crashing the entire /api/data
            # response while indexing rounds[0]/rounds[-1].  ``round_count``
            # remains zero so the UI does not claim that an unknown round is a
            # known review cycle; the display label makes the missing metadata
            # explicit.
            display_rounds = rounds or ["Unknown"]

            first_row = next((row for row in member_rows if row.get("source_document")), member_rows[0])
            discipline = str(first_row.get("discipline") or "General")
            event_text = " ".join(
                str(event.get("exact_text") or event.get("text") or "")
                for event in meaningful_events
            )
            topic = classify_topic(event_text, discipline)
            response_ids: set[str] = set()
            represented_response_ids: set[str] = set()
            source_documents: set[str] = set()
            event_summaries: list[dict[str, Any]] = []
            represented_company_texts: set[str] = set()
            comment_event_count = 0
            response_event_count = 0
            previous_text = ""
            for event in meaningful_events:
                event_source_document = str(
                    event.get("source_document", "")
                    or next((
                        occurrence.get("source_document", "")
                        for occurrence in event.get("source_occurrences", []) or []
                        if isinstance(occurrence, dict)
                    ), "")
                )
                exact_text, embedded_date, embedded_date_note = embedded_date_annotation(
                    event.get("exact_text") or event.get("text") or ""
                )
                event_type = str(event.get("event_type", "discussion_note"))
                is_company_event = (
                    str(event.get("actor_role", "")).casefold() == "company"
                    or event_type in {"applicant_response", "current_applicant_response"}
                )
                if is_company_event:
                    response_event_count += 1
                    normalized_company_text = normalized_comment(exact_text)
                    if normalized_company_text:
                        represented_company_texts.add(normalized_company_text)
                else:
                    comment_event_count += 1
                source_comment_ids = [
                    str(occurrence.get("comment_id", ""))
                    for occurrence in event.get("source_occurrences", []) or []
                    if isinstance(occurrence, dict) and str(occurrence.get("comment_id", ""))
                ]
                response_texts: list[str] = []
                event_documents: set[str] = set()
                for source_comment_id in source_comment_ids:
                    source_row = all_by_id.get(source_comment_id)
                    if source_row:
                        source_document = str(source_row.get("source_document", ""))
                        if source_document:
                            source_documents.add(source_document)
                            event_documents.add(source_document)
                        response_id = str(source_row.get("response_id", ""))
                        response = self._responses_by_id.get(response_id)
                        if response_id and response:
                            response_ids.add(response_id)
                            if is_company_event:
                                represented_response_ids.add(response_id)
                            response_text = readable_evidence_text(verified_text(response))
                            if response_text and response_text not in response_texts:
                                response_texts.append(response_text)
                    for occurrence in event.get("source_occurrences", []) or []:
                        if not isinstance(occurrence, dict):
                            continue
                        source_document = str(occurrence.get("source_document", ""))
                        if source_document:
                            source_documents.add(source_document)
                            event_documents.add(source_document)
                normalized = normalized_comment(exact_text)
                if not previous_text:
                    relationship = "initial"
                elif normalized == previous_text:
                    relationship = "exact_reissue"
                elif re.search(r"\b(no changes|comment remains|still|not addressed|remain open)\b", exact_text, re.IGNORECASE):
                    relationship = "response_rejected"
                else:
                    relationship = "reissued_with_clarification"
                previous_text = normalized or previous_text
                event_summaries.append({
                    "event_id": str(event.get("event_id", "")),
                    "effective_round": canonical_round_label(
                        event.get("effective_round") or event.get("review_round"),
                        event_source_document,
                    ),
                    "embedded_date": embedded_date,
                    "embedded_date_note": embedded_date_note,
                    "event_type": event_type,
                    "comment_text": exact_text,
                    "response_text": "\n\n".join(response_texts),
                    "source_occurrence_count": len(source_comment_ids),
                    "source_document_count": len(event_documents),
                    "source_comment_ids": source_comment_ids,
                    "relationship_to_previous": relationship,
                })

            # Recurrence is defined by the shape of the canonical history,
            # not by whether it crosses a numbered plan-check round.  A lone
            # government comment and an ordinary one-comment/one-response
            # pair stay out.  Two government/reviewer events, or any history
            # with at least three independent entries, is recurring.  Source
            # occurrences never add entries because they are already nested
            # under their canonical event.
            for response_id in sorted(response_ids):
                response = self._responses_by_id.get(response_id)
                response_text = normalized_comment(verified_text(response)) if response else ""
                if response_id in represented_response_ids or not response_text or response_text in represented_company_texts:
                    continue
                represented_company_texts.add(response_text)
                response_event_count += 1
            history_event_count = comment_event_count + response_event_count
            ordinary_pair = (
                comment_event_count == 1
                and response_event_count == 1
            )
            if history_event_count < 2 or ordinary_pair:
                continue

            status_values = {
                str(row.get("issue_status", "")).strip().casefold()
                for row in member_rows
                if str(row.get("issue_status", "")).strip()
            }
            if "responded" in status_values:
                status = "resolved"
                status_reason = "Explicitly marked responded in the source record."
            elif "unresolved" in status_values or any(
                event.get("event_type") == "reviewer_follow_up" for event in meaningful_events
            ):
                status = "open"
                status_reason = "A later review or follow-up remains in the source history."
            else:
                status = "unknown"
                status_reason = "No explicit resolution evidence was recorded."

            primary_event = next((
                event for event in meaningful_events
                if str(event.get("event_type", "")) == "government_comment"
            ), meaningful_events[0])
            title = recurring_issue_title(primary_event.get("exact_text") or primary_event.get("text"))
            persistence_explanation = recurring_issue_explanation(
                event_summaries, status, len(rounds),
            )
            issues.append({
                "issue_thread_id": str(thread_id),
                "project_id": str(first_row.get("project_id", "")),
                "site_id": str(first_row.get("site_id", "")),
                "site_name": str(first_row.get("site_name") or first_row.get("property_project", "")),
                "city": str(first_row.get("city", "")),
                "title": title,
                "common_topic": str(topic.get("aspect", "review")).title(),
                "discipline": discipline,
                "status": status,
                "status_reason": status_reason,
                "persistence_explanation": persistence_explanation,
                "first_round": display_rounds[0],
                "latest_round": display_rounds[-1],
                "round_count": len(rounds),
                # Counts are deliberately separated.  A source occurrence is
                # evidence for an event, not another event; duplicate files
                # must never inflate the history shown to users.
                "event_count": history_event_count,
                "history_event_count": history_event_count,
                "comment_event_count": comment_event_count,
                "response_event_count": response_event_count,
                "source_occurrence_count": sum(item["source_occurrence_count"] for item in event_summaries),
                "source_document_count": len(source_documents),
                "company_response_count": response_event_count,
                "comment_ids": visible_member_ids,
                "events": event_summaries,
            })

        issues.sort(key=lambda item: (
            -int(item["history_event_count"]),
            -int(item["round_count"]),
            -int(item["company_response_count"]),
            -int(item["source_document_count"]),
            str(item["title"]).casefold(),
        ))
        resolved = sum(item["status"] == "resolved" for item in issues)
        open_count = sum(item["status"] == "open" for item in issues)
        unknown = len(issues) - resolved - open_count
        resolved_rounds = [int(item["round_count"]) for item in issues if item["status"] == "resolved"]
        longest = max(issues, key=lambda item: (int(item["round_count"]), int(item["event_count"])), default=None)
        stats = {
            "total": len(issues),
            "open": open_count,
            "resolved": resolved,
            "unknown": unknown,
            "average_rounds_to_resolution": round(sum(resolved_rounds) / len(resolved_rounds), 1) if resolved_rounds else None,
            "longest_running_rounds": int(longest["round_count"]) if longest else 0,
            "longest_running_issue_id": longest["issue_thread_id"] if longest else "",
            "longest_running_title": longest["title"] if longest else "",
        }
        return issues, stats

    def analysis(self, city: str) -> dict[str, Any]:
        city = canonical_city_name(city)
        if city in self._analysis_cache:
            return self._analysis_cache[city]
        comments = [row for row in self._comments if row.get("city") == city]
        technical = sum(classify_comment(verified_text(row), row.get("discipline", "")) == "technical" for row in comments)
        unique_comments = len({normalized_comment(verified_text(row)) for row in comments})
        topic_count, common_topics = self._common_topics_by_aspect(comments)
        recurring_issues, recurring_issue_stats = self._recurring_issues(comments)
        projects = len({row.get("project_id") or row.get("property_project", "") for row in comments})
        rounds = len({(row.get("project_id") or row.get("property_project", ""), row.get("review_round", "")) for row in comments})
        nontechnical = len(comments) - technical
        summary = (
            f"{city} has {len(comments)} historical review comments across {projects} project scopes "
            f"and {rounds} review cycles. {technical} are classified as technical and {nontechnical} "
            f"as administrative or non-technical. After line-break normalization, {unique_comments} "
            f"comment texts are distinct; topic grouping identifies {topic_count} recurring or standalone issues."
        )
        payload = {
            "summary": summary,
            "total_comments": len(comments),
            "unique_comments": unique_comments,
            "topic_count": topic_count,
            "common_topic_count": len(common_topics),
            "technical": technical,
            "nontechnical": nontechnical,
            "projects": projects,
            "review_cycles": rounds,
            "common_topics": common_topics,
            "recurring_issues": recurring_issues,
            "recurring_issue_stats": recurring_issue_stats,
            "method_note": "Common Topic is classified by reviewed object and design aspect, not comment-text similarity. A common historical topic requires distinct project_id values; issue, event, and source counts are distinct and later snapshots remain inside one issue timeline.",
        }
        self._analysis_cache[city] = payload
        return payload

    def recurring_issues(self, city: str = "") -> dict[str, Any]:
        """Return the independent Review History layer for a city."""
        self.reload()
        city = canonical_city_name(city) if city else ""
        comments = [row for row in self._comments if not city or row.get("city") == city]
        issues, stats = self._recurring_issues(comments)
        return {"city": city, "issues": issues, "stats": stats}

    def data(self, city: str = "") -> dict[str, Any]:
        self.reload()
        city = canonical_city_name(city) if city else ""
        with self._lock:
            comments = [
                self._view_comment(row) for row in self._comments
                if not city or row["city"] == city
            ]
            matched = sum(row["match_status"] == "matched" for row in comments)
            canonical_events = [
                row.get("canonical_event", {}) for row in comments
                if row.get("canonical_event", {}).get("event_id")
            ]
            event_ids = {str(row.get("event_id")) for row in canonical_events if row.get("event_id")}
            source_occurrences = sum(int(row.get("occurrence_count", 0) or 0) for row in canonical_events)
            return {
                "cities": self.cities(),
                "categories": self.categories(city),
                "comments": comments,
                "stats": {
                    "comments": len(comments),
                    "matched": matched,
                    "unmatched": len(comments) - matched,
                },
                "evidence_layer": {
                    "stages": ["uploaded", "parsed", "prescanned", "extracted", "verified", "deduplicated", "timeline_linked", "indexed"],
                    "canonical_events": len(event_ids),
                    "source_occurrences": source_occurrences,
                    "note": "List rows are canonical events; physical duplicate files remain as source occurrences.",
                },
                "analysis": self.analysis(city) if city else None,
            }

    def search(self, city: str, query: str, limit: int = 30) -> list[dict[str, Any]]:
        self.reload()
        city = canonical_city_name(city) if city else ""
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        candidates = [row for row in self._comments if not city or row["city"] == city]
        if not candidates:
            return []
        tokenized = [tokenize(verified_text(row)) for row in candidates]
        document_frequency: Counter[str] = Counter()
        for tokens in tokenized:
            document_frequency.update(set(tokens))
        count = len(candidates)
        idf = {
            token: math.log((count + 1) / (document_frequency[token] + 0.5)) + 1
            for token in set(query_tokens)
        }
        query_counts = Counter(query_tokens)
        query_vector = {token: frequency * idf[token] for token, frequency in query_counts.items()}
        query_norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1.0
        results: list[dict[str, Any]] = []
        query_phrase = re.sub(r"\s+", " ", query.casefold()).strip()
        for comment, tokens in zip(candidates, tokenized):
            counts = Counter(token for token in tokens if token in idf)
            if not counts:
                continue
            vector = {token: frequency * idf[token] for token, frequency in counts.items()}
            norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
            score = sum(query_vector.get(token, 0) * value for token, value in vector.items()) / (query_norm * norm)
            text_lower = verified_text(comment).casefold()
            if query_phrase and query_phrase in text_lower:
                score += 0.35
            results.append({"comment_id": comment["comment_id"], "score": round(score, 4)})
        results.sort(key=lambda row: (-row["score"], row["comment_id"]))
        return results[: max(1, min(limit, 100))]

    def progressive_search(
        self,
        query: str,
        *,
        city: str = "",
        discipline: str = "",
        category: str = "",
        intent: str = "precedent_search",
        filters: dict[str, Any] | None = None,
        force_stage3: bool = False,
    ) -> dict[str, Any]:
        """Run the offline progressive retrieval contract.

        This endpoint is intentionally deterministic and does not call
        Gemini.  It is useful for the Chat layer, diagnostics, and CI tests;
        an optional model validator may consume its candidate packet later.
        """
        request_started = time.time()
        self.reload()
        scope = {
            key: value for key, value in {
                "city": city, "discipline": discipline, "category": category,
            }.items() if str(value).strip()
        }
        if isinstance(filters, dict):
            for key in ("city", "site_id", "project_id", "discipline", "review_round", "category"):
                value = str(filters.get(key) or "").strip()
                if value:
                    scope[key] = value
        # Response links are stored separately from comments.  Project a
        # confirmed link status into the retrieval packet so backend coverage
        # counts, not Gemini, determine response totals.
        prepared: list[dict[str, Any]] = []
        for source in self._tag_overlaid_rows():
            row = dict(source)
            comment_id = str(row.get("comment_id") or "")
            link = self._links_by_comment.get(comment_id, {})
            if row.get("response_id") or link.get("response_id"):
                row["response_id"] = row.get("response_id") or link.get("response_id")
                row["match_status"] = self._effective_link_status(link)
                row["response_status"] = row["match_status"]
            prepared.append(row)
        result = progressive_retrieve(
            query,
            prepared,
            intent=intent,
            filters=scope,
            tag_index=ValidatedTagIndex(prepared),
            force_stage3=force_stage3,
        )
        payload = result.as_dict()
        payload["rows"] = [
            {
                "comment_id": row.get("comment_id", ""),
                "canonical_event_id": row.get("canonical_event_id", ""),
                "canonical_issue_id": row.get("canonical_issue_id") or row.get("issue_timeline_id", ""),
                "city": row.get("city", ""),
                "project": row.get("project_name") or row.get("property_project", ""),
                "text": row.get("verified_text") or row.get("original_text", ""),
                "retrieval_stage": row.get("retrieval_stage", result.stage),
                "matched_tags": row.get("matched_tags", []),
                "retrieval_reason": row.get("retrieval_reason", ""),
                "relationship_to_query": row.get("relationship_to_query", ""),
                "event_tags": row.get("event_tags", []),
                "issue_tags": row.get("issue_tags", []),
                "tag_status": row.get("tag_status", "confirmed"),
                "tag_relationships": row.get("tag_relationships", []),
                "source_occurrence_count": row.get("_source_occurrence_count", 1),
                "topic_validation": row.get("topic_validation", {}),
                "verification_status": row.get("verification_status", "confirmed"),
                "response_status": row.get("response_status") or row.get("match_status", "missing"),
            }
            for row in result.rows
        ]
        payload["validated_results"] = payload["rows"]
        payload["excluded_results"] = result.excluded
        # Stage 3 discoveries are deliberately suggestions.  They are not
        # promoted into the validated tag index until an administrator reviews
        # the sidecar decision.
        fresh_suggestions: list[dict[str, Any]] = []
        suggestions_changed = False
        for item in result.suggested_tags:
            suggestion = dict(item)
            suggestion_id = f"{item.get('event_id', '')}:{item.get('suggested_tag', '')}"
            suggestion["suggestion_id"] = suggestion_id
            saved = self._tag_suggestions.get(suggestion_id, {})
            suggestion["status"] = saved.get("status", "suggested")
            if suggestion_id not in self._tag_suggestions:
                self._tag_suggestions[suggestion_id] = suggestion
                suggestions_changed = True
            fresh_suggestions.append(suggestion)
        if suggestions_changed:
            self._save_tag_suggestions()
        payload["suggested_tags"] = fresh_suggestions
        payload["tag_index"] = ValidatedTagIndex(prepared).as_dict()
        query_plan = enrich_query_plan(
            fallback_query_plan(query, False),
            query,
            False,
            scope,
        )
        query_plan["filters"] = dict(scope)
        payload["query_plan"] = query_plan
        payload["scope"] = scope
        payload["should_expand"] = bool(result.stage < 3 and not result.coverage.get("project_count"))
        payload["telemetry"] = {
            "query_id": "PQ-" + hashlib.sha256(
                f"{query}|{json.dumps(scope, sort_keys=True)}|{request_started}".encode("utf-8")
            ).hexdigest()[:20],
            "intent": intent,
            "request_created_at": datetime.fromtimestamp(request_started).isoformat(timespec="milliseconds"),
            "upload_duration_ms": 0,
            "queue_duration_ms": 0,
            "retrieval_duration_ms": round((time.time() - request_started) * 1000),
            "retrieval_stage": result.stage,
            "retrieval_stage_used": result.stage,
            "stage_1_candidate_count": result.stage_candidate_counts.get("stage_1", 0),
            "stage_2_candidate_count": result.stage_candidate_counts.get("stage_2", 0),
            "stage_3_candidate_count": result.stage_candidate_counts.get("stage_3", 0),
            "validated_count": len(result.rows),
            "excluded_count": len(result.excluded),
            "event_count": result.coverage.get("event_count", 0),
            "issue_count": result.coverage.get("issue_count", 0),
            "project_count": result.coverage.get("project_count", 0),
            "fallback_reason": result.fallback_reason,
            "validation_duration_ms": 0,
            "time_to_first_token_ms": None,
            "generation_duration_ms": 0,
            "input_tokens": 0,
            "gemini_input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "gemini_output_tokens": 0,
            "image_count": 0,
            "image_resolution": None,
            "evidence_unit_count": 0,
            "expected_record_count": None,
            "actual_record_count": len(result.rows),
            "response_bytes": 0,
            "retry_count": 0,
            "finish_reason": "local_retrieval",
            "gemini_used": False,
        }
        self._progressive_telemetry.append(payload["telemetry"])
        self._progressive_telemetry = self._progressive_telemetry[-100:]
        return payload

    def gemini_search(
        self, city: str, query: str, limit: int = 10,
        discipline: str = "", category: str = "",
    ) -> dict[str, Any]:
        self.reload()
        self._sync_search_index()
        final_limit = max(1, min(limit, 10))
        cache_key = json.dumps(["accuracy-rag-2.0", getattr(self.gemini_client, "model", ""), city, discipline, category, query.casefold().strip(), final_limit], ensure_ascii=False)
        cached = self._search_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            return {**cached[1], "cached": True}

        timings: dict[str, int] = {}
        failures: list[str] = []
        started = time.monotonic()
        analysis = normalize_analysis({}, query)
        if self.gemini_client:
            try:
                analysis = normalize_analysis(self.gemini_client.analyze_search_query(query), query)
            except RuntimeError:
                failures.append("query_analysis")
        timings["query_analysis_ms"] = round((time.monotonic() - started) * 1000)
        # Explicit UI filters are authoritative; inferred values only help ranking.
        if city:
            analysis["city"] = city
        if discipline:
            analysis["discipline"] = discipline
        if category:
            analysis["category"] = category

        rewrite_started = time.monotonic()
        rewrites: list[str] = []
        if self.gemini_client and "query_analysis" not in failures:
            try:
                rewrites = self.gemini_client.rewrite_search_query(query, analysis)
            except RuntimeError:
                failures.append("query_rewrites")
        timings["query_rewrites_ms"] = round((time.monotonic() - rewrite_started) * 1000)

        retrieval_started = time.monotonic()
        has_embeddings = any(
            unit.get("embedding")
            for record in self.search_index.records.values()
            for unit in record.get("search_units", [])
        )
        merged: dict[str, dict[str, Any]] = {}
        for search_query in [query, *rewrites]:
            query_embedding: list[float] | None = None
            if self.gemini_client and has_embeddings:
                try:
                    query_embedding = self.gemini_client.embed_query(search_query)
                except RuntimeError:
                    if "query_embedding" not in failures:
                        failures.append("query_embedding")
            rows = self.search_index.retrieve(
                search_query, analysis, city, query_embedding, discipline, category,
                vector_limit=100, keyword_limit=100, candidate_limit=200,
            )
            for row in rows:
                existing = merged.get(row["comment_id"])
                if not existing or row["score"] > existing["score"]:
                    row["retrieval_queries"] = [search_query]
                    merged[row["comment_id"]] = row
                elif search_query not in existing["retrieval_queries"]:
                    existing["retrieval_queries"].append(search_query)
        candidates = sorted(merged.values(), key=lambda row: (-row["score"], row["comment_id"]))[:200]
        timings["retrieval_ms"] = round((time.monotonic() - retrieval_started) * 1000)
        compact_candidates = [{
            "candidate_id": row["comment_id"],
            "city": row["record"].get("city", ""),
            "discipline": row["record"].get("discipline", ""),
            "category": row["record"].get("category", ""),
            "comment_excerpt": row.get("matched_excerpt", "")[:1200],
            "code_sections": row["record"].get("code_sections", []),
            "accepted": bool(row["record"].get("accepted")),
            "hybrid_score": row["score"],
            "data_quality_flags": row["record"].get("data_quality_flags", []),
        } for row in candidates]

        evaluation_started = time.monotonic()
        evaluations: list[dict[str, Any]] = []
        if self.gemini_client and compact_candidates and not failures:
            for offset in range(0, len(compact_candidates), 25):
                try:
                    evaluations.extend(self.gemini_client.evaluate_search_candidates(analysis, compact_candidates[offset:offset + 25]))
                except RuntimeError:
                    failures.append("candidate_evaluation")
                    evaluations = []
                    break
        timings["candidate_evaluation_ms"] = round((time.monotonic() - evaluation_started) * 1000)

        evaluation_by_id = {str(item.get("candidate_id", "")): item for item in evaluations}
        strongest = [
            row for row in candidates
            if evaluation_by_id.get(row["comment_id"], {}).get("match_class") in {"direct", "related", "uncertain"}
        ]
        strongest.sort(key=lambda row: -float(evaluation_by_id[row["comment_id"]].get("relevance_score", 0)))
        strongest = strongest[:30]

        deep_started = time.monotonic()
        deep_results: list[dict[str, Any]] = []
        full_candidates: list[dict[str, Any]] = []
        if self.gemini_client and strongest and not failures:
            for row in strongest:
                comment = self._comments_by_id[row["comment_id"]]
                response = self._responses_by_id.get(comment.get("response_id", ""))
                link = self._links_by_comment.get(row["comment_id"], {})
                full_candidates.append({
                    "candidate_id": row["comment_id"],
                    "city": comment.get("city", ""), "discipline": comment.get("discipline", ""),
                    "property_project": comment.get("property_project", ""), "review_round": comment.get("review_round", ""),
                    "heading_and_original_comment": verified_text(comment),
                    "matched_search_unit": row.get("matched_excerpt", ""),
                    "historical_response": verified_text(response) if response else "",
                    "response_review_status": response.get("human_review_status", "") if response else "no_response",
                    "response_link_review_status": self._effective_link_status(link),
                    "data_quality_flags": row["record"].get("data_quality_flags", []),
                    "initial_evaluation": evaluation_by_id[row["comment_id"]],
                })
            try:
                deep_results = self.gemini_client.deep_rerank(analysis, full_candidates)
            except RuntimeError:
                failures.append("deep_reranking")
        timings["deep_reranking_ms"] = round((time.monotonic() - deep_started) * 1000)

        verification_started = time.monotonic()
        verified: list[dict[str, Any]] = []
        verification_completed = False
        if self.gemini_client and not failures:
            full_by_id = {item["candidate_id"]: item for item in full_candidates}
            proposed = [{**item, "stored_record": full_by_id.get(str(item.get("candidate_id", "")), {})} for item in deep_results if item.get("match_class") in {"direct", "related"}][:15]
            try:
                verified = self.gemini_client.verify_search_results(analysis, proposed)
                verification_completed = True
            except RuntimeError:
                failures.append("verification")
        timings["verification_ms"] = round((time.monotonic() - verification_started) * 1000)

        results: list[dict[str, Any]] = []
        engine_label = "Hybrid database fallback"
        if verification_completed and not failures:
            for item in verified[:final_limit]:
                results.append({
                    "comment_id": str(item.get("candidate_id", "")),
                    "score": round(max(0.0, min(1.0, float(item.get("relevance_score", 0)))), 4),
                    "match_class": item.get("match_class"),
                    "confidence": round(max(0.0, min(1.0, float(item.get("confidence", 0)))), 4),
                    "response_applicable": bool(item.get("response_applicable")),
                    "important_difference": "; ".join(str(value) for value in item.get("important_differences", []) if str(value).strip()),
                    "reason": str(item.get("reason", "")).strip(),
                })
            engine_label = "Gemini accuracy-verified RAG"
        elif not self.gemini_client or failures:
            # Never label deterministic candidates as verified or direct.
            fallback_candidates = [
                row for row in candidates
                if row.get("keyword_score", 0) >= 0.22 or row.get("semantic_score", 0) >= (0.70 if has_embeddings else 0.34)
            ][:final_limit]
            results = [{
                "comment_id": row["comment_id"],
                "score": row["score"],
                "match_class": "unverified",
                "confidence": 0.0,
                "response_applicable": False,
                "important_difference": "Semantic verification is unavailable; this result is not classified as a direct precedent.",
                "reason": "Unverified deterministic candidate from lexical, vector, code, and metadata retrieval.",
            } for row in fallback_candidates]
        timings["total_ms"] = round((time.monotonic() - started) * 1000)
        payload = {
            "results": results,
            "engine_label": engine_label,
            "candidate_count": len(candidates),
            "has_direct_matches": any(row.get("match_class") == "direct" for row in results),
            "no_result_message": "" if results else "No sufficiently relevant historical precedent was found.",
            "timings": timings,
            "gemini_failures": failures,
            "cached": False,
        }
        if os.environ.get("PERMIT_SEARCH_DEBUG") == "1":
            payload["diagnostics"] = {
                "pipeline_version": "accuracy-rag-2.0", "prompt_version": "search-2.0",
                "gemini_model": getattr(self.gemini_client, "model", "") if self.gemini_client else "",
                "parsed_query": analysis, "query_rewrites": rewrites,
                "retrieval": [{"comment_id": row["comment_id"], "queries": row.get("retrieval_queries", []), "score": row["score"], "unit_id": row.get("matched_unit_id", "")} for row in candidates],
                "candidate_evaluations": evaluations, "deep_ranking": deep_results,
                "verification": verified,
                "final_source_ids": {row["comment_id"]: [source.get("source_id") for source in self.source_registry.sources_for_owner(row["comment_id"])] for row in results},
            }
        self._search_cache[cache_key] = (time.monotonic(), payload)
        return payload

class PermitHandler(BaseHTTPRequestHandler):
    server_version = "PermitBrowser/1.0"

    @property
    def app(self) -> "PermitServer":
        return self.server  # type: ignore[return-value]

    def end_headers(self) -> None:
        origin = self.headers.get("Origin", "").strip().rstrip("/")
        if origin and self.app.origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Expose-Headers",
                "Accept-Ranges, Content-Length, Content-Range, Content-Type",
            )
        super().end_headers()

    def do_OPTIONS(self) -> None:
        parsed = urlparse(self.path)
        origin = self.headers.get("Origin", "").strip().rstrip("/")
        if not parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
            return
        if origin and not self.app.origin_allowed(origin):
            self._error(HTTPStatus.FORBIDDEN, "Origin is not allowed")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_root_to_localhost(self, path: str) -> bool:
        host = self.headers.get("Host", "")
        hostname, separator, port = host.partition(":")
        if path not in {"", "/", "/index.html"} or hostname not in {"127.0.0.1", "0.0.0.0", "::1"}:
            return False
        authority = f"localhost:{port}" if separator and port else "localhost"
        self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
        self.send_header("Location", f"http://{authority}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > 1_000_000:
            raise ValueError("Request body must be between 1 byte and 1 MB")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _registry_error(self, exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        elif isinstance(exc, KeyError):
            self._error(HTTPStatus.NOT_FOUND, str(exc.args[0] if exc.args else exc))
        elif isinstance(exc, FileNotFoundError):
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        elif isinstance(exc, (ValueError, RuntimeError, TypeError)):
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc))
        else:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to open source")

    def _serve_document(self, document_id: str, mode: str, send_body: bool = True) -> None:
        try:
            delivery = self.app.store.source_registry.delivery(
                document_id, mode, self.headers.get("Range", ""),
            )
        except (PermissionError, KeyError, FileNotFoundError, ValueError, RuntimeError) as exc:
            self._registry_error(exc)
            return
        path = delivery["path"]
        length = delivery["end"] - delivery["start"] + 1
        filename = str(delivery["filename"]).replace('"', "")
        self.send_response(delivery["status"])
        self.send_header("Content-Type", delivery["mime_type"])
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'{delivery["disposition"]}; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        if delivery["mime_type"] == "application/pdf":
            self.send_header("Accept-Ranges", "bytes")
        if delivery["status"] == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f'bytes {delivery["start"]}-{delivery["end"]}/{delivery["size"]}')
        self.end_headers()
        if not send_body:
            return
        with path.open("rb") as stream:
            stream.seek(delivery["start"])
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if self._redirect_root_to_localhost(parsed.path):
            return
        if re.fullmatch(r"/api/documents/[A-Za-z0-9-]+/original", parsed.path):
            self.send_response(HTTPStatus.GONE)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        document_match = re.fullmatch(r"/api/documents/([A-Za-z0-9-]+)/preview", parsed.path)
        if document_match:
            self._serve_document(document_match.group(1), "preview", send_body=False)
            return
        self._serve_static(parsed.path, send_body=False)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({
                "status": "ok",
                "dataset_loaded": True,
                "source_registry_loaded": bool(self.app.store.source_registry),
                "gemini_configured": bool(self.app.store.gemini_client),
            })
            return
        if self._redirect_root_to_localhost(parsed.path):
            return
        if parsed.path == "/api/data":
            city = parse_qs(parsed.query).get("city", [""])[0]
            self._json(self.app.store.data(city))
            return
        if parsed.path == "/api/recurring-issues":
            city = parse_qs(parsed.query).get("city", [""])[0]
            self._json(self.app.store.recurring_issues(city))
            return
        if parsed.path == "/api/admin/tag-suggestions":
            self._json(self.app.store.tag_suggestions())
            return
        if parsed.path == "/api/categories":
            city = parse_qs(parsed.query).get("city", [""])[0]
            self._json({"categories": self.app.store.categories(city)})
            return
        if parsed.path == "/api/link-reviews":
            query = parse_qs(parsed.query)
            try:
                self._json(self.app.store.link_review_queue(
                    query.get("status", ["pending"])[0], query.get("city", [""])[0],
                    query.get("summary", ["0"])[0] == "1",
                ))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/workbook-reviews":
            query = parse_qs(parsed.query)
            try:
                self._json(self.app.store.workbook_review_queue(
                    query.get("status", ["pending"])[0],
                    query.get("city", [""])[0],
                    query.get("summary", ["0"])[0] == "1",
                ))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if parsed.path == "/api/config":
            self._json({
                "adobe_pdf_embed_client_id": self.app.adobe_pdf_embed_client_id,
                "smart_search_model": getattr(self.app.store.gemini_client, "model", ""),
                "knowledge_chat_model": getattr(self.app.store.knowledge_gemini_client, "model", ""),
                "knowledge_router_model": getattr(self.app.store.knowledge_router_client, "model", ""),
            })
            return
        if parsed.path == "/api/ingestion":
            self._json(self.app.ingestion_admin.snapshot())
            return
        conversation_match = re.fullmatch(r"/api/conversations/([A-Za-z0-9_-]+)", parsed.path)
        if conversation_match:
            try:
                self._json(self.app.store.knowledge_chat.conversation(conversation_match.group(1)))
            except KeyError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc.args[0]))
            return
        result_set_match = re.fullmatch(r"/api/result-sets/([A-Za-z0-9_-]+)/comments", parsed.path)
        if result_set_match:
            try:
                self._json(self.app.store.knowledge_chat.result_comments(result_set_match.group(1)))
            except KeyError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc.args[0]))
            return
        source_match = re.fullmatch(r"/api/sources/([A-Za-z0-9-]+)", parsed.path)
        if source_match:
            try:
                self._json(self.app.store.source_registry.public_source(source_match.group(1)))
            except (PermissionError, KeyError, FileNotFoundError, ValueError, RuntimeError) as exc:
                self._registry_error(exc)
            return
        document_match = re.fullmatch(r"/api/documents/([A-Za-z0-9-]+)/(preview|original|spreadsheet)", parsed.path)
        if document_match:
            document_id, action = document_match.groups()
            if action == "original":
                self._error(HTTPStatus.GONE, "Original-file downloads are disabled; use the in-app viewer")
                return
            if action == "preview":
                self._serve_document(document_id, action)
                return
            query = parse_qs(parsed.query)
            try:
                payload = self.app.store.source_registry.spreadsheet(
                    document_id,
                    query.get("sheet", [""])[0],
                    query.get("range", [""])[0],
                    int(query.get("page", ["1"])[0]),
                    int(query.get("page_size", ["100"])[0]),
                    query.get("context_range", [""])[0],
                )
                self._json(payload)
            except (PermissionError, KeyError, FileNotFoundError, ValueError, RuntimeError, TypeError) as exc:
                self._registry_error(exc)
            return
        if parsed.path == "/source":
            self._error(HTTPStatus.GONE, "Filesystem source links were replaced by the in-app Source Viewer")
            return
        self._serve_static(parsed.path)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        upload_match = re.fullmatch(
            r"/api/ingestion/uploads/(upl-[A-Za-z0-9]+)/files/(file-[A-Za-z0-9]+)",
            parsed.path,
        )
        if not upload_match:
            self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "-1"))
            if content_length < 0:
                raise ValueError("A valid Content-Length is required")
            upload_id, file_id = upload_match.groups()
            self._json(
                self.app.ingestion_admin.upload_file(
                    upload_id, file_id, self.rfile, content_length,
                )
            )
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except (ValueError, TypeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to store uploaded file")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/chat/query-plan":
                message = str(payload.get("message") or payload.get("query") or "").strip()
                if not message:
                    raise ValueError("message is required")
                has_previous = bool(payload.get("has_previous_result_set"))
                scope = (
                    dict(payload.get("scope"))
                    if isinstance(payload.get("scope"), dict)
                    else dict(payload.get("filters"))
                    if isinstance(payload.get("filters"), dict)
                    else {}
                )
                city_id = str(payload.get("city_id") or "").strip()
                if city_id and "city" not in scope and "city_ids" not in scope:
                    scope["city"] = city_id
                self._json({
                    "query_plan": enrich_query_plan(
                        fallback_query_plan(message, has_previous),
                        message,
                        has_previous,
                        scope,
                    ),
                    "gemini_used": False,
                    "retrieval_stage": 0,
                })
                return
            tag_decision = re.fullmatch(
                r"/api/admin/tag-suggestions/([^/]+)/(confirm|reject)", parsed.path,
            )
            if tag_decision:
                suggestion_id, decision = tag_decision.groups()
                self._json(self.app.store.set_tag_suggestion(suggestion_id, decision))
                return
            if parsed.path == "/api/search/progressive/expand":
                query = str(payload.get("query", ""))
                if not query.strip():
                    raise ValueError("Search query is required")
                scope = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
                result = self.app.store.progressive_search(
                    query,
                    city=str(payload.get("city", scope.get("city", ""))),
                    discipline=str(payload.get("discipline", scope.get("discipline", ""))),
                    category=str(payload.get("category", scope.get("category", ""))),
                    intent=str(payload.get("intent", "precedent_search")),
                    filters=scope,
                    force_stage3=True,
                )
                result["expanded"] = True
                self._json(result)
                return
            if parsed.path == "/api/search/progressive":
                query = str(payload.get("query", ""))
                if not query.strip():
                    raise ValueError("Search query is required")
                result = self.app.store.progressive_search(
                    query,
                    city=str(payload.get("city", "")),
                    discipline=str(payload.get("discipline", "")),
                    category=str(payload.get("category", "")),
                    intent=str(payload.get("intent", "precedent_search")),
                    filters=payload.get("filters") if isinstance(payload.get("filters"), dict) else None,
                    force_stage3=bool(payload.get("force_stage3")),
                )
                self._json(result)
                return
            if parsed.path == "/api/search":
                city = str(payload.get("city", ""))
                query = str(payload.get("query", ""))
                if not query.strip():
                    raise ValueError("Search query is required")
                if len(query) > 50_000:
                    raise ValueError("New comment is too long")
                limit = int(payload.get("limit", 5))
                result = self.app.store.gemini_search(
                    city, query, limit,
                    str(payload.get("discipline", "")),
                    str(payload.get("category", "")),
                )
                self._json(result)
                return
            if parsed.path == "/api/knowledge-chat":
                self._json(self.app.store.knowledge_chat.chat(payload))
                return
            if parsed.path == "/api/ingestion/uploads":
                files = payload.get("files")
                if not isinstance(files, list):
                    raise ValueError("files must be a list")
                self._json(self.app.ingestion_admin.initiate_upload(
                    str(payload.get("project_name", "")), files,
                ), status=HTTPStatus.CREATED)
                return
            upload_complete = re.fullmatch(
                r"/api/ingestion/uploads/(upl-[A-Za-z0-9]+)/complete", parsed.path,
            )
            if upload_complete:
                self._json(
                    self.app.ingestion_admin.complete_upload(upload_complete.group(1)),
                    status=HTTPStatus.ACCEPTED,
                )
                return
            if parsed.path == "/api/ingestion":
                self._json(self.app.ingestion_admin.start(
                    str(payload.get("mode", "")),
                    str(payload.get("site_id", "")),
                    confirmed=bool(payload.get("confirmed")),
                ), status=HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/categories":
                comment_ids = payload.get("comment_ids", [])
                if not isinstance(comment_ids, list) or not all(isinstance(item, str) for item in comment_ids):
                    raise ValueError("comment_ids must be a list of strings")
                result = self.app.store.set_category(comment_ids, str(payload.get("category", "")))
                self._json(result)
                return
            if parsed.path == "/api/link-reviews":
                result = self.app.store.set_link_review(
                    str(payload.get("link_id", "")), str(payload.get("decision", "")),
                    str(payload.get("note", "")),
                )
                self._json(result)
                return
            if parsed.path == "/api/workbook-reviews":
                result = self.app.store.set_workbook_review(
                    str(payload.get("source_document", "")),
                    str(payload.get("decision", "")),
                    str(payload.get("note", "")),
                )
                self._json(result)
                return
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc.args[0] if exc.args else exc))
            return
        except (ValueError, TypeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
            return
        self._error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")

    def _serve_static(self, request_path: str, send_body: bool = True) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (self.app.static_root / relative).resolve()
        try:
            candidate.relative_to(self.app.static_root)
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, "Static file not found")
            return
        if not candidate.is_file():
            candidate = self.app.static_root / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") or content_type == "application/javascript" else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}")


class PermitServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], store: DatasetStore, static_root: Path, adobe_pdf_embed_client_id: str = "", ingestion_admin: IngestionAdmin | None = None, allowed_origins: str = ""):
        self.store = store
        self.static_root = static_root.resolve()
        self.adobe_pdf_embed_client_id = adobe_pdf_embed_client_id
        self.ingestion_admin = ingestion_admin or IngestionAdmin(
            Path(__file__).resolve().parents[1], enabled=False,
        )
        self.allowed_origins = frozenset(
            origin.strip().rstrip("/")
            for origin in allowed_origins.split(",")
            if origin.strip()
        )
        super().__init__(address, PermitHandler)

    def origin_allowed(self, origin: str) -> bool:
        return origin.strip().rstrip("/") in self.allowed_origins


def build_parser() -> argparse.ArgumentParser:
    workspace = Path(__file__).resolve().parents[1]
    configured_path = lambda name, default: Path(runtime_setting(name, str(default))).expanduser()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=runtime_setting("PERMIT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(runtime_setting("PERMIT_PORT", runtime_setting("PORT", "8000"))))
    parser.add_argument(
        "--allowed-origins",
        default=runtime_setting("PERMIT_ALLOWED_ORIGINS", ""),
        help="Comma-separated exact browser origins allowed to call the API",
    )
    parser.add_argument("--dataset", type=Path, default=configured_path("PERMIT_DATASET_PATH", workspace / "phase2_dataset" / "dataset.json"))
    parser.add_argument("--categories", type=Path, default=configured_path("PERMIT_CATEGORIES_PATH", workspace / "web_app" / "data" / "category_assignments.json"))
    parser.add_argument("--source-root", type=Path, default=configured_path("PERMIT_SOURCE_ROOT", workspace / "comments&response"))
    parser.add_argument("--static-root", type=Path, default=configured_path("PERMIT_STATIC_ROOT", workspace / "web_app" / "static"))
    parser.add_argument("--source-registry", type=Path, default=configured_path("PERMIT_SOURCE_REGISTRY_PATH", workspace / "web_app" / "data" / "source_registry.json"))
    parser.add_argument("--preview-root", type=Path, default=configured_path("PERMIT_PREVIEW_ROOT", workspace / "web_app" / "data" / "previews"))
    parser.add_argument("--enrichment", type=Path, default=configured_path("PERMIT_ENRICHMENT_PATH", workspace / "web_app" / "data" / "gemini_enrichment.json"))
    parser.add_argument("--search-index", type=Path, default=configured_path("PERMIT_SEARCH_INDEX_PATH", workspace / "web_app" / "data" / "search_index.json"))
    parser.add_argument("--link-reviews", type=Path, default=configured_path("PERMIT_LINK_REVIEWS_PATH", workspace / "web_app" / "data" / "link_review_decisions.json"))
    parser.add_argument("--workbook-reviews", type=Path, default=configured_path("PERMIT_WORKBOOK_REVIEWS_PATH", workspace / "web_app" / "data" / "workbook_review_decisions.json"))
    parser.add_argument("--gemini-model", default=runtime_setting("GEMINI_MODEL", "gemini-3.5-flash"))
    parser.add_argument(
        "--knowledge-gemini-model",
        default=runtime_setting("KNOWLEDGE_GEMINI_MODEL", "gemini-3.6-flash"),
        help="Gemini model used for Knowledge Chat grounded answer synthesis",
    )
    parser.add_argument(
        "--knowledge-router-model",
        default=runtime_setting("KNOWLEDGE_ROUTER_MODEL", "gemini-3.1-flash-lite"),
        help="Low-cost Gemini model used only to choose direct, evidence reuse, or search",
    )
    parser.add_argument("--gemini-api-key-stdin", action="store_true", help="Read Gemini key from a hidden startup prompt")
    parser.add_argument(
        "--enable-ingestion-admin", action="store_true",
        help="Enable browser-triggered ingestion when the server is not bound to a loopback host",
    )
    parser.add_argument(
        "--adobe-pdf-embed-client-id",
        default=runtime_setting("ADOBE_PDF_EMBED_CLIENT_ID", "da40245968664bb9bf47141e8e0e9195"),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    workspace = Path(__file__).resolve().parents[1]
    try:
        shared_gemini_api_key = gemini_api_key()
        if not shared_gemini_api_key and args.gemini_api_key_stdin:
            shared_gemini_api_key = getpass.getpass("Gemini API key: ")
        gemini_client = GeminiClient(shared_gemini_api_key, args.gemini_model) if shared_gemini_api_key else None
        knowledge_gemini_client = GeminiClient(shared_gemini_api_key, args.knowledge_gemini_model) if shared_gemini_api_key else None
        knowledge_router_client = GeminiClient(shared_gemini_api_key, args.knowledge_router_model) if shared_gemini_api_key else None
        store = DatasetStore(
            args.dataset, args.categories, args.source_root,
            args.source_registry, args.preview_root, args.enrichment, args.search_index,
            gemini_client=gemini_client,
            knowledge_gemini_client=knowledge_gemini_client,
            knowledge_router_client=knowledge_router_client,
            link_reviews_path=args.link_reviews,
            workbook_reviews_path=args.workbook_reviews,
        )

        def reload_after_ingestion() -> None:
            store.reload(force=True)
            try:
                store.source_registry.payload = json.loads(
                    store.source_registry.registry_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                pass
            store.search_index._load()

        loopback = args.host in {"127.0.0.1", "localhost", "::1"}
        ingestion_admin = IngestionAdmin(
            workspace,
            enabled=bool(loopback or args.enable_ingestion_admin),
            gemini_api_key=shared_gemini_api_key,
            on_dataset_changed=reload_after_ingestion,
        )
        server = PermitServer(
            (args.host, args.port), store, args.static_root,
            args.adobe_pdf_embed_client_id,
            ingestion_admin,
            args.allowed_origins,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Unable to start permit browser: {exc}")
        return 2
    browser_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0", "::1"} else args.host
    print(f"Permit browser: http://{browser_host}:{args.port}")
    print(f"Dataset: {args.dataset.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
