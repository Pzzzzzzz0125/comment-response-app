#!/usr/bin/env python3
"""Backfill trustworthy document/event dates without re-running Gemini.

Dates are a presentation and deduplication aid, not a replacement for the
immutable extracted text.  This repair deliberately uses existing structured
metadata first, then dates printed in a source filename, and finally the
visible/core metadata of a local DOCX.  It never uses filesystem mtime and it
never treats a review-round label as a date.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

_ISO_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[-_.](?P<month>\d{1,2})[-_.](?P<day>\d{1,2})(?!\d)"
)
_NUMERIC_DATE = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})[-/.](?P<day>\d{1,2})[-/.](?P<year>\d{2}|20\d{2})(?!\d)"
)
_COMPACT_TIMESTAMP = re.compile(r"(?<!\d)(?P<value>20\d{6})(?:\d{4,})?(?!\d)")
_WORD_DATE = re.compile(
    r"\b(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:,|\s)\s*"
    r"(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        start=1,
    )
}
_MONTHS.update({name[:3].casefold(): value for name, value in list(_MONTHS.items())})


def _first_path(value: Any) -> str:
    return str(value or "").split(" | ", 1)[0].strip()


def _iso(year: int, month: int, day: int) -> str:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def parse_date(value: Any) -> tuple[str, str]:
    """Return (ISO date, exact evidence) from a value, if it is unambiguous."""
    if isinstance(value, dict):
        raw = value.get("iso") or value.get("raw") or value.get("value") or value.get("evidence")
    else:
        raw = value
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return "", ""
    matches = list(_ISO_DATE.finditer(text))
    if matches:
        match = matches[-1]
        parsed = _iso(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        if parsed:
            return parsed, match.group(0)
    matches = list(_NUMERIC_DATE.finditer(text))
    if matches:
        match = matches[-1]
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        parsed = _iso(year, int(match.group("month")), int(match.group("day")))
        if parsed:
            return parsed, match.group(0)
    matches = list(_WORD_DATE.finditer(text))
    if matches:
        match = matches[-1]
        month = _MONTHS.get(match.group("month").casefold().rstrip("."), 0)
        parsed = _iso(int(match.group("year")), month, int(match.group("day"))) if month else ""
        if parsed:
            return parsed, match.group(0)
    return "", ""


def filename_date(path: str) -> tuple[str, str, str]:
    """Find the most credible calendar date in a filename/path.

    ``20250515155401`` is a ProjectDox upload timestamp and is reduced to its
    calendar date.  Identifiers such as ``2025-01057`` are intentionally not
    accepted because they do not contain a month/day pair.  A clock suffix in
    a ProjectDox workbook (``02-20-2026_5_04_PM``) must not be read as the
    unrelated date ``2026-05-04``.  Explicit month-day-year and hyphen/slash
    year-month-day dates therefore outrank underscore-separated fallbacks.
    """
    text = str(path or "")
    candidates: list[tuple[int, int, str, str, str]] = []
    for match in _ISO_DATE.finditer(text):
        parsed = _iso(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        if parsed:
            # ``2026_5_04_PM`` is conventionally the time part of a preceding
            # ProjectDox timestamp, not an independent calendar date.
            clock_suffix = bool(re.match(r"_(?:AM|PM)\\b", text[match.end():], re.I))
            separator = match.group(0)[4:5]
            priority = 3 if separator in {"-", "/", "."} else 1
            if not clock_suffix:
                candidates.append((priority, match.start(), parsed, match.group(0), "filename_iso"))
    for match in _NUMERIC_DATE.finditer(text):
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        parsed = _iso(year, int(match.group("month")), int(match.group("day")))
        if parsed:
            candidates.append((4, match.start(), parsed, match.group(0), "filename_numeric"))
    for match in _COMPACT_TIMESTAMP.finditer(text):
        value = match.group("value")
        parsed = _iso(int(value[:4]), int(value[4:6]), int(value[6:8]))
        if parsed:
            candidates.append((2, match.start(), parsed, match.group(0), "filename_compact_timestamp"))
    if not candidates:
        return "", "", ""
    _priority, _position, parsed, evidence, method = max(
        candidates, key=lambda item: (item[0], item[1])
    )
    return parsed, evidence, method


def _candidate_from_row(row: dict[str, Any]) -> tuple[str, str, str]:
    """Use only fields that represent the physical source document date."""
    for field in (
        "source_document_date", "document_date_iso", "document_date",
        "report_date", "letter_date",
    ):
        parsed, evidence = parse_date(row.get(field))
        if parsed:
            value = row.get(field)
            source = value.get("source") if isinstance(value, dict) else field
            return parsed, evidence or str(value), f"existing_{source or field}"
    return "", "", ""


def _row_date_is_filename_derived(row: dict[str, Any]) -> bool:
    """Whether a stored date is only a low-confidence filename fallback."""
    methods = (
        str(row.get("source_date_method") or ""),
        str(row.get("document_date_source") or ""),
        str((row.get("document_date") or {}).get("source") or "")
        if isinstance(row.get("document_date"), dict) else "",
    )
    return any("filename" in method.casefold() for method in methods)


def source_date(path: str, rows: list[dict[str, Any]], workspace: Path) -> tuple[str, str, str]:
    # Printed/header dates are authoritative.  A legacy filename-derived
    # value is deliberately considered only after reparsing the filename,
    # because older versions could confuse a trailing ``_H_MM_PM`` time with
    # an ISO-looking date.
    for row in rows:
        if _row_date_is_filename_derived(row):
            continue
        parsed, evidence, method = _candidate_from_row(row)
        if parsed:
            return parsed, evidence, method
    parsed, evidence, method = filename_date(path)
    if parsed:
        return parsed, evidence, method
    for row in rows:
        parsed, evidence, method = _candidate_from_row(row)
        if parsed:
            return parsed, evidence, method
    source_path = Path(path)
    if not source_path.is_absolute():
        source_path = workspace / source_path
    # source_lineage's DOCX reader uses visible/core document metadata and does
    # not rely on file timestamps.  Keep this optional for missing/old files.
    if source_path.suffix.casefold() == ".docx" and source_path.exists():
        try:
            from web_app.source_lineage import document_date

            parsed, evidence, method = document_date(source_path, [])
            if parsed:
                return parsed, evidence, f"docx_{method}"
        except (OSError, ValueError, ImportError, TypeError):
            pass
    return "", "", ""


def _metadata(iso: str, evidence: str, method: str) -> dict[str, Any]:
    confidence = 0.9 if method.startswith("existing_") else 0.75
    return {
        "raw": evidence,
        "iso": iso,
        "source": method,
        "page": 0,
        "evidence": evidence,
        "confidence": confidence,
    }


def _round_metadata(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return round provenance without inferring a round from a filename.

    A PC marker in a filename is useful for prescan routing, but it is not
    reliable enough to overwrite the reviewed-plan round.  Existing explicit
    fields are therefore copied into the normalized provenance object and
    remain auditable.
    """
    value = str(row.get("reviewed_plan_round") or row.get("review_round") or "").strip()
    if not value:
        return None
    source = str(row.get("review_round_source") or "").strip()
    if not source:
        source = "reviewed_plan_round" if row.get("reviewed_plan_round") else "record_field"
    raw = str(row.get("review_round_raw") or value).strip()
    try:
        confidence = float(row.get("review_round_confidence"))
    except (TypeError, ValueError):
        confidence = 0.99 if source in {"document_header", "reviewed_plan_round"} else 0.75
    return {"value": value, "raw": raw, "source": source, "confidence": confidence}


