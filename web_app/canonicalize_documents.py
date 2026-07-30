#!/usr/bin/env python3
"""Persist physical-source and canonical-document identities in a dataset.

This is an explicit repair step rather than an implicit write during app
startup.  The browser annotates the same structures in memory, while ingestion
and this command persist them for audit and future imports.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    from .document_identity import canonicalize_documents
except ImportError:
    from document_identity import canonicalize_documents


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix="canonicalize-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def canonicalize_dataset(dataset: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = canonicalize_documents(dataset.get("comments", []))
    dataset["source_files"] = result["source_files"]
    dataset["canonical_documents"] = result["canonical_documents"]
    dataset["source_file_aliases"] = result["source_file_aliases"]
    dataset["near_duplicate_review"] = result["near_duplicate_review"]

    # Source summaries are also physical-file metadata.  Link them to the
    # logical document without replacing the original path or summary fields.
    by_path = {}
    for row in dataset.get("comments", []):
        path = str(row.get("source_document", "")).split(" | ", 1)[0].strip()
        if path and row.get("canonical_document_id"):
            by_path[path] = row
    for row in dataset.get("sources", []):
        path = str(row.get("source_document", "")).split(" | ", 1)[0].strip()
        comment = by_path.get(path)
        if comment:
            row["source_file_id"] = comment.get("source_file_id", "")
            row["canonical_document_id"] = comment.get("canonical_document_id", "")
    return dataset, result


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--apply", action="store_true", help="Write the canonical identities to the dataset")
    args = parser.parse_args()
    original = json.loads(args.dataset.read_text(encoding="utf-8"))
    candidate, result = canonicalize_dataset(json.loads(json.dumps(original)))
    report = {
        "applied": bool(args.apply),
        "physical_source_file_count": result["physical_source_file_count"],
        "canonical_document_count": result["canonical_document_count"],
        "duplicate_source_file_aliases": len(result["source_file_aliases"]),
        "near_duplicate_review": len(result["near_duplicate_review"]),
        "comments_marked_copied_duplicate": sum(
            row.get("occurrence_type") == "copied_duplicate" for row in candidate.get("comments", [])
        ),
    }
    if args.apply:
        backup = args.dataset.with_suffix(".pre_document_identity.json")
        if not backup.exists():
            atomic_json(backup, original)
        atomic_json(args.dataset, candidate)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
