#!/usr/bin/env python3
"""Accuracy-first, full-document Gemini ingestion with independent verification."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from corpus_audit import audit_corpus as audit
from phase2 import extract_dataset as base
from web_app.source_registry import _xlsx_cells, xlsx_sheet_names


SUPPORTED_TYPES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv"}
EXTRACTION_PROMPT_VERSION = "visual-document-extraction-v1"
VERIFICATION_PROMPT_VERSION = "visual-document-verification-v1"

LOCATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "pages": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "description": {"type": "STRING"},
        "bounding_boxes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "page": {"type": "INTEGER"}, "x_min": {"type": "NUMBER"}, "y_min": {"type": "NUMBER"},
            "x_max": {"type": "NUMBER"}, "y_max": {"type": "NUMBER"},
        }, "required": ["page", "x_min", "y_min", "x_max", "y_max"]}},
    },
    "required": ["pages", "description", "bounding_boxes"],
}

EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "property": {"type": "STRING"}, "city": {"type": "STRING"},
        "review_round": {"type": "STRING"}, "document_type": {"type": "STRING"},
        "document_uncertain": {"type": "BOOLEAN"}, "document_uncertainty_reason": {"type": "STRING"},
        "records": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "record_key": {"type": "STRING"}, "comment_number": {"type": "STRING"},
            "exact_comment_text": {"type": "STRING"}, "exact_response_text": {"type": "STRING"},
            "comment_location": LOCATION_SCHEMA, "response_location": LOCATION_SCHEMA,
            "uncertain": {"type": "BOOLEAN"}, "uncertainty_reason": {"type": "STRING"},
        }, "required": [
            "record_key", "comment_number", "exact_comment_text", "exact_response_text",
            "comment_location", "response_location", "uncertain", "uncertainty_reason",
        ]}},
    },
    "required": [
        "property", "city", "review_round", "document_type", "document_uncertain",
        "document_uncertainty_reason", "records",
    ],
}

VERIFICATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "document_verified": {"type": "BOOLEAN"}, "every_comment_captured": {"type": "BOOLEAN"},
        "every_response_captured": {"type": "BOOLEAN"}, "verification_summary": {"type": "STRING"},
        "records": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "record_key": {"type": "STRING"}, "comment_captured": {"type": "BOOLEAN"},
            "response_captured": {"type": "BOOLEAN"}, "text_complete_and_verbatim": {"type": "BOOLEAN"},
            "pairing_correct": {"type": "BOOLEAN"}, "verified": {"type": "BOOLEAN"},
            "uncertainty_reason": {"type": "STRING"},
        }, "required": [
            "record_key", "comment_captured", "response_captured", "text_complete_and_verbatim",
            "pairing_correct", "verified", "uncertainty_reason",
        ]}},
    },
    "required": ["document_verified", "every_comment_captured", "every_response_captured", "verification_summary", "records"],
}

EXTRACTION_INSTRUCTION = """You are transcribing a complete permit-review document from every rendered page image plus direct machine text.

Visually understand the actual document structure: tables, rows, numbering, headings, columns, form fields, continuation pages, and the spatial relationship between government comments and applicant/company responses.

Extract every government comment and every company/applicant response. Copy text verbatim. Never summarize, paraphrase, correct spelling, improve grammar, merge separate comments, omit repeated text, or invent missing text. Raw text is supporting evidence only; page images control structure and pairing. An empty response is allowed only when the document truly contains none for that comment.

Locations must identify every source page and describe the row, column, field, heading, or other visible locator. Bounding boxes use normalized page coordinates from 0 to 1000 (top-left origin). Set uncertainty whenever any character, boundary, numbering, completeness, or pairing is not visually certain. Return only the required JSON."""

VERIFICATION_INSTRUCTION = """Independently audit the proposed extraction against every original rendered page image. Do not trust the proposed JSON or raw text.

