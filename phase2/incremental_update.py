#!/usr/bin/env python3
"""Append newly audited permit sources without re-extracting recorded sources."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import csv
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from corpus_audit import audit_corpus as audit
from phase2 import extract_dataset as base
from phase2.issue_event_dedup import (
    assign_issue_threads,
    build_issue_event_index,
    collect_issue_event_review_queue,
)
from phase2.visual_ingestion import (
    CHECKPOINT_SCHEMA_VERSION, EXTRACTION_PROMPT_VERSION, PAGE_SCREENING_VERSION,
    PIPELINE_VERSION, PRESCAN_PROMPT_VERSION, SUPPORTED_TYPES,
    TEXT_EXTRACTION_VERSION, VERIFICATION_PROMPT_VERSION,
    GeminiCircuitOpenError, VisualGeminiClient, VisualIngestionPipeline,
    atomic_json, sha256_file, utc_timestamp,
)
from phase2.straggler_monitor import summarize_request_metrics
from phase2.evidence_model import materialize_evidence_model
from web_app.comment_dedup import mark_duplicate_comments
from web_app.comment_hierarchy import (
    merge_docx_comment_hierarchy,
    read_docx_paragraphs,
)
from web_app.source_lineage import document_date as derive_document_date, mark_copied_source_documents
from web_app.document_identity import canonicalize_documents, source_file_id
from web_app.local_secrets import gemini_api_key


def rebuild_issue_event_index(comments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rebuild persisted recurring-issue history whenever the dataset is written.

    ``issue_event_index`` is a derived presentation index, not an immutable
    source field.  Recomputing it after a merge/repair keeps Recurring Issues
    available after incremental ingestion instead of silently dropping the
    index from the JSON envelope.
    """
    assign_issue_threads(comments)
    return build_issue_event_index(comments)


def write_pipeline_checkpoint(
    output_dir: Path,
    run_id: str,
    stages: dict[str, str],
    **details: Any,
) -> dict[str, Any]:
    """Persist resumable stage state independently from dataset materialization."""
    path = output_dir / "pipeline_checkpoint.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError, TypeError):
        previous = {}
    version_map = {
        "uploaded": "inventory-v1", "parsed": TEXT_EXTRACTION_VERSION,
        "prescanned": PRESCAN_PROMPT_VERSION,
        "extracted": EXTRACTION_PROMPT_VERSION,
        "verified": VERIFICATION_PROMPT_VERSION,
        "deduplicated": CHECKPOINT_SCHEMA_VERSION,
        "timeline_linked": PIPELINE_VERSION,
        "indexed": PIPELINE_VERSION,
    }
    entries = previous.get("stages", {}) if isinstance(previous.get("stages"), dict) else {}
    now = utc_timestamp()
    for name, status in stages.items():
        entries[name] = {
            "status": status,
            "updated_at": now,
            "version": version_map.get(name, PIPELINE_VERSION),
        }
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "run_id": run_id,
        "updated_at": now,
        "stages": entries,
        **details,
    }
    atomic_json(path, payload)
    return payload


def _metadata_from_path(relative_path: str) -> dict[str, str]:
    city, _confidence, _evidence = audit.infer_city(Path(relative_path), "")
    city = "Unknown" if city.casefold() == "unknown" else city
    round_match = re.search(r"(?:^|[/ ])(?:pc|round\s*)(\d+)", relative_path, re.IGNORECASE)
    review_round = round_match.group(1) if round_match else ""
    project_folder = Path(relative_path).parts[1] if len(Path(relative_path).parts) > 1 else ""
    return {"city": city, "project": project_folder, "review_round": review_round}


def _known_city(value: Any) -> bool:
    return bool(str(value or "").strip()) and str(value).casefold() != "unknown"