def _reviewer_header_date(row: dict[str, Any]) -> tuple[str, str]:
    """Extract a printed event date from a reviewer header, if present.

    Some workbook imports put ``Reviewer Name 7/11/25 3:50 PM`` in the
    reviewer column rather than in a dedicated date field.  That is an event
    date for the government comment, not the date of the workbook itself.
    """
    for field in ("reviewer", "reviewer_context", "reviewer_name"):
        raw = str(row.get(field) or "").strip()
        parsed, evidence = parse_date(raw)
        if parsed:
            return parsed, evidence
    return "", ""


def apply_quality_metadata(row: dict[str, Any], role: str, link: dict[str, Any] | None = None) -> bool:
    """Fill split quality fields locally; never replace an existing audit value.

    Older exports used ``source_status=verified`` without the normalized
    verification fields.  That status is an explicit legacy admission signal,
    so it is upgraded to the new fields.  Other rows with no two-pass audit are
    deliberately quarantined instead of being silently treated as searchable.
    """
    changed = False
    text = str(row.get("verified_text") or row.get("original_text") or "").strip()
    verified = str(row.get("verification_status") or row.get("text_trust_status") or "").casefold()
    legacy_verified = (
        str(row.get("source_status") or "").casefold() in {"verified", "confirmed"}
        and str(row.get("human_review_status") or "").casefold() in {"confirmed", "not_required"}
        and bool(text)
    )
    if "verification_status" not in row:
        row["verification_status"] = "confirmed" if legacy_verified else "needs_review"
        changed = True
    if "text_trust_status" not in row:
        row["text_trust_status"] = "verified" if legacy_verified else "quarantined"
        changed = True
    if role == "comment" and "search_eligible" not in row:
        row["search_eligible"] = bool(legacy_verified)
        changed = True
    verified = str(row.get("verification_status") or row.get("text_trust_status") or "").casefold()
    if "transcription_confidence" not in row:
        row["transcription_confidence"] = 1.0 if verified in {"confirmed", "verified"} else (0.5 if text else 0.0)
        changed = True
    if "role_confidence" not in row:
        row["role_confidence"] = 1.0 if role in {"comment", "response"} and text else 0.0
        changed = True
    if "date_confidence" not in row:
        metadata = row.get("document_date") if isinstance(row.get("document_date"), dict) else {}
        row["date_confidence"] = float(metadata.get("confidence") or 0.0)
        changed = True
    if "round_confidence" not in row:
        metadata = row.get("review_round_metadata") if isinstance(row.get("review_round_metadata"), dict) else {}
        row["round_confidence"] = float(metadata.get("confidence") or 0.0)
        changed = True
    if "pairing_confidence" not in row:
        if role == "response":
            row["pairing_confidence"] = 1.0 if link and str(link.get("match_status", "")).casefold() == "matched" else 0.0
        else:
            response_id = str(row.get("response_id") or (link or {}).get("response_id") or "")
            row["pairing_confidence"] = 1.0 if response_id and str((link or {}).get("match_status", "")).casefold() == "matched" else 0.0
        changed = True
    return changed


