#!/usr/bin/env python3
"""Audit or suppress exact duplicate comments within one site/review round."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

try:
    from .comment_dedup import mark_duplicate_comments
    from .comment_hierarchy import (
        merge_docx_comment_hierarchy,
        refresh_hierarchy_source_locations,
    )
    from .source_lineage import mark_copied_source_documents
    from .document_identity import canonicalize_documents
except ImportError:
    from comment_dedup import mark_duplicate_comments
    from comment_hierarchy import (
        merge_docx_comment_hierarchy,
        refresh_hierarchy_source_locations,
    )
    from source_lineage import mark_copied_source_documents
    from document_identity import canonicalize_documents


def atomic_json(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix="deduplicate-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument(
        "--source-registry", type=Path,
        default=workspace / "web_app" / "data" / "source_registry.json",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    original = json.loads(args.dataset.read_text(encoding="utf-8"))
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    hierarchy_report = merge_docx_comment_hierarchy(dataset, workspace)
    lineage_report = mark_copied_source_documents(dataset, workspace)
    report = mark_duplicate_comments(dataset)
    report.update(hierarchy_report)
    report.update({
        key: lineage_report[key]
        for key in (
            "copied_source_groups", "copied_source_paths_suppressed",
            "copied_comment_rows_suppressed", "copied_source_details",
        )
    })
    identity = canonicalize_documents(dataset.get("comments", []))
    dataset.update({
        "source_files": identity["source_files"],
        "canonical_documents": identity["canonical_documents"],
        "source_file_aliases": identity["source_file_aliases"],
        "near_duplicate_review": identity["near_duplicate_review"],
    })
    report.update({
        "canonical_document_count": identity["canonical_document_count"],
        "source_file_aliases": len(identity["source_file_aliases"]),
    })
    report["applied"] = args.apply
    if args.apply:
        hierarchy_backup = args.dataset.with_suffix(".pre_comment_hierarchy.json")
        if not hierarchy_backup.exists():
            atomic_json(hierarchy_backup, original)
        lineage_backup = args.dataset.with_suffix(".pre_source_lineage.json")
        if not lineage_backup.exists():
            atomic_json(lineage_backup, original)
        backup = args.dataset.with_suffix(".pre_comment_dedup.json")
        if not backup.exists():
            atomic_json(backup, json.loads(args.dataset.read_text(encoding="utf-8")))
        atomic_json(args.dataset, dataset)
        if args.source_registry.is_file():
            registry = json.loads(args.source_registry.read_text(encoding="utf-8"))
            report["source_locations_refreshed"] = refresh_hierarchy_source_locations(
                dataset, registry,
            )
            atomic_json(args.source_registry, registry)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
