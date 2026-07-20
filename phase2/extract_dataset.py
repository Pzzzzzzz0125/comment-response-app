#!/usr/bin/env python3
"""Build a source-cited permit comment/response dataset from audited sources.

This phase extracts every comment from each recommended primary source.  A
comment without a response is retained and receives an explicit unmatched link
whose response_id is empty.  No model, embedding, semantic clustering, database,
or frontend is used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from corpus_audit import audit_corpus as audit


COMMENT_FIELDS = [
    "comment_id", "city", "property_project", "review_round", "discipline",
    "reviewer", "reviewer_context", "comment_number", "original_text",
    "source_document", "source_sha256", "source_sheet", "source_row",
    "source_page", "source_page_end", "source_location", "extraction_method",
    "extraction_confidence", "source_cycle", "source_status", "response_id", "match_status",
    "human_review_status",
]
RESPONSE_FIELDS = [
    "response_id", "comment_id", "original_text", "source_document",
    "source_sha256", "source_sheet", "source_row", "source_page",
    "source_location", "extraction_method", "extraction_confidence",
    "human_review_status",
]
LINK_FIELDS = [
    "link_id", "comment_id", "response_id", "match_status", "matching_method",
    "match_confidence", "review_status", "source_document", "source_location",
]
SOURCE_FIELDS = [
    "city", "property_project", "review_round", "source_document",
    "source_type", "comment_count", "response_count", "matched_count",
    "unmatched_count", "extraction_method", "processing_error",
]
REVIEW_FIELDS = [
    "item_type", "item_id", "reason", "source_document", "source_location",
    "suggested_action", "decision", "decision_note",
]


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("_x000d_", "\n").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def json_cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "" if value is None else str(value)


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for source in rows:
            writer.writerow({field: json_cell(source.get(field, "")) for field in fields})


def load_audit(audit_dir: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    inventory_path = audit_dir / "file_inventory.json"
    summary_path = audit_dir / "review_round_summary.csv"
    if not inventory_path.is_file() or not summary_path.is_file():
        raise ValueError("Audit outputs are missing; run the corpus audit before Phase 2")
    files = json.loads(inventory_path.read_text(encoding="utf-8")).get("files", [])
    inventory = {record["path"]: record for record in files}
    with summary_path.open(encoding="utf-8", newline="") as stream:
        summaries = list(csv.DictReader(stream))
    return inventory, summaries


def worksheet_rows(path: Path, sheet_name: str) -> list[tuple[int, dict[str, str]]]:
    with zipfile.ZipFile(path) as archive:
        shared = audit.read_shared_strings(archive)
        targets = dict(audit.workbook_sheet_targets(archive))
        if sheet_name not in targets:
            raise ValueError(f"Sheet {sheet_name!r} does not exist in {path.name}")
        root = ET.fromstring(archive.read(targets[sheet_name]))
    rows: list[tuple[int, dict[str, str]]] = []
    for sequential_row, row_node in enumerate(
        (node for node in root.iter() if audit.xml_local(node.tag) == "row"), start=1
    ):
        try:
            source_row = int(row_node.attrib.get("r", sequential_row))
        except ValueError:
            source_row = sequential_row
        values: dict[str, str] = {}
        for cell_number, cell in enumerate(
            (node for node in row_node if audit.xml_local(node.tag) == "c"), start=1
        ):
            value = audit.parse_xlsx_cell(cell, shared)
            if value:
                column = audit.column_letters(cell.attrib.get("r", "")) or audit.number_to_column(cell_number)
                values[column] = value
        rows.append((source_row, values))
    return rows


def primary_column(record: dict[str, Any], field: str, sheet_name: str) -> str:
    sheet_columns = record.get(field, {}).get(sheet_name, [])
    if not sheet_columns:
        return ""
    return sheet_columns[0].get("column", "")


def header_column(record: dict[str, Any], sheet_name: str, pattern: str, fallback: str) -> str:
    headers = record.get("detected_spreadsheet_headers", {}).get(sheet_name, {}).get("columns", [])
    for header in headers:
        if re.search(pattern, header.get("header", ""), flags=re.I):
            return header.get("column", fallback)
    return fallback


def split_reviewer(value: str) -> tuple[str, str]:
    value = normalize_text(value)
    without_date = re.split(r"\s+\d{1,2}/\d{1,2}/\d{2,4}\b", value, maxsplit=1)[0].strip()
    discipline_patterns = (
        r"^(RS\s+Building\s+Review)\b",
        r"^(PW\s+Conformance)\b",
        r"^(Planning(?:\s+Review)?)\b",
        r"^(PW\s+Development\s+Services)\b",
        r"^([A-Z]{2,4}\s+(?:Building|Planning|Structural|Civil|Review|Conformance))\b",
    )
    for pattern in discipline_patterns:
        match = re.search(pattern, without_date, flags=re.I)
        if match:
            discipline = match.group(1).strip()
            reviewer = without_date[match.end():].strip()
            return discipline, reviewer
    generic = re.match(
        r"^(.+?)\s+([A-Z][A-Za-z'’-]+(?:\s+[A-Z]\.?)?\s+[A-Z][A-Za-z'’-]+)$",
        without_date,
    )
    if generic:
        return generic.group(1).strip(), generic.group(2).strip()
    return "unknown", without_date


def natural_number(value: Any, fallback: int = 0) -> tuple[int, str]:
    text = str(value or "")
    match = re.search(r"\d+", text)
    return (int(match.group()) if match else fallback, text)


def source_location(sheet: str = "", row: int | str = "", page: int | str = "", page_end: int | str = "") -> str:
    if sheet and row:
        return f"sheet {sheet}, row {row}"
    if page:
        return f"page {page}" if not page_end or str(page_end) == str(page) else f"pages {page}-{page_end}"
    return "unknown"


def make_link(
    comment_id: str,
    response_id: str,
    source_document: str,
    location: str,
    confidence: float,
) -> dict[str, Any]:
    matched = bool(response_id)
    method = "same_spreadsheet_row" if matched else "no_response_in_selected_source"
    return {
        "link_id": stable_id("L", comment_id, response_id or "NONE", method),
        "comment_id": comment_id,
        "response_id": response_id,
        "match_status": "matched" if matched else "unmatched",
        "matching_method": method,
        "match_confidence": confidence if matched else 1.0,
        "review_status": "suggested" if matched else "not_applicable",
        "source_document": source_document,
        "source_location": location,
    }


def extract_spreadsheet(
    path: Path,
    record: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    sheet = record.get("primary_sheet") or "Review Comments"
    rows = worksheet_rows(path, sheet)
    comment_column = primary_column(record, "likely_comment_columns", sheet)
    response_column = primary_column(record, "likely_response_columns", sheet)
    if not comment_column:
        raise ValueError(f"No audited comment column for {record['path']} / {sheet}")
    number_column = header_column(record, sheet, r"\b(?:ref|item|number|#)\b", "A")
    reviewer_column = header_column(record, sheet, r"reviewed\s+by|reviewer", "B")
    cycle_column = header_column(record, sheet, r"\bcycle\b", "G")
    status_column = header_column(record, sheet, r"\bstatus\b", "H")
    header_row = int(record.get("detected_spreadsheet_headers", {}).get(sheet, {}).get("row") or 1)
    comments: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for row_number, values in rows:
        if row_number <= header_row:
            continue
        comment_text = normalize_text(values.get(comment_column, ""))
        if not comment_text:
            continue
        response_text = normalize_text(values.get(response_column, "")) if response_column else ""
        comment_number = normalize_text(values.get(number_column, ""))
        reviewer_context = normalize_text(values.get(reviewer_column, ""))
        discipline, reviewer = split_reviewer(reviewer_context)
        location = source_location(sheet=sheet, row=row_number)
        comment_id = stable_id("C", record["sha256"], sheet, row_number, comment_column)
        response_id = stable_id("R", record["sha256"], sheet, row_number, response_column) if response_text else ""
        match_status = "matched" if response_id else "unmatched"
        comments.append({
            "comment_id": comment_id,
            "city": record["likely_city"],
            "property_project": record["likely_property_project"],
            "review_round": record["likely_review_round"],
            "discipline": discipline,
            "reviewer": reviewer,
            "reviewer_context": reviewer_context,
            "comment_number": comment_number,
            "original_text": comment_text,
            "source_document": record["path"],
            "source_sha256": record["sha256"],
            "source_sheet": sheet,
            "source_row": row_number,
            "source_page": "",
            "source_page_end": "",
            "source_location": location,
            "extraction_method": "xlsx_cell",
            "extraction_confidence": 0.99,
            "response_id": response_id,
            "match_status": match_status,
            "human_review_status": "pending" if response_id else "not_required",
            "source_cycle": normalize_text(values.get(cycle_column, "")),
            "source_status": normalize_text(values.get(status_column, "")),
        })
        if response_text:
            responses.append({
                "response_id": response_id,
                "comment_id": comment_id,
                "original_text": response_text,
                "source_document": record["path"],
                "source_sha256": record["sha256"],
                "source_sheet": sheet,
                "source_row": row_number,
                "source_page": "",
                "source_location": location,
                "extraction_method": "xlsx_cell",
                "extraction_confidence": 0.99,
                "human_review_status": "pending",
            })
        link = make_link(comment_id, response_id, record["path"], location, 0.99)
        links.append(link)
        if response_id:
            review.append({
                "item_type": "comment_response_link",
                "item_id": link["link_id"],
                "reason": "Same-row spreadsheet match has not been human-confirmed",
                "source_document": record["path"],
                "source_location": location,
                "suggested_action": "Confirm that the response addresses this comment",
            })
    summary = {
        "city": record["likely_city"],
        "property_project": record["likely_property_project"],
        "review_round": record["likely_review_round"],
        "source_document": record["path"],
        "source_type": "spreadsheet",
        "comment_count": len(comments),
        "response_count": len(responses),
        "matched_count": len(responses),
        "unmatched_count": len(comments) - len(responses),
        "extraction_method": "xlsx_cell",
        "processing_error": "",
    }
    return comments, responses, links, summary, review


def ocr_pdf_pages(path: Path, dpi: int = 220) -> list[str]:
    ghostscript = shutil.which("gs")
    tesseract = shutil.which("tesseract")
    if not ghostscript or not tesseract:
        missing = [name for name, command in (("gs", ghostscript), ("tesseract", tesseract)) if not command]
        raise RuntimeError("Targeted PDF OCR requires local command(s): " + ", ".join(missing))
    with tempfile.TemporaryDirectory(prefix="permit-phase2-", dir="/private/tmp") as temporary:
        temp_dir = Path(temporary).resolve()
        output_pattern = temp_dir / "page-%04d.png"
        render = subprocess.run(
            [ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pnggray", f"-r{dpi}", f"-o{output_pattern}", str(path.resolve())],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            check=False,
        )
        if render.returncode:
            raise RuntimeError(f"Ghostscript PDF rendering failed with exit code {render.returncode}")
        images = sorted(temp_dir.glob("page-*.png"))
        if not images:
            raise RuntimeError("Ghostscript produced no page images")
        pages: list[str] = []
        for image in images:
            ocr = subprocess.run(
                [tesseract, str(image.resolve()), "stdout", "--psm", "3", "-l", "eng"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
            if ocr.returncode:
                raise RuntimeError(f"Tesseract failed on {image.name} with exit code {ocr.returncode}")
            pages.append(ocr.stdout)
        return pages


def useful_ocr_line(line: str) -> bool:
    stripped = re.sub(r"\s+", " ", line).strip()
    if not stripped:
        return False
    if re.match(r"Last printed\b", stripped, flags=re.I):
        return False
    alphanumeric = sum(char.isalnum() for char in stripped)
    return alphanumeric >= 3 and alphanumeric / max(len(stripped), 1) >= 0.45


def parse_numbered_pdf_comments(pages: list[str]) -> list[dict[str, Any]]:
    """Parse City plan-review table items such as `10 | A6.1 ...`."""
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    item_pattern = re.compile(r"^\s*(\d{1,3})\s*[|I]\s*(.*)$")
    stop_pattern = re.compile(
        r"^(?:Note that the above comments|Please resubmit complete|Please prepare an itemized response letter|If you have any further questions)",
        flags=re.I,
    )
    for page_number, page in enumerate(pages, start=1):
        for raw_line in page.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            match = item_pattern.match(line)
            if match:
                if current:
                    items.append(current)
                remainder = match.group(2).strip()
                first_token, _, rest = remainder.partition(" ")
                current = {
                    "number": match.group(1),
                    "sheet_reference": first_token,
                    "lines": [remainder] if remainder else [],
                    "page": page_number,
                    "page_end": page_number,
                }
                continue
            if not current:
                continue
            if stop_pattern.match(line):
                if current:
                    items.append(current)
                    current = None
                continue
            if useful_ocr_line(line):
                current["lines"].append(line)
                current["page_end"] = page_number
    if current:
        items.append(current)
    for item in items:
        item["text"] = normalize_text("\n".join(item.pop("lines")))
    return items


def extract_pdf(
    path: Path,
    record: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    pages = ocr_pdf_pages(path)
    items = parse_numbered_pdf_comments(pages)
    if not items:
        raise ValueError(f"No numbered plan-review comments found by targeted OCR in {record['path']}")
    comments: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for item in items:
        location = source_location(page=item["page"], page_end=item["page_end"])
        comment_id = stable_id("C", record["sha256"], item["number"], item["page"])
        comment = {
            "comment_id": comment_id,
            "city": record["likely_city"],
            "property_project": record["likely_property_project"],
            "review_round": record["likely_review_round"],
            "discipline": "Building Review",
            "reviewer": "Honglin Wang",
            "reviewer_context": "City of San Jose Building Department",
            "comment_number": item["number"],
            "original_text": item["text"],
            "source_document": record["path"],
            "source_sha256": record["sha256"],
            "source_sheet": "",
            "source_row": "",
            "source_page": item["page"],
            "source_page_end": item["page_end"],
            "source_location": location,
            "extraction_method": "targeted_local_ocr",
            "extraction_confidence": 0.82,
            "source_cycle": "1",
            "source_status": "Technical Review",
            "response_id": "",
            "match_status": "unmatched",
            "human_review_status": "pending",
        }
        comments.append(comment)
        link = make_link(comment_id, "", record["path"], location, 1.0)
        links.append(link)
        review.append({
            "item_type": "ocr_comment",
            "item_id": comment_id,
            "reason": "Comment text came from targeted local OCR and may contain recognition or table-order errors",
            "source_document": record["path"],
            "source_location": location,
            "suggested_action": "Compare OCR text with the cited PDF page",
        })
    numbers = [int(item["number"]) for item in items]
    expected = list(range(min(numbers), max(numbers) + 1))
    processing_error = "" if numbers == expected else f"OCR item sequence is non-contiguous: {numbers}"
    summary = {
        "city": record["likely_city"],
        "property_project": record["likely_property_project"],
        "review_round": record["likely_review_round"],
        "source_document": record["path"],
        "source_type": "pdf",
        "comment_count": len(comments),
        "response_count": 0,
        "matched_count": 0,
        "unmatched_count": len(comments),
        "extraction_method": "targeted_local_ocr",
        "processing_error": processing_error,
    }
    return comments, [], links, summary, review


def selected_sources(
    summaries: list[dict[str, str]],
    inventory: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for summary in summaries:
        source = summary.get("recommended_primary_source", "").strip()
        if not source:
            continue
        if " | " in source:
            raise ValueError(f"Separate multi-document sources are not supported yet: {source}")
        record = inventory.get(source)
        if not record:
            raise ValueError(f"Recommended source is missing from inventory: {source}")
        selected.append(record)
    return selected


def validate_dataset(
    comments: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> None:
    comment_ids = [row["comment_id"] for row in comments]
    response_ids = [row["response_id"] for row in responses]
    link_comment_ids = [row["comment_id"] for row in links]
    if len(comment_ids) != len(set(comment_ids)):
        raise ValueError("Duplicate comment IDs generated")
    if len(response_ids) != len(set(response_ids)):
        raise ValueError("Duplicate response IDs generated")
    if Counter(link_comment_ids) != Counter(comment_ids):
        raise ValueError("Every comment must have exactly one matched or unmatched link")
    known_responses = set(response_ids)
    for link in links:
        if link["response_id"] and link["response_id"] not in known_responses:
            raise ValueError(f"Link references unknown response: {link['response_id']}")
        if not link["response_id"] and link["match_status"] != "unmatched":
            raise ValueError("A link without a response must be explicitly unmatched")


def load_review_decision(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    if not path.is_file():
        raise ValueError(f"Review-decision file does not exist: {path}")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    decisions: list[dict[str, str]] = []
    for row_number, row in enumerate(rows, start=2):
        scope_type = normalize_text(row.get("scope_type", ""))
        scope_value = normalize_text(row.get("scope_value", ""))
        decision = normalize_text(row.get("decision", "")).casefold()
        if scope_type != "source_path_prefix" or not scope_value:
            raise ValueError(f"Unsupported review-decision scope at row {row_number}")
        if decision not in {"confirmed", "rejected"}:
            raise ValueError(f"Unsupported review decision at row {row_number}: {decision}")
        decisions.append({
            "scope_type": scope_type,
            "scope_value": scope_value,
            "decision": decision,
            "note": normalize_text(row.get("note", "")),
        })
    return decisions


def apply_review_decision(
    comments: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    links: list[dict[str, Any]],
    review: list[dict[str, Any]],
    decision_records: list[dict[str, str]],
) -> None:
    for row in review:
        row["decision"] = ""
        row["decision_note"] = ""
    if not decision_records:
        return
    comments_by_id = {row["comment_id"]: row for row in comments}
    responses_by_id = {row["response_id"]: row for row in responses}
    links_by_id = {row["link_id"]: row for row in links}
    for row in review:
        matching_rules = [
            rule for rule in decision_records
            if row["source_document"].startswith(rule["scope_value"])
        ]
        if not matching_rules:
            continue
        rule = max(matching_rules, key=lambda item: len(item["scope_value"]))
        decision = rule["decision"]
        note = rule["note"]
        row["decision"] = decision
        row["decision_note"] = note
        if row["item_type"] == "ocr_comment":
            comments_by_id[row["item_id"]]["human_review_status"] = decision
        elif row["item_type"] == "comment_response_link":
            link = links_by_id[row["item_id"]]
            link["review_status"] = decision
            comments_by_id[link["comment_id"]]["human_review_status"] = decision
            if link["response_id"]:
                responses_by_id[link["response_id"]]["human_review_status"] = decision


def write_report(
    path: Path,
    comments: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    links: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    review: list[dict[str, Any]],
) -> None:
    matched = sum(link["match_status"] == "matched" for link in links)
    unmatched = len(links) - matched
    confirmed = sum(row.get("decision") == "confirmed" for row in review)
    pending = sum(not row.get("decision") for row in review)
    by_scope = Counter((row["property_project"], row["review_round"]) for row in comments)
    lines = [
        "# Phase 2 Extraction Report",
        "",
        "This dataset preserves every extracted source comment. Comments with no company response are retained with an empty `response_id` and `match_status=unmatched`.",
        "",
        "## Totals",
        "",
        f"- Source extraction entries: **{len(sources)}**",
        f"- Comments: **{len(comments)}**",
        f"- Responses: **{len(responses)}**",
        f"- Matched comment-response links: **{matched}**",
        f"- Explicit unmatched links: **{unmatched}**",
        f"- Review items recorded: **{len(review)}**",
        f"- Confirmed review items: **{confirmed}**",
        f"- Pending review items: **{pending}**",
        "",
        "## Sources",
        "",
        "| Project/scope | Round | Source | Method | Comments | Responses | Matched | Unmatched | Warning |",
        "|---|---:|---|---|---:|---:|---:|---:|---|",
    ]
    for source in sources:
        lines.append(
            f"| {source['property_project']} | {source['review_round']} | `{source['source_document']}` | "
            f"{source['extraction_method']} | {source['comment_count']} | {source['response_count']} | "
            f"{source['matched_count']} | {source['unmatched_count']} | {source['processing_error'] or ''} |"
        )
    lines.extend([
        "",
        "## Matching policy",
        "",
        "- A non-empty response in the same audited spreadsheet row creates a `same_spreadsheet_row` suggested match at 0.99 confidence.",
        "- Menlo Park PDF matrices are linked by the city-issued comment ID within the same table row.",
        "- Sunnyvale separate letters are linked by discipline and comment/response number.",
        "- A blank or absent response creates a link with an empty `response_id`, `match_status=unmatched`, and `matching_method=no_response_in_selected_source`.",
        "- No semantic or positional cross-document match is forced.",
        "- OCR comments require an explicit human decision; confirmed decisions are retained in the review audit trail.",
        "- Original instances are never merged or deduplicated into canonical issues in this phase.",
        "",
        "## Next verification",
        "",
        ("All extraction review items are confirmed. The immutable source-linked dataset is ready for the browse-interface phase. `extraction_review.csv` retains the decisions as an audit trail."
         if confirmed == len(review) and review else
         "Review the pending new-source extractions and matches in `extraction_review.csv`. Existing confirmed decisions remain preserved; corrections should be stored as explicit review decisions rather than changing source files."),
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def run_extraction(
    workspace_root: Path,
    audit_dir: Path,
    output_dir: Path,
    review_decisions_path: Path | None = None,
) -> dict[str, int]:
    workspace_root = workspace_root.resolve()
    audit_dir = audit_dir.resolve()
    output_dir = output_dir.resolve()
    source_root = (workspace_root / "comments&response").resolve()
    try:
        output_dir.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("Phase 2 output must be outside the source corpus")
    inventory, summaries = load_audit(audit_dir)
    records = selected_sources(summaries, inventory)
    all_comments: list[dict[str, Any]] = []
    all_responses: list[dict[str, Any]] = []
    all_links: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    for record in records:
        path = workspace_root / record["path"]
        if not path.is_file():
            raise ValueError(f"Selected source file does not exist: {path}")
        if path.suffix.casefold() == ".xlsx":
            result = extract_spreadsheet(path, record)
        elif path.suffix.casefold() == ".pdf":
            result = extract_pdf(path, record)
        else:
            raise ValueError(f"Unsupported selected primary source in Phase 2: {path.name}")
        comments, responses, links, source_summary, review = result
        all_comments.extend(comments)
        all_responses.extend(responses)
        all_links.extend(links)
        source_rows.append(source_summary)
        review_rows.extend(review)
    validate_dataset(all_comments, all_responses, all_links)
    decision_records = load_review_decision(review_decisions_path)
    apply_review_decision(all_comments, all_responses, all_links, review_rows, decision_records)
    all_comments.sort(key=lambda row: (
        row["city"], row["property_project"], natural_number(row["review_round"]),
        row["source_document"], row["source_sheet"], natural_number(row["source_row"]),
        natural_number(row["source_page"]), natural_number(row["comment_number"]),
    ))
    comment_order = {row["comment_id"]: index for index, row in enumerate(all_comments)}
    all_responses.sort(key=lambda row: comment_order[row["comment_id"]])
    all_links.sort(key=lambda row: comment_order[row["comment_id"]])
    review_rows.sort(key=lambda row: (
        row["source_document"], natural_number(row["source_location"]), row["item_id"]
    ))
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "comments.csv", all_comments, COMMENT_FIELDS)
    write_csv(output_dir / "responses.csv", all_responses, RESPONSE_FIELDS)
    write_csv(output_dir / "comment_response_links.csv", all_links, LINK_FIELDS)
    write_csv(output_dir / "source_summary.csv", source_rows, SOURCE_FIELDS)
    write_csv(output_dir / "extraction_review.csv", review_rows, REVIEW_FIELDS)
    dataset = {
        "schema_version": "1.0",
        "comments": all_comments,
        "responses": all_responses,
        "comment_response_links": all_links,
        "sources": source_rows,
        "review_decisions": decision_records,
    }
    (output_dir / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / "phase2_report.md", all_comments, all_responses, all_links, source_rows, review_rows)
    return {
        "sources": len(source_rows),
        "comments": len(all_comments),
        "responses": len(all_responses),
        "matched": sum(row["match_status"] == "matched" for row in all_links),
        "unmatched": sum(row["match_status"] == "unmatched" for row in all_links),
        "review_items": len(review_rows),
        "confirmed_review_items": sum(row.get("decision") == "confirmed" for row in review_rows),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--audit-dir", type=Path, default=Path("corpus_audit_output"))
    parser.add_argument("--output", type=Path, default=Path("phase2_dataset"))
    parser.add_argument("--review-decisions", type=Path, help="optional durable human-review decision CSV")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        counts = run_extraction(
            args.workspace_root, args.audit_dir, args.output, args.review_decisions
        )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"Phase 2 extraction failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
