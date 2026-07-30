"""Normalized, opaque source registry and read-only document preview services."""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from math import ceil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from corpus_audit import audit_corpus as audit
try:
    from .gemini_enrich import record_digest
except ImportError:
    from gemini_enrich import record_digest


PDF_TYPES = {"pdf"}
WORD_TYPES = {"doc", "docx"}
SPREADSHEET_TYPES = {"xls", "xlsx", "csv"}


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def normalize_quote(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("_x000D_", " ").replace("_x000A_", " ")
    return re.sub(r"\s+", " ", value.replace("\r", " ").replace("\n", " ")).strip().casefold()


def document_type(path: str | Path) -> str:
    return Path(path).suffix.lstrip(".").casefold() or "unknown"


def viewer_type_for(file_type: str, preview_available: bool = False) -> str:
    file_type = file_type.casefold()
    if file_type in PDF_TYPES:
        return "pdf"
    if file_type in WORD_TYPES:
        return "pdf_preview"
    if file_type in SPREADSHEET_TYPES:
        return "spreadsheet"
    return "unsupported"


def column_number(letters: str) -> int:
    result = 0
    for character in letters.upper():
        if not character.isalpha():
            break
        result = result * 26 + ord(character) - 64
    return result


def column_letters(number: int) -> str:
    result = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def parse_cell_range(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\s*([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?\s*", value or "")
    if not match:
        return None
    start_column = column_number(match.group(1))
    start_row = int(match.group(2))
    end_column = column_number(match.group(3) or match.group(1))
    end_row = int(match.group(4) or match.group(2))
    return (
        min(start_row, end_row), min(start_column, end_column),
        max(start_row, end_row), max(start_column, end_column),
    )


def _parse_boxes(value: Any) -> list[list[float]]:
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    if value and all(isinstance(item, (int, float)) for item in value) and len(value) == 4:
        value = [value]
    boxes: list[list[float]] = []
    for box in value:
        if isinstance(box, list) and len(box) == 4 and all(isinstance(item, (int, float)) for item in box):
            boxes.append([float(item) for item in box])
    return boxes


@dataclass(slots=True)
class SourceLocation:
    document_id: str
    original_document_type: str
    viewer_type: str
    page_number: int | None = None
    pdf_bounding_boxes: list[list[float]] = field(default_factory=list)
    exact_quote: str = ""
    normalized_quote: str = ""
    sheet_name: str = ""
    cell_range: str = ""
    paragraph_index: int | None = None
    preview_document_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(
        cls,
        record: dict[str, Any],
        document_id: str,
        file_type: str,
        preview_document_id: str | None = None,
        cell_range: str = "",
    ) -> "SourceLocation":
        raw_location = str(record.get("source_location", ""))
        page_value = record.get("source_page") or ""
        page_match = re.search(r"\bpage\s+(\d+)", raw_location, flags=re.IGNORECASE)
        page = int(page_value) if str(page_value).isdigit() else (int(page_match.group(1)) if page_match else None)
        sheet = str(record.get("source_sheet", "") or "")
        if not sheet:
            sheet_match = re.search(r"\bsheet\s+(.+?),\s*(?:row|cell)\b", raw_location, flags=re.IGNORECASE)
            sheet = sheet_match.group(1).strip() if sheet_match else ""
        row_value = record.get("source_row") or ""
        paragraph_match = re.search(r"\bparagraph\s+(\d+)", raw_location, flags=re.IGNORECASE)
        paragraph = int(paragraph_match.group(1)) if paragraph_match else None
        if not paragraph and file_type in WORD_TYPES and str(row_value).isdigit():
            paragraph = int(row_value)
        quote = str(
            record.get("verified_text")
            if record.get("text_trust_status") == "verified" and record.get("verified_text")
            else record.get("original_text", "")
        )
        structured = record.get("source_locator_json") if isinstance(record.get("source_locator_json"), dict) else {}
        if not sheet:
            sheet = str(structured.get("sheet_name", "") or "")
        structured_paragraph = structured.get("paragraph_index")
        if file_type in WORD_TYPES and str(structured_paragraph).isdigit():
            paragraph = int(structured_paragraph)
        metadata = {
            "legacy_location": raw_location,
            "source_row": int(row_value) if str(row_value).isdigit() else None,
            "source_page_end": int(record["source_page_end"]) if str(record.get("source_page_end", "")).isdigit() else None,
            "comment_number": str(record.get("comment_number", "") or ""),
        }
        if isinstance(record.get("source_locator_json"), dict):
            metadata["structured_locator_json"] = record["source_locator_json"]
            metadata["coordinate_source"] = str(record.get("extraction_method", ""))
        return cls(
            document_id=document_id,
            original_document_type=file_type,
            viewer_type=viewer_type_for(file_type, bool(preview_document_id)),
            page_number=page,
            pdf_bounding_boxes=_parse_boxes(record.get("pdf_bounding_boxes") or record.get("source_bounding_boxes") or []),
            exact_quote=quote,
            normalized_quote=normalize_quote(quote),
            sheet_name=sheet,
            cell_range=(
                cell_range
                or str(record.get("source_cell_range", "") or "")
                or str(structured.get("cell_range", "") or "")
            ),
            paragraph_index=paragraph,
            preview_document_id=preview_document_id,
            metadata=metadata,
        )


def pdf_navigation(location: SourceLocation | dict[str, Any]) -> dict[str, Any]:
    value = location.to_dict() if isinstance(location, SourceLocation) else location
    boxes = value.get("pdf_bounding_boxes") or []
    if boxes:
        return {"method": "coordinates", "page_number": value.get("page_number") or 1, "bounding_boxes": boxes}
    if value.get("exact_quote"):
        return {"method": "text_search", "page_number": value.get("page_number") or 1, "query": value["exact_quote"]}
    return {"method": "page", "page_number": value.get("page_number") or 1}


def structured_locator_boxes(
    locators: list[dict[str, Any]], page_number: int, companion: list[dict[str, Any]] | None = None,
) -> list[list[float]]:
    """Convert reviewed form locators into Adobe/PDF coordinate boxes."""
    combined = [*locators, *(companion or [])]
    heights: list[float] = []
    for item in combined:
        pdf_rect = item.get("pdf_rect")
        top_left = item.get("top_left_bbox")
        if int(item.get("page") or 0) == page_number and isinstance(pdf_rect, list) and isinstance(top_left, list) and len(pdf_rect) == len(top_left) == 4:
            heights.extend([float(pdf_rect[1]) + float(top_left[3]), float(pdf_rect[3]) + float(top_left[1])])
    height = sum(heights) / len(heights) if heights else 0.0
    boxes: list[list[float]] = []
    for item in locators:
        if int(item.get("page") or 0) != page_number:
            continue
        pdf_rect = item.get("pdf_rect")
        top_left = item.get("top_left_bbox")
        if isinstance(pdf_rect, list) and len(pdf_rect) == 4:
            boxes.append([round(float(value), 3) for value in pdf_rect])
        elif height and isinstance(top_left, list) and len(top_left) == 4:
            x_min, top, x_max, bottom = (float(value) for value in top_left)
            boxes.append([round(x_min, 3), round(height - bottom, 3), round(x_max, 3), round(height - top, 3)])
    return boxes


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LibreOfficePreviewConverter:
    """Replaceable DOC/DOCX-to-PDF converter using LibreOffice headless."""

    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("soffice") or shutil.which("libreoffice")

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def convert(self, source: Path, destination: Path) -> None:
        if not self.executable:
            raise RuntimeError("LibreOffice is not installed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="permit-preview-") as temporary:
            output_dir = Path(temporary)
            completed = subprocess.run(
                [self.executable, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )
            generated = output_dir / f"{source.stem}.pdf"
            if completed.returncode or not generated.is_file():
                message = completed.stderr.strip() or completed.stdout.strip() or "LibreOffice produced no PDF"
                raise RuntimeError(message)
            shutil.copyfile(generated, destination)

    def convert_spreadsheet(self, source: Path, destination: Path) -> None:
        if not self.executable:
            raise RuntimeError("LibreOffice is not installed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="permit-sheet-preview-") as temporary:
            output_dir = Path(temporary)
            completed = subprocess.run(
                [self.executable, "--headless", "--convert-to", "xlsx", "--outdir", str(output_dir), str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=180,
                check=False,
            )
            generated = output_dir / f"{source.stem}.xlsx"
            if completed.returncode or not generated.is_file():
                message = completed.stderr.strip() or completed.stdout.strip() or "LibreOffice produced no XLSX"
                raise RuntimeError(message)
            shutil.copyfile(generated, destination)


def _xlsx_cells(path: Path, sheet_name: str, start_row: int = 1, end_row: int | None = None) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        shared = audit.read_shared_strings(archive)
        targets = dict(audit.workbook_sheet_targets(archive))
        if not targets:
            raise ValueError("Workbook has no readable sheets")
        selected = sheet_name if sheet_name in targets else next(iter(targets))
        root = ET.fromstring(archive.read(targets[selected]))
    rows: list[dict[str, Any]] = []
    for sequence, row_node in enumerate((node for node in root.iter() if audit.xml_local(node.tag) == "row"), start=1):
        try:
            row_number = int(row_node.attrib.get("r", sequence))
        except ValueError:
            row_number = sequence
        if row_number < start_row or (end_row is not None and row_number > end_row):
            continue
        cells: list[dict[str, Any]] = []
        for cell_number, cell in enumerate((node for node in row_node if audit.xml_local(node.tag) == "c"), start=1):
            address = cell.attrib.get("r", "")
            column = audit.column_letters(address) or audit.number_to_column(cell_number)
            value = audit.parse_xlsx_cell(cell, shared)
            cells.append({"address": address or f"{column}{row_number}", "column": column, "value": value})
        rows.append({"row_number": row_number, "cells": cells})
    return rows


def xlsx_sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        return [name for name, _target in audit.workbook_sheet_targets(archive)]


def find_xlsx_cell(path: Path, sheet: str, row: int | None, quote: str) -> str:
    if not sheet or not row:
        return ""
    try:
        rows = _xlsx_cells(path, sheet, row, row)
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return ""
    normalized = normalize_quote(quote)
    best = ""
    for item in rows:
        for cell in item["cells"]:
            value = normalize_quote(str(cell["value"]))
            if value == normalized:
                return cell["address"]
            if normalized and (normalized in value or value in normalized) and len(value) > 12:
                best = cell["address"]
    return best


REFERENCE_STOPWORDS = {
    "a", "added", "and", "attached", "document", "file", "for", "in", "included", "is", "of", "on",
    "please", "refer", "reference", "revised", "see", "sheet", "submission", "system", "the", "to", "under",
    "csv", "doc", "docx", "pdf", "version", "xls", "xlsx",
}

REFERENCE_SYNONYMS = {
    "geotech": {"soil", "geotechnical"},
    "geotechnical": {"soil", "geotech"},
    "soil": {"geotech", "geotechnical"},
    "calc": {"calculation", "structural"},
    "calculation": {"calc", "structural"},
    "foundation": {"footing", "structural"},
    "footing": {"foundation", "structural"},
}


def reference_tokens(value: str) -> set[str]:
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value or "")
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", value).casefold())
    tokens: set[str] = set()
    for word in words:
        if word in REFERENCE_STOPWORDS or word.isdigit() or len(word) < 3:
            continue
        if word.endswith("ing") and len(word) > 6:
            word = word[:-3]
        elif word.endswith("ed") and len(word) > 5:
            word = word[:-2]
        elif word.endswith("s") and len(word) > 4:
            word = word[:-1]
        if word.endswith("e") and len(word) > 5:
            word = word[:-1]
        tokens.add(word)
        tokens.update(REFERENCE_SYNONYMS.get(word, set()))
    return tokens


def sheet_references(value: str) -> list[str]:
    matches = re.findall(r"\bsheets?\s+([A-Za-z]{1,3}\s*[-.]?\s*\d+(?:\.\d+)*)\b", value or "", re.IGNORECASE)
    matches += re.findall(r"\b(?:on|at)\s+(?:the\s+)?([A-Za-z]{1,3}\d+(?:\.\d+)*)\b", value or "", re.IGNORECASE)
    matches += re.findall(r"\b(?:updated|revised)\s+([A-Za-z]{1,3}\d+(?:\.\d+)*)\b", value or "", re.IGNORECASE)
    matches += re.findall(r"&\s*([A-Za-z]{1,3}\d+(?:\.\d+)*)\b", value or "", re.IGNORECASE)
    references: list[str] = []
    for match in matches:
        reference = re.sub(r"\s+", "", match).upper().replace("-", ".")
        if reference not in references:
            references.append(reference)
    return references


def _path_proximity(left: str, right: str) -> int:
    score = 0
    for left_part, right_part in zip(Path(left).parts[:-1], Path(right).parts[:-1]):
        if left_part.casefold() != right_part.casefold():
            break
        score += 1
    return score


def _same_project(left: str, right: str) -> bool:
    left_parts, right_parts = Path(left).parts, Path(right).parts
    return len(left_parts) > 1 and len(right_parts) > 1 and left_parts[1].casefold() == right_parts[1].casefold()


def _pdf_text_pages(path: Path) -> list[str]:
    executable = shutil.which("gs")
    if not executable:
        return []
    with tempfile.TemporaryDirectory(prefix="permit-pdf-text-") as temporary:
        pattern = Path(temporary) / "page-%05d.txt"
        try:
            completed = subprocess.run(
                [
                    executable, "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=txtwrite",
                    f"-sOutputFile={pattern}", str(path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode:
            return []
        return [item.read_text(encoding="utf-8", errors="ignore") for item in sorted(Path(temporary).glob("page-*.txt"))]


def _best_pdf_quote(text: str, wanted_tokens: set[str]) -> str:
    if not text or not wanted_tokens:
        return ""
    collapsed = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", collapsed)
    raw_lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    candidates = [value for value in sentences + raw_lines if 4 <= len(value.split()) <= 100]
    if not candidates:
        return ""
    ranked = sorted(
        candidates,
        key=lambda value: (
            len(reference_tokens(value) & wanted_tokens),
            sum(len(token) for token in reference_tokens(value) & wanted_tokens),
            -abs(len(value.split()) - 24),
        ),
        reverse=True,
    )
    best = ranked[0]
    if not reference_tokens(best) & wanted_tokens:
        return ""

    # Plan sheets are multi-column drawings. txtwrite can place an unrelated
    # left-column sentence before a bullet in the same line; keep the bullet
    # phrase so Adobe can search one contiguous run of PDF text.
    for marker in (" · ", " • "):
        pieces = best.split(marker)
        matching = [piece for piece in pieces if len(reference_tokens(piece) & wanted_tokens) >= 2]
        if matching:
            best = max(matching, key=lambda value: len(reference_tokens(value) & wanted_tokens))
    best = re.sub(r"\s+\d+\s*$", "", best).strip()

    # Join a wrapped parenthetical note when the continuation is recoverable
    # from the following drawing-text line.
    if best.count("(") > best.count(")"):
        for index, line in enumerate(raw_lines[:-1]):
            if best not in line:
                continue
            continuation = raw_lines[index + 1]
            matches = re.findall(r"\b(?:BE|IS|ARE|MUST|SHALL|PROVIDE|COMPLY|INSTALLED?)\b[^)]*\)", continuation, re.IGNORECASE)
            if matches:
                best = f"{best} {matches[-1]}"
            break
    return best[:900].strip()


def _prepare_pdf_search_pages(
    pages: list[str],
) -> list[tuple[str, str, set[str]]]:
    return [(
        re.sub(r"[^A-Z0-9]", "", page_text.upper()),
        re.sub(
            r"[^A-Z0-9]", "",
            "\n".join(page_text.splitlines()[-25:]).upper(),
        ),
        reference_tokens(page_text),
    ) for page_text in pages]


def _sheet_pdf_location(
    path: Path,
    sheet: str,
    reference_text: str,
    pages: list[str],
    prepared_pages: list[tuple[str, str, set[str]]] | None = None,
) -> tuple[int, str]:
    if not pages:
        return 1, ""
    compact_sheet = re.sub(r"[^A-Z0-9]", "", sheet.upper())
    wanted = reference_tokens(reference_text)
    best_index, best_score = 0, -1
    searchable_pages = prepared_pages or _prepare_pdf_search_pages(pages)
    for index, (compact_page, compact_tail, page_tokens) in enumerate(searchable_pages):
        score = (
            (80 if compact_sheet and compact_sheet in compact_tail else 0)
            + (12 if compact_sheet and compact_sheet in compact_page else 0)
            + 5 * len(page_tokens & wanted)
        )
        if score > best_score:
            best_index, best_score = index, score
    return best_index + 1, _best_pdf_quote(pages[best_index], wanted)


def _postscript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_page_size(path: Path, page_number: int, executable: str) -> tuple[float, float]:
    script = f"({_postscript_string(str(path))}) (r) file runpdfbegin {page_number} pdfgetpage /MediaBox get == quit"
    try:
        completed = subprocess.run(
            [executable, "-q", "-dNOSAFER", "-dNODISPLAY", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30, check=False,
        )
        numbers = re.findall(r"-?\d+(?:\.\d+)?", completed.stdout)
        if completed.returncode == 0 and len(numbers) >= 4:
            left, bottom, right, top = (float(value) for value in numbers[-4:])
            return max(1.0, right - left), max(1.0, top - bottom)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return 612.0, 792.0


def _pdf_page_height(path: Path, page_number: int, executable: str) -> float:
    return _pdf_page_size(path, page_number, executable)[1]


def _normalized_box_to_pdf(width: float, height: float, item: dict[str, Any]) -> list[float]:
    x_min, y_min = float(item["x_min"]), float(item["y_min"])
    x_max, y_max = float(item["x_max"]), float(item["y_max"])
    if not (0 <= x_min < x_max <= 1000 and 0 <= y_min < y_max <= 1000):
        return []
    return [
        round(width * x_min / 1000.0, 3),
        round(height * (1.0 - y_max / 1000.0), 3),
        round(width * x_max / 1000.0, 3),
        round(height * (1.0 - y_min / 1000.0), 3),
    ]


def _normalized_locator_boxes(
    path: Path, page_number: int, value: Any,
) -> list[list[float]]:
    """Convert Gemini's 0-1000 top-left boxes to PDF bottom-left points."""
    if not isinstance(value, dict) or not isinstance(value.get("bounding_boxes"), list):
        return []
    executable = shutil.which("gs")
    if not executable or page_number < 1:
        return []
    width, height = _pdf_page_size(path, page_number, executable)
    boxes: list[list[float]] = []
    for item in value["bounding_boxes"]:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("page") or 0) != page_number:
                continue
            converted = _normalized_box_to_pdf(width, height, item)
        except (KeyError, TypeError, ValueError):
            continue
        if converted:
            boxes.append(converted)
    return boxes


def _pdf_page_layout(path: Path, page_number: int) -> tuple[float, list[dict[str, Any]]]:
    executable = shutil.which("gs")
    if not executable or page_number < 1:
        return 0.0, []
    try:
        completed = subprocess.run(
            [
                executable, "-q", "-dNOPAUSE", "-dBATCH",
                f"-dFirstPage={page_number}", f"-dLastPage={page_number}",
                "-sDEVICE=txtwrite", "-dTextFormat=1", "-sOutputFile=%stdout", str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90, check=False,
        )
        if completed.returncode:
            return 0.0, []
        root = ET.fromstring(completed.stdout)
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return 0.0, []
    lines: list[dict[str, Any]] = []
    for line_node in (node for node in root.iter() if audit.xml_local(node.tag) == "line"):
        text_parts: list[str] = []
        characters: list[tuple[float, float, float, float, float]] = []
        for span in (node for node in line_node if audit.xml_local(node.tag) == "span"):
            try:
                size = float(span.attrib.get("size", "10") or 10)
            except ValueError:
                size = 10.0
            for character in (node for node in span if audit.xml_local(node.tag) == "char"):
                value = character.attrib.get("c", "")
                bbox = [float(item) for item in character.attrib.get("bbox", "").split()]
                if not value or len(bbox) != 4:
                    continue
                text_parts.append(value)
                characters.append((bbox[0], bbox[1], bbox[2], bbox[3], size))
        text = "".join(text_parts)
        if text.strip() and characters:
            lines.append({"text": text, "characters": characters})
    return _pdf_page_height(path, page_number, executable), lines


def _quote_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", value or "").casefold())


def _boxes_for_quote(page_height: float, lines: list[dict[str, Any]], quote: str) -> list[list[float]]:
    wanted = _quote_tokens(quote)
    if len(wanted) < 3 or not lines:
        return []
    tokens: list[tuple[str, int, int, int]] = []
    for line_index, line in enumerate(lines):
        for match in re.finditer(r"[a-z0-9]+", unicodedata.normalize("NFKC", line["text"]).casefold()):
            tokens.append((match.group(0), line_index, match.start(), match.end()))
    match_slice: list[tuple[str, int, int, int]] = []
    for start in range(0, len(tokens) - len(wanted) + 1):
        if [item[0] for item in tokens[start:start + len(wanted)]] == wanted:
            match_slice = tokens[start:start + len(wanted)]
            break

    if not match_slice:
        normalized_quote = " ".join(wanted)
        best: tuple[float, int, int] = (0.0, 0, 0)
        for start in range(len(lines)):
            for end in range(start, min(len(lines), start + 3)):
                candidate = " ".join(_quote_tokens(" ".join(line["text"] for line in lines[start:end + 1])))
                ratio = difflib.SequenceMatcher(None, normalized_quote, candidate).ratio()
                if ratio > best[0]:
                    best = (ratio, start, end)
        if best[0] < 0.68:
            return []
        for line_index in range(best[1], best[2] + 1):
            text = lines[line_index]["text"]
            matches = list(re.finditer(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).casefold()))
            if matches:
                match_slice.extend((match.group(0), line_index, match.start(), match.end()) for match in matches)

    boxes: list[list[float]] = []
    for line_index in sorted({item[1] for item in match_slice}):
        selected = [item for item in match_slice if item[1] == line_index]
        start = min(item[2] for item in selected)
        end = max(item[3] for item in selected)
        characters = lines[line_index]["characters"][start:end]
        if not characters:
            continue
        x_min = min(item[0] for item in characters)
        x_max = max(item[2] for item in characters)
        baseline = max(item[1] for item in characters)
        font_size = max(item[4] for item in characters)
        top_from_page = max(0.0, baseline - font_size * 1.05)
        bottom_from_page = baseline + font_size * 0.25
        boxes.append([
            round(x_min, 2), round(page_height - bottom_from_page, 2),
            round(x_max, 2), round(page_height - top_from_page, 2),
        ])
    return boxes


class SourceRegistry:
    def __init__(
        self,
        dataset_path: Path,
        source_root: Path,
        registry_path: Path,
        preview_root: Path,
        converter: LibreOfficePreviewConverter | None = None,
        authorizer: Callable[[dict[str, Any]], bool] | None = None,
        auto_migrate: bool = True,
    ):
        self.dataset_path = dataset_path.resolve()
        self.source_root = source_root.resolve()
        self.registry_path = registry_path.resolve()
        self.preview_root = preview_root.resolve()
        self.converter = converter or LibreOfficePreviewConverter()
        self.authorizer = authorizer or (lambda _document: True)
        self.payload: dict[str, Any] = {"schema_version": "1.0", "documents": {}, "sources": {}}
        if self.registry_path.is_file():
            self.payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry_is_stale = (
            not self.registry_path.is_file()
            or self.dataset_path.stat().st_mtime_ns > self.registry_path.stat().st_mtime_ns
        )
        if auto_migrate and registry_is_stale:
            self.migrate()

    @property
    def documents(self) -> dict[str, dict[str, Any]]:
        return self.payload.setdefault("documents", {})

    @property
    def sources(self) -> dict[str, dict[str, Any]]:
        return self.payload.setdefault("sources", {})

    def _relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.source_root.parent).as_posix()

    def _path_for_relative(self, relative: str) -> Path:
        candidate = (self.source_root.parent / relative).resolve()
        try:
            candidate.relative_to(self.source_root)
        except ValueError as exc:
            raise PermissionError("Document is outside the authorized corpus") from exc
        return candidate

    def _document_for_path(self, path: Path, previous_by_path: dict[str, dict[str, Any]]) -> dict[str, Any]:
        relative = self._relative_path(path)
        stat = path.stat()
        previous = previous_by_path.get(relative, {})
        unchanged = previous.get("size") == stat.st_size and previous.get("mtime_ns") == stat.st_mtime_ns
        digest = previous.get("sha256", "") if unchanged else sha256_file(path)
        file_type = document_type(path)
        document_id = stable_id("D", relative.casefold())
        return {
            "document_id": document_id,
            "filename": path.name,
            "relative_path": relative,
            "original_document_type": file_type,
            "viewer_type": viewer_type_for(file_type),
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "sha256": digest,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "preview_document_id": previous.get("preview_document_id"),
            "preview_status": previous.get("preview_status", "not_required" if file_type not in WORD_TYPES else "missing"),
            "preview_error": previous.get("preview_error", ""),
        }

    def _ensure_word_preview(self, document: dict[str, Any], old_document: dict[str, Any]) -> dict[str, Any] | None:
        if document["original_document_type"] not in WORD_TYPES:
            return None
        preview_id = stable_id("P", document["document_id"])
        destination = self.preview_root / document["document_id"] / f"{document['sha256']}.pdf"
        old_preview = old_document.get("preview_document_id") == preview_id
        if not destination.is_file() and self.converter.available:
            try:
                self.converter.convert(self._path_for_relative(document["relative_path"]), destination)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                document["preview_status"] = "conversion_failed"
                document["preview_error"] = str(exc)
                document["preview_document_id"] = None
                return None
        if not destination.is_file():
            document["preview_status"] = "missing_dependency"
            document["preview_error"] = "Install LibreOffice to generate DOC/DOCX PDF previews."
            document["preview_document_id"] = None
            return None
        preview_stat = destination.stat()
        document["preview_status"] = "ready"
        document["preview_error"] = ""
        document["preview_document_id"] = preview_id
        return {
            "document_id": preview_id,
            "filename": f"{Path(document['filename']).stem} preview.pdf",
            "preview_path": destination.relative_to(self.registry_path.parent).as_posix(),
            "original_document_type": "pdf",
            "viewer_type": "pdf",
            "mime_type": "application/pdf",
            "sha256": sha256_file(destination) if not old_preview else "",
            "size": preview_stat.st_size,
            "mtime_ns": preview_stat.st_mtime_ns,
            "is_preview": True,
            "original_document_id": document["document_id"],
        }

    def migrate(self) -> dict[str, int]:
        dataset = json.loads(self.dataset_path.read_text(encoding="utf-8"))
        rematch_by_owner: dict[str, tuple[dict[str, Any], str]] = {}
        for link in dataset.get("comment_response_links", []):
            if link.get("provenance") != "document_structure_rematch" or link.get("review_status") != "confirmed":
                continue
            rematch_by_owner[str(link.get("comment_id", ""))] = (link, "comment")
            rematch_by_owner[str(link.get("response_id", ""))] = (link, "response")
        enrichment_path = self.registry_path.parent / "gemini_enrichment.json"
        enrichment_entries: dict[str, dict[str, Any]] = {}
        if enrichment_path.is_file():
            enrichment_payload = json.loads(enrichment_path.read_text(encoding="utf-8"))
            if isinstance(enrichment_payload.get("entries"), dict):
                enrichment_entries = enrichment_payload["entries"]
        old_documents = self.documents.copy()
        old_by_path = {row.get("relative_path", ""): row for row in old_documents.values() if row.get("relative_path")}
        documents: dict[str, dict[str, Any]] = {}
        path_to_id: dict[str, str] = {}
        aliases: dict[str, list[str]] = {}
        for path in sorted(self.source_root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            document = self._document_for_path(path, old_by_path)
            documents[document["document_id"]] = document
            path_to_id[document["relative_path"]] = document["document_id"]
            names = {path.name.casefold()}
            versioned = re.match(r"(.+?\.(?:pdf|docx?|xlsx?))\s+v\d+\.\w+$", path.name, re.IGNORECASE)
            if versioned:
                names.add(versioned.group(1).casefold())
            for name in names:
                aliases.setdefault(name, []).append(document["document_id"])
        for document in list(documents.values()):
            preview = self._ensure_word_preview(document, old_documents.get(document["document_id"], {}))
            if preview:
                documents[preview["document_id"]] = preview

        sources: dict[str, dict[str, Any]] = {}
        pdf_page_cache: dict[str, list[str]] = {}
        pdf_search_cache: dict[str, list[tuple[str, str, set[str]]]] = {}

        def pdf_pages(document: dict[str, Any]) -> list[str]:
            document_id = document["document_id"]
            if document_id not in pdf_page_cache:
                pdf_page_cache[document_id] = _pdf_text_pages(self._path_for_relative(document["relative_path"]))
            return pdf_page_cache[document_id]

        def pdf_search_pages(
            document: dict[str, Any],
        ) -> list[tuple[str, str, set[str]]]:
            document_id = document["document_id"]
            if document_id not in pdf_search_cache:
                pdf_search_cache[document_id] = _prepare_pdf_search_pages(
                    pdf_pages(document)
                )
            return pdf_search_cache[document_id]

        for collection in ("comments", "responses"):
            for record in dataset.get(collection, []):
                owner_id = str(record.get("comment_id") if collection == "comments" else record.get("response_id"))
                enrichment = enrichment_entries.get(owner_id, {})
                if enrichment.get("input_sha256") != record_digest(record):
                    enrichment = {}
                ai_references = [
                    item for item in enrichment.get("secondary_references", [])
                    if isinstance(item, dict) and float(item.get("confidence", 0) or 0) >= 0.55
                ]
                record_text = str(
                    record.get("verified_text")
                    if record.get("text_trust_status") == "verified" and record.get("verified_text")
                    else record.get("original_text", "")
                )
                raw_paths = [part.strip() for part in re.split(r"\s+\|\s+", str(record.get("source_document", ""))) if part.strip()]
                for ordinal, relative in enumerate(raw_paths):
                    document_id = path_to_id.get(relative)
                    if not document_id:
                        continue
                    document = documents[document_id]
                    row = int(record["source_row"]) if str(record.get("source_row", "")).isdigit() else None
                    cell_range = ""
                    if document["original_document_type"] == "xlsx":
                        cell_range = find_xlsx_cell(
                            self._path_for_relative(relative), str(record.get("source_sheet", "")), row,
                            record_text,
                        )
                    location = SourceLocation.from_record(
                        record, document_id, document["original_document_type"],
                        document.get("preview_document_id"), cell_range,
                    )
                    rematch = rematch_by_owner.get(owner_id)
                    if rematch and relative == rematch[0].get("source_pdf"):
                        link, role = rematch
                        locator_key = f"{role}_locator_json"
                        pages_key = f"{role}_pages"
                        locators = link.get(locator_key, []) if isinstance(link.get(locator_key), list) else []
                        companion_key = "response_locator_json" if role == "comment" else "comment_locator_json"
                        companion = link.get(companion_key, []) if isinstance(link.get(companion_key), list) else []
                        cited_pages = link.get(pages_key, []) if isinstance(link.get(pages_key), list) else []
                        page_number = int(cited_pages[0]) if cited_pages else int(location.page_number or 1)
                        quote = record_text
                        location.page_number = page_number
                        location.pdf_bounding_boxes = structured_locator_boxes(locators, page_number, companion)
                        location.exact_quote = quote
                        location.normalized_quote = normalize_quote(quote)
                        location.metadata.update({
                            "coordinate_source": "document_structure_rematch",
                            "import_key": link.get("import_key", ""),
                            "structured_locator_json": locators,
                        })
                    if (
                        not location.pdf_bounding_boxes
                        and document["original_document_type"] == "pdf"
                        and location.page_number
                    ):
                        normalized_boxes = _normalized_locator_boxes(
                            self._path_for_relative(relative),
                            int(location.page_number),
                            record.get("source_locator_json"),
                        )
                        if normalized_boxes:
                            location.pdf_bounding_boxes = normalized_boxes
                            location.metadata["coordinate_source"] = "gemini_normalized_1000"
                    source_id = stable_id("S", f"{owner_id}|{document_id}|primary|{ordinal}")
                    sources[source_id] = {
                        "source_id": source_id,
                        "owner_id": owner_id,
                        "relation": "Primary source",
                        "document_id": document_id,
                        "location": location.to_dict(),
                    }
                    for locator_index, locator in enumerate(
                        record.get("additional_source_locators", []) or [],
                        1,
                    ):
                        if not isinstance(locator, dict):
                            continue
                        locator_pages = locator.get("pages", [])
                        if not isinstance(locator_pages, list):
                            locator_pages = []
                        locator_quote = str(
                            locator.get("exact_quote", "")
                        )
                        additional_record = {
                            "original_text": locator_quote,
                            "verified_text": locator_quote,
                            "text_trust_status": "verified",
                            "source_location": (
                                f"paragraph "
                                f"{locator.get('paragraph_index', '')}"
                            ),
                            "source_row": locator.get(
                                "paragraph_index", "",
                            ),
                            "source_page": (
                                locator_pages[0]
                                if locator_pages else ""
                            ),
                            "source_page_end": (
                                locator_pages[-1]
                                if locator_pages else ""
                            ),
                            "source_locator_json": locator,
                            "extraction_method": record.get(
                                "extraction_method", "",
                            ),
                        }
                        additional_location = SourceLocation.from_record(
                            additional_record,
                            document_id,
                            document["original_document_type"],
                            document.get("preview_document_id"),
                        )
                        additional_location.metadata[
                            "additional_source_ordinal"
                        ] = locator_index
                        additional_source_id = stable_id(
                            "S",
                            f"{owner_id}|{document_id}|additional|"
                            f"{ordinal}|{locator_index}",
                        )
                        sources[additional_source_id] = {
                            "source_id": additional_source_id,
                            "owner_id": owner_id,
                            "relation": (
                                "Additional source location"
                            ),
                            "document_id": document_id,
                            "location": additional_location.to_dict(),
                        }
                if collection == "comments":
                    for event_index, event in enumerate(
                        record.get("issue_thread_events", []) or [], 1,
                    ):
                        if not isinstance(event, dict):
                            continue
                        relative = str(
                            event.get("source_document", "")
                            or (raw_paths[0] if raw_paths else "")
                        )
                        document_id = path_to_id.get(relative)
                        locator = (
                            event.get("source_locator_json")
                            if isinstance(
                                event.get("source_locator_json"), dict,
                            )
                            else {}
                        )
                        exact_text = str(event.get("exact_text", ""))
                        if not document_id or not exact_text.strip():
                            continue
                        document = documents[document_id]
                        event_pages = locator.get("pages", [])
                        if not isinstance(event_pages, list):
                            event_pages = []
                        paragraph_index = locator.get(
                            "paragraph_index", "",
                        )
                        source_location = (
                            f"sheet {locator.get('sheet_name', '')} · "
                            f"cell {locator.get('cell_range', '')}"
                            if locator.get("sheet_name")
                            and locator.get("cell_range")
                            else f"paragraph {paragraph_index}"
                            if str(paragraph_index).isdigit()
                            else f"page {event_pages[0]}"
                            if event_pages else "issue history evidence"
                        )
                        event_record = {
                            "original_text": exact_text,
                            "verified_text": exact_text,
                            "text_trust_status": "verified",
                            "source_location": source_location,
                            "source_sheet": locator.get("sheet_name", ""),
                            "source_row": locator.get(
                                "row_number", paragraph_index,
                            ),
                            "source_page": (
                                event_pages[0] if event_pages else ""
                            ),
                            "source_page_end": (
                                event_pages[-1] if event_pages else ""
                            ),
                            "source_bounding_boxes": locator.get(
                                "bounding_boxes", [],
                            ),
                            "source_cell_range": locator.get(
                                "cell_range", "",
                            ),
                            "source_locator_json": locator,
                            "extraction_method": record.get(
                                "extraction_method", "",
                            ),
                        }
                        location = SourceLocation.from_record(
                            event_record,
                            document_id,
                            document["original_document_type"],
                            document.get("preview_document_id"),
                        )
                        event_id = str(
                            event.get("event_id")
                            or f"discussion-{event_index}"
                        )
                        location.metadata.update({
                            "issue_thread_id": str(
                                record.get("issue_thread_id", "")
                            ),
                            "issue_event_id": event_id,
                            "issue_event_type": str(
                                event.get("event_type", "")
                            ),
                        })
                        source_id = stable_id(
                            "S",
                            f"{owner_id}|{document_id}|discussion|{event_id}",
                        )
                        relation = (
                            "Reviewer follow-up"
                            if event.get("event_type")
                            == "reviewer_follow_up"
                            else "Prior applicant response"
                            if event.get("event_type")
                            == "applicant_response"
                            else "Discussion history"
                        )
                        sources[source_id] = {
                            "source_id": source_id,
                            "owner_id": owner_id,
                            "relation": relation,
                            "document_id": document_id,
                            "location": location.to_dict(),
                        }
                display = normalize_quote(record_text)
                current_ids = {path_to_id.get(path) for path in raw_paths}

                def add_reference(candidate: str, relation: str, location: SourceLocation) -> None:
                    source_id = stable_id("S", f"{owner_id}|{candidate}|referenced")
                    sources[source_id] = {
                        "source_id": source_id,
                        "owner_id": owner_id,
                        "relation": relation,
                        "document_id": candidate,
                        "location": location.to_dict(),
                    }

                for alias, candidates in aliases.items():
                    if alias not in display:
                        continue
                    candidate = next((item for item in candidates if item not in current_ids), None)
                    if not candidate:
                        continue
                    document = documents[candidate]
                    location = SourceLocation(
                        document_id=candidate,
                        original_document_type=document["original_document_type"],
                        viewer_type=viewer_type_for(document["original_document_type"], bool(document.get("preview_document_id"))),
                        preview_document_id=document.get("preview_document_id"),
                        metadata={"legacy_location": "Referenced by filename in evidence text"},
                    )
                    add_reference(candidate, "Secondary source", location)

                # Match human-readable document names even when the response omits
                # spacing, punctuation, or uses "landscape" for "landscaping".
                record_path = raw_paths[0] if raw_paths else ""
                display_tokens = reference_tokens(str(record.get("original_text", "")))

                # Gemini can identify implicit attachments whose filename is not
                # repeated literally in the response.
                for ai_reference in ai_references:
                    hint = str(ai_reference.get("document_hint", "")).strip()
                    if not hint or not record_path:
                        continue
                    hint_tokens = reference_tokens(hint)
                    hinted_candidates: list[tuple[int, int, str]] = []
                    for candidate, document in documents.items():
                        if candidate in current_ids or document.get("is_preview"):
                            continue
                        if not _same_project(record_path, document.get("relative_path", "")):
                            continue
                        name_tokens = reference_tokens(Path(document["filename"]).stem)
                        overlap = len(name_tokens & hint_tokens)
                        required = max(2, ceil(min(len(name_tokens), len(hint_tokens)) * 0.65))
                        if overlap >= required:
                            hinted_candidates.append((overlap, _path_proximity(record_path, document["relative_path"]), candidate))
                    if not hinted_candidates:
                        continue
                    _overlap, _proximity, candidate = max(hinted_candidates)
                    document = documents[candidate]
                    evidence_query = str(ai_reference.get("evidence_query", "")).strip()
                    title_quote = ""
                    page_number = 1 if document["original_document_type"] == "pdf" else None
                    if document["original_document_type"] == "pdf":
                        pages = pdf_pages(document)
                        wanted = reference_tokens(evidence_query) or reference_tokens(Path(document["filename"]).stem)
                        title_quote = _best_pdf_quote(pages[0] if pages else "", wanted)
                    location = SourceLocation(
                        document_id=candidate,
                        original_document_type=document["original_document_type"],
                        viewer_type=viewer_type_for(document["original_document_type"], bool(document.get("preview_document_id"))),
                        page_number=page_number,
                        exact_quote=title_quote,
                        normalized_quote=normalize_quote(title_quote),
                        preview_document_id=document.get("preview_document_id"),
                        metadata={
                            "legacy_location": "Secondary document identified from response",
                            "resolution_method": "gemini",
                            "reference_reason": str(ai_reference.get("reason", "")),
                        },
                    )
                    add_reference(candidate, "Secondary source", location)

                named_candidates: list[tuple[int, int, str]] = []
                for candidate, document in documents.items():
                    if candidate in current_ids or document.get("is_preview") or not record_path:
                        continue
                    if not _same_project(record_path, document.get("relative_path", "")):
                        continue
                    name_tokens = reference_tokens(Path(document["filename"]).stem)
                    overlap = len(name_tokens & display_tokens)
                    required = max(2, ceil(len(name_tokens) * 0.7))
                    if len(name_tokens) >= 2 and overlap >= required:
                        named_candidates.append((overlap, _path_proximity(record_path, document["relative_path"]), candidate))
                if named_candidates:
                    _overlap, _proximity, candidate = max(named_candidates)
                    document = documents[candidate]
                    title_quote = ""
                    page_number = 1 if document["original_document_type"] == "pdf" else None
                    if document["original_document_type"] == "pdf":
                        pages = pdf_pages(document)
                        title_quote = _best_pdf_quote(pages[0] if pages else "", reference_tokens(Path(document["filename"]).stem))
                    location = SourceLocation(
                        document_id=candidate,
                        original_document_type=document["original_document_type"],
                        viewer_type=viewer_type_for(document["original_document_type"], bool(document.get("preview_document_id"))),
                        page_number=page_number,
                        exact_quote=title_quote,
                        normalized_quote=normalize_quote(title_quote),
                        preview_document_id=document.get("preview_document_id"),
                        metadata={"legacy_location": "Named document referenced in response"},
                    )
                    add_reference(candidate, "Secondary source", location)

                # A response such as "see the note on A0.1" points to the plan
                # set, not merely to the response letter that contains the words.
                reference_text = str(record.get("original_text", ""))
                has_reference_language = re.search(
                    r"\b(?:refer(?:red)?\s+to|see|shown|located|added|updated|provided|included)\b",
                    reference_text,
                    re.IGNORECASE,
                )
                referenced_sheets = sheet_references(reference_text) if has_reference_language else []
                ai_sheet_queries: dict[str, str] = {}
                for ai_reference in ai_references:
                    sheet = re.sub(r"\s+", "", str(ai_reference.get("sheet", ""))).upper()
                    if sheet:
                        ai_sheet_queries[sheet] = str(ai_reference.get("evidence_query", "")).strip()
                        if sheet not in referenced_sheets:
                            referenced_sheets.append(sheet)
                for sheet in referenced_sheets:
                    plan_candidates: list[tuple[int, str]] = []
                    for candidate, document in documents.items():
                        if candidate in current_ids or document.get("is_preview") or document["original_document_type"] != "pdf":
                            continue
                        if not record_path or not _same_project(record_path, document.get("relative_path", "")):
                            continue
                        filename_tokens = reference_tokens(Path(document["filename"]).stem)
                        if not filename_tokens & {"plan", "set", "fullset", "drawing"}:
                            continue
                        plan_candidates.append((_path_proximity(record_path, document["relative_path"]), candidate))
                    if not plan_candidates:
                        continue
                    _proximity, candidate = max(plan_candidates)
                    document = documents[candidate]
                    candidate_pages = pdf_pages(document)
                    if sheet in ai_sheet_queries:
                        compact_sheet = re.sub(r"[^A-Z0-9]", "", sheet.upper())
                        sheet_verified = any(
                            compact_sheet in compact_page
                            for compact_page, _compact_tail, _tokens
                            in pdf_search_pages(document)
                        )
                        if not sheet_verified:
                            continue
                    page_number, target_quote = _sheet_pdf_location(
                        self._path_for_relative(document["relative_path"]),
                        sheet,
                        ai_sheet_queries.get(sheet) or reference_text,
                        candidate_pages,
                        pdf_search_pages(document),
                    )
                    location = SourceLocation(
                        document_id=candidate,
                        original_document_type="pdf",
                        viewer_type="pdf",
                        page_number=page_number,
                        exact_quote=target_quote,
                        normalized_quote=normalize_quote(target_quote),
                        metadata={
                            "legacy_location": f"Referenced sheet {sheet} · preview page {page_number}",
                            "sheet_reference": sheet,
                            "reference_text": reference_text,
                            "resolution_method": "gemini" if sheet in ai_sheet_queries else "explicit_reference",
                        },
                    )
                    add_reference(candidate, f"Secondary source · sheet {sheet}", location)

        # Prefer one coordinate annotation over Adobe's multi-result search
        # highlighting. Geometry is cached per PDF page and remains optional.
        layout_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}
        coordinate_sources = 0
        for source in sources.values():
            location = source["location"]
            document = documents[source["document_id"]]
            page_number = int(location.get("page_number") or 0)
            quote = str(location.get("exact_quote", ""))
            if (
                location.get("pdf_bounding_boxes")
                and location.get("metadata", {}).get("coordinate_source")
                in {"document_structure_rematch", "gemini_normalized_1000"}
            ):
                coordinate_sources += 1
                continue
            if document["original_document_type"] != "pdf" or page_number < 1 or not quote:
                continue
            key = (document["document_id"], page_number)
            if key not in layout_cache:
                layout_cache[key] = _pdf_page_layout(self._path_for_relative(document["relative_path"]), page_number)
            page_height, lines = layout_cache[key]
            boxes = _boxes_for_quote(page_height, lines, quote)
            if boxes:
                location["pdf_bounding_boxes"] = boxes
                location.setdefault("metadata", {})["coordinate_source"] = "ghostscript_text_geometry"
                coordinate_sources += 1

        self.payload = {"schema_version": "1.2", "documents": documents, "sources": sources}
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(json.dumps(self.payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {
            "documents": sum(not row.get("is_preview") for row in documents.values()),
            "previews": sum(bool(row.get("is_preview")) for row in documents.values()),
            "sources": len(sources),
            "coordinate_sources": coordinate_sources,
            "missing_previews": sum(row.get("preview_status") == "missing_dependency" for row in documents.values()),
        }

    def sources_for_owner(self, owner_id: str) -> list[dict[str, Any]]:
        rows = [row for row in self.sources.values() if row.get("owner_id") == owner_id]
        rows.sort(key=lambda row: (row.get("relation") != "Primary source", row["source_id"]))
        visible: list[dict[str, Any]] = []
        for row in rows:
            try:
                visible.append(self.public_source(row["source_id"]))
            except PermissionError:
                continue
        return visible

    def _authorized_document(self, document_id: str) -> dict[str, Any]:
        document = self.documents.get(document_id)
        if not document:
            raise KeyError("Unknown document")
        authorization_document = document
        if document.get("is_preview"):
            authorization_document = self.documents.get(document.get("original_document_id"), document)
        if not self.authorizer(authorization_document):
            raise PermissionError("Document access is not authorized")
        return document

    def public_document(self, document_id: str) -> dict[str, Any]:
        document = self._authorized_document(document_id)
        return {
            "document_id": document["document_id"],
            "filename": document["filename"],
            "original_document_type": document["original_document_type"],
            "viewer_type": document["viewer_type"],
            "mime_type": document["mime_type"],
            "size": document["size"],
            "sha256": document.get("sha256", ""),
            "preview_document_id": document.get("preview_document_id"),
            "preview_status": document.get("preview_status", "not_required"),
            "preview_error": document.get("preview_error", ""),
            "is_preview": bool(document.get("is_preview")),
        }

    def public_source(self, source_id: str) -> dict[str, Any]:
        source = self.sources.get(source_id)
        if not source:
            raise KeyError("Unknown source citation")
        document = self.public_document(source["document_id"])
        location = source["location"].copy()
        location["navigation"] = pdf_navigation(location) if location["viewer_type"] in {"pdf", "pdf_preview"} else None
        return {
            "source_id": source_id,
            "relation": source["relation"],
            "document": document,
            "location": location,
            "preview_url": f"/api/documents/{location.get('preview_document_id') or document['document_id']}/preview",
            "spreadsheet_url": f"/api/documents/{document['document_id']}/spreadsheet",
        }

    def path_for_document(self, document_id: str, preview: bool = False) -> Path:
        document = self._authorized_document(document_id)
        if document.get("is_preview"):
            path = (self.registry_path.parent / document["preview_path"]).resolve()
            try:
                path.relative_to(self.preview_root)
            except ValueError as exc:
                raise PermissionError("Preview is outside the preview store") from exc
            return path
        if preview and document["original_document_type"] in WORD_TYPES:
            preview_id = document.get("preview_document_id")
            if not preview_id:
                raise FileNotFoundError(document.get("preview_error") or "Preview is unavailable")
            return self.path_for_document(preview_id, preview=True)
        return self._path_for_relative(document["relative_path"])

    def delivery(self, document_id: str, mode: str, range_header: str = "") -> dict[str, Any]:
        if mode not in {"preview", "original"}:
            raise ValueError("Unknown delivery mode")
        document = self._authorized_document(document_id)
        if mode == "preview":
            if not document.get("is_preview") and document["original_document_type"] not in PDF_TYPES | WORD_TYPES:
                raise RuntimeError("This format has no inline file preview; use its routed in-app viewer")
            path = self.path_for_document(document_id, preview=True)
            mime = "application/pdf" if path.suffix.casefold() == ".pdf" else document["mime_type"]
            disposition = "inline"
        else:
            if document.get("is_preview"):
                raise PermissionError("Preview documents cannot be downloaded as originals")
            path = self.path_for_document(document_id)
            mime = document["mime_type"]
            disposition = "attachment"
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        if range_header and mime == "application/pdf":
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                raise ValueError("Invalid byte range")
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            elif match.group(2):
                length = int(match.group(2))
                start = max(0, size - length)
            if start > end or start >= size:
                raise ValueError("Requested range is outside the document")
            end = min(end, size - 1)
            status = 206
        return {
            "path": path,
            "mime_type": mime,
            "disposition": disposition,
            "filename": document["filename"],
            "start": start,
            "end": end,
            "size": size,
            "status": status,
        }

    def spreadsheet(self, document_id: str, sheet: str = "", cell_range: str = "", page: int = 1, page_size: int = 100) -> dict[str, Any]:
        document = self._authorized_document(document_id)
        file_type = document["original_document_type"]
        if file_type not in SPREADSHEET_TYPES:
            raise ValueError("Document is not a spreadsheet")
        path = self.path_for_document(document_id)
        if file_type == "xls":
            converted = self.preview_root / "spreadsheets" / document_id / f"{document['sha256']}.xlsx"
            if not converted.is_file():
                if not self.converter.available or not hasattr(self.converter, "convert_spreadsheet"):
                    raise RuntimeError("Legacy XLS viewing requires LibreOffice conversion or an XLS parser")
                self.converter.convert_spreadsheet(path, converted)
            path = converted
            file_type = "xlsx"
        page = max(1, page)
        page_size = max(10, min(page_size, 250))
        selection = parse_cell_range(cell_range)
        focus_row = selection[0] if selection else 1
        start_row = max(1, focus_row - 12) if cell_range else (page - 1) * page_size + 1
        end_row = start_row + page_size - 1
        if file_type == "xlsx":
            sheets = xlsx_sheet_names(path)
            selected_sheet = sheet if sheet in sheets else sheets[0]
            rows = _xlsx_cells(path, selected_sheet, start_row, end_row)
        elif file_type == "csv":
            selected_sheet = sheet or "CSV"
            sheets = [selected_sheet]
            rows = []
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for row_number, values in enumerate(csv.reader(stream), start=1):
                    if row_number < start_row:
                        continue
                    if row_number > end_row:
                        break
                    rows.append({
                        "row_number": row_number,
                        "cells": [
                            {"address": f"{column_letters(index)}{row_number}", "column": column_letters(index), "value": value}
                            for index, value in enumerate(values, start=1)
                        ],
                    })
        else:
            raise RuntimeError("Unsupported spreadsheet format")
        max_column = max((column_number(cell["column"]) for row in rows for cell in row["cells"]), default=1)
        return {
            "document_id": document_id,
            "filename": document["filename"],
            "sheet_names": sheets,
            "sheet_name": selected_sheet,
            "selection": cell_range,
            "selection_bounds": selection,
            "start_row": start_row,
            "page": page,
            "page_size": page_size,
            "columns": [column_letters(index) for index in range(1, max_column + 1)],
            "rows": rows,
        }
