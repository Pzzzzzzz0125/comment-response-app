#!/usr/bin/env python3
"""Accuracy-first, full-document Gemini ingestion with independent verification."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from corpus_audit import audit_corpus as audit
from phase2 import extract_dataset as base
from phase2.spreadsheet_ingestion import (
    SPREADSHEET_PIPELINE_VERSION,
    SPREADSHEET_VERIFICATION_INSTRUCTION,
    SPREADSHEET_VERIFICATION_PROMPT_VERSION,
    SPREADSHEET_VERIFICATION_SCHEMA,
    build_spreadsheet_evidence,
    detect_spreadsheet_schemas,
    local_verification_result,
)
from web_app.source_registry import _xlsx_cells, xlsx_sheet_names


SUPPORTED_TYPES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv"}
PIPELINE_VERSION = "adaptive-document-ingestion-v4"
TEXT_EXTRACTION_VERSION = "native-text-v3-exact-xlsx-cells"
PAGE_SCREENING_VERSION = "all-page-routing-v3"
PAGE_RENDER_VERSION = "selected-page-render-v2"
EXTRACTION_PROMPT_VERSION = "adaptive-extraction-v4"
VERIFICATION_PROMPT_VERSION = "adaptive-verification-v4-visual-only-context"
PRESCAN_PROMPT_VERSION = "content-page-screening-v2"
MIN_VERIFIED_CONFIDENCE = 0.95
INITIAL_SCREEN_PAGES = 8
SCREENING_DPI = 120
LOW_TEXT_CHARACTER_LIMIT = 80

PROCESSING_STATUSES = {
    "pending", "classified", "comments_found", "responses_found",
    "comments_and_responses_found", "no_relevant_content", "needs_review", "failed",
}
PAGE_CLASSES = {
    "comment_list", "response_list", "comment_response_table", "drawing_markup",
    "design_drawing", "supporting_document", "uncertain",
}

COMMENT_TERMS = (
    "comments", "corrections required", "plan review", "review comments",
    "provide", "revise", "show", "clarify", "shall", "required",
)
RESPONSE_TERMS = (
    "response", "applicant response", "company response", "responses",
    "please see updated", "please see revised",
)
MARKUP_REFERENCE_RE = re.compile(
    r"\b(?:see|refer\s+to)\s+(?:the\s+)?(?:comments?|corrections?|annotations?|"
    r"changemarks?|markups?)\s+(?:marked|shown|noted|provided)?\s*(?:on|in)?\s*"
    r"(?:the\s+)?(?:plans?|drawings?|sheets?)\b|"
    r"\badditional comments? (?:are )?(?:shown|marked|annotated)\b|"
    r"\bcomments? (?:are )?annotated as\b",
    re.IGNORECASE,
)
NUMBERED_REQUIREMENT_RE = re.compile(
    r"(?:^|\n)\s*(?:comment\s*)?(?:#\s*)?(?:\d{1,3}|[A-Z]\d{1,3})[.)\]:-]\s+\S",
    re.IGNORECASE,
)
COMMENT_NUMBER_CAPTURE_RE = re.compile(
    r"(?:^|\n)\s*(?:comment\s*)?(?:#\s*)?"
    r"(?P<number>(?:\d{1,3}|[A-Z]\d{1,3})(?:\.[a-z0-9]+)?)"
    r"[.)\]:-]\s+\S",
    re.IGNORECASE,
)
RESPONSE_NUMBER_CAPTURE_RE = re.compile(
    r"(?:^|\n)\s*(?:re(?:sponse)?\s*[:.#-]?\s*)"
    r"(?P<number>\d{1,3}(?:\.[a-z0-9]+)?)\b",
    re.IGNORECASE,
)
CODE_CITATION_RE = re.compile(
    r"\b(?:CBC|CRC|CMC|CPC|CEC|NFPA|SMC|MMC|MPC|CFC|IBC|IRC)\s*"
    r"(?:§|section\s*)?\d",
    re.IGNORECASE,
)
DRAWING_RE = re.compile(
    r"\b(?:floor plan|site plan|elevation|section|detail|scale|sheet\s+[A-Z]\d|"
    r"structural calculations?|foundation plan)\b",
    re.IGNORECASE,
)

LOCATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "pages": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "description": {"type": "STRING"},
        "bounding_boxes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "page": {"type": "INTEGER"}, "x_min": {"type": "NUMBER"}, "y_min": {"type": "NUMBER"},
            "x_max": {"type": "NUMBER"}, "y_max": {"type": "NUMBER"},
        }, "required": ["page", "x_min", "y_min", "x_max", "y_max"]}},
    },
    "required": ["pages", "description", "bounding_boxes"],
}

EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "property": {"type": "STRING"}, "city": {"type": "STRING"},
        "review_round": {"type": "STRING"},
        "document_class": {"type": "STRING", "enum": [
            "government_comments", "company_response", "combined", "supporting", "uncertain",
        ]},
        "comment_section_complete": {"type": "BOOLEAN"},
        "comments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "record_key": {"type": "STRING"}, "comment_number": {"type": "STRING"},
            "department": {"type": "STRING"}, "reviewer": {"type": "STRING"},
            "exact_comment_text": {"type": "STRING"}, "normalized_comment_text": {"type": "STRING"},
            "page_start": {"type": "INTEGER"}, "page_end": {"type": "INTEGER"},
            "bounding_boxes": LOCATION_SCHEMA["properties"]["bounding_boxes"],
            "continues_from_previous_page": {"type": "BOOLEAN"},
            "continues_to_next_page": {"type": "BOOLEAN"},
            "confidence": {"type": "NUMBER"}, "uncertain": {"type": "BOOLEAN"},
            "uncertainty_reason": {"type": "STRING"},
        }, "required": [
            "record_key", "comment_number", "department", "reviewer", "exact_comment_text",
            "normalized_comment_text", "page_start", "page_end", "bounding_boxes",
            "continues_from_previous_page", "continues_to_next_page", "confidence",
            "uncertain", "uncertainty_reason",
        ]}},
        "responses": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "record_key": {"type": "STRING"}, "response_number": {"type": "STRING"},
            "exact_response_text": {"type": "STRING"},
            "page_start": {"type": "INTEGER"}, "page_end": {"type": "INTEGER"},
            "bounding_boxes": LOCATION_SCHEMA["properties"]["bounding_boxes"],
            "confidence": {"type": "NUMBER"}, "uncertain": {"type": "BOOLEAN"},
            "uncertainty_reason": {"type": "STRING"},
        }, "required": [
            "record_key", "response_number", "exact_response_text", "page_start", "page_end",
            "bounding_boxes", "confidence", "uncertain", "uncertainty_reason",
        ]}},
        "additional_markups_referenced": {"type": "BOOLEAN"},
        "review_reason": {"type": "STRING"},
    },
    "required": [
        "property", "city", "review_round", "document_class", "comment_section_complete",
        "comments", "responses", "additional_markups_referenced", "review_reason",
    ],
}

VERIFICATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "document_verified": {"type": "BOOLEAN"}, "every_comment_captured": {"type": "BOOLEAN"},
        "every_response_captured": {"type": "BOOLEAN"}, "verification_summary": {"type": "STRING"},
        "number_sequence_correct": {"type": "BOOLEAN"},
        "continuations_joined_correctly": {"type": "BOOLEAN"},
        "headers_excluded": {"type": "BOOLEAN"},
        "neighboring_items_separate": {"type": "BOOLEAN"},
        "no_response_leakage": {"type": "BOOLEAN"},
        "later_markup_check_complete": {"type": "BOOLEAN"},
        "verified_record_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
        "rejected_record_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing_visible_comments": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing_visible_responses": {"type": "ARRAY", "items": {"type": "STRING"}},
        "incorrect_links": {"type": "ARRAY", "items": {"type": "STRING"}},
        "incorrect_page_locations": {"type": "ARRAY", "items": {"type": "STRING"}},
        "duplicate_fragments": {"type": "ARRAY", "items": {"type": "STRING"}},
        "continuation_errors": {"type": "ARRAY", "items": {"type": "STRING"}},
        "comments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "record_key": {"type": "STRING"}, "comment_captured": {"type": "BOOLEAN"},
            "text_complete_and_verbatim": {"type": "BOOLEAN"}, "verified": {"type": "BOOLEAN"},
            "locations_and_boxes_correct": {"type": "BOOLEAN"},
            "uncertainty_reason": {"type": "STRING"},
        }, "required": [
            "record_key", "comment_captured", "text_complete_and_verbatim",
            "locations_and_boxes_correct", "verified", "uncertainty_reason",
        ]}},
        "responses": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "record_key": {"type": "STRING"}, "response_captured": {"type": "BOOLEAN"},
            "text_complete_and_verbatim": {"type": "BOOLEAN"}, "verified": {"type": "BOOLEAN"},
            "locations_and_boxes_correct": {"type": "BOOLEAN"},
            "uncertainty_reason": {"type": "STRING"},
        }, "required": [
            "record_key", "response_captured", "text_complete_and_verbatim",
            "locations_and_boxes_correct", "verified", "uncertainty_reason",
        ]}},
    },
    "required": [
        "document_verified", "every_comment_captured", "every_response_captured",
        "number_sequence_correct", "continuations_joined_correctly", "headers_excluded",
        "neighboring_items_separate", "no_response_leakage", "later_markup_check_complete",
        "verified_record_ids", "rejected_record_ids", "missing_visible_comments",
        "missing_visible_responses", "incorrect_links", "incorrect_page_locations",
        "duplicate_fragments", "continuation_errors",
        "verification_summary", "comments", "responses",
    ],
}

PRESCAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "files": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "relative_path": {"type": "STRING"},
            "decision": {"type": "STRING"},
            "document_role": {"type": "STRING"},
            "reason": {"type": "STRING"},
            "confidence": {"type": "NUMBER"},
            "linked_topics": {"type": "ARRAY", "items": {"type": "STRING"}},
        }, "required": [
            "relative_path", "decision", "document_role", "reason", "confidence", "linked_topics",
        ]}},
    },
    "required": ["files"],
}

EXTRACTION_INSTRUCTION = """You are transcribing the complete detected comment section of a permit-review document from rendered page images plus direct/OCR text.

When `known_context_hints.visual_batch` is present, the supplied images are one complete overlapping batch from a larger document. Extract every record visible on the supplied batch pages and judge completeness only for those supplied pages; do not mark the batch uncertain merely because other document pages are handled in separate batches.

Visually understand the actual document structure: tables, rows, numbering, headings, columns, form fields, continuation pages, and the spatial relationship between government comments and applicant/company responses.

Classify the document, then extract government comments and company responses into separate arrays. Do not match them in this step. Copy every item verbatim. Never summarize, paraphrase, correct spelling, improve grammar, merge neighboring items, include a reviewer header, include the tail of a previous row, omit repeated text, or invent missing text. Raw/OCR text is screening evidence only; page images control boundaries.

Join a comment that visibly continues across a page boundary, retaining every original character and line break. Keep neighboring comments separate. Record the printed comment/response number exactly. `normalized_comment_text` is additional retrieval text and must never replace `exact_comment_text`.

Locations must identify every source page and complete visible item using normalized coordinates from 0 to 1000 (top-left origin). Set uncertainty whenever any character, boundary, numbering, completeness, or location is not visually certain. Report whether later drawing markups are referenced. `review_reason` must be empty when the document is complete and certain; never use it to describe the document's subject. Return only the required JSON."""

VERIFICATION_INSTRUCTION = """Independently audit the proposed extraction against every original rendered page image. Do not trust the proposed JSON or raw text.

When `known_context_hints.visual_batch` is present, independently verify completeness for every supplied batch page. Other document pages are verified in separate batches and their absence from this request is not a failure.

Verify that every numbered comment and response was captured, text is complete and verbatim, cross-page continuations were joined correctly, headers were excluded, neighboring comments remain separate, response text did not leak into a government comment, the visible number sequence agrees with the extraction, and later drawing-markup references were handled. Mark the entire document unverified if any item is missing, duplicated, combined incorrectly, truncated, paraphrased, or incorrectly located. Explicitly return verified and rejected record IDs plus every missing visible comment/response, incorrect link/location, duplicate fragment, and continuation error; use empty arrays when none exist. Do not perform comment-response matching. Return only the required JSON."""

PRESCAN_INSTRUCTION = """You are prioritizing permit-review files before local content/page screening.

Classify each file independently using the filename, audit metadata, and short direct-text snippet. Return:
- decision `full_read` for files that may contain government comments, applicant/company responses, or a visible comment-response table/letter that should become searchable records.
- decision `context_only` for plan sets, calculations, reports, checklist/support documents, geotechnical/foundation/arborist/special-inspection files that should be kept as secondary source evidence but not extracted as comment-response records.
- decision `skip` is only a low-priority hint. Every supported file will still be opened and content-classified; an unfamiliar filename can never cause a silent skip.

Do not invent records or citations. This is only routing. When unsure between `full_read` and `context_only`, choose `full_read` for potential comment/response sources and `context_only` for supporting documents. Return only the required JSON."""


@dataclass(slots=True)
class PageImage:
    page_number: int
    path: Path
    mime_type: str = "image/jpeg"


@dataclass(slots=True)
class EvidenceBundle:
    artifact_id: str
    source_path: Path
    source_sha256: str
    original_type: str
    raw_text: dict[str, Any]
    pages: list[PageImage]
    artifact_dir: Path
    document_page_count: int = 0
    screening: dict[str, Any] = field(default_factory=dict)


class VisualClient(Protocol):
    def pre_scan_sources(self, files: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]: ...
    def extract_document(self, bundle: EvidenceBundle, context: dict[str, Any]) -> dict[str, Any]: ...
    def verify_document(self, bundle: EvidenceBundle, extraction: dict[str, Any]) -> dict[str, Any]: ...
    def verify_spreadsheet_units(
        self, packet: dict[str, Any], context: dict[str, Any],
    ) -> dict[str, Any]: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.stem + "-", suffix=".tmp", delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _run(command: list[str], purpose: str, timeout: int = 600) -> None:
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{purpose} failed ({completed.returncode}): {detail[:1000]}")


def render_pdf_pages(path: Path, output_dir: Path, dpi: int = 180) -> list[PageImage]:
    ghostscript = shutil.which("gs")
    if not ghostscript:
        raise RuntimeError("Full-page visual ingestion requires Ghostscript (gs)")
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "page-%04d.jpg"
    _run([
        ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=jpeg",
        f"-r{dpi}", "-dJPEGQ=95", f"-sOutputFile={pattern}", str(path.resolve()),
    ], f"Rendering {path.name}")
    paths = sorted(output_dir.glob("page-*.jpg"))
    if not paths:
        raise RuntimeError(f"No rendered pages were produced for {path.name}")
    return [PageImage(index, page) for index, page in enumerate(paths, 1)]


def render_pdf_page_selection(
    path: Path, output_dir: Path, page_numbers: list[int], dpi: int,
) -> list[PageImage]:
    """Render only selected source pages while preserving original page numbers."""
    ghostscript = shutil.which("gs")
    if not ghostscript:
        raise RuntimeError("Visual ingestion requires Ghostscript (gs)")
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[PageImage] = []
    for page_number in sorted(set(page_numbers)):
        destination = output_dir / f"page-{page_number:04d}.jpg"
        if not destination.is_file():
            _run([
                ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                "-sDEVICE=jpeg", f"-r{dpi}", "-dJPEGQ=92",
                f"-dFirstPage={page_number}", f"-dLastPage={page_number}",
                f"-sOutputFile={destination}", str(path.resolve()),
            ], f"Rendering page {page_number} of {path.name}")
        rendered.append(PageImage(page_number, destination))
    return rendered


