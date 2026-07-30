#!/usr/bin/env python3
"""Apply verified visual repairs without changing immutable raw extraction."""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from phase2.incremental_update import docx_paragraphs
from phase2.visual_ingestion import (
    VisualGeminiClient,
    VisualIngestionPipeline,
    location_pages,
    location_text,
    normalized_whitespace,
    result_is_verified,
    sha256_file,
    valid_pdf_location,
)
from web_app.local_secrets import gemini_api_key


def atomic_dataset(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix="dataset-repair-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def locator_boxes(locators: Any) -> list[list[float]]:
    boxes: list[list[float]] = []
    for locator in locators if isinstance(locators, list) else []:
        box = locator.get("pdf_rect") if isinstance(locator, dict) else None
        if isinstance(box, list) and len(box) == 4:
            boxes.append([float(value) for value in box])
    return boxes


def quarantine_legacy_orphan_responses(dataset: dict[str, Any]) -> int:
    linked = {str(row.get("response_id", "")) for row in dataset.get("comment_response_links", []) if row.get("response_id")}
    count = 0
    for response in dataset.get("responses", []):
        is_orphan_pdf = (
            response.get("response_id") not in linked
            and response.get("extraction_method") == "pdf_layout_text_matrix"
            and str(response.get("source_document", "")).casefold().endswith(".pdf")
        )
        if not is_orphan_pdf:
            continue
        response["search_eligible"] = False
        response["text_trust_status"] = "quarantined"
        response["quarantine_reason"] = "legacy_unlinked_pdf_response"
        count += 1
    return count


def repair_docx_paragraph_locators(dataset: dict[str, Any], workspace: Path) -> tuple[int, list[str]]:
    targets = [row for row in dataset.get("comments", []) if row.get("extraction_method") == "docx_numbered_paragraph"]
    by_document: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in targets:
        by_document[str(row.get("source_document", ""))].append(row)
    repaired = 0
    conflicts: list[str] = []
    links = {str(row.get("comment_id", "")): row for row in dataset.get("comment_response_links", [])}
    for relative, rows in by_document.items():
        path = workspace / relative
        if not path.is_file():
            conflicts.append(f"missing DOCX source: {relative}")
            continue
        paragraphs = docx_paragraphs(path)
        by_text: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for visible_index, paragraph in enumerate(paragraphs, 1):
            by_text[normalized_whitespace(paragraph["text"])].append((visible_index, paragraph))
        for row in rows:
            candidates = by_text.get(normalized_whitespace(str(row.get("original_text", ""))), [])
            if len(candidates) > 1 and str(row.get("source_row", "")).isdigit():
                xml_index = int(row["source_row"])
                candidates = [item for item in candidates if int(item[1]["source_number"]) == xml_index]
            if len(candidates) != 1:
                conflicts.append(f"{row.get('comment_id')}: expected one exact DOCX paragraph, found {len(candidates)}")
                continue
            paragraph_index, paragraph = candidates[0]
            row.setdefault("raw_source_location", row.get("source_location", ""))
            row.setdefault("raw_source_row", row.get("source_row", ""))
            row["source_row"] = paragraph_index
            row["source_location"] = f"paragraph {paragraph_index}"
            row["source_locator_json"] = {
                "paragraph_index": paragraph_index,
                "xml_paragraph_index": int(paragraph["source_number"]),
                "match_method": "exact_source_text",
            }
            row["locator_trust_status"] = "verified"
            row["verified_text"] = str(row.get("original_text", ""))
            row["text_trust_status"] = "verified"
            row["search_eligible"] = True
            link = links.get(str(row.get("comment_id", "")))
            if link is not None:
                link["source_location"] = row["source_location"]
                link["comment_locator_json"] = copy.deepcopy(row["source_locator_json"])
            repaired += 1
    return repaired, conflicts


def apply_warner_visual_repairs(
    dataset: dict[str, Any], workspace: Path, artifact_root: Path,
) -> tuple[int, list[str]]:
    links = [row for row in dataset.get("comment_response_links", []) if row.get("provenance") == "document_structure_rematch"]
    comments = {str(row.get("comment_id", "")): row for row in dataset.get("comments", [])}
    responses = {str(row.get("response_id", "")): row for row in dataset.get("responses", [])}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        grouped[str(link.get("source_pdf", ""))].append(link)
    planned: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]] = []
    conflicts: list[str] = []
    for relative, source_links in grouped.items():
        source = workspace / relative
        if not source.is_file():
            conflicts.append(f"missing PDF source: {relative}")
            continue
        artifact_dir = artifact_root / ("VI-" + sha256_file(source)[:20])
        extraction_path = artifact_dir / "gemini_extraction.json"
        verification_path = artifact_dir / "gemini_verification.json"
        if not extraction_path.is_file() or not verification_path.is_file():
            conflicts.append(f"missing Gemini artifacts: {source.name}")
            continue
        extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        verification = json.loads(verification_path.read_text(encoding="utf-8"))
        actual: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for result in extraction.get("records", []):
            actual[str(result.get("comment_id") or result.get("comment_number", ""))].append(result)
        expected_ids = {str(link.get("city_comment_id", "")) for link in source_links}
        if set(actual) != expected_ids:
            conflicts.append(
                f"{source.name}: expected IDs {len(expected_ids)}, received {len(actual)} "
                f"(missing={sorted(expected_ids-set(actual))}, unexpected={sorted(set(actual)-expected_ids)})"
            )
            continue
        for link in source_links:
            printed_id = str(link.get("city_comment_id", ""))
            rows = actual.get(printed_id, [])
            if len(rows) != 1:
                conflicts.append(f"{source.name} comment {printed_id}: expected one row, found {len(rows)}")
                continue
            result = rows[0]
            verified, reason = result_is_verified(result, verification)
            pages = location_pages(result.get("comment_location"))
            if not verified or not valid_pdf_location(result.get("comment_location"), len(json.loads((artifact_dir / "manifest.json").read_text())["pages"])):
                conflicts.append(f"{source.name} comment {printed_id}: unverified ({reason or 'invalid PDF location'})")
                continue
            comment = comments.get(str(link.get("comment_id", "")))
            response = responses.get(str(link.get("response_id", "")))
            if comment is None or response is None:
                conflicts.append(f"{source.name} comment {printed_id}: existing confirmed parent or response is missing")
                continue
            if normalized_whitespace(str(result.get("exact_response_text", ""))) != normalized_whitespace(str(response.get("original_text", ""))):
                conflicts.append(f"{source.name} comment {printed_id}: response conflicts with confirmed link")
                continue
            planned.append((link, comment, response, result, artifact_dir.name))
    if conflicts or len(planned) != len(links):
        if len(planned) != len(links):
            conflicts.append(f"atomic repair aborted: validated {len(planned)} of {len(links)} parent links")
        return 0, conflicts
    for link, comment, response, result, artifact_id in planned:
        comment.setdefault("raw_original_text", str(comment.get("original_text", "")))
        comment.setdefault("raw_source_location", str(comment.get("source_location", "")))
        comment["verified_text"] = str(result["exact_comment_text"])
        comment["text_trust_status"] = "verified"
        comment["search_eligible"] = True
        comment["verification_status"] = "confirmed"
        comment["verified_source_locator_json"] = copy.deepcopy(result["comment_location"])
        pages = location_pages(result["comment_location"])
        comment["source_page"] = pages[0]
        comment["source_page_end"] = pages[-1]
        comment["source_location"] = location_text(result["comment_location"], f"page {pages[0]}")
        comment["source_locator_json"] = copy.deepcopy(link.get("comment_locator_json", result["comment_location"]))
        boxes = locator_boxes(link.get("comment_locator_json"))
        if boxes:
            comment["source_bounding_boxes"] = boxes
        comment["verification_audit"] = {
            "artifact_id": artifact_id,
            "method": "gemini_visual_two_pass",
            "printed_comment_id": str(link.get("city_comment_id", "")),
            "confidence": float(result.get("confidence") or 0),
        }
        response.setdefault("raw_original_text", str(response.get("original_text", "")))
        response["verified_text"] = str(result["exact_response_text"])
        response["text_trust_status"] = "verified"
        response["search_eligible"] = True
        response["verification_status"] = "confirmed"
    return len(planned), []