def apply_row_date(row: dict[str, Any], info: tuple[str, str, str]) -> bool:
    iso, evidence, method = info
    if not iso:
        return False
    changed = False
    replace_filename_fallback = _row_date_is_filename_derived(row) and str(row.get("source_document_date") or "") != iso
    if not row.get("source_document_date") or replace_filename_fallback:
        row["source_document_date"] = iso
        changed = True
    if not row.get("source_date_evidence") or replace_filename_fallback:
        row["source_date_evidence"] = evidence or iso
        changed = True
    if not row.get("source_date_method") or row.get("source_date_method") == "missing" or replace_filename_fallback:
        row["source_date_method"] = method
        changed = True
    if not row.get("document_date_iso") or replace_filename_fallback:
        row["document_date_iso"] = iso
        changed = True
    if not row.get("document_date_raw") or replace_filename_fallback:
        row["document_date_raw"] = evidence or iso
        changed = True
    if not row.get("document_date_source") or replace_filename_fallback:
        row["document_date_source"] = method
        changed = True
    if not row.get("document_date") or not isinstance(row.get("document_date"), dict) or replace_filename_fallback:
        row["document_date"] = _metadata(iso, evidence or iso, method)
        changed = True
    if not row.get("document_date_provenance") or replace_filename_fallback:
        row["document_date_provenance"] = {
            "value": iso,
            "raw_text": evidence or iso,
            "source": method,
            "confidence": float(row.get("document_date", {}).get("confidence") or 0.0),
        }
        changed = True
    return changed