def ocr_page(image: Path) -> str:
    executable = shutil.which("tesseract")
    if not executable:
        return ""
    completed = subprocess.run(
        [executable, str(image), "stdout", "--psm", "6"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=180, check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""


def pdf_page_features(path: Path, page_count: int) -> dict[str, Any]:
    """Inspect annotations/widgets per page when PyMuPDF is available.

    If it is unavailable, detect document-level PDF markers and conservatively
    escalate the whole document rather than silently skipping possible markups.
    """
    empty = [{
        "page": page,
        "annotation_count": 0,
        "form_field_count": 0,
        "drawing_object_count": 0,
        "annotation_types": [],
    } for page in range(1, page_count + 1)]
    try:
        import fitz  # type: ignore
    except ImportError:
        payload = path.read_bytes()
        markers = [
            marker for marker, token in (
                ("annotations", b"/Annots"),
                ("acroform", b"/AcroForm"),
            )
            if token in payload
        ]
        return {
            "supported": False,
            "pages": empty,
            "document_markers": markers,
            "conservative_full_document_escalation": bool(markers),
        }
    document = fitz.open(path)
    pages: list[dict[str, Any]] = []
    try:
        for page_number in range(document.page_count):
            page = document.load_page(page_number)
            annotations = list(page.annots() or [])
            widgets = list(page.widgets() or [])
            annotation_types = sorted({
                str(getattr(annotation, "type", ("", ""))[1] or "")
                for annotation in annotations
            })
            try:
                drawings = len(page.get_drawings())
            except (AttributeError, RuntimeError, ValueError):
                drawings = 0
            pages.append({
                "page": page_number + 1,
                "annotation_count": len(annotations),
                "form_field_count": len(widgets),
                "drawing_object_count": drawings,
                "annotation_types": annotation_types,
            })
    finally:
        document.close()
    return {
        "supported": True,
        "pages": pages,
        "document_markers": [],
        "conservative_full_document_escalation": False,
    }


def page_signal_classification(
    text: str,
    page_number: int,
    *,
    native_text: str | None = None,
    ocr_required: bool = False,
    annotation_count: int = 0,
    form_field_count: int = 0,
) -> dict[str, Any]:
    """Classify a page from native/OCR text without treating filenames as evidence."""
    normalized = normalized_whitespace(text)
    lowered = normalized.casefold()
    comment_hits = [term for term in COMMENT_TERMS if term in lowered]
    response_hits = [term for term in RESPONSE_TERMS if term in lowered]
    numbered = len(NUMBERED_REQUIREMENT_RE.findall(text or ""))
    code_hits = len(CODE_CITATION_RE.findall(text or ""))
    drawing_hits = len(DRAWING_RE.findall(text or ""))
    instruction_hits = sum(
        len(re.findall(rf"\b{term}\b", lowered))
        for term in ("provide", "revise", "show", "clarify")
    )
    comment_score = len(comment_hits) + min(numbered, 3) + min(code_hits, 2) + min(instruction_hits, 3)
    response_score = len(response_hits)
    comment_numbers = [
        match.group("number") for match in COMMENT_NUMBER_CAPTURE_RE.finditer(text or "")
    ]
    response_numbers = [
        match.group("number") for match in RESPONSE_NUMBER_CAPTURE_RE.finditer(text or "")
    ]
    markup_reference = bool(MARKUP_REFERENCE_RE.search(text or ""))
    table_header = (
        "review comments" in lowered and "applicant response" in lowered
    )
    explicit_comment_heading = any(
        term in lowered for term in (
            "plan check comments", "corrections required", "review comments",
            "plan review comments",
        )
    )
    if table_header:
        page_class = "comment_response_table"
    elif explicit_comment_heading and comment_score >= 2 and response_score >= 1:
        page_class = "comment_response_table"
    elif explicit_comment_heading and comment_score >= 2:
        page_class = "comment_list"
    elif response_score >= 2:
        page_class = "response_list"
    elif markup_reference or ("markup" in lowered and numbered):
        page_class = "drawing_markup"
    elif drawing_hits >= 1:
        page_class = "design_drawing"
    elif comment_score >= 3 and (numbered or code_hits or instruction_hits >= 2):
        page_class = "comment_list"
    elif len(normalized) < LOW_TEXT_CHARACTER_LIMIT:
        page_class = "uncertain"
    else:
        page_class = "supporting_document"
    confidence = {
        "comment_response_table": 0.92,
        "comment_list": 0.88,
        "response_list": 0.88,
        "drawing_markup": 0.78,
        "design_drawing": 0.82,
        "supporting_document": 0.75,
        "uncertain": 0.35,
    }[page_class]
    drawing_likelihood = min(
        1.0,
        (0.55 if page_class in {"design_drawing", "drawing_markup"} else 0.0)
        + min(drawing_hits, 4) * 0.1
        + (0.15 if len(normalized) < LOW_TEXT_CHARACTER_LIMIT else 0.0),
    )
    native_value = text if native_text is None else native_text
    return {
        "page": page_number, "page_class": page_class, "confidence": confidence,
        "native_text_length": len(normalized_whitespace(native_value or "")),
        "ocr_required": bool(ocr_required),
        # The current Ghostscript inspection path cannot reliably enumerate
        # page annotations or AcroForm widgets. Keep explicit zero counts and
        # capability metadata rather than pretending those pages were skipped.
        "annotation_count": max(0, int(annotation_count)),
        "form_field_count": max(0, int(form_field_count)),
        "annotation_inspection_supported": False,
        "drawing_likelihood": round(drawing_likelihood, 4),
        "comment_signal_score": round(min(1.0, comment_score / 8.0), 4),
        "response_signal_score": round(min(1.0, response_score / 5.0), 4),
        "page_fingerprint": hashlib.sha256(
            normalized.casefold().encode("utf-8")
        ).hexdigest(),
        "processing_decision": "pending_routing",
        "detected_comment_numbers": list(dict.fromkeys(comment_numbers)),
        "detected_response_numbers": list(dict.fromkeys(response_numbers)),
        "native_or_ocr_character_count": len(normalized),
        "signals": sorted(set([
            *comment_hits, *response_hits,
            *(["numbered_requirements"] if numbered else []),
            *(["code_citations"] if code_hits else []),
            *(["drawing_terms"] if drawing_hits else []),
            *(["additional_markup_reference"] if markup_reference else []),
        ])),
        "additional_markup_referenced": markup_reference,
    }


def select_relevant_pages(
    page_texts: list[str], initial_pages: int = INITIAL_SCREEN_PAGES,
) -> dict[str, Any]:
    """Select the complete first comment section and conservatively flag uncertainty."""
    classifications = [
        page_signal_classification(text, index)
        for index, text in enumerate(page_texts, 1)
    ]
    relevant_classes = {"comment_list", "response_list", "comment_response_table"}
    initial = classifications[:initial_pages]
    initial_relevant = [row["page"] for row in initial if row["page_class"] in relevant_classes]
    additional_markup = any(row["additional_markup_referenced"] for row in initial)
    selected: set[int] = set(initial_relevant)
    first_relevant = min(initial_relevant) if initial_relevant else None
    pending_gap: list[int] = []
    consecutive_non_comment = 0
    transitioned = False
    if first_relevant is not None:
        for row in classifications[first_relevant:]:
            page = int(row["page"])
            if row["page_class"] in relevant_classes:
                selected.update(pending_gap)
                pending_gap.clear()
                selected.add(page)
                consecutive_non_comment = 0
            else:
                pending_gap.append(page)
                consecutive_non_comment += 1
                if consecutive_non_comment >= 3 and row["page_class"] in {
                    "design_drawing", "supporting_document",
                }:
                    transitioned = True
                    break
    if additional_markup:
        selected.update(
            int(row["page"]) for row in classifications
            if row["page_class"] == "drawing_markup"
        )
    uncertain_later = [
        int(row["page"]) for row in classifications[initial_pages:]
        if additional_markup and row["page_class"] == "uncertain"
    ]
    if not selected:
        status = "needs_review" if any(row["page_class"] == "uncertain" for row in initial) else "no_relevant_content"
    elif uncertain_later:
        status = "needs_review"
    else:
        has_comments = any(
            row["page"] in selected and row["page_class"] in {"comment_list", "comment_response_table"}
            for row in classifications
        )
        has_responses = any(
            row["page"] in selected and row["page_class"] in {"response_list", "comment_response_table"}
            for row in classifications
        )
        status = (
            "comments_and_responses_found" if has_comments and has_responses
            else "comments_found" if has_comments else "responses_found"
        )
    reason = ""
    if uncertain_later:
        reason = f"Could not rule out additional marked comments on pages {uncertain_later}"
    elif not selected and status == "needs_review":
        reason = "The first-page content is visually/textually uncertain"
    for row in classifications:
        page = int(row["page"])
        if page in selected:
            row["processing_decision"] = "full_gemini_extraction"
        elif row["page_class"] == "uncertain":
            row["processing_decision"] = "lightweight_screen_needs_review"
        else:
            row["processing_decision"] = (
                f"lightweight_screen_only:{row['page_class']}"
            )
    return {
        "pipeline_version": PIPELINE_VERSION,
        "page_screening_version": PAGE_SCREENING_VERSION,
        "page_count": len(page_texts),
        "initial_pages_screened": min(initial_pages, len(page_texts)),
        "pages_screened": [row["page"] for row in classifications],
        "pages_selected_for_full_analysis": sorted(selected),
        "pages_escalated": sorted(page for page in selected if page > initial_pages),
        "page_manifest": classifications,
        "page_classifications": classifications,
        "additional_markup_detected": additional_markup,
        "comment_section_transition_detected": transitioned,
        "processing_status": status,
        "review_reason": reason,
    }


def pdf_direct_text(path: Path) -> dict[str, Any]:
    ghostscript = shutil.which("gs")
    if not ghostscript:
        raise RuntimeError("Direct PDF text extraction requires Ghostscript (gs)")
    with tempfile.TemporaryDirectory(prefix="visual-text-") as temporary:
        pattern = Path(temporary) / "page-%04d.txt"
        _run([
            ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=txtwrite",
            f"-sOutputFile={pattern}", str(path.resolve()),
        ], f"Extracting direct text from {path.name}")
        pages = sorted(Path(temporary).glob("page-*.txt"))
        return {"kind": "pdf_text_pages", "pages": [
            {"page": index, "text": item.read_text(encoding="utf-8", errors="replace")}
            for index, item in enumerate(pages, 1)
        ]}


def docx_direct_text(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        comments_by_id: dict[str, str] = {}
        if "word/comments.xml" in archive.namelist():
            comments_root = ET.fromstring(archive.read("word/comments.xml"))
            for comment in (
                node for node in comments_root.iter()
                if audit.xml_local(node.tag) == "comment"
            ):
                identifier = next((
                    str(value) for key, value in comment.attrib.items()
                    if audit.xml_local(key) == "id"
                ), "")
                comments_by_id[identifier] = "".join(
                    child.text or "" for child in comment.iter()
                    if audit.xml_local(child.tag) == "t"
                )
    blocks: list[dict[str, Any]] = []
    body = next((node for node in root.iter() if audit.xml_local(node.tag) == "body"), root)
    for index, node in enumerate(list(body), 1):
        kind = audit.xml_local(node.tag)
        if kind == "p":
            text = "".join(child.text or "" for child in node.iter() if audit.xml_local(child.tag) == "t")
            style = next((
                str(value)
                for child in node.iter()
                if audit.xml_local(child.tag) == "pStyle"
                for key, value in child.attrib.items()
                if audit.xml_local(key) == "val"
            ), "")
            comment_ids = list(dict.fromkeys(
                str(value)
                for child in node.iter()
                if audit.xml_local(child.tag) in {
                    "commentRangeStart", "commentReference",
                }
                for key, value in child.attrib.items()
                if audit.xml_local(key) == "id"
            ))
            blocks.append({
                "index": index,
                "kind": "paragraph",
                "text": text,
                "style": style,
                "is_heading": style.casefold().startswith("heading"),
                "comment_ids": comment_ids,
                "comments": [
                    comments_by_id[item] for item in comment_ids
                    if item in comments_by_id
                ],
            })
        elif kind == "tbl":
            rows = []
            for row_number, row in enumerate(
                (item for item in node.iter() if audit.xml_local(item.tag) == "tr"),
                1,
            ):
                cells = []
                for column_number, cell in enumerate(
                    (item for item in row if audit.xml_local(item.tag) == "tc"),
                    1,
                ):
                    cells.append({
                        "cell": f"R{row_number}C{column_number}",
                        "text": "".join(
                            child.text or "" for child in cell.iter()
                            if audit.xml_local(child.tag) == "t"
                        ),
                    })
                rows.append(cells)
            blocks.append({"index": index, "kind": "table", "rows": rows})
    return {"kind": "docx_blocks", "blocks": blocks}


def xlsx_direct_text(path: Path) -> dict[str, Any]:
    sheets: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        exact_shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(
                archive.read("xl/sharedStrings.xml")
            )
            exact_shared_strings = [
                "".join(
                    node.text or ""
                    for node in item.iter()
                    if audit.xml_local(node.tag) == "t"
                )
                for item in shared_root
                if audit.xml_local(item.tag) == "si"
            ]
        targets = dict(audit.workbook_sheet_targets(archive))
        for sheet, target in targets.items():
            rows = _xlsx_cells(path, sheet)
            root = ET.fromstring(archive.read(target))
            merged_ranges = [
                str(node.attrib.get("ref", ""))
                for node in root.iter()
                if audit.xml_local(node.tag) == "mergeCell"
                and str(node.attrib.get("ref", "")).strip()
            ]
            hidden_rows = {
                int(node.attrib.get("r") or 0)
                for node in root.iter()
                if audit.xml_local(node.tag) == "row"
                and str(node.attrib.get("hidden", "")).casefold()
                in {"1", "true"}
                and str(node.attrib.get("r", "")).isdigit()
            }
            hidden_columns = [
                {
                    "min": int(node.attrib.get("min") or 0),
                    "max": int(node.attrib.get("max") or 0),
                }
                for node in root.iter()
                if audit.xml_local(node.tag) == "col"
                and str(node.attrib.get("hidden", "")).casefold()
                in {"1", "true"}
            ]
            cell_xml: dict[str, dict[str, str]] = {}
            for cell in (
                node for node in root.iter()
                if audit.xml_local(node.tag) == "c"
            ):
                address = str(cell.attrib.get("r", ""))
                cell_type = str(cell.attrib.get("t", ""))
                raw_value = next((
                    node.text or "" for node in cell
                    if audit.xml_local(node.tag) == "v"
                ), "")
                formula = "".join(
                    node.text or "" for node in cell
                    if audit.xml_local(node.tag) == "f"
                )
                if cell_type == "s" and raw_value:
                    try:
                        exact_value = exact_shared_strings[int(raw_value)]
                    except (ValueError, IndexError):
                        exact_value = raw_value
                elif cell_type == "inlineStr":
                    exact_value = "".join(
                        node.text or ""
                        for node in cell.iter()
                        if audit.xml_local(node.tag) == "t"
                    )
                elif cell_type == "b":
                    exact_value = "TRUE" if raw_value == "1" else "FALSE"
                else:
                    exact_value = raw_value
                cell_xml[address] = {
                    "raw_value": raw_value,
                    "exact_value": exact_value,
                    "formula": formula,
                    "cell_type": cell_type,
                }
            comments: dict[str, str] = {}
            has_drawing_objects = False
            relation_path = posixpath.join(
                posixpath.dirname(target), "_rels",
                posixpath.basename(target) + ".rels",
            )
            if relation_path in archive.namelist():
                relation_root = ET.fromstring(archive.read(relation_path))
                for relation in relation_root:
                    if str(relation.attrib.get("Type", "")).endswith(
                        "/drawing"
                    ):
                        has_drawing_objects = True
                    if not str(relation.attrib.get("Type", "")).endswith(
                        "/comments"
                    ):
                        continue
                    comment_path = posixpath.normpath(posixpath.join(
                        posixpath.dirname(target),
                        str(relation.attrib.get("Target", "")),
                    )).lstrip("/")
                    if comment_path not in archive.namelist():
                        continue
                    comment_root = ET.fromstring(archive.read(comment_path))
                    for comment in (
                        node for node in comment_root.iter()
                        if audit.xml_local(node.tag) == "comment"
                    ):
                        address = str(comment.attrib.get("ref", ""))
                        comments[address] = "".join(
                            node.text or "" for node in comment.iter()
                            if audit.xml_local(node.tag) == "t"
                        )
            addresses: list[str] = []
            header_columns: dict[str, str] = {}
            for row in rows:
                row["hidden"] = (
                    int(row.get("row_number") or 0) in hidden_rows
                )
                for cell in row.get("cells", []):
                    address = str(cell.get("address", ""))
                    addresses.append(address)
                    parsed = cell_xml.get(address, {})
                    cell["display_value"] = str(cell.get("value", ""))
                    cell["raw_value"] = str(parsed.get("raw_value", ""))
                    cell["cell_type"] = str(parsed.get("cell_type", ""))
                    if "exact_value" in parsed:
                        cell["value"] = str(parsed["exact_value"])
                    if parsed.get("formula"):
                        cell["formula"] = str(parsed["formula"])
                    if comments.get(address):
                        cell["comment"] = comments[address]
                    if int(row.get("row_number") or 0) <= 10:
                        header_columns[str(cell.get("column", ""))] = (
                            header_columns.get(str(cell.get("column", "")), "")
                            + " " + str(cell.get("value", ""))
                        ).strip()
            comment_columns = [
                column for column, value in header_columns.items()
                if any(term in value.casefold() for term in (
                    "comment", "correction", "requirement",
                ))
            ]
            response_columns = [
                column for column, value in header_columns.items()
                if any(term in value.casefold() for term in (
                    "response", "applicant", "resolution",
                ))
            ]
            sheets.append({
                "name": sheet,
                "rows": rows,
                "used_cell_addresses": addresses,
                "likely_comment_columns": comment_columns,
                "likely_response_columns": response_columns,
                "merged_ranges": merged_ranges,
                "hidden_rows": sorted(hidden_rows),
                "hidden_columns": hidden_columns,
                "has_drawing_objects": has_drawing_objects,
            })
    return {"kind": "xlsx_cells", "sheets": sheets}


def csv_direct_text(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    return {"kind": "csv_cells", "rows": [
        {"row_number": index, "values": values} for index, values in enumerate(rows, 1)
    ]}


def direct_text_for_source(path: Path) -> dict[str, Any]:
    """Extract the cheapest authoritative structure available for a source."""
    extension = path.suffix.casefold()
    if extension == ".pdf":
        return pdf_direct_text(path)
    if extension == ".docx":
        return docx_direct_text(path)
    if extension == ".xlsx":
        return xlsx_direct_text(path)
    if extension == ".csv":
        return csv_direct_text(path)
    return {
        "kind": "legacy_office",
        "text": "Direct parsing unavailable; rendered pages are authoritative.",
    }


def compact_direct_text_for_gemini(raw_text: dict[str, Any]) -> dict[str, Any]:
    """Remove structural noise while preserving every extracted source character."""
    kind = str(raw_text.get("kind", ""))
    if kind == "docx_blocks":
        blocks: list[dict[str, Any]] = []
        for block in raw_text.get("blocks", []):
            if not isinstance(block, dict):
                continue
            if block.get("kind") == "paragraph":
                text = str(block.get("text", ""))
                comments = [
                    str(value) for value in block.get("comments", [])
                    if str(value)
                ]
                if not text and not comments:
                    continue
                compact = {
                    "index": block.get("index"),
                    "kind": "paragraph",
                    "text": text,
                }
                if block.get("style"):
                    compact["style"] = block["style"]
                if block.get("is_heading"):
                    compact["is_heading"] = True
                comment_ids = [
                    str(value) for value in block.get("comment_ids", [])
                    if str(value)
                ]
                if comment_ids:
                    compact["comment_ids"] = comment_ids
                if comments:
                    compact["comments"] = comments
                blocks.append(compact)
            elif block.get("kind") == "table":
                rows = []
                for row in block.get("rows", []):
                    cells = [
                        {
                            "cell": str(cell.get("cell", "")),
                            "text": str(cell.get("text", "")),
                        }
                        for cell in row if isinstance(cell, dict)
                        and str(cell.get("text", ""))
                    ]
                    if cells:
                        rows.append(cells)
                if rows:
                    blocks.append({
                        "index": block.get("index"),
                        "kind": "table",
                        "rows": rows,
                    })
        return {"kind": kind, "blocks": blocks}
    if kind == "xlsx_cells":
        sheets = []
        for sheet in raw_text.get("sheets", []):
            if not isinstance(sheet, dict):
                continue
            compact_sheet = {
                "name": str(sheet.get("name", "")),
                "rows": copy.deepcopy(sheet.get("rows", [])),
            }
            for field in ("likely_comment_columns", "likely_response_columns"):
                values = sheet.get(field, [])
                if values:
                    compact_sheet[field] = copy.deepcopy(values)
            sheets.append(compact_sheet)
        return {"kind": kind, "sheets": sheets}
    return copy.deepcopy(raw_text)


def _fingerprint_text_values(value: Any, key: str = "") -> list[str]:
    """Collect content while excluding volatile page/cell ordinal metadata."""
    ignored = {
        "page", "page_number", "row_number", "index", "address",
        "x_min", "y_min", "x_max", "y_max",
    }
    if key in ignored:
        return []
    if isinstance(value, dict):
        result: list[str] = []
        for child_key in sorted(value):
            result.extend(_fingerprint_text_values(value[child_key], str(child_key)))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_fingerprint_text_values(item, key))
        return result
    if value is None:
        return []
    return [str(value)]


def normalized_content_fingerprint(raw_text: dict[str, Any], binary_sha256: str) -> str:
    """Return a safe pre-Gemini fingerprint for renamed or re-exported copies."""
    text = normalized_whitespace(
        "\n".join(_fingerprint_text_values(raw_text))
    ).casefold()
    # Empty/scanned documents must not all collapse into one logical document.
    if len(text) < LOW_TEXT_CHARACTER_LIMIT:
        return f"binary:{binary_sha256}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def office_pdf(path: Path, output_dir: Path) -> Path:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise RuntimeError(f"Rendering every page of {path.suffix.upper()} requires LibreOffice (soffice)")
    output_dir.mkdir(parents=True, exist_ok=True)
    converted = output_dir / f"{path.stem}.pdf"
    if converted.is_file():
        return converted
    _run([executable, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(path.resolve())], f"Converting {path.name} to PDF")
    if not converted.is_file():
        raise RuntimeError(f"LibreOffice did not produce a PDF for {path.name}")
    return converted


class DocumentEvidenceBuilder:
    def __init__(self, artifact_root: Path, dpi: int = 220):
        self.artifact_root = artifact_root.resolve()
        self.dpi = dpi

    def _raw_text(
        self,
        source: Path,
        digest: str,
        directory: Path,
    ) -> dict[str, Any]:
        raw_path = directory / "raw_text.json"
        identity_path = directory / "raw_text_cache_metadata.json"
        if raw_path.is_file() and identity_path.is_file():
            try:
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
                if identity == {
                    "source_sha256": digest,
                    "text_extraction_version": TEXT_EXTRACTION_VERSION,
                }:
                    return json.loads(raw_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        raw_text = direct_text_for_source(source)
        atomic_json(raw_path, raw_text)
        atomic_json(identity_path, {
            "source_sha256": digest,
            "text_extraction_version": TEXT_EXTRACTION_VERSION,
        })
        return raw_text

    def content_fingerprint(self, source: Path) -> tuple[str, str]:
        """Cache local extraction and fingerprint content before Gemini routing."""
        source = source.resolve()
        digest = sha256_file(source)
        directory = self.artifact_root / f"VI-{digest[:20]}"
        raw_text = self._raw_text(source, digest, directory)
        return digest, normalized_content_fingerprint(raw_text, digest)

    def build(self, source: Path) -> EvidenceBundle:
        source = source.resolve()
        extension = source.suffix.casefold()
        if extension not in SUPPORTED_TYPES:
            raise ValueError(f"Unsupported visual-ingestion file type: {extension or 'none'}")
        digest = sha256_file(source)
        artifact_id = f"VI-{digest[:20]}"
        directory = self.artifact_root / artifact_id
        pages_dir = directory / "pages"
        manifest_path = directory / "manifest.json"
        old_manifest: dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old_manifest = {}
        source_cache_valid = old_manifest.get("source_sha256") == digest
        screening_cache_valid = (
            source_cache_valid
            and old_manifest.get("page_screening_version") == PAGE_SCREENING_VERSION
            and int(old_manifest.get("screening_dpi") or 0) == SCREENING_DPI
        )
        render_cache_valid = (
            source_cache_valid
            and old_manifest.get("page_render_version") == PAGE_RENDER_VERSION
            and int(old_manifest.get("render_dpi") or 0) == self.dpi
        )
        cached_raw_path = directory / "raw_text.json"
        cached_screening_path = directory / "page_screening.json"
        if screening_cache_valid and render_cache_valid and cached_raw_path.is_file():
            cached_pages = [
                PageImage(int(row.get("page") or 0), pages_dir / str(row.get("filename", "")))
                for row in old_manifest.get("pages", []) if isinstance(row, dict)
            ]
            if all(page.page_number > 0 and page.path.is_file() for page in cached_pages):
                raw_text = json.loads(cached_raw_path.read_text(encoding="utf-8"))
                screening = (
                    json.loads(cached_screening_path.read_text(encoding="utf-8"))
                    if cached_screening_path.is_file()
                    else {
                        "pipeline_version": PIPELINE_VERSION,
                        "page_count": int(old_manifest.get("page_count") or len(cached_pages)),
                        "pages_screened": old_manifest.get("pages_screened", []),
                        "pages_selected_for_full_analysis": old_manifest.get("pages_fully_analyzed", []),
                        "processing_status": old_manifest.get("processing_status", "classified"),
                    }
                )
                selected = {page.page_number for page in cached_pages}
                raw_for_gemini = compact_direct_text_for_gemini(raw_text)
                if raw_text.get("kind") == "pdf_text_pages":
                    raw_for_gemini = {
                        "kind": "pdf_text_pages",
                        "pages": [
                            row for row in raw_text.get("pages", [])
                            if isinstance(row, dict) and int(row.get("page") or 0) in selected
                        ],
                        "selection_note": "Only adaptively selected comment/response pages are supplied for full analysis.",
                    }
                screening = copy.deepcopy(screening)
                screening["current_run_stage_timings"] = {
                    "text_extraction_seconds": 0.0,
                    "office_conversion_seconds": 0.0,
                    "page_screening_and_ocr_seconds": 0.0,
                    "selected_page_rendering_seconds": 0.0,
                }
                screening["local_stage_cache_hit"] = True
                return EvidenceBundle(
                    artifact_id, source, digest, extension.lstrip("."),
                    raw_for_gemini, cached_pages, directory,
                    document_page_count=int(old_manifest.get("page_count") or len(cached_pages)),
                    screening=screening,
                )
        raw_started = time.perf_counter()
        raw_text = self._raw_text(source, digest, directory)
        raw_seconds = time.perf_counter() - raw_started
        conversion_started = time.perf_counter()
        if extension == ".pdf":
            rendered_from = source
        else:
            rendered_from = office_pdf(source, directory / "rendered")
        conversion_seconds = time.perf_counter() - conversion_started

        screening: dict[str, Any]
        document_page_count: int
        screening_started = time.perf_counter()
        selected_render_seconds = 0.0
        if extension == ".pdf":
            native_pages = [
                str(row.get("text", "")) for row in raw_text.get("pages", [])
                if isinstance(row, dict)
            ]
            document_page_count = len(native_pages)
            if not document_page_count:
                raise RuntimeError(f"Could not determine the page count for {source.name}")
            screening_path = directory / "page_screening.json"
            if screening_cache_valid and screening_path.is_file():
                screening = json.loads(screening_path.read_text(encoding="utf-8"))
            else:
                native_features = pdf_page_features(
                    rendered_from, document_page_count,
                )
                features_by_page = {
                    int(row["page"]): row
                    for row in native_features.get("pages", [])
                    if isinstance(row, dict)
                }
                thumbnails_dir = directory / "screening_pages"
                thumbnails = sorted(thumbnails_dir.glob("page-*.jpg"))
                if len(thumbnails) != document_page_count:
                    if thumbnails_dir.exists():
                        shutil.rmtree(thumbnails_dir)
                    thumbnails = [
                        page.path for page in render_pdf_pages(
                            rendered_from, thumbnails_dir, SCREENING_DPI,
                        )
                    ]
                ocr_cache_path = directory / "ocr_text.json"
                ocr_cache: dict[str, str] = {}
                if ocr_cache_path.is_file():
                    try:
                        ocr_cache = json.loads(ocr_cache_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        ocr_cache = {}
                effective_pages = list(native_pages)
                ocr_pages: list[int] = []
                ocr_attempted_pages: list[int] = []
                for index, native_text in enumerate(native_pages, 1):
                    if len(normalized_whitespace(native_text)) >= LOW_TEXT_CHARACTER_LIMIT:
                        continue
                    ocr_attempted_pages.append(index)
                    key = str(index)
                    if key not in ocr_cache:
                        ocr_cache[key] = ocr_page(thumbnails[index - 1])
                    if len(normalized_whitespace(ocr_cache[key])) > len(normalized_whitespace(native_text)):
                        effective_pages[index - 1] = ocr_cache[key]
                        ocr_pages.append(index)
                atomic_json(ocr_cache_path, ocr_cache)
                screening = select_relevant_pages(effective_pages)
                screening["ocr_pages"] = ocr_pages
                screening["ocr_attempted_pages"] = ocr_attempted_pages
                screening["screening_dpi"] = SCREENING_DPI
                for row in screening.get("page_classifications", []):
                    page_number = int(row.get("page") or 0)
                    if not 1 <= page_number <= document_page_count:
                        continue
                    features = features_by_page.get(page_number, {})
                    row["native_text_length"] = len(
                        normalized_whitespace(native_pages[page_number - 1])
                    )
                    row["ocr_required"] = page_number in ocr_attempted_pages
                    row["annotation_count"] = int(
                        features.get("annotation_count") or 0
                    )
                    row["form_field_count"] = int(
                        features.get("form_field_count") or 0
                    )
                    row["annotation_types"] = features.get(
                        "annotation_types", []
                    )
                    row["annotation_inspection_supported"] = bool(
                        native_features.get("supported")
                    )
                    row["drawing_likelihood"] = round(max(
                        float(row.get("drawing_likelihood") or 0.0),
                        min(
                            1.0,
                            int(features.get("drawing_object_count") or 0)
                            / 100.0,
                        ),
                    ), 4)
                    row["page_fingerprint"] = sha256_file(
                        thumbnails[page_number - 1]
                    )
                feature_pages = {
                    page
                    for page, features in features_by_page.items()
                    if int(features.get("annotation_count") or 0) > 0
                    or int(features.get("form_field_count") or 0) > 0
                }
                if native_features.get("conservative_full_document_escalation"):
                    feature_pages = set(range(1, document_page_count + 1))
                    screening["routing_warning"] = (
                        "PDF annotation/form markers exist but per-page native "
                        "inspection is unavailable; every page was escalated"
                    )
                if feature_pages:
                    selected = set(
                        int(page) for page in screening.get(
                            "pages_selected_for_full_analysis", []
                        )
                    )
                    selected.update(feature_pages)
                    screening["pages_selected_for_full_analysis"] = sorted(
                        selected
                    )
                    screening["pages_escalated"] = sorted(
                        page for page in selected if page > INITIAL_SCREEN_PAGES
                    )
                    screening["additional_markup_detected"] = True
                    if screening.get("processing_status") == "no_relevant_content":
                        screening["processing_status"] = "classified"
                    for row in screening.get("page_classifications", []):
                        if int(row.get("page") or 0) in feature_pages:
                            row["processing_decision"] = (
                                "full_gemini_extraction:native_annotation_or_form"
                            )
                screening["page_manifest"] = screening.get(
                    "page_classifications", []
                )
                atomic_json(screening_path, screening)
                atomic_json(directory / "page_manifest.json", screening["page_manifest"])
            selected_numbers = [
                int(page) for page in screening.get("pages_selected_for_full_analysis", [])
            ]
            if not render_cache_valid and pages_dir.exists():
                shutil.rmtree(pages_dir)
            render_started = time.perf_counter()
            page_images = render_pdf_page_selection(
                rendered_from, pages_dir, selected_numbers, self.dpi,
            )
            selected_render_seconds = time.perf_counter() - render_started
            selected_set = set(selected_numbers)
            raw_text_for_gemini = {
                "kind": "pdf_text_pages",
                "pages": [
                    row for row in raw_text.get("pages", [])
                    if isinstance(row, dict) and int(row.get("page") or 0) in selected_set
                ],
                "selection_note": "Only adaptively selected comment/response pages are supplied for full analysis.",
            }
        else:
            # Preserve the structured DOCX/XLSX/CSV extraction as Gemini input,
            # but use the converted preview only for all-page routing. This
            # avoids sending every office-preview page at extraction DPI.
            routing_text = pdf_direct_text(rendered_from)
            native_pages = [
                str(row.get("text", "")) for row in routing_text.get("pages", [])
                if isinstance(row, dict)
            ]
            document_page_count = len(native_pages)
            if not document_page_count:
                raise RuntimeError(
                    f"Could not determine preview page count for {source.name}"
                )
            screening_path = directory / "page_screening.json"
            if screening_cache_valid and screening_path.is_file():
                screening = json.loads(screening_path.read_text(encoding="utf-8"))
            else:
                thumbnails_dir = directory / "screening_pages"
                thumbnails = sorted(thumbnails_dir.glob("page-*.jpg"))
                if len(thumbnails) != document_page_count:
                    if thumbnails_dir.exists():
                        shutil.rmtree(thumbnails_dir)
                    thumbnails = [
                        page.path for page in render_pdf_pages(
                            rendered_from, thumbnails_dir, SCREENING_DPI,
                        )
                    ]
                screening = select_relevant_pages(native_pages)
                screening["ocr_pages"] = []
                screening["ocr_attempted_pages"] = []
                screening["screening_dpi"] = SCREENING_DPI
                screening["format_routing"] = (
                    "structured_content_with_selected_visual_preview"
                )
                for row in screening.get("page_classifications", []):
                    page_number = int(row.get("page") or 0)
                    if not 1 <= page_number <= document_page_count:
                        continue
                    row["native_text_length"] = len(
                        normalized_whitespace(native_pages[page_number - 1])
                    )
                    row["page_fingerprint"] = sha256_file(
                        thumbnails[page_number - 1]
                    )
                screening["page_manifest"] = screening.get(
                    "page_classifications", []
                )
                atomic_json(screening_path, screening)
                atomic_json(
                    directory / "page_manifest.json", screening["page_manifest"],
                )
            selected_numbers = [
                int(page)
                for page in screening.get(
                    "pages_selected_for_full_analysis", []
                )
            ]
            structured_signal = page_signal_classification(
                "\n".join(_fingerprint_text_values(raw_text)), 1,
            )
            if (
                not selected_numbers
                and structured_signal["page_class"] in {
                    "comment_list", "response_list", "comment_response_table",
                }
            ):
                selected_numbers = list(
                    range(1, min(INITIAL_SCREEN_PAGES, document_page_count) + 1)
                )
                screening["pages_selected_for_full_analysis"] = selected_numbers
                screening["processing_status"] = "needs_review"
                screening["review_reason"] = (
                    "Structured content contains comment/response signals but "
                    "preview page routing was ambiguous"
                )
                for row in screening.get("page_manifest", []):
                    if int(row.get("page") or 0) in selected_numbers:
                        row["processing_decision"] = (
                            "full_gemini_extraction:structured_signal_fallback"
                        )
                atomic_json(screening_path, screening)
                atomic_json(
                    directory / "page_manifest.json",
                    screening.get("page_manifest", []),
                )
            if not render_cache_valid and pages_dir.exists():
                shutil.rmtree(pages_dir)
            render_started = time.perf_counter()
            page_images = render_pdf_page_selection(
                rendered_from, pages_dir, selected_numbers, self.dpi,
            )
            selected_render_seconds = time.perf_counter() - render_started
            raw_text_for_gemini = compact_direct_text_for_gemini(raw_text)
            raw_text_for_gemini["visual_page_selection"] = {
                "selected_preview_pages": selected_numbers,
                "preview_page_count": document_page_count,
                "selection_note": (
                    "The complete structured content is supplied. Only visually "
                    "relevant preview pages are rendered at extraction resolution."
                ),
            }
        screening["current_run_stage_timings"] = {
            "text_extraction_seconds": round(raw_seconds, 4),
            "office_conversion_seconds": round(conversion_seconds, 4),
            "page_screening_and_ocr_seconds": round(
                max(
                    0.0,
                    time.perf_counter() - screening_started
                    - selected_render_seconds,
                ),
                4,
            ),
            "selected_page_rendering_seconds": round(
                selected_render_seconds, 4,
            ),
        }
        screening["local_stage_cache_hit"] = False
        atomic_json(directory / "raw_text.json", raw_text)
        manifest = {
            "artifact_id": artifact_id, "source_filename": source.name, "source_sha256": digest,
            "original_type": extension.lstrip("."), "render_dpi": self.dpi,
            "pipeline_version": PIPELINE_VERSION,
            "text_extraction_version": TEXT_EXTRACTION_VERSION,
            "page_screening_version": PAGE_SCREENING_VERSION,
            "page_render_version": PAGE_RENDER_VERSION,
            "screening_dpi": SCREENING_DPI,
            "normalized_content_fingerprint": normalized_content_fingerprint(
                raw_text, digest
            ),
            "page_count": document_page_count,
            "pages_screened": screening.get("pages_screened", []),
            "pages_fully_analyzed": [page.page_number for page in page_images],
            "processing_status": screening.get("processing_status", "classified"),
            "pages": [
                {"page": page.page_number, "filename": page.path.name, "sha256": sha256_file(page.path)} for page in page_images
            ],
        }
        atomic_json(manifest_path, manifest)
        return EvidenceBundle(
            artifact_id, source, digest, extension.lstrip("."),
            raw_text_for_gemini, page_images, directory,
            document_page_count=document_page_count, screening=screening,
        )


def multimodal_context(bundle: EvidenceBundle, context: dict[str, Any], extracted: dict[str, Any] | None = None) -> dict[str, Any]:
    total_pages = int(context.get("document_page_count") or bundle.document_page_count or len(bundle.pages))
    introduction = {
        "document": {"filename": bundle.source_path.name, "type": bundle.original_type, "page_count": total_pages},
        "known_context_hints": context,
        "page_screening": bundle.screening,
    }
    if extracted is None:
        introduction["selected_page_text_complete"] = bundle.raw_text
    else:
        # The independent verification pass is image-authoritative. Re-sending
        # the complete DOCX/XLSX/PDF direct extraction duplicates a large part
        # of the first request and can bias verification toward the proposal.
        introduction["verification_evidence_policy"] = (
            "Verify against the original rendered page images. The complete "
            "direct-text payload was used only by the extraction pass."
        )
        introduction["proposed_extraction_to_verify"] = extracted
    return introduction


def multimodal_parts(bundle: EvidenceBundle, context: dict[str, Any], extracted: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    introduction = multimodal_context(bundle, context, extracted)
    total_pages = int(context.get("document_page_count") or bundle.document_page_count or len(bundle.pages))
    parts: list[dict[str, Any]] = [{"text": json.dumps(introduction, ensure_ascii=False)}]
    for page in bundle.pages:
        parts.append({"text": f"ORIGINAL RENDERED PAGE {page.page_number} OF {total_pages} — inspect the entire image."})
        parts.append({"inlineData": {
            "mimeType": page.mime_type,
            "data": base64.b64encode(page.path.read_bytes()).decode("ascii"),
        }})
    return parts


class VisualGeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash", timeout: int = 600, inline_limit_bytes: int = 18_000_000):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for visual ingestion")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.inline_limit_bytes = inline_limit_bytes
        self._uploaded_pages: dict[tuple[str, int], tuple[str, str]] = {}
        self.last_usage_metadata: dict[str, Any] = {}
        self.last_request_metadata: dict[str, Any] = {}

    def _upload_file(self, page: PageImage) -> tuple[str, str]:
        size = page.path.stat().st_size
        start = Request(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            data=json.dumps({"file": {"display_name": page.path.name}}).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key, "Content-Type": "application/json",
                "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": page.mime_type,
            }, method="POST",
        )
        try:
            with urlopen(start, timeout=self.timeout) as response:
                upload_url = response.headers.get("X-Goog-Upload-URL", "")
            if not upload_url:
                raise RuntimeError("Gemini Files API did not return an upload URL")
            upload = Request(
                upload_url, data=page.path.read_bytes(), headers={
                    "Content-Length": str(size), "Content-Type": page.mime_type,
                    "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                }, method="POST",
            )
            with urlopen(upload, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            file = body["file"]
            return str(file["uri"]), str(file.get("mimeType") or page.mime_type)
        except (HTTPError, OSError, URLError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini page upload failed for page {page.page_number}: {exc}") from exc

    def _parts(self, bundle: EvidenceBundle, context: dict[str, Any], extracted: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        introduction = multimodal_context(bundle, context, extracted)
        estimated_inline = len(json.dumps(introduction, ensure_ascii=False).encode("utf-8")) + sum(
            ((page.path.stat().st_size + 2) // 3) * 4 for page in bundle.pages
        )
        if estimated_inline <= self.inline_limit_bytes:
            return multimodal_parts(bundle, context, extracted)
        parts: list[dict[str, Any]] = [{"text": json.dumps(introduction, ensure_ascii=False)}]
        total_pages = int(context.get("document_page_count") or bundle.document_page_count or len(bundle.pages))
        for page in bundle.pages:
            key = (bundle.artifact_id, page.page_number)
            if key not in self._uploaded_pages:
                self._uploaded_pages[key] = self._upload_file(page)
            uri, mime_type = self._uploaded_pages[key]
            parts.append({"text": f"ORIGINAL RENDERED PAGE {page.page_number} OF {total_pages} — inspect the entire image."})
            parts.append({"fileData": {"mimeType": mime_type, "fileUri": uri}})
        return parts

    @staticmethod
    def _read_with_deadline(response: Any, deadline: float) -> bytes:
        """Read a response with a true wall-clock deadline, not idle timeout."""
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Gemini response exceeded the hard deadline")
            socket = getattr(
                getattr(getattr(response, "fp", None), "raw", None),
                "_sock", None,
            )
            if socket is not None:
                socket.settimeout(max(1.0, min(30.0, remaining)))
            chunk = response.read(64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _request(
        self,
        instruction: str,
        parts: list[dict[str, Any]],
        schema: dict[str, Any],
        max_output_tokens: int = 32768,
    ) -> dict[str, Any]:
        payload = {
            "systemInstruction": {"parts": [{"text": instruction}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json", "responseSchema": schema,
            },
        }
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model, safe='')}:generateContent"
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_started = time.monotonic()
        deadline = request_started + self.timeout
        attempts = 0
        for attempt in range(5):
            attempts = attempt + 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            request = Request(endpoint, data=encoded, headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key}, method="POST")
            try:
                with urlopen(
                    # Gemini may legitimately spend several minutes before
                    # sending the first response byte. Use the remaining hard
                    # deadline here; short idle timeouts created false retries
                    # and multiplied both latency and cost.
                    request, timeout=max(1.0, remaining),
                ) as response:
                    body = json.loads(
                        self._read_with_deadline(
                            response, deadline,
                        ).decode("utf-8")
                    )
                self.last_usage_metadata = (
                    body.get("usageMetadata", {})
                    if isinstance(body.get("usageMetadata"), dict) else {}
                )
                self.last_request_metadata = {
                    "attempts": attempts,
                    "request_bytes": len(encoded),
                    "elapsed_seconds": round(
                        time.monotonic() - request_started, 4,
                    ),
                    "model": self.model,
                }
                candidate = body["candidates"][0]
                finish = str(candidate.get("finishReason", "STOP"))
                if finish not in {"STOP", ""}:
                    raise RuntimeError(f"Gemini stopped before a complete result: {finish}")
                raw = "".join(str(part.get("text", "")) for part in candidate["content"]["parts"])
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise TypeError("Gemini result is not an object")
                return result
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1200]
                self.last_request_metadata = {
                    "attempts": attempts,
                    "request_bytes": len(encoded),
                    "elapsed_seconds": round(
                        time.monotonic() - request_started, 4,
                    ),
                    "model": self.model,
                    "http_status": exc.code,
                    "timed_out": False,
                }
                if exc.code == 429 and "monthly spending cap" in detail.casefold():
                    raise RuntimeError(
                        f"Gemini visual ingestion HTTP {exc.code}: {detail}"
                    ) from exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise RuntimeError(f"Gemini visual ingestion HTTP {exc.code}: {detail}") from exc
            except (OSError, URLError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                if attempt == 4 or time.monotonic() >= deadline:
                    raise RuntimeError(f"Gemini visual ingestion failed: {exc}") from exc
            pause = min(30, 2 ** attempt * 2)
            if time.monotonic() + pause >= deadline:
                break
            time.sleep(pause)
        self.last_request_metadata = {
            "attempts": attempts,
            "request_bytes": len(encoded),
            "elapsed_seconds": round(
                time.monotonic() - request_started, 4,
            ),
            "model": self.model,
            "timed_out": True,
        }
        raise RuntimeError(
            f"Gemini visual ingestion exceeded {self.timeout}s hard deadline"
        )

    def extract_document(self, bundle: EvidenceBundle, context: dict[str, Any]) -> dict[str, Any]:
        return self._request(EXTRACTION_INSTRUCTION, self._parts(bundle, context), EXTRACTION_SCHEMA)

    def verify_document(self, bundle: EvidenceBundle, extraction: dict[str, Any]) -> dict[str, Any]:
        context = extraction.get("_visual_batch_context", {}) if isinstance(extraction, dict) else {}
        return self._request(VERIFICATION_INSTRUCTION, self._parts(bundle, context, extraction), VERIFICATION_SCHEMA)

    def verify_spreadsheet_units(
        self, packet: dict[str, Any], context: dict[str, Any],
    ) -> dict[str, Any]:
        parts = [{"text": json.dumps({
            "known_context_hints": context,
            "spreadsheet_evidence_packet": packet,
        }, ensure_ascii=False)}]
        return self._request(
            SPREADSHEET_VERIFICATION_INSTRUCTION,
            parts,
            SPREADSHEET_VERIFICATION_SCHEMA,
            max_output_tokens=4096,
        )

    def pre_scan_sources(self, files: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
        parts = [{"text": json.dumps({
            "known_context_hints": context,
            "files": files,
            "instruction": "Classify each file as full_read, context_only, or skip.",
        }, ensure_ascii=False)}]
        return self._request(PRESCAN_INSTRUCTION, parts, PRESCAN_SCHEMA)


def match_verified_extraction(
    extraction: dict[str, Any], verification: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Match separately extracted items only after independent verification.

    Existing v2 artifacts with ``records`` remain readable. New v3 artifacts
    keep comments and responses independent until this explicit stage.
    """
    if isinstance(extraction.get("records"), list):
        return extraction, verification
    comments = [row for row in extraction.get("comments", []) if isinstance(row, dict)]
    responses = [row for row in extraction.get("responses", []) if isinstance(row, dict)]
    response_by_number: dict[str, list[dict[str, Any]]] = {}
    for response in responses:
        response_by_number.setdefault(
            normalized_whitespace(str(response.get("response_number", ""))).casefold(), [],
        ).append(response)
    comment_checks = {
        str(row.get("record_key", "")): row
        for row in verification.get("comments", []) if isinstance(row, dict)
    }
    response_checks = {
        str(row.get("record_key", "")): row
        for row in verification.get("responses", []) if isinstance(row, dict)
    }
    records: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    matched_response_keys: set[str] = set()
    for index, comment in enumerate(comments, 1):
        key = str(comment.get("record_key", "")).strip() or f"comment-{index}"
        number = str(comment.get("comment_number", "")).strip()
        candidates = response_by_number.get(normalized_whitespace(number).casefold(), []) if number else []
        response = candidates[0] if len(candidates) == 1 else None
        response_key = str(response.get("record_key", "")) if response else ""
        if response_key:
            matched_response_keys.add(response_key)
        comment_start = int(comment.get("page_start") or 0)
        comment_end = int(comment.get("page_end") or comment_start)
        response_start = int(response.get("page_start") or 0) if response else 0
        response_end = int(response.get("page_end") or response_start) if response else 0
        comment_check = comment_checks.get(key, {})
        response_check = response_checks.get(response_key, {}) if response else {}
        comment_ok = all(comment_check.get(name) is True for name in (
            "comment_captured", "text_complete_and_verbatim",
            "locations_and_boxes_correct", "verified",
        ))
        response_ok = not response or all(response_check.get(name) is True for name in (
            "response_captured", "text_complete_and_verbatim",
            "locations_and_boxes_correct", "verified",
        ))
        shared_id = bool(response and number and normalized_whitespace(
            str(response.get("response_number", "")),
        ).casefold() == normalized_whitespace(number).casefold())
        uncertainty = " ".join(filter(None, [
            str(comment.get("uncertainty_reason", "")).strip(),
            str(response.get("uncertainty_reason", "")).strip() if response else "",
            str(comment_check.get("uncertainty_reason", "")).strip(),
            str(response_check.get("uncertainty_reason", "")).strip() if response else "",
            "Multiple responses share this printed number." if len(candidates) > 1 else "",
        ]))
        records.append({
            "record_key": key, "comment_id": number, "comment_number": number,
            "page": comment_start, "exact_comment_text": str(comment.get("exact_comment_text", "")),
            "normalized_comment_text": str(comment.get("normalized_comment_text", "")),
            "department": str(comment.get("department", "")),
            "reviewer": str(comment.get("reviewer", "")),
            "exact_response_text": str(response.get("exact_response_text", "")) if response else "",
            "comment_location": {
                "pages": list(range(comment_start, comment_end + 1)) if comment_start else [],
                "description": "complete government comment",
                "bounding_boxes": comment.get("bounding_boxes", []),
            },
            "response_location": {
                "pages": list(range(response_start, response_end + 1)) if response_start else [],
                "description": "complete company response" if response else "",
                "bounding_boxes": response.get("bounding_boxes", []) if response else [],
            },
            "same_visible_row": False, "explicit_shared_comment_id": shared_id,
            "pairing_evidence": (
                f"Post-verification exact printed identifier match {number!r}"
                if shared_id else "No response was matched during verified matching"
            ),
            "confidence": min(
                float(comment.get("confidence") or 0.0),
                float(response.get("confidence") or 1.0) if response else 1.0,
            ),
            "uncertain": (
                comment.get("uncertain") is True
                or (response is not None and response.get("uncertain") is True)
                or len(candidates) > 1
            ),
            "uncertainty_reason": uncertainty,
        })
        checks.append({
            "record_key": key, "comment_captured": comment_ok,
            "response_captured": response_ok,
            "text_complete_and_verbatim": comment_ok and response_ok,
            "pairing_correct": (not response) or shared_id,
            "locations_and_boxes_correct": (
                comment_check.get("locations_and_boxes_correct") is True
                and (not response or response_check.get("locations_and_boxes_correct") is True)
            ),
            "same_visible_row_or_shared_id": (not response) or shared_id,
            "verified": comment_ok and response_ok and ((not response) or shared_id),
            "uncertainty_reason": uncertainty,
        })
    document_checks = all(verification.get(name) is True for name in (
        "number_sequence_correct", "continuations_joined_correctly", "headers_excluded",
        "neighboring_items_separate", "no_response_leakage", "later_markup_check_complete",
    ))
    legacy_extraction = {
        "property": str(extraction.get("property", "")),
        "city": str(extraction.get("city", "")),
        "review_round": str(extraction.get("review_round", "")),
        "document_type": str(extraction.get("document_class", "uncertain")),
        "document_uncertain": (
            extraction.get("comment_section_complete") is not True
            or str(extraction.get("document_class", "")) == "uncertain"
        ),
        "document_uncertainty_reason": (
            str(extraction.get("review_reason", ""))
            if (
                extraction.get("comment_section_complete") is not True
                or str(extraction.get("document_class", "")) == "uncertain"
            )
            else ""
        ),
        "records": records,
        "additional_markups_referenced": extraction.get("additional_markups_referenced") is True,
        "structured_comment_count": len(comments),
        "structured_response_count": len(responses),
        "unmatched_response_keys": sorted(
            str(row.get("record_key", "")) for row in responses
            if str(row.get("record_key", "")) not in matched_response_keys
        ),
    }
    legacy_verification = {
        "document_verified": verification.get("document_verified") is True and document_checks,
        "every_comment_captured": verification.get("every_comment_captured") is True,
        "every_response_captured": verification.get("every_response_captured") is True,
        "verification_summary": str(verification.get("verification_summary", "")),
        "records": checks,
    }
    return legacy_extraction, legacy_verification


def verification_map(verification: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("record_key", "")): row for row in verification.get("records", []) if isinstance(row, dict)
    }


def document_verified(verification: dict[str, Any]) -> bool:
    required_flags = all(verification.get(field) is True for field in (
        "document_verified", "every_comment_captured", "every_response_captured",
    ))
    reported_errors = any(
        verification.get(field)
        for field in (
            "rejected_record_ids", "missing_visible_comments",
            "missing_visible_responses", "incorrect_links",
            "incorrect_page_locations", "duplicate_fragments",
            "continuation_errors",
        )
    )
    return required_flags and not reported_errors


def result_is_verified(result: dict[str, Any], verification: dict[str, Any]) -> tuple[bool, str]:
    check = verification_map(verification).get(str(result.get("record_key", "")), {})
    checks = all(check.get(field) is True for field in (
        "comment_captured", "response_captured", "text_complete_and_verbatim", "pairing_correct",
        "locations_and_boxes_correct", "verified",
    ))
    confidence = float(result.get("confidence") or 0.0)
    response_present = bool(str(result.get("exact_response_text", "")).strip())
    pairing_supported = not response_present or (
        result.get("same_visible_row") is True or result.get("explicit_shared_comment_id") is True
    )
    if response_present:
        checks = checks and check.get("same_visible_row_or_shared_id") is True
    verified = (
        document_verified(verification) and checks and result.get("uncertain") is False
        and confidence >= MIN_VERIFIED_CONFIDENCE and pairing_supported
    )
    reasons = [str(result.get("uncertainty_reason", "")).strip(), str(check.get("uncertainty_reason", "")).strip()]
    if confidence < MIN_VERIFIED_CONFIDENCE:
        reasons.append(f"Extraction confidence {confidence:.3f} is below {MIN_VERIFIED_CONFIDENCE:.2f}")
    if not pairing_supported:
        reasons.append("No same-visible-row or explicit shared-comment-ID evidence")
    if not document_verified(verification):
        reasons.append(str(verification.get("verification_summary", "Document-level verification failed")))
    return verified, " ".join(value for value in reasons if value)


def normalized_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def regression_against_oracle(extraction: dict[str, Any], dataset: dict[str, Any], filename: str) -> dict[str, Any]:
    responses = {row["response_id"]: row for row in dataset.get("responses", [])}
    links = [
        row for row in dataset.get("comment_response_links", [])
        if row.get("provenance") == "document_structure_rematch" and Path(str(row.get("source_pdf", ""))).name == filename
    ]
    if not links:
        return {"applicable": False, "passed": True, "expected": 0, "actual": len(extraction.get("records", [])), "failures": []}
    expected: dict[str, dict[str, str]] = {}
    for link in links:
        expected[str(link.get("city_comment_id", ""))] = {
            "response": str(responses.get(str(link.get("response_id", "")), {}).get("original_text", "")),
        }
    actual: dict[str, list[dict[str, Any]]] = {}
    for row in extraction.get("records", []):
        printed_id = str(row.get("comment_id") or row.get("comment_number", ""))
        actual.setdefault(printed_id, []).append(row)
    failures: list[dict[str, str]] = []
    for number, oracle in expected.items():
        rows = actual.get(number, [])
        if len(rows) != 1:
            failures.append({"comment_number": number, "reason": f"expected one record, found {len(rows)}"})
            continue
        row = rows[0]
        if normalized_whitespace(str(row.get("exact_response_text", ""))) != normalized_whitespace(oracle["response"]):
            failures.append({"comment_number": number, "reason": "response text differs from confirmed reference"})
    unexpected = sorted(set(actual) - set(expected))
    failures.extend({"comment_number": number, "reason": "unexpected record"} for number in unexpected)
    return {
        "applicable": True, "passed": not failures, "expected": len(expected),
        "actual": len(extraction.get("records", [])), "failures": failures,
    }


def location_pages(value: Any) -> list[int]:
    if not isinstance(value, dict) or not isinstance(value.get("pages"), list):
        return []
    return [int(page) for page in value["pages"] if str(page).isdigit() and int(page) > 0]


def location_boxes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("bounding_boxes"), list):
        return []
    return [box for box in value["bounding_boxes"] if isinstance(box, dict)]


def valid_pdf_location(value: Any, page_count: int) -> bool:
    pages = location_pages(value)
    boxes = location_boxes(value)
    if not pages or not boxes or any(page > page_count for page in pages):
        return False
    for box in boxes:
        try:
            page = int(box["page"])
            x_min, y_min = float(box["x_min"]), float(box["y_min"])
            x_max, y_max = float(box["x_max"]), float(box["y_max"])
        except (KeyError, TypeError, ValueError):
            return False
        if page not in pages or not (0 <= x_min < x_max <= 1000 and 0 <= y_min < y_max <= 1000):
            return False
    return True


def spreadsheet_location(value: Any) -> tuple[str, str, int]:
    if not isinstance(value, dict):
        return "", "", 0
    sheet = str(value.get("sheet_name", "")).strip()
    cell_range = str(value.get("cell_range", "")).strip()
    try:
        row_number = int(value.get("row_number") or 0)
    except (TypeError, ValueError):
        row_number = 0
    return sheet, cell_range, row_number


def valid_spreadsheet_location(value: Any) -> bool:
    sheet, cell_range, row_number = spreadsheet_location(value)
    return bool(
        sheet
        and cell_range
        and row_number > 0
        and re.fullmatch(r"[A-Z]{1,4}\d+(?::[A-Z]{1,4}\d+)?", cell_range.upper())
    )


def location_text(value: Any, fallback: str) -> str:
    sheet, cell_range, _row_number = spreadsheet_location(value)
    if sheet and cell_range:
        description = str(value.get("description", "")).strip()
        label = f"sheet {sheet} · cell {cell_range}"
        return f"{label} · {description}" if description else label
    pages = location_pages(value)
    description = str(value.get("description", "")).strip() if isinstance(value, dict) else ""
    page_label = "pages " + ", ".join(map(str, pages)) if len(pages) > 1 else (f"page {pages[0]}" if pages else "unknown page")
    if not pages:
        return description or fallback
    return f"{page_label} · {description}" if description else page_label


def results_to_dataset_rows(
    bundle: EvidenceBundle, extraction: dict[str, Any], verification: dict[str, Any],
    source_relative: str, regression: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    comments: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    force_review = extraction.get("document_uncertain") is True
    document_extraction_method = str(
        extraction.get("extraction_method")
        or "gemini_visual_two_pass"
    )
    regression_failures = {
        str(row.get("comment_number", ""))
        for row in (regression or {}).get("failures", [])
        if str(row.get("comment_number", "")).strip()
    }
    document_page_count = bundle.document_page_count or len(bundle.pages)
    number_counts: dict[str, int] = {}
    key_counts: dict[str, int] = {}
    for row in extraction.get("records", []):
        number = str(row.get("comment_id") or row.get("comment_number", "")).strip()
        number_counts[number] = number_counts.get(number, 0) + 1
        key = str(row.get("record_key", "")).strip()
        key_counts[key] = key_counts.get(key, 0) + 1
    for index, result in enumerate(extraction.get("records", []), 1):
        record_key = str(result.get("record_key", "")).strip() or f"record-{index}"
        number = str(result.get("comment_id") or result.get("comment_number", "")).strip()
        verified, uncertainty = result_is_verified(result, verification)
        comment_pages = location_pages(result.get("comment_location"))
        response_pages = location_pages(result.get("response_location"))
        is_structured_spreadsheet = (
            bundle.original_type in {"xls", "xlsx", "csv"}
            and valid_spreadsheet_location(result.get("comment_location"))
        )
        if is_structured_spreadsheet:
            locations_valid = True
            if str(result.get("exact_response_text", "")):
                locations_valid = valid_spreadsheet_location(
                    result.get("response_location")
                )
        else:
            locations_valid = bool(comment_pages) and all(
                page <= document_page_count for page in comment_pages
            )
            if str(result.get("exact_response_text", "")):
                locations_valid = (
                    locations_valid
                    and bool(response_pages)
                    and all(page <= document_page_count for page in response_pages)
                )
        if bundle.original_type == "pdf":
            locations_valid = locations_valid and valid_pdf_location(result.get("comment_location"), document_page_count)
            if str(result.get("exact_response_text", "")):
                locations_valid = locations_valid and valid_pdf_location(result.get("response_location"), document_page_count)
        row_regression_failed = number in regression_failures
        duplicate_number_is_error = (
            number_counts[number] > 1
            and extraction.get("comment_number_scope") != "sheet_row"
        )
        if force_review or row_regression_failed or duplicate_number_is_error or key_counts[record_key] > 1 or not number or not str(result.get("exact_comment_text", "")) or not locations_valid:
            verified = False
        if force_review or row_regression_failed:
            prefix = str(extraction.get("document_uncertainty_reason", "")).strip()
            if row_regression_failed:
                prefix = "Confirmed-reference regression failed. " + prefix
            uncertainty = (prefix + " " + uncertainty).strip()
        if duplicate_number_is_error:
            uncertainty = f"Duplicate comment number {number!r}. " + uncertainty
        if key_counts[record_key] > 1:
            uncertainty = f"Duplicate Gemini record_key {record_key!r}. " + uncertainty
        if not locations_valid:
            uncertainty = "One or more required source pages are missing or outside the rendered document. " + uncertainty
        status = "confirmed" if verified else "needs_review"
        record_identity = record_key if key_counts[record_key] == 1 else f"{record_key}:duplicate:{index}"
        comment_id = base.stable_id("C", bundle.source_sha256, record_identity, "visual")
        response_text = str(result.get("exact_response_text", ""))
        response_id = base.stable_id("R", bundle.source_sha256, record_identity, "visual") if response_text else ""
        row_round = str(
            result.get("review_round")
            or extraction.get("review_round", "")
        )
        reviewed_plan_round = str(
            result.get("reviewed_plan_round")
            or extraction.get("reviewed_plan_round", row_round)
        )
        response_letter_round = str(
            result.get("response_letter_round")
            or extraction.get("response_letter_round", "")
        )
        comment_sheet, comment_cell, comment_row = spreadsheet_location(
            result.get("comment_location")
        )
        response_sheet, response_cell, response_row = spreadsheet_location(
            result.get("response_location")
        )
        extraction_method = str(
            result.get("extraction_method")
            or extraction.get("extraction_method")
            or "gemini_visual_two_pass"
        )
        structured_method = extraction_method == "local_structured_spreadsheet"
        matching_method = (
            "same_visible_row_structured"
            if structured_method else "gemini_visual_verified"
        )
        provenance = (
            "local_structured_gemini_verified"
            if structured_method else "gemini_visual_two_pass"
        )
        audit_payload = {
            "artifact_id": bundle.artifact_id, "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
            "verification_prompt_version": VERIFICATION_PROMPT_VERSION,
            "ingestion_pipeline_version": PIPELINE_VERSION,
            "gemini_record_key": record_key, "printed_comment_id": str(result.get("comment_id", "")),
            "pairing_evidence": str(result.get("pairing_evidence", "")),
            "same_visible_row": result.get("same_visible_row") is True,
            "explicit_shared_comment_id": result.get("explicit_shared_comment_id") is True,
            "gemini_confidence": float(result.get("confidence") or 0.0),
            "comment_unit_ids": list(result.get("comment_unit_ids", []) or []),
            "response_unit_ids": list(result.get("response_unit_ids", []) or []),
            "spreadsheet_verification_prompt_version": (
                SPREADSHEET_VERIFICATION_PROMPT_VERSION
                if structured_method else ""
            ),
            "uncertainty_reason": uncertainty,
        }
        issue_anchor = normalized_whitespace(str(
            result.get("normalized_comment_text")
            or result.get("exact_comment_text", "")
        )).casefold()
        issue_thread_id = base.stable_id(
            "T",
            str(extraction.get("city", "")).casefold(),
            str(extraction.get("property", "")).casefold(),
            str(result.get("department", "")).casefold(),
            issue_anchor,
        )
        discussion_events: list[dict[str, Any]] = []
        for event_index, event in enumerate(
            result.get("discussion_events", []) or [], 1,
        ):
            if not isinstance(event, dict):
                continue
            event_location = (
                event.get("source_location")
                if isinstance(event.get("source_location"), dict)
                else result.get("discussion_location", {})
            )
            discussion_events.append({
                **event,
                "event_id": base.stable_id(
                    "E", issue_thread_id, "discussion", str(event_index),
                ),
                "issue_thread_id": issue_thread_id,
                "source_document": source_relative,
                "source_locator_json": event_location,
            })
        comment = {
            "comment_id": comment_id, "city": str(extraction.get("city", "")),
            "property_project": str(extraction.get("property", "")), "review_round": row_round,
            "reviewed_plan_round": reviewed_plan_round,
            "response_letter_round": response_letter_round,
            "discipline": str(result.get("department", "")) or "unknown",
            "reviewer": str(result.get("reviewer", "")), "reviewer_context": "",
            "comment_number": number,
            "original_text": str(result.get("exact_comment_text", "")), "source_document": source_relative,
            "normalized_comment_text": str(result.get("normalized_comment_text", "")),
            "verified_text": str(result.get("exact_comment_text", "")) if verified else "",
            "source_sha256": bundle.source_sha256, "source_sheet": comment_sheet,
            "source_row": comment_row or "",
            "source_cell_range": comment_cell,
            "source_page": comment_pages[0] if comment_pages else "", "source_page_end": comment_pages[-1] if comment_pages else "",
            "source_location": location_text(result.get("comment_location"), "unknown"),
            "source_locator_json": result.get("comment_location", {}), "extraction_method": extraction_method,
            "extraction_confidence": 1.0 if verified else 0.0, "source_cycle": row_round,
            "source_status": status, "response_id": response_id,
            "match_status": "matched" if response_id else "unmatched", "human_review_status": status,
            "verification_status": status,
            "text_trust_status": "verified" if verified else "quarantined",
            "search_eligible": verified,
            "ingestion_pipeline_version": PIPELINE_VERSION,
            "ingestion_audit": audit_payload,
            "issue_thread_id": issue_thread_id,
            "issue_grouping_status": (
                "explicit" if discussion_events else "deterministic_exact"
            ),
            "issue_grouping_method": (
                "same_spreadsheet_row_with_history"
                if discussion_events
                else "exact_site_discipline_comment"
            ),
            "issue_status": str(
                (result.get("source_metadata") or {}).get("status", "")
            ),
            "discussion_raw_text": str(
                result.get("exact_discussion_text", "")
            ),
            "discussion_source_locator_json": (
                result.get("discussion_location", {})
            ),
            "issue_thread_events": discussion_events,
        }
        comments.append(comment)
        if response_id:
            responses.append({
                "response_id": response_id, "comment_id": comment_id, "original_text": response_text,
                "verified_text": response_text if verified else "",
                "source_document": source_relative, "source_sha256": bundle.source_sha256,
                "source_sheet": response_sheet, "source_row": response_row or "",
                "source_cell_range": response_cell,
                "source_page": response_pages[0] if response_pages else "",
                "source_page_end": response_pages[-1] if response_pages else "",
                "source_location": location_text(result.get("response_location"), "unknown"),
                "source_locator_json": result.get("response_location", {}),
                "extraction_method": extraction_method, "extraction_confidence": 1.0 if verified else 0.0,
                "reviewed_plan_round": reviewed_plan_round,
                "response_letter_round": response_letter_round,
                "human_review_status": status, "ingestion_audit": audit_payload,
                "verification_status": status,
                "text_trust_status": "verified" if verified else "quarantined",
                "search_eligible": verified,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
        link_id = base.stable_id("L", comment_id, response_id or "NONE", provenance)
        link_status = status if response_id else ("not_required" if verified else "needs_review")
        links.append({
            "link_id": link_id, "comment_id": comment_id, "response_id": response_id,
            "match_status": "matched" if response_id else "unmatched", "matching_method": matching_method,
            "match_confidence": 1.0 if verified and response_id else 0.0, "review_status": link_status,
            "verification_status": status, "pairing_evidence": str(result.get("pairing_evidence", "")),
            "provenance": provenance, "source_document": source_relative,
            "source_location": location_text(result.get("comment_location"), "unknown"),
            "comment_locator_json": result.get("comment_location", {}),
            "response_locator_json": result.get("response_location", {}), "ingestion_audit": audit_payload,
        })
        if not verified:
            review.append({
                "item_type": (
                    "spreadsheet_record"
                    if structured_method else "gemini_visual_record"
                ),
                "item_id": link_id,
                "reason": uncertainty or "Visual extraction or matching did not pass independent verification",
                "source_document": source_relative, "source_location": comment["source_location"],
                "suggested_action": "Compare the verbatim extraction and pairing against every cited page image",
                "decision": "", "decision_note": "",
            })
    if regression and regression.get("applicable") and not regression.get("passed"):
        review.append({
            "item_type": "confirmed_reference_regression",
            "item_id": bundle.artifact_id,
            "reason": f"{len(regression.get('failures', []))} extracted row(s) differ from the confirmed reference; affected rows remain needs_review",
            "source_document": source_relative,
            "source_location": "complete document",
            "suggested_action": "Compare each failed row against the saved confirmed reference and source page image",
            "decision": "", "decision_note": "",
        })
    if not extraction.get("records") or not document_verified(verification):
        review.append({
            "item_type": (
                "spreadsheet_document"
                if document_extraction_method
                == "local_structured_spreadsheet"
                else "gemini_visual_document"
            ),
            "item_id": bundle.artifact_id,
            "reason": str(verification.get("verification_summary", "No records were extracted or document verification failed")),
            "source_document": source_relative, "source_location": "complete document",
            "suggested_action": "Review every rendered page and compare it with the saved extraction and verification artifacts",
            "decision": "", "decision_note": "",
        })
    summary = {
        "city": str(extraction.get("city", "")), "property_project": str(extraction.get("property", "")),
        "review_round": str(extraction.get("review_round", "")), "source_document": source_relative,
        "reviewed_plan_round": str(extraction.get("reviewed_plan_round", extraction.get("review_round", ""))),
        "response_letter_round": str(extraction.get("response_letter_round", "")),
        "source_type": str(extraction.get("document_type", "")) or "gemini_visual_document",
        "comment_count": len(comments),
        "response_count": int(extraction.get("structured_response_count", len(responses))),
        "matched_count": sum(row["response_id"] != "" for row in links),
        "unmatched_count": sum(row["response_id"] == "" for row in links),
        "extraction_method": document_extraction_method,
        "processing_error": "" if all(row["review_status"] in {"confirmed", "not_required"} for row in links) else "One or more records require review",
        "ingestion_artifact_id": bundle.artifact_id,
        "processing_status": (
            "comments_and_responses_found" if comments and extraction.get("structured_response_count", len(responses))
            else "comments_found" if comments
            else "responses_found" if extraction.get("structured_response_count", len(responses))
            else "no_relevant_content"
        ),
        "opened": True,
        "pages_screened": bundle.screening.get("pages_screened", []),
        "pages_fully_analyzed": [page.page_number for page in bundle.pages],
        "additional_markup_detected": bool(
            bundle.screening.get("additional_markup_detected")
            or extraction.get("additional_markups_referenced")
        ),
        "verification_result": "verified" if document_verified(verification) else "needs_review",
        "ingestion_pipeline_version": PIPELINE_VERSION,
        "source_sha256": bundle.source_sha256,
    }
    return comments, responses, links, summary, review


def page_batches(pages: list[PageImage], batch_size: int, overlap: int = 1) -> list[list[PageImage]]:
    if batch_size <= 0 or len(pages) <= batch_size:
        return [pages]
    overlap = max(0, min(overlap, batch_size - 1))
    step = batch_size - overlap
    result: list[list[PageImage]] = []
    start = 0
    while start < len(pages):
        batch = pages[start:start + batch_size]
        if not batch:
            break
        result.append(batch)
        if start + batch_size >= len(pages):
            break
        start += step
    return result


def raw_text_for_page_batch(raw_text: Any, pages: list[PageImage]) -> Any:
    """Keep complete native text for the rendered pages in this visual batch."""
    if not isinstance(raw_text, dict):
        return raw_text
    if raw_text.get("kind") == "xlsx_cells":
        # Known tabular workbooks are handled by the deterministic structured
        # route before visual batching. If an anomalous workbook falls back to
        # images, send only routing metadata here; repeating every cell in every
        # preview-page batch was the primary source of token amplification.
        return {
            "kind": "xlsx_visual_fallback_metadata",
            "sheets": [{
                "name": str(sheet.get("name", "")),
                "likely_comment_columns": list(
                    sheet.get("likely_comment_columns", []) or []
                ),
                "likely_response_columns": list(
                    sheet.get("likely_response_columns", []) or []
                ),
                "merged_ranges": list(sheet.get("merged_ranges", []) or []),
                "has_drawing_objects": bool(
                    sheet.get("has_drawing_objects")
                ),
            } for sheet in raw_text.get("sheets", [])
                if isinstance(sheet, dict)],
            "batch_text_note": (
                "Visual fallback only. Full workbook cells are deliberately "
                "not repeated across preview-page batches."
            ),
        }
    if raw_text.get("kind") != "pdf_text_pages":
        return raw_text
    selected = {page.page_number for page in pages}
    result = copy.deepcopy(raw_text)
    result["pages"] = [
        row for row in raw_text.get("pages", [])
        if isinstance(row, dict) and int(row.get("page") or 0) in selected
    ]
    result["batch_text_note"] = (
        "Complete native text for every rendered page in this visual batch; "
        "all selected pages are covered across overlapping batches."
    )
    return result


def merge_visual_batches(
    extractions: list[dict[str, Any]], verifications: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge overlapping page batches while retaining conflicts for review."""
    if not extractions or len(extractions) != len(verifications):
        raise ValueError("Visual batch extraction and verification counts do not match")
    combined_records: list[dict[str, Any]] = []
    combined_checks: list[dict[str, Any]] = []
    seen_signatures: set[tuple[str, str, str]] = set()
    uncertainty_reasons: list[str] = []
    for batch_index, (extraction, verification) in enumerate(zip(extractions, verifications), 1):
        checks = verification_map(verification)
        if extraction.get("document_uncertain"):
            reason = str(extraction.get("document_uncertainty_reason", "")).strip()
            if reason:
                uncertainty_reasons.append(f"batch {batch_index}: {reason}")
        for record in extraction.get("records", []):
            if not isinstance(record, dict):
                continue
            number = str(record.get("comment_id") or record.get("comment_number", "")).strip()
            signature = (
                number,
                normalized_whitespace(str(record.get("exact_comment_text", ""))),
                normalized_whitespace(str(record.get("exact_response_text", ""))),
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            original_key = str(record.get("record_key", "")).strip() or f"record-{len(combined_records) + 1}"
            merged_key = f"batch-{batch_index}:{original_key}"
            merged_record = copy.deepcopy(record)
            merged_record["record_key"] = merged_key
            combined_records.append(merged_record)
            check = copy.deepcopy(checks.get(original_key, {}))
            if not check:
                check = {
                    "comment_captured": False, "response_captured": False,
                    "text_complete_and_verbatim": False, "pairing_correct": False,
                    "locations_and_boxes_correct": False, "same_visible_row_or_shared_id": False,
                    "verified": False, "uncertainty_reason": "Batch verifier omitted this record",
                }
            check["record_key"] = merged_key
            combined_checks.append(check)
    first = extractions[0]
    all_verified = all(document_verified(value) for value in verifications)
    metadata_conflicts = []
    # Property labels and document-role wording legitimately vary by page
    # (address-only vs full address; applicant_response vs company_response).
    # Only city and review-round disagreement invalidate the whole merge.
    for field in ("city", "review_round"):
        values = {normalized_whitespace(str(value.get(field, ""))).casefold() for value in extractions if str(value.get(field, "")).strip()}
        if len(values) > 1:
            metadata_conflicts.append(f"visual batches disagree on {field}")
    uncertainty_reasons.extend(metadata_conflicts)
    # A page batch can truthfully report that it does not contain the entire
    # document-level comment section simply because more pages exist outside
    # that batch.  Do not quarantine every record for that expected boundary
    # condition when the independent verifier confirms the batch is complete
    # and accurate.  Genuine verifier failures and cross-batch metadata
    # conflicts still force review.
    unresolved_batch_uncertainty = any(
        extraction.get("document_uncertain") is True and not document_verified(verification)
        for extraction, verification in zip(extractions, verifications)
    )
    combined_extraction = {
        "property": str(first.get("property", "")),
        "city": str(first.get("city", "")),
        "review_round": str(first.get("review_round", "")),
        "document_type": str(first.get("document_type", "")),
        "document_uncertain": bool(metadata_conflicts) or unresolved_batch_uncertainty,
        "document_uncertainty_reason": " ".join(uncertainty_reasons),
        "records": combined_records,
        "visual_batch_count": len(extractions),
    }
    combined_verification = {
        "document_verified": all_verified,
        "every_comment_captured": all(value.get("every_comment_captured") is True for value in verifications),
        "every_response_captured": all(value.get("every_response_captured") is True for value in verifications),
        "verification_summary": "All visual page batches verified" if all_verified else "One or more visual page batches require review",
        "records": combined_checks,
        "visual_batch_count": len(verifications),
    }
    for field in (
        "verified_record_ids", "rejected_record_ids",
        "missing_visible_comments", "missing_visible_responses",
        "incorrect_links", "incorrect_page_locations",
        "duplicate_fragments", "continuation_errors",
    ):
        combined_verification[field] = [
            str(item)
            for value in verifications
            for item in value.get(field, [])
            if str(item).strip()
        ]
    return combined_extraction, combined_verification


def reconcile_document_completeness(
    bundle: EvidenceBundle,
    extraction: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    """Deterministically compare routed page signals with verified records."""
    records = [
        row for row in extraction.get("records", []) if isinstance(row, dict)
    ]
    extracted_numbers = {
        str(row.get("comment_id") or row.get("comment_number") or "").strip()
        for row in records
        if str(row.get("comment_id") or row.get("comment_number") or "").strip()
    }
    manifest = [
        row for row in (
            bundle.screening.get("page_manifest")
            or bundle.screening.get("page_classifications")
            or []
        )
        if isinstance(row, dict)
    ]
    expected_numbers = {
        str(number).strip()
        for row in manifest
        if row.get("page_class") in {
            "comment_list", "comment_response_table", "response_list",
        }
        for number in row.get("detected_comment_numbers", [])
        if str(number).strip()
    }
    expected_response_numbers = {
        str(number).strip()
        for row in manifest
        for number in row.get("detected_response_numbers", [])
        if str(number).strip()
    }
    extracted_response_numbers = {
        str(row.get("comment_id") or row.get("comment_number") or "").strip()
        for row in records
        if str(row.get("exact_response_text", "")).strip()
    }
    missing_comment_numbers = sorted(
        expected_numbers - extracted_numbers, key=base.natural_number,
    )
    missing_response_numbers = sorted(
        expected_response_numbers - extracted_response_numbers,
        key=base.natural_number,
    )
    processed_pages = {page.page_number for page in bundle.pages}
    signal_pages = {
        int(row.get("page") or 0)
        for row in manifest
        if row.get("page_class") in {
            "comment_list", "response_list", "comment_response_table",
            "drawing_markup",
        }
        or int(row.get("annotation_count") or 0) > 0
        or int(row.get("form_field_count") or 0) > 0
    }
    unprocessed_signal_pages = sorted(
        page for page in signal_pages if page > 0 and page not in processed_pages
    )
    verified_count = sum(
        result_is_verified(row, verification)[0] for row in records
    )
    unresolved = (
        len(missing_comment_numbers)
        + len(missing_response_numbers)
        + len(unprocessed_signal_pages)
        + (0 if document_verified(verification) else 1)
    )
    # A signal page can be deliberately left at lightweight coverage only when
    # routing gave it an explicit stored reason. It remains visible here but is
    # not unresolved unless its extracted numbering/reconciliation fails.
    completion_status = "complete" if unresolved == 0 else "needs_review"
    return {
        "expected_comment_count": (
            len(expected_numbers) if expected_numbers else len(records)
        ),
        "extracted_comment_count": len(records),
        "verified_comment_count": verified_count,
        "expected_comment_numbers": sorted(
            expected_numbers, key=base.natural_number,
        ),
        "missing_comment_numbers": missing_comment_numbers,
        "missing_response_numbers": missing_response_numbers,
        "unprocessed_signal_pages": unprocessed_signal_pages,
        "unresolved_signal_count": unresolved,
        "pages_screened": len(bundle.screening.get("pages_screened", [])),
        "pages_fully_processed": len(processed_pages),
        "pages_escalated": bundle.screening.get("pages_escalated", []),
        "completion_status": completion_status,
    }


class VisualIngestionPipeline:
    def __init__(
        self, client: VisualClient, artifact_root: Path, oracle_dataset: Path | None = None,
        dpi: int = 220, batch_pages: int = 0, batch_overlap: int = 1,
        prescan_client: VisualClient | None = None,
    ):
        self.client = client
        self.prescan_client = prescan_client or client
        self.builder = DocumentEvidenceBuilder(artifact_root, dpi)
        self.oracle_dataset = oracle_dataset
        self.batch_pages = max(0, int(batch_pages))
        self.batch_overlap = max(0, int(batch_overlap))
        self.batch_text_character_limit = max(
            0, int(os.environ.get("VISUAL_BATCH_TEXT_CHARACTER_LIMIT", "21500")),
        )
        self._metrics: dict[str, Any] = {}

    @staticmethod
    def _json_digest(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _extraction_cache_identity(self, bundle: EvidenceBundle) -> dict[str, Any]:
        return {
            "stage": "gemini_extraction",
            "source_sha256": bundle.source_sha256,
            "model": str(getattr(self.client, "model", "test-client")),
            "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
            "visual_batch_pages": str(self.batch_pages),
            "visual_batch_overlap": str(self.batch_overlap),
            "batch_text_scope": "rendered_visual_pages",
            "raw_text_fingerprint": self._json_digest(bundle.raw_text),
            "selected_page_fingerprints": [
                {
                    "page": page.page_number,
                    "sha256": sha256_file(page.path),
                }
                for page in bundle.pages
            ],
        }

    def _verification_cache_identity(
        self, bundle: EvidenceBundle, extraction: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "stage": "gemini_verification",
            "source_sha256": bundle.source_sha256,
            "model": str(getattr(self.client, "model", "test-client")),
            "verification_prompt_version": VERIFICATION_PROMPT_VERSION,
            "extraction_fingerprint": self._json_digest(extraction),
            "verification_text_fingerprint": self._json_digest(bundle.raw_text),
            "verification_page_fingerprints": [
                {"page": page.page_number, "sha256": sha256_file(page.path)}
                for page in bundle.pages
            ],
        }

    @staticmethod
    def _read_cache_metadata(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _stage_cache_is_compatible(
        self,
        bundle: EvidenceBundle,
        stage: str,
        expected: dict[str, Any],
        metadata_path: Path | None = None,
    ) -> bool:
        path = metadata_path or bundle.artifact_dir / "gemini_cache_metadata.json"
        return self._read_cache_metadata(path).get(stage) == expected

    def _write_stage_cache_identity(
        self,
        bundle: EvidenceBundle,
        stage: str,
        identity: dict[str, Any],
        metadata_path: Path | None = None,
    ) -> None:
        path = metadata_path or bundle.artifact_dir / "gemini_cache_metadata.json"
        metadata = self._read_cache_metadata(path)
        metadata[stage] = identity
        atomic_json(path, metadata)

    def _record_gemini_metrics(
        self,
        stage: str,
        elapsed: float,
        packet_id: str = "",
    ) -> None:
        usage = getattr(self.client, "last_usage_metadata", {})
        request = getattr(self.client, "last_request_metadata", {})
        usage = usage if isinstance(usage, dict) else {}
        request = request if isinstance(request, dict) else {}
        input_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0)
        cached_tokens = int(usage.get("cachedContentTokenCount") or 0)
        thought_tokens = int(usage.get("thoughtsTokenCount") or 0)
        self._metrics[f"{stage}_input_tokens"] += input_tokens
        self._metrics[f"{stage}_output_tokens"] += output_tokens
        self._metrics["gemini_cached_input_tokens"] += cached_tokens
        self._metrics["gemini_thought_tokens"] += thought_tokens
        self._metrics["request_metrics"].append({
            "stage": stage,
            "packet_id": packet_id,
            "elapsed_seconds": round(elapsed, 4),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_tokens,
            "thought_tokens": thought_tokens,
            "request_bytes": int(request.get("request_bytes") or 0),
            "attempts": int(request.get("attempts") or 1),
            "model": str(
                request.get("model")
                or getattr(self.client, "model", "test-client")
            ),
            "timed_out": bool(request.get("timed_out")),
        })

    def _extract(self, bundle: EvidenceBundle, context: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        self._metrics["gemini_extraction_calls"] += 1
        if hasattr(self.client, "last_usage_metadata"):
            self.client.last_usage_metadata = {}
        if hasattr(self.client, "last_request_metadata"):
            self.client.last_request_metadata = {}
        try:
            return self.client.extract_document(bundle, context)
        finally:
            elapsed = time.perf_counter() - started
            self._metrics["gemini_extraction_seconds"] += elapsed
            self._record_gemini_metrics(
                "gemini_extraction", elapsed, bundle.artifact_id,
            )

    def _verify(
        self, bundle: EvidenceBundle, extraction: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self._metrics["gemini_verification_calls"] += 1
        if hasattr(self.client, "last_usage_metadata"):
            self.client.last_usage_metadata = {}
        if hasattr(self.client, "last_request_metadata"):
            self.client.last_request_metadata = {}
        try:
            return self.client.verify_document(bundle, extraction)
        finally:
            elapsed = time.perf_counter() - started
            self._metrics["gemini_verification_seconds"] += elapsed
            self._record_gemini_metrics(
                "gemini_verification", elapsed, bundle.artifact_id,
            )

    def _verify_spreadsheet_units(
        self,
        packet: dict[str, Any],
        context: dict[str, Any],
        artifact_id: str,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self._metrics["gemini_spreadsheet_verification_calls"] += 1
        if hasattr(self.client, "last_usage_metadata"):
            self.client.last_usage_metadata = {}
        if hasattr(self.client, "last_request_metadata"):
            self.client.last_request_metadata = {}
        try:
            return self.client.verify_spreadsheet_units(packet, context)
        finally:
            elapsed = time.perf_counter() - started
            self._metrics["gemini_spreadsheet_verification_seconds"] += elapsed
            self._record_gemini_metrics(
                "gemini_spreadsheet_verification", elapsed, artifact_id,
            )

    def _verification_bundle_for_confidence(
        self,
        bundle: EvidenceBundle,
        extraction: dict[str, Any],
    ) -> EvidenceBundle:
        candidate_rows = [
            row
            for key in ("records", "comments", "responses")
            for row in extraction.get(key, [])
            if isinstance(row, dict)
        ]
        confidences = [
            float(row.get("confidence") or 0.0) for row in candidate_rows
        ]
        minimum = min(confidences, default=1.0)
        if minimum >= 0.97 or bundle.original_type != "pdf":
            return bundle
        escalation_dpi = 300 if minimum >= 0.80 else 360
        escalation_dir = (
            bundle.artifact_dir / f"confidence-pages-{escalation_dpi}dpi"
        )
        pages = render_pdf_page_selection(
            bundle.source_path,
            escalation_dir,
            [page.page_number for page in bundle.pages],
            escalation_dpi,
        )
        raw_text = copy.deepcopy(bundle.raw_text)
        escalation: dict[str, Any] = {
            "minimum_extraction_confidence": minimum,
            "rerender_dpi": escalation_dpi,
            "reason": (
                "medium_confidence_high_resolution_verification"
                if minimum >= 0.80
                else "low_confidence_ocr_native_comparison"
            ),
        }
        if minimum < 0.80:
            escalation["ocr_text_by_page"] = {
                str(page.page_number): ocr_page(page.path) for page in pages
            }
        raw_text["confidence_escalation"] = escalation
        atomic_json(
            bundle.artifact_dir / "confidence_escalation.json", escalation,
        )
        self._metrics["confidence_escalated_pages"] = (
            self._metrics.get("confidence_escalated_pages", 0) + len(pages)
        )
        return EvidenceBundle(
            bundle.artifact_id,
            bundle.source_path,
            bundle.source_sha256,
            bundle.original_type,
            raw_text,
            pages,
            bundle.artifact_dir,
            document_page_count=bundle.document_page_count,
            screening=bundle.screening,
        )

    @staticmethod
    def _write_job_progress(
        bundle: EvidenceBundle,
        stage: str,
        status: str = "complete",
        **details: Any,
    ) -> None:
        path = bundle.artifact_dir / "job_progress.json"
        progress = VisualIngestionPipeline._read_cache_metadata(path)
        progress.setdefault("artifact_id", bundle.artifact_id)
        progress.setdefault("source_sha256", bundle.source_sha256)
        stages = progress.setdefault("stages", {})
        stages[stage] = {
            "status": status,
            "updated_at_epoch": round(time.time(), 3),
            **details,
        }
        progress["last_completed_stage"] = stage if status == "complete" else ""
        atomic_json(path, progress)

    @staticmethod
    def _batch_text_characters(
        bundle: EvidenceBundle, pages: list[PageImage],
    ) -> int:
        scoped = raw_text_for_page_batch(bundle.raw_text, pages)
        if not isinstance(scoped, dict) or scoped.get("kind") != "pdf_text_pages":
            return 0
        return sum(
            len(str(row.get("text", "")))
            for row in scoped.get("pages", [])
            if isinstance(row, dict)
        )

    def _split_batch_pages(
        self, pages: list[PageImage],
    ) -> list[list[PageImage]]:
        if len(pages) <= 1:
            return []
        midpoint = len(pages) // 2
        overlap = min(self.batch_overlap, 1) if len(pages) > 2 else 0
        left = pages[:midpoint + overlap]
        right = pages[midpoint:]
        if not left or not right or left == pages or right == pages:
            return [[page] for page in pages]
        return [left, right]

    @staticmethod
    def _retryable_batch_failure(exc: BaseException) -> bool:
        message = str(exc).casefold()
        return any(signal in message for signal in (
            "timed out",
            "timeout",
            "deadline exceeded",
            "request entity too large",
            "payload too large",
        ))

    def _extract_and_verify_page_group(
        self,
        bundle: EvidenceBundle,
        context: dict[str, Any],
        pages: list[PageImage],
        root_index: int,
        root_count: int,
        force: bool,
        label: str,
        split_depth: int = 0,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        extraction_path = (
            bundle.artifact_dir / f"gemini_extraction.{label}.json"
        )
        verification_path = (
            bundle.artifact_dir / f"gemini_verification.{label}.json"
        )
        metadata_path = (
            bundle.artifact_dir / f"gemini_cache_metadata.{label}.json"
        )
        split_marker_path = (
            bundle.artifact_dir / f"adaptive_split.{label}.json"
        )
        page_numbers = [page.page_number for page in pages]
        batch_context = {
            **context,
            "document_page_count": (
                bundle.document_page_count or len(bundle.pages)
            ),
            "visual_batch": {
                "index": root_index,
                "count": root_count,
                "pages": page_numbers,
                "overlap_pages": self.batch_overlap,
                "split_depth": split_depth,
            },
        }
        batch_bundle = EvidenceBundle(
            bundle.artifact_id,
            bundle.source_path,
            bundle.source_sha256,
            bundle.original_type,
            raw_text_for_page_batch(bundle.raw_text, pages),
            pages,
            bundle.artifact_dir,
            document_page_count=bundle.document_page_count,
            screening=bundle.screening,
        )
        extraction_identity = self._extraction_cache_identity(batch_bundle)
        split_marker = self._read_cache_metadata(split_marker_path)
        text_characters = self._batch_text_characters(bundle, pages)
        split_reason = ""
        if (
            len(pages) > 1
            and split_marker.get("parent_extraction_identity")
            == extraction_identity
        ):
            split_reason = str(
                split_marker.get("reason", "cached adaptive split"),
            )
        elif (
            len(pages) > 1
            and self.batch_text_character_limit
            and text_characters > self.batch_text_character_limit
        ):
            split_reason = (
                f"native text payload {text_characters} exceeds "
                f"{self.batch_text_character_limit}"
            )
        if split_reason:
            child_batches = self._split_batch_pages(pages)
            atomic_json(split_marker_path, {
                "parent_extraction_identity": extraction_identity,
                "reason": split_reason,
                "children": [
                    [page.page_number for page in child]
                    for child in child_batches
                ],
            })
            print(
                f"Adaptive split {label} pages {page_numbers[0]}-"
                f"{page_numbers[-1]}: {split_reason}",
                file=sys.stderr,
                flush=True,
            )
            results: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for child in child_batches:
                child_label = (
                    f"{label}.split-p{child[0].page_number:04d}-"
                    f"{child[-1].page_number:04d}"
                )
                results.extend(self._extract_and_verify_page_group(
                    bundle,
                    context,
                    child,
                    root_index,
                    root_count,
                    force,
                    child_label,
                    split_depth + 1,
                ))
            return results

        print(
            f"Gemini visual batch {root_index}/{root_count} pages "
            f"{page_numbers[0]}-{page_numbers[-1]}",
            file=sys.stderr,
            flush=True,
        )
        try:
            extraction_cached = (
                not force
                and extraction_path.is_file()
                and self._stage_cache_is_compatible(
                    batch_bundle,
                    "extraction",
                    extraction_identity,
                    metadata_path,
                )
            )
            if extraction_cached:
                extraction = json.loads(
                    extraction_path.read_text(encoding="utf-8"),
                )
                self._metrics["extraction_cache_hits"] += 1
            else:
                extraction = self._extract(batch_bundle, batch_context)
                extraction["_visual_batch_context"] = batch_context
                atomic_json(extraction_path, extraction)
                self._write_stage_cache_identity(
                    batch_bundle,
                    "extraction",
                    extraction_identity,
                    metadata_path,
                )
            verification_bundle = self._verification_bundle_for_confidence(
                batch_bundle,
                extraction,
            )
            verification_identity = self._verification_cache_identity(
                verification_bundle,
                extraction,
            )
            verification_cached = (
                not force
                and verification_path.is_file()
                and self._stage_cache_is_compatible(
                    batch_bundle,
                    "verification",
                    verification_identity,
                    metadata_path,
                )
            )
            if verification_cached:
                verification = json.loads(
                    verification_path.read_text(encoding="utf-8"),
                )
                self._metrics["verification_cache_hits"] += 1
            else:
                verification = self._verify(
                    verification_bundle,
                    extraction,
                )
                atomic_json(verification_path, verification)
                self._write_stage_cache_identity(
                    batch_bundle,
                    "verification",
                    verification_identity,
                    metadata_path,
                )
        except (OSError, RuntimeError, TimeoutError) as exc:
            child_batches = self._split_batch_pages(pages)
            if not child_batches or not self._retryable_batch_failure(exc):
                raise
            atomic_json(split_marker_path, {
                "parent_extraction_identity": extraction_identity,
                "reason": f"retryable request failure: {exc}",
                "children": [
                    [page.page_number for page in child]
                    for child in child_batches
                ],
            })
            print(
                f"Adaptive retry split {label} pages {page_numbers[0]}-"
                f"{page_numbers[-1]} after: {exc}",
                file=sys.stderr,
                flush=True,
            )
            results = []
            for child in child_batches:
                child_label = (
                    f"{label}.split-p{child[0].page_number:04d}-"
                    f"{child[-1].page_number:04d}"
                )
                results.extend(self._extract_and_verify_page_group(
                    bundle,
                    context,
                    child,
                    root_index,
                    root_count,
                    force,
                    child_label,
                    split_depth + 1,
                ))
            return results
        extraction, verification = match_verified_extraction(
            extraction,
            verification,
        )
        return [(extraction, verification)]

    def _extract_and_verify_batches(
        self, bundle: EvidenceBundle, context: dict[str, Any], force: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        batches = page_batches(bundle.pages, self.batch_pages, self.batch_overlap)
        extractions: list[dict[str, Any]] = []
        verifications: list[dict[str, Any]] = []
        for index, pages in enumerate(batches, 1):
            pairs = self._extract_and_verify_page_group(
                bundle,
                context,
                pages,
                index,
                len(batches),
                force,
                f"batch-{index:03d}",
            )
            for extraction, verification in pairs:
                extractions.append(extraction)
                verifications.append(verification)
        extraction, verification = merge_visual_batches(extractions, verifications)
        atomic_json(bundle.artifact_dir / "gemini_extraction.json", extraction)
        atomic_json(bundle.artifact_dir / "gemini_verification.json", verification)
        self._write_stage_cache_identity(
            bundle, "extraction", self._extraction_cache_identity(bundle),
        )
        self._write_stage_cache_identity(
            bundle, "verification",
            self._verification_cache_identity(bundle, extraction),
        )
        return extraction, verification

    def _process_structured_spreadsheet(
        self,
        source: Path,
        source_relative: str,
        context: dict[str, Any],
        force: bool,
        process_started: float,
    ):
        """Use direct XLSX cells as truth and Gemini only as a compact auditor."""
        if source.suffix.casefold() not in {".xlsx", ".csv"}:
            return None
        local_started = time.perf_counter()
        resolved = source.resolve()
        digest = sha256_file(resolved)
        artifact_id = f"VI-{digest[:20]}"
        directory = self.builder.artifact_root / artifact_id
        raw_text = self.builder._raw_text(resolved, digest, directory)
        schemas = detect_spreadsheet_schemas(raw_text)
        if not schemas or any(
            schema.get("requires_visual") is True for schema in schemas
        ):
            return None
        evidence = build_spreadsheet_evidence(raw_text, schemas, context)
        extraction = evidence["extraction"]
        completeness = evidence["completeness"]
        packet = evidence["packet"]
        screening = {
            "processing_status": (
                "classified"
                if completeness["completion_status"] == "complete"
                else "needs_review"
            ),
            "format_routing": "deterministic_spreadsheet_cells",
            "pages_screened": [],
            "pages_selected_for_full_analysis": [],
            "page_manifest": [],
            "schemas": schemas,
        }
        original_type = resolved.suffix.casefold().lstrip(".")
        bundle = EvidenceBundle(
            artifact_id,
            resolved,
            digest,
            original_type,
            raw_text,
            [],
            directory,
            document_page_count=0,
            screening=screening,
        )
        atomic_json(directory / "spreadsheet_schema.json", schemas)
        atomic_json(directory / "spreadsheet_evidence_packet.json", packet)
        atomic_json(directory / "completeness_manifest.json", completeness)
        atomic_json(directory / "gemini_extraction.json", extraction)
        atomic_json(directory / "manifest.json", {
            "artifact_id": artifact_id,
            "source_filename": resolved.name,
            "source_sha256": digest,
            "original_type": original_type,
            "pipeline_version": PIPELINE_VERSION,
            "spreadsheet_pipeline_version": SPREADSHEET_PIPELINE_VERSION,
            "text_extraction_version": TEXT_EXTRACTION_VERSION,
            "normalized_content_fingerprint": normalized_content_fingerprint(
                raw_text, digest,
            ),
            "structured_comment_count": len(extraction.get("records", [])),
            "structured_response_count": int(
                extraction.get("structured_response_count") or 0
            ),
        })
        self._write_job_progress(
            bundle,
            "fingerprint_file",
            source_sha256=digest,
            normalized_content_fingerprint=normalized_content_fingerprint(
                raw_text, digest,
            ),
        )
        self._write_job_progress(
            bundle,
            "classify_canonical_document",
            route="deterministic_spreadsheet_cells",
            schema_count=len(schemas),
        )
        verification_path = directory / "spreadsheet_verification.json"
        metadata_path = directory / "spreadsheet_cache_metadata.json"
        verification_identity = {
            "stage": "spreadsheet_verification",
            "source_sha256": digest,
            "model": str(getattr(self.client, "model", "test-client")),
            "pipeline_version": SPREADSHEET_PIPELINE_VERSION,
            "prompt_version": SPREADSHEET_VERIFICATION_PROMPT_VERSION,
            "packet_fingerprint": self._json_digest(packet),
        }
        verification_error = ""
        if not packet.get("groups"):
            compact_verification = {
                "document_verified": True,
                "template_verified": True,
                "every_candidate_assigned": True,
                "same_row_links_correct": True,
                "verified_group_ids": [],
                "rejected_group_ids": [],
                "missing_unit_ids": [],
                "incorrect_groupings": [],
                "incorrect_links": [],
                "verification_summary": "No candidate comment rows were present",
            }
        elif (
            not force
            and verification_path.is_file()
            and self._stage_cache_is_compatible(
                bundle,
                "verification",
                verification_identity,
                metadata_path,
            )
        ):
            compact_verification = json.loads(
                verification_path.read_text(encoding="utf-8")
            )
            self._metrics["spreadsheet_verification_cache_hits"] += 1
        else:
            try:
                compact_verification = self._verify_spreadsheet_units(
                    packet,
                    context,
                    artifact_id,
                )
            except Exception as exc:
                verification_error = str(exc)
                compact_verification = {
                    "document_verified": False,
                    "template_verified": False,
                    "every_candidate_assigned": False,
                    "same_row_links_correct": False,
                    "verified_group_ids": [],
                    "rejected_group_ids": [],
                    "missing_unit_ids": [],
                    "incorrect_groupings": [],
                    "incorrect_links": [],
                    "verification_summary": (
                        "Gemini spreadsheet verification failed: "
                        f"{verification_error}"
                    ),
                }
            atomic_json(verification_path, compact_verification)
            if not verification_error:
                self._write_stage_cache_identity(
                    bundle,
                    "verification",
                    verification_identity,
                    metadata_path,
                )
        atomic_json(verification_path, compact_verification)
        verification = local_verification_result(
            evidence, compact_verification,
        )
        atomic_json(directory / "gemini_verification.json", verification)
        atomic_json(directory / "verified_matching.json", {
            "pipeline_version": PIPELINE_VERSION,
            "spreadsheet_pipeline_version": SPREADSHEET_PIPELINE_VERSION,
            "records": extraction.get("records", []),
            "verification_records": verification.get("records", []),
            "completeness_manifest": completeness,
        })
        self._write_job_progress(
            bundle,
            "extract_records",
            extracted_records=len(extraction.get("records", [])),
            local_structured=True,
        )
        self._write_job_progress(
            bundle,
            "verify_records",
            extracted_records=len(extraction.get("records", [])),
            document_verified=document_verified(verification),
        )

        result = results_to_dataset_rows(
            bundle,
            extraction,
            verification,
            source_relative,
        )
        comments, responses, links, summary, review = result
        verified_count = sum(
            row.get("search_eligible") is True for row in comments
        )
        unresolved = int(completeness.get("unresolved_signal_count") or 0)
        if not document_verified(verification):
            unresolved += 1
        reconciliation = {
            **completeness,
            "expected_comment_count": int(
                completeness.get("candidate_comment_count") or 0
            ),
            "extracted_comment_count": len(comments),
            "verified_comment_count": verified_count,
            "unresolved_signal_count": unresolved,
            "completion_status": (
                "complete" if unresolved == 0 else "needs_review"
            ),
            "pages_screened": 0,
            "pages_fully_processed": 0,
            "pages_escalated": [],
        }
        local_seconds = time.perf_counter() - local_started
        total_cache_checks = (
            self._metrics["gemini_spreadsheet_verification_calls"]
            + self._metrics["spreadsheet_verification_cache_hits"]
        )
        performance = {
            **self._metrics,
            "local_evidence_build_seconds": round(local_seconds, 4),
            "total_wall_seconds": round(
                time.perf_counter() - process_started, 4,
            ),
            "pages_screened": 0,
            "pages_fully_analyzed": 0,
            "structured_rows": len(comments),
            "cache_hit_percentage": round(
                100.0
                * self._metrics["spreadsheet_verification_cache_hits"]
                / total_cache_checks,
                2,
            ) if total_cache_checks else 0.0,
            "gemini_input_tokens": self._metrics[
                "gemini_spreadsheet_verification_input_tokens"
            ],
            "gemini_output_tokens": self._metrics[
                "gemini_spreadsheet_verification_output_tokens"
            ],
        }
        summary.update(reconciliation)
        summary["performance"] = performance
        if verification_error:
            summary["processing_error"] = verification_error
        if reconciliation["completion_status"] != "complete":
            review.append({
                "item_type": "spreadsheet_completeness_reconciliation",
                "item_id": artifact_id,
                "reason": (
                    f"{unresolved} spreadsheet unit/verification signal(s) "
                    "remain unresolved"
                ),
                "source_document": source_relative,
                "source_location": "complete workbook",
                "suggested_action": (
                    "Review the saved cell-unit packet and only the affected "
                    "sheet rows"
                ),
                "decision": "",
                "decision_note": "",
            })
        atomic_json(directory / "audit.json", {
            "artifact_id": artifact_id,
            "source_filename": resolved.name,
            "source_sha256": digest,
            "raw_text_artifact": "raw_text.json",
            "evidence_packet_artifact": "spreadsheet_evidence_packet.json",
            "verification_artifact": "spreadsheet_verification.json",
            "pipeline_version": PIPELINE_VERSION,
            "spreadsheet_pipeline_version": SPREADSHEET_PIPELINE_VERSION,
            "schemas": schemas,
            "completeness_reconciliation": reconciliation,
            "performance": performance,
        })
        self._write_job_progress(
            bundle,
            "finalize_site_report",
            completion_status=reconciliation["completion_status"],
            verified_records=verified_count,
        )
        return comments, responses, links, summary, review

    def process(self, source: Path, source_relative: str, context: dict[str, Any], force: bool = False):
        process_started = time.perf_counter()
        self._metrics = {
            "gemini_extraction_calls": 0,
            "gemini_verification_calls": 0,
            "gemini_spreadsheet_verification_calls": 0,
            "gemini_extraction_seconds": 0.0,
            "gemini_verification_seconds": 0.0,
            "gemini_spreadsheet_verification_seconds": 0.0,
            "extraction_cache_hits": 0,
            "verification_cache_hits": 0,
            "spreadsheet_verification_cache_hits": 0,
            "gemini_extraction_input_tokens": 0,
            "gemini_extraction_output_tokens": 0,
            "gemini_verification_input_tokens": 0,
            "gemini_verification_output_tokens": 0,
            "gemini_spreadsheet_verification_input_tokens": 0,
            "gemini_spreadsheet_verification_output_tokens": 0,
            "gemini_cached_input_tokens": 0,
            "gemini_thought_tokens": 0,
            "request_metrics": [],
        }
        structured_result = self._process_structured_spreadsheet(
            source,
            source_relative,
            context,
            force,
            process_started,
        )
        if structured_result is not None:
            return structured_result
        build_started = time.perf_counter()
        bundle = self.builder.build(source)
        build_seconds = time.perf_counter() - build_started
        self._write_job_progress(
            bundle, "fingerprint_file",
            source_sha256=bundle.source_sha256,
            normalized_content_fingerprint=str(
                json.loads(
                    (bundle.artifact_dir / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                ).get("normalized_content_fingerprint", "")
            ) if (bundle.artifact_dir / "manifest.json").is_file() else "",
        )
        self._write_job_progress(
            bundle, "classify_canonical_document",
            pages_screened=len(bundle.screening.get("pages_screened", [])),
            pages_selected=len(bundle.pages),
        )
        screening_status = str(bundle.screening.get("processing_status", "classified"))
        if not bundle.pages:
            local_stage_timings = bundle.screening.get(
                "current_run_stage_timings", {}
            )
            performance = {
                **self._metrics,
                "local_evidence_build_seconds": round(build_seconds, 4),
                **local_stage_timings,
                "total_wall_seconds": round(time.perf_counter() - process_started, 4),
                "pages_screened": len(bundle.screening.get("pages_screened", [])),
                "pages_fully_analyzed": 0,
                "cache_hit_percentage": 0.0,
                "gemini_input_tokens": 0,
                "gemini_output_tokens": 0,
            }
            summary = {
                "city": str(context.get("city_hint", "")),
                "property_project": str(context.get("property_hint", "")),
                "review_round": str(context.get("review_round_hint", "")),
                "source_document": source_relative,
                "source_type": "adaptive_page_screening",
                "comment_count": 0, "response_count": 0,
                "matched_count": 0, "unmatched_count": 0,
                "extraction_method": "adaptive_local_screening",
                "processing_error": str(bundle.screening.get("review_reason", "")),
                "processing_status": screening_status,
                "opened": True,
                "pages_screened": bundle.screening.get("pages_screened", []),
                "pages_fully_analyzed": [],
                "additional_markup_detected": bool(bundle.screening.get("additional_markup_detected")),
                "verification_result": "not_run",
                "ingestion_pipeline_version": PIPELINE_VERSION,
                "source_sha256": bundle.source_sha256,
                "expected_comment_count": 0,
                "extracted_comment_count": 0,
                "verified_comment_count": 0,
                "unresolved_signal_count": (
                    1 if screening_status == "needs_review" else 0
                ),
                "pages_escalated": bundle.screening.get("pages_escalated", []),
                "completion_status": (
                    "needs_review" if screening_status == "needs_review"
                    else "no_relevant_content"
                ),
                "performance": performance,
            }
            review = []
            if screening_status == "needs_review":
                review.append({
                    "item_type": "page_screening", "item_id": bundle.artifact_id,
                    "reason": summary["processing_error"] or "Page screening was uncertain",
                    "source_document": source_relative, "source_location": "complete document",
                    "suggested_action": "Review the screening thumbnails before declaring no relevant content",
                    "decision": "", "decision_note": "",
                })
            atomic_json(bundle.artifact_dir / "audit.json", {
                "artifact_id": bundle.artifact_id, "source_filename": source.name,
                "source_sha256": bundle.source_sha256,
                "pipeline_version": PIPELINE_VERSION,
                "page_count": bundle.document_page_count,
                "page_screening": bundle.screening,
                "processing_status": screening_status,
                "completeness_reconciliation": {
                    "expected_comment_count": 0,
                    "extracted_comment_count": 0,
                    "verified_comment_count": 0,
                    "unresolved_signal_count": summary["unresolved_signal_count"],
                    "completion_status": summary["completion_status"],
                },
                "performance": performance,
            })
            self._write_job_progress(
                bundle, "finalize_site_report",
                completion_status=summary["completion_status"],
                verified_records=0,
            )
            return [], [], [], summary, review
        extraction_path = bundle.artifact_dir / "gemini_extraction.json"
        verification_path = bundle.artifact_dir / "gemini_verification.json"
        use_batches = self.batch_pages > 0 and len(bundle.pages) > self.batch_pages
        if use_batches:
            extraction, verification = self._extract_and_verify_batches(bundle, context, force)
        else:
            extraction_identity = self._extraction_cache_identity(bundle)
            extraction_cached = (
                not force
                and extraction_path.is_file()
                and self._stage_cache_is_compatible(
                    bundle, "extraction", extraction_identity,
                )
            )
            if extraction_cached:
                extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
                self._metrics["extraction_cache_hits"] += 1
            else:
                extraction = self._extract(bundle, context)
                atomic_json(extraction_path, extraction)
                self._write_stage_cache_identity(
                    bundle, "extraction", extraction_identity,
                )
            verification_bundle = self._verification_bundle_for_confidence(
                bundle, extraction,
            )
            verification_identity = self._verification_cache_identity(
                verification_bundle, extraction,
            )
            verification_cached = (
                not force
                and verification_path.is_file()
                and self._stage_cache_is_compatible(
                    bundle, "verification", verification_identity,
                )
            )
            if verification_cached:
                verification = json.loads(verification_path.read_text(encoding="utf-8"))
                self._metrics["verification_cache_hits"] += 1
            else:
                verification = self._verify(verification_bundle, extraction)
                atomic_json(verification_path, verification)
                self._write_stage_cache_identity(
                    bundle, "verification", verification_identity,
                )
        self._write_job_progress(
            bundle, "extract_records",
            extracted_records=len(extraction.get("records", [])),
        )
        extraction, verification = match_verified_extraction(extraction, verification)
        self._write_job_progress(
            bundle, "verify_records",
            extracted_records=len(extraction.get("records", [])),
            document_verified=document_verified(verification),
        )
        atomic_json(bundle.artifact_dir / "verified_matching.json", {
            "pipeline_version": PIPELINE_VERSION,
            "records": extraction.get("records", []),
            "verification_records": verification.get("records", []),
            "unmatched_response_keys": extraction.get("unmatched_response_keys", []),
        })
        context_conflicts: list[str] = []
        city_hint = str(context.get("city_hint", "")).strip()
        round_hint = str(context.get("review_round_hint", "")).strip()
        if city_hint and normalized_whitespace(str(extraction.get("city", ""))).casefold() != normalized_whitespace(city_hint).casefold():
            context_conflicts.append(f"Gemini city {extraction.get('city')!r} conflicts with audited city {city_hint!r}")
        extracted_round = str(extraction.get("review_round", "")).strip()
        audit_document_type = str(context.get("audit_document_type_hint", "")).casefold()
        response_document = "response" in audit_document_type or "response" in source.name.casefold()
        round_offset_is_expected = False
        if response_document and round_hint and extracted_round and extracted_round.isdigit() and round_hint.isdigit():
            round_offset_is_expected = int(extracted_round) == int(round_hint) + 1
        if round_hint and normalized_whitespace(extracted_round).casefold() != normalized_whitespace(round_hint).casefold():
            if round_offset_is_expected:
                extraction["response_letter_round"] = extracted_round
                extraction["reviewed_plan_round"] = round_hint
                extraction["review_round"] = round_hint
            else:
                context_conflicts.append(f"Gemini review round {extraction.get('review_round')!r} conflicts with audited round {round_hint!r}")
        if context_conflicts:
            extraction["document_uncertain"] = True
            existing_reason = str(extraction.get("document_uncertainty_reason", "")).strip()
            extraction["document_uncertainty_reason"] = " ".join([*context_conflicts, existing_reason]).strip()
        if screening_status == "needs_review":
            extraction["document_uncertain"] = True
            extraction["document_uncertainty_reason"] = " ".join(filter(None, [
                str(bundle.screening.get("review_reason", "")).strip(),
                str(extraction.get("document_uncertainty_reason", "")).strip(),
            ]))
        regression = None
        if self.oracle_dataset and self.oracle_dataset.is_file():
            oracle = json.loads(self.oracle_dataset.read_text(encoding="utf-8"))
            regression = regression_against_oracle(extraction, oracle, source.name)
            atomic_json(bundle.artifact_dir / "confirmed_reference_regression.json", regression)
        reconciliation = reconcile_document_completeness(
            bundle, extraction, verification,
        )
        local_stage_timings = bundle.screening.get(
            "current_run_stage_timings", {}
        )
        total_cache_checks = (
            self._metrics["gemini_extraction_calls"]
            + self._metrics["gemini_verification_calls"]
            + self._metrics["extraction_cache_hits"]
            + self._metrics["verification_cache_hits"]
        )
        performance = {
            **self._metrics,
            "local_evidence_build_seconds": round(build_seconds, 4),
            **local_stage_timings,
            "total_wall_seconds": round(time.perf_counter() - process_started, 4),
            "pages_screened": len(bundle.screening.get("pages_screened", [])),
            "pages_fully_analyzed": len(bundle.pages),
            "cache_hit_percentage": round(
                100.0 * (
                    self._metrics["extraction_cache_hits"]
                    + self._metrics["verification_cache_hits"]
                ) / total_cache_checks,
                2,
            ) if total_cache_checks else 0.0,
            "gemini_input_tokens": (
                self._metrics["gemini_extraction_input_tokens"]
                + self._metrics["gemini_verification_input_tokens"]
            ),
            "gemini_output_tokens": (
                self._metrics["gemini_extraction_output_tokens"]
                + self._metrics["gemini_verification_output_tokens"]
            ),
        }
        audit = {
            "artifact_id": bundle.artifact_id, "source_filename": source.name,
            "source_sha256": bundle.source_sha256, "raw_text_artifact": "raw_text.json",
            "extraction_artifact": "gemini_extraction.json", "verification_artifact": "gemini_verification.json",
            "page_count": bundle.document_page_count or len(bundle.pages),
            "pages_screened": bundle.screening.get("pages_screened", []),
            "pages_fully_analyzed": [page.page_number for page in bundle.pages],
            "additional_markup_detected": bool(bundle.screening.get("additional_markup_detected")),
            "page_screening": bundle.screening,
            "pipeline_version": PIPELINE_VERSION,
            "regression": regression,
            "completeness_reconciliation": reconciliation,
            "performance": performance,
        }
        atomic_json(bundle.artifact_dir / "audit.json", audit)
        result = results_to_dataset_rows(
            bundle, extraction, verification, source_relative, regression,
        )
        comments, responses, links, summary, review = result
        summary.update(reconciliation)
        summary["performance"] = performance
        if reconciliation["completion_status"] != "complete":
            summary["processing_error"] = "Completeness reconciliation requires review"
            review.append({
                "item_type": "completeness_reconciliation",
                "item_id": bundle.artifact_id,
                "reason": (
                    f"{reconciliation['unresolved_signal_count']} deterministic "
                    "page/number signal(s) remain unresolved"
                ),
                "source_document": source_relative,
                "source_location": "complete document",
                "suggested_action": (
                    "Review missing number/response signals and rerun only the "
                    "affected page batch"
                ),
                "decision": "",
                "decision_note": "",
            })
        self._write_job_progress(
            bundle, "finalize_site_report",
            completion_status=reconciliation["completion_status"],
            verified_records=reconciliation["verified_comment_count"],
        )
        return comments, responses, links, summary, review
