#!/usr/bin/env python3
"""Read-only permit corpus discovery and document classification.

The script intentionally uses only the Python standard library.  It inventories
source files without modifying them, inspects spreadsheets before other formats,
and writes deterministic CSV/JSON/Markdown reports to a separate directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
import zlib
import zipfile
from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET


SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv", ".ods"}
TEXT_EXTENSIONS = {".txt", ".md", ".rtf"}
DOCUMENT_TYPES = {
    "city_comments",
    "company_response",
    "combined_comment_response",
    "correction_notice",
    "review_letter",
    "drawing_or_plan",
    "supporting_document",
    "unknown",
}
DISCOVERY_STATUSES = {
    "spreadsheet_source_found",
    "dedicated_comment_response_source_found",
    "separate_comment_and_response_sources_found",
    "comments_found_response_missing",
    "response_found_comments_missing",
    "no_structured_source_drawings_only",
    "ambiguous_needs_review",
}

COMMENT_TERMS = (
    "comment", "comments", "city comment", "review comment", "plan check",
    "plan review", "correction", "corrections", "correction notice",
    "review notes", "markups", "checker comment", "disposition",
)
RESPONSE_TERMS = (
    "response", "responses", "applicant response", "consultant response",
    "response letter", "resubmittal response", "our response", "reply",
    "addressed", "resolved",
)
HEADER_TERMS = (
    "ref", "reference", "item", "number", "reviewed by", "reviewer",
    "type", "view", "comment", "response", "discussion", "cycle", "status",
    "department", "discipline", "sheet", "page", "date", "description",
)
DRAWING_TERMS = (
    "plan set", "planset", "drawing", "drawings", "sheet set", "map",
    "grading plan", "architectural", "diagram", "detail sheet", "civil plan",
)
SUPPORTING_TERMS = (
    "calculation", "calculations", "report", "study", "specification",
    "geotechnical", "survey", "application", "form", "worksheet",
)
CITY_ALIASES = {
    "san jose": "San Jose",
    "sanjose": "San Jose",
    "palo alto": "Palo Alto",
    "paloalto": "Palo Alto",
    "sunnyvale": "Sunnyvale",
    "santa clara": "Santa Clara",
    "santaclara": "Santa Clara",
    "fremont": "Fremont",
    "milpitas": "Milpitas",
    "cupertino": "Cupertino",
    "mountain view": "Mountain View",
    "mountainview": "Mountain View",
    "redwood city": "Redwood City",
    "redwoodcity": "Redwood City",
    "menlo park": "Menlo Park",
    "menlopark": "Menlo Park",
}

INVENTORY_FIELDS = [
    "path", "absolute_path", "parent_folder", "filename", "extension",
    "file_size_bytes", "sha256", "likely_city", "city_confidence",
    "city_evidence", "likely_property_project", "property_confidence",
    "property_evidence", "likely_review_round", "review_round_confidence",
    "review_round_evidence", "document_type", "is_spreadsheet_table_source",
    "likely_contains_city_comments", "likely_contains_company_responses",
    "likely_contains_both", "appears_drawing_heavy",
    "text_extraction_succeeded", "page_count", "sheet_count", "sheet_names",
    "detected_spreadsheet_headers", "likely_comment_columns",
    "likely_response_columns", "primary_sheet", "page_text_character_counts",
    "classification_confidence", "classification_evidence", "processing_error",
    "manual_override_applied", "manual_override_note",
    "source_mtime_ns", "audit_cache_status",
]

OVERRIDE_BOOL_FIELDS = {
    "is_spreadsheet_table_source", "likely_contains_city_comments",
    "likely_contains_company_responses", "likely_contains_both",
    "appears_drawing_heavy",
}
OVERRIDE_FLOAT_FIELDS = {
    "city_confidence", "property_confidence", "review_round_confidence",
    "classification_confidence",
}
OVERRIDE_TEXT_FIELDS = {
    "likely_city", "likely_property_project", "likely_review_round",
    "document_type",
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def json_cell(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_text(blob: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return blob.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return blob.decode("utf-8", errors="replace")


def keyword_hits(text: str, terms: Iterable[str]) -> list[str]:
    lower = text.casefold()
    return sorted({
        term for term in terms
        if re.search(rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])", lower)
    })


def confidence_label(score: float) -> str:
    if score >= 0.82:
        return "high"
    if score >= 0.58:
        return "medium"
    return "low"


def parse_override_bool(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"invalid override boolean: {value!r}")


def load_overrides(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    if not path.is_file():
        raise ValueError(f"Override file does not exist: {path}")
    overrides: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream), start=2):
            item_path = normalize_space(row.get("path", ""))
            if not item_path:
                raise ValueError(f"Override row {row_number} has no path")
            if item_path in overrides:
                raise ValueError(f"Duplicate override path at row {row_number}: {item_path}")
            overrides[item_path] = row
    return overrides


def apply_override(record: dict[str, Any], override: dict[str, str] | None) -> None:
    record["manual_override_applied"] = False
    record["manual_override_note"] = ""
    if not override:
        return
    for field in OVERRIDE_TEXT_FIELDS:
        value = normalize_space(override.get(field, ""))
        if not value:
            continue
        if field == "document_type" and value not in DOCUMENT_TYPES:
            raise ValueError(f"Invalid document_type override for {record['path']}: {value}")
        record[field] = value
    for field in OVERRIDE_FLOAT_FIELDS:
        value = normalize_space(override.get(field, ""))
        if value:
            record[field] = round(float(value), 2)
    for field in OVERRIDE_BOOL_FIELDS:
        value = normalize_space(override.get(field, ""))
        if value:
            record[field] = parse_override_bool(value)
    note = normalize_space(override.get("note", ""))
    record["manual_override_applied"] = True
    record["manual_override_note"] = note
    evidence = record.setdefault("classification_evidence", [])
    evidence.append("human-verified override" + (f": {note}" if note else ""))


def column_letters(cell_reference: str) -> str:
    match = re.match(r"([A-Za-z]+)", cell_reference or "")
    return match.group(1).upper() if match else ""


def number_to_column(number: int) -> str:
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root:
        if xml_local(item.tag) != "si":
            continue
        values.append(normalize_space("".join(node.text or "" for node in item.iter() if xml_local(node.tag) == "t")))
    return values


def workbook_sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
        for rel in relationships
    }
    sheets: list[tuple[str, str]] = []
    for node in workbook.iter():
        if xml_local(node.tag) != "sheet":
            continue
        rel_id = next((v for k, v in node.attrib.items() if xml_local(k) == "id"), "")
        target = rel_targets.get(rel_id, "")
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")
        parts: list[str] = []
        for part in target.split("/"):
            if part == "..":
                if parts:
                    parts.pop()
            elif part not in ("", "."):
                parts.append(part)
        sheets.append((node.attrib.get("name", "Unnamed sheet"), "/".join(parts)))
    return sheets


def parse_xlsx_cell(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return normalize_space("".join(n.text or "" for n in cell.iter() if xml_local(n.tag) == "t"))
    value_node = next((n for n in cell if xml_local(n.tag) == "v"), None)
    value = value_node.text if value_node is not None and value_node.text else ""
    if cell_type == "s" and value:
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return value
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return normalize_space(value)


def choose_header(rows: list[dict[str, str]]) -> tuple[int | None, dict[str, str]]:
    best_score = -1.0
    best_index: int | None = None
    best_row: dict[str, str] = {}
    for index, row in enumerate(rows[:15], start=1):
        values = [v for v in row.values() if v]
        if not values:
            continue
        short_values = [value for value in values if len(value) <= 100]
        joined = " ".join(short_values).casefold()
        keyword_count = len(keyword_hits(joined, HEADER_TERMS))
        text_cells = sum(any(char.isalpha() for char in value) for value in values)
        short_cells = sum(len(value) <= 80 for value in values)
        long_cells = sum(len(value) > 150 for value in values)
        total_length_penalty = sum(len(value) for value in values) / 250
        score = (
            keyword_count * 8 + min(len(values), 10) + text_cells
            + short_cells * 0.5 - long_cells * 6 - total_length_penalty - index * 0.15
        )
        if score > best_score:
            best_score = score
            best_index = index
            best_row = row
    return best_index, best_row


def inspect_xlsx(path: Path, row_limit: int = 30) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sheet_count": 0,
        "sheet_names": [],
        "headers": {},
        "comment_columns": {},
        "response_columns": {},
        "primary_sheet": "",
        "sample_signals": "",
        "text_extraction_succeeded": False,
        "processing_error": "",
    }
    try:
        with zipfile.ZipFile(path) as archive:
            shared = read_shared_strings(archive)
            targets = workbook_sheet_targets(archive)
            result["sheet_count"] = len(targets)
            result["sheet_names"] = [name for name, _ in targets]
            signal_parts: list[str] = []
            primary_score = -1
            for sheet_name, target in targets:
                root = ET.fromstring(archive.read(target))
                rows: list[dict[str, str]] = []
                for row_node in (n for n in root.iter() if xml_local(n.tag) == "row"):
                    values: dict[str, str] = {}
                    for cell_number, cell in enumerate(
                        (n for n in row_node if xml_local(n.tag) == "c"), start=1
                    ):
                        value = parse_xlsx_cell(cell, shared)
                        if value:
                            column = column_letters(cell.attrib.get("r", "")) or number_to_column(cell_number)
                            values[column] = value
                    if values:
                        rows.append(values)
                    if len(rows) >= row_limit:
                        break
                header_index, header_row = choose_header(rows)
                headers = [
                    {"column": column, "header": value}
                    for column, value in sorted(header_row.items())
                ]
                data_rows = rows[header_index:] if header_index is not None else rows
                comment_columns = []
                response_columns = []
                for header in headers:
                    column = header["column"]
                    header_text = header["header"]
                    data_values = [row[column] for row in data_rows if row.get(column)]
                    data_text = " ".join(data_values[:20])
                    header_comment = bool(keyword_hits(header_text, COMMENT_TERMS))
                    header_response = bool(keyword_hits(header_text, RESPONSE_TERMS))
                    data_comment = any(
                        re.match(r"\s*(?:comment|markup)\b", value, flags=re.I)
                        for value in data_values
                    )
                    # An empty response template column is not evidence that responses exist.
                    if header_response and data_values:
                        response_columns.append(header)
                    elif header_comment and data_values:
                        comment_columns.append(header)
                    elif data_comment:
                        comment_columns.append(header)
                if headers:
                    result["headers"][sheet_name] = {"row": header_index, "columns": headers}
                if comment_columns:
                    result["comment_columns"][sheet_name] = comment_columns
                if response_columns:
                    result["response_columns"][sheet_name] = response_columns
                limited_values = [value for row in rows for value in row.values()]
                signal_text = " ".join(limited_values)
                comments = keyword_hits(signal_text, COMMENT_TERMS)
                responses = keyword_hits(signal_text, RESPONSE_TERMS)
                signal_parts.extend(comments + responses)
                score = len(comment_columns) * 8 + len(response_columns) * 8 + len(comments) * 2 + len(responses) * 2 + len(rows) / 100
                if score > primary_score:
                    primary_score = score
                    result["primary_sheet"] = sheet_name
            result["sample_signals"] = " ".join(sorted(set(signal_parts)))
            result["text_extraction_succeeded"] = bool(targets)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        result["processing_error"] = f"XLSX inspection failed: {type(exc).__name__}: {exc}"
    return result


def inspect_delimited(path: Path) -> dict[str, Any]:
    delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
    result = {
        "sheet_count": 1, "sheet_names": [path.stem], "headers": {},
        "comment_columns": {}, "response_columns": {}, "primary_sheet": path.stem,
        "sample_signals": "", "text_extraction_succeeded": False,
        "processing_error": "",
    }
    try:
        text = safe_text(path.read_bytes())
        rows = list(csv.reader(text.splitlines()[:30], delimiter=delimiter))
        header = rows[0] if rows else []
        headers = [{"column": str(i + 1), "header": normalize_space(v)} for i, v in enumerate(header) if v.strip()]
        result["headers"] = {path.stem: {"row": 1, "columns": headers}}
        result["comment_columns"] = {path.stem: [h for h in headers if keyword_hits(h["header"], COMMENT_TERMS)]}
        result["response_columns"] = {path.stem: [h for h in headers if keyword_hits(h["header"], RESPONSE_TERMS)]}
        result["sample_signals"] = " ".join(keyword_hits(" ".join(" ".join(r) for r in rows), COMMENT_TERMS + RESPONSE_TERMS))
        result["text_extraction_succeeded"] = True
    except (OSError, csv.Error) as exc:
        result["processing_error"] = f"Delimited file inspection failed: {type(exc).__name__}: {exc}"
    return result


def inspect_ods(path: Path, row_limit: int = 30) -> dict[str, Any]:
    result = {
        "sheet_count": 0, "sheet_names": [], "headers": {}, "comment_columns": {},
        "response_columns": {}, "primary_sheet": "", "sample_signals": "",
        "text_extraction_succeeded": False, "processing_error": "",
    }
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("content.xml"))
        tables = [node for node in root.iter() if xml_local(node.tag) == "table"]
        signals: list[str] = []
        for table_index, table in enumerate(tables, start=1):
            name = next((v for k, v in table.attrib.items() if xml_local(k) == "name"), f"Sheet {table_index}")
            result["sheet_names"].append(name)
            rows: list[dict[str, str]] = []
            for row in (n for n in table if xml_local(n.tag) == "table-row"):
                values: dict[str, str] = {}
                col = 0
                for cell in (n for n in row if xml_local(n.tag) in {"table-cell", "covered-table-cell"}):
                    col += 1
                    value = normalize_space(" ".join(n.text or "" for n in cell.iter() if xml_local(n.tag) == "p"))
                    if value:
                        values[str(col)] = value
                if values:
                    rows.append(values)
                if len(rows) >= row_limit:
                    break
            header_index, header_row = choose_header(rows)
            headers = [{"column": col, "header": val} for col, val in header_row.items()]
            result["headers"][name] = {"row": header_index, "columns": headers}
            result["comment_columns"][name] = [h for h in headers if keyword_hits(h["header"], COMMENT_TERMS)]
            result["response_columns"][name] = [h for h in headers if keyword_hits(h["header"], RESPONSE_TERMS)]
            signals.extend(keyword_hits(" ".join(v for r in rows for v in r.values()), COMMENT_TERMS + RESPONSE_TERMS))
        result["sheet_count"] = len(tables)
        result["primary_sheet"] = result["sheet_names"][0] if tables else ""
        result["sample_signals"] = " ".join(sorted(set(signals)))
        result["text_extraction_succeeded"] = bool(tables)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        result["processing_error"] = f"ODS inspection failed: {type(exc).__name__}: {exc}"
    return result


def decode_pdf_literal(value: bytes) -> str:
    output = bytearray()
    index = 0
    escapes = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f"}
    while index < len(value):
        byte = value[index]
        if byte != 92:
            output.append(byte)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        escaped = value[index]
        if escaped in escapes:
            output.extend(escapes[escaped])
            index += 1
        elif escaped in b"()\\":
            output.append(escaped)
            index += 1
        elif escaped in b"\r\n":
            if escaped == 13 and index + 1 < len(value) and value[index + 1] == 10:
                index += 1
            index += 1
        elif 48 <= escaped <= 55:
            digits = bytes([escaped])
            index += 1
            for _ in range(2):
                if index < len(value) and 48 <= value[index] <= 55:
                    digits += bytes([value[index]])
                    index += 1
                else:
                    break
            output.append(int(digits, 8))
        else:
            output.append(escaped)
            index += 1
    if output.startswith((b"\xfe\xff", b"\xff\xfe")):
        try:
            return output.decode("utf-16")
        except UnicodeDecodeError:
            pass
    return output.decode("latin-1", errors="ignore")


def text_from_pdf_stream(stream: bytes) -> str:
    fragments: list[str] = []
    for block in re.findall(rb"BT(.*?)ET", stream, flags=re.DOTALL):
        for match in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*(?:Tj|'|\")", block, flags=re.DOTALL):
            literal = match.group(0)
            fragments.append(decode_pdf_literal(literal[1:literal.rfind(b")")]))
        for array in re.findall(rb"\[(.*?)\]\s*TJ", block, flags=re.DOTALL):
            for literal in re.findall(rb"\((?:\\.|[^\\)])*\)", array, flags=re.DOTALL):
                fragments.append(decode_pdf_literal(literal[1:-1]))
            for hex_value in re.findall(rb"<([0-9A-Fa-f\s]+)>", array):
                try:
                    raw = bytes.fromhex(re.sub(rb"\s+", b"", hex_value).decode("ascii"))
                    fragments.append(safe_text(raw))
                except ValueError:
                    continue
        for hex_value in re.findall(rb"<([0-9A-Fa-f\s]+)>\s*Tj", block):
            try:
                fragments.append(safe_text(bytes.fromhex(re.sub(rb"\s+", b"", hex_value).decode("ascii"))))
            except ValueError:
                continue
    text = " ".join(fragments)
    return normalize_space("".join(char if char.isprintable() else " " for char in text))


def inspect_pdf(path: Path) -> dict[str, Any]:
    result = {
        "page_count": None, "page_text_character_counts": [], "text": "",
        "text_extraction_succeeded": False, "processing_error": "",
    }
    try:
        data = path.read_bytes()
        objects: dict[int, bytes] = {}
        for match in re.finditer(rb"(?ms)(\d+)\s+\d+\s+obj\b(.*?)endobj", data):
            objects[int(match.group(1))] = match.group(2)
        page_objects = [body for body in objects.values() if re.search(rb"/Type\s*/Page\b", body)]
        result["page_count"] = len(page_objects) or max(
            [int(v) for v in re.findall(rb"/Count\s+(\d+)", data)] or [0]
        ) or None
        all_text: list[str] = []
        page_counts: list[int] = []
        for page in page_objects:
            refs: list[int] = []
            content_array = re.search(rb"/Contents\s*\[(.*?)\]", page, flags=re.DOTALL)
            if content_array:
                refs.extend(int(value) for value in re.findall(rb"(\d+)\s+\d+\s+R", content_array.group(1)))
            else:
                single = re.search(rb"/Contents\s+(\d+)\s+\d+\s+R", page)
                if single:
                    refs.append(int(single.group(1)))
            page_text: list[str] = []
            for reference in refs:
                body = objects.get(reference, b"")
                stream_match = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, flags=re.DOTALL)
                if not stream_match:
                    continue
                stream = stream_match.group(1)
                if b"/FlateDecode" in body:
                    try:
                        stream = zlib.decompress(stream)
                    except zlib.error:
                        continue
                page_text.append(text_from_pdf_stream(stream))
            joined = normalize_space(" ".join(page_text))
            page_counts.append(len(joined))
            if joined:
                all_text.append(joined)
        result["page_text_character_counts"] = page_counts
        result["text"] = normalize_space(" ".join(all_text))[:200_000]
        result["text_extraction_succeeded"] = bool(result["text"])
        if not objects:
            result["processing_error"] = "PDF structure could not be inspected (object streams or invalid PDF)"
        elif not result["text_extraction_succeeded"]:
            result["processing_error"] = "No locally extractable PDF text; possible scan, drawing, or unsupported encoding"
    except OSError as exc:
        result["processing_error"] = f"PDF inspection failed: {type(exc).__name__}: {exc}"
    return result


def inspect_docx(path: Path) -> dict[str, Any]:
    result = {"text": "", "text_extraction_succeeded": False, "processing_error": ""}
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        text = normalize_space(" ".join(node.text or "" for node in root.iter() if xml_local(node.tag) == "t"))
        result.update(text=text[:200_000], text_extraction_succeeded=bool(text))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        result["processing_error"] = f"DOCX inspection failed: {type(exc).__name__}: {exc}"
    return result


def inspect_eml(path: Path) -> dict[str, Any]:
    result = {"text": "", "text_extraction_succeeded": False, "processing_error": ""}
    try:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        pieces = [str(message.get("subject", ""))]
        for part in message.walk() if message.is_multipart() else [message]:
            if part.get_content_type() != "text/plain":
                continue
            try:
                pieces.append(part.get_content())
            except (LookupError, UnicodeDecodeError):
                payload = part.get_payload(decode=True) or b""
                pieces.append(safe_text(payload))
        text = normalize_space(" ".join(pieces))
        result.update(text=text[:200_000], text_extraction_succeeded=bool(text))
    except (OSError, ValueError) as exc:
        result["processing_error"] = f"EML inspection failed: {type(exc).__name__}: {exc}"
    return result


def infer_city(relative_path: Path, text: str) -> tuple[str, float, list[str]]:
    path_text = " ".join(relative_path.parts).replace("_", " ").replace("-", " ").casefold()
    text_lower = text[:20_000].casefold()
    candidates: dict[str, tuple[float, list[str]]] = {}
    for alias, city in CITY_ALIASES.items():
        score = 0.0
        evidence: list[str] = []
        if alias in path_text:
            score += 0.82
            evidence.append(f"folder/filename contains '{alias}'")
        if alias in text_lower:
            score += 0.16
            evidence.append(f"extracted text contains '{alias}'")
        if score:
            candidates[city] = (min(score, 0.98), evidence)
    if not candidates:
        return "unknown", 0.0, ["no recognized city alias"]
    city, (score, evidence) = max(candidates.items(), key=lambda item: item[1][0])
    return city, score, evidence


def prettify_project_component(value: str) -> str:
    words = re.sub(r"[_-]+", " ", value).split()
    return " ".join(word.upper() if re.fullmatch(r"(?:sb|ip|gr|rs)\d*", word, re.I) else word.title() for word in words)


def infer_project(relative_path: Path) -> tuple[str, float, list[str]]:
    parts = list(relative_path.parts)
    if not parts:
        return "unknown", 0.0, ["empty relative path"]
    project_root = parts[0]
    root_clean = re.sub(r"_(?:san[_ ]?jose|palo[_ ]?alto|sunnyvale|santa[_ ]?clara|fremont|milpitas|cupertino|mountain[_ ]?view|redwood[_ ]?city|menlo[_ ]?park)$", "", project_root, flags=re.I)
    root_name = prettify_project_component(root_clean)
    evidence = [f"top-level project folder '{project_root}'"]
    scope_parts: list[str] = []
    for part in parts[1:-1]:
        lower = part.casefold()
        if lower in {"deliverable and submittals", "deliverables and submittals"}:
            continue
        if re.search(r"\b(?:1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth)\b.*\b(?:comment|review|round|submission)", lower):
            break
        scope_parts.append(prettify_project_component(part))
    if root_name:
        name = root_name
        confidence = 0.92 if re.search(r"\d", root_name) else 0.76
        if scope_parts:
            name += " — " + " / ".join(scope_parts)
            evidence.append("permit scope inferred from folders before review-round folder")
        folder_text = " ".join(relative_path.parts[:-1])
        folder_lot = re.search(r"\blot[ _-]*(\d+)\b", folder_text, flags=re.I)
        filename_lot = re.search(r"\blot[ _-]*(\d+)\b", relative_path.name, flags=re.I)
        if folder_lot and filename_lot and folder_lot.group(1) != filename_lot.group(1):
            evidence.append(
                f"folder lot {folder_lot.group(1)} conflicts with filename lot {filename_lot.group(1)}"
            )
            confidence = 0.55
        return name, confidence, evidence
    return "unknown", 0.0, ["project folder could not be normalized"]


ROUND_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}


def ordinal_before(text: str, phrase: str) -> int | None:
    numeric = re.search(rf"\b(\d+)(?:st|nd|rd|th)?\s+{phrase}", text, flags=re.I)
    if numeric:
        return int(numeric.group(1))
    for word, value in ROUND_WORDS.items():
        if re.search(rf"\b{word}\s+{phrase}", text, flags=re.I):
            return value
    return None


def infer_round(relative_path: Path) -> tuple[str, float, list[str]]:
    folders = list(relative_path.parts[:-1])
    for part in reversed(folders):
        lower = part.casefold()
        value = ordinal_before(
            lower,
            r"(?:round\s+of\s+comments|round\s+comments|comments?|review)",
        )
        if value is not None:
            return str(value), 0.96, [f"review folder '{part}'"]
    for part in reversed(folders):
        value = ordinal_before(part.casefold(), r"round\s+submission\s+package")
        if value is not None:
            return str(value), 0.94, [f"response package folder '{part}'"]
    for part in reversed(folders):
        value = ordinal_before(part.casefold(), r"submission\s+package")
        if value is not None:
            review_round = max(1, value - 1)
            return str(review_round), 0.9, [
                f"submission package '{part}' inferred to answer review round {review_round}"
            ]
    for part in reversed(folders):
        value = ordinal_before(part.casefold(), r"submission")
        if value is not None:
            review_round = max(1, value - 1)
            return str(review_round), 0.82, [
                f"submission folder '{part}' inferred to contain review round {review_round}"
            ]
    filename = relative_path.name.casefold()
    match = re.search(r"\b(\d+)(?:st|nd|rd|th)\s+(?:review|round|comment)", filename)
    if match:
        return match.group(1), 0.68, [f"filename '{relative_path.name}'"]
    return "unknown", 0.0, ["no reliable review-round marker"]


def classify_document(
    path: Path,
    extracted_text: str,
    spreadsheet: dict[str, Any] | None,
    page_counts: list[int],
    page_count: int | None,
) -> dict[str, Any]:
    name = path.name.casefold().replace("_", " ").replace("-", " ")
    signal_text = f"{name} {extracted_text[:40_000]}"
    extracted_lower = extracted_text[:40_000].casefold()
    comment_hits = keyword_hits(signal_text, COMMENT_TERMS)
    response_hits = keyword_hits(signal_text, RESPONSE_TERMS)
    drawing_hits = keyword_hits(name, DRAWING_TERMS)
    support_hits = keyword_hits(name, SUPPORTING_TERMS)
    comment_columns = []
    response_columns = []
    if spreadsheet:
        comment_columns = [item for values in spreadsheet.get("comment_columns", {}).values() for item in values]
        response_columns = [item for values in spreadsheet.get("response_columns", {}).values() for item in values]
    dedicated_comment_text = bool(re.search(
        r"(?:our comments follow|comments are as follows|plan review comments|"
        r"completed.{0,120}review.{0,240}comments|corrections required)",
        extracted_lower,
        flags=re.DOTALL,
    ))
    comment_score = min(1.0, (0.58 if comment_columns else 0) + (0.55 if keyword_hits(name, COMMENT_TERMS) else 0) + (0.62 if dedicated_comment_text else 0) + min(len(comment_hits), 3) * 0.08)
    response_score = min(1.0, (0.58 if response_columns else 0) + (0.55 if keyword_hits(name, RESPONSE_TERMS) else 0) + min(len(response_hits), 3) * 0.08)
    is_spreadsheet = path.suffix.casefold() in SPREADSHEET_EXTENSIONS
    useful_spreadsheet = is_spreadsheet and (comment_score >= 0.5 or response_score >= 0.5)

    low_page_ratio = 0.0
    median_chars = 0.0
    if page_counts:
        low_page_ratio = sum(value < 80 for value in page_counts) / len(page_counts)
        median_chars = statistics.median(page_counts)
    appears_drawing_heavy = bool(drawing_hits) or (
        path.suffix.casefold() == ".pdf"
        and bool(page_count)
        and (low_page_ratio >= 0.7 or (page_count >= 8 and median_chars < 120))
        and not keyword_hits(name, COMMENT_TERMS + RESPONSE_TERMS)
    )

    evidence: list[str] = []
    if comment_columns:
        evidence.append("spreadsheet has comment-like header(s): " + ", ".join(sorted({item["header"] for item in comment_columns})))
    if response_columns:
        evidence.append("spreadsheet has response-like header(s): " + ", ".join(sorted({item["header"] for item in response_columns})))
    if keyword_hits(name, COMMENT_TERMS):
        evidence.append("filename has comment/review terminology")
    if keyword_hits(name, RESPONSE_TERMS):
        evidence.append("filename has response terminology")
    if drawing_hits:
        evidence.append("filename has drawing/support terminology: " + ", ".join(drawing_hits))
    if page_counts:
        evidence.append(f"PDF text-density sample: median {int(median_chars)} chars/page; {low_page_ratio:.0%} low-text pages")

    suffix = path.suffix.casefold()
    if comment_score >= 0.5 and response_score >= 0.5:
        document_type = "combined_comment_response"
        confidence = max(comment_score, response_score)
    elif response_score >= 0.5:
        document_type = "company_response"
        confidence = response_score
    elif "correction notice" in signal_text or "corrections notice" in signal_text:
        document_type = "correction_notice"
        confidence = max(0.7, comment_score)
    elif appears_drawing_heavy:
        document_type = "drawing_or_plan"
        confidence = 0.82 if drawing_hits else 0.66
    elif comment_score >= 0.5:
        document_type = "city_comments"
        confidence = comment_score
    elif support_hits:
        document_type = "supporting_document"
        confidence = 0.72
    elif suffix in {".pdf", ".docx", ".doc", ".eml"} and ("review" in name or "letter" in name):
        document_type = "review_letter"
        confidence = 0.55
    else:
        document_type = "unknown"
        confidence = 0.25
        evidence.append("no decisive local classification signal")
    assert document_type in DOCUMENT_TYPES
    return {
        "document_type": document_type,
        "is_spreadsheet_table_source": useful_spreadsheet,
        "likely_contains_city_comments": comment_score >= 0.5,
        "likely_contains_company_responses": response_score >= 0.5,
        "likely_contains_both": comment_score >= 0.5 and response_score >= 0.5,
        "appears_drawing_heavy": appears_drawing_heavy,
        "classification_confidence": round(confidence, 2),
        "classification_evidence": evidence,
    }


def inspect_file(path: Path, source_root: Path, workspace_root: Path) -> dict[str, Any]:
    relative_to_source = path.relative_to(source_root)
    try:
        workspace_path = path.relative_to(workspace_root).as_posix()
    except ValueError:
        workspace_path = str(path.resolve())
    record: dict[str, Any] = {
        "path": workspace_path,
        "absolute_path": str(path.resolve()),
        "parent_folder": str(Path(workspace_path).parent),
        "filename": path.name,
        "extension": path.suffix.casefold(),
        "file_size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "page_count": None,
        "sheet_count": None,
        "sheet_names": [],
        "detected_spreadsheet_headers": {},
        "likely_comment_columns": {},
        "likely_response_columns": {},
        "primary_sheet": "",
        "page_text_character_counts": [],
        "text_extraction_succeeded": False,
        "processing_error": "",
        "source_mtime_ns": path.stat().st_mtime_ns,
        "audit_cache_status": "processed",
    }
    suffix = path.suffix.casefold()
    text = ""
    spreadsheet: dict[str, Any] | None = None
    if suffix == ".xlsx":
        spreadsheet = inspect_xlsx(path)
    elif suffix in {".csv", ".tsv"}:
        spreadsheet = inspect_delimited(path)
    elif suffix == ".ods":
        spreadsheet = inspect_ods(path)
    elif suffix == ".xls":
        spreadsheet = {"processing_error": "Legacy XLS requires an optional parser; filename-only classification used"}

    if spreadsheet is not None:
        record.update(
            sheet_count=spreadsheet.get("sheet_count"),
            sheet_names=spreadsheet.get("sheet_names", []),
            detected_spreadsheet_headers=spreadsheet.get("headers", {}),
            likely_comment_columns=spreadsheet.get("comment_columns", {}),
            likely_response_columns=spreadsheet.get("response_columns", {}),
            primary_sheet=spreadsheet.get("primary_sheet", ""),
            text_extraction_succeeded=spreadsheet.get("text_extraction_succeeded", False),
            processing_error=spreadsheet.get("processing_error", ""),
        )
        text = spreadsheet.get("sample_signals", "")
    elif suffix == ".pdf":
        detail = inspect_pdf(path)
        record.update({key: value for key, value in detail.items() if key != "text"})
        text = detail["text"]
    elif suffix == ".docx":
        detail = inspect_docx(path)
        record.update({key: value for key, value in detail.items() if key != "text"})
        text = detail["text"]
    elif suffix == ".eml":
        detail = inspect_eml(path)
        record.update({key: value for key, value in detail.items() if key != "text"})
        text = detail["text"]
    elif suffix in TEXT_EXTENSIONS:
        try:
            text = safe_text(path.read_bytes())[:200_000]
            record["text_extraction_succeeded"] = bool(text)
        except OSError as exc:
            record["processing_error"] = f"Text inspection failed: {type(exc).__name__}: {exc}"
    else:
        record["processing_error"] = "Unsupported format; metadata and filename inspected only"

    city, city_confidence, city_evidence = infer_city(relative_to_source, text)
    project, property_confidence, property_evidence = infer_project(relative_to_source)
    review_round, round_confidence, round_evidence = infer_round(relative_to_source)
    classification = classify_document(
        path, text, spreadsheet, record["page_text_character_counts"], record["page_count"]
    )
    record.update(
        likely_city=city,
        city_confidence=round(city_confidence, 2),
        city_evidence=city_evidence,
        likely_property_project=project,
        property_confidence=round(property_confidence, 2),
        property_evidence=property_evidence,
        likely_review_round=review_round,
        review_round_confidence=round(round_confidence, 2),
        review_round_evidence=round_evidence,
        **classification,
    )
    return record


def group_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        record["likely_city"],
        record["likely_property_project"],
        record["likely_review_round"],
    )


def dedicated_kind(record: dict[str, Any]) -> bool:
    return record["document_type"] in {
        "city_comments", "company_response", "combined_comment_response",
        "correction_notice", "review_letter",
    }


def select_primary_source(records: list[dict[str, Any]]) -> tuple[str, str, str]:
    def paths(items: Iterable[dict[str, Any]]) -> list[str]:
        return sorted(item["path"] for item in items)

    spreadsheets = [r for r in records if r["is_spreadsheet_table_source"] and r["likely_contains_both"]]
    if spreadsheets:
        chosen = max(spreadsheets, key=lambda r: (r["classification_confidence"], -len(r["path"])))
        return chosen["path"], "spreadsheet_source_found", "spreadsheet containing both comment and response signals"
    combined = [r for r in records if r["document_type"] == "combined_comment_response"]
    if combined:
        chosen = max(combined, key=lambda r: r["classification_confidence"])
        status = "spreadsheet_source_found" if chosen["is_spreadsheet_table_source"] else "dedicated_comment_response_source_found"
        return chosen["path"], status, "dedicated combined comment-response source"
    comments = [r for r in records if r["likely_contains_city_comments"] and dedicated_kind(r)]
    responses = [r for r in records if r["likely_contains_company_responses"] and dedicated_kind(r)]

    def dedicated_rank(record: dict[str, Any], kind: str) -> tuple[float, str]:
        lower_path = record["path"].casefold()
        filename = record["filename"].casefold()
        score = float(record["classification_confidence"]) * 10
        if "/archive/" not in lower_path:
            score += 5
        else:
            score -= 4
        if kind == "comment":
            if record["extension"] == ".docx":
                score += 4
            if keyword_hits(filename, COMMENT_TERMS):
                score += 2
            if record["appears_drawing_heavy"]:
                score -= 5
        else:
            if "response letter" in filename:
                score += 4
            if any(term in filename for term in ("old", "markup")):
                score -= 5
            if any(term in filename for term in ("arborist", "geotech")):
                score -= 2
        return score, record["path"]

    if comments and responses:
        comment = max(comments, key=lambda r: dedicated_rank(r, "comment"))
        response = max(responses, key=lambda r: dedicated_rank(r, "response"))
        return f"{comment['path']} | {response['path']}", "separate_comment_and_response_sources_found", "separate dedicated comment and response sources"
    if comments:
        chosen = max(comments, key=lambda r: r["classification_confidence"])
        return chosen["path"], "comments_found_response_missing", "comment-only source"
    if responses:
        chosen = max(responses, key=lambda r: r["classification_confidence"])
        return chosen["path"], "response_found_comments_missing", "response-only source"
    non_drawing_unknown = [r for r in records if not r["appears_drawing_heavy"] and r["document_type"] not in {"drawing_or_plan", "supporting_document"}]
    if records and not non_drawing_unknown:
        return "", "no_structured_source_drawings_only", "no structured source; remaining files appear drawing/support-heavy"
    return "", "ambiguous_needs_review", "no reliable primary source selected"


def summarize_rounds(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[group_key(record)].append(record)
    summaries: list[dict[str, Any]] = []
    for key in sorted(groups):
        items = groups[key]
        primary, status, reason = select_primary_source(items)
        assert status in DISCOVERY_STATUSES
        summaries.append({
            "likely_city": key[0],
            "likely_property_project": key[1],
            "likely_review_round": key[2],
            "number_of_files": len(items),
            "spreadsheet_candidates": sum(r["is_spreadsheet_table_source"] for r in items),
            "dedicated_comment_documents": sum(r["document_type"] in {"city_comments", "correction_notice", "review_letter"} and r["likely_contains_city_comments"] for r in items),
            "dedicated_response_documents": sum(r["document_type"] == "company_response" for r in items),
            "combined_comment_response_documents": sum(r["document_type"] == "combined_comment_response" for r in items),
            "drawing_heavy_files": sum(r["appears_drawing_heavy"] for r in items),
            "recommended_primary_source": primary,
            "primary_source_reason": reason,
            "discovery_status": status,
        })
    return summaries


def duplicate_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_hash[record["sha256"]].append(record)
    rows: list[dict[str, Any]] = []
    group_number = 0
    for digest, items in sorted(by_hash.items()):
        if len(items) < 2:
            continue
        group_number += 1
        for item in sorted(items, key=lambda r: r["path"]):
            rows.append({
                "duplicate_group": group_number,
                "sha256": digest,
                "file_size_bytes": item["file_size_bytes"],
                "path": item["path"],
                "group_file_count": len(items),
            })
    return rows


def review_rows(records: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        reasons: list[str] = []
        if record["classification_confidence"] < 0.58 or record["document_type"] == "unknown":
            reasons.append("ambiguous document classification")
        for field, label in (
            ("likely_city", "city"),
            ("likely_property_project", "property/project"),
            ("likely_review_round", "review round"),
        ):
            if record[field] == "unknown":
                reasons.append(f"unknown {label}")
        if record["property_confidence"] < 0.58 and record["likely_property_project"] != "unknown":
            reasons.append("low-confidence property/project inference or folder/filename conflict")
        if record["processing_error"]:
            reasons.append("processing warning/error")
        if record["extension"] == ".pdf" and not record["text_extraction_succeeded"]:
            reasons.append("possible scanned/drawing PDF or unsupported PDF text encoding")
        if record["extension"] in SPREADSHEET_EXTENSIONS and not record["is_spreadsheet_table_source"]:
            reasons.append("spreadsheet lacks clear comment/response signals")
        if reasons:
            rows.append({
                "item_type": "file",
                "path_or_group": record["path"],
                "reasons": " | ".join(reasons),
                "document_type": record["document_type"],
                "confidence": record["classification_confidence"],
                "processing_error": record["processing_error"],
            })
    for summary in summaries:
        if summary["discovery_status"] in {
            "no_structured_source_drawings_only", "ambiguous_needs_review",
            "comments_found_response_missing", "response_found_comments_missing",
        }:
            label = " / ".join((summary["likely_city"], summary["likely_property_project"], f"round {summary['likely_review_round']}"))
            rows.append({
                "item_type": "review_round",
                "path_or_group": label,
                "reasons": summary["discovery_status"],
                "document_type": "",
                "confidence": "",
                "processing_error": "",
            })
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for original in rows:
            row = {}
            for field in fields:
                value = original.get(field, "")
                row[field] = json_cell(value) if isinstance(value, (list, dict)) else value
            writer.writerow(row)


def format_counter(counter: Counter[str]) -> str:
    return ", ".join(f"{key or '[no extension]'}: {value}" for key, value in sorted(counter.items())) or "None"


def write_report(
    path: Path,
    source_root: Path,
    records: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    duplicates: list[dict[str, Any]],
    needs_review: list[dict[str, Any]],
) -> None:
    formats = Counter(r["extension"] or "[none]" for r in records)
    cities = Counter(r["likely_city"] for r in records)
    projects = Counter(r["likely_property_project"] for r in records)
    rounds = Counter(s["discovery_status"] for s in summaries)
    any_spreadsheet_rounds = sum(s["spreadsheet_candidates"] > 0 for s in summaries)
    failures = [r for r in records if r["processing_error"]]
    overridden = [r for r in records if r.get("manual_override_applied")]
    duplicate_groups = len({r["duplicate_group"] for r in duplicates})
    lines = [
        "# Corpus Audit Report",
        "",
        f"Source: `{source_root}`",
        "",
        "This report is a read-only discovery audit. A missing structured source does not prove that drawings contain no comments; those rounds are deferred to a later visual-document phase.",
        "",
        "## Overview",
        "",
        f"- Total source files: **{len(records)}**",
        f"- Formats: {format_counter(formats)}",
        f"- Probable cities: {format_counter(cities)}",
        f"- Probable projects/scopes: **{len(projects)}**",
        f"- Inferred review rounds: **{len(summaries)}**",
        f"- Rounds with any useful spreadsheet source: **{any_spreadsheet_rounds}**",
        f"- Rounds with a combined comment-response spreadsheet: **{rounds['spreadsheet_source_found']}**",
        f"- Other dedicated combined-source rounds: **{rounds['dedicated_comment_response_source_found']}**",
        f"- Separate comment/response rounds: **{rounds['separate_comment_and_response_sources_found']}**",
        f"- Drawing/support-only rounds: **{rounds['no_structured_source_drawings_only']}**",
        f"- Files with processing warnings/errors: **{len(failures)}**",
        f"- Exact duplicate groups: **{duplicate_groups}**",
        f"- Needs-review rows: **{len(needs_review)}**",
        f"- Human-verified file overrides: **{len(overridden)}**",
        "",
        "## Review-round discovery",
        "",
        "| City | Project/scope | Round | Files | Status | Recommended primary source |",
        "|---|---|---:|---:|---|---|",
    ]
    for summary in summaries:
        source = summary["recommended_primary_source"] or "None"
        lines.append(
            f"| {summary['likely_city']} | {summary['likely_property_project']} | {summary['likely_review_round']} | "
            f"{summary['number_of_files']} | `{summary['discovery_status']}` | `{source}` |"
        )
    lines.extend([
        "",
        "## Patterns and assumptions",
        "",
        "- Folder and filename evidence is treated as an inference, never as guaranteed truth; unresolved values remain `unknown`.",
        "- A project scope is built from the top-level property folder plus folders between `deliverable and submittals` and the review-round folder. This keeps building, map, encroachment, grading, and lot streams separate.",
        "- When a folder lot number conflicts with a filename lot number, the folder-based grouping is retained at low confidence and sent to human review unless an explicit human override confirms it.",
        "- Workbooks are inspected before other documents. Reports retain sheet names, detected header row numbers, source column letters, and likely primary sheet.",
        "- PDF extraction is local and conservative. Scanned, font-encoded, object-stream, and drawing-heavy PDFs may produce no text and are placed in `needs_review.csv`; no corpus-wide OCR was run.",
        "- Classification uses only paths, filenames, headers, limited spreadsheet rows, locally extractable text, and PDF text-density signals. No paid model was called.",
        "- Exact duplicates are reported by SHA-256 and are not removed.",
        "- Human verification decisions are loaded from the optional local overrides file; comment-only rounds remain marked response-missing when no response source is found.",
        "",
        "## Human-verified decisions",
        "",
    ])
    if overridden:
        for record in overridden:
            lines.append(
                f"- `{record['path']}` — {record['manual_override_note']}"
            )
    else:
        lines.append("None.")
    lines.extend([
        "",
        "## Extraction warnings/failures",
        "",
    ])
    if failures:
        for record in failures:
            lines.append(f"- `{record['path']}` — {record['processing_error']}")
    else:
        lines.append("None.")
    lines.extend([
        "",
        "## Recommended Phase 2",
        "",
        "Process only the recommended primary sources, in this order: useful spreadsheets; dedicated combined documents; separate comment and response documents; selected scanned documents needing OCR; drawing-heavy plan sets last. Extract immutable source-linked comment and response instances with workbook row or PDF page citations. Then match them with explicit confidence and human-review status. Do not merge originals: introduce canonical issues only after the source-linked extraction and matching dataset is reviewable.",
        "",
        "Phase 2 should not yet build the frontend or response-drafting assistant. Its deliverable should be a reviewable dataset for one city containing original comment/response text, source location, discipline, match confidence/method, and review status.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def run_audit(
    source_root: Path,
    output_dir: Path,
    workspace_root: Path,
    limit: int | None = None,
    overrides_path: Path | None = None,
    reuse_inventory_path: Path | None = None,
    reprocess_prefixes: tuple[str, ...] = (),
) -> dict[str, int]:
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    workspace_root = workspace_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"Source folder does not exist or is not a directory: {source_root}")
    try:
        output_dir.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("Output directory must be outside the source folder to keep the source corpus read-only")
    files = sorted(
        (path for path in source_root.rglob("*") if path.is_file() and path.name not in {".DS_Store"}),
        key=lambda path: path.relative_to(source_root).as_posix().casefold(),
    )
    if limit is not None:
        files = files[:limit]
    overrides = load_overrides(overrides_path)
    reusable: dict[str, dict[str, Any]] = {}
    if reuse_inventory_path is not None:
        if not reuse_inventory_path.is_file():
            raise ValueError(f"Reuse inventory does not exist: {reuse_inventory_path}")
        prior = json.loads(reuse_inventory_path.read_text(encoding="utf-8"))
        reusable = {record["path"]: record for record in prior.get("files", [])}
    records: list[dict[str, Any]] = []
    for path in files:
        try:
            workspace_path = path.relative_to(workspace_root).as_posix()
        except ValueError:
            workspace_path = str(path.resolve())
        cached = reusable.get(workspace_path)
        force = any(workspace_path.startswith(prefix) for prefix in reprocess_prefixes)
        stat = path.stat()
        cache_matches = bool(
            cached
            and not force
            and int(cached.get("file_size_bytes", -1)) == stat.st_size
            and (
                not cached.get("source_mtime_ns")
                or int(cached["source_mtime_ns"]) == stat.st_mtime_ns
            )
        )
        if cache_matches:
            record = dict(cached)
            record["source_mtime_ns"] = stat.st_mtime_ns
            record["audit_cache_status"] = "reused"
        else:
            record = inspect_file(path, source_root, workspace_root)
        records.append(record)
    records_by_path = {record["path"]: record for record in records}
    unknown_override_paths = sorted(set(overrides) - set(records_by_path))
    if unknown_override_paths:
        raise ValueError("Override path(s) not found in source corpus: " + ", ".join(unknown_override_paths))
    for record in records:
        apply_override(record, overrides.get(record["path"]))
    summaries = summarize_rounds(records)
    duplicates = duplicate_rows(records)
    needs_review = review_rows(records, summaries)
    output_dir.mkdir(parents=True, exist_ok=True)

    write_csv(output_dir / "file_inventory.csv", records, INVENTORY_FIELDS)
    structured = {
        "schema_version": "1.0",
        "source_root": str(source_root),
        "files": records,
    }
    (output_dir / "file_inventory.json").write_text(
        json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary_fields = [
        "likely_city", "likely_property_project", "likely_review_round",
        "number_of_files", "spreadsheet_candidates", "dedicated_comment_documents",
        "dedicated_response_documents", "combined_comment_response_documents",
        "drawing_heavy_files", "recommended_primary_source", "primary_source_reason",
        "discovery_status",
    ]
    write_csv(output_dir / "review_round_summary.csv", summaries, summary_fields)
    write_csv(output_dir / "duplicate_files.csv", duplicates, ["duplicate_group", "sha256", "file_size_bytes", "path", "group_file_count"])
    write_csv(output_dir / "needs_review.csv", needs_review, ["item_type", "path_or_group", "reasons", "document_type", "confidence", "processing_error"])
    write_report(output_dir / "audit_report.md", source_root, records, summaries, duplicates, needs_review)
    return {
        "files": len(records),
        "rounds": len(summaries),
        "duplicate_rows": len(duplicates),
        "needs_review_rows": len(needs_review),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="source corpus directory (never modified)")
    parser.add_argument("--output", type=Path, default=Path("corpus_audit_output"), help="separate report directory")
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd(), help="base for workspace-relative inventory paths")
    parser.add_argument("--limit", type=int, help="inspect only the first N sorted files (for representative dry runs)")
    parser.add_argument("--overrides", type=Path, help="optional CSV of explicit human-verified classifications")
    parser.add_argument("--reuse-inventory", type=Path, help="reuse unchanged records from a prior file_inventory.json")
    parser.add_argument("--reprocess-prefix", action="append", default=[], help="workspace-relative path prefix to force reinspection; repeatable")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        counts = run_audit(
            args.source, args.output, args.workspace_root, args.limit,
            args.overrides, args.reuse_inventory, tuple(args.reprocess_prefix),
        )
    except (OSError, ValueError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
