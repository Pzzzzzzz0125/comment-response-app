#!/usr/bin/env python3
"""Attach explicit ProjectDox DISCUSSION history to existing spreadsheet rows.

This is a deterministic repair. It does not call Gemini, alter source text,
change verification decisions, or create new comment/response links.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from phase2 import extract_dataset as base
from phase2.spreadsheet_ingestion import parse_discussion_events
from phase2.visual_ingestion import atomic_json


def _group_lookup(
    artifact_root: Path,
    artifact_id: str,
) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, str]]:
    packet_path = (
        artifact_root / artifact_id / "spreadsheet_evidence_packet.json"
    )
    schema_path = artifact_root / artifact_id / "spreadsheet_schema.json"
    if not packet_path.is_file() or not schema_path.is_file():
        return {}, {}
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    schemas = json.loads(schema_path.read_text(encoding="utf-8"))
    discussion_columns = {
        str(schema.get("sheet_name", "")): str(
            schema.get("discussion_column", "")
        ).upper()
        for schema in schemas
        if isinstance(schema, dict)
        and str(schema.get("discussion_column", "")).strip()
    }
    groups = {
        (
            str(group.get("sheet_name", "")),
            int(group.get("row_number") or 0),
        ): group
        for group in packet.get("groups", [])
        if isinstance(group, dict)
    }
    return groups, discussion_columns


def repair(
    dataset: dict[str, Any],
    artifact_root: Path,
) -> dict[str, int]:
    cache: dict[
        str,
        tuple[
            dict[tuple[str, int], dict[str, Any]],
            dict[str, str],
        ],
    ] = {}
    updated = 0
    events_added = 0
    threads_with_history = 0
    for comment in dataset.get("comments", []):
        if (
            comment.get("extraction_method")
            != "local_structured_spreadsheet"
        ):
            continue
        audit = comment.get("ingestion_audit") or {}
        artifact_id = str(audit.get("artifact_id", ""))
        if not artifact_id:
            continue
        if artifact_id not in cache:
            cache[artifact_id] = _group_lookup(
                artifact_root, artifact_id,
            )
        groups, discussion_columns = cache[artifact_id]
        sheet = str(comment.get("source_sheet", ""))
        try:
            row_number = int(comment.get("source_row") or 0)
        except (TypeError, ValueError):
            continue
        group = groups.get((sheet, row_number), {})
        discussion_column = discussion_columns.get(sheet, "")
        discussion_unit = next((
            unit for unit in group.get("units", [])
            if isinstance(unit, dict)
            and str(unit.get("column", "")).upper()
            == discussion_column
        ), None)
        exact_text = str(
            (discussion_unit or {}).get("text", "")
        )
        cell = str(
            (discussion_unit or {}).get("cell")
            or (
                f"{discussion_column}{row_number}"
                if discussion_column else ""
            )
        )
        location = ({
            "viewer_type": "spreadsheet",
            "sheet_name": sheet,
            "cell_range": cell,
            "row_number": row_number,
            "unit_ids": [str(
                (discussion_unit or {}).get("unit_id", "")
            )],
            "description": "comment-response discussion history cell",
        } if exact_text.strip() and cell else {})
        events = (
            parse_discussion_events(exact_text, location)
            if location else []
        )
        issue_anchor = str(
            comment.get("normalized_comment_text")
            or comment.get("original_text", "")
        ).casefold()
        issue_anchor = " ".join(issue_anchor.split())
        thread_id = base.stable_id(
            "T",
            str(comment.get("city", "")).casefold(),
            str(comment.get("property_project", "")).casefold(),
            str(comment.get("discipline", "")).casefold(),
            issue_anchor,
        )
        for index, event in enumerate(events, 1):
            event.update({
                "event_id": base.stable_id(
                    "E", thread_id, "discussion", str(index),
                ),
                "issue_thread_id": thread_id,
                "source_document": str(
                    comment.get("source_document", "")
                ),
                "source_locator_json": location,
            })
        status_unit = next((
            unit for unit in group.get("units", [])
            if isinstance(unit, dict)
            and str(unit.get("column", "")).upper() == "H"
        ), None)
        comment.update({
            "issue_thread_id": thread_id,
            "issue_grouping_status": (
                "explicit" if events else "deterministic_exact"
            ),
            "issue_grouping_method": (
                "same_spreadsheet_row_with_history"
                if events else "exact_site_discipline_comment"
            ),
            "issue_status": str(
                (status_unit or {}).get("text", "")
            ),
            "discussion_raw_text": exact_text,
            "discussion_source_locator_json": location,
            "issue_thread_events": events,
        })
        updated += 1
        events_added += len(events)
        threads_with_history += bool(events)
    return {
        "comments_updated": updated,
        "discussion_events_added": events_added,
        "threads_with_history": threads_with_history,
    }


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=workspace / "phase2_dataset" / "dataset.json",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=workspace / "phase2_dataset" / "ingestion_artifacts",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    result = repair(dataset, args.artifact_root)
    if args.apply and result["comments_updated"]:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = args.dataset.with_name(
            f"{args.dataset.stem}.pre-issue-thread-repair-{stamp}.json"
        )
        atomic_json(backup, json.loads(
            args.dataset.read_text(encoding="utf-8")
        ))
        metadata = dataset.setdefault("metadata", {})
        metadata["spreadsheet_issue_thread_repair"] = {
            **result,
            "applied_at": dt.datetime.now(
                dt.timezone.utc
            ).isoformat(),
            "method": "deterministic_projectdox_discussion_parser",
        }
        atomic_json(args.dataset, dataset)
        result["backup_created"] = str(backup)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
