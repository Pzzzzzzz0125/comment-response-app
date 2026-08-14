#!/usr/bin/env python3
"""Repair legacy comments that contain a dated response/status line.

The visual importer now prevents this shape.  This one-shot migration makes
existing searchable data obey the same rule without rereading source files or
calling Gemini.  Raw extracted text is retained on each repaired comment and
the operation is idempotent.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from phase2.extract_dataset import stable_id
from phase2.visual_ingestion import split_embedded_response_lines


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def repair_dataset(path: Path, *, dry_run: bool = False) -> dict[str, int]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    comments = dataset.get("comments", [])
    responses = dataset.setdefault("responses", [])
    links = dataset.setdefault("comment_response_links", [])
    responses_by_id = {
        str(row.get("response_id")): row for row in responses if row.get("response_id")
    }
    links_by_comment = {
        str(row.get("comment_id")): row for row in links if row.get("comment_id")
    }
    inserted = 0
    skipped = 0
    for comment in comments:
        if not isinstance(comment, dict) or str(comment.get("response_id", "")).strip():
            skipped += 1
            continue
        candidate = {
            "record_key": str(comment.get("comment_id", "")),
            "comment_number": str(comment.get("comment_number", "")),
            "exact_comment_text": str(comment.get("original_text", "")),
            "normalized_comment_text": str(comment.get("normalized_comment_text", "")),
            "exact_response_text": "",
            "page_start": int(comment.get("source_page") or 0),
            "page_end": int(comment.get("source_page_end") or comment.get("source_page") or 0),
            "bounding_boxes": copy.deepcopy(comment.get("source_locator_json", {}).get("bounding_boxes", [])),
        }
        repaired = split_embedded_response_lines(candidate)
        if repaired is candidate or not str(repaired.get("exact_response_text", "")).strip():
            skipped += 1
            continue
        comment_id = str(comment.get("comment_id", ""))
        response_text = str(repaired["exact_response_text"])
        response_id = stable_id(
            "R", comment_id, "embedded-status",
            str(repaired.get("response_date_iso", "")), response_text,
        )
        if response_id in responses_by_id:
            comment["response_id"] = response_id
            skipped += 1
            continue
        raw_text = str(comment.get("raw_extracted_text") or comment.get("original_text", ""))
        clean_text = str(repaired["exact_comment_text"])
        comment["raw_extracted_text"] = raw_text
        comment["original_text"] = clean_text
        comment["normalized_comment_text"] = str(repaired.get("normalized_comment_text") or clean_text)
        if str(comment.get("verified_text", "")).strip():
            comment["verified_text"] = clean_text
        comment["source_locator_json"] = copy.deepcopy(repaired.get("comment_location", comment.get("source_locator_json", {})))
        comment["source_location"] = "page " + str(comment.get("source_page", "unknown")) + " · government comment before embedded response/status line"
        comment["response_id"] = response_id
        comment["match_status"] = "matched"
        comment["human_review_status"] = "confirmed"
        comment["verification_status"] = "confirmed"
        comment["text_trust_status"] = "verified"
        comment["search_eligible"] = True
        comment["response_date_raw"] = str(repaired.get("response_date_raw", ""))
        comment["response_date_iso"] = str(repaired.get("response_date_iso", ""))
        comment["response_type"] = str(repaired.get("response_type", ""))
        audit = comment.setdefault("ingestion_audit", {})
        audit.update({
            "embedded_response_repair": copy.deepcopy(repaired.get("embedded_response_repair", {})),
            "raw_extracted_comment_text": raw_text,
            "response_date_raw": str(repaired.get("response_date_raw", "")),
            "response_date_iso": str(repaired.get("response_date_iso", "")),
            "response_type": str(repaired.get("response_type", "")),
            "pairing_evidence": "Deterministic embedded response/status line split from the same source item",
        })
        response = {
            "response_id": response_id,
            "comment_id": comment_id,
            "original_text": response_text,
            "verified_text": response_text,
            "raw_extracted_text": response_text,
            "response_type": str(repaired.get("response_type", "")),
            "response_date_raw": str(repaired.get("response_date_raw", "")),
            "response_date_iso": str(repaired.get("response_date_iso", "")),
            "source_document": comment.get("source_document", ""),
            "source_sha256": comment.get("source_sha256", ""),
            "source_document_date": comment.get("source_document_date", ""),
            "document_date": copy.deepcopy(comment.get("document_date", {})),
            "source_page": comment.get("source_page", ""),
            "source_page_end": comment.get("source_page_end", ""),
            "source_location": "page " + str(comment.get("source_page", "unknown")) + " · embedded dated response/status line",
            "source_locator_json": copy.deepcopy(repaired.get("response_location", {})),
            "extraction_method": "deterministic_embedded_response_split",
            "extraction_confidence": 1.0,
            "human_review_status": "confirmed",
            "verification_status": "confirmed",
            "text_trust_status": "verified",
            "search_eligible": True,
            "ingestion_pipeline_version": comment.get("ingestion_pipeline_version", ""),
        }
        responses.append(response)
        responses_by_id[response_id] = response
        link = links_by_comment.get(comment_id)
        if link is None:
            link = {"link_id": stable_id("L", comment_id, response_id, "embedded-status")}
            links.append(link)
            links_by_comment[comment_id] = link
        link.update({
            "comment_id": comment_id,
            "response_id": response_id,
            "match_status": "matched",
            "matching_method": "embedded_status_line",
            "match_confidence": 1.0,
            "review_status": "confirmed",
            "verification_status": "confirmed",
            "pairing_evidence": "Dated status line immediately follows the same visible government comment",
            "provenance": "deterministic_embedded_response_split",
            "source_document": comment.get("source_document", ""),
            "source_document_date": comment.get("source_document_date", ""),
            "source_location": comment.get("source_location", ""),
            "comment_locator_json": copy.deepcopy(comment.get("source_locator_json", {})),
            "response_locator_json": copy.deepcopy(repaired.get("response_location", {})),
            "ingestion_audit": copy.deepcopy(audit),
            "response_date_raw": str(repaired.get("response_date_raw", "")),
            "response_date_iso": str(repaired.get("response_date_iso", "")),
            "response_type": str(repaired.get("response_type", "")),
        })
        inserted += 1
    if inserted and not dry_run:
        dataset.setdefault("repair_history", []).append({
            "repair": "embedded_dated_response_split",
            "inserted": inserted,
            "description": "Separated dated/status response lines from legacy government comments.",
        })
        _atomic_json(path, dataset)
    return {"inserted": inserted, "skipped": skipped}


def patch_source_registry(dataset_path: Path, registry_path: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Add response-owner sources and refresh comment locators in a live registry."""
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    documents = registry.setdefault("documents", {})
    sources = registry.setdefault("sources", {})
    by_relative = {
        str(row.get("relative_path")): row
        for row in documents.values()
        if isinstance(row, dict) and row.get("relative_path")
    }
    responses = {
        str(row.get("response_id")): row
        for row in dataset.get("responses", [])
        if row.get("response_id")
    }
    refreshed = 0
    added = 0
    for comment in dataset.get("comments", []):
        response_id = str(comment.get("response_id", ""))
        if not response_id or response_id not in responses or comment.get("response_type") != "applicant_status":
            continue
        comment_id = str(comment.get("comment_id", ""))
        owner_rows = [row for row in sources.values() if row.get("owner_id") == comment_id]
        response = responses[response_id]
        comment_locator = copy.deepcopy(comment.get("source_locator_json", {}))
        response_locator = copy.deepcopy(response.get("source_locator_json", {}))
        response_paths = [part.strip() for part in str(response.get("source_document", "")).split("|") if part.strip()]
        primary_for_response: list[dict[str, Any]] = []
        for row in owner_rows:
            location = row.get("location") if isinstance(row.get("location"), dict) else {}
            if row.get("relation") == "Primary source":
                location["exact_quote"] = str(comment.get("verified_text") or comment.get("original_text", ""))
                location["normalized_quote"] = str(comment.get("normalized_comment_text", ""))
                location["pdf_bounding_boxes"] = []
                metadata = location.setdefault("metadata", {})
                metadata["structured_locator_json"] = comment_locator
                metadata["coordinate_source"] = "embedded_status_repair"
                metadata["legacy_location"] = str(comment.get("source_location", ""))
                refreshed += 1
                doc = documents.get(str(row.get("document_id")), {})
                if str(doc.get("relative_path", "")) in response_paths:
                    primary_for_response.append(row)
        for row in primary_for_response:
            source_id = stable_id("S", f"{response_id}|{row.get('document_id')}|primary|0")
            if source_id in sources:
                continue
            source_row = copy.deepcopy(row)
            source_row["source_id"] = source_id
            source_row["owner_id"] = response_id
            source_row["relation"] = "Primary source"
            location = source_row.setdefault("location", {})
            location["exact_quote"] = str(response.get("verified_text") or response.get("original_text", ""))
            location["normalized_quote"] = str(response.get("response_date_iso") or response.get("original_text", "")).casefold()
            location["pdf_bounding_boxes"] = []
            location["page_number"] = response.get("source_page") or location.get("page_number") or 1
            metadata = location.setdefault("metadata", {})
            metadata["structured_locator_json"] = response_locator
            metadata["coordinate_source"] = "embedded_status_repair"
            metadata["legacy_location"] = str(response.get("source_location", ""))
            sources[source_id] = source_row
            added += 1
    if (refreshed or added) and not dry_run:
        _atomic_json(registry_path, registry)
    return {"refreshed_comment_sources": refreshed, "added_response_sources": added}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = {"dataset": repair_dataset(args.dataset, dry_run=args.dry_run)}
    if args.registry:
        result["registry"] = patch_source_registry(
            args.dataset, args.registry, dry_run=args.dry_run,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
