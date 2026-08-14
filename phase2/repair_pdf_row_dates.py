#!/usr/bin/env python3
"""Backfill authoritative PDF same-row reviewer dates without Gemini.

The repair is deliberately additive. It fills only missing government-comment
event dates, associates that date with a paired response, and records the
visible same-row evidence. Existing dates, record IDs, text, relationships,
locators, source occurrence IDs, and timeline ordering remain unchanged.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from web_app.source_registry import (  # noqa: E402
    _pdf_page_layout,
    normalize_quote,
    pdf_same_row_context,
)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=path.stem + "-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _page(row: dict[str, Any]) -> int:
    value = row.get("source_page")
    if str(value).isdigit():
        return int(value)
    locator = row.get("source_locator_json")
    if isinstance(locator, dict):
        pages = locator.get("pages")
        if isinstance(pages, list) and pages and str(pages[0]).isdigit():
            return int(pages[0])
    return 0


def _text(row: dict[str, Any]) -> str:
    return str(
        row.get("verified_text")
        if row.get("text_trust_status") == "verified" and row.get("verified_text")
        else row.get("original_text") or ""
    ).strip()


def _path(row: dict[str, Any]) -> str:
    return str(row.get("source_document") or "").split(" | ", 1)[0].strip()


def _location(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "viewer_type": "pdf",
        "page": int(context.get("page") or 1),
        "pdf_bounding_boxes": copy.deepcopy(
            context.get("date_pdf_bounding_boxes") or []
        ),
        "source": "adjacent_reviewer_cell",
    }


def _context_is_authoritative(
    context: dict[str, Any], printed_comment_id: str,
) -> bool:
    if not context.get("event_date") or not context.get("reviewer"):
        return False
    if float(context.get("confidence") or 0) < 0.9:
        return False
    if printed_comment_id and not context.get("printed_comment_id_seen"):
        return False
    return True


def repair(
    dataset: dict[str, Any], workspace: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = copy.deepcopy(dataset)
    comments = result.get("comments", [])
    responses = result.get("responses", [])
    links = result.get("comment_response_links", [])
    response_by_comment = {
        str(row.get("comment_id") or ""): row
        for row in responses if row.get("comment_id")
    }
    links_by_comment: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        links_by_comment.setdefault(str(link.get("comment_id") or ""), []).append(link)

    page_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
    context_cache: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    repaired: dict[str, dict[str, Any]] = {}
    repaired_details: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    scanned = located = rejected = 0

    for comment in comments:
        relative = _path(comment)
        if not relative.casefold().endswith(".pdf"):
            continue
        page = _page(comment)
        quote = _text(comment)
        if page < 1 or len(quote) < 8:
            continue
        source_path = Path(relative)
        if not source_path.is_absolute():
            source_path = workspace / source_path
        if not source_path.is_file():
            continue
        scanned += 1
        page_key = (str(source_path), page)
        if page_key not in page_cache:
            page_cache[page_key] = _pdf_page_layout(source_path, page)
        printed = str(comment.get("comment_number") or "").strip()
        context_key = (str(source_path), page, normalize_quote(quote), printed)
        if context_key not in context_cache:
            context_cache[context_key] = pdf_same_row_context(
                source_path, page, quote, printed,
                page_layout=page_cache[page_key],
            )
        context = context_cache[context_key]
        if not context:
            continue
        located += 1
        if not _context_is_authoritative(context, printed):
            rejected += 1
            continue
        recovered = str(context["event_date"])
        existing = str(comment.get("event_date_iso") or comment.get("event_date") or "")
        if existing and existing != recovered:
            conflicts.append({
                "comment_id": comment.get("comment_id"),
                "existing": existing,
                "recovered": recovered,
                "source_document": relative,
                "page": page,
            })
            continue
        comment_id = str(comment.get("comment_id") or "")
        metadata = comment.get("source_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            comment["source_metadata"] = metadata
        metadata["pdf_same_row_context"] = copy.deepcopy(context)
        audit = comment.get("ingestion_audit")
        if isinstance(audit, dict):
            audit["pdf_same_row_context"] = copy.deepcopy(context)
        if not existing:
            comment.update({
                "event_date": recovered,
                "event_date_iso": recovered,
                "event_date_raw": str(context.get("event_date_raw") or ""),
                "event_date_source": str(context.get("event_date_source") or ""),
                "event_date_location": _location(context),
                "event_date_confidence": float(context.get("confidence") or 0),
                "date_confidence": float(context.get("confidence") or 0),
            })
        if not str(comment.get("reviewer") or "").strip():
            comment["reviewer"] = str(context.get("reviewer") or "")
        repaired[comment_id] = copy.deepcopy(context)
        repaired_details.append({
            "comment_id": comment_id,
            "source_document": relative,
            "page": page,
            "comment_number": printed,
            "event_date": recovered,
            "event_date_raw": context.get("event_date_raw", ""),
            "reviewer": context.get("reviewer", ""),
            "comment_preview": quote[:180],
        })

        response = response_by_comment.get(comment_id)
        if response is not None:
            response.update({
                "associated_comment_event_date": recovered,
                "associated_comment_event_date_raw": str(
                    context.get("event_date_raw") or ""
                ),
                "associated_comment_event_date_source": str(
                    context.get("event_date_source") or ""
                ),
                "associated_comment_event_date_location": _location(context),
                "source_row_context": copy.deepcopy(context),
            })
        for link in links_by_comment.get(comment_id, []):
            link.update({
                "associated_comment_event_date": recovered,
                "associated_comment_event_date_raw": str(
                    context.get("event_date_raw") or ""
                ),
                "associated_comment_event_date_source": str(
                    context.get("event_date_source") or ""
                ),
                "source_row_context": copy.deepcopy(context),
            })

    # Enrich existing timeline nodes in place. IDs, array order, aliases, and
    # grouping are deliberately untouched.
    index = result.get("issue_event_index")
    if isinstance(index, dict):
        for timeline in index.values():
            if not isinstance(timeline, dict):
                continue
            for event in timeline.get("events", []) or []:
                if not isinstance(event, dict):
                    continue
                comment_id = str(
                    event.get("linked_comment_id")
                    or event.get("parent_comment_id") or ""
                )
                context = repaired.get(comment_id)
                if (
                    context
                    and event.get("event_type") == "government_comment"
                    and not (event.get("event_date_iso") or event.get("event_date"))
                ):
                    event.update({
                        "event_date": context["event_date"],
                        "event_date_iso": context["event_date"],
                        "event_date_raw": context.get("event_date_raw", ""),
                        "event_date_source": context.get("event_date_source", ""),
                        "event_date_location": _location(context),
                    })
                for occurrence in event.get("source_occurrences", []) or []:
                    if not isinstance(occurrence, dict):
                        continue
                    occurrence_context = repaired.get(
                        str(occurrence.get("comment_id") or "")
                    )
                    if occurrence_context and not occurrence.get("event_date"):
                        occurrence["event_date"] = occurrence_context["event_date"]
                        occurrence["event_date_source"] = occurrence_context.get(
                            "event_date_source", ""
                        )

    history = result.setdefault("repair_history", [])
    history.append({
        "repair": "pdf_same_row_event_date_v1",
        "records_repaired": len(repaired),
        "conflicts": len(conflicts),
        "semantics": "additive_missing_dates_only",
    })
    report = {
        "pdf_comments_scanned": scanned,
        "row_context_located": located,
        "insufficient_context_rejected": rejected,
        "comments_repaired": len(repaired),
        "conflicts": conflicts,
        "page_layouts_processed": len(page_cache),
        "repaired_comment_ids": sorted(repaired),
        "repaired_records": repaired_details,
    }
    return result, report


def patch_registry(
    registry: dict[str, Any], dataset: dict[str, Any],
) -> int:
    comments = {
        str(row.get("comment_id") or ""): row
        for row in dataset.get("comments", [])
    }
    responses = {
        str(row.get("response_id") or ""): row
        for row in dataset.get("responses", [])
    }
    changed = 0
    for source in (registry.get("sources") or {}).values():
        if not isinstance(source, dict):
            continue
        owner = str(source.get("owner_id") or "")
        record = comments.get(owner) or responses.get(owner)
        if not record:
            continue
        context = (
            (record.get("source_metadata") or {}).get("pdf_same_row_context")
            if owner in comments and isinstance(record.get("source_metadata"), dict)
            else record.get("source_row_context")
        )
        if not isinstance(context, dict) or not context.get("event_date"):
            continue
        location = source.get("location")
        if not isinstance(location, dict) or location.get("original_document_type") != "pdf":
            continue
        metadata = location.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            location["metadata"] = metadata
        metadata["pdf_same_row_context"] = copy.deepcopy(context)
        if owner in comments:
            metadata["event_date"] = context["event_date"]
            metadata["event_date_source"] = context.get("event_date_source", "")
        else:
            metadata["associated_comment_event_date"] = context["event_date"]
        changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path,
        default=WORKSPACE / "phase2_dataset" / "dataset.json",
    )
    parser.add_argument(
        "--registry", type=Path,
        default=WORKSPACE / "web_app" / "data" / "source_registry.json",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    before = json.loads(args.dataset.read_text(encoding="utf-8"))
    repaired, report = repair(before, WORKSPACE)

    # Non-regression guard: this repair may add provenance but cannot alter
    # identities, verbatim text, pairings, locators, or timeline array order.
    for collection, id_key in (
        ("comments", "comment_id"), ("responses", "response_id"),
        ("comment_response_links", "link_id"),
    ):
        old_rows = before.get(collection, [])
        new_rows = repaired.get(collection, [])
        assert [row.get(id_key) for row in old_rows] == [row.get(id_key) for row in new_rows]
        immutable_fields = {
            "comments": (
                "comment_id", "response_id", "original_text",
                "source_locator_json", "source_occurrences",
            ),
            "responses": (
                "response_id", "comment_id", "original_text",
                "source_locator_json",
            ),
            "comment_response_links": (
                "link_id", "comment_id", "response_id",
                "comment_locator_json", "response_locator_json",
            ),
        }[collection]
        assert [
            tuple(row.get(field) for field in immutable_fields)
            for row in old_rows
        ] == [
            tuple(row.get(field) for field in immutable_fields)
            for row in new_rows
        ]
    assert [row.get("original_text") for row in before.get("comments", [])] == [
        row.get("original_text") for row in repaired.get("comments", [])
    ]
    old_index = before.get("issue_event_index", {})
    new_index = repaired.get("issue_event_index", {})
    assert list(old_index) == list(new_index)
    for key in old_index:
        assert [event.get("event_id") for event in old_index[key].get("events", [])] == [
            event.get("event_id") for event in new_index[key].get("events", [])
        ]

    registry_payload = None
    if args.registry.is_file():
        registry_payload = json.loads(args.registry.read_text(encoding="utf-8"))
        report["registry_sources_enriched"] = patch_registry(
            registry_payload, repaired
        )
    if args.apply:
        atomic_json(args.dataset, repaired)
        if registry_payload is not None:
            atomic_json(args.registry, registry_payload)
    if args.report:
        atomic_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
