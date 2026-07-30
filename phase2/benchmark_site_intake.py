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
    xlsx_direct_text,
)


COMMENT_RESPONSE_TYPES = {
    "city_comments",
    "company_response",
    "combined_comment_response",
    "correction_notice",
    "review_letter",
}


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


def benchmark_site(workspace: Path, site: Path) -> dict[str, Any]:
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
    for path in paths:
        raw = direct_text_for_source(path)
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
    xlsx_keys = [
        key
        for path in paths if path.suffix.casefold() == ".xlsx"
        for key in xlsx_comment_row_keys(path)
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
    return {
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
    args = parser.parse_args()
    workspace = args.workspace_root.resolve()
    results = [
        benchmark_site(
            workspace,
            site.resolve() if site.is_absolute() else (workspace / site).resolve(),
        )
        for site in args.sites
    ]
    print(json.dumps({"sites": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
