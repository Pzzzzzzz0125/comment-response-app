"""Deterministic-first spreadsheet ingestion primitives.

The spreadsheet cell model is authoritative for stable tabular formats.
Gemini verifies compact row/unit relationships instead of retranscribing an
entire workbook from repeated preview images.
"""

from __future__ import annotations

import copy
import html
import re
from datetime import datetime
from typing import Any


SPREADSHEET_PIPELINE_VERSION = "structured-spreadsheet-v2"
SPREADSHEET_VERIFICATION_PROMPT_VERSION = "spreadsheet-unit-verification-v1"

PROJECTDOX_HEADERS = {
    "A": "ref #",
    "B": "reviewed by",
    "C": "type",
    "D": "view",
    "E": "enter your comment response here",
    "F": "discussion",
    "G": "cycle",
    "H": "status",
}

SPREADSHEET_VERIFICATION_INSTRUCTION = """You are independently verifying a
locally parsed permit-review spreadsheet. The supplied cell values and cell
addresses come directly from the XLSX XML and are authoritative. Do not
rewrite, summarize, correct, or return their text.

Verify the proposed template and row groups:
- every candidate government-comment row is represented exactly once;
- the comment unit belongs to the configured comment column;
- a response is linked only when its response unit is in the same visible row;
- discussion/history cells remain attached to their visible row as prior
  applicant/reviewer events, but are never treated as the current response;
- group IDs and unit IDs are complete and unique;
- no context-only unit is treated as a record.

Return IDs and errors only. Never return full comment or response text."""

SPREADSHEET_VERIFICATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "document_verified": {"type": "BOOLEAN"},
        "template_verified": {"type": "BOOLEAN"},
        "every_candidate_assigned": {"type": "BOOLEAN"},
        "same_row_links_correct": {"type": "BOOLEAN"},
        "verified_group_ids": {
            "type": "ARRAY", "items": {"type": "STRING"},
        },
        "rejected_group_ids": {
            "type": "ARRAY", "items": {"type": "STRING"},
        },
        "missing_unit_ids": {
            "type": "ARRAY", "items": {"type": "STRING"},
        },
        "incorrect_groupings": {
            "type": "ARRAY", "items": {"type": "STRING"},
        },
        "incorrect_links": {
            "type": "ARRAY", "items": {"type": "STRING"},
        },
        "verification_summary": {"type": "STRING"},
    },
    "required": [
        "document_verified", "template_verified",
        "every_candidate_assigned", "same_row_links_correct",
        "verified_group_ids", "rejected_group_ids", "missing_unit_ids",
        "incorrect_groupings", "incorrect_links", "verification_summary",
    ],
}


def normalized_visible_text(value: Any) -> str:
    """Normalize only retrieval/display text; source cell text stays immutable."""
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalized_identifier(value: Any) -> str:
    text = normalized_visible_text(value)
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


DISCUSSION_EVENT_HEADER = re.compile(
    r"(?im)^[ \t]*(Reviewer Response|Responded by)[ \t]*:"
    r"[ \t]*(.*?)[ \t]*-[ \t]*"
    r"(\d{1,2}/\d{1,2}/\d{2,4}"
    r"(?:[ \t]+\d{1,2}:\d{2}(?:[ \t]*[AP]M)?)?)[ \t]*$"
)
DISCUSSION_SEPARATOR = re.compile(r"(?m)^[ \t]*-{8,}[ \t]*$")


