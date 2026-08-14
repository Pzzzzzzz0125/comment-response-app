#!/usr/bin/env python3
"""Remove explicitly invalid exact comment rows and their derived records.

This is intentionally narrow: only rows whose stored comment text is exactly
the supplied value are removed. Source documents themselves are preserved.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f"{path.stem}-", suffix=".tmp", delete=False
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def backup(path: Path, suffix: str) -> Path:
    destination = path.with_name(path.name + suffix)
    if not destination.exists():
        shutil.copy2(path, destination)
    return destination


def exact_text(row: dict[str, Any]) -> str:
    for key in ("verified_text", "exact_comment_text", "original_text", "normalized_comment_text"):
        value = row.get(key)
        if isinstance(value, str) and value == "A":
            return value
    return ""


def remove_exact_comments(
    dataset_path: Path,
    registry_path: Path | None = None,
    search_index_paths: tuple[Path, ...] = (),
    text: str = "A",
    explicit_comment_ids: set[str] | None = None,
) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    comments = dataset.get("comments", [])
    removed_comments = [
        row for row in comments
        if isinstance(row, dict) and any(row.get(key) == text for key in ("original_text", "verified_text", "exact_comment_text"))
    ]
    comment_ids = {str(row.get("comment_id")) for row in removed_comments if row.get("comment_id")}
    comment_ids.update(explicit_comment_ids or set())
    response_ids = {
        str(row.get("response_id"))
        for row in removed_comments
        if row.get("response_id")
    }
    # Remove any responses owned by the deleted comments, even when the comment
    # row did not carry response_id consistently.
    response_ids.update(
        str(row.get("response_id"))
        for row in dataset.get("responses", [])
        if isinstance(row, dict) and row.get("comment_id") in comment_ids and row.get("response_id")
    )
    dataset["comments"] = [row for row in comments if row not in removed_comments]
    dataset["responses"] = [
        row for row in dataset.get("responses", [])
        if not (isinstance(row, dict) and (row.get("response_id") in response_ids or row.get("comment_id") in comment_ids))
    ]
    dataset["comment_response_links"] = [
        row for row in dataset.get("comment_response_links", [])
        if not (isinstance(row, dict) and (row.get("comment_id") in comment_ids or row.get("response_id") in response_ids))
    ]
    index = dataset.get("issue_event_index")
    if isinstance(index, dict):
        for key in list(index):
            value = index[key]
            if key in comment_ids or (isinstance(value, dict) and (
                value.get("comment_id") in comment_ids or value.get("issue_thread_id") in comment_ids
            )):
                index.pop(key, None)
    dataset.setdefault("metadata", {})["invalid_exact_comment_deletions"] = {
        "text": text,
        "comment_ids": sorted(comment_ids),
        "response_ids": sorted(response_ids),
    }
    backup(dataset_path, ".pre_delete_invalid_exact_A.json")
    atomic_json(dataset_path, dataset)

    removed_sources: list[str] = []
    if registry_path and registry_path.is_file():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        sources = registry.get("sources", {})
        if isinstance(sources, dict):
            for source_id, source in list(sources.items()):
                if isinstance(source, dict) and source.get("owner_id") in comment_ids | response_ids:
                    sources.pop(source_id, None)
                    removed_sources.append(str(source_id))
        backup(registry_path, ".pre_delete_invalid_exact_A.json")
        atomic_json(registry_path, registry)

    removed_from_indexes: dict[str, int] = {}
    for path in search_index_paths:
        if not path.is_file():
            continue
        index_payload = json.loads(path.read_text(encoding="utf-8"))
        records = index_payload.get("records") if isinstance(index_payload, dict) else None
        if isinstance(records, dict):
            before = len(records)
            index_payload["records"] = {
                key: value for key, value in records.items() if key not in comment_ids
            }
            removed_from_indexes[str(path)] = before - len(index_payload["records"])
        elif isinstance(records, list):
            before = len(records)
            index_payload["records"] = [
                row for row in records
                if not (isinstance(row, dict) and row.get("comment_id") in comment_ids)
            ]
            removed_from_indexes[str(path)] = before - len(index_payload["records"])
        else:
            continue
        backup(path, ".pre_delete_invalid_exact_A.json")
        atomic_json(path, index_payload)

    return {
        "removed_comments": len(comment_ids),
        "removed_comment_ids": sorted(comment_ids),
        "removed_responses": len(response_ids),
        "removed_response_ids": sorted(response_ids),
        "removed_source_citations": len(removed_sources),
        "removed_source_ids": sorted(removed_sources),
        "removed_from_search_indexes": removed_from_indexes,
    }


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--registry", type=Path, default=workspace / "web_app" / "data" / "source_registry.json")
    parser.add_argument("--search-index", type=Path, action="append", default=[])
    parser.add_argument("--text", default="A")
    parser.add_argument("--comment-id", action="append", default=[])
    args = parser.parse_args()
    paths = tuple(args.search_index) or (workspace / "web_app" / "data" / "search_index.json", workspace / "phase2_dataset" / "search_index.json")
    print(json.dumps(remove_exact_comments(args.dataset, args.registry, paths, args.text, set(args.comment_id)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
