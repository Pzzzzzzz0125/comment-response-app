#!/usr/bin/env python3
"""Benchmark local intake routing for one or more newly added permit sites."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from corpus_audit import audit_corpus as audit
from phase2.incremental_update import (
    _known_city,
    quick_city_for_source,
    resolve_inventory_cities,
)
from phase2.visual_ingestion import (
    SUPPORTED_TYPES,
    direct_text_for_source,
    normalized_content_fingerprint,
    select_relevant_pages,
    xlsx_direct_text,
)


COMMENT_RESPONSE_TYPES = {
    "city_comments",
    "company_response",
    "combined_comment_response",
    "correction_notice",
    "review_letter",
}
ESTIMATE_VERSION = "local-site-preflight-v1"
HIGH_COST_INPUT_TOKENS = 100_000
HIGH_COST_MINUTES = 5.0


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_row_text(value: Any) -> str:
    return audit.normalize_space(html.unescape(str(value or "")).replace("\xa0", " ")).casefold()


def xlsx_comment_row_keys(path: Path) -> list[tuple[str, str, str]]:
    """Return stable ProjectDox comment identities for cross-snapshot reuse."""
    application_match = re.search(r"\b(\d{6,})\b", path.name)
    application = application_match.group(1) if application_match else path.parent.name
    keys: list[tuple[str, str, str]] = []
    for sheet in xlsx_direct_text(path).get("sheets", []):
        if "comment" not in str(sheet.get("name", "")).casefold():
            continue
        for row in sheet.get("rows", [])[1:]:
            cells = {
                str(cell.get("column", "")): str(cell.get("value", ""))
                for cell in row.get("cells", [])
            }
            text = normalized_row_text(cells.get("C", ""))
            if not text.startswith(("comment ", "markup ")):
                continue
            keys.append((
                application,
                normalized_row_text(cells.get("A", "")),
                text,
            ))
    return keys


def nested_text_characters(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(nested_text_characters(item) for item in value.values())
    if isinstance(value, list):
        return sum(nested_text_characters(item) for item in value)
    return 0


def pdf_page_texts(value: dict[str, Any]) -> list[str]:
    if value.get("kind") != "pdf_text_pages":
        return []
    return [
        str(row.get("text", ""))
        for row in value.get("pages", [])
        if isinstance(row, dict)
    ]


def estimated_page_count(row: dict[str, Any], text_characters: int) -> int:
    try:
        pages = int(row.get("page_count") or 0)
    except (TypeError, ValueError):
        pages = 0
    if pages > 0:
        return pages
    if str(row.get("file_type", "")).casefold() in {"xlsx", "xls", "csv"}:
        return 0
    return max(1, (text_characters + 2999) // 3000)


def estimate_file_cost(
    row: dict[str, Any],
    text_characters: int,
    spreadsheet_rows: int = 0,
    batch_pages: int = 6,
    batch_overlap: int = 1,
    batch_workers: int = 2,
) -> dict[str, Any]:
    """Estimate Gemini work from local structure without making an API call."""
    route = str(row.get("preflight_route") or "context_only")
    file_type = str(row.get("file_type", "")).casefold()
    pages = estimated_page_count(row, text_characters)
    if route in {"context_only", "cache_reuse"}:
        return {
            "route": route,
            "evidence_unit_count": pages,
            "estimated_input_tokens": {"low": 0, "central": 0, "high": 0},
            "estimated_minutes": {"low": 0.0, "central": 0.0, "high": 0.0},
            "high_cost": False,
            "requires_confirmation": False,
            "confidence": "high",
        }
    if route == "structured_spreadsheet":
        central_tokens = max(4_000, 2_500 + spreadsheet_rows * 500)
        central_seconds = max(30.0, 18.0 + spreadsheet_rows * 1.4)
        low_tokens = round(central_tokens * 0.70)
        high_tokens = round(central_tokens * 1.40)
        low_seconds = central_seconds * 0.75
        high_seconds = central_seconds * 1.50
        evidence_units = spreadsheet_rows
        confidence = "medium"
    else:
        batch_pages = max(1, batch_pages)
        overlap = max(0, min(batch_overlap, batch_pages - 1))
        step = max(1, batch_pages - overlap)
        batch_count = (
            1 if pages <= batch_pages
            else 1 + max(0, (pages - batch_pages + step - 1) // step)
        )
        transmitted_pages = min(pages, batch_pages)
        if batch_count > 1:
            transmitted_pages += max(0, batch_count - 1) * step
            transmitted_pages += max(0, batch_count - 1) * overlap
        overlap_factor = transmitted_pages / max(1, pages)
        scoped_characters = min(text_characters, pages * 25_000)
        image_tokens = transmitted_pages * 4_500
        text_tokens = scoped_characters / 4.0 * 3.0 * overlap_factor
        schema_tokens = batch_count * 2 * 600
        central_tokens = max(5_000, round(image_tokens + text_tokens + schema_tokens))
        token_latency_rate = 0.0045 if central_tokens < 100_000 else 0.0032
        sequential_seconds = max(
            central_tokens * token_latency_rate,
            batch_count * 2 * 12.0,
        )
        parallel_factor = (
            0.58
            if batch_workers > 1 and batch_count >= 3 and overlap <= 1
            else 1.0
        )
        central_seconds = sequential_seconds * parallel_factor + pages * 0.2
        low_tokens = round(central_tokens * 0.75)
        high_tokens = round(central_tokens * 1.30)
        low_seconds = central_seconds * 0.75
        high_seconds = central_seconds * 1.50
        evidence_units = pages
        confidence = "low" if pages >= 25 or text_characters == 0 else "medium"
    central_minutes = central_seconds / 60.0
    high_cost = (
        central_tokens >= HIGH_COST_INPUT_TOKENS
        or central_minutes >= HIGH_COST_MINUTES
    )
    return {
        "route": route,
        "evidence_unit_count": evidence_units,
        "estimated_input_tokens": {
            "low": low_tokens,
            "central": round(central_tokens),
            "high": high_tokens,
        },
        "estimated_minutes": {
            "low": round(low_seconds / 60.0, 2),
            "central": round(central_minutes, 2),
            "high": round(high_seconds / 60.0, 2),
        },
        "high_cost": high_cost,
        "requires_confirmation": high_cost,
        "confidence": confidence,
    }


def benchmark_site(
    workspace: Path,
    site: Path,
    batch_pages: int = 6,
    batch_overlap: int = 1,
    batch_workers: int = 2,
    processed_source_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    paths = sorted(
        path for path in site.rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.casefold() in SUPPORTED_TYPES
    )

    hash_started = time.perf_counter()
    digests = {path: file_hash(path) for path in paths}
    hash_seconds = time.perf_counter() - hash_started

    city_started = time.perf_counter()
    city_rows: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(workspace).as_posix()
        city, confidence, evidence, method = quick_city_for_source(
            path, relative,
        )
        city_rows.append({
            "relative_path": relative,
            "filename": path.name,
            "file_type": path.suffix.casefold().lstrip("."),
            "file_size_bytes": path.stat().st_size,
            "city": city,
            "city_confidence": confidence,
            "city_evidence": evidence,
            "city_resolution_method": method,
        })
    resolve_inventory_cities(workspace, city_rows, {})
    city_seconds = time.perf_counter() - city_started

    classification_started = time.perf_counter()
    inspected = [
        audit.inspect_file(path, site, workspace)
        for path in paths
    ]
    classification_seconds = time.perf_counter() - classification_started

    fingerprint_started = time.perf_counter()
    fingerprints: dict[str, list[str]] = defaultdict(list)
    raw_by_path: dict[Path, dict[str, Any]] = {}
    for path in paths:
        raw = direct_text_for_source(path)
        raw_by_path[path] = raw
        fingerprint = normalized_content_fingerprint(raw, digests[path])
        fingerprints[fingerprint].append(path.relative_to(workspace).as_posix())
    fingerprint_seconds = time.perf_counter() - fingerprint_started

    full_read = [
        row for row in inspected
        if row.get("document_type") in COMMENT_RESPONSE_TYPES
        or row.get("likely_contains_city_comments")
        or row.get("likely_contains_company_responses")
    ]
    context_only = [
        row for row in inspected if row not in full_read
    ]
    duplicate_groups = [
        rows for rows in fingerprints.values() if len(rows) > 1
    ]
    spreadsheet_keys_by_path = {
        path: xlsx_comment_row_keys(path)
        for path in paths if path.suffix.casefold() == ".xlsx"
    }
    xlsx_keys = [
        key for keys in spreadsheet_keys_by_path.values() for key in keys
    ]
    repeated_snapshot_rows = sum(
        count - 1 for count in Counter(xlsx_keys).values() if count > 1
    )
    resolved = [
        row for row in city_rows
        if _known_city(row.get("city"))
    ]
    city_counts = Counter(str(row["city"]) for row in resolved)
    site_city = (
        city_counts.most_common(1)[0][0]
        if city_counts and len(city_counts) == 1
        else "conflict" if len(city_counts) > 1
        else "Unknown"
    )
    processed_source_hashes = processed_source_hashes or {}
    processed_hash_values = {
        digest for digest in processed_source_hashes.values() if digest
    }
    duplicate_aliases = {
        relative
        for group in duplicate_groups
        for relative in group[1:]
    }
    nonarchive_full_read_types = {
        str(row.get("document_type", "unknown"))
        for path, row in zip(paths, inspected)
        if row in full_read
        and "/archive/" not in (
            "/" + path.relative_to(workspace).as_posix().casefold()
        )
    }
    file_estimates: list[dict[str, Any]] = []
    for path, inspected_row, city_row in zip(paths, inspected, city_rows):
        relative = path.relative_to(workspace).as_posix()
        current_digest = digests[path]
        prior_digest = processed_source_hashes.get(relative, "")
        if prior_digest == current_digest:
            scope_status = "unchanged_cached"
        elif prior_digest:
            scope_status = "changed"
        elif current_digest in processed_hash_values:
            scope_status = "identical_hash_cached"
        else:
            scope_status = "new"
        spreadsheet_rows = len(spreadsheet_keys_by_path.get(path, []))
        document_type = str(inspected_row.get("document_type", "unknown"))
        archived_superseded = (
            "/archive/" in ("/" + relative.casefold())
            and document_type in nonarchive_full_read_types
        )
        if scope_status in {"unchanged_cached", "identical_hash_cached"}:
            route = "cache_reuse"
            route_reason = (
                "source hash is already processed; reuse its cached extraction"
            )
        elif relative in duplicate_aliases:
            route = "context_only"
            route_reason = "duplicate file alias; canonical copy is estimated once"
        elif archived_superseded:
            route = "context_only"
            route_reason = (
                "archived same-site source; a newer non-archive source has "
                "the same document role"
            )
        elif spreadsheet_rows:
            route = "structured_spreadsheet"
            route_reason = (
                f"{spreadsheet_rows} locally detected spreadsheet comment rows"
            )
        elif inspected_row in full_read:
            route = "visual_full_read"
            route_reason = (
                f"local classification: {inspected_row.get('document_type', 'unknown')}"
            )
        else:
            route = "context_only"
            route_reason = (
                f"supporting/local classification: {inspected_row.get('document_type', 'unknown')}"
            )
        characters = nested_text_characters(raw_by_path[path])
        page_texts = pdf_page_texts(raw_by_path[path])
        physical_pages = len(page_texts)
        selected_pages: list[int] = []
        if route == "visual_full_read" and page_texts:
            selected_pages = list(
                select_relevant_pages(page_texts).get(
                    "pages_selected_for_full_analysis", []
                )
            )
        estimated_evidence_pages = (
            len(selected_pages)
            if selected_pages
            else physical_pages
        )
        estimate_row = {
            **inspected_row,
            "file_type": path.suffix.casefold().lstrip("."),
            "preflight_route": route,
        }
        if estimated_evidence_pages:
            estimate_row["page_count"] = estimated_evidence_pages
        cost = estimate_file_cost(
            estimate_row,
            characters,
            spreadsheet_rows=spreadsheet_rows,
            batch_pages=batch_pages,
            batch_overlap=batch_overlap,
            batch_workers=batch_workers,
        )
        file_estimates.append({
            "relative_path": relative,
            "filename": path.name,
            "file_type": path.suffix.casefold().lstrip("."),
            "file_size_bytes": path.stat().st_size,
            "city": city_row.get("city", "Unknown"),
            "document_type": inspected_row.get("document_type", "unknown"),
            "page_count": (
                physical_pages
                or estimated_page_count(estimate_row, characters)
            ),
            "estimated_selected_pages": estimated_page_count(
                estimate_row, characters,
            ),
            "direct_text_characters": characters,
            "spreadsheet_evidence_rows": spreadsheet_rows,
            "scope_status": scope_status,
            "route_reason": route_reason,
            **cost,
        })
    estimated_tokens = {
        key: sum(
            int(row["estimated_input_tokens"][key])
            for row in file_estimates
        )
        for key in ("low", "central", "high")
    }
    estimated_minutes = {
        key: round(sum(
            float(row["estimated_minutes"][key])
            for row in file_estimates
        ), 2)
        for key in ("low", "central", "high")
    }
    high_cost_files = [
        row["relative_path"] for row in file_estimates if row["high_cost"]
    ]
    approval_reasons: list[str] = []
    if site_city in {"Unknown", "conflict"}:
        approval_reasons.append(f"site city is {site_city}")
    if high_cost_files:
        approval_reasons.append(
            f"{len(high_cost_files)} high-cost file(s) require confirmation"
        )
    return {
        "estimate_version": ESTIMATE_VERSION,
        "site_folder": site.relative_to(workspace).as_posix(),
        "resolved_city": site_city,
        "city_resolution_methods": dict(sorted(Counter(
            str(row.get("city_resolution_method", ""))
            for row in city_rows
        ).items())),
        "files": len(paths),
        "bytes": sum(path.stat().st_size for path in paths),
        "local_route": {
            "full_read_candidates": len(full_read),
            "context_only_candidates": len(context_only),
            "exact_or_normalized_duplicate_aliases": sum(
                len(rows) - 1 for rows in duplicate_groups
            ),
            "canonical_files_after_dedup": len(paths) - sum(
                len(rows) - 1 for rows in duplicate_groups
            ),
            "spreadsheet_comment_rows": len(xlsx_keys),
            "unique_spreadsheet_comment_rows": len(set(xlsx_keys)),
            "repeated_spreadsheet_snapshot_rows": repeated_snapshot_rows,
            "document_types": dict(sorted(Counter(
                str(row.get("document_type", "unknown"))
                for row in inspected
            ).items())),
        },
        "preflight_estimate": {
            "scope": (
                "incremental_against_dataset"
                if processed_source_hashes else "full_folder_from_empty_cache"
            ),
            "configuration": {
                "visual_batch_pages": batch_pages,
                "visual_batch_overlap": batch_overlap,
                "visual_batch_workers": batch_workers,
            },
            "estimated_input_tokens": estimated_tokens,
            "estimated_minutes": estimated_minutes,
            "full_read_files": sum(
                row["route"] in {"visual_full_read", "structured_spreadsheet"}
                for row in file_estimates
            ),
            "context_only_files": sum(
                row["route"] == "context_only" for row in file_estimates
            ),
            "cache_reuse_files": sum(
                row["route"] == "cache_reuse" for row in file_estimates
            ),
            "new_files": sum(
                row["scope_status"] == "new" for row in file_estimates
            ),
            "changed_files": sum(
                row["scope_status"] == "changed" for row in file_estimates
            ),
            "high_cost_files": high_cost_files,
            "requires_confirmation": bool(approval_reasons),
            "confirmation_reasons": approval_reasons,
            "estimate_note": (
                "Local estimate only; Gemini demand, adaptive page splitting, "
                "OCR escalation, and uncertain document structure can change actual cost."
            ),
        },
        "file_estimates": file_estimates,
        "duplicate_groups": duplicate_groups,
        "timing_seconds": {
            "hashing": round(hash_seconds, 4),
            "city_resolution": round(city_seconds, 4),
            "local_classification": round(classification_seconds, 4),
            "normalized_fingerprinting": round(fingerprint_seconds, 4),
            "total": round(time.perf_counter() - started, 4),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sites", nargs="+", type=Path)
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE)
    parser.add_argument("--visual-batch-pages", type=int, default=6)
    parser.add_argument("--visual-batch-overlap", type=int, default=1)
    parser.add_argument("--visual-batch-workers", type=int, default=2)
    parser.add_argument(
        "--dataset", type=Path, default=Path("phase2_dataset/dataset.json"),
        help="Dataset whose processed hashes should be excluded from incremental cost",
    )
    parser.add_argument(
        "--ignore-dataset-cache", action="store_true",
        help="Estimate a first ingestion with an empty extraction cache",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Optional JSON path for the complete local preflight report",
    )
    args = parser.parse_args()
    workspace = args.workspace_root.resolve()
    processed_source_hashes: dict[str, str] = {}
    dataset_path = (
        args.dataset.resolve()
        if args.dataset.is_absolute() else (workspace / args.dataset).resolve()
    )
    if not args.ignore_dataset_cache and dataset_path.is_file():
        try:
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            hashes = dataset.get("processed_source_hashes", {})
            if isinstance(hashes, dict):
                processed_source_hashes = {
                    str(path): str(digest)
                    for path, digest in hashes.items() if str(digest)
                }
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            parser.error(f"Cannot read processed hashes from {dataset_path}: {exc}")
    results = [
        benchmark_site(
            workspace,
            site.resolve() if site.is_absolute() else (workspace / site).resolve(),
            batch_pages=max(1, args.visual_batch_pages),
            batch_overlap=max(0, args.visual_batch_overlap),
            batch_workers=max(1, args.visual_batch_workers),
            processed_source_hashes=processed_source_hashes,
        )
        for site in args.sites
    ]
    payload = {"estimate_version": ESTIMATE_VERSION, "sites": results}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = (
            args.output.resolve()
            if args.output.is_absolute()
            else (workspace / args.output).resolve()
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