def _discussion_timestamp(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip().upper()
    for pattern in (
        "%m/%d/%y %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%y %H:%M",
        "%m/%d/%Y %H:%M",
        "%m/%d/%y",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(compact, pattern).isoformat(
                timespec="minutes"
            )
        except ValueError:
            continue
    return ""


def parse_discussion_events(
    exact_text: str,
    location: dict[str, Any],
) -> list[dict[str, Any]]:
    """Parse explicit ProjectDox history without rewriting its body text.

    ProjectDox writes newest-first history into DISCUSSION.  The returned
    events carry parsed dates so the UI can present the issue chronologically,
    while ``raw_segment`` retains the complete source segment for audit.
    """
    if not exact_text.strip():
        return []
    matches = list(DISCUSSION_EVENT_HEADER.finditer(exact_text))
    if not matches:
        body = DISCUSSION_SEPARATOR.sub("", exact_text).strip()
        return [{
            "event_type": "discussion_note",
            "actor_role": "unknown",
            "actor": "",
            "occurred_at": "",
            "occurred_at_label": "",
            "exact_text": body,
            "raw_segment": exact_text,
            "source_order": 1,
            "source_location": copy.deepcopy(location),
            "parse_status": "unstructured",
        }] if body else []
    events: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(
            exact_text
        )
        raw_segment = exact_text[match.start():end]
        body = exact_text[match.end():end]
        body = DISCUSSION_SEPARATOR.sub("", body).strip()
        label = match.group(1).casefold()
        events.append({
            "event_type": (
                "reviewer_follow_up"
                if label == "reviewer response"
                else "applicant_response"
            ),
            "actor_role": (
                "government"
                if label == "reviewer response"
                else "company"
            ),
            "actor": normalized_visible_text(match.group(2)),
            "occurred_at": _discussion_timestamp(match.group(3)),
            "occurred_at_label": normalized_visible_text(match.group(3)),
            "exact_text": body,
            "raw_segment": raw_segment.strip(),
            "source_order": index + 1,
            "source_location": copy.deepcopy(location),
            "parse_status": "explicit_header",
        })
    return events


def _column_letters(number: int) -> str:
    value = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _column_number(value: str) -> int:
    result = 0
    for character in value.upper():
        if not "A" <= character <= "Z":
            return 0
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _column_is_hidden(sheet: dict[str, Any], column: str) -> bool:
    number = _column_number(column)
    return bool(number and any(
        int(item.get("min") or 0) <= number <= int(item.get("max") or 0)
        for item in sheet.get("hidden_columns", [])
        if isinstance(item, dict)
    ))


def _tabular_raw(raw_text: dict[str, Any]) -> dict[str, Any]:
    if raw_text.get("kind") != "csv_cells":
        return raw_text
    rows = []
    for row in raw_text.get("rows", []):
        if not isinstance(row, dict):
            continue
        row_number = int(row.get("row_number") or len(rows) + 1)
        rows.append({
            "row_number": row_number,
            "hidden": False,
            "cells": [{
                "column": _column_letters(index),
                "address": f"{_column_letters(index)}{row_number}",
                "value": str(value),
                "display_value": str(value),
                "raw_value": str(value),
                "cell_type": "csv",
            } for index, value in enumerate(
                row.get("values", []), 1,
            )],
        })
    return {
        "kind": "xlsx_cells",
        "sheets": [{
            "name": "CSV",
            "rows": rows,
            "merged_ranges": [],
            "hidden_rows": [],
            "hidden_columns": [],
            "has_drawing_objects": False,
        }],
    }


def _cell_map(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(cell.get("column", "")).upper(): cell
        for cell in row.get("cells", [])
        if isinstance(cell, dict) and str(cell.get("column", "")).strip()
    }


def _cell_value(cells: dict[str, dict[str, Any]], column: str) -> str:
    return str(cells.get(column, {}).get("value", "") or "")


def _unit_id(sheet: str, address: str) -> str:
    return f"XLSX:{sheet}:{address}"


def detect_spreadsheet_schemas(
    raw_text: dict[str, Any],
) -> list[dict[str, Any]]:
    """Detect conservative deterministic templates from direct cell structure."""
    raw_text = _tabular_raw(raw_text)
    if raw_text.get("kind") != "xlsx_cells":
        return []
    schemas: list[dict[str, Any]] = []
    for sheet in raw_text.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        rows = [row for row in sheet.get("rows", []) if isinstance(row, dict)]
        for row in rows[:10]:
            cells = _cell_map(row)
            headers = {
                column: normalized_visible_text(cell.get("value")).casefold()
                for column, cell in cells.items()
            }
            if all(headers.get(column) == label for column, label in PROJECTDOX_HEADERS.items()):
                schemas.append({
                    "template_id": "projectdox_review_comments_v1",
                    "mode": "deterministic",
                    "confidence": 1.0,
                    "sheet_name": str(sheet.get("name", "")),
                    "header_row": int(row.get("row_number") or 1),
                    "ref_column": "A",
                    "reviewer_column": "B",
                    "comment_column": "C",
                    "view_column": "D",
                    "response_column": "E",
                    "discussion_column": "F",
                    "cycle_column": "G",
                    "status_column": "H",
                    "same_row_pairing": True,
                    # ProjectDox exports commonly contain hyperlink/drawing
                    # relationships that do not carry comment text. Direct
                    # C/E cells remain authoritative; merged cell structure is
                    # the anomaly that requires targeted visual review.
                    "requires_visual": bool(
                        sheet.get("merged_ranges")
                        or _column_is_hidden(sheet, "C")
                        or _column_is_hidden(sheet, "E")
                    ),
                    "drawing_objects_ignored_as_non_text_links": bool(
                        sheet.get("has_drawing_objects")
                    ),
                })
                break
            explicit_comment = [
                column for column, label in headers.items()
                if label in {
                    "government comment", "city comment", "review comment",
                    "comment", "correction", "requirement",
                }
            ]
            explicit_response = [
                column for column, label in headers.items()
                if label in {
                    "applicant response", "company response", "response",
                    "resolution",
                }
            ]
            if len(explicit_comment) == 1 and len(explicit_response) <= 1:
                schemas.append({
                    "template_id": "generic_comment_response_table_v1",
                    "mode": "deterministic",
                    "confidence": 0.98,
                    "sheet_name": str(sheet.get("name", "")),
                    "header_row": int(row.get("row_number") or 1),
                    "ref_column": next((
                        column for column, label in headers.items()
                        if label in {"ref #", "comment #", "comment number", "id"}
                    ), ""),
                    "reviewer_column": next((
                        column for column, label in headers.items()
                        if label in {"reviewed by", "reviewer", "department"}
                    ), ""),
                    "comment_column": explicit_comment[0],
                    "view_column": "",
                    "response_column": explicit_response[0] if explicit_response else "",
                    "discussion_column": "",
                    "cycle_column": next((
                        column for column, label in headers.items()
                        if label in {"cycle", "round", "review round"}
                    ), ""),
                    "status_column": next((
                        column for column, label in headers.items()
                        if label == "status"
                    ), ""),
                    "same_row_pairing": True,
                    "requires_visual": bool(
                        sheet.get("merged_ranges")
                        or sheet.get("has_drawing_objects")
                        or _column_is_hidden(sheet, explicit_comment[0])
                        or (
                            explicit_response
                            and _column_is_hidden(
                                sheet, explicit_response[0],
                            )
                        )
                    ),
                })
                break
    return schemas


def _department(reviewer_context: str) -> str:
    value = normalized_visible_text(reviewer_context)
    date_match = re.search(
        r"\s+\d{1,2}/\d{1,2}/\d{2,4}\b", value,
    )
    prefix = value[:date_match.start()] if date_match else value
    review_match = re.match(
        r"(.+?\b(?:Review|Conformance|Division|Department))\b",
        prefix,
        re.IGNORECASE,
    )
    return (
        normalized_visible_text(review_match.group(1))
        if review_match else "unknown"
    )


def build_spreadsheet_evidence(
    raw_text: dict[str, Any],
    schemas: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Build exclusive row groups and exact-text extraction from cell units."""
    raw_text = _tabular_raw(raw_text)
    sheets = {
        str(sheet.get("name", "")): sheet
        for sheet in raw_text.get("sheets", [])
        if isinstance(sheet, dict)
    }
    groups: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    candidate_unit_ids: list[str] = []
    assigned_unit_ids: list[str] = []
    context_unit_ids: list[str] = []
    unassigned_unit_ids: list[str] = []
    needs_review_unit_ids: list[str] = []
    header_units: list[dict[str, Any]] = []
    requires_visual = False

    for schema in schemas:
        sheet_name = str(schema.get("sheet_name", ""))
        sheet = sheets.get(sheet_name, {})
        rows = [row for row in sheet.get("rows", []) if isinstance(row, dict)]
        header_row = int(schema.get("header_row") or 1)
        comment_column = str(schema.get("comment_column", "")).upper()
        response_column = str(schema.get("response_column", "")).upper()
        requires_visual = requires_visual or bool(schema.get("requires_visual"))
        for row in rows:
            row_number = int(row.get("row_number") or 0)
            cells = _cell_map(row)
            if row_number == header_row:
                header_units.extend({
                    "unit_id": _unit_id(sheet_name, str(cell.get("address", ""))),
                    "cell": str(cell.get("address", "")),
                    "text": str(cell.get("value", "") or ""),
                } for cell in cells.values() if str(cell.get("value", "")).strip())
                continue
            if row_number <= header_row:
                continue
            comment_cell = cells.get(comment_column, {})
            response_cell = cells.get(response_column, {}) if response_column else {}
            comment_text = str(comment_cell.get("value", "") or "")
            response_text = str(response_cell.get("value", "") or "")
            comment_address = str(
                comment_cell.get("address") or f"{comment_column}{row_number}"
            )
            response_address = str(
                response_cell.get("address") or (
                    f"{response_column}{row_number}" if response_column else ""
                )
            )
            if not comment_text.strip():
                if response_text.strip() and response_address:
                    unassigned_unit_ids.append(
                        _unit_id(sheet_name, response_address)
                    )
                continue

            comment_unit_id = _unit_id(sheet_name, comment_address)
            response_unit_id = (
                _unit_id(sheet_name, response_address)
                if response_text.strip() and response_address else ""
            )
            group_id = f"XLSX:{sheet_name}:ROW:{row_number}"
            if row.get("hidden") is True:
                needs_review_unit_ids.append(comment_unit_id)
                requires_visual = True
            if comment_cell.get("formula"):
                needs_review_unit_ids.append(comment_unit_id)
                requires_visual = True
            if response_unit_id and response_cell.get("formula"):
                needs_review_unit_ids.append(response_unit_id)
                requires_visual = True
            row_units = []
            for column, cell in sorted(cells.items()):
                value = str(cell.get("value", "") or "")
                if not value.strip():
                    continue
                address = str(cell.get("address") or f"{column}{row_number}")
                unit = {
                    "unit_id": _unit_id(sheet_name, address),
                    "cell": address,
                    "column": column,
                    "text": value,
                    "role": (
                        "comment" if column == comment_column
                        else "response" if column == response_column
                        else "context"
                    ),
                }
                row_units.append(unit)
                if unit["role"] == "context":
                    context_unit_ids.append(unit["unit_id"])
            candidate_unit_ids.append(comment_unit_id)
            assigned_unit_ids.append(comment_unit_id)
            if response_unit_id:
                candidate_unit_ids.append(response_unit_id)
                assigned_unit_ids.append(response_unit_id)

            ref = _cell_value(cells, str(schema.get("ref_column", "")).upper())
            reviewer = _cell_value(
                cells, str(schema.get("reviewer_column", "")).upper(),
            )
            cycle = _cell_value(
                cells, str(schema.get("cycle_column", "")).upper(),
            )
            status = _cell_value(
                cells, str(schema.get("status_column", "")).upper(),
            )
            view_cell = cells.get(
                str(schema.get("view_column", "")).upper(), {},
            )
            discussion_cell = cells.get(
                str(schema.get("discussion_column", "")).upper(), {},
            )
            comment_location = {
                "viewer_type": "spreadsheet",
                "sheet_name": sheet_name,
                "cell_range": comment_address,
                "row_number": row_number,
                "unit_ids": [comment_unit_id],
                "description": "government comment cell",
            }
            response_location = ({
                "viewer_type": "spreadsheet",
                "sheet_name": sheet_name,
                "cell_range": response_address,
                "row_number": row_number,
                "unit_ids": [response_unit_id],
                "description": "company response cell",
            } if response_unit_id else {})
            discussion_text = str(
                discussion_cell.get("value", "") or ""
            )
            discussion_address = str(
                discussion_cell.get("address")
                or (
                    f"{str(schema.get('discussion_column', '')).upper()}"
                    f"{row_number}"
                    if schema.get("discussion_column") else ""
                )
            )
            discussion_location = ({
                "viewer_type": "spreadsheet",
                "sheet_name": sheet_name,
                "cell_range": discussion_address,
                "row_number": row_number,
                "unit_ids": [
                    _unit_id(sheet_name, discussion_address),
                ],
                "description": "comment-response discussion history cell",
            } if discussion_text.strip() and discussion_address else {})
            discussion_events = parse_discussion_events(
                discussion_text,
                discussion_location,
            )
            records.append({
                "record_key": group_id,
                "comment_id": normalized_identifier(ref) or f"row-{row_number}",
                "comment_number": normalized_identifier(ref) or f"row-{row_number}",
                "review_round": normalized_identifier(cycle) or str(
                    context.get("review_round_hint", "")
                ),
                "page": 0,
                "exact_comment_text": comment_text,
                "normalized_comment_text": normalized_visible_text(
                    comment_text
                ),
                "department": _department(reviewer),
                "reviewer": reviewer,
                "exact_response_text": response_text,
                "comment_location": comment_location,
                "response_location": response_location,
                "exact_discussion_text": discussion_text,
                "discussion_location": discussion_location,
                "discussion_events": discussion_events,
                "same_visible_row": bool(response_unit_id),
                "explicit_shared_comment_id": False,
                "pairing_evidence": (
                    f"Direct XLSX same-row cells {comment_address} and "
                    f"{response_address}"
                    if response_unit_id
                    else "Direct XLSX row contains no current response cell"
                ),
                "confidence": 1.0,
                "uncertain": False,
                "uncertainty_reason": "",
                "comment_unit_ids": [comment_unit_id],
                "response_unit_ids": (
                    [response_unit_id] if response_unit_id else []
                ),
                "source_metadata": {
                    "raw_reference_value": ref,
                    "raw_cycle_value": cycle,
                    "status": status,
                    "view_cell": str(view_cell.get("address", "")),
                    "view_value": str(view_cell.get("value", "") or ""),
                    "discussion_cell": str(
                        discussion_cell.get("address", "")
                    ),
                    "discussion_event_count": len(discussion_events),
                },
                "extraction_method": "local_structured_spreadsheet",
            })
            groups.append({
                "group_id": group_id,
                "sheet_name": sheet_name,
                "row_number": row_number,
                "comment_unit_id": comment_unit_id,
                "response_unit_id": response_unit_id,
                "units": row_units,
                "relationship": (
                    "same_visible_row" if response_unit_id else "comment_only"
                ),
            })

    duplicate_core_units = sorted({
        unit_id for unit_id in assigned_unit_ids
        if assigned_unit_ids.count(unit_id) > 1
    })
    unresolved = (
        len(set(unassigned_unit_ids))
        + len(duplicate_core_units)
        + len(set(needs_review_unit_ids))
    )
    completeness = {
        "candidate_unit_ids": candidate_unit_ids,
        "assigned_unit_ids": assigned_unit_ids,
        "context_only_unit_ids": sorted(set(context_unit_ids)),
        "duplicate_unit_ids": duplicate_core_units,
        "needs_review_unit_ids": sorted(set(
            [*unassigned_unit_ids, *needs_review_unit_ids]
        )),
        "unassigned_unit_ids": sorted(set(unassigned_unit_ids)),
        "candidate_comment_count": len(records),
        "candidate_response_count": sum(
            bool(row.get("exact_response_text")) for row in records
        ),
        "assigned_group_count": len(groups),
        "unresolved_signal_count": unresolved,
        "completion_status": (
            "needs_review" if unresolved or requires_visual else "complete"
        ),
        "requires_visual": requires_visual,
    }
    extraction = {
        "property": str(context.get("property_hint", "")),
        "city": str(context.get("city_hint", "")),
        "review_round": str(context.get("review_round_hint", "")),
        "document_type": "structured_spreadsheet",
        "document_uncertain": bool(unresolved or requires_visual),
        "document_uncertainty_reason": (
            "Spreadsheet contains unresolved or visual-only structures"
            if unresolved or requires_visual else ""
        ),
        "records": records,
        "structured_comment_count": len(records),
        "structured_response_count": completeness[
            "candidate_response_count"
        ],
        "comment_number_scope": "sheet_row",
        "extraction_method": "local_structured_spreadsheet",
        "spreadsheet_pipeline_version": SPREADSHEET_PIPELINE_VERSION,
    }
    packet = {
        "packet_version": SPREADSHEET_PIPELINE_VERSION,
        "schemas": copy.deepcopy(schemas),
        "header_units": header_units,
        "groups": groups,
        "completeness_manifest": completeness,
        "instruction": (
            "Verify unit ownership and same-row relationships. "
            "Do not return or rewrite cell text."
        ),
    }
    return {
        "packet": packet,
        "extraction": extraction,
        "completeness": completeness,
    }


def local_verification_result(
    evidence: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Translate compact group verification into the existing verified schema."""
    groups = evidence["packet"].get("groups", [])
    expected_ids = {
        str(group.get("group_id", "")) for group in groups
        if str(group.get("group_id", ""))
    }
    verified_ids = {
        str(value) for value in result.get("verified_group_ids", [])
        if str(value)
    }
    rejected_ids = {
        str(value) for value in result.get("rejected_group_ids", [])
        if str(value)
    }
    errors = (
        list(result.get("missing_unit_ids", []) or [])
        + list(result.get("incorrect_groupings", []) or [])
        + list(result.get("incorrect_links", []) or [])
    )
    complete = all(result.get(field) is True for field in (
        "document_verified", "template_verified",
        "every_candidate_assigned", "same_row_links_correct",
    ))
    complete = (
        complete
        and not errors
        and not rejected_ids
        and verified_ids == expected_ids
        and evidence["completeness"]["completion_status"] == "complete"
    )
    checks = []
    for group in groups:
        group_id = str(group.get("group_id", ""))
        verified = complete and group_id in verified_ids
        checks.append({
            "record_key": group_id,
            "comment_captured": verified,
            "response_captured": verified,
            "text_complete_and_verbatim": verified,
            "pairing_correct": verified,
            "locations_and_boxes_correct": verified,
            "same_visible_row_or_shared_id": verified,
            "verified": verified,
            "uncertainty_reason": (
                "" if verified else str(
                    result.get("verification_summary", "")
                    or "Spreadsheet unit verification failed"
                )
            ),
        })
    return {
        "document_verified": complete,
        "every_comment_captured": complete,
        "every_response_captured": complete,
        "verification_summary": str(
            result.get("verification_summary", "")
        ),
        "records": checks,
        "rejected_record_ids": sorted(rejected_ids),
        "missing_visible_comments": list(
            result.get("missing_unit_ids", []) or []
        ),
        "missing_visible_responses": [],
        "incorrect_links": list(result.get("incorrect_links", []) or []),
        "incorrect_page_locations": list(
            result.get("incorrect_groupings", []) or []
        ),
        "duplicate_fragments": [],
        "continuation_errors": [],
    }