def _source_path(row: dict[str, Any]) -> str:
    return _first_path(row.get("source_document"))


def _source_file_path(row: dict[str, Any]) -> str:
    folder = str(row.get("folder_path") or "").strip().rstrip("/")
    filename = str(row.get("filename") or "").strip()
    return f"{folder}/{filename}" if folder and filename else ""


def backfill(dataset_path: Path, workspace: Path, *, dry_run: bool = False) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    comments = [row for row in dataset.get("comments", []) if isinstance(row, dict)]
    responses = [row for row in dataset.get("responses", []) if isinstance(row, dict)]
    links = [row for row in dataset.get("comment_response_links", []) if isinstance(row, dict)]
    links_by_comment = {str(row.get("comment_id", "")): row for row in links}

    rows_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in [*comments, *responses, *links]:
        path = _source_path(row)
        if path:
            rows_by_path[path].append(row)
    # Build once per physical path so a large source repeated across rows is
    # never reopened or re-parsed.
    dates: dict[str, tuple[str, str, str]] = {
        path: source_date(path, rows, workspace)
        for path, rows in rows_by_path.items()
    }

    changed_comments = changed_responses = changed_links = changed_events = 0
    exact_dates = Counter()
    for row in comments:
        info = dates.get(_source_path(row), ("", "", ""))
        if apply_row_date(row, info):
            changed_comments += 1
        round_info = _round_metadata(row)
        if round_info and row.get("review_round_metadata") != round_info:
            row["review_round_metadata"] = round_info
            changed_comments += 1
        if apply_quality_metadata(row, "comment", links_by_comment.get(str(row.get("comment_id", "")))):
            changed_comments += 1
        if row.get("source_document_date"):
            exact_dates[str(row["source_date_method"])] += 1
        reviewer_date, reviewer_evidence = _reviewer_header_date(row)
        existing_event_date, _existing_evidence = parse_date(
            row.get("event_date") or row.get("event_date_iso")
        )
        # A reviewer-header timestamp is direct evidence of this comment's
        # occurrence.  It outranks a source workbook upload/export date but
        # never changes that physical document date.
        if reviewer_date and not existing_event_date:
            row["event_date"] = reviewer_date
            row["event_date_iso"] = reviewer_date
            row["event_date_raw"] = reviewer_evidence
            row["event_date_source"] = "reviewer_header"
            row["event_date_confidence"] = 1.0
            changed_comments += 1
        # Embedded status lines are an event date, not a document date.
        if row.get("response_date_iso"):
            for event in row.get("issue_thread_events", []) or []:
                if isinstance(event, dict) and str(event.get("event_type", "")) == "applicant_response":
                    if not event.get("occurred_at_label"):
                        event["occurred_at_label"] = row["response_date_iso"]
                        event["occurred_at"] = row["response_date_iso"]
                        changed_events += 1

    for row in responses:
        info = dates.get(_source_path(row), ("", "", ""))
        if apply_row_date(row, info):
            changed_responses += 1
        round_info = _round_metadata(row)
        if round_info and row.get("review_round_metadata") != round_info:
            row["review_round_metadata"] = round_info
            changed_responses += 1
        if apply_quality_metadata(row, "response", links_by_comment.get(str(row.get("comment_id", "")))):
            changed_responses += 1
        response_date, response_evidence = parse_date(row.get("response_date_iso") or row.get("response_date_raw"))
        if response_date and not row.get("event_date"):
            row["event_date"] = response_date
            row["event_date_raw"] = response_evidence or response_date
            row["event_date_source"] = "embedded_response_status"
            changed_responses += 1
        elif row.get("source_document_date") and not row.get("event_date"):
            row["event_date"] = row["source_document_date"]
            row["event_date_raw"] = row.get("source_date_evidence", row["source_document_date"])
            row["event_date_source"] = "source_document_date"
            changed_responses += 1

    for row in links:
        info = dates.get(_source_path(row), ("", "", ""))
        if apply_row_date(row, info):
            changed_links += 1
        if "date_confidence" not in row:
            row["date_confidence"] = float(row.get("document_date", {}).get("confidence") or 0.0) if isinstance(row.get("document_date"), dict) else 0.0
            changed_links += 1

    # Every issue-index occurrence is enriched independently.  This keeps
    # source provenance while letting the server merge same-date copies.
    index = dataset.get("issue_event_index", {})
    for thread in index.values() if isinstance(index, dict) else []:
        if not isinstance(thread, dict):
            continue
        for event in thread.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            occurrence_rows: list[dict[str, Any]] = []
            for occurrence in event.get("source_occurrences", []) or []:
                if not isinstance(occurrence, dict):
                    continue
                path = _source_path(occurrence)
                info = dates.get(path, ("", "", ""))
                if apply_row_date(occurrence, info):
                    changed_events += 1
                occurrence_rows.extend(rows_by_path.get(path, []))
            event_path = _source_path(event)
            info = dates.get(event_path, ("", "", ""))
            if not info[0] and occurrence_rows:
                info = _candidate_from_row(occurrence_rows[0])
            if apply_row_date(event, info):
                changed_events += 1

    # Keep the physical-document registry consistent with row-level metadata.
    for registry_name in ("source_files", "sources", "canonical_documents"):
        registry = dataset.get(registry_name, {})
        values = registry.values() if isinstance(registry, dict) else registry
        for row in values:
            if not isinstance(row, dict):
                continue
            path = _source_path(row) or _source_file_path(row)
            info = dates.get(path, ("", "", ""))
            if apply_row_date(row, info):
                changed_events += 1

    report = {
        "method": "existing_metadata_then_filename_then_local_docx_metadata",
        "gemini_calls": 0,
        "source_documents_scanned": len(dates),
        "source_documents_with_date": sum(bool(value[0]) for value in dates.values()),
        "date_methods": dict(Counter(value[2] for value in dates.values() if value[0])),
        "changed_comments": changed_comments,
        "changed_responses": changed_responses,
        "changed_links": changed_links,
        "changed_events": changed_events,
        "exact_dates_on_comments_after": sum(bool(row.get("source_document_date")) for row in comments),
        "exact_dates_on_responses_after": sum(bool(row.get("source_document_date")) for row in responses),
        "quality_fields": {
            "comments": sum(all(field in row for field in ("transcription_confidence", "pairing_confidence", "date_confidence", "round_confidence", "role_confidence")) for row in comments),
            "responses": sum(all(field in row for field in ("transcription_confidence", "pairing_confidence", "date_confidence", "round_confidence", "role_confidence")) for row in responses),
        },
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dry_run": dry_run,
    }
    if not dry_run:
        backup = dataset_path.with_name(
            f"{dataset_path.stem}.pre-date-backfill-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
        )
        shutil.copy2(dataset_path, backup)
        dataset.setdefault("metadata", {})["timeline_date_backfill"] = report
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=dataset_path.parent,
            prefix=f"{dataset_path.stem}-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(dataset, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, dataset_path)
        report["backup_created"] = str(backup)
        try:
            from phase2.incremental_update import write_pipeline_checkpoint

            report["checkpoint"] = write_pipeline_checkpoint(
                dataset_path.parent,
                f"date-backfill-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                {"timeline_linked": "complete", "indexed": "complete"},
                repair="date_and_provenance_backfill",
                gemini_calls=0,
                changed_comments=changed_comments,
                changed_responses=changed_responses,
                changed_links=changed_links,
            )
        except Exception as exc:
            # A date repair must remain usable as a standalone script even if
            # the full incremental-ingestion dependencies are unavailable.
            report["checkpoint"] = {"status": "not_written", "error": f"{type(exc).__name__}: {exc}"[:300]}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=WORKSPACE / "phase2_dataset" / "dataset.json")
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(backfill(args.dataset, args.workspace, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
