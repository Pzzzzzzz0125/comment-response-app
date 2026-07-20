#!/usr/bin/env python3
"""Validate imported rematch links and their SourceViewer citations."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_ROUNDS = {"3": 92, "4": 19, "5": 12}


def validate(dataset_path: Path, registry_path: Path, baseline_path: Path | None = None) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    comments = {row["comment_id"]: row for row in dataset.get("comments", [])}
    responses = {row["response_id"]: row for row in dataset.get("responses", [])}
    imported = [row for row in dataset.get("comment_response_links", []) if row.get("provenance") == "document_structure_rematch"]
    sources_by_owner: dict[str, list[dict[str, Any]]] = {}
    for source in registry.get("sources", {}).values():
        sources_by_owner.setdefault(str(source.get("owner_id", "")), []).append(source)
    documents = registry.get("documents", {})
    failures: list[dict[str, str]] = []
    citation_count = 0

    if len(imported) != 123:
        failures.append({"scope": "dataset", "reason": f"expected 123 imported links, found {len(imported)}"})
    keys = [str(row.get("import_key", "")) for row in imported]
    if len(keys) != len(set(keys)):
        failures.append({"scope": "dataset", "reason": "duplicate import_key values"})
    rounds = Counter(str(row.get("response_letter_round", "")) for row in imported)
    if dict(rounds) != EXPECTED_ROUNDS:
        failures.append({"scope": "dataset", "reason": f"unexpected round counts {dict(rounds)}"})

    for link in imported:
        key = str(link.get("import_key", ""))
        if not link.get("response_locator_json"):
            failures.append({"scope": key, "reason": "missing response locator"})
        if not link.get("comment_locator_json"):
            failures.append({"scope": key, "reason": "missing comment locator"})
        required = {
            "match_status": "confirmed", "matching_method": "same_pdf_form_row",
            "match_confidence": 1.0, "review_status": "confirmed",
            "provenance": "document_structure_rematch",
        }
        for field, expected in required.items():
            if link.get(field) != expected:
                failures.append({"scope": key, "reason": f"{field} is {link.get(field)!r}, expected {expected!r}"})
        comment = comments.get(str(link.get("comment_id", "")))
        response = responses.get(str(link.get("response_id", "")))
        if not comment or not response:
            failures.append({"scope": key, "reason": "comment or response record is missing"})
            continue
        if response.get("comment_id") != comment.get("comment_id") or comment.get("response_id") != response.get("response_id"):
            failures.append({"scope": key, "reason": "comment/response foreign keys are inconsistent"})
        expected_filename = Path(str(link.get("source_pdf", ""))).name
        for owner, page_field, label in (
            (comment, "comment_pages", "comment"), (response, "response_pages", "response"),
        ):
            # Comments use comment_id; responses use response_id.
            owner_id = str(comment["comment_id"] if label == "comment" else response["response_id"])
            primary = [row for row in sources_by_owner.get(owner_id, []) if row.get("relation") == "Primary source"]
            if len(primary) != 1:
                failures.append({"scope": key, "reason": f"{label} has {len(primary)} primary sources"})
                continue
            source = primary[0]
            document = documents.get(str(source.get("document_id", "")), {})
            expected_page = int(link.get(page_field, [0])[0])
            actual_page = int(source.get("location", {}).get("page_number") or 0)
            if document.get("filename") != expected_filename or actual_page != expected_page:
                failures.append({"scope": key, "reason": f"{label} citation resolved to {document.get('filename')} page {actual_page}, expected {expected_filename} page {expected_page}"})
            location = source.get("location", {})
            if not location.get("pdf_bounding_boxes") and not location.get("exact_quote"):
                failures.append({"scope": key, "reason": f"{label} citation has neither coordinates nor exact-text fallback"})
            citation_count += 1

    parent_141 = next((row for row in imported if row.get("import_key") == "BLD2025-01058:PC2:141"), {})
    if len(parent_141.get("embedded_subpairs", [])) != 7:
        failures.append({"scope": "BLD2025-01058:PC2:141", "reason": "expected seven structured child subpairs"})

    immutable_checked = 0
    if baseline_path:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        old_comments = {row["comment_id"]: row for row in baseline.get("comments", [])}
        for comment_id, old in old_comments.items():
            if comment_id in comments and comments[comment_id].get("original_text") != old.get("original_text"):
                failures.append({"scope": comment_id, "reason": "immutable original comment text changed"})
            elif comment_id in comments:
                immutable_checked += 1

    return {
        "ok": not failures, "confirmed_parent_links": len(imported),
        "response_letter_rounds": dict(rounds), "citations_checked": citation_count,
        "immutable_comments_checked": immutable_checked, "failures": failures,
    }


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--registry", type=Path, default=workspace / "web_app" / "data" / "source_registry.json")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    result = validate(args.dataset, args.registry, args.baseline)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