def main() -> int:
    workspace = WORKSPACE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--artifacts", type=Path, default=workspace / "phase2_dataset" / "visual_regression_artifacts")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"))
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--force-gemini", action="store_true")
    parser.add_argument("--local-only", action="store_true", help="Apply only DOCX locator repair and legacy-response quarantine")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.local_only and (args.force_gemini or not args.artifacts.is_dir()):
        api_key = gemini_api_key()
        if not api_key and args.api_key_stdin:
            api_key = getpass.getpass("Gemini API key: ")
        if not api_key:
            raise SystemExit("Gemini API key is required to generate repair artifacts")
        dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
        source_paths = sorted({str(row["source_pdf"]) for row in dataset.get("comment_response_links", []) if row.get("provenance") == "document_structure_rematch"})
        pipeline = VisualIngestionPipeline(VisualGeminiClient(api_key, args.model), args.artifacts, args.dataset)
        for relative in source_paths:
            pipeline.process(workspace / relative, relative, {
                "property_hint": "25 001 2311 Warner Range Ave — Building",
                "city_hint": "Menlo Park", "audit_document_type_hint": "combined comment response form",
            }, force=args.force_gemini)
    original = json.loads(args.dataset.read_text(encoding="utf-8"))
    candidate = copy.deepcopy(original)
    repaired, conflicts = (0, []) if args.local_only else apply_warner_visual_repairs(candidate, workspace, args.artifacts)
    docx_repaired, docx_conflicts = repair_docx_paragraph_locators(candidate, workspace)
    quarantined = quarantine_legacy_orphan_responses(candidate)
    report = {
        "warner_parent_links_repaired": repaired,
        "docx_locators_repaired": docx_repaired,
        "legacy_orphan_pdf_responses_quarantined": quarantined,
        "conflicts": [*conflicts, *docx_conflicts],
        "applied": False,
    }
    expected = (
        (args.local_only or repaired == 123)
        and docx_repaired == 67 and quarantined == 61 and not report["conflicts"]
    )
    if args.apply and expected:
        backup = args.dataset.with_suffix(".pre_verified_repair.json")
        if not backup.exists():
            atomic_dataset(backup, original)
        atomic_dataset(args.dataset, candidate)
        report["applied"] = True
    elif args.apply:
        report["conflicts"].append("dataset was not written because expected 123/67/61 integrity counts were not met")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