Verify that every government comment and every response was captured, text is complete and verbatim, boundaries are correct, and each response belongs to the correct comment based on visible table rows, fields, numbering, headings, columns, and continuation structure. A response that merely appears nearby is not enough. Mark a record verified only if every relevant check is true. Mark the entire document unverified if any comment or response is missing, duplicated, combined incorrectly, truncated, paraphrased, or paired incorrectly. Return only the required JSON."""


@dataclass(slots=True)
class PageImage:
    page_number: int
    path: Path
    mime_type: str = "image/jpeg"


@dataclass(slots=True)
class EvidenceBundle:
    artifact_id: str
    source_path: Path
    source_sha256: str
    original_type: str
    raw_text: dict[str, Any]
    pages: list[PageImage]
    artifact_dir: Path


class VisualClient(Protocol):
    def extract_document(self, bundle: EvidenceBundle, context: dict[str, Any]) -> dict[str, Any]: ...
    def verify_document(self, bundle: EvidenceBundle, extraction: dict[str, Any]) -> dict[str, Any]: ...


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.stem + "-", suffix=".tmp", delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _run(command: list[str], purpose: str, timeout: int = 600) -> None:
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{purpose} failed ({completed.returncode}): {detail[:1000]}")


def render_pdf_pages(path: Path, output_dir: Path, dpi: int = 180) -> list[PageImage]:
    ghostscript = shutil.which("gs")
    if not ghostscript:
        raise RuntimeError("Full-page visual ingestion requires Ghostscript (gs)")
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = output_dir / "page-%04d.jpg"
    _run([
        ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=jpeg",
        f"-r{dpi}", "-dJPEGQ=95", f"-sOutputFile={pattern}", str(path.resolve()),
    ], f"Rendering {path.name}")
    paths = sorted(output_dir.glob("page-*.jpg"))
    if not paths:
        raise RuntimeError(f"No rendered pages were produced for {path.name}")
    return [PageImage(index, page) for index, page in enumerate(paths, 1)]


def pdf_direct_text(path: Path) -> dict[str, Any]:
    ghostscript = shutil.which("gs")
    if not ghostscript:
        raise RuntimeError("Direct PDF text extraction requires Ghostscript (gs)")
    with tempfile.TemporaryDirectory(prefix="visual-text-") as temporary:
        pattern = Path(temporary) / "page-%04d.txt"
        _run([
            ghostscript, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=txtwrite",
            f"-sOutputFile={pattern}", str(path.resolve()),
        ], f"Extracting direct text from {path.name}")
        pages = sorted(Path(temporary).glob("page-*.txt"))
        return {"kind": "pdf_text_pages", "pages": [
            {"page": index, "text": item.read_text(encoding="utf-8", errors="replace")}
            for index, item in enumerate(pages, 1)
        ]}


def docx_direct_text(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    blocks: list[dict[str, Any]] = []
    body = next((node for node in root.iter() if audit.xml_local(node.tag) == "body"), root)
    for index, node in enumerate(list(body), 1):
        kind = audit.xml_local(node.tag)
        if kind == "p":
            text = "".join(child.text or "" for child in node.iter() if audit.xml_local(child.tag) == "t")
            blocks.append({"index": index, "kind": "paragraph", "text": text})
        elif kind == "tbl":
            rows = []
            for row in (item for item in node.iter() if audit.xml_local(item.tag) == "tr"):
                cells = []
                for cell in (item for item in row if audit.xml_local(item.tag) == "tc"):
                    cells.append("".join(child.text or "" for child in cell.iter() if audit.xml_local(child.tag) == "t"))
                rows.append(cells)
            blocks.append({"index": index, "kind": "table", "rows": rows})
    return {"kind": "docx_blocks", "blocks": blocks}


def xlsx_direct_text(path: Path) -> dict[str, Any]:
    return {"kind": "xlsx_cells", "sheets": [
        {"name": sheet, "rows": _xlsx_cells(path, sheet)} for sheet in xlsx_sheet_names(path)
    ]}


def csv_direct_text(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    return {"kind": "csv_cells", "rows": [
        {"row_number": index, "values": values} for index, values in enumerate(rows, 1)
    ]}


def office_pdf(path: Path, output_dir: Path) -> Path:
    executable = shutil.which("soffice") or shutil.which("libreoffice")
    if not executable:
        raise RuntimeError(f"Rendering every page of {path.suffix.upper()} requires LibreOffice (soffice)")
    output_dir.mkdir(parents=True, exist_ok=True)
    _run([executable, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(path.resolve())], f"Converting {path.name} to PDF")
    converted = output_dir / f"{path.stem}.pdf"
    if not converted.is_file():
        raise RuntimeError(f"LibreOffice did not produce a PDF for {path.name}")
    return converted


class DocumentEvidenceBuilder:
    def __init__(self, artifact_root: Path, dpi: int = 180):
        self.artifact_root = artifact_root.resolve()
        self.dpi = dpi

    def build(self, source: Path) -> EvidenceBundle:
        source = source.resolve()
        extension = source.suffix.casefold()
        if extension not in SUPPORTED_TYPES:
            raise ValueError(f"Unsupported visual-ingestion file type: {extension or 'none'}")
        digest = sha256_file(source)
        artifact_id = f"VI-{digest[:20]}"
        directory = self.artifact_root / artifact_id
        pages_dir = directory / "pages"
        if pages_dir.exists():
            shutil.rmtree(pages_dir)
        if extension == ".pdf":
            raw_text = pdf_direct_text(source)
            rendered_from = source
        elif extension == ".docx":
            raw_text = docx_direct_text(source)
            rendered_from = office_pdf(source, directory / "rendered")
        elif extension == ".xlsx":
            raw_text = xlsx_direct_text(source)
            rendered_from = office_pdf(source, directory / "rendered")
        elif extension == ".csv":
            raw_text = csv_direct_text(source)
            rendered_from = office_pdf(source, directory / "rendered")
        else:
            raw_text = {"kind": "legacy_office", "text": "Direct parsing unavailable; rendered pages are authoritative."}
            rendered_from = office_pdf(source, directory / "rendered")
        page_images = render_pdf_pages(rendered_from, pages_dir, self.dpi)
        raw_page_count = len(raw_text.get("pages", [])) if raw_text.get("kind") == "pdf_text_pages" else None
        if raw_page_count is not None and raw_page_count != len(page_images):
            raise RuntimeError(f"Page completeness check failed for {source.name}: {len(page_images)} images, {raw_page_count} text pages")
        atomic_json(directory / "raw_text.json", raw_text)
        manifest = {
            "artifact_id": artifact_id, "source_filename": source.name, "source_sha256": digest,
            "original_type": extension.lstrip("."), "render_dpi": self.dpi,
            "page_count": len(page_images), "pages": [
                {"page": page.page_number, "filename": page.path.name, "sha256": sha256_file(page.path)} for page in page_images
            ],
        }
        atomic_json(directory / "manifest.json", manifest)
        return EvidenceBundle(artifact_id, source, digest, extension.lstrip("."), raw_text, page_images, directory)


def multimodal_context(bundle: EvidenceBundle, context: dict[str, Any], extracted: dict[str, Any] | None = None) -> dict[str, Any]:
    introduction = {
        "document": {"filename": bundle.source_path.name, "type": bundle.original_type, "page_count": len(bundle.pages)},
        "known_context_hints": context,
        "direct_extracted_text_complete": bundle.raw_text,
    }
    if extracted is not None:
        introduction["proposed_extraction_to_verify"] = extracted
    return introduction


def multimodal_parts(bundle: EvidenceBundle, context: dict[str, Any], extracted: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    introduction = multimodal_context(bundle, context, extracted)
    parts: list[dict[str, Any]] = [{"text": json.dumps(introduction, ensure_ascii=False)}]
    for page in bundle.pages:
        parts.append({"text": f"ORIGINAL RENDERED PAGE {page.page_number} OF {len(bundle.pages)} — inspect the entire image."})
        parts.append({"inlineData": {
            "mimeType": page.mime_type,
            "data": base64.b64encode(page.path.read_bytes()).decode("ascii"),
        }})
    return parts


class VisualGeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-3.5-flash", timeout: int = 600, inline_limit_bytes: int = 18_000_000):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for visual ingestion")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.inline_limit_bytes = inline_limit_bytes
        self._uploaded_pages: dict[tuple[str, int], tuple[str, str]] = {}

    def _upload_file(self, page: PageImage) -> tuple[str, str]:
        size = page.path.stat().st_size
        start = Request(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            data=json.dumps({"file": {"display_name": page.path.name}}).encode("utf-8"),
            headers={
                "x-goog-api-key": self.api_key, "Content-Type": "application/json",
                "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": page.mime_type,
            }, method="POST",
        )
        try:
            with urlopen(start, timeout=self.timeout) as response:
                upload_url = response.headers.get("X-Goog-Upload-URL", "")
            if not upload_url:
                raise RuntimeError("Gemini Files API did not return an upload URL")
            upload = Request(
                upload_url, data=page.path.read_bytes(), headers={
                    "Content-Length": str(size), "Content-Type": page.mime_type,
                    "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                }, method="POST",
            )
            with urlopen(upload, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            file = body["file"]
            return str(file["uri"]), str(file.get("mimeType") or page.mime_type)
        except (HTTPError, OSError, URLError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Gemini page upload failed for page {page.page_number}: {exc}") from exc

    def _parts(self, bundle: EvidenceBundle, context: dict[str, Any], extracted: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        introduction = multimodal_context(bundle, context, extracted)
        estimated_inline = len(json.dumps(introduction, ensure_ascii=False).encode("utf-8")) + sum(
            ((page.path.stat().st_size + 2) // 3) * 4 for page in bundle.pages
        )
        if estimated_inline <= self.inline_limit_bytes:
            return multimodal_parts(bundle, context, extracted)
        parts: list[dict[str, Any]] = [{"text": json.dumps(introduction, ensure_ascii=False)}]
        for page in bundle.pages:
            key = (bundle.artifact_id, page.page_number)
            if key not in self._uploaded_pages:
                self._uploaded_pages[key] = self._upload_file(page)
            uri, mime_type = self._uploaded_pages[key]
            parts.append({"text": f"ORIGINAL RENDERED PAGE {page.page_number} OF {len(bundle.pages)} — inspect the entire image."})
            parts.append({"fileData": {"mimeType": mime_type, "fileUri": uri}})
        return parts

    def _request(self, instruction: str, parts: list[dict[str, Any]], schema: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "systemInstruction": {"parts": [{"text": instruction}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.0, "maxOutputTokens": 65536,
                "responseMimeType": "application/json", "responseSchema": schema,
            },
        }
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model, safe='')}:generateContent"
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(5):
            request = Request(endpoint, data=encoded, headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key}, method="POST")
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                candidate = body["candidates"][0]
                finish = str(candidate.get("finishReason", "STOP"))
                if finish not in {"STOP", ""}:
                    raise RuntimeError(f"Gemini stopped before a complete result: {finish}")
                raw = "".join(str(part.get("text", "")) for part in candidate["content"]["parts"])
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise TypeError("Gemini result is not an object")
                return result
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1200]
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise RuntimeError(f"Gemini visual ingestion HTTP {exc.code}: {detail}") from exc
            except (OSError, URLError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                if attempt == 4:
                    raise RuntimeError(f"Gemini visual ingestion failed: {exc}") from exc
            time.sleep(min(30, 2 ** attempt * 2))
        raise RuntimeError("Gemini visual ingestion produced no response")

    def extract_document(self, bundle: EvidenceBundle, context: dict[str, Any]) -> dict[str, Any]:
        return self._request(EXTRACTION_INSTRUCTION, self._parts(bundle, context), EXTRACTION_SCHEMA)

    def verify_document(self, bundle: EvidenceBundle, extraction: dict[str, Any]) -> dict[str, Any]:
        return self._request(VERIFICATION_INSTRUCTION, self._parts(bundle, {}, extraction), VERIFICATION_SCHEMA)


def verification_map(verification: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("record_key", "")): row for row in verification.get("records", []) if isinstance(row, dict)
    }


def document_verified(verification: dict[str, Any]) -> bool:
    return all(verification.get(field) is True for field in (
        "document_verified", "every_comment_captured", "every_response_captured",
    ))


def result_is_verified(result: dict[str, Any], verification: dict[str, Any]) -> tuple[bool, str]:
    check = verification_map(verification).get(str(result.get("record_key", "")), {})
    checks = all(check.get(field) is True for field in (
        "comment_captured", "response_captured", "text_complete_and_verbatim", "pairing_correct", "verified",
    ))
    verified = document_verified(verification) and checks and result.get("uncertain") is False
    reasons = [str(result.get("uncertainty_reason", "")).strip(), str(check.get("uncertainty_reason", "")).strip()]
    if not document_verified(verification):
        reasons.append(str(verification.get("verification_summary", "Document-level verification failed")))
    return verified, " ".join(value for value in reasons if value)


def normalized_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def regression_against_oracle(extraction: dict[str, Any], dataset: dict[str, Any], filename: str) -> dict[str, Any]:
    comments = {row["comment_id"]: row for row in dataset.get("comments", [])}
    responses = {row["response_id"]: row for row in dataset.get("responses", [])}
    links = [
        row for row in dataset.get("comment_response_links", [])
        if row.get("provenance") == "document_structure_rematch" and Path(str(row.get("source_pdf", ""))).name == filename
    ]
    if not links:
        return {"applicable": False, "passed": True, "expected": 0, "actual": len(extraction.get("records", [])), "failures": []}
    expected: dict[str, dict[str, str]] = {}
    for link in links:
        expected[str(link.get("city_comment_id", ""))] = {
            "comment": str(link.get("imported_current_round_comment_text", "")),
            "response": str(responses.get(str(link.get("response_id", "")), {}).get("original_text", "")),
        }
    actual: dict[str, list[dict[str, Any]]] = {}
    for row in extraction.get("records", []):
        actual.setdefault(str(row.get("comment_number", "")), []).append(row)
    failures: list[dict[str, str]] = []
    for number, oracle in expected.items():
        rows = actual.get(number, [])
        if len(rows) != 1:
            failures.append({"comment_number": number, "reason": f"expected one record, found {len(rows)}"})
            continue
        row = rows[0]
        if normalized_whitespace(str(row.get("exact_comment_text", ""))) != normalized_whitespace(oracle["comment"]):
            failures.append({"comment_number": number, "reason": "comment text differs from confirmed reference"})
        if normalized_whitespace(str(row.get("exact_response_text", ""))) != normalized_whitespace(oracle["response"]):
            failures.append({"comment_number": number, "reason": "response text differs from confirmed reference"})
    unexpected = sorted(set(actual) - set(expected))
    failures.extend({"comment_number": number, "reason": "unexpected record"} for number in unexpected)
    return {
        "applicable": True, "passed": not failures, "expected": len(expected),
        "actual": len(extraction.get("records", [])), "failures": failures,
    }


def location_pages(value: Any) -> list[int]:
    if not isinstance(value, dict) or not isinstance(value.get("pages"), list):
        return []
    return [int(page) for page in value["pages"] if str(page).isdigit() and int(page) > 0]


def location_text(value: Any, fallback: str) -> str:
    pages = location_pages(value)
    description = str(value.get("description", "")).strip() if isinstance(value, dict) else ""
    page_label = "pages " + ", ".join(map(str, pages)) if len(pages) > 1 else (f"page {pages[0]}" if pages else "unknown page")
    return f"{page_label} · {description}" if description else (page_label or fallback)


def results_to_dataset_rows(
    bundle: EvidenceBundle, extraction: dict[str, Any], verification: dict[str, Any],
    source_relative: str, regression: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    comments: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    force_review = bool(
        extraction.get("document_uncertain") is True
        or (regression and regression.get("applicable") and not regression.get("passed"))
    )
    number_counts: dict[str, int] = {}
    key_counts: dict[str, int] = {}
    for row in extraction.get("records", []):
        number = str(row.get("comment_number", "")).strip()
        number_counts[number] = number_counts.get(number, 0) + 1
        key = str(row.get("record_key", "")).strip()
        key_counts[key] = key_counts.get(key, 0) + 1
    for index, result in enumerate(extraction.get("records", []), 1):
        record_key = str(result.get("record_key", "")).strip() or f"record-{index}"
        number = str(result.get("comment_number", "")).strip()
        verified, uncertainty = result_is_verified(result, verification)
        comment_pages = location_pages(result.get("comment_location"))
        response_pages = location_pages(result.get("response_location"))
        locations_valid = bool(comment_pages) and all(page <= len(bundle.pages) for page in comment_pages)
        if str(result.get("exact_response_text", "")):
            locations_valid = locations_valid and bool(response_pages) and all(page <= len(bundle.pages) for page in response_pages)
        if force_review or number_counts[number] > 1 or key_counts[record_key] > 1 or not number or not str(result.get("exact_comment_text", "")) or not locations_valid:
            verified = False
        if force_review:
            prefix = str(extraction.get("document_uncertainty_reason", "")).strip()
            if regression and regression.get("applicable") and not regression.get("passed"):
                prefix = "Confirmed-reference regression failed. " + prefix
            uncertainty = (prefix + " " + uncertainty).strip()
        if number_counts[number] > 1:
            uncertainty = f"Duplicate comment number {number!r}. " + uncertainty
        if key_counts[record_key] > 1:
            uncertainty = f"Duplicate Gemini record_key {record_key!r}. " + uncertainty
        if not locations_valid:
            uncertainty = "One or more required source pages are missing or outside the rendered document. " + uncertainty
        status = "confirmed" if verified else "needs_review"
        record_identity = record_key if key_counts[record_key] == 1 else f"{record_key}:duplicate:{index}"
        comment_id = base.stable_id("C", bundle.source_sha256, record_identity, "visual")
        response_text = str(result.get("exact_response_text", ""))
        response_id = base.stable_id("R", bundle.source_sha256, record_identity, "visual") if response_text else ""
        audit_payload = {
            "artifact_id": bundle.artifact_id, "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
            "verification_prompt_version": VERIFICATION_PROMPT_VERSION,
            "gemini_record_key": record_key, "uncertainty_reason": uncertainty,
        }
        comment = {
            "comment_id": comment_id, "city": str(extraction.get("city", "")),
            "property_project": str(extraction.get("property", "")), "review_round": str(extraction.get("review_round", "")),
            "discipline": "unknown", "reviewer": "", "reviewer_context": "", "comment_number": number,
            "original_text": str(result.get("exact_comment_text", "")), "source_document": source_relative,
            "source_sha256": bundle.source_sha256, "source_sheet": "", "source_row": "",
            "source_page": comment_pages[0] if comment_pages else "", "source_page_end": comment_pages[-1] if comment_pages else "",
            "source_location": location_text(result.get("comment_location"), "unknown"),
            "source_locator_json": result.get("comment_location", {}), "extraction_method": "gemini_visual_two_pass",
            "extraction_confidence": 1.0 if verified else 0.0, "source_cycle": str(extraction.get("review_round", "")),
            "source_status": status, "response_id": response_id,
            "match_status": "matched" if response_id else "unmatched", "human_review_status": status,
            "verification_status": status,
            "ingestion_audit": audit_payload,
        }
        comments.append(comment)
        if response_id:
            responses.append({
                "response_id": response_id, "comment_id": comment_id, "original_text": response_text,
                "source_document": source_relative, "source_sha256": bundle.source_sha256,
                "source_sheet": "", "source_row": "", "source_page": response_pages[0] if response_pages else "",
                "source_page_end": response_pages[-1] if response_pages else "",
                "source_location": location_text(result.get("response_location"), "unknown"),
                "source_locator_json": result.get("response_location", {}),
                "extraction_method": "gemini_visual_two_pass", "extraction_confidence": 1.0 if verified else 0.0,
                "human_review_status": status, "ingestion_audit": audit_payload,
                "verification_status": status,
            })
        link_id = base.stable_id("L", comment_id, response_id or "NONE", "gemini_visual_two_pass")
        links.append({
            "link_id": link_id, "comment_id": comment_id, "response_id": response_id,
            "match_status": "matched" if response_id else "unmatched", "matching_method": "gemini_visual_verified",
            "match_confidence": 1.0 if verified else 0.0, "review_status": status,
            "verification_status": status,
            "provenance": "gemini_visual_two_pass", "source_document": source_relative,
            "source_location": location_text(result.get("comment_location"), "unknown"),
            "comment_locator_json": result.get("comment_location", {}),
            "response_locator_json": result.get("response_location", {}), "ingestion_audit": audit_payload,
        })
        if not verified:
            review.append({
                "item_type": "gemini_visual_record", "item_id": link_id,
                "reason": uncertainty or "Visual extraction or matching did not pass independent verification",
                "source_document": source_relative, "source_location": comment["source_location"],
                "suggested_action": "Compare the verbatim extraction and pairing against every cited page image",
                "decision": "", "decision_note": "",
            })
    if not extraction.get("records") or not document_verified(verification):
        review.append({
            "item_type": "gemini_visual_document", "item_id": bundle.artifact_id,
            "reason": str(verification.get("verification_summary", "No records were extracted or document verification failed")),
            "source_document": source_relative, "source_location": "complete document",
            "suggested_action": "Review every rendered page and compare it with the saved extraction and verification artifacts",
            "decision": "", "decision_note": "",
        })
    summary = {
        "city": str(extraction.get("city", "")), "property_project": str(extraction.get("property", "")),
        "review_round": str(extraction.get("review_round", "")), "source_document": source_relative,
        "source_type": str(extraction.get("document_type", "")) or "gemini_visual_document",
        "comment_count": len(comments), "response_count": len(responses),
        "matched_count": sum(row["response_id"] != "" for row in links),
        "unmatched_count": sum(row["response_id"] == "" for row in links),
        "extraction_method": "gemini_visual_two_pass",
        "processing_error": "" if all(row["review_status"] == "confirmed" for row in links) else "One or more records require review",
        "ingestion_artifact_id": bundle.artifact_id,
    }
    return comments, responses, links, summary, review


class VisualIngestionPipeline:
    def __init__(
        self, client: VisualClient, artifact_root: Path, oracle_dataset: Path | None = None,
        dpi: int = 180,
    ):
        self.client = client
        self.builder = DocumentEvidenceBuilder(artifact_root, dpi)
        self.oracle_dataset = oracle_dataset

    def process(self, source: Path, source_relative: str, context: dict[str, Any], force: bool = False):
        bundle = self.builder.build(source)
        extraction_path = bundle.artifact_dir / "gemini_extraction.json"
        verification_path = bundle.artifact_dir / "gemini_verification.json"
        if not force and extraction_path.is_file() and verification_path.is_file():
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
        else:
            extraction = self.client.extract_document(bundle, context)
            atomic_json(extraction_path, extraction)
            verification = self.client.verify_document(bundle, extraction)
            atomic_json(verification_path, verification)
        context_conflicts: list[str] = []
        city_hint = str(context.get("city_hint", "")).strip()
        round_hint = str(context.get("review_round_hint", "")).strip()
        if city_hint and normalized_whitespace(str(extraction.get("city", ""))).casefold() != normalized_whitespace(city_hint).casefold():
            context_conflicts.append(f"Gemini city {extraction.get('city')!r} conflicts with audited city {city_hint!r}")
        if round_hint and normalized_whitespace(str(extraction.get("review_round", ""))).casefold() != normalized_whitespace(round_hint).casefold():
            context_conflicts.append(f"Gemini review round {extraction.get('review_round')!r} conflicts with audited round {round_hint!r}")
        if context_conflicts:
            extraction["document_uncertain"] = True
            existing_reason = str(extraction.get("document_uncertainty_reason", "")).strip()
            extraction["document_uncertainty_reason"] = " ".join([*context_conflicts, existing_reason]).strip()
        regression = None
        if self.oracle_dataset and self.oracle_dataset.is_file():
            oracle = json.loads(self.oracle_dataset.read_text(encoding="utf-8"))
            regression = regression_against_oracle(extraction, oracle, source.name)
            atomic_json(bundle.artifact_dir / "confirmed_reference_regression.json", regression)
        audit = {
            "artifact_id": bundle.artifact_id, "source_filename": source.name,
            "source_sha256": bundle.source_sha256, "raw_text_artifact": "raw_text.json",
            "extraction_artifact": "gemini_extraction.json", "verification_artifact": "gemini_verification.json",
            "page_count": len(bundle.pages), "regression": regression,
        }
        atomic_json(bundle.artifact_dir / "audit.json", audit)
        return results_to_dataset_rows(bundle, extraction, verification, source_relative, regression)
