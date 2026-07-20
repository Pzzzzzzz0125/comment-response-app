#!/usr/bin/env python3
"""Audit whether permit records are safe inputs for precedent retrieval."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

try:
    from .rag_search import coherent_units, truncation_reason
except ImportError:
    from rag_search import coherent_units, truncation_reason


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    comments = dataset.get("comments", [])
    responses = dataset.get("responses", [])
    links = dataset.get("comment_response_links", [])
    comments_by_id = {str(row.get("comment_id", "")): row for row in comments}
    responses_by_id = {str(row.get("response_id", "")): row for row in responses}
    links_by_comment = {str(row.get("comment_id", "")): row for row in links}

    findings = []
    multi_unit = []
    truncation = []
    missing_sources = []
    metadata_fields = ("city", "property_project", "review_round", "discipline", "original_text", "source_document", "source_location")
    for comment in comments:
        comment_id = str(comment.get("comment_id", ""))
        units = coherent_units(str(comment.get("original_text", "")))
        if len(units) > 1:
            multi_unit.append({"comment_id": comment_id, "search_units": len(units)})
        reason = truncation_reason(str(comment.get("original_text", "")))
        if reason:
            truncation.append({"comment_id": comment_id, "reason": reason})
        for raw_path in str(comment.get("source_document", "")).split(" | "):
            if raw_path.strip() and not (workspace / raw_path.strip()).is_file():
                missing_sources.append({"owner_id": comment_id, "filename": Path(raw_path).name})
    for response in responses:
        for raw_path in str(response.get("source_document", "")).split(" | "):
            if raw_path.strip() and not (workspace / raw_path.strip()).is_file():
                missing_sources.append({"owner_id": str(response.get("response_id", "")), "filename": Path(raw_path).name})

    link_errors = []
    for comment_id, comment in comments_by_id.items():
        link = links_by_comment.get(comment_id)
        if not link:
            link_errors.append({"comment_id": comment_id, "error": "missing_link_row"})
            continue
        response_id = str(link.get("response_id", ""))
        comment_response_id = str(comment.get("response_id", ""))
        if response_id != comment_response_id:
            link_errors.append({"comment_id": comment_id, "error": "comment_link_response_mismatch"})
        if response_id:
            response = responses_by_id.get(response_id)
            if not response:
                link_errors.append({"comment_id": comment_id, "error": "unknown_response_id"})
            elif str(response.get("comment_id", "")) != comment_id:
                link_errors.append({"comment_id": comment_id, "error": "response_points_to_other_comment"})
    duplicates = {
        "comment_ids": [key for key, count in Counter(str(row.get("comment_id", "")) for row in comments).items() if count > 1],
        "response_ids": [key for key, count in Counter(str(row.get("response_id", "")) for row in responses).items() if count > 1],
        "link_comment_ids": [key for key, count in Counter(str(row.get("comment_id", "")) for row in links).items() if count > 1],
    }
    missing_metadata = {field: sum(not str(row.get(field, "")).strip() for row in comments) for field in metadata_fields}
    if multi_unit:
        findings.append("Some extracted parent comments contain multiple top-level numbered requirements; index them as coherent search units while preserving the parent citation.")
    if truncation:
        findings.append("Suspected mid-sentence truncation exists; do not repair it automatically or present it as high-confidence evidence.")
    if link_errors:
        findings.append("Response-link structural errors require correction before retrieval.")
    if missing_sources:
        findings.append("Some stored source documents cannot be resolved under the authorized workspace root.")
    report = {
        "schema_version": "1.0", "dataset": args.dataset.name,
        "counts": {"comments": len(comments), "responses": len(responses), "links": len(links), "unmatched_comments": sum(not str(row.get("response_id", "")) for row in comments)},
        "cities": dict(sorted(Counter(str(row.get("city", "")) for row in comments).items())),
        "missing_metadata": missing_metadata, "duplicates": duplicates,
        "multi_unit_records": multi_unit, "suspected_truncation": truncation,
        "response_link_errors": link_errors, "missing_source_files": missing_sources,
        "link_methods": dict(Counter(str(row.get("matching_method", "")) for row in links)),
        "link_review_status": dict(Counter(str(row.get("review_status", "")) for row in links)),
        "findings": findings,
        "semantic_link_validation": "Requires Gemini/domain review; this audit verifies structural association only.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if link_errors or any(duplicates.values()) or any(missing_metadata.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