def _explicit_city_from_site_folder(relative_path: str) -> str:
    """Return the city explicitly encoded in ``Address, City, CA ZIP``.

    Contact addresses inside Title-24 and consultant documents frequently name
    a different city.  The project-folder address is therefore the stronger
    site-level jurisdiction signal when it contains this complete pattern.
    """
    site = _site_folder(relative_path)
    match = re.search(
        r",\s*([^,/]+?)\s*,\s*CA\s+\d{5}(?:-\d{4})?(?:\b|$)",
        site, re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", match.group(1)).strip().title() if match else ""


def quick_city_for_source(
    path: Path,
    relative_path: str,
    audit_row: dict[str, Any] | None = None,
    *,
    allow_pdf: bool = False,
) -> tuple[str, float, list[str], str]:
    """Determine jurisdiction from bounded local content without an AI call."""
    audit_row = audit_row or {}
    folder_city = _explicit_city_from_site_folder(relative_path)
    if folder_city:
        return (
            folder_city, 1.0,
            [f"project folder explicitly identifies {folder_city}, CA"],
            "explicit_site_folder_address",
        )
    candidates: list[tuple[str, float, list[str], str]] = []
    audit_city = str(audit_row.get("likely_city", ""))
    if _known_city(audit_city):
        try:
            confidence = float(audit_row.get("city_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = audit_row.get("city_evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        candidates.append((audit_city, confidence, evidence, "audit_cache"))
        if confidence >= 0.99 and any(
            marker in " ".join(evidence).casefold()
            for marker in (
                "authoritative city signal",
                "postal address naming",
                "source identifies city of",
                "propagated from authoritative source",
            )
        ):
            return audit_city, confidence, evidence, str(
                audit_row.get("city_resolution_method") or "audit_cache"
            )

    path_city, path_confidence, path_evidence = audit.infer_city(
        Path(relative_path), "",
    )
    if _known_city(path_city):
        candidates.append((
            path_city, path_confidence, path_evidence, "folder_or_filename",
        ))

    suffix = path.suffix.casefold()
    content = ""
    try:
        if suffix == ".xlsx":
            detail = audit.inspect_xlsx(path)
            content = str(
                detail.get("content_sample") or detail.get("sample_signals", "")
            )
        elif suffix in {".csv", ".tsv"}:
            detail = audit.inspect_delimited(path)
            content = str(
                detail.get("content_sample") or detail.get("sample_signals", "")
            )
        elif suffix == ".ods":
            detail = audit.inspect_ods(path)
            content = str(
                detail.get("content_sample") or detail.get("sample_signals", "")
            )
        elif suffix == ".docx":
            content = str(audit.inspect_docx(path).get("text", ""))[:20_000]
        elif suffix in audit.TEXT_EXTENSIONS:
            content = path.read_text(
                encoding="utf-8", errors="replace",
            )[:20_000]
        elif suffix == ".pdf" and allow_pdf:
            content = str(audit.inspect_pdf(path).get("text", ""))[:20_000]
    except (OSError, RuntimeError, zipfile.BadZipFile, ET.ParseError):
        content = ""
    if content:
        content_city, confidence, evidence = audit.infer_city(
            Path(relative_path), content,
        )
        if _known_city(content_city):
            candidates.append((
                content_city, confidence, evidence, "bounded_source_content",
            ))

    if not candidates:
        return "Unknown", 0.0, ["no local city evidence"], "unresolved"
    city, confidence, evidence, method = max(
        candidates,
        key=lambda item: (
            item[1],
            item[3] == "bounded_source_content",
            item[0],
        ),
    )
    conflicting = sorted({
        item[0] for item in candidates
        if item[0] != city and item[1] >= 0.82
    })
    if conflicting:
        evidence = list(evidence) + [
            "selected over conflicting candidate(s): " + ", ".join(conflicting)
        ]
    return city, confidence, evidence, method


def _site_folder(relative_path: str) -> str:
    parts = Path(relative_path).parts
    return parts[1] if len(parts) > 1 and parts[0] in {"comments&response", "new"} else (
        parts[0] if parts else ""
    )


def load_prescan_decisions(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load per-file routing without trusting group-level city/round hints."""
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    decisions: dict[str, dict[str, Any]] = {}
    for group in payload.get("groups", []) if isinstance(payload, dict) else []:
        if not isinstance(group, dict):
            continue
        for row in group.get("files", []) or []:
            if not isinstance(row, dict):
                continue
            relative = str(row.get("relative_path", ""))
            if relative:
                decisions[relative] = row
    return decisions


def resolve_inventory_cities(
    workspace: Path,
    files: list[dict[str, Any]],
    audit_inventory: dict[str, dict[str, Any]],
) -> None:
    """Propagate one authoritative city signal across a site folder."""
    by_site: dict[str, list[dict[str, Any]]] = {}
    for row in files:
        by_site.setdefault(_site_folder(str(row["relative_path"])), []).append(row)

    for site_rows in by_site.values():
        folder_city = _explicit_city_from_site_folder(
            str(site_rows[0]["relative_path"]),
        ) if site_rows else ""
        if folder_city:
            for row in site_rows:
                row.update(
                    city=folder_city,
                    city_confidence=1.0,
                    city_evidence=[
                        f"project folder explicitly identifies {folder_city}, CA",
                    ],
                    city_resolution_method="explicit_site_folder_address",
                )
            continue
        strong = [
            row for row in site_rows
            if _known_city(row.get("city"))
            and float(row.get("city_confidence") or 0.0) >= 0.82
        ]
        strong_cities = {str(row["city"]) for row in strong}
        if not strong:
            pdf_candidates = sorted(
                (
                    row for row in site_rows
                    if str(row.get("file_type", "")).casefold() == "pdf"
                ),
                key=lambda row: (
                    not bool(re.search(
                        r"comment|response|review|cover|letter",
                        str(row.get("filename", "")), re.I,
                    )),
                    int(row.get("file_size_bytes") or 0),
                ),
            )
            if pdf_candidates:
                row = pdf_candidates[0]
                city, confidence, evidence, method = quick_city_for_source(
                    workspace / str(row["relative_path"]),
                    str(row["relative_path"]),
                    audit_inventory.get(str(row["relative_path"]), {}),
                    allow_pdf=True,
                )
                row.update(
                    city=city,
                    city_confidence=round(confidence, 2),
                    city_evidence=evidence,
                    city_resolution_method=method,
                )
                if _known_city(city) and confidence >= 0.82:
                    strong = [row]
                    strong_cities = {city}
        if len(strong_cities) != 1:
            continue
        city = next(iter(strong_cities))
        source = max(strong, key=lambda row: float(row.get("city_confidence") or 0.0))
        for row in site_rows:
            if _known_city(row.get("city")):
                continue
            row.update(
                city=city,
                city_confidence=round(float(source.get("city_confidence") or 0.0), 2),
                city_evidence=[
                    f"propagated from authoritative source in site folder: {source['filename']}",
                    *list(source.get("city_evidence") or []),
                ],
                city_resolution_method="site_folder_propagation",
            )


def inventory_supported_files(
    workspace: Path,
    audit_inventory: dict[str, dict[str, Any]],
    report_path: Path,
) -> dict[str, Any]:
    """Register every supported source; filenames affect priority, never inclusion."""
    discovery_started = time.perf_counter()
    workspace = workspace.resolve()
    source_roots = [
        root for root in (
            workspace / "comments&response",
            workspace / "new",
        )
        if root.is_dir()
    ]
    if not source_roots:
        raise ValueError(
            "No source folders found; expected comments&response/ or new/"
        )
    previous_entries: list[dict[str, Any]] = []
    if report_path.is_file():
        try:
            previous_entries = json.loads(report_path.read_text(encoding="utf-8")).get("files", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            previous_entries = []
    previous_by_path = {
        str(row.get("relative_path", "")): row
        for row in previous_entries if isinstance(row, dict)
    }
    previous_by_hash = {
        str(row.get("sha256", "")): row
        for row in previous_entries if isinstance(row, dict) and row.get("sha256")
    }
    files: list[dict[str, Any]] = []
    discovered_paths = sorted({
        path
        for source_root in source_roots
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.casefold() in SUPPORTED_TYPES
    })
    for path in discovered_paths:
        relative = path.relative_to(workspace).as_posix()
        stat = path.stat()
        previous = previous_by_path.get(relative, {})
        unchanged = (
            previous.get("file_size_bytes") == stat.st_size
            and previous.get("source_mtime_ns") == stat.st_mtime_ns
            and previous.get("sha256")
        )
        hash_started = time.perf_counter()
        digest = str(previous.get("sha256")) if unchanged else sha256_file(path)
        hashing_seconds = time.perf_counter() - hash_started
        audit_row = audit_inventory.get(relative, {})
        if not audit_row and unchanged and previous:
            # ``new/`` sources are not necessarily present in the legacy audit
            # workbook.  The reconciled ingestion report already stores the
            # same cheap local classification, so reuse it when size/mtime are
            # unchanged instead of reopening hundreds of files on every
            # incremental site run.
            audit_row = previous
        if not audit_row:
            # New files should receive the same cheap local classification as a
            # full audit before Gemini routing. This avoids filename-only
            # fallback decisions while keeping existing audited files cached.
            owning_root = next(
                root for root in source_roots if path.is_relative_to(root)
            )
            audit_row = audit.inspect_file(path, owning_root, workspace)
        if unchanged and _known_city(previous.get("city")):
            audit_row = {
                **audit_row,
                "likely_city": previous.get("city"),
                "city_confidence": previous.get("city_confidence", 0.0),
                "city_evidence": previous.get("city_evidence", []),
                "city_resolution_method": previous.get(
                    "city_resolution_method", "inventory_cache",
                ),
            }
        fallback = _metadata_from_path(relative)
        city, city_confidence, city_evidence, city_method = quick_city_for_source(
            path, relative, audit_row,
        )
        hash_cache = previous_by_hash.get(digest, {})
        cache_reused = bool(
            hash_cache
            and hash_cache.get("ingestion_pipeline_version") == PIPELINE_VERSION
            and hash_cache.get("processing_status") not in {
                "", "pending", "failed", "paused_quota", "circuit_open",
            }
        )
        status = (
            str(hash_cache.get("processing_status"))
            if cache_reused
            else str(previous.get("processing_status", "pending"))
            if (
                unchanged
                and previous.get("ingestion_pipeline_version") == PIPELINE_VERSION
                and previous.get("processing_status") not in {
                    "failed", "paused_quota", "circuit_open",
                }
            )
            else "pending"
        )
        files.append({
            "relative_path": relative,
            "folder_path": path.parent.relative_to(workspace).as_posix(),
            "filename": path.name,
            "file_type": path.suffix.casefold().lstrip("."),
            "file_size_bytes": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "sha256": digest,
            "hashing_seconds": round(hashing_seconds, 6),
            "project": str(audit_row.get("likely_property_project") or fallback["project"]),
            "city": city if _known_city(city) else fallback["city"],
            "city_confidence": round(city_confidence, 2),
            "city_evidence": city_evidence,
            "city_resolution_method": city_method,
            "submission_round": str(audit_row.get("likely_review_round") or fallback["review_round"]),
            "document_type": str(audit_row.get("document_type", "unknown")),
            "likely_contains_city_comments": bool(
                audit_row.get("likely_contains_city_comments")
            ),
            "likely_contains_company_responses": bool(
                audit_row.get("likely_contains_company_responses")
            ),
            "likely_contains_both": bool(audit_row.get("likely_contains_both")),
            "appears_drawing_heavy": bool(
                audit_row.get("appears_drawing_heavy")
            ),
            "classification_evidence": audit_row.get(
                "classification_evidence", [],
            ),
            "page_count": audit_row.get("page_count", ""),
            "sheet_count": audit_row.get("sheet_count", ""),
            "sheet_names": audit_row.get("sheet_names", []),
            "processing_status": status,
            "ingestion_pipeline_version": PIPELINE_VERSION,
            "cache_reused_from": str(hash_cache.get("relative_path", "")) if cache_reused else "",
            "opened": bool(hash_cache.get("opened")) if cache_reused else False,
            "pages_screened": hash_cache.get("pages_screened", []) if cache_reused else [],
            "pages_fully_analyzed": hash_cache.get("pages_fully_analyzed", []) if cache_reused else [],
            "comments_extracted": int(hash_cache.get("comments_extracted") or 0) if cache_reused else 0,
            "responses_extracted": int(hash_cache.get("responses_extracted") or 0) if cache_reused else 0,
            "additional_markup_detected": bool(hash_cache.get("additional_markup_detected")) if cache_reused else False,
            "verification_result": str(hash_cache.get("verification_result", "")) if cache_reused else "",
            "review_reason": str(hash_cache.get("review_reason", "")) if cache_reused else "",
        })
    resolve_inventory_cities(workspace, files, audit_inventory)
    if files:
        files[0]["inventory_discovery_seconds"] = round(
            time.perf_counter() - discovery_started, 6,
        )
    return write_ingestion_report(report_path, files, [])


def write_ingestion_report(
    report_path: Path,
    inventory_files: list[dict[str, Any]],
    source_summaries: list[dict[str, Any]],
    run_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_path = {str(row.get("relative_path", "")): row for row in inventory_files}
    for summary in source_summaries:
        paths = [
            item.strip() for item in str(summary.get("source_document", "")).split(" | ")
            if item.strip()
        ]
        for path in paths:
            row = by_path.get(path)
            if not row:
                continue
            row.update({
                "opened": bool(summary.get("opened", True)),
                "processing_status": str(summary.get("processing_status") or (
                    "needs_review" if summary.get("processing_error") else "classified"
                )),
                "pages_screened": summary.get("pages_screened", []),
                "pages_fully_analyzed": summary.get("pages_fully_analyzed", []),
                "comments_extracted": int(summary.get("comment_count") or 0),
                "responses_extracted": int(summary.get("response_count") or 0),
                "additional_markup_detected": bool(summary.get("additional_markup_detected")),
                "verification_result": str(summary.get("verification_result", "")),
                "review_reason": str(summary.get("processing_error", "")),
                "expected_comment_count": int(
                    summary.get("expected_comment_count") or 0
                ),
                "verified_comment_count": int(
                    summary.get("verified_comment_count") or 0
                ),
                "unresolved_signal_count": int(
                    summary.get("unresolved_signal_count") or 0
                ),
                "pages_escalated": summary.get("pages_escalated", []),
                "completion_status": str(summary.get("completion_status", "")),
                "performance": summary.get("performance", {}),
                "prescan_performance": summary.get("prescan_performance", {}),
                "parser_version": str(summary.get("parser_version", TEXT_EXTRACTION_VERSION)),
                "page_screening_version": str(summary.get("page_screening_version", PAGE_SCREENING_VERSION)),
                "prescan_prompt_version": str(summary.get("prescan_prompt_version", PRESCAN_PROMPT_VERSION)),
                "extraction_prompt_version": str(summary.get("extraction_prompt_version", EXTRACTION_PROMPT_VERSION)),
                "verification_prompt_version": str(summary.get("verification_prompt_version", VERIFICATION_PROMPT_VERSION)),
                "dedup_version": str(summary.get("dedup_version", CHECKPOINT_SCHEMA_VERSION)),
                "verification_contract_version": str(summary.get("verification_contract_version", "")),
                "pair_verification": copy.deepcopy(summary.get("pair_verification", {})),
                "coverage_verification": copy.deepcopy(summary.get("coverage_verification", {})),
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
    files = sorted(by_path.values(), key=lambda row: str(row.get("relative_path", "")))
    cached = sum(bool(row.get("cache_reused_from")) for row in files)
    failed = sum(
        not row.get("cache_reused_from") and row.get("processing_status") == "failed"
        for row in files
    )
    pending = sum(
        not row.get("cache_reused_from") and row.get("processing_status") == "pending"
        for row in files
    )
    paused_quota = sum(
        not row.get("cache_reused_from")
        and row.get("processing_status") in {"paused_quota", "circuit_open"}
        for row in files
    )
    processed = len(files) - cached - failed - pending - paused_quota
    by_status: dict[str, int] = {}
    for row in files:
        status = str(row.get("processing_status", "pending"))
        by_status[status] = by_status.get(status, 0) + 1
    performance_rows = [
        row.get("performance", {}) for row in files
        if isinstance(row.get("performance"), dict)
    ]

    def page_count(value: Any) -> int:
        """Accept both legacy page-number lists and newer numeric counters."""
        if isinstance(value, (list, tuple, set)):
            return len(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
        return 0

    performance = {
        "discovery_seconds": max(
            (
                float(row.get("inventory_discovery_seconds") or 0.0)
                for row in files
            ),
            default=0.0,
        ),
        "hashing_seconds": round(sum(
            float(row.get("hashing_seconds") or 0.0) for row in files
        ), 4),
        "canonicalization_seconds": round(sum(
            float(row.get("canonicalization_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "text_extraction_seconds": round(sum(
            float(row.get("text_extraction_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "office_conversion_seconds": round(sum(
            float(row.get("office_conversion_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "page_screening_and_ocr_seconds": round(sum(
            float(row.get("page_screening_and_ocr_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "selected_page_rendering_seconds": round(sum(
            float(row.get("selected_page_rendering_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "total_wall_seconds": round(sum(
            float(row.get("total_wall_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "local_evidence_build_seconds": round(sum(
            float(row.get("local_evidence_build_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "gemini_extraction_seconds": round(sum(
            float(row.get("gemini_extraction_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "gemini_verification_seconds": round(sum(
            float(row.get("gemini_verification_seconds") or 0.0)
            for row in performance_rows
        ), 4),
        "gemini_calls": sum(
            int(row.get("gemini_extraction_calls") or 0)
            + int(row.get("gemini_verification_calls") or 0)
            for row in performance_rows
        ),
        "extraction_cache_hits": sum(
            int(row.get("extraction_cache_hits") or 0)
            for row in performance_rows
        ),
        "verification_cache_hits": sum(
            int(row.get("verification_cache_hits") or 0)
            for row in performance_rows
        ),
        "confidence_escalated_pages": sum(
            int(row.get("confidence_escalated_pages") or 0)
            for row in performance_rows
        ),
        "gemini_input_tokens": None,
        "pages_screened": sum(
            page_count(row.get("pages_screened", [])) for row in files
        ),
        "pages_fully_analyzed": sum(
            page_count(row.get("pages_fully_analyzed", [])) for row in files
        ),
        "pages_escalated": sum(
            page_count(row.get("pages_escalated", [])) for row in files
        ),
    }
    all_request_metrics = [
        request
        for row in performance_rows
        for request in row.get("request_metrics", [])
        if isinstance(request, dict)
    ]
    prescan_request_rows: list[dict[str, Any]] = []
    seen_prescan_metrics: set[str] = set()
    for row in files:
        value = row.get("prescan_performance", {})
        if not isinstance(value, dict) or not value:
            continue
        signature = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen_prescan_metrics:
            continue
        seen_prescan_metrics.add(signature)
        prescan_request_rows.append(value)
    performance["prescan_request_count"] = len(prescan_request_rows)

    def request_sum(field: str) -> int | float:
        return sum(
            float(item.get(field) or 0.0)
            for item in all_request_metrics
        )

    def prescan_sum(field: str) -> int | float:
        return sum(
            float(item.get(field) or 0.0)
            for item in prescan_request_rows
        )

    # Keep the full request-level accounting in the report.  Previously this
    # field was hard-coded to None, which made a completed run look cheap even
    # when prescan and visual verification consumed substantial tokens.
    performance["gemini_input_tokens"] = int(request_sum("input_tokens") + prescan_sum("input_tokens"))
    performance["gemini_cached_input_tokens"] = int(
        request_sum("cached_input_tokens") + prescan_sum("cached_input_tokens")
    )
    performance["gemini_output_tokens"] = int(request_sum("output_tokens") + prescan_sum("output_tokens"))
    performance["gemini_thought_tokens"] = int(request_sum("thought_tokens") + prescan_sum("thought_tokens"))
    performance["request_count"] = len(all_request_metrics) + len(prescan_request_rows)
    for field in (
        "request_bytes", "response_bytes", "retry_count", "image_count",
        "evidence_unit_count", "expected_record_count", "actual_record_count",
    ):
        performance[field] = int(request_sum(field) + sum(float(item.get(field) or 0.0) for item in prescan_request_rows))
    for field in (
        "upload_duration", "time_to_first_token", "total_time_to_first_token",
        "pre_generation_wait", "generation_duration", "queue_duration",
    ):
        performance[field] = round(request_sum(field) + sum(float(item.get(field) or 0.0) for item in prescan_request_rows), 4)
    performance["finish_reasons"] = sorted({
        str(item.get("finish_reason", "")) for item in [*all_request_metrics, *prescan_request_rows]
        if str(item.get("finish_reason", ""))
    })
    performance["models_used"] = sorted({
        str(item.get("model", "")) for item in [*all_request_metrics, *prescan_request_rows]
        if str(item.get("model", ""))
    } | {
        str(row.get("model", "")) for row in prescan_request_rows
        if str(row.get("model", ""))
    })
    performance["straggler_summary"] = summarize_request_metrics(
        all_request_metrics + prescan_request_rows
    )
    performance["total_compute_api_seconds"] = round(
        performance["hashing_seconds"]
        + performance["canonicalization_seconds"]
        + performance["local_evidence_build_seconds"]
        + performance["gemini_extraction_seconds"]
        + performance["gemini_verification_seconds"],
        4,
    )
    cache_attempts = (
        performance["gemini_calls"]
        + performance["extraction_cache_hits"]
        + performance["verification_cache_hits"]
    )
    performance["cache_hit_percentage"] = round(
        100.0 * (
            performance["extraction_cache_hits"]
            + performance["verification_cache_hits"]
        ) / cache_attempts,
        2,
    ) if cache_attempts else 0.0
    payload = {
        "schema_version": "1.1",
        "ingestion_pipeline_version": PIPELINE_VERSION,
        "run": dict(run_metadata or {}),
        "totals": {
            "discovered_files": len(files),
            "processed_files": processed,
            "cached_files": cached,
            "failed_files": failed,
        "pending_files": pending,
        "paused_quota_files": paused_quota,
            "reconciles": len(files) == processed + cached + failed,
            "inventory_reconciles": len(files) == processed + cached + failed + pending,
            "by_status": dict(sorted(by_status.items())),
            "unresolved_signal_count": sum(
                int(row.get("unresolved_signal_count") or 0) for row in files
            ),
        },
        "performance": performance,
        "files": files,
    }
    atomic_json(report_path, payload)
    return payload


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
    return read_docx_paragraphs(path)


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
    current_section = ""
    grouped: list[tuple[str, list[dict[str, Any]]]] = []
    current_group: list[dict[str, Any]] | None = None
    for paragraph in paragraphs[start_index:]:
        text = paragraph["text"]
        if (
            not paragraph["num_id"]
            and len(text) <= 80
            and text.upper() == text
        ):
            current_section = text.title()
            current_group = None
            continue
        if not paragraph["num_id"]:
            current_group = None
            continue
        if paragraph.get("list_level") in (None, 0) or current_group is None:
            current_group = [paragraph]
            grouped.append((current_section or discipline, current_group))
        else:
            current_group.append(paragraph)

    for sequence, (section, paragraph_group) in enumerate(grouped, start=1):
        paragraph = paragraph_group[0]
        text_parts = [paragraph["text"]]
        for child in paragraph_group[1:]:
            label = str(child.get("number_label") or "").strip()
            text_parts.append(f"{label} {child['text']}".strip())
        text = "\n".join(text_parts)
        number = str(sequence)
        end_row = int(paragraph_group[-1]["source_number"])
        location = (
            f"paragraph {paragraph['source_number']}"
            if end_row == int(paragraph["source_number"])
            else f"paragraphs {paragraph['source_number']}-{end_row}"
        )
        comment_id = base.stable_id(
            "C", record["path"], record["sha256"], paragraph["source_number"]
        )
        comment = make_comment(
            record, comment_id, number, text,
            section, reviewer,
            "Jillian Keller, Menlo Park City Arborist",
            location, "docx_numbered_paragraph", 0.94,
            row=paragraph["source_number"],
        )
        comment["source_row_end"] = end_row
        comment["source_locator_json"] = {
            "paragraph_index": int(paragraph["source_number"]),
            "paragraph_index_end": end_row,
            "paragraph_indices": [
                int(item["source_number"]) for item in paragraph_group
            ],
            "match_method": "docx_numbering_hierarchy",
            "exact_quote": text,
        }
        comment["hierarchy_status"] = (
            "merged_parent" if len(paragraph_group) > 1 else "single_item"
        )
        comment["hierarchy_components"] = [{
            "label": item.get("number_label", ""),
            "list_level": item.get("list_level"),
            "source_row": int(item["source_number"]),
            "original_text": item["text"],
        } for item in paragraph_group]
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
        records = expand_menlo_source_group(summary, records, inventory)
        groups.append((summary, records))
    return groups


def all_source_groups(
    inventory: dict[str, dict[str, Any]],
    ingestion_files: list[dict[str, Any]] | None = None,
) -> list[tuple[dict[str, str], list[dict[str, Any]]]]:
    """Group every supported file; audit summaries are metadata, not gates."""
    records = [copy.deepcopy(row) for row in inventory.values()]
    ingestion_by_path = {
        str(row.get("relative_path", "")): row
        for row in ingestion_files or []
        if row.get("relative_path")
    }
    for record in records:
        discovered = ingestion_by_path.get(str(record.get("path", "")), {})
        if not discovered:
            continue
        if _known_city(discovered.get("city")):
            record["likely_city"] = discovered["city"]
            record["city_confidence"] = discovered.get("city_confidence", 0.0)
            record["city_evidence"] = discovered.get("city_evidence", [])
            record["city_resolution_method"] = discovered.get(
                "city_resolution_method", "",
            )
        if not record.get("likely_property_project") and discovered.get("project"):
            record["likely_property_project"] = discovered["project"]
        if not record.get("likely_review_round") and discovered.get("submission_round"):
            record["likely_review_round"] = discovered["submission_round"]
    known = {str(row.get("path", "")) for row in records}
    for item in ingestion_files or []:
        relative = str(item.get("relative_path", ""))
        if not relative or relative in known:
            continue
        records.append({
            "path": relative, "filename": item.get("filename", ""),
            "extension": "." + str(item.get("file_type", "")).lstrip("."),
            "likely_city": item.get("city", "Unknown"),
            "likely_property_project": item.get("project", ""),
            "likely_review_round": item.get("submission_round", ""),
            "document_type": item.get("document_type", "unknown"),
            "likely_contains_city_comments": item.get(
                "likely_contains_city_comments", False,
            ),
            "likely_contains_company_responses": item.get(
                "likely_contains_company_responses", False,
            ),
            "likely_contains_both": item.get("likely_contains_both", False),
            "appears_drawing_heavy": item.get("appears_drawing_heavy", False),
            "classification_evidence": item.get(
                "classification_evidence",
                "Discovered directly from project folder",
            ),
            "page_count": item.get("page_count", ""),
            "sheet_count": item.get("sheet_count", ""),
            "sheet_names": item.get("sheet_names", []),
            "sha256": item.get("sha256", ""),
        })
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        if str(record.get("extension", "")).casefold() not in SUPPORTED_TYPES:
            continue
        key = (
            str(record.get("likely_city", "Unknown")),
            str(record.get("likely_property_project", "")),
            str(record.get("likely_review_round", "")),
        )
        grouped.setdefault(key, []).append(record)
    return [(
        {
            "likely_city": city,
            "likely_property_project": project,
            "likely_review_round": review_round,
        },
        sorted(rows, key=lambda row: str(row.get("path", ""))),
    ) for (city, project, review_round), rows in sorted(grouped.items())]


def coalesce_prescan_groups(
    groups: list[tuple[dict[str, str], list[dict[str, Any]]]],
    max_files: int = 20,
) -> list[tuple[dict[str, str], list[dict[str, Any]]]]:
    """Use one bounded Gemini routing request per physical site folder."""
    sites: dict[str, list[dict[str, Any]]] = {}
    for _summary, records in groups:
        for record in records:
            sites.setdefault(
                _site_folder(str(record.get("path", ""))),
                [],
            ).append(record)
    result: list[tuple[dict[str, str], list[dict[str, Any]]]] = []
    for site, records in sorted(sites.items()):
        unique = {
            str(record.get("path", "")): record for record in records
        }
        ordered = [unique[path] for path in sorted(unique)]
        for start in range(0, len(ordered), max_files):
            chunk = ordered[start:start + max_files]
            cities = {
                str(record.get("likely_city", ""))
                for record in chunk if _known_city(record.get("likely_city"))
            }
            rounds = {
                str(record.get("likely_review_round", ""))
                for record in chunk
                if str(record.get("likely_review_round", "")).casefold()
                not in {"", "unknown"}
            }
            result.append(({
                "likely_city": next(iter(cities)) if len(cities) == 1 else "Unknown",
                "likely_property_project": site,
                "likely_review_round": next(iter(rounds)) if len(rounds) == 1 else "",
            }, chunk))
    return result


def _same_audit_round(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("likely_city", "")) == str(right.get("likely_city", ""))
        and str(left.get("likely_property_project", "")) == str(right.get("likely_property_project", ""))
        and str(left.get("likely_review_round", "")) == str(right.get("likely_review_round", ""))
    )


def _is_archive_path(path: str) -> bool:
    return bool(re.search(r"(?:^|/)archive(?:/|$)", path, re.I))


def _looks_like_menlo_correction_source(record: dict[str, Any]) -> bool:
    filename = str(record.get("filename", ""))
    path = str(record.get("path", ""))
    if str(record.get("likely_city", "")) != "Menlo Park":
        return False
    if str(record.get("extension", "")).casefold() not in {".pdf", ".docx"}:
        return False
    if str(record.get("likely_contains_city_comments", "")) != "True" and record.get("likely_contains_city_comments") is not True:
        return False
    if str(record.get("document_type", "")) not in {"city_comments", "correction_notice", "review_letter"}:
        return False
    return bool(
        re.search(r"reviewed[-\s]*corrections[-\s]*required|round of comments|correction", filename, re.I)
        or re.search(r"/\d+(?:st|nd|rd|th)\s+round\s+of\s+comments/", path, re.I)
        or filename.casefold().endswith(".docx")
    )


def _looks_like_menlo_response_or_support(record: dict[str, Any]) -> bool:
    if str(record.get("likely_city", "")) != "Menlo Park":
        return False
    filename = str(record.get("filename", ""))
    document_type = str(record.get("document_type", ""))
    if document_type == "company_response":
        return True
    return bool(re.search(
        r"\b(?:foundation review letter|geotech|soil report|arborist report|"
        r"structure|structural calculation|special inspection|checklist|plan(?:s| set)?)\b",
        filename,
        re.I,
    ))


def _prefer_non_archive(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_name.setdefault(str(record.get("filename", "")).casefold(), []).append(record)
    selected: list[dict[str, Any]] = []
    for candidates in by_name.values():
        non_archive = [item for item in candidates if not _is_archive_path(str(item.get("path", "")))]
        selected.extend(non_archive or candidates)
    return selected


def expand_menlo_source_group(
    summary: dict[str, str],
    selected: list[dict[str, Any]],
    inventory: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Menlo Park rounds often spread comments across indirectly named PDFs.

    The audit summary intentionally picks one primary source per round. That is
    too narrow for Menlo Park packages such as
    ``Structural Calculation-Reviewed-Corrections-Required.pdf``. For these
    rounds, include every audited city-comment correction source plus response
    and support files from the same round so Gemini and the source registry can
    see the complete package.
    """
    if str(summary.get("likely_city", "")) != "Menlo Park":
        return selected
    if not selected:
        return selected
    anchor = selected[0]
    candidates = [record for record in inventory.values() if _same_audit_round(anchor, record)]
    comment_candidates = [
        record for record in candidates if _looks_like_menlo_correction_source(record)
    ]
    non_archive_comments = [
        record for record in comment_candidates if not _is_archive_path(str(record.get("path", "")))
    ]
    comment_records = _prefer_non_archive(non_archive_comments or comment_candidates)
    context_records = _prefer_non_archive([
        record for record in candidates
        if _looks_like_menlo_response_or_support(record)
        and not _is_archive_path(str(record.get("path", "")))
    ])
    ordered: dict[str, dict[str, Any]] = {}
    for record in [*comment_records, *selected, *context_records]:
        ordered[str(record["path"])] = record
    return list(ordered.values())


def _annotate_legacy_source_provenance(
    comments: list[dict[str, Any]], responses: list[dict[str, Any]],
    links: list[dict[str, Any]], source: Path, source_relative: str,
    summary: dict[str, Any],
) -> None:
    """Attach the same auditable date/round contract to local-parser output."""
    try:
        iso, evidence, method = derive_document_date(source, [*comments, *responses])
    except (OSError, ValueError, TypeError, zipfile.BadZipFile, ET.ParseError):
        iso, evidence, method = "", "", "missing"
    explicit_round = next((
        str(row.get("review_round") or row.get("document_round") or row.get("source_cycle") or "").strip()
        for row in [*comments, *responses]
        if str(row.get("review_round") or row.get("document_round") or row.get("source_cycle") or "").strip()
    ), "")
    round_value = explicit_round or str(summary.get("likely_review_round", "") or "").strip()
    round_meta = {
        "value": round_value,
        "raw": round_value,
        "source": "record_content" if explicit_round else (
            "audit_or_filename_fallback" if round_value else "unknown"
        ),
        "confidence": 0.8 if explicit_round else (0.45 if round_value else 0.0),
    }
    for row in [*comments, *responses]:
        row.setdefault("source_document", source_relative)
        if iso:
            row.setdefault("source_document_date", iso)
            row.setdefault("document_date_iso", iso)
            row.setdefault("document_date", {
                "raw": evidence, "iso": iso, "source": method,
                "page": 0, "evidence": evidence,
                "confidence": 0.8 if method != "missing" else 0.0,
            })
            row.setdefault("document_date_raw", evidence)
            row.setdefault("document_date_source", method)
            row.setdefault("source_date_evidence", evidence)
            row.setdefault("source_date_method", method)
        row.setdefault("review_round_metadata", copy.deepcopy(round_meta))
    for row in links:
        row.setdefault("source_document", source_relative)
        if iso:
            row.setdefault("source_document_date", iso)
            row.setdefault("document_date", {
                "raw": evidence, "iso": iso, "source": method,
                "page": 0, "evidence": evidence,
                "confidence": 0.8 if method != "missing" else 0.0,
            })
        row.setdefault("review_round_metadata", copy.deepcopy(round_meta))
def process_new_group(
    workspace: Path,
    summary: dict[str, str],
    records: list[dict[str, Any]],
    pipeline: VisualIngestionPipeline,
    file_workers: int = 1,
    prescan_decisions: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    comments: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    source_summaries: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    canonical_started = time.perf_counter()
    canonical_records, source_aliases = canonicalize_records_before_gemini(
        workspace, records, pipeline,
    )
    canonicalization_seconds = time.perf_counter() - canonical_started
    print(
        f"Prescanning {len(canonical_records)} canonical source files "
        f"({len(source_aliases)} aliases reused) for "
        f"{summary.get('likely_city', '')} round {summary.get('likely_review_round', '')}",
        file=sys.stderr,
        flush=True,
    )
    prescan = prescan_source_group(
        workspace, summary, canonical_records, pipeline, use_gemini=False,
    )
    decisions = prescan_decision_map(prescan)
    supplied_decisions = prescan_decisions or {}
    for alias in source_aliases:
        record = alias["record"]
        decision = {
            "decision": "metadata_only",
            "document_role": "source_file_alias",
            "confidence": 1.0,
            "reason": (
                f"Reused canonical source {alias['canonical_path']} "
                f"({alias['duplicate_reason']})"
            ),
            "linked_topics": [],
        }
        source_summaries.append({
            **prescan_summary_row(summary, record, decision),
            "source_type": "source_file_alias",
            "processing_status": "classified",
            "opened": False,
            "processing_error": "",
            "verification_result": "reused_canonical_document",
            "pages_screened": [],
            "pages_fully_analyzed": [],
            "additional_markup_detected": False,
            "cache_reused_from": alias["canonical_path"],
            "canonical_source_document": alias["canonical_path"],
            "canonical_source_sha256": str(
                alias["canonical_record"].get("sha256", "")
            ),
            "source_file_alias_reason": alias["duplicate_reason"],
            "normalized_content_fingerprint": alias["content_fingerprint"],
            "ingestion_pipeline_version": PIPELINE_VERSION,
        })
    work_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for record in canonical_records:
        decision = sanitize_prescan_decision(
            record,
            supplied_decisions.get(
                record["path"],
                decisions.get(record["path"], fallback_prescan_decision(record)),
            ),
        )
        source = workspace / record["path"]
        if source.suffix.casefold() not in SUPPORTED_TYPES:
            continue
        if source.is_file() and source.stat().st_size == 0:
            source_summaries.append({
                **prescan_summary_row(summary, record, {
                    "decision": "skip",
                    "document_role": "empty_file",
                    "confidence": 1.0,
                    "reason": "The source file is empty (0 bytes).",
                    "linked_topics": [],
                }),
                "source_type": "empty_source_file",
                "processing_status": "no_relevant_content",
                "opened": False,
                "processing_error": "",
                "verification_result": "not_run_empty_file",
                "pages_screened": [],
                "pages_fully_analyzed": [],
                "additional_markup_detected": False,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
            continue
        if decision.get("decision") in {"context_only", "skip"}:
            source_summaries.append({
                **prescan_summary_row(summary, record, decision),
                "source_type": "prescan_context_source",
                "processing_status": (
                    "no_relevant_content"
                    if decision.get("decision") == "skip" else "classified"
                ),
                "opened": False,
                "processing_error": "",
                "verification_result": "not_run_context_only",
                "pages_screened": [],
                "pages_fully_analyzed": [],
                "additional_markup_detected": False,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
            continue
        if (
            _is_archive_path(str(record.get("path", "")))
            and str(record.get("document_type", "")) == "company_response"
            and any(
                other is not record
                and not _is_archive_path(str(other.get("path", "")))
                and str(other.get("document_type", "")) == "company_response"
                for other in canonical_records
            )
        ):
            canonical_response = next(
                other for other in canonical_records
                if other is not record
                and not _is_archive_path(str(other.get("path", "")))
                and str(other.get("document_type", "")) == "company_response"
            )
            source_summaries.append({
                **prescan_summary_row(summary, record, {
                    "decision": "context_only",
                    "document_role": "superseded_archive_response",
                    "confidence": 0.95,
                    "reason": (
                        "A current non-archive response source is available; "
                        "retain this archive as source context without another "
                        "Gemini extraction."
                    ),
                    "linked_topics": [],
                }),
                "source_type": "archive_context_source",
                "processing_status": "classified",
                "opened": False,
                "processing_error": "",
                "verification_result": "not_run_context_only",
                "pages_screened": [],
                "pages_fully_analyzed": [],
                "additional_markup_detected": False,
                "cache_reused_from": canonical_response["path"],
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
            continue
        work_items.append((record, decision))

    def process_record(
        record: dict[str, Any],
        decision: dict[str, Any],
        worker_pipeline: VisualIngestionPipeline,
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
        list[dict[str, Any]], list[dict[str, Any]],
    ]:
        source = workspace / record["path"]
        print(
            f"Open and content-screen ({decision['decision']} priority): {record['path']}",
            file=sys.stderr, flush=True,
        )
        try:
            result = worker_pipeline.process(source, record["path"], {
                "property_hint": summary.get("likely_property_project", record.get("likely_property_project", "")),
                "city_hint": summary.get("likely_city", record.get("likely_city", "")),
                "review_round_hint": summary.get("likely_review_round", record.get("likely_review_round", "")),
                "audit_document_type_hint": record.get("document_type", ""),
                "prescan_decision": decision,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            })
        except GeminiCircuitOpenError as exc:
            message = str(exc)
            paused_summary = {
                **prescan_summary_row(summary, record, decision),
                "source_type": "paused_quota_circuit",
                "processing_status": "paused_quota",
                "opened": False,
                "processing_error": message,
                "verification_result": "paused_quota",
                "pages_screened": [], "pages_fully_analyzed": [],
                "additional_markup_detected": False,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            }
            paused_review = {
                "item_type": "ingestion_file",
                "item_id": base.stable_id("F", record["path"]),
                "reason": message,
                "source_document": record["path"],
                "source_location": "complete document",
                "suggested_action": "Replenish Gemini credits, then resume this checkpoint",
                "decision": "",
                "decision_note": "Paused by run-level Gemini credit circuit breaker",
            }
            return [], [], [], [paused_summary], [paused_review]
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            message = str(exc)
            failed_summary = {
                **prescan_summary_row(summary, record, decision),
                "source_type": "failed_content_screening",
                "processing_status": "failed", "opened": True,
                "processing_error": message, "verification_result": "failed",
                "pages_screened": [], "pages_fully_analyzed": [],
                "additional_markup_detected": False,
                "ingestion_pipeline_version": PIPELINE_VERSION,
            }
            failed_review = {
                "item_type": "ingestion_file", "item_id": base.stable_id("F", record["path"]),
                "reason": message, "source_document": record["path"],
                "source_location": "complete document",
                "suggested_action": "Resolve the file-level failure and selectively retry this hash",
                "decision": "", "decision_note": "",
            }
            return [], [], [], [failed_summary], [failed_review]
        c, r, l, s, q = result
        _annotate_legacy_source_provenance(
            c, r, l, source, record["path"], summary,
        )
        return c, r, l, [s], q

    indexed_results: dict[int, tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
        list[dict[str, Any]], list[dict[str, Any]],
    ]] = {}
    fork_pipeline = getattr(pipeline, "fork", None)
    parallel = (
        max(1, file_workers) > 1
        and len(work_items) > 1
        and callable(fork_pipeline)
    )
    if parallel:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max(1, file_workers), len(work_items)),
            thread_name_prefix="gemini-file",
        ) as executor:
            futures = {
                executor.submit(
                    process_record, record, decision, fork_pipeline(),
                ): index
                for index, (record, decision) in enumerate(work_items)
            }
            for future in concurrent.futures.as_completed(futures):
                indexed_results[futures[future]] = future.result()
    else:
        for index, (record, decision) in enumerate(work_items):
            indexed_results[index] = process_record(
                record, decision, pipeline,
            )
    for index in sorted(indexed_results):
        c, r, l, s, q = indexed_results[index]
        comments.extend(c)
        responses.extend(r)
        links.extend(l)
        source_summaries.extend(s)
        review.extend(q)
    grouped = apply_grouped_response_notes(
        summary, canonical_records, comments, responses, links,
    )
    review.extend(grouped)
    if source_summaries:
        performance = source_summaries[0].setdefault("performance", {})
        if isinstance(performance, dict):
            performance["canonicalization_seconds"] = round(
                canonicalization_seconds, 4,
            )
        # Keep prescan timing/token accounting separate from the per-file
        # extraction metrics.  The report aggregates this field once, so a
        # multi-file prescan cannot be mistaken for repeated Gemini calls.
        source_summaries[0]["prescan_performance"] = copy.deepcopy(
            prescan.get("performance", {})
        )
    return comments, responses, links, source_summaries, review


def canonicalize_records_before_gemini(
    workspace: Path,
    records: list[dict[str, Any]],
    pipeline: VisualIngestionPipeline,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse exact and normalized-content copies before Gemini calls."""
    by_sha: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        digest = str(record.get("sha256", "")).strip()
        key = digest or f"path:{record.get('path', '')}"
        by_sha.setdefault(key, []).append(record)

    def rank(record: dict[str, Any]) -> tuple[bool, int, str]:
        path = str(record.get("path", ""))
        return (_is_archive_path(path), len(path), path.casefold())

    binary_representatives: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    fingerprints: dict[str, str] = {}
    for digest, group in sorted(by_sha.items()):
        canonical = min(group, key=rank)
        binary_representatives.append(canonical)
        for duplicate in group:
            if duplicate is canonical:
                continue
            aliases.append({
                "record": duplicate,
                "canonical_record": canonical,
                "canonical_path": canonical["path"],
                "duplicate_reason": "identical_binary_sha256",
                "content_fingerprint": f"binary:{digest}",
            })

    by_content: dict[str, list[dict[str, Any]]] = {}
    for record in binary_representatives:
        path = workspace / str(record["path"])
        digest = str(record.get("sha256", "")).strip()
        try:
            builder = getattr(pipeline, "builder", None)
            if builder is None or not hasattr(builder, "content_fingerprint"):
                raise AttributeError("Pipeline does not expose a fingerprint builder")
            _actual_digest, fingerprint = builder.content_fingerprint(path)
        except (
            AttributeError, OSError, RuntimeError, ValueError,
            zipfile.BadZipFile, ET.ParseError,
        ):
            fingerprint = f"binary:{digest or record['path']}"
        fingerprints[str(record["path"])] = fingerprint
        by_content.setdefault(fingerprint, []).append(record)

    canonical_records: list[dict[str, Any]] = []
    replacement: dict[str, dict[str, Any]] = {}
    for fingerprint, group in sorted(by_content.items()):
        canonical = min(group, key=rank)
        canonical_records.append(canonical)
        for duplicate in group:
            if duplicate is canonical:
                continue
            replacement[str(duplicate["path"])] = canonical
            aliases.append({
                "record": duplicate,
                "canonical_record": canonical,
                "canonical_path": canonical["path"],
                "duplicate_reason": "identical_normalized_content",
                "content_fingerprint": fingerprint,
            })

    # Exact-byte aliases may point to a representative that itself became a
    # normalized-content alias. Resolve that chain to the final canonical file.
    for alias in aliases:
        target = alias["canonical_record"]
        final = replacement.get(str(target["path"]), target)
        alias["canonical_record"] = final
        alias["canonical_path"] = final["path"]
        alias["content_fingerprint"] = fingerprints.get(
            str(final["path"]), alias["content_fingerprint"],
        )
    canonical_paths = {str(row["path"]) for row in canonical_records}
    canonical_records = [
        row for row in records if str(row["path"]) in canonical_paths
    ]
    aliases.sort(key=lambda row: str(row["record"].get("path", "")).casefold())
    return canonical_records, aliases


def merge_pre_gemini_source_aliases(
    identity: dict[str, Any],
    source_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist aliases that intentionally produced no duplicate comment rows."""
    alias_rows = [
        row for row in source_rows
        if str(row.get("source_type", "")) == "source_file_alias"
        and str(row.get("canonical_source_document", "")).strip()
    ]
    if not alias_rows:
        return identity
    source_files = identity.setdefault("source_files", {})
    canonical_documents = identity.setdefault("canonical_documents", {})
    aliases = identity.setdefault("source_file_aliases", [])
    existing_alias_ids = {
        str(row.get("source_file_id", "")) for row in aliases
        if isinstance(row, dict)
    }
    source_to_canonical: dict[str, str] = {}
    for canonical_id, document in canonical_documents.items():
        first = str(document.get("first_seen_source_file_id", ""))
        if first:
            source_to_canonical[first] = canonical_id
    for row in aliases:
        if isinstance(row, dict):
            source_to_canonical[str(row.get("source_file_id", ""))] = str(
                row.get("canonical_document_id", "")
            )

    for row in alias_rows:
        alias_path = str(row.get("source_document", "")).split(" | ", 1)[0].strip()
        canonical_path = str(row.get("canonical_source_document", "")).strip()
        if not alias_path or not canonical_path:
            continue
        alias_id = source_file_id(alias_path)
        canonical_file_id = source_file_id(canonical_path)
        canonical_id = source_to_canonical.get(canonical_file_id)
        if not canonical_id:
            fingerprint = str(row.get("normalized_content_fingerprint", ""))
            canonical_id = "CD-" + hashlib.sha256(
                f"pre-gemini|{fingerprint}|{canonical_path}".encode("utf-8")
            ).hexdigest()[:20]
            canonical_documents.setdefault(canonical_id, {
                "canonical_document_id": canonical_id,
                "document_type": Path(canonical_path).suffix.casefold().lstrip("."),
                "normalized_content_hash": fingerprint,
                "page_fingerprints": [],
                "substantive_text_fingerprint": fingerprint,
                "canonical_project": row.get("property_project", ""),
                "canonical_round": row.get("review_round", ""),
                "first_seen_source_file_id": canonical_file_id,
                "duplicate_group_size": 1,
                "duplicate_review_status": "confirmed",
            })
            source_to_canonical[canonical_file_id] = canonical_id
        for path, file_id in (
            (canonical_path, canonical_file_id), (alias_path, alias_id),
        ):
            binary_sha = (
                row.get("canonical_source_sha256", "")
                if path == canonical_path
                else row.get("source_sha256", "")
            )
            source_files.setdefault(file_id, {
                "source_file_id": file_id,
                "filename": Path(path).name,
                "folder_path": Path(path).parent.as_posix(),
                "declared_project": row.get("property_project", ""),
                "declared_round": row.get("review_round", ""),
                "binary_sha256": binary_sha,
                "ingestion_timestamp": "",
            })
        if alias_id not in existing_alias_ids:
            aliases.append({
                "source_file_id": alias_id,
                "canonical_document_id": canonical_id,
                "duplicate_reason": str(
                    row.get("source_file_alias_reason", "canonical_content_duplicate")
                ),
                "similarity_score": 1.0,
            })
            existing_alias_ids.add(alias_id)
            canonical_documents[canonical_id]["duplicate_group_size"] = int(
                canonical_documents[canonical_id].get("duplicate_group_size") or 1
            ) + 1
    identity["physical_source_file_count"] = len(source_files)
    identity["canonical_document_count"] = len(canonical_documents)
    return identity


def prescan_text_snippet(path: Path, limit: int = 2200) -> str:
    try:
        if path.suffix.casefold() == ".docx":
            return "\n".join(item["text"] for item in docx_paragraphs(path)[:25])[:limit]
        if path.suffix.casefold() == ".pdf":
            pages = gs_text_pages(path)
            sample_pages = pages[:2] + (pages[-1:] if len(pages) > 2 else [])
            return "\n--- page break ---\n".join(sample_pages)[:limit]
        if path.suffix.casefold() == ".xlsx":
            detail = audit.inspect_xlsx(path)
            return str(
                detail.get("content_sample") or detail.get("sample_signals", "")
            )[:limit]
        if path.suffix.casefold() in {".csv", ".tsv"}:
            detail = audit.inspect_delimited(path)
            return str(
                detail.get("content_sample") or detail.get("sample_signals", "")
            )[:limit]
        if path.suffix.casefold() in {".txt", ".md", ".rtf"}:
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, RuntimeError, zipfile.BadZipFile, ET.ParseError):
        return ""
    return ""


def prescan_file_payload(workspace: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = workspace / record["path"]
    comment_signal = (
        record.get("likely_contains_city_comments") is True
        or str(record.get("likely_contains_city_comments", "")) == "True"
        or record.get("likely_contains_company_responses") is True
        or str(record.get("likely_contains_company_responses", "")) == "True"
        or str(record.get("document_type", "")) in {
            "city_comments", "company_response", "combined_comment_response",
            "correction_notice", "review_letter",
        }
    )
    try:
        page_count = int(record.get("page_count") or 0)
    except (TypeError, ValueError):
        page_count = 0
    include_snippet = comment_signal or page_count <= 5
    return {
        "relative_path": record["path"],
        "filename": record.get("filename", ""),
        "extension": record.get("extension", ""),
        "document_type": record.get("document_type", ""),
        "likely_contains_city_comments": record.get("likely_contains_city_comments", False),
        "likely_contains_company_responses": record.get("likely_contains_company_responses", False),
        "likely_contains_both": record.get("likely_contains_both", False),
        "appears_drawing_heavy": record.get("appears_drawing_heavy", False),
        "page_count": record.get("page_count", ""),
        "classification_evidence": record.get("classification_evidence", ""),
        "snippet": prescan_text_snippet(path) if include_snippet else "",
    }


def fallback_prescan_decision(record: dict[str, Any]) -> dict[str, Any]:
    path = str(record.get("path", ""))
    if _is_archive_path(path):
        return {"decision": "context_only", "document_role": "archive_or_old_copy", "confidence": 0.9, "reason": "Archive path; still content-screened and hash-deduplicated", "linked_topics": []}
    if (
        record.get("likely_contains_city_comments") is True
        or str(record.get("likely_contains_city_comments", "")) == "True"
        or record.get("likely_contains_company_responses") is True
        or str(record.get("likely_contains_company_responses", "")) == "True"
        or str(record.get("document_type", "")) in {
            "city_comments", "company_response", "combined_comment_response",
            "correction_notice", "review_letter",
        }
    ):
        return {"decision": "full_read", "document_role": "comment_or_response_source", "confidence": 0.8, "reason": "Audit classified file as a comment/response source", "linked_topics": []}
    if _looks_like_menlo_response_or_support(record):
        return {"decision": "context_only", "document_role": "supporting_source", "confidence": 0.8, "reason": "Supporting file should remain available as secondary source evidence", "linked_topics": []}
    return {"decision": "context_only", "document_role": "unknown_content", "confidence": 0.55, "reason": "Filename/audit metadata are only priority hints; content screening decides", "linked_topics": []}


def sanitize_prescan_decision(record: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    allowed = {"full_read", "context_only", "skip"}
    value = str(decision.get("decision", "")).strip()
    if value not in allowed:
        return fallback_prescan_decision(record)
    if _looks_like_menlo_correction_source(record) and value != "full_read":
        fallback = fallback_prescan_decision(record)
        fallback["reason"] = f"Prescan requested {value}, but Menlo correction sources must be fully read"
        return fallback
    if str(record.get("document_type", "")) == "company_response" and value == "context_only":
        fallback = fallback_prescan_decision(record)
        fallback["reason"] = "Company response sources must be fully read"
        return fallback
    non_comment_roles = {
        "permit_application", "application", "application_form",
        "authorization", "approval_document", "permit_document",
        "permit_record", "permit_summary", "revision_documentation",
        "revision_summary",
    }
    role = str(decision.get("document_role", "")).strip().casefold()
    local_comment_signal = any(
        record.get(field) is True
        or str(record.get(field, "")) == "True"
        for field in (
            "likely_contains_city_comments",
            "likely_contains_company_responses",
            "likely_contains_both",
        )
    )
    if value == "full_read" and role in non_comment_roles and not local_comment_signal:
        return {
            "decision": "context_only",
            "document_role": role,
            "reason": (
                "Gemini identified an administrative/application document, "
                "and local content signals found no government comments or "
                "company responses"
            ),
            "confidence": max(float(decision.get("confidence") or 0.0), 0.9),
            "linked_topics": decision.get("linked_topics", [])
            if isinstance(decision.get("linked_topics"), list) else [],
        }
    return {
        "decision": value,
        "document_role": str(decision.get("document_role", "")) or fallback_prescan_decision(record)["document_role"],
        "reason": str(decision.get("reason", "")),
        "confidence": float(decision.get("confidence") or 0.0),
        "linked_topics": decision.get("linked_topics", []) if isinstance(decision.get("linked_topics"), list) else [],
    }


def prescan_decision_map(prescan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("relative_path", "")): row
        for row in prescan.get("files", [])
        if isinstance(row, dict)
    }


def prescan_source_group(
    workspace: Path,
    summary: dict[str, str],
    records: list[dict[str, Any]],
    pipeline: VisualIngestionPipeline,
    use_gemini: bool = True,
) -> dict[str, Any]:
    payload = [prescan_file_payload(workspace, record) for record in records]
    context = {
        "city_hint": summary.get("likely_city", ""),
        "property_hint": summary.get("likely_property_project", ""),
        "review_round_hint": summary.get("likely_review_round", ""),
        "prescan_prompt_version": PRESCAN_PROMPT_VERSION,
    }
    shared_client = (
        getattr(pipeline, "prescan_client", None)
        or getattr(pipeline, "client", None)
    )
    fork_client = getattr(shared_client, "fork", None)
    # Prescan groups run concurrently.  Each request needs an isolated client
    # so usage/timing metadata cannot be overwritten by a neighboring site.
    client = fork_client() if callable(fork_client) else shared_client
    raw: dict[str, Any] = {}
    prescan_error = ""
    started = time.perf_counter()
    if use_gemini and client and hasattr(client, "pre_scan_sources"):
        try:
            raw = client.pre_scan_sources(payload, context)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            prescan_error = f"{type(exc).__name__}: {exc}"
            raw = {}
    if not raw.get("files"):
        raw = {"files": [
            {"relative_path": record["path"], **fallback_prescan_decision(record)}
            for record in records
        ]}
    usage = getattr(client, "last_usage_metadata", {}) if client else {}
    request = getattr(client, "last_request_metadata", {}) if client else {}
    usage = usage if isinstance(usage, dict) else {}
    request = request if isinstance(request, dict) else {}
    by_path = prescan_decision_map(raw)
    by_filename: dict[str, list[dict[str, Any]]] = {}
    for returned_path, decision in by_path.items():
        by_filename.setdefault(Path(returned_path).name.casefold(), []).append(
            decision,
        )
    files = []
    for record in records:
        decision = by_path.get(record["path"], {})
        if not decision:
            filename_matches = by_filename.get(
                str(record.get("filename", "")).casefold(), [],
            )
            if len(filename_matches) == 1:
                decision = filename_matches[0]
        files.append({
            "relative_path": record["path"],
            **sanitize_prescan_decision(record, decision),
        })
    return {
        "prompt_version": PRESCAN_PROMPT_VERSION,
        "files": files,
        "prescan_error": prescan_error,
        "routing_method": "gemini" if use_gemini else "local_deterministic",
        "returned_file_count": len(by_path),
        "requested_file_count": len(records),
        "performance": {
            **request,
            "stage": "gemini_prescan",
            "elapsed_seconds": round(time.perf_counter() - started, 4),
            "input_tokens": int(usage.get("promptTokenCount") or request.get("input_tokens") or 0),
            "cached_input_tokens": int(usage.get("cachedContentTokenCount") or request.get("cached_input_tokens") or 0),
            "output_tokens": int(usage.get("candidatesTokenCount") or request.get("output_tokens") or 0),
            "thought_tokens": int(usage.get("thoughtsTokenCount") or 0),
        },
    }


def prescan_summary_row(summary: dict[str, str], record: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "city": record.get("likely_city", summary.get("likely_city", "")),
        "property_project": record.get("likely_property_project", summary.get("likely_property_project", "")),
        "review_round": record.get("likely_review_round", summary.get("likely_review_round", "")),
        "source_document": record["path"],
        "source_type": f"prescan_{decision['decision']}",
        "comment_count": 0,
        "response_count": 0,
        "matched_count": 0,
        "unmatched_count": 0,
        "extraction_method": "gemini_source_prescan",
        "processing_error": decision.get("reason", ""),
        "prescan_decision": decision,
    }


BROAD_RESPONSE_PATTERN = re.compile(
    r"\b(?:note:\s*)?(?:please\s+)?see\s+(?:the\s+)?(?:updated|new|revised|uploaded|included)"
    r"|included\s+in\s+the\s+plan\s+set|new\s+geotechnical\s+report|foundation\s+review\s+letter"
    r"|uploaded\b",
    re.IGNORECASE,
)


def _broad_response_topics(text: str) -> set[str]:
    topics: set[str] = set()
    lowered = text.casefold()
    topic_terms = {
        "foundation": ("foundation", "mat slab", "footing"),
        "geotechnical": ("geotech", "geotechnical", "soil report"),
        "structural": ("structural", "calculation", "framing", "shear", "ledger", "anchor", "beam"),
        "arborist": ("arborist", "tree", "landscape"),
        "special_inspection": ("special inspection", "inspection form"),
        "plan_set": ("plan set", "plans", "sheet", "updated plan"),
    }
    for topic, terms in topic_terms.items():
        if any(term in lowered for term in terms):
            topics.add(topic)
    return topics


def _comment_topics(comment: dict[str, Any]) -> set[str]:
    return _broad_response_topics(
        " ".join([
            str(comment.get("original_text", "")),
            str(comment.get("discipline", "")),
            str(comment.get("source_document", "")),
        ])
    )


def _is_broad_response_note(text: str) -> bool:
    """Return true only for an explicit multi-comment/package response note.

    Ordinary row responses such as "Noted. Please see updated sheet A2.00"
    must remain attached to their own visible row.  A true grouped response is
    either explicitly labeled ``Note:`` or references at least two substantive
    supporting-document topics (for example foundation + geotechnical).
    """
    if not BROAD_RESPONSE_PATTERN.search(text):
        return False
    if re.search(r"^\s*note\s*:", text, re.IGNORECASE):
        return True
    substantive_topics = _broad_response_topics(text) - {"plan_set"}
    return len(substantive_topics) >= 2


def apply_grouped_response_notes(
    summary: dict[str, str],
    records: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if str(summary.get("likely_city", "")) != "Menlo Park":
        return []
    links_by_comment = {str(row.get("comment_id", "")): row for row in links}
    review: list[dict[str, Any]] = []
    broad_responses = [
        response for response in responses
        if _is_broad_response_note(str(response.get("original_text", "")))
    ]
    for response in broad_responses:
        topics = _broad_response_topics(str(response.get("original_text", "")))
        if not topics:
            continue
        response_id = str(response.get("response_id", ""))
        matched_ids = set(response.get("comment_ids", []) if isinstance(response.get("comment_ids"), list) else [])
        primary_comment = str(response.get("comment_id", ""))
        if primary_comment:
            matched_ids.add(primary_comment)
        for comment in comments:
            comment_id = str(comment.get("comment_id", ""))
            link = links_by_comment.get(comment_id)
            if not link or link.get("response_id") or comment_id in matched_ids:
                continue
            comment_topics = _comment_topics(comment)
            substantive_topics = topics - {"plan_set"}
            if not (
                comment_topics & substantive_topics
                or ("plan_set" in topics and not substantive_topics and "plan_set" in comment_topics)
            ):
                continue
            link.update({
                "link_id": base.stable_id("L", comment_id, response_id, "gemini_grouped_broad_response_note"),
                "response_id": response_id,
                "match_status": "matched",
                "matching_method": "gemini_grouped_broad_response_note",
                "match_confidence": 0.72,
                "review_status": "needs_review",
                "verification_status": "needs_review",
                "provenance": "gemini_grouped_broad_response_note",
                "match_basis": "Broad response note references updated supporting package for the same Menlo Park review round",
                "response_locator_json": response.get("source_locator_json", {}),
            })
            comment.update({
                "response_id": response_id,
                "match_status": "matched",
                "human_review_status": "needs_review",
            })
            matched_ids.add(comment_id)
            review.append({
                "item_type": "comment_response_link",
                "item_id": link["link_id"],
                "reason": "Grouped Menlo response note may cover this same-topic comment; confirm before treating as production truth",
                "source_document": response.get("source_document", ""),
                "source_location": response.get("source_location", ""),
                "suggested_action": "Confirm the response note covers the cited government comment and supporting source package",
                "decision": "",
                "decision_note": "",
            })
        response["comment_ids"] = sorted(matched_ids)
    return review


def enforce_visual_verification(
    comments: list[dict[str, Any]], responses: list[dict[str, Any]], links: list[dict[str, Any]],
) -> None:
    """Legacy prefix decisions cannot silently confirm failed Gemini verification."""
    comments_by_id = {row["comment_id"]: row for row in comments}
    responses_by_id = {row["response_id"]: row for row in responses}
    for link in links:
        if link.get("provenance") != "gemini_visual_two_pass" or link.get("verification_status") != "needs_review":
            continue
        link["review_status"] = "needs_review"
        link["match_confidence"] = 0.0
        comments_by_id[link["comment_id"]]["human_review_status"] = "needs_review"
        if link.get("response_id") in responses_by_id:
            responses_by_id[link["response_id"]]["human_review_status"] = "needs_review"


def legacy_structured_refresh_paths(
    dataset: dict[str, Any],
    site_filters: list[str] | None = None,
) -> set[str]:
    """Select only replaceable old visual spreadsheet rows.

    Manually confirmed/rematched spreadsheet records are intentionally excluded;
    a pipeline refresh must never overwrite those authoritative links.
    """
    filters = [
        value.casefold() for value in site_filters or [] if value.strip()
    ]
    links = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comment_response_links", [])
        if isinstance(row, dict)
    }
    selected: set[str] = set()
    for comment in dataset.get("comments", []):
        if not isinstance(comment, dict):
            continue
        link = links.get(str(comment.get("comment_id", "")), {})
        provenance = str(link.get("provenance", ""))
        if provenance not in {
            "gemini_visual_two_pass",
            "local_structured_gemini_verified",
        }:
            continue
        if (
            provenance == "local_structured_gemini_verified"
            and link.get("review_status") != "needs_review"
            and comment.get("search_eligible") is True
        ):
            continue
        version = str(comment.get("ingestion_pipeline_version", ""))
        if not version.startswith("adaptive-document-ingestion-"):
            continue
        for path in _row_source_paths(comment):
            if Path(path).suffix.casefold() not in {".xlsx", ".csv"}:
                continue
            if filters and not any(
                value in path.casefold() for value in filters
            ):
                continue
            selected.add(path)
    return selected


def retryable_ingestion_paths(
    ingestion_report: dict[str, Any],
    site_filters: list[str] | None = None,
) -> set[str]:
    """Return failed source paths that a normal incremental run must resume.

    Older runs could leave a failed source in ``processed_source_paths``.  That
    made a later ``--site`` run skip the source unless the operator also knew
    to pass ``--refresh-source``.  The ingestion report is the authoritative
    source state, so terminal retryable states reopen the source automatically.
    Restricting the result to the active site filters prevents a focused run
    from unexpectedly retrying failures elsewhere in the workspace.
    """
    filters = [
        value.casefold() for value in site_filters or [] if value.strip()
    ]
    retryable = {"failed", "paused_quota", "circuit_open"}
    selected: set[str] = set()
    for row in ingestion_report.get("files", []):
        relative = str(row.get("relative_path", "")).strip()
        if not relative:
            continue
        if str(row.get("processing_status", "")).strip() not in retryable:
            continue
        reason = str(
            row.get("processing_error")
            or row.get("review_reason")
            or ""
        ).casefold()
        if "status is unknown after submission" in reason:
            # The server may still have completed this request.  Reusing the
            # same request identity is not enough to prevent a second charge,
            # so ordinary resume runs quarantine it for explicit review.
            continue
        if filters and not any(value in relative.casefold() for value in filters):
            continue
        selected.add(relative)
    return selected


def ambiguous_submission_paths(
    ingestion_report: dict[str, Any],
    site_filters: list[str] | None = None,
) -> set[str]:
    filters = [
        value.casefold() for value in site_filters or [] if value.strip()
    ]
    selected: set[str] = set()
    for row in ingestion_report.get("files", []):
        relative = str(row.get("relative_path", "")).strip()
        reason = str(
            row.get("processing_error")
            or row.get("review_reason")
            or ""
        ).casefold()
        if not relative or "status is unknown after submission" not in reason:
            continue
        if filters and not any(value in relative.casefold() for value in filters):
            continue
        selected.add(relative)
    return selected


def orphaned_pending_paths(
    ingestion_report: dict[str, Any],
    processed: set[str],
    represented_paths: set[str],
    site_filters: list[str] | None = None,
) -> set[str]:
    """Find pending paths incorrectly marked processed with no stored output."""
    filters = [
        value.casefold() for value in site_filters or [] if value.strip()
    ]
    selected: set[str] = set()
    for row in ingestion_report.get("files", []):
        relative = str(row.get("relative_path", "")).strip()
        if (
            not relative
            or str(row.get("processing_status", "")) != "pending"
            or relative not in processed
            or relative in represented_paths
        ):
            continue
        if filters and not any(value in relative.casefold() for value in filters):
            continue
        selected.add(relative)
    return selected


def upsert_ingested_group(
    comments: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    links: list[dict[str, Any]],
    incoming_comments: list[dict[str, Any]],
    incoming_responses: list[dict[str, Any]],
    incoming_links: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    """Idempotently replace cached records without overwriting confirmations."""
    existing_comments = {
        str(row.get("comment_id", "")): row for row in comments
    }
    existing_links = {
        str(row.get("comment_id", "")): row for row in links
    }
    incoming_by_id: dict[str, dict[str, Any]] = {}
    for row in incoming_comments:
        comment_id = str(row.get("comment_id", ""))
        previous = incoming_by_id.get(comment_id)
        if previous is not None and str(previous.get("original_text", "")).strip() != str(
            row.get("original_text", "")
        ).strip():
            raise ValueError(
                f"Conflicting duplicate comment ID in incoming extraction: {comment_id}"
            )
        incoming_by_id.setdefault(comment_id, row)
    incoming_comments = list(incoming_by_id.values())
    incoming_ids = set(incoming_by_id)
    preserve_confirmed: set[str] = set()
    for comment_id in incoming_ids & set(existing_comments):
        old_link = existing_links.get(comment_id, {})
        confirmed = (
            str(old_link.get("review_status", "")).casefold() == "confirmed"
            or str(old_link.get("match_status", "")).casefold() == "confirmed"
        )
        if not confirmed:
            continue
        old_text = str(existing_comments[comment_id].get("original_text", "")).strip()
        new_text = str(incoming_by_id[comment_id].get("original_text", "")).strip()
        if old_text != new_text:
            raise ValueError(
                f"Incoming extraction conflicts with confirmed comment {comment_id}"
            )
        preserve_confirmed.add(comment_id)
    replace_ids = incoming_ids - preserve_confirmed
    removed_response_ids = {
        str(row.get("response_id", "")) for row in responses
        if str(row.get("comment_id", "")) in replace_ids
    }
    comments = [
        row for row in comments
        if str(row.get("comment_id", "")) not in replace_ids
    ]
    responses = [
        row for row in responses
        if str(row.get("comment_id", "")) not in replace_ids
        and str(row.get("response_id", "")) not in removed_response_ids
    ]
    links = [
        row for row in links
        if str(row.get("comment_id", "")) not in replace_ids
        and str(row.get("response_id", "")) not in removed_response_ids
    ]
    incoming_comments = [
        row for row in incoming_comments
        if str(row.get("comment_id", "")) not in preserve_confirmed
    ]
    incoming_responses = [
        row for row in incoming_responses
        if str(row.get("comment_id", "")) not in preserve_confirmed
    ]
    incoming_links = [
        row for row in incoming_links
        if str(row.get("comment_id", "")) not in preserve_confirmed
    ]
    return (
        comments, responses, links,
        incoming_comments, incoming_responses, incoming_links,
    )


def run_incremental(
    workspace: Path,
    audit_dir: Path,
    output_dir: Path,
    review_decisions: Path | None,
    pipeline: VisualIngestionPipeline,
    site_filters: list[str] | None = None,
    refresh_structured_spreadsheets: bool = False,
    file_workers: int = 1,
    prescan_decisions: dict[str, dict[str, Any]] | None = None,
    refresh_source_filters: list[str] | None = None,
) -> dict[str, int]:
    run_started_monotonic = time.perf_counter()
    run_id = utc_timestamp()
    workspace = workspace.resolve()
    audit_dir = audit_dir.resolve()
    output_dir = output_dir.resolve()
    dataset, existing_review = load_existing(output_dir)
    inventory, summaries = base.load_audit(audit_dir)
    ingestion_report_path = output_dir / "ingestion_report.json"
    try:
        previous_ingestion_report = json.loads(
            ingestion_report_path.read_text(encoding="utf-8")
        ) if ingestion_report_path.is_file() else {}
    except (OSError, json.JSONDecodeError, AttributeError):
        previous_ingestion_report = {}
    ingestion_report = inventory_supported_files(workspace, inventory, ingestion_report_path)
    write_pipeline_checkpoint(
        output_dir,
        run_id,
        {"uploaded": "complete", "parsed": "complete", "prescanned": "in_progress",
         "extracted": "pending", "verified": "pending", "deduplicated": "pending",
         "timeline_linked": "pending", "indexed": "pending"},
        source_root=str(workspace),
        discovered_files=len(ingestion_report.get("files", [])),
    )
    ingestion_report_by_path = {
        str(row.get("relative_path", "")): row for row in ingestion_report["files"]
    }
    groups = all_source_groups(inventory, ingestion_report["files"])
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
    processed_hashes = {
        str(path): str(digest) for path, digest in dataset.get("processed_source_hashes", {}).items()
    } if isinstance(dataset.get("processed_source_hashes"), dict) else {}
    for path in processed:
        if path not in processed_hashes and path in inventory:
            processed_hashes[path] = str(inventory[path].get("sha256", ""))
    # A full-read prescan decision is authoritative. Re-open only sources that
    # an older local page gate declared irrelevant without extracting records.
    for relative, decision in (prescan_decisions or {}).items():
        if str(decision.get("decision", "")) != "full_read":
            continue
        report_row = ingestion_report_by_path.get(relative, {})
        if str(report_row.get("processing_status", "")) != "no_relevant_content":
            continue
        digest = str(report_row.get("sha256", ""))
        manifest = (
            output_dir / "ingestion_artifacts" /
            f"VI-{digest[:20]}" / "manifest.json"
        )
        try:
            screen_version = str(json.loads(
                manifest.read_text(encoding="utf-8")
            ).get("page_screening_version", ""))
        except (OSError, json.JSONDecodeError, AttributeError):
            screen_version = ""
        if screen_version != PAGE_SCREENING_VERSION:
            processed.discard(relative)
            processed_hashes.pop(relative, None)
    processed_hash_values = {value for value in processed_hashes.values() if value}
    comments = list(dataset.get("comments", []))
    responses = list(dataset.get("responses", []))
    links = list(dataset.get("comment_response_links", []))
    source_rows = list(dataset.get("sources", []))
    review_rows: list[dict[str, Any]] = list(existing_review)
    normalized_site_filters = [
        value.casefold() for value in site_filters or [] if value.strip()
    ]
    refresh_paths = (
        legacy_structured_refresh_paths(dataset, site_filters)
        if refresh_structured_spreadsheets else set()
    )
    automatic_retry_paths = retryable_ingestion_paths(
        previous_ingestion_report, site_filters,
    )
    ambiguous_paths = ambiguous_submission_paths(
        previous_ingestion_report, site_filters,
    )
    refresh_paths.update(automatic_retry_paths)
    represented_paths = {
        path
        for collection in (comments, responses, source_rows, review_rows)
        for row in collection
        for path in _row_source_paths(row)
    }
    refresh_paths.update(orphaned_pending_paths(
        ingestion_report, processed, represented_paths, site_filters,
    ))
    previous_rows_by_path = {
        str(row.get("relative_path", "")): row
        for row in previous_ingestion_report.get("files", [])
        if isinstance(row, dict)
    }
    for path in ambiguous_paths:
        current = ingestion_report_by_path.get(path)
        previous = previous_rows_by_path.get(path)
        if current is not None and previous is not None:
            current.update({
                "processing_status": "failed",
                "completion_status": "failed",
                "review_reason": str(
                    previous.get("review_reason")
                    or previous.get("processing_error")
                    or "Gemini request status is unknown after submission"
                ),
            })
    normalized_refresh_filters = [
        value.casefold() for value in refresh_source_filters or []
        if value.strip()
    ]
    if normalized_refresh_filters:
        refresh_paths.update({
            path for path in processed
            if any(token in path.casefold() for token in normalized_refresh_filters)
        })
    if refresh_paths:
        refresh_comments = [
            row for row in comments if _row_source_paths(row) & refresh_paths
        ]
        refresh_comment_ids = {
            str(row.get("comment_id", "")) for row in refresh_comments
        }
        refresh_responses = [
            row for row in responses
            if str(row.get("comment_id", "")) in refresh_comment_ids
        ]
        refresh_response_ids = {
            str(row.get("response_id", "")) for row in refresh_responses
        }
        refresh_links = [
            row for row in links
            if str(row.get("comment_id", "")) in refresh_comment_ids
            or str(row.get("response_id", "")) in refresh_response_ids
        ]
        _archive_repair_rows(
            dataset,
            refresh_paths,
            refresh_comments,
            refresh_responses,
            refresh_links,
            dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            (
                "automatic_retry_failed_source"
                if refresh_paths.issubset(automatic_retry_paths)
                else "structured_spreadsheet_pipeline_refresh"
            ),
        )
        comments = [
            row for row in comments
            if str(row.get("comment_id", "")) not in refresh_comment_ids
        ]
        responses = [
            row for row in responses
            if str(row.get("response_id", "")) not in refresh_response_ids
            and str(row.get("comment_id", "")) not in refresh_comment_ids
        ]
        links = [
            row for row in links
            if str(row.get("comment_id", "")) not in refresh_comment_ids
            and str(row.get("response_id", "")) not in refresh_response_ids
        ]
        source_rows = [
            row for row in source_rows
            if not (_row_source_paths(row) & refresh_paths)
        ]
        review_rows = [
            row for row in review_rows
            if not (_row_source_paths(row) & refresh_paths)
        ]
        for path in refresh_paths:
            processed.discard(path)
            processed_hashes.pop(path, None)
        processed_hash_values = {
            value for value in processed_hashes.values() if value
        }
    new_groups = 0
    reused_groups = 0
    new_comments = 0
    for summary, records in groups:
        if normalized_site_filters:
            records = [
                record for record in records
                if any(
                    value in str(record.get("path", "")).casefold()
                    for value in normalized_site_filters
                )
            ]
            if not records:
                continue
        paths = {record["path"] for record in records}
        changed_in_place = [
            record["path"] for record in records
            if record["path"] in processed and processed_hashes.get(record["path"]) not in {"", str(record.get("sha256", ""))}
        ]
        if changed_in_place:
            changed_paths = set(changed_in_place)
            removed_comments = [
                row for row in comments if _row_source_paths(row) & changed_paths
            ]
            removed_comment_ids = {
                str(row.get("comment_id", "")) for row in removed_comments
            }
            removed_responses = [
                row for row in responses
                if _row_source_paths(row) & changed_paths
                or str(row.get("comment_id", "")) in removed_comment_ids
            ]
            removed_response_ids = {
                str(row.get("response_id", "")) for row in removed_responses
            }
            removed_links = [
                row for row in links
                if str(row.get("comment_id", "")) in removed_comment_ids
                or str(row.get("response_id", "")) in removed_response_ids
            ]
            _archive_repair_rows(
                dataset, changed_paths, removed_comments, removed_responses,
                removed_links,
                dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                str(summary.get("likely_city", "")),
            )
            comments = [
                row for row in comments
                if str(row.get("comment_id", "")) not in removed_comment_ids
            ]
            responses = [
                row for row in responses
                if str(row.get("response_id", "")) not in removed_response_ids
                and str(row.get("comment_id", "")) not in removed_comment_ids
            ]
            links = [
                row for row in links
                if str(row.get("comment_id", "")) not in removed_comment_ids
                and str(row.get("response_id", "")) not in removed_response_ids
            ]
            source_rows = [
                row for row in source_rows if not (_row_source_paths(row) & changed_paths)
            ]
            review_rows = [
                row for row in review_rows if not (_row_source_paths(row) & changed_paths)
            ]
            for path in changed_paths:
                processed.discard(path)
                processed_hashes.pop(path, None)
            processed_hash_values = {value for value in processed_hashes.values() if value}
        if paths.issubset(processed):
            reused_groups += 1
            continue
        cached_records = [
            record for record in records
            if record["path"] not in processed
            and record["path"] not in refresh_paths
            and str(record.get("sha256", "")) in processed_hash_values
        ]
        for record in cached_records:
            processed.add(record["path"])
            processed_hashes[record["path"]] = str(record.get("sha256", ""))
            report_row = ingestion_report_by_path.get(record["path"])
            if report_row is not None:
                original_path = next((
                    path for path, digest in processed_hashes.items()
                    if path != record["path"] and digest == str(record.get("sha256", ""))
                ), "")
                report_row.update({
                    "cache_reused_from": original_path,
                    "processing_status": "classified",
                    "opened": False,
                    "review_reason": "Reused prior result for identical SHA-256",
                })
        new_records = [
            record for record in records
            if record["path"] not in processed
            and record["path"] not in ambiguous_paths
        ]
        if not new_records:
            reused_groups += 1
            continue
        result = process_new_group(
            workspace, summary, new_records, pipeline,
            file_workers=max(1, file_workers),
            prescan_decisions=prescan_decisions,
        )
        c, r, l, s, q = result
        failed_paths = {
            path
            for source_summary in s
            if str(source_summary.get("processing_status", "")) in {
                "failed", "paused_quota", "circuit_open",
            }
            for path in _row_source_paths(source_summary)
        }
        completed_paths = paths - failed_paths
        # A successful visual two-pass extraction supersedes the temporary
        # deterministic XLSX staging rows. A failed Gemini attempt leaves the
        # staged rows intact so no auditable data is lost.
        staged_comments = [
            row for row in comments
            if row.get("ingestion_pipeline_version")
            == OFFLINE_STRUCTURED_PIPELINE_VERSION
            and (_row_source_paths(row) & completed_paths)
        ]
        staged_comment_ids = {
            str(row.get("comment_id", "")) for row in staged_comments
        }
        staged_response_ids = {
            str(row.get("response_id", "")) for row in responses
            if str(row.get("comment_id", "")) in staged_comment_ids
        }
        staged_link_ids = {
            str(row.get("link_id", "")) for row in links
            if str(row.get("comment_id", "")) in staged_comment_ids
        }
        if staged_comment_ids:
            comments = [
                row for row in comments
                if str(row.get("comment_id", "")) not in staged_comment_ids
            ]
            responses = [
                row for row in responses
                if str(row.get("response_id", "")) not in staged_response_ids
                and str(row.get("comment_id", "")) not in staged_comment_ids
            ]
            links = [
                row for row in links
                if str(row.get("comment_id", "")) not in staged_comment_ids
            ]
            review_rows = [
                row for row in review_rows
                if str(row.get("item_id", "")) not in staged_link_ids
            ]
            source_rows = [
                row for row in source_rows
                if not (
                    row.get("ingestion_pipeline_version")
                    == OFFLINE_STRUCTURED_PIPELINE_VERSION
                    and (_row_source_paths(row) & completed_paths)
                )
            ]
        comments, responses, links, c, r, l = upsert_ingested_group(
            comments, responses, links, c, r, l,
        )
        comments.extend(c)
        responses.extend(r)
        links.extend(l)
        source_rows.extend(s)
        review_rows.extend(q)
        processed.update(completed_paths)
        processed_hashes.update({
            record["path"]: str(record.get("sha256", ""))
            for record in new_records
            if record["path"] in completed_paths
        })
        processed_hash_values.update(
            str(record.get("sha256", ""))
            for record in new_records
            if record["path"] in completed_paths and record.get("sha256")
        )
        new_groups += 1
        new_comments += len(c)
    base.validate_dataset(comments, responses, links)
    decisions = base.load_review_decision(review_decisions)
    base.apply_review_decision(comments, responses, links, review_rows, decisions)
    enforce_visual_verification(comments, responses, links)
    hierarchy_report = merge_docx_comment_hierarchy(
        {"comments": comments, "comment_response_links": links},
        workspace,
    )
    lineage_context = {"comments": comments, "comment_response_links": links}
    lineage_report = mark_copied_source_documents(lineage_context, workspace)
    duplicate_report = mark_duplicate_comments({
        "comments": comments, "responses": responses,
        "comment_response_links": links,
    })
    issue_event_index = rebuild_issue_event_index(comments)
    issue_event_review_queue = collect_issue_event_review_queue(issue_event_index)
    comments.sort(key=lambda row: (
        str(row.get("city", "")),
        str(row.get("property_project", "")),
        base.natural_number(row.get("review_round", "")),
        str(row.get("source_document", "")),
        base.natural_number(row.get("source_row", "")),
        base.natural_number(row.get("source_page", "")),
        base.natural_number(row.get("comment_number", "")),
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
        "ingestion_pipeline_version": PIPELINE_VERSION,
        "comments": comments,
        "responses": responses,
        "comment_response_links": links,
        "sources": source_rows,
        "review_items": review_rows,
        "review_decisions": decisions,
        "processed_source_paths": sorted(processed),
        "processed_source_hashes": dict(sorted(processed_hashes.items())),
        "repair_history": dataset.get("repair_history", []),
        "source_lineage_groups": lineage_context.get("source_lineage_groups", {}),
        "issue_event_index": issue_event_index,
        "issue_event_review_queue": issue_event_review_queue,
    }
    identity = merge_pre_gemini_source_aliases(
        canonicalize_documents(updated["comments"]), source_rows,
    )
    updated.update({
        "source_files": identity["source_files"],
        "canonical_documents": identity["canonical_documents"],
        "source_file_aliases": identity["source_file_aliases"],
        "near_duplicate_review": identity["near_duplicate_review"],
    })
    # Keep the legacy dataset shape for the current app, while publishing a
    # normalized, versioned evidence projection for the next data layer.  This
    # is deterministic and does not re-read files or call Gemini.
    materialize_evidence_model(updated, output_dir)
    atomic_json(output_dir / "dataset.json", updated)
    ingestion_report = write_ingestion_report(
        ingestion_report_path, ingestion_report["files"], source_rows,
        {
            "run_id": run_id,
            "request_created_at": run_id,
            "finished_at": utc_timestamp(),
            "elapsed_seconds": round(time.perf_counter() - run_started_monotonic, 4),
            "file_workers": max(1, file_workers),
            "site_filters": list(site_filters or []),
        },
    )
    base.write_report(
        output_dir / "phase2_report.md",
        comments, responses, links, source_rows, review_rows,
    )
    failed_stage = "needs_review" if any(
        str(row.get("processing_status", "")) in {"failed", "paused_quota", "circuit_open", "needs_review"}
        for row in ingestion_report.get("files", [])
    ) else "complete"
    write_pipeline_checkpoint(
        output_dir,
        run_id,
        {"prescanned": "complete", "extracted": failed_stage, "verified": failed_stage,
         "deduplicated": "complete", "timeline_linked": "complete", "indexed": "complete"},
        run_elapsed_seconds=round(time.perf_counter() - run_started_monotonic, 4),
        totals=ingestion_report.get("totals", {}),
        evidence_model=updated.get("evidence_model", {}),
    )
    return {
        "reused_groups": reused_groups,
        "new_groups": new_groups,
        "new_comments": new_comments,
        "refreshed_structured_sources": len(refresh_paths),
        "total_comments": len(comments),
        "total_responses": len(responses),
        "matched": sum(row["match_status"] == "matched" for row in links),
        "unmatched": sum(row["match_status"] == "unmatched" for row in links),
        "confirmed_review_items": sum(
            row.get("decision") == "confirmed" for row in review_rows
        ),
        "pending_review_items": sum(not row.get("decision") for row in review_rows),
        "hierarchy_groups_merged": int(hierarchy_report["hierarchy_groups_merged"]),
        "hierarchy_children_suppressed": int(hierarchy_report["hierarchy_children_suppressed"]),
        "copied_source_groups": int(lineage_report["copied_source_groups"]),
        "copied_source_paths_suppressed": int(lineage_report["copied_source_paths_suppressed"]),
        "copied_comment_rows_suppressed": int(lineage_report["copied_comment_rows_suppressed"]),
        "canonical_document_count": int(identity["canonical_document_count"]),
        "source_file_aliases": len(identity["source_file_aliases"]),
        "duplicate_rows_suppressed": int(duplicate_report["duplicate_rows_suppressed"]),
        "ingestion_report": ingestion_report["totals"],
    }


OFFLINE_STRUCTURED_PIPELINE_VERSION = "deterministic-structured-offline-v1"


def _cycle_round(value: Any, fallback: Any = "") -> str:
    """Prefer an explicit spreadsheet cycle without displaying a trailing .0."""
    text = base.normalize_text(str(value or ""))
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    return text or base.normalize_text(str(fallback or ""))


def prepare_offline_spreadsheet_rows(
    path: Path,
    record: dict[str, Any],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    dict[str, Any], list[dict[str, Any]],
]:
    """Extract exact spreadsheet cells without promoting them to verified truth."""
    comments, responses, links, summary, review = base.extract_spreadsheet(path, record)
    sheet = str(record.get("primary_sheet") or "Review Comments")
    comment_column = base.primary_column(record, "likely_comment_columns", sheet)
    response_column = base.primary_column(record, "likely_response_columns", sheet)
    response_by_id = {str(row["response_id"]): row for row in responses}
    link_by_comment = {str(row["comment_id"]): row for row in links}

    for comment in comments:
        review_round = _cycle_round(
            comment.get("source_cycle"), comment.get("review_round"),
        )
        row = int(comment["source_row"])
        comment_cell = f"{comment_column}{row}"
        comment.update({
            "review_round": review_round,
            "reviewed_plan_round": review_round,
            "source_cell_range": comment_cell,
            "source_locator_json": {
                "viewer_type": "spreadsheet",
                "sheet_name": comment.get("source_sheet") or sheet,
                "cell_range": comment_cell,
                "source_row": row,
            },
            "normalized_comment_text": base.normalize_text(
                str(comment.get("original_text", "")),
            ),
            "human_review_status": "needs_review",
            "verification_status": "needs_review",
            "text_trust_status": "quarantined",
            "search_eligible": False,
            "ingestion_pipeline_version": OFFLINE_STRUCTURED_PIPELINE_VERSION,
        })
        link = link_by_comment[str(comment["comment_id"])]
        link.update({
            "comment_locator_json": copy.deepcopy(comment["source_locator_json"]),
            "verification_status": "needs_review",
            "provenance": "deterministic_structured_offline",
        })
        if comment.get("response_id"):
            response = response_by_id[str(comment["response_id"])]
            response_cell = f"{response_column}{row}"
            response.update({
                "source_cell_range": response_cell,
                "source_locator_json": {
                    "viewer_type": "spreadsheet",
                    "sheet_name": response.get("source_sheet") or sheet,
                    "cell_range": response_cell,
                    "source_row": row,
                },
                "human_review_status": "needs_review",
                "verification_status": "needs_review",
                "text_trust_status": "quarantined",
                "search_eligible": False,
                "ingestion_pipeline_version": OFFLINE_STRUCTURED_PIPELINE_VERSION,
            })
            comment["match_status"] = "needs_review"
            link.update({
                "match_status": "needs_review",
                "matching_method": "same_spreadsheet_row",
                "match_confidence": 0.0,
                "review_status": "needs_review",
                "response_locator_json": copy.deepcopy(
                    response["source_locator_json"],
                ),
            })
        else:
            comment["match_status"] = "unmatched"
            link.update({
                "match_status": "unmatched",
                "matching_method": "no_response_in_structured_source",
                "match_confidence": 1.0,
                "review_status": "not_applicable",
                "response_locator_json": {},
            })

    summary.update({
        "review_round": (
            _cycle_round(comments[0].get("source_cycle"))
            if comments and len({row.get("source_cycle") for row in comments}) == 1
            else str(record.get("likely_review_round", "unknown"))
        ),
        "processing_status": "needs_review",
        "opened": True,
        "pages_screened": [],
        "pages_fully_analyzed": [],
        "additional_markup_detected": False,
        "verification_result": "deterministic_structure_only_needs_review",
        "ingestion_pipeline_version": OFFLINE_STRUCTURED_PIPELINE_VERSION,
        "source_sha256": record.get("sha256", ""),
    })
    for item in review:
        item.update({
            "reason": (
                "Exact same-row spreadsheet extraction is locally structured "
                "but has not completed Gemini visual verification"
            ),
            "suggested_action": (
                "Verify the comment and response cells against the spreadsheet"
            ),
            "decision": "",
            "decision_note": "",
        })
    return comments, responses, links, summary, review


def _prefer_existing_verified(
    existing: dict[str, dict[str, Any]],
    incoming: list[dict[str, Any]],
    key: str,
) -> tuple[list[dict[str, Any]], int, int, int]:
    inserted = updated = skipped = 0
    for row in incoming:
        row_id = str(row[key])
        current = existing.get(row_id)
        if current and (
            current.get("text_trust_status") == "verified"
            or current.get("review_status") == "confirmed"
            or current.get("match_status") == "confirmed"
        ):
            skipped += 1
            continue
        if current:
            if all(
                current.get(field) == value
                for field, value in row.items()
                if field not in {"duplicate_of", "duplicate_status"}
            ):
                skipped += 1
                continue
        if current:
            updated += 1
        else:
            inserted += 1
        existing[row_id] = row
    return list(existing.values()), inserted, updated, skipped


def run_offline_structured(
    workspace: Path,
    audit_dir: Path,
    output_dir: Path,
    site_filters: list[str] | None = None,
) -> dict[str, Any]:
    """Idempotently stage auditable XLSX rows while Gemini is unavailable."""
    started = time.perf_counter()
    workspace = workspace.resolve()
    output_dir = output_dir.resolve()
    dataset, existing_review = load_existing(output_dir)
    inventory, _summaries = base.load_audit(audit_dir.resolve())
    report_path = output_dir / "ingestion_report.json"
    report = inventory_supported_files(workspace, inventory, report_path)
    filters = [value.casefold() for value in site_filters or [] if value.strip()]
    selected = [
        row for row in report["files"]
        if str(row.get("file_type", "")).casefold() == "xlsx"
        and (
            not filters
            or any(
                value in str(row.get("relative_path", "")).casefold()
                for value in filters
            )
        )
    ]
    if not selected:
        raise ValueError("No XLSX sources matched --site for offline structured import")

    incoming_comments: list[dict[str, Any]] = []
    incoming_responses: list[dict[str, Any]] = []
    incoming_links: list[dict[str, Any]] = []
    incoming_sources: list[dict[str, Any]] = []
    incoming_review: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for discovered in selected:
        relative = str(discovered["relative_path"])
        path = workspace / relative
        record = audit.inspect_file(
            path, workspace / "comments&response", workspace,
        )
        if _known_city(discovered.get("city")):
            record["likely_city"] = discovered["city"]
        if discovered.get("project"):
            record["likely_property_project"] = discovered["project"]
        file_started = time.perf_counter()
        c, r, l, s, q = prepare_offline_spreadsheet_rows(path, record)
        elapsed = time.perf_counter() - file_started
        incoming_comments.extend(c)
        incoming_responses.extend(r)
        incoming_links.extend(l)
        incoming_sources.append(s)
        incoming_review.extend(q)
        files.append({
            "source_document": relative,
            "comments": len(c),
            "responses": len(r),
            "elapsed_seconds": round(elapsed, 4),
        })
        discovered.update({
            "processing_status": "needs_review",
            "opened": True,
            "comments_extracted": len(c),
            "responses_extracted": len(r),
            "verification_result": "deterministic_structure_only_needs_review",
            "review_reason": (
                "Stored exact spreadsheet cells offline; Gemini verification pending"
            ),
        })

    comments_by_id = {
        str(row["comment_id"]): row for row in dataset.get("comments", [])
    }
    responses_by_id = {
        str(row["response_id"]): row for row in dataset.get("responses", [])
    }
    links_by_id = {
        str(row["link_id"]): row
        for row in dataset.get("comment_response_links", [])
    }
    comments, inserted_comments, updated_comments, skipped_comments = (
        _prefer_existing_verified(
            comments_by_id, incoming_comments, "comment_id",
        )
    )
    responses, inserted_responses, updated_responses, skipped_responses = (
        _prefer_existing_verified(
            responses_by_id, incoming_responses, "response_id",
        )
    )
    links, inserted_links, updated_links, skipped_links = (
        _prefer_existing_verified(
            links_by_id, incoming_links, "link_id",
        )
    )
    source_by_document = {
        str(row.get("source_document", "")): row
        for row in dataset.get("sources", [])
    }
    for row in incoming_sources:
        source_by_document[str(row["source_document"])] = row
    review_by_id = {
        str(row.get("item_id", "")): row
        for row in existing_review if row.get("item_id")
    }
    for row in incoming_review:
        review_by_id[str(row["item_id"])] = row
    review_rows = list(review_by_id.values())

    base.validate_dataset(comments, responses, links)
    duplicate_report = mark_duplicate_comments({
        "comments": comments, "responses": responses,
        "comment_response_links": links,
    })
    incoming_comment_ids = {
        str(row["comment_id"]) for row in incoming_comments
    }
    imported_duplicates = sum(
        str(row.get("comment_id", "")) in incoming_comment_ids
        and bool(row.get("duplicate_of"))
        for row in comments
    )
    comments.sort(key=lambda row: (
        str(row.get("city", "")), str(row.get("property_project", "")),
        base.natural_number(row.get("review_round", "")),
        str(row.get("source_document", "")),
        base.natural_number(row.get("source_row", "")),
    ))
    order = {str(row["comment_id"]): index for index, row in enumerate(comments)}
    responses.sort(key=lambda row: order.get(str(row["comment_id"]), 10**9))
    links.sort(key=lambda row: order.get(str(row["comment_id"]), 10**9))
    review_rows.sort(key=lambda row: (
        str(row.get("source_document", "")),
        base.natural_number(row.get("source_location", "")),
        str(row.get("item_id", "")),
    ))
    issue_event_index = rebuild_issue_event_index(comments)
    issue_event_review_queue = collect_issue_event_review_queue(issue_event_index)
    source_rows = list(source_by_document.values())
    offline_hashes = dataset.get("offline_structured_source_hashes", {})
    if not isinstance(offline_hashes, dict):
        offline_hashes = {}
    offline_hashes.update({
        str(row["relative_path"]): str(row.get("sha256", ""))
        for row in selected
    })
    dataset.update({
        "schema_version": "1.1",
        "comments": comments,
        "responses": responses,
        "comment_response_links": links,
        "sources": source_rows,
        "review_items": review_rows,
        "offline_structured_source_hashes": dict(sorted(offline_hashes.items())),
        "issue_event_index": issue_event_index,
        "issue_event_review_queue": issue_event_review_queue,
    })
    identity = merge_pre_gemini_source_aliases(
        canonicalize_documents(comments), source_rows,
    )
    dataset.update({
        "source_files": identity["source_files"],
        "canonical_documents": identity["canonical_documents"],
        "source_file_aliases": identity["source_file_aliases"],
        "near_duplicate_review": identity["near_duplicate_review"],
    })
    materialize_evidence_model(dataset, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    backup = output_dir / (
        "dataset.pre_offline_structured-"
        + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    atomic_json(backup, json.loads((output_dir / "dataset.json").read_text()))
    atomic_json(output_dir / "dataset.json", dataset)
    write_ingestion_report(report_path, report["files"], source_rows)
    base.write_csv(output_dir / "comments.csv", comments, base.COMMENT_FIELDS)
    base.write_csv(output_dir / "responses.csv", responses, base.RESPONSE_FIELDS)
    base.write_csv(
        output_dir / "comment_response_links.csv", links, base.LINK_FIELDS,
    )
    base.write_csv(output_dir / "source_summary.csv", source_rows, base.SOURCE_FIELDS)
    base.write_csv(
        output_dir / "extraction_review.csv", review_rows, base.REVIEW_FIELDS,
    )
    base.write_report(
        output_dir / "phase2_report.md",
        comments, responses, links, source_rows, review_rows,
    )
    return {
        "files": files,
        "files_imported": len(files),
        "inserted_comments": inserted_comments,
        "updated_comments": updated_comments,
        "skipped_comments": skipped_comments,
        "inserted_responses": inserted_responses,
        "updated_responses": updated_responses,
        "skipped_responses": skipped_responses,
        "inserted_links": inserted_links,
        "updated_links": updated_links,
        "skipped_links": skipped_links,
        "duplicate_rows_suppressed": int(
            duplicate_report["duplicate_rows_suppressed"],
        ),
        "imported_duplicate_rows_suppressed": imported_duplicates,
        "search_eligible_imported": sum(
            row.get("search_eligible") is True for row in incoming_comments
        ),
        "confirmed_links_imported": sum(
            row.get("review_status") == "confirmed" for row in incoming_links
        ),
        "total_comments": len(comments),
        "total_responses": len(responses),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "backup": str(backup),
    }


def run_inventory_only(workspace: Path, audit_dir: Path, output_dir: Path) -> dict[str, Any]:
    inventory, _summaries = base.load_audit(audit_dir.resolve())
    return inventory_supported_files(
        workspace.resolve(), inventory,
        output_dir.resolve() / "ingestion_report.json",
    )


def run_prescan_only(
    workspace: Path,
    audit_dir: Path,
    output_dir: Path,
    pipeline: VisualIngestionPipeline,
    include_processed: bool = False,
    site_filters: list[str] | None = None,
    workers: int = 3,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    audit_dir = audit_dir.resolve()
    output_dir = output_dir.resolve()
    dataset, _existing_review = load_existing(output_dir)
    inventory, summaries = base.load_audit(audit_dir)
    ingestion_report = inventory_supported_files(
        workspace, inventory, output_dir / "ingestion_report.json",
    )
    groups = coalesce_prescan_groups(
        all_source_groups(inventory, ingestion_report["files"]),
    )
    processed = set(dataset.get("processed_source_paths", []))
    plan: list[dict[str, Any]] = []
    totals = {"full_read": 0, "context_only": 0, "skip": 0}
    normalized_filters = [
        value.casefold() for value in site_filters or [] if value.strip()
    ]
    jobs: list[tuple[dict[str, str], list[dict[str, Any]]]] = []
    for summary, records in groups:
        if normalized_filters:
            records = [
                record for record in records
                if any(
                    value in str(record.get("path", "")).casefold()
                    for value in normalized_filters
                )
            ]
            if not records:
                continue
        new_records = records if include_processed else [record for record in records if record["path"] not in processed]
        if not new_records:
            continue
        print(
            f"Prescan-only: {summary.get('likely_city', '')} round {summary.get('likely_review_round', '')} "
            f"({len(new_records)} files)",
            file=sys.stderr,
            flush=True,
        )
        jobs.append((summary, new_records))

    if workers > 1 and len(jobs) > 1:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(workers, len(jobs)),
        ) as executor:
            futures = [
                executor.submit(
                    prescan_source_group, workspace, summary, records, pipeline,
                    True,
                )
                for summary, records in jobs
            ]
            prescans = [future.result() for future in futures]
    else:
        prescans = [
            prescan_source_group(
                workspace, summary, records, pipeline, use_gemini=True,
            )
            for summary, records in jobs
        ]

    for (summary, new_records), prescan in zip(jobs, prescans):
        for item in prescan.get("files", []):
            decision = str(item.get("decision", "skip"))
            if decision in totals:
                totals[decision] += 1
        plan.append({
            "city": summary.get("likely_city", ""),
            "property_project": summary.get("likely_property_project", ""),
            "review_round": summary.get("likely_review_round", ""),
            "files": prescan.get("files", []),
            "prescan_error": prescan.get("prescan_error", ""),
            "returned_file_count": prescan.get("returned_file_count", 0),
            "requested_file_count": prescan.get("requested_file_count", 0),
            "performance": prescan.get("performance", {}),
        })
    request_metrics = [
        row.get("performance", {}) for row in plan
        if isinstance(row.get("performance"), dict)
    ]
    result = {
        "include_processed": include_processed,
        "site_filters": site_filters or [],
        "workers": workers,
        "totals": totals,
        "performance": {
            "request_count": len(request_metrics),
            "elapsed_seconds": round(sum(float(row.get("elapsed_seconds") or 0.0) for row in request_metrics), 4),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in request_metrics),
            "cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in request_metrics),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in request_metrics),
            "thought_tokens": sum(int(row.get("thought_tokens") or 0) for row in request_metrics),
            "stragglers": summarize_request_metrics(request_metrics),
        },
        "groups": plan,
    }
    atomic_json(output_dir / "prescan_plan.json", result)
    return result


def _row_source_paths(row: dict[str, Any]) -> set[str]:
    return {
        item.strip()
        for item in str(row.get("source_document", "")).split(" | ")
        if item.strip()
    }


def _archive_repair_rows(
    dataset: dict[str, Any],
    source_paths: set[str],
    removed_comments: list[dict[str, Any]],
    removed_responses: list[dict[str, Any]],
    removed_links: list[dict[str, Any]],
    run_id: str,
    city: str,
) -> None:
    """Keep replaced rows available for audit instead of destroying history."""
    history = dataset.setdefault("repair_history", [])
    if not isinstance(history, list):
        history = []
        dataset["repair_history"] = history
    history.append({
        "run_id": run_id,
        "repair_type": "prescan_selective_visual_repair",
        "city": city,
        "source_paths": sorted(source_paths),
        "removed_comments": copy.deepcopy(removed_comments),
        "removed_responses": copy.deepcopy(removed_responses),
        "removed_links": copy.deepcopy(removed_links),
    })


def run_prescan_repair(
    workspace: Path,
    audit_dir: Path,
    output_dir: Path,
    plan_path: Path,
    pipeline: VisualIngestionPipeline,
    city: str = "Menlo Park",
    force: bool = False,
    source_filters: list[str] | None = None,
) -> dict[str, int]:
    """Re-extract only prescan-approved sources and atomically replace their rows.

    Existing rows are copied into ``repair_history`` before replacement. This
    makes the operation recoverable and keeps the immutable original extraction
    available for investigation while the repaired rows become searchable.
    """
    workspace = workspace.resolve()
    audit_dir = audit_dir.resolve()
    output_dir = output_dir.resolve()
    plan_path = plan_path.resolve()
    if not plan_path.is_file():
        raise ValueError(f"Prescan plan is missing: {plan_path}; run --prescan-only first")
    dataset, existing_review = load_existing(output_dir)
    pre_repair_dataset = copy.deepcopy(dataset)
    inventory, _summaries = base.load_audit(audit_dir)
    ingestion_report_path = output_dir / "ingestion_report.json"
    ingestion_report = inventory_supported_files(workspace, inventory, ingestion_report_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    selected: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for group in plan.get("groups", []):
        if str(group.get("city", "")) != city:
            continue
        summary = {
            "likely_city": group.get("city", city),
            "likely_property_project": group.get("property_project", ""),
            "likely_review_round": group.get("review_round", ""),
        }
        for decision in group.get("files", []):
            if not isinstance(decision, dict) or decision.get("decision") != "full_read":
                continue
            path = str(decision.get("relative_path", "")).strip()
            if source_filters and not any(token.casefold() in path.casefold() for token in source_filters):
                continue
            if path and path in inventory:
                selected[path] = (summary, decision)
    if not selected:
        suffix = f" matching {source_filters!r}" if source_filters else ""
        raise ValueError(f"No full_read files for {city!r}{suffix} were found in {plan_path}")

    source_paths = set(selected)
    existing_comments = list(dataset.get("comments", []))
    existing_responses = list(dataset.get("responses", []))
    existing_links = list(dataset.get("comment_response_links", []))
    removed_comments = [row for row in existing_comments if _row_source_paths(row) & source_paths]
    removed_comment_ids = {str(row.get("comment_id", "")) for row in removed_comments}
    removed_responses = [
        row for row in existing_responses
        if _row_source_paths(row) & source_paths
        or str(row.get("comment_id", "")) in removed_comment_ids
    ]
    removed_response_ids = {str(row.get("response_id", "")) for row in removed_responses}
    removed_links = [
        row for row in existing_links
        if str(row.get("comment_id", "")) in removed_comment_ids
        or str(row.get("response_id", "")) in removed_response_ids
    ]
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _archive_repair_rows(
        dataset, source_paths, removed_comments, removed_responses, removed_links,
        run_id, city,
    )

    comments = [row for row in existing_comments if str(row.get("comment_id", "")) not in removed_comment_ids]
    responses = [
        row for row in existing_responses
        if str(row.get("response_id", "")) not in removed_response_ids
        and str(row.get("comment_id", "")) not in removed_comment_ids
    ]
    links = [
        row for row in existing_links
        if str(row.get("comment_id", "")) not in removed_comment_ids
        and str(row.get("response_id", "")) not in removed_response_ids
    ]

    old_links_by_comment = {
        str(row.get("comment_id", "")): row for row in removed_links
        if str(row.get("comment_id", "")) not in removed_comment_ids
    }
    for comment in comments:
        response_id = str(comment.get("response_id", ""))
        if response_id not in removed_response_ids:
            continue
        comment["response_id"] = ""
        comment["match_status"] = "unmatched"
        comment["human_review_status"] = "needs_review"
        old_link = old_links_by_comment.get(str(comment.get("comment_id", "")))
        replacement = copy.deepcopy(old_link) if old_link else base.make_link(
            str(comment.get("comment_id", "")), "", str(comment.get("source_document", "")),
            str(comment.get("source_location", "")), 1.0,
        )
        replacement.update({
            "link_id": base.stable_id("L", comment["comment_id"], "NONE", "prescan_repair"),
            "response_id": "", "match_status": "unmatched",
            "matching_method": "prescan_repair_response_replaced",
            "match_confidence": 0.0, "review_status": "needs_review",
            "verification_status": "needs_review", "provenance": "prescan_selective_visual_repair",
        })
        links.append(replacement)

    new_comments: list[dict[str, Any]] = []
    new_responses: list[dict[str, Any]] = []
    new_links: list[dict[str, Any]] = []
    new_sources: list[dict[str, Any]] = []
    new_review: list[dict[str, Any]] = []
    repair_failures: list[str] = []
    groups: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for path, pair in selected.items():
        summary, decision = pair
        key = (
            str(summary.get("likely_city", city)),
            str(summary.get("likely_property_project", "")),
            str(summary.get("likely_review_round", "")),
        )
        groups.setdefault(key, []).append((inventory[path], decision))

    for (group_city, property_project, review_round), group_records in groups.items():
        group_comments: list[dict[str, Any]] = []
        group_responses: list[dict[str, Any]] = []
        group_links: list[dict[str, Any]] = []
        group_summaries: list[dict[str, Any]] = []
        group_review: list[dict[str, Any]] = []
        for record, decision in group_records:
            print(f"Repair full visual extraction: {record['path']}", file=sys.stderr, flush=True)
            try:
                result = pipeline.process(
                    workspace / record["path"], record["path"], {
                        "property_hint": property_project or record.get("likely_property_project", ""),
                        "city_hint": group_city,
                        "review_round_hint": review_round,
                        "audit_document_type_hint": record.get("document_type", ""),
                        "prescan_decision": decision,
                        "repair_run_id": run_id,
                        "ingestion_pipeline_version": PIPELINE_VERSION,
                    },
                    force=force,
                )
            except GeminiCircuitOpenError as exc:
                message = str(exc)
                repair_failures.append(f"{record['path']}: {message}")
                group_summaries.append({
                    **prescan_summary_row({
                        "likely_city": group_city,
                        "likely_property_project": property_project,
                        "likely_review_round": review_round,
                    }, record, decision),
                    "source_type": "paused_quota_circuit",
                    "processing_status": "paused_quota", "opened": False,
                    "processing_error": message,
                    "verification_result": "paused_quota",
                    "pages_screened": [], "pages_fully_analyzed": [],
                    "additional_markup_detected": False,
                    "ingestion_pipeline_version": PIPELINE_VERSION,
                })
                group_review.append({
                    "item_type": "ingestion_file",
                    "item_id": base.stable_id("F", record["path"]),
                    "reason": message,
                    "source_document": record["path"],
                    "source_location": "complete document",
                    "suggested_action": "Replenish Gemini credits, then resume this checkpoint",
                    "decision": "",
                    "decision_note": "Paused by run-level Gemini credit circuit breaker",
                })
                continue
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
                message = str(exc)
                repair_failures.append(f"{record['path']}: {message}")
                group_summaries.append({
                    **prescan_summary_row({
                        "likely_city": group_city,
                        "likely_property_project": property_project,
                        "likely_review_round": review_round,
                    }, record, decision),
                    "source_type": "failed_content_screening",
                    "processing_status": "failed", "opened": True,
                    "processing_error": message, "verification_result": "failed",
                    "pages_screened": [], "pages_fully_analyzed": [],
                    "additional_markup_detected": False,
                    "ingestion_pipeline_version": PIPELINE_VERSION,
                })
                group_review.append({
                    "item_type": "ingestion_file", "item_id": base.stable_id("F", record["path"]),
                    "reason": message, "source_document": record["path"],
                    "source_location": "complete document",
                    "suggested_action": "Resolve the file-level failure and selectively retry this hash",
                    "decision": "", "decision_note": "",
                })
                continue
            c, r, l, s, q = result
            group_comments.extend(c); group_responses.extend(r); group_links.extend(l)
            group_summaries.extend(s if isinstance(s, list) else [s]); group_review.extend(q)
        group_review.extend(apply_grouped_response_notes(
            {"likely_city": group_city}, [record for record, _decision in group_records],
            group_comments, group_responses, group_links,
        ))
        enforce_visual_verification(group_comments, group_responses, group_links)
        new_comments.extend(group_comments); new_responses.extend(group_responses)
        new_links.extend(group_links); new_sources.extend(group_summaries); new_review.extend(group_review)

    if repair_failures:
        raise RuntimeError(
            "Selective repair produced file-level failures; existing rows were preserved and no dataset changes were committed: "
            + " | ".join(repair_failures)
        )

    removed_confirmed_by_source: dict[str, int] = {}
    removed_link_by_comment = {
        str(row.get("comment_id", "")): row for row in removed_links
    }
    for comment in removed_comments:
        link = removed_link_by_comment.get(str(comment.get("comment_id", "")), {})
        if link.get("review_status") != "confirmed":
            continue
        for path in _row_source_paths(comment):
            if path in source_paths:
                removed_confirmed_by_source[path] = removed_confirmed_by_source.get(path, 0) + 1
    verified_new_by_source: dict[str, int] = {}
    for comment in new_comments:
        if comment.get("text_trust_status") != "verified" or comment.get("search_eligible") is not True:
            continue
        for path in _row_source_paths(comment):
            verified_new_by_source[path] = verified_new_by_source.get(path, 0) + 1
    destructive_conflicts = [
        f"{path}: would replace {expected} confirmed rows with "
        f"{verified_new_by_source.get(path, 0)} verified rows"
        for path, expected in removed_confirmed_by_source.items()
        if verified_new_by_source.get(path, 0) < expected
    ]
    if destructive_conflicts:
        raise RuntimeError(
            "Selective repair failed the confirmed-record preservation gate; existing rows were preserved and no dataset changes were committed: "
            + " | ".join(destructive_conflicts)
        )

    comments.extend(new_comments)
    responses.extend(new_responses)
    links.extend(new_links)
    source_rows = [
        row for row in dataset.get("sources", [])
        if not (_row_source_paths(row) & source_paths)
    ]
    source_rows.extend(new_sources)
    review_rows = [
        row for row in existing_review
        if not (_row_source_paths(row) & source_paths)
    ]
    review_rows.extend(new_review)

    base.validate_dataset(comments, responses, links)
    hierarchy_report = merge_docx_comment_hierarchy(
        {"comments": comments, "comment_response_links": links},
        workspace,
    )
    lineage_context = {"comments": comments, "comment_response_links": links}
    lineage_report = mark_copied_source_documents(lineage_context, workspace)
    duplicate_report = mark_duplicate_comments({
        "comments": comments, "responses": responses,
        "comment_response_links": links,
    })
    issue_event_index = rebuild_issue_event_index(comments)
    issue_event_review_queue = collect_issue_event_review_queue(issue_event_index)
    comments.sort(key=lambda row: (
        row["city"], row["property_project"], base.natural_number(row["review_round"]),
        row["source_document"], base.natural_number(row.get("source_row", "")),
        base.natural_number(row.get("source_page", "")), base.natural_number(row.get("comment_number", "")),
    ))
    order = {row["comment_id"]: index for index, row in enumerate(comments)}
    responses.sort(key=lambda row: order[row["comment_id"]])
    links.sort(key=lambda row: order[row["comment_id"]])
    review_rows.sort(key=lambda row: (
        row.get("source_document", ""), base.natural_number(row.get("source_location", "")), row.get("item_id", ""),
    ))
    processed_hashes = dataset.get("processed_source_hashes", {})
    if not isinstance(processed_hashes, dict):
        processed_hashes = {}
    processed_hashes.update({path: str(inventory[path].get("sha256", "")) for path in source_paths})
    dataset.update({
        "schema_version": "1.1",
        "ingestion_pipeline_version": PIPELINE_VERSION,
        "comments": comments, "responses": responses, "comment_response_links": links,
        "sources": source_rows, "review_items": review_rows,
        "processed_source_hashes": dict(sorted(processed_hashes.items())),
        "source_lineage_groups": lineage_context.get("source_lineage_groups", {}),
        "issue_event_index": issue_event_index,
        "issue_event_review_queue": issue_event_review_queue,
    })
    identity = merge_pre_gemini_source_aliases(
        canonicalize_documents(dataset["comments"]), source_rows,
    )
    dataset.update({
        "source_files": identity["source_files"],
        "canonical_documents": identity["canonical_documents"],
        "source_file_aliases": identity["source_file_aliases"],
        "near_duplicate_review": identity["near_duplicate_review"],
    })
    dataset.setdefault("processed_source_paths", [])
    dataset["processed_source_paths"] = sorted(set(dataset["processed_source_paths"]) | source_paths)
    materialize_evidence_model(dataset, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    backup = output_dir / f"dataset.pre_prescan_repair-{run_id}.json"
    atomic_json(backup, pre_repair_dataset)
    atomic_json(output_dir / "dataset.json", dataset)
    ingestion_report = write_ingestion_report(
        ingestion_report_path, ingestion_report["files"], source_rows,
    )
    base.write_csv(output_dir / "comments.csv", comments, base.COMMENT_FIELDS)
    base.write_csv(output_dir / "responses.csv", responses, base.RESPONSE_FIELDS)
    base.write_csv(output_dir / "comment_response_links.csv", links, base.LINK_FIELDS)
    base.write_csv(output_dir / "source_summary.csv", source_rows, base.SOURCE_FIELDS)
    base.write_csv(output_dir / "extraction_review.csv", review_rows, base.REVIEW_FIELDS)
    base.write_report(output_dir / "phase2_report.md", comments, responses, links, source_rows, review_rows)
    return {
        "city": city,
        "repaired_sources": len(source_paths),
        "removed_comments": len(removed_comments),
        "removed_responses": len(removed_responses),
        "removed_links": len(removed_links),
        "inserted_comments": len(new_comments),
        "inserted_responses": len(new_responses),
        "total_comments": len(comments),
        "total_responses": len(responses),
        "matched": sum(row.get("match_status") == "matched" for row in links),
        "unmatched": sum(row.get("match_status") == "unmatched" for row in links),
        "duplicate_rows_suppressed": int(duplicate_report["duplicate_rows_suppressed"]),
        "hierarchy_groups_merged": int(hierarchy_report["hierarchy_groups_merged"]),
        "hierarchy_children_suppressed": int(hierarchy_report["hierarchy_children_suppressed"]),
        "copied_source_groups": int(lineage_report["copied_source_groups"]),
        "copied_source_paths_suppressed": int(lineage_report["copied_source_paths_suppressed"]),
        "copied_comment_rows_suppressed": int(lineage_report["copied_comment_rows_suppressed"]),
        "canonical_document_count": int(identity["canonical_document_count"]),
        "source_file_aliases": len(identity["source_file_aliases"]),
        "review_items": len(review_rows),
        "ingestion_report": ingestion_report["totals"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=Path.cwd())
    parser.add_argument("--audit-dir", type=Path, default=Path("corpus_audit_output"))
    parser.add_argument("--output", type=Path, default=Path("phase2_dataset"))
    parser.add_argument("--review-decisions", type=Path)
    parser.add_argument("--artifact-root", type=Path, default=Path("phase2_dataset/ingestion_artifacts"))
    parser.add_argument("--oracle-dataset", type=Path, default=Path("phase2_dataset/dataset.json"))
    parser.add_argument(
        "--gemini-model",
        default=os.environ.get(
            "INGESTION_GEMINI_MODEL",
            os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        ),
        help="Strong Gemini model used for document extraction and verification",
    )
    parser.add_argument(
        "--prescan-gemini-model",
        default=os.environ.get(
            "PRESCAN_GEMINI_MODEL", "gemini-3.1-flash-lite",
        ),
        help="Lower-cost Gemini model used only for prescan and simple file classification",
    )
    parser.add_argument("--gemini-api-key-stdin", action="store_true", help="Read Gemini key from a hidden prompt")
    parser.add_argument("--prescan-only", action="store_true", help="Write a Gemini prescan plan without full visual extraction")
    parser.add_argument("--inventory-only", action="store_true", help="Register every supported source and write the reconciled ingestion report without Gemini")
    parser.add_argument("--offline-structured", action="store_true", help="Stage exact XLSX cells as needs_review without using Gemini")
    parser.add_argument(
        "--refresh-structured-spreadsheets",
        action="store_true",
        help=(
            "Replace only older Gemini-visual XLSX/CSV rows with the current "
            "structured pipeline; manually confirmed rematches are preserved"
        ),
    )
    parser.add_argument("--prescan-include-processed", action="store_true", help="Include already processed sources in the prescan-only plan")
    parser.add_argument("--prescan-site", action="append", default=[], help="Prescan only source paths containing this text; repeatable")
    parser.add_argument("--prescan-workers", type=int, default=3, help="Maximum concurrent site-level Gemini prescan requests")
    parser.add_argument("--site", action="append", default=[], help="Fully ingest only source paths containing this text; repeatable")
    parser.add_argument(
        "--refresh-source", action="append", default=[],
        help=(
            "Rebuild existing rows for source paths containing this text, "
            "reusing compatible extraction/verification artifacts; repeatable"
        ),
    )
    parser.add_argument("--repair-prescan", action="store_true", help="Replace one city's existing rows using full_read files from the prescan plan")
    parser.add_argument("--repair-city", default="Menlo Park", help="City to repair with --repair-prescan")
    parser.add_argument("--prescan-plan", type=Path, default=Path("phase2_dataset/prescan_plan.json"), help="Prescan plan used by --repair-prescan")
    parser.add_argument("--repair-force", action="store_true", help="Ignore cached Gemini extraction artifacts during repair")
    parser.add_argument("--repair-source", action="append", default=[], help="Repair only full_read sources whose path contains this value; repeatable")
    parser.add_argument("--gemini-timeout", type=int, default=600, help="Gemini request timeout in seconds")
    parser.add_argument("--render-dpi", type=int, default=220, help="Resolution for selected extraction evidence pages")
    parser.add_argument("--visual-batch-pages", type=int, default=6, help="Process selected visual pages in overlapping batches; 0 sends one full request")
    parser.add_argument("--visual-batch-overlap", type=int, default=1, help="Page overlap between visual batches")
    parser.add_argument(
        "--visual-batch-workers",
        type=int,
        default=int(os.environ.get("VISUAL_BATCH_WORKERS", "2")),
        help="Maximum independent Gemini page batches in flight (default: 2)",
    )
    parser.add_argument(
        "--folder-workers",
        type=int,
        default=int(os.environ.get("FOLDER_INGESTION_WORKERS", "2")),
        help=(
            "Maximum source files processed concurrently inside one folder "
            "group (default: 2)"
        ),
    )
    args = parser.parse_args()
    command_started = time.perf_counter()
    command_started_at = (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    try:
        if args.inventory_only:
            result = run_inventory_only(args.workspace_root, args.audit_dir, args.output)
            print(json.dumps(result["totals"], sort_keys=True))
            return 0
        if args.offline_structured:
            result = run_offline_structured(
                args.workspace_root, args.audit_dir, args.output,
                site_filters=args.site,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        api_key = gemini_api_key()
        if not api_key and args.gemini_api_key_stdin:
            api_key = getpass.getpass("Gemini API key: ")
        if not api_key:
            raise ValueError("New-file ingestion requires GEMINI_API_KEY or --gemini-api-key-stdin")
        extraction_client = VisualGeminiClient(
            api_key, args.gemini_model, timeout=args.gemini_timeout,
        )
        prescan_client = VisualGeminiClient(
            api_key, args.prescan_gemini_model, timeout=args.gemini_timeout,
        )
        pipeline = VisualIngestionPipeline(
            extraction_client, args.artifact_root,
            args.oracle_dataset if args.oracle_dataset.is_file() else None,
            dpi=args.render_dpi,
            batch_pages=args.visual_batch_pages,
            batch_overlap=args.visual_batch_overlap,
            prescan_client=prescan_client,
            batch_workers=args.visual_batch_workers,
        )
        if args.repair_prescan:
            result = run_prescan_repair(
                args.workspace_root, args.audit_dir, args.output, args.prescan_plan, pipeline,
                city=args.repair_city, force=args.repair_force, source_filters=args.repair_source,
            )
        elif args.prescan_only:
            result = run_prescan_only(
                args.workspace_root, args.audit_dir, args.output, pipeline,
                include_processed=args.prescan_include_processed,
                site_filters=args.prescan_site,
                workers=max(1, args.prescan_workers),
            )
        else:
            decisions = load_prescan_decisions(
                args.prescan_plan.resolve()
                if args.prescan_plan else None
            )
            result = run_incremental(
                args.workspace_root, args.audit_dir, args.output,
                args.review_decisions,
                pipeline,
                site_filters=args.site,
                refresh_structured_spreadsheets=(
                    args.refresh_structured_spreadsheets
                ),
                file_workers=max(1, args.folder_workers),
                prescan_decisions=decisions,
                refresh_source_filters=args.refresh_source,
            )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"Incremental Phase 2 update failed: {exc}", file=__import__("sys").stderr)
        return 2
    command_completed_at = (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    run_timing = {
        "command_started_at": command_started_at,
        "dataset_ready_at": command_completed_at,
        "full_elapsed_seconds": round(
            time.perf_counter() - command_started, 4,
        ),
        "mode": (
            "repair_prescan" if args.repair_prescan
            else "prescan_only" if args.prescan_only
            else "incremental"
        ),
        "site_filters": list(args.site),
        "prescan_site_filters": list(args.prescan_site),
        "folder_workers": max(1, args.folder_workers),
        "visual_batch_workers": max(1, args.visual_batch_workers),
        "result_summary": result,
    }
    atomic_json(args.output.resolve() / "last_intake_timing.json", run_timing)
    if isinstance(result, dict):
        result = {**result, "run_timing": run_timing}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
