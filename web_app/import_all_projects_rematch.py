#!/usr/bin/env python3
"""Idempotently import the verified all-project paired and unpaired workbook."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .import_rematched_workbook import workbook_rows
    from .source_registry import sha256_file
    from .comment_dedup import mark_duplicate_comments
    from .comment_hierarchy import merge_docx_comment_hierarchy
    from .source_lineage import mark_copied_source_documents
    from .document_identity import canonicalize_documents
except ImportError:
    from import_rematched_workbook import workbook_rows
    from source_registry import sha256_file
    from comment_dedup import mark_duplicate_comments
    from comment_hierarchy import merge_docx_comment_hierarchy
    from source_lineage import mark_copied_source_documents
    from document_identity import canonicalize_documents


PROVENANCE = "all_projects_verified_rematch"


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def relative_source(path: Path, workspace: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def source_file(source_root: Path, filename: str) -> Path:
    wanted = normalized(filename)
    matches = [path for path in source_root.rglob("*") if path.is_file() and normalized(path.name) == wanted]
    if len(matches) != 1:
        raise ValueError(f"Expected one source file named {filename!r}; found {len(matches)}")
    return matches[0]


def locator_fields(locator: str) -> dict[str, Any]:
    page = re.search(r"\bpage\s+(\d+)\b", locator, re.I)
    paragraph = re.search(r"\bparagraph\s+(\d+)\b", locator, re.I)
    sheet_row = re.search(r"\bsheet\s+(.+?),\s*row\s+(\d+)\b", locator, re.I)
    result: dict[str, Any] = {"source_location": locator}
    if page:
        result.update({"source_page": int(page.group(1)), "source_page_end": int(page.group(1))})
    if paragraph:
        result.update({"source_row": int(paragraph.group(1)), "paragraph_index": int(paragraph.group(1))})
    if sheet_row:
        result.update({"source_sheet": sheet_row.group(1).strip(), "source_row": int(sheet_row.group(2))})
    return result


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix="all-projects-rematch-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def import_verified_workbook(
    workbook: Path, dataset_path: Path, source_root: Path, *, apply: bool = False,
) -> dict[str, Any]:
    pairs = workbook_rows(workbook, "Pairs to Import")
    new_responses = workbook_rows(workbook, "New Responses")
    no_response = workbook_rows(workbook, "No Source Response")
    report: dict[str, Any] = {
        "workbook": workbook.name, "workbook_sha256": sha256_file(workbook), "applied": False,
        "paired_rows": len(pairs), "unpaired_rows": len(no_response), "new_response_definitions": len(new_responses),
        "responses_inserted": 0, "links_created": 0, "links_confirmed": 0,
        "unpaired_verified": 0, "comments_verified": 0, "skipped": 0, "conflicts": [],
        "duplicate_rows_suppressed": 0, "duplicate_groups": 0,
        "hierarchy_groups_merged": 0, "hierarchy_children_suppressed": 0,
    }
    pair_ids = [row.get("Comment ID", "") for row in pairs]
    no_ids = [row.get("Comment ID", "") for row in no_response]
    if len(pairs) != 74 or len(no_response) != 181 or len(new_responses) != 11:
        report["conflicts"].append("Expected 74 paired rows, 181 unpaired rows, and 11 response definitions")
        return report
    if len(set(pair_ids + no_ids)) != 255 or set(pair_ids) & set(no_ids):
        report["conflicts"].append("Workbook comment IDs must be 255 unique, disjoint records")
        return report

    original = json.loads(dataset_path.read_text(encoding="utf-8"))
    candidate = copy.deepcopy(original)
    comments = {str(row.get("comment_id", "")): row for row in candidate.get("comments", [])}
    responses = {str(row.get("response_id", "")): row for row in candidate.get("responses", [])}
    links = {str(row.get("comment_id", "")): row for row in candidate.get("comment_response_links", [])}
    rematch_ids = {
        str(row.get("comment_id", "")) for row in candidate.get("comment_response_links", [])
        if row.get("provenance") == "document_structure_rematch"
    }
    if set(pair_ids + no_ids) != set(comments) - rematch_ids:
        report["conflicts"].append("Workbook does not exactly cover the 255 non-PC3/PC4/PC5 comments")
        return report

    new_by_id = {str(row.get("Response ID", "")): row for row in new_responses}
    if len(new_by_id) != 11 or any(not response_id for response_id in new_by_id):
        report["conflicts"].append("New Responses contains blank or duplicate Response IDs")
        return report
    used_new = {str(row.get("Response ID", "")) for row in pairs if row.get("Import Action") == "ADD_RESPONSE_AND_LINK"}
    if used_new != set(new_by_id):
        report["conflicts"].append("Every new response must be used by at least one ADD_RESPONSE_AND_LINK row")
        return report

    workspace = dataset_path.resolve().parents[1]
    response_paths: dict[str, Path] = {}
    for response_id, row in new_by_id.items():
        try:
            response_paths[response_id] = source_file(source_root, row.get("Source File", ""))
        except ValueError as exc:
            report["conflicts"].append(str(exc))
    for row in pairs + no_response:
        comment = comments.get(str(row.get("Comment ID", "")))
        if not comment:
            report["conflicts"].append(f"Unknown Comment ID {row.get('Comment ID')}")
            continue
        expected_text = row.get("Government Comment", "")
        if normalized(comment.get("original_text")) != normalized(expected_text):
            report["conflicts"].append(f"{comment['comment_id']}: government comment text conflict")
        expected_file = row.get("Comment Source File") or row.get("Source File", "")
        if normalized(Path(str(comment.get("source_document", ""))).name) != normalized(expected_file):
            report["conflicts"].append(f"{comment['comment_id']}: comment source file conflict")
    for row in no_response:
        link = links.get(str(row.get("Comment ID", "")), {})
        if link.get("response_id"):
            report["conflicts"].append(f"{row.get('Comment ID')}: workbook says unpaired but data pool has a response")
    for row in pairs:
        comment_id = str(row.get("Comment ID", ""))
        response_id = str(row.get("Response ID", ""))
        link = links.get(comment_id)
        if link is None:
            report["conflicts"].append(f"{comment_id}: link row is missing")
            continue
        existing_response = str(link.get("response_id", ""))
        action = row.get("Import Action", "")
        if action == "KEEP_EXISTING_LINK" and existing_response != response_id:
            report["conflicts"].append(f"{comment_id}: existing response differs from verified workbook")
        elif action == "ADD_RESPONSE_AND_LINK" and existing_response not in {"", response_id}:
            report["conflicts"].append(f"{comment_id}: conflicting response link already exists")
        elif action not in {"KEEP_EXISTING_LINK", "ADD_RESPONSE_AND_LINK"}:
            report["conflicts"].append(f"{comment_id}: unsupported Import Action {action!r}")
        response = responses.get(response_id)
        definition = new_by_id.get(response_id)
        expected_text = row.get("Company Response", "")
        if response and normalized(response.get("original_text")) != normalized(expected_text):
            report["conflicts"].append(f"{response_id}: existing response text conflict")
        if response and normalized(Path(str(response.get("source_document", ""))).name) != normalized(row.get("Response Source File", "")):
            report["conflicts"].append(f"{response_id}: existing response source file conflict")
        if response and normalized(response.get("source_location")).casefold() != normalized(row.get("Response Locator", "")).casefold():
            report["conflicts"].append(f"{response_id}: existing response locator conflict")
        if not response and not definition:
            report["conflicts"].append(f"{response_id}: response is neither existing nor defined as new")
        if definition and normalized(definition.get("Exact Response Text")) != normalized(expected_text):
            report["conflicts"].append(f"{response_id}: pair and response-definition text differ")
        if definition and normalized(definition.get("Source File")) != normalized(row.get("Response Source File", "")):
            report["conflicts"].append(f"{response_id}: pair and response-definition source differ")
        if definition and normalized(definition.get("Source Locator")).casefold() != normalized(row.get("Response Locator", "")).casefold():
            report["conflicts"].append(f"{response_id}: pair and response-definition locator differ")
    if report["conflicts"]:
        return report

    workbook_hash = report["workbook_sha256"]
    response_comment_ids: dict[str, list[str]] = defaultdict(list)
    for row in pairs:
        response_comment_ids[str(row["Response ID"])].append(str(row["Comment ID"]))
    for response_id, row in new_by_id.items():
        existing = responses.get(response_id)
        path = response_paths[response_id]
        loc = locator_fields(row.get("Source Locator", ""))
        record = {
            "response_id": response_id,
            "comment_id": response_comment_ids[response_id][0],
            "comment_ids": response_comment_ids[response_id],
            "original_text": row.get("Exact Response Text", ""),
            "verified_text": row.get("Exact Response Text", ""),
            "source_document": relative_source(path, workspace),
            "source_sha256": sha256_file(path),
            **loc,
            "source_locator_json": {key: value for key, value in loc.items() if key != "source_location"},
            "extraction_method": "manual_verified_rematch",
            "extraction_confidence": 1.0,
            "human_review_status": "confirmed",
            "verification_status": "confirmed",
            "text_trust_status": "verified",
            "search_eligible": True,
            "provenance": PROVENANCE,
            "verification_audit": {"workbook_sha256": workbook_hash, "response_label": row.get("Response Label", "")},
        }
        if existing:
            if all(existing.get(key) == value for key, value in record.items()):
                report["skipped"] += 1
            else:
                existing.update(record)
        else:
            candidate.setdefault("responses", []).append(record)
            responses[response_id] = record
            report["responses_inserted"] += 1

    for row in pairs + no_response:
        comment = comments[str(row["Comment ID"])]
        locator = row.get("Comment Locator") or row.get("Source Locator", "")
        loc = locator_fields(locator)
        prior_location = str(comment.get("source_location", ""))
        comment.setdefault("raw_original_text", str(comment.get("original_text", "")))
        comment["verified_text"] = row.get("Government Comment", "")
        comment["text_trust_status"] = "verified"
        comment["search_eligible"] = True
        comment["verification_status"] = "confirmed"
        comment["source_location"] = locator
        if "paragraph_index" in loc:
            current_locator = comment.get("source_locator_json", {}) if isinstance(comment.get("source_locator_json"), dict) else {}
            visible_index = current_locator.get("paragraph_index") if current_locator.get("match_method") == "exact_source_text" else None
            xml_index = int(loc["paragraph_index"])
            comment["source_row"] = xml_index
            comment["source_locator_json"] = {
                "paragraph_index": xml_index,
                "xml_paragraph_index": xml_index,
                "visible_paragraph_index": visible_index,
                "match_method": "manual_verified_workbook",
            }
        else:
            comment.update({key: value for key, value in loc.items() if key != "source_location"})
            comment["source_locator_json"] = {key: value for key, value in loc.items() if key != "source_location"}
        comment["verification_audit"] = {"workbook_sha256": workbook_hash, "provenance": PROVENANCE}
        report["comments_verified"] += 1
        if prior_location == locator and comment.get("verified_text") == row.get("Government Comment", ""):
            report["skipped"] += 1

    for row in pairs:
        comment_id, response_id = str(row["Comment ID"]), str(row["Response ID"])
        link = links[comment_id]
        was_linked = bool(link.get("response_id"))
        was_confirmed = link.get("review_status") == "confirmed"
        match_class = str(row.get("Match Status", ""))
        link.update({
            "response_id": response_id,
            "match_status": "confirmed",
            "matching_method": "manual_verified_grouped" if match_class == "verified_grouped" else "manual_verified_direct",
            "match_method": "manual_verified_grouped" if match_class == "verified_grouped" else "manual_verified_direct",
            "match_confidence": 0.85 if row.get("Confidence") == "medium" else 1.0,
            "review_status": "confirmed",
            "provenance": PROVENANCE,
            "import_key": f"all-projects:{comment_id}:{response_id}",
            "workbook_sha256": workbook_hash,
            "match_basis": row.get("Match Basis", ""),
            "source_document": comments[comment_id].get("source_document", ""),
            "source_location": comments[comment_id].get("source_location", ""),
            "comment_locator_json": copy.deepcopy(comments[comment_id].get("source_locator_json", {})),
            "response_locator_json": copy.deepcopy(responses[response_id].get("source_locator_json", {})),
        })
        comment = comments[comment_id]
        comment.update({"response_id": response_id, "match_status": "matched", "human_review_status": "confirmed"})
        if not was_linked:
            report["links_created"] += 1
        if not was_confirmed:
            report["links_confirmed"] += 1
    for row in no_response:
        comment_id = str(row["Comment ID"])
        link = links[comment_id]
        link.update({
            "review_status": "not_applicable", "provenance": PROVENANCE,
            "workbook_sha256": workbook_hash, "no_response_verified": True,
            "no_response_reason": row.get("Reason", ""),
            "comment_locator_json": copy.deepcopy(comments[comment_id].get("source_locator_json", {})),
        })
        report["unpaired_verified"] += 1

    hierarchy_report = merge_docx_comment_hierarchy(candidate, workspace)
    lineage_report = mark_copied_source_documents(candidate, workspace)
    report.update({
        key: hierarchy_report[key]
        for key in ("hierarchy_groups_merged", "hierarchy_children_suppressed")
    })
    report.update({
        key: lineage_report[key]
        for key in (
            "copied_source_groups", "copied_source_paths_suppressed",
            "copied_comment_rows_suppressed", "copied_source_details",
        )
    })
    report["conflicts"].extend(hierarchy_report["hierarchy_conflicts"])
    if report["conflicts"]:
        return report
    dedup_report = mark_duplicate_comments(candidate)
    report.update({key: dedup_report[key] for key in ("duplicate_rows_suppressed", "duplicate_groups")})
    identity = canonicalize_documents(candidate.get("comments", []))
    candidate.update({
        "source_files": identity["source_files"],
        "canonical_documents": identity["canonical_documents"],
        "source_file_aliases": identity["source_file_aliases"],
        "near_duplicate_review": identity["near_duplicate_review"],
    })
    report.update({
        "canonical_document_count": identity["canonical_document_count"],
        "source_file_aliases": len(identity["source_file_aliases"]),
    })

    if apply:
        backup = dataset_path.with_suffix(".pre_all_projects_rematch.json")
        if not backup.exists():
            atomic_json(backup, original)
        atomic_json(dataset_path, candidate)
        report["applied"] = True
    return report


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--source-root", type=Path, default=workspace / "comments&response")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = import_verified_workbook(args.workbook, args.dataset, args.source_root, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
