#!/usr/bin/env python3
"""Append newly audited permit sources without re-extracting recorded sources."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from corpus_audit import audit_corpus as audit
from phase2 import extract_dataset as base


def gs_text_pages(path: Path) -> list[str]:
    ghostscript = __import__("shutil").which("gs")
    if not ghostscript:
        raise RuntimeError("Incremental PDF extraction requires local Ghostscript (gs)")
    with tempfile.TemporaryDirectory(prefix="permit-text-", dir="/private/tmp") as temporary:
        directory = Path(temporary).resolve()
        pattern = directory / "page-%04d.txt"
        result = subprocess.run(
            [
                ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                "-sDEVICE=txtwrite", f"-o{pattern}", str(path.resolve()),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=240,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Ghostscript text extraction failed for {path.name} "
                f"with exit code {result.returncode}"
            )
        pages = sorted(directory.glob("page-*.txt"))
        if not pages:
            raise RuntimeError(f"Ghostscript produced no text pages for {path.name}")
        return [page.read_text(encoding="utf-8", errors="replace") for page in pages]


def docx_paragraphs(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs: list[dict[str, Any]] = []
    for source_number, paragraph in enumerate(
        (node for node in root.iter() if audit.xml_local(node.tag) == "p"), start=1
    ):
        text = base.normalize_text(
            "".join(
                node.text or ""
                for node in paragraph.iter()
                if audit.xml_local(node.tag) == "t"
            )
        )
        if not text:
            continue
        style = ""
        num_id = ""
        for node in paragraph.iter():
            if audit.xml_local(node.tag) == "pStyle":
                style = next(iter(node.attrib.values()), "")
            elif audit.xml_local(node.tag) == "numId":
                num_id = next(iter(node.attrib.values()), "")
        paragraphs.append({
            "source_number": source_number,
            "text": text,
            "style": style,
            "num_id": num_id,
        })
    return paragraphs


def make_comment(
    record: dict[str, Any],
    comment_id: str,
    number: str,
    text: str,
    discipline: str,
    reviewer: str,
    reviewer_context: str,
    location: str,
    method: str,
    confidence: float,
    sheet: str = "",
    row: Any = "",
    page: Any = "",
    page_end: Any = "",
) -> dict[str, Any]:
    return {
        "comment_id": comment_id,
        "city": record["likely_city"],
        "property_project": record["likely_property_project"],
        "review_round": record["likely_review_round"],
        "discipline": discipline or "unknown",
        "reviewer": reviewer,
        "reviewer_context": reviewer_context,
        "comment_number": number,
        "original_text": base.normalize_text(text),
        "source_document": record["path"],
        "source_sha256": record["sha256"],
        "source_sheet": sheet,
        "source_row": row,
        "source_page": page,
        "source_page_end": page_end,
        "source_location": location,
        "extraction_method": method,
        "extraction_confidence": confidence,
        "source_cycle": record["likely_review_round"],
        "source_status": "",
        "response_id": "",
        "match_status": "unmatched",
        "human_review_status": "pending",
    }


def review_comment(comment: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "item_type": "extracted_comment",
        "item_id": comment["comment_id"],
        "reason": reason,
        "source_document": comment["source_document"],
        "source_location": comment["source_location"],
        "suggested_action": "Compare extracted text and boundary with the cited source location",
        "decision": "",
        "decision_note": "",
    }


def extract_menlo_docx(
    path: Path, record: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    paragraphs = docx_paragraphs(path)
    start_index = next(
        (
            index for index, paragraph in enumerate(paragraphs)
            if paragraph["text"].upper() in {"PRELIMINARY ARBORIST REPORT", "PLANS"}
        ),
        None,
    )
    if start_index is None:
        raise ValueError(f"No attached numbered comment list found in {record['path']}")
    discipline = "City Arborist"
    reviewer = "Jillian Keller"
    comments: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    sequence = 0
    current_section = ""
    for paragraph in paragraphs[start_index:]:
        text = paragraph["text"]
        if (
            not paragraph["num_id"]
            and len(text) <= 80
            and text.upper() == text
        ):
            current_section = text.title()
            continue
        if not paragraph["num_id"]:
            continue
        sequence += 1
        number = str(sequence)
        location = f"paragraph {paragraph['source_number']}"
        comment_id = base.stable_id(
            "C", record["path"], record["sha256"], paragraph["source_number"]
        )
        comment = make_comment(
            record, comment_id, number, text,
            current_section or discipline, reviewer,
            "Jillian Keller, Menlo Park City Arborist",
            location, "docx_numbered_paragraph", 0.94,
            row=paragraph["source_number"],
        )
        comments.append(comment)
        links.append(base.make_link(
            comment_id, "", record["path"], location, 1.0
        ))
        review.append(review_comment(
            comment,
            "New-city DOCX list item has not been human-confirmed",
        ))
    summary = {
        "city": record["likely_city"],
        "property_project": record["likely_property_project"],
        "review_round": record["likely_review_round"],
        "source_document": record["path"],
        "source_type": "docx_city_comment_letter",
        "comment_count": len(comments),
        "response_count": 0,
        "matched_count": 0,
        "unmatched_count": len(comments),
        "extraction_method": "docx_numbered_paragraph",
        "processing_error": "",
    }
    return comments, [], links, summary, review


def clean_column_fragment(value: str) -> str:
    text = base.normalize_text(value)
    if not text:
        return ""
    if re.search(r"^(?:Comment|Applicant Response|Review Comments|ID)$", text, re.I):
        return ""
    return text


def parse_menlo_matrix_pages(pages: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        header_indexes = [
            index for index, line in enumerate(lines)
            if "Review Comments" in line and "Applicant Response" in line
        ]
        for header_position, header_index in enumerate(header_indexes):
            segment_end = (
                header_indexes[header_position + 1]
                if header_position + 1 < len(header_indexes)
                else len(lines)
            )
            header = lines[header_index]
            page_ref_start = header.find("Page Ref")
            reviewer_start = header.find("Reviewer")
            comment_start = header.find("Review Comments")
            response_start = header.find("Applicant Response")
            if min(page_ref_start, reviewer_start, comment_start, response_start) < 0:
                continue
            id_rows: list[tuple[int, str]] = []
            for line_index in range(header_index + 1, segment_end):
                left = lines[line_index][:page_ref_start]
                match = re.fullmatch(r"\s*(\d{1,6})\s*", left)
                if match:
                    id_rows.append((line_index, match.group(1)))
            for item_index, (line_index, comment_number) in enumerate(id_rows):
                previous_line = id_rows[item_index - 1][0] if item_index else header_index
                next_line = (
                    id_rows[item_index + 1][0]
                    if item_index + 1 < len(id_rows)
                    else segment_end
                )
                start = (previous_line + line_index) // 2 + 1
                end = (line_index + next_line) // 2 + 1
                comment_fragments: list[str] = []
                response_fragments: list[str] = []
                for raw in lines[start:end]:
                    padded = raw.ljust(response_start)
                    comment_fragment = clean_column_fragment(
                        padded[comment_start:response_start]
                    )
                    response_fragment = clean_column_fragment(
                        raw[response_start:] if len(raw) > response_start else ""
                    )
                    if comment_fragment:
                        comment_fragments.append(comment_fragment)
                    if response_fragment:
                        response_fragments.append(response_fragment)
                id_line = lines[line_index].ljust(comment_start)
                page_reference = base.normalize_text(
                    id_line[page_ref_start:reviewer_start]
                )
                reviewer_context = base.normalize_text(
                    id_line[reviewer_start:comment_start]
                )
                comment_text = base.normalize_text("\n".join(comment_fragments))
                response_text = base.normalize_text("\n".join(response_fragments))
                if len(comment_text) < 15:
                    continue
                items.append({
                    "number": comment_number,
                    "page_reference": page_reference,
                    "reviewer_context": reviewer_context,
                    "comment": comment_text,
                    "response": response_text,
                    "page": page_number,
                })
    return items


def extract_menlo_matrix(
    path: Path, record: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    pages = gs_text_pages(path)
    items = parse_menlo_matrix_pages(pages)
    if not items:
        summary = {
            "city": record["likely_city"],
            "property_project": record["likely_property_project"],
            "review_round": record["likely_review_round"],
            "source_document": record["path"],
            "source_type": "pdf_matrix_deferred",
            "comment_count": 0,
            "response_count": 0,
            "matched_count": 0,
            "unmatched_count": 0,
            "extraction_method": "deferred_mixed_image_matrix",
            "processing_error": (
                "Mixed image/text matrix needs a dedicated spatial OCR pass; "
                "source retained but no rows guessed"
            ),
        }
        return [], [], [], summary, []
    comments: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for ordinal, item in enumerate(items, start=1):
        reviewer_context = item["reviewer_context"]
        if ":" in reviewer_context:
            reviewer, discipline = [
                part.strip() for part in reviewer_context.split(":", 1)
            ]
        else:
            reviewer, discipline = reviewer_context, "Building"
        location = f"page {item['page']}, comment ID {item['number']}"
        comment_id = base.stable_id(
            "C", record["path"], record["sha256"], item["page"], item["number"]
        )
        comment = make_comment(
            record, comment_id, item["number"], item["comment"],
            discipline, reviewer, reviewer_context, location,
            "pdf_layout_text_matrix", 0.86, page=item["page"],
        )
        response_id = ""
        if item["response"]:
            response_id = base.stable_id(
                "R", record["path"], record["sha256"],
                item["page"], item["number"],
            )
            response = {
                "response_id": response_id,
                "comment_id": comment_id,
                "original_text": item["response"],
                "source_document": record["path"],
                "source_sha256": record["sha256"],
                "source_sheet": "",
                "source_row": "",
                "source_page": item["page"],
                "source_location": location,
                "extraction_method": "pdf_layout_text_matrix",
                "extraction_confidence": 0.84,
                "human_review_status": "pending",
            }
            responses.append(response)
            comment["response_id"] = response_id
            comment["match_status"] = "matched"
        comments.append(comment)
        link = base.make_link(
            comment_id, response_id, record["path"], location,
            0.9 if response_id else 1.0,
        )
        links.append(link)
        review.append(review_comment(
            comment,
            "New-city PDF matrix column extraction has not been human-confirmed",
        ))
        if response_id:
            review.append({
                "item_type": "comment_response_link",
                "item_id": link["link_id"],
                "reason": "PDF matrix same-row match has not been human-confirmed",
                "source_document": record["path"],
                "source_location": location,
                "suggested_action": "Confirm the extracted response belongs to this comment ID",
                "decision": "",
                "decision_note": "",
            })
    summary = {
        "city": record["likely_city"],
        "property_project": record["likely_property_project"],
        "review_round": record["likely_review_round"],
        "source_document": record["path"],
        "source_type": "pdf_combined_comment_response_matrix",
        "comment_count": len(comments),
        "response_count": len(responses),
        "matched_count": len(responses),
        "unmatched_count": len(comments) - len(responses),
        "extraction_method": "pdf_layout_text_matrix",
        "processing_error": "",
    }
    return comments, responses, links, summary, review


def sunnyvale_comment_units(pages: list[str]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    discipline = ""
    current: dict[str, Any] | None = None
    architecture_sheet_number = 0
    info_number = 0

    def flush() -> None:
        nonlocal current
        if current and current["lines"]:
            current["text"] = base.normalize_text("\n".join(current.pop("lines")))
            units.append(current)
        current = None

    for page_number, page in enumerate(pages, start=1):
        for raw in page.splitlines():
            line = base.normalize_text(raw)
            if not line:
                continue
            section = re.match(
                r"^(?:\d+\.\s+|Building\s*-\s*)"
                r"(Planning|Fire Prevention|Structural|Architectural)\s*:?\s*$",
                line, re.I,
            )
            if section:
                flush()
                discipline = section.group(1).title()
                continue
            if discipline == "Structural":
                continue
            numbered = re.match(r"^(\d+)\.\)\s*(.*)$", line)
            if numbered and discipline in {"Planning", "Fire Prevention"}:
                flush()
                current = {
                    "discipline": discipline,
                    "number": numbered.group(1),
                    "page": page_number,
                    "page_end": page_number,
                    "lines": [numbered.group(2)],
                }
                continue
            if discipline == "Architectural" and line.startswith("Sheet "):
                flush()
                architecture_sheet_number += 1
                current = {
                    "discipline": discipline,
                    "number": str(architecture_sheet_number),
                    "page": page_number,
                    "page_end": page_number,
                    "lines": [line],
                }
                continue
            if discipline == "Architectural" and (
                line.startswith("This project requires")
                or line.startswith("Please complete a brief survey")
            ):
                flush()
                info_number += 1
                current = {
                    "discipline": discipline,
                    "number": f"INFO-{info_number}",
                    "page": page_number,
                    "page_end": page_number,
                    "lines": [line],
                }
                continue
            if current:
                if re.search(
                    r"questions regarding|written response letter|"
                    r"Cloud all revisions|Comments are as follows",
                    line, re.I,
                ):
                    continue
                current["lines"].append(line)
                current["page_end"] = page_number
    flush()
    return [unit for unit in units if len(unit["text"]) >= 15]


def sunnyvale_response_units(pages: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    responses: dict[tuple[str, str], dict[str, Any]] = {}
    discipline = ""
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current:
            current["text"] = base.normalize_text("\n".join(current.pop("lines")))
            responses[(current["discipline"], current["number"])] = current
        current = None

    for page_number, page in enumerate(pages, start=1):
        for raw in page.splitlines():
            line = base.normalize_text(raw)
            if not line:
                continue
            section = re.match(
                r"^(Planning|Fire Prevention|Architectural|Structural)\s*:\s*$",
                line, re.I,
            )
            if section:
                flush()
                discipline = section.group(1).title()
                continue
            numbered = re.match(r"^Re\s*:\s*(\d+)\.\s*(.*)$", line, re.I)
            if numbered and discipline != "Structural":
                flush()
                current = {
                    "discipline": discipline,
                    "number": numbered.group(1),
                    "page": page_number,
                    "lines": [numbered.group(2)],
                }
                continue
            if current:
                current["lines"].append(line)
    flush()
    return responses


def extract_sunnyvale(
    comment_path: Path,
    comment_record: dict[str, Any],
    response_path: Path | None = None,
    response_record: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    comment_units = sunnyvale_comment_units(gs_text_pages(comment_path))
    response_units = (
        sunnyvale_response_units(gs_text_pages(response_path))
        if response_path is not None else {}
    )
    comments: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for unit in comment_units:
        location = base.source_location(
            page=unit["page"], page_end=unit["page_end"]
        )
        comment_id = base.stable_id(
            "C", comment_record["path"], comment_record["sha256"],
            unit["discipline"], unit["number"], unit["page"],
        )
        comment = make_comment(
            comment_record, comment_id, unit["number"], unit["text"],
            unit["discipline"], "", "City of Sunnyvale",
            location, "pdf_layout_text_letter", 0.92,
            page=unit["page"], page_end=unit["page_end"],
        )
        response_id = ""
        response_unit = response_units.get((unit["discipline"], unit["number"]))
        if response_unit and response_record:
            response_location = f"page {response_unit['page']}"
            response_id = base.stable_id(
                "R", response_record["path"], response_record["sha256"],
                unit["discipline"], unit["number"],
            )
            responses.append({
                "response_id": response_id,
                "comment_id": comment_id,
                "original_text": response_unit["text"],
                "source_document": response_record["path"],
                "source_sha256": response_record["sha256"],
                "source_sheet": "",
                "source_row": "",
                "source_page": response_unit["page"],
                "source_location": response_location,
                "extraction_method": "pdf_layout_text_letter",
                "extraction_confidence": 0.92,
                "human_review_status": "pending",
            })
            comment["response_id"] = response_id
            comment["match_status"] = "matched"
        comments.append(comment)
        link_source = response_record["path"] if response_id and response_record else comment_record["path"]
        link = base.make_link(
            comment_id, response_id, link_source, location,
            0.94 if response_id else 1.0,
        )
        links.append(link)
        review.append(review_comment(
            comment,
            "New-city PDF letter extraction has not been human-confirmed",
        ))
        if response_id:
            review.append({
                "item_type": "comment_response_link",
                "item_id": link["link_id"],
                "reason": "Discipline-and-number PDF letter match has not been human-confirmed",
                "source_document": link_source,
                "source_location": location,
                "suggested_action": "Confirm the response corresponds to this discipline and number",
                "decision": "",
                "decision_note": "",
            })
    summaries = [{
        "city": comment_record["likely_city"],
        "property_project": comment_record["likely_property_project"],
        "review_round": comment_record["likely_review_round"],
        "source_document": (
            f"{comment_record['path']} | {response_record['path']}"
            if response_record else comment_record["path"]
        ),
        "source_type": "pdf_comment_response_letters" if response_record else "pdf_city_comment_letter",
        "comment_count": len(comments),
        "response_count": len(responses),
        "matched_count": len(responses),
        "unmatched_count": len(comments) - len(responses),
        "extraction_method": "pdf_layout_text_letter",
        "processing_error": "",
    }]
    return comments, responses, links, summaries, review


def load_existing(output_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    dataset_path = output_dir / "dataset.json"
    if not dataset_path.is_file():
        raise ValueError(
            f"Existing dataset is missing: {dataset_path}; run initial Phase 2 first"
        )
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    review_path = output_dir / "extraction_review.csv"
    if dataset.get("review_items") is not None:
        review = dataset["review_items"]
    elif review_path.is_file():
        with review_path.open(encoding="utf-8", newline="") as stream:
            review = list(csv.DictReader(stream))
    else:
        review = []
    return dataset, review


def selected_groups(
    summaries: list[dict[str, str]],
    inventory: dict[str, dict[str, Any]],
) -> list[tuple[dict[str, str], list[dict[str, Any]]]]:
    groups = []
    for summary in summaries:
        paths = [
            item.strip()
            for item in summary.get("recommended_primary_source", "").split(" | ")
            if item.strip()
        ]
        if not paths:
            continue
        records = []
        for path in paths:
            if path not in inventory:
                raise ValueError(f"Selected audit source is missing: {path}")
            records.append(inventory[path])
        groups.append((summary, records))
    return groups


def process_new_group(
    workspace: Path,
    summary: dict[str, str],
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    city = summary["likely_city"]
    if city == "Menlo Park":
        comments: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        comment_record = records[0]
        response_record = records[-1] if len(records) > 1 else None
        if comment_record["extension"] == ".docx":
            result = extract_menlo_docx(
                workspace / comment_record["path"], comment_record
            )
            c, r, l, s, q = result
            comments += c; responses += r; links += l; summaries.append(s); review += q
        else:
            summaries.append({
                "city": city,
                "property_project": comment_record["likely_property_project"],
                "review_round": comment_record["likely_review_round"],
                "source_document": comment_record["path"],
                "source_type": "drawing_markup_deferred",
                "comment_count": 0,
                "response_count": 0,
                "matched_count": 0,
                "unmatched_count": 0,
                "extraction_method": "deferred_visual_document",
                "processing_error": "Marked-up plan comments deferred to visual-document phase",
            })
        if response_record is not None:
            result = extract_menlo_matrix(
                workspace / response_record["path"], response_record
            )
            c, r, l, s, q = result
            comments += c; responses += r; links += l; summaries.append(s); review += q
        return comments, responses, links, summaries, review
    if city == "Sunnyvale":
        comment_record = records[0]
        response_record = records[1] if len(records) > 1 else None
        return extract_sunnyvale(
            workspace / comment_record["path"],
            comment_record,
            workspace / response_record["path"] if response_record else None,
            response_record,
        )
    raise ValueError(f"No incremental extractor for city: {city}")


def run_incremental(
    workspace: Path,
    audit_dir: Path,
    output_dir: Path,
    review_decisions: Path | None,
) -> dict[str, int]:
    workspace = workspace.resolve()
    audit_dir = audit_dir.resolve()
    output_dir = output_dir.resolve()
    dataset, existing_review = load_existing(output_dir)
    inventory, summaries = base.load_audit(audit_dir)
    groups = selected_groups(summaries, inventory)
    processed = set(dataset.get("processed_source_paths", []))
    if not processed:
        processed.update(
            row["source_document"]
            for kind in ("comments", "responses")
            for row in dataset.get(kind, [])
        )
        for source in dataset.get("sources", []):
            processed.update(
                item.strip()
                for item in source.get("source_document", "").split(" | ")
                if item.strip()
            )
    comments = list(dataset.get("comments", []))
    responses = list(dataset.get("responses", []))
    links = list(dataset.get("comment_response_links", []))
    source_rows = list(dataset.get("sources", []))
    review_rows: list[dict[str, Any]] = list(existing_review)
    new_groups = 0
    reused_groups = 0
    new_comments = 0
    for summary, records in groups:
        paths = {record["path"] for record in records}
        if paths.issubset(processed):
            reused_groups += 1
            continue
        result = process_new_group(workspace, summary, records)
        c, r, l, s, q = result
        comments.extend(c)
        responses.extend(r)
        links.extend(l)
        source_rows.extend(s)
        review_rows.extend(q)
        processed.update(paths)
        new_groups += 1
        new_comments += len(c)
    base.validate_dataset(comments, responses, links)
    decisions = base.load_review_decision(review_decisions)
    base.apply_review_decision(comments, responses, links, review_rows, decisions)
    comments.sort(key=lambda row: (
        row["city"], row["property_project"], base.natural_number(row["review_round"]),
        row["source_document"], base.natural_number(row["source_row"]),
        base.natural_number(row["source_page"]), base.natural_number(row["comment_number"]),
    ))
    order = {row["comment_id"]: index for index, row in enumerate(comments)}
    responses.sort(key=lambda row: order[row["comment_id"]])
    links.sort(key=lambda row: order[row["comment_id"]])
    review_rows.sort(key=lambda row: (
        row["source_document"], base.natural_number(row["source_location"]),
        row["item_id"],
    ))
    base.write_csv(output_dir / "comments.csv", comments, base.COMMENT_FIELDS)
    base.write_csv(output_dir / "responses.csv", responses, base.RESPONSE_FIELDS)
    base.write_csv(output_dir / "comment_response_links.csv", links, base.LINK_FIELDS)
    base.write_csv(output_dir / "source_summary.csv", source_rows, base.SOURCE_FIELDS)
    base.write_csv(output_dir / "extraction_review.csv", review_rows, base.REVIEW_FIELDS)
    updated = {
        "schema_version": "1.1",
        "comments": comments,
        "responses": responses,
        "comment_response_links": links,
        "sources": source_rows,
        "review_items": review_rows,
        "review_decisions": decisions,
        "processed_source_paths": sorted(processed),
    }
    (output_dir / "dataset.json").write_text(
        json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    base.write_report(
        output_dir / "phase2_report.md",
        comments, responses, links, source_rows, review_rows,
    )
    return {
        "reused_groups": reused_groups,
        "new_groups": new_groups,
        "new_comments": new_comments,
        "total_comments": len(comments),
        "total_responses": len(responses),
        "matched": sum(row["match_status"] == "matched" for row in links),
        "unmatched": sum(row["match_status"] == "unmatched" for row in links),
        "confirmed_review_items": sum(
            row.get("decision") == "confirmed" for row in review_rows
        ),
        "pending_review_items": sum(not row.get("decision") for row in review_rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--audit-dir", type=Path, default=Path("corpus_audit_output"))
    parser.add_argument("--output", type=Path, default=Path("phase2_dataset"))
    parser.add_argument("--review-decisions", type=Path)
    args = parser.parse_args()
    try:
        result = run_incremental(
            args.workspace_root, args.audit_dir, args.output,
            args.review_decisions,
        )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"Incremental Phase 2 update failed: {exc}", file=__import__("sys").stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
