#!/usr/bin/env python3
"""Idempotently import structurally confirmed comment/response rematches."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .source_registry import _xlsx_cells, sha256_file
except ImportError:
    from source_registry import _xlsx_cells, sha256_file


EXPECTED_ROUNDS = {"3": 92, "4": 19, "5": 12}
PROJECT_MARKER = "2311 Warner Range"
PROJECT_NAME = "25 001 2311 Warner Range Ave — Building"


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_json_list(value: str, field: str, import_key: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{import_key}: invalid {field}: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError(f"{import_key}: {field} must be a JSON array of objects")
    return parsed


def workbook_rows(path: Path, sheet: str) -> list[dict[str, str]]:
    rows = _xlsx_cells(path, sheet)
    if not rows:
        raise ValueError(f"Sheet {sheet!r} is empty")
    headers = {cell["column"]: cell["value"].strip() for cell in rows[0]["cells"] if cell["value"].strip()}
    return [
        {headers[cell["column"]]: cell["value"] for cell in row["cells"] if cell["column"] in headers}
        for row in rows[1:]
    ]


def excel_date(value: str) -> str:
    value = normalized(value)
    if re.fullmatch(r"\d+(?:\.0+)?", value):
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(value)))).isoformat()
    for candidate in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(value, candidate).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unsupported report date {value!r}")


def pages(value: str) -> list[int]:
    return [int(item) for item in re.findall(r"\d+", value or "")]


def source_location(page_numbers: list[int]) -> str:
    if not page_numbers:
        return "unknown"
    if len(page_numbers) == 1:
        return f"page {page_numbers[0]}"
    return "pages " + ", ".join(str(value) for value in page_numbers)


def locator_boxes(
    locators: list[dict[str, Any]], page: int, companion: list[dict[str, Any]],
) -> list[list[float]]:
    """Return Adobe/PDF-coordinate boxes, converting top-left boxes when possible."""
    page_heights: list[float] = []
    for item in [*locators, *companion]:
        pdf_rect = item.get("pdf_rect")
        top_left = item.get("top_left_bbox")
        if item.get("page") == page and isinstance(pdf_rect, list) and isinstance(top_left, list) and len(pdf_rect) == len(top_left) == 4:
            page_heights.extend([float(pdf_rect[1]) + float(top_left[3]), float(pdf_rect[3]) + float(top_left[1])])
    height = sum(page_heights) / len(page_heights) if page_heights else 0.0
    boxes: list[list[float]] = []
    for item in locators:
        if int(item.get("page") or 0) != page:
            continue
        pdf_rect = item.get("pdf_rect")
        top_left = item.get("top_left_bbox")
        if isinstance(pdf_rect, list) and len(pdf_rect) == 4:
            boxes.append([round(float(value), 3) for value in pdf_rect])
        elif height and isinstance(top_left, list) and len(top_left) == 4:
            x_min, top, x_max, bottom = (float(value) for value in top_left)
            boxes.append([round(x_min, 3), round(height - bottom, 3), round(x_max, 3), round(height - top, 3)])
    return boxes


def payload_digest(row: dict[str, Any], children: list[dict[str, Any]]) -> str:
    fields = {
        key: row.get(key, "") for key in (
            "import_key", "application_number", "report_date", "response_letter_round",
            "reviewed_plan_round", "city_comment_id", "comment_text", "response_text",
            "source_file", "comment_source_pages", "response_source_pages",
            "comment_locator_json", "response_locator_json", "match_status",
            "match_method", "match_confidence",
        )
    }
    fields["embedded_subpairs"] = children
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_paths(source_root: Path, filenames: set[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for filename in filenames:
        matches = [path for path in source_root.rglob(filename) if path.is_file()]
        current_package = [path for path in matches if "archive" not in {part.casefold() for part in path.parts}]
        if len(current_package) == 1:
            matches = current_package
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one authorized source named {filename!r}; found {len(matches)}")
        result[filename] = matches[0]
    return result


def relative_source(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"Source file is outside the authorized workspace: {path.name}") from exc


def import_workbook(
    workbook: Path, dataset_path: Path, source_root: Path, *, dry_run: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    workspace = dataset_path.resolve().parents[1]
    parent_rows = [row for row in workbook_rows(workbook, "Database Import") if row.get("ready_for_import", "").upper() == "TRUE"]
    if len(parent_rows) != 123:
        raise ValueError(f"Expected 123 ready rows; found {len(parent_rows)}")
    round_counts = Counter(row.get("response_letter_round", "") for row in parent_rows)
    if dict(round_counts) != EXPECTED_ROUNDS:
        raise ValueError(f"Unexpected response-letter round counts: {dict(round_counts)}")
    import_keys = [row.get("import_key", "") for row in parent_rows]
    if any(not value for value in import_keys) or len(import_keys) != len(set(import_keys)):
        raise ValueError("Database Import contains a blank or duplicate import_key")

    raw_children = workbook_rows(workbook, "Embedded Subpairs")
    children_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_children:
        parent_key = row.get("Parent Import Key", "")
        locator = parse_json_list(row.get("Source Locator JSON", ""), "Source Locator JSON", row.get("Atomic Pair Key", ""))
        children_by_parent[parent_key].append({
            "atomic_pair_key": row.get("Atomic Pair Key", ""),
            "sequence": int(row.get("Sequence", "0") or 0),
            "reviewed_plan_round": row.get("Reviewed Plan Round", ""),
            "parent_comment_id": row.get("Parent Comment ID", ""),
            "comment_text": row.get("Embedded City Follow-up", ""),
            "response_text": row.get("Matched Response", ""),
            "source_file": row.get("Source PDF", ""),
            "source_pages": pages(row.get("Source Pages", "")),
            "source_locator": locator,
            "match_status": row.get("Match Status", ""),
        })
    for values in children_by_parent.values():
        values.sort(key=lambda item: item["sequence"])
    comment_141_key = "BLD2025-01058:PC2:141"
    if len(children_by_parent.get(comment_141_key, [])) != 7:
        raise ValueError("Comment 141 must contain exactly seven Embedded Subpairs")

    sources = source_paths(source_root, {row["source_file"] for row in parent_rows})
    source_hashes = {name: sha256_file(path) for name, path in sources.items()}
    source_relatives = {name: relative_source(path, workspace) for name, path in sources.items()}
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    comments = dataset.setdefault("comments", [])
    responses = dataset.setdefault("responses", [])
    links = dataset.setdefault("comment_response_links", [])
    response_by_id = {row["response_id"]: row for row in responses}
    links_by_comment = {row["comment_id"]: (index, row) for index, row in enumerate(links)}
    links_by_import: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, link in enumerate(links):
        if link.get("import_key"):
            links_by_import[str(link["import_key"])].append((index, link))

    candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for comment in comments:
        application = str(comment.get("application_number", ""))
        if not application and PROJECT_MARKER.casefold() in str(comment.get("property_project", "")).casefold():
            application = "BLD2025-01058"
        candidates[(application, str(comment.get("review_round", "")), str(comment.get("comment_number", "")))].append(comment)

    report: dict[str, Any] = {
        "workbook": workbook.name,
        "workbook_sha256": sha256_file(workbook),
        "dry_run": dry_run,
        "inserted": 0, "updated": 0, "skipped": 0, "conflicted": 0, "failed": 0,
        "details": [], "deterministic_disambiguations": [],
        "expected": {"parent_links": 123, "response_letter_rounds": EXPECTED_ROUNDS, "missing_response_locators": 0},
    }

    for row in parent_rows:
        import_key = row["import_key"]
        try:
            comment_locators = parse_json_list(row.get("comment_locator_json", ""), "comment_locator_json", import_key)
            response_locators = parse_json_list(row.get("response_locator_json", ""), "response_locator_json", import_key)
            if not response_locators:
                raise ValueError(f"{import_key}: missing response locator")
            children = children_by_parent.get(import_key, [])
            digest = payload_digest(row, children)
            existing_imports = links_by_import.get(import_key, [])
            if len(existing_imports) > 1:
                raise RuntimeError("duplicate existing links share this import_key")
            if existing_imports and existing_imports[0][1].get("import_payload_sha256") == digest:
                report["skipped"] += 1
                continue
            if existing_imports and existing_imports[0][1].get("review_status") == "confirmed":
                report["conflicted"] += 1
                report["details"].append({"import_key": import_key, "outcome": "conflicted", "reason": "confirmed imported link differs from workbook"})
                continue

            key = (row["application_number"], row["reviewed_plan_round"], row["city_comment_id"])
            matches = candidates.get(key, [])
            source_relative = source_relatives[row["source_file"]]
            structural_matches = [item for item in matches if Path(str(item.get("source_document", ""))).name == row["source_file"]]
            if len(matches) > 1 and len(structural_matches) == 1:
                comment = structural_matches[0]
                report["deterministic_disambiguations"].append({
                    "import_key": import_key, "candidate_count": len(matches),
                    "selected_comment_id": comment["comment_id"], "rule": "same_source_pdf_structure",
                })
            elif len(matches) == 1:
                comment = matches[0]
            elif not matches:
                comment = None
            else:
                report["conflicted"] += 1
                report["details"].append({"import_key": import_key, "outcome": "conflicted", "reason": f"{len(matches)} exact identifier matches"})
                continue

            existing_link = links_by_comment.get(comment["comment_id"])[1] if comment else None
            if existing_link and existing_link.get("review_status") == "confirmed" and existing_link.get("import_key") != import_key:
                existing_response = response_by_id.get(str(existing_link.get("response_id", "")), {})
                same_response = normalized(existing_response.get("original_text")) == normalized(row["response_text"])
                same_source = Path(str(existing_response.get("source_document", ""))).name == row["source_file"]
                if not (same_response and same_source):
                    report["conflicted"] += 1
                    report["details"].append({"import_key": import_key, "outcome": "conflicted", "reason": "different confirmed response link already exists"})
                    continue

            comment_pages = pages(row.get("comment_source_pages", ""))
            response_pages = pages(row.get("response_source_pages", ""))
            if not comment_pages or not response_pages:
                raise ValueError("comment or response page is missing")
            comment_boxes = locator_boxes(comment_locators, comment_pages[0], response_locators)
            response_boxes = locator_boxes(response_locators, response_pages[0], comment_locators)
            report_date = excel_date(row["report_date"])
            response_id = stable_id("R", f"rematch:{import_key}")
            link_id = stable_id("L", f"rematch:{import_key}")

            if comment is None:
                comment_id = stable_id("C", f"rematch:{import_key}")
                comment = {
                    "comment_id": comment_id, "city": row["city"], "property_project": PROJECT_NAME,
                    "application_number": row["application_number"], "review_round": row["reviewed_plan_round"],
                    "reviewed_plan_round": row["reviewed_plan_round"], "response_letter_round": row["response_letter_round"],
                    "report_date": report_date, "discipline": row.get("reviewer_department", "") or "Building",
                    "reviewer": row.get("reviewer_department", ""), "comment_number": row["city_comment_id"],
                    "city_comment_id": row["city_comment_id"], "original_text": row["comment_text"],
                    "source_document": source_relative, "source_page": comment_pages[0],
                    "source_page_end": comment_pages[-1], "source_location": source_location(comment_pages),
                    "source_sha256": source_hashes[row["source_file"]], "source_locator_json": comment_locators,
                    "source_bounding_boxes": comment_boxes, "extraction_method": "document_structure_rematch",
                    "extraction_confidence": 1.0, "match_status": "matched", "human_review_status": "confirmed",
                    "response_id": response_id, "source_status": "verified",
                }
                comments.append(comment)
                candidates[key].append(comment)
                outcome = "inserted"
            else:
                # Relationship/provenance fields are mutable; original_text and its source citation remain immutable.
                comment["application_number"] = row["application_number"]
                comment["reviewed_plan_round"] = row["reviewed_plan_round"]
                comment["response_letter_round"] = row["response_letter_round"]
                comment["report_date"] = report_date
                comment["city_comment_id"] = row["city_comment_id"]
                comment["match_status"] = "matched"
                comment["human_review_status"] = "confirmed"
                comment["response_id"] = response_id
                outcome = "updated"

            response_record = {
                "response_id": response_id, "comment_id": comment["comment_id"],
                "original_text": row["response_text"], "source_document": source_relative,
                "source_page": response_pages[0], "source_page_end": response_pages[-1],
                "source_location": source_location(response_pages), "source_sha256": source_hashes[row["source_file"]],
                "source_locator_json": response_locators, "source_bounding_boxes": response_boxes,
                "extraction_method": "document_structure_rematch", "extraction_confidence": 1.0,
                "human_review_status": "confirmed", "application_number": row["application_number"],
                "report_date": report_date, "response_letter_round": row["response_letter_round"],
                "reviewed_plan_round": row["reviewed_plan_round"], "import_key": import_key,
            }
            if response_id in response_by_id:
                response_by_id[response_id].update(response_record)
            else:
                responses.append(response_record)
                response_by_id[response_id] = response_record

            link_record = {
                "link_id": link_id, "import_key": import_key, "comment_id": comment["comment_id"],
                "response_id": response_id, "match_status": "confirmed", "matching_method": "same_pdf_form_row",
                "match_method": "same_pdf_form_row", "match_confidence": 1.0, "review_status": "confirmed",
                "provenance": "document_structure_rematch", "source_document": source_relative,
                "source_pdf": source_relative, "source_location": source_location(sorted(set(comment_pages + response_pages))),
                "comment_pages": comment_pages, "response_pages": response_pages,
                "comment_locator_json": comment_locators, "response_locator_json": response_locators,
                "report_date": report_date, "response_letter_round": row["response_letter_round"],
                "reviewed_plan_round": row["reviewed_plan_round"], "application_number": row["application_number"],
                "city_comment_id": row["city_comment_id"], "imported_current_round_comment_text": row["comment_text"],
                "embedded_subpairs": children, "import_payload_sha256": digest,
            }
            if existing_link:
                link_index = links_by_comment[comment["comment_id"]][0]
                links[link_index] = link_record
            elif existing_imports:
                links[existing_imports[0][0]] = link_record
            else:
                links.append(link_record)
            links_by_comment[comment["comment_id"]] = (links.index(link_record), link_record)
            links_by_import[import_key] = [(links.index(link_record), link_record)]
            report[outcome] += 1
        except RuntimeError as exc:
            report["conflicted"] += 1
            report["details"].append({"import_key": import_key, "outcome": "conflicted", "reason": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            report["failed"] += 1
            report["details"].append({"import_key": import_key, "outcome": "failed", "reason": str(exc)})

    imported_links = [row for row in links if row.get("import_key") in set(import_keys) and row.get("review_status") == "confirmed"]
    imported_rounds = Counter(str(row.get("response_letter_round", "")) for row in imported_links)
    missing_locators = sum(not row.get("response_locator_json") for row in imported_links)
    report["validation"] = {
        "confirmed_parent_links": len(imported_links),
        "response_letter_rounds": dict(imported_rounds),
        "missing_response_locators": missing_locators,
        "embedded_subpairs_for_comment_141": len(next((row.get("embedded_subpairs", []) for row in imported_links if row.get("import_key") == comment_141_key), [])),
    }
    if not dry_run and not report["conflicted"] and not report["failed"]:
        backup = dataset_path.with_name(f"{dataset_path.stem}.before-rematch-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}{dataset_path.suffix}")
        shutil.copy2(dataset_path, backup)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=dataset_path.parent, prefix="dataset-rematch-", suffix=".tmp", delete=False) as stream:
            json.dump(dataset, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, dataset_path)
        report["backup_path"] = str(backup)
    report["written"] = bool(not dry_run and not report["conflicted"] and not report["failed"])
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--source-root", type=Path, default=workspace / "comments&response")
    parser.add_argument("--report", type=Path, help="JSON report path; committed imports default to phase2_dataset/rematch_import_report.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report_path = args.report or (None if args.dry_run else workspace / "phase2_dataset" / "rematch_import_report.json")
    report = import_workbook(args.workbook, args.dataset, args.source_root, dry_run=args.dry_run, report_path=report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["conflicted"] or report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
