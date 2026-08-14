"""Checkpointed, non-regression text reconstruction for an existing dataset.

This migration only adds representation fields.  It does not rebuild or
rewrite comments, links, source occurrences, issue timelines, or topics.
Use ``--in-place`` only after reviewing the generated report; the default is a
separate output file so a production dataset is never silently overwritten.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Support both ``python -m phase2.reconstruct_existing`` and the documented
# direct script form from the repository root.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from phase2.evidence_model import relationship_snapshot
from web_app.text_reconstruction import (
    IDENTITY_NORMALIZATION_VERSION,
    RECONSTRUCTION_VERSION,
    SEARCH_NORMALIZATION_VERSION,
    attach_reconstruction,
)


MIGRATION_VERSION = "reconstruction-backfill-v1"


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _verified(record: dict[str, Any]) -> bool:
    return (
        str(record.get("text_trust_status", "")).casefold() == "verified"
        or str(record.get("verification_status", "")).casefold() == "confirmed"
        or (isinstance(record.get("reconstruction"), dict) and record["reconstruction"].get("verified") is True)
    )


def _units(record: dict[str, Any], role: str) -> list[str]:
    value = record.get("source_unit_ids")
    if isinstance(value, list) and value:
        return [str(item) for item in value if str(item).strip()]
    legacy = record.get("comment_unit_ids" if role == "comment" else "response_unit_ids")
    if isinstance(legacy, list):
        return [str(item) for item in legacy if str(item).strip()]
    audit = record.get("ingestion_audit")
    if isinstance(audit, dict):
        value = audit.get("comment_unit_ids" if role == "comment" else "response_unit_ids")
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
    return []


def _content_fingerprint(dataset: dict[str, Any]) -> str:
    """Fingerprint only the relationship graph for an audit report."""
    encoded = json.dumps(relationship_snapshot(dataset), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _representation_complete(record: dict[str, Any]) -> bool:
    """Whether a record already has the current additive representation."""
    metadata = record.get("reconstruction")
    return (
        isinstance(metadata, dict)
        and metadata.get("version") == RECONSTRUCTION_VERSION
        and "text_raw" in record
        and "text_reconstructed" in record
        and "display_structure" in record
        and "normalized_identity_text_v2" in record
        and "normalized_search_text_v2" in record
        and isinstance(record.get("source_unit_ids"), list)
    )


def backfill_dataset(dataset: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a copy with reconstruction fields and a migration report."""
    before = relationship_snapshot(dataset)
    result = copy.deepcopy(dataset)
    changed_comments = 0
    changed_responses = 0
    skipped_comments = 0
    skipped_responses = 0
    skipped_empty = 0
    formatting_only_changes = 0
    list_or_paragraph_repairs = 0
    manual_review = 0

    for record in result.get("comments", []):
        if not isinstance(record, dict):
            continue
        if _representation_complete(record):
            skipped_comments += 1
            continue
        old = (
            record.get("text_reconstructed"),
            record.get("normalized_identity_text_v2"),
            record.get("normalized_search_text_v2"),
            record.get("display_structure"),
        )
        updated = attach_reconstruction(
            record,
            role="comment",
            source_unit_ids=_units(record, "comment"),
            verified=_verified(record),
            method=(record.get("reconstruction") or {}).get("method", "local_deterministic_cleanup")
            if isinstance(record.get("reconstruction"), dict)
            else "local_deterministic_cleanup",
        )
        record.update(updated)
        if not updated.get("text_raw") and not updated.get("text_reconstructed"):
            skipped_empty += 1
        if old != (
            record.get("text_reconstructed"),
            record.get("normalized_identity_text_v2"),
            record.get("normalized_search_text_v2"),
            record.get("display_structure"),
        ):
            changed_comments += 1
            if record.get("text_raw") != record.get("text_reconstructed"):
                formatting_only_changes += 1
            if any(block.get("type") in {"list_item", "heading"} for block in record.get("display_structure", []) if isinstance(block, dict)):
                list_or_paragraph_repairs += 1
        if isinstance(record.get("reconstruction"), dict) and record["reconstruction"].get("uncertain"):
            manual_review += 1

    for record in result.get("responses", []):
        if not isinstance(record, dict):
            continue
        if _representation_complete(record):
            skipped_responses += 1
            continue
        old = (
            record.get("text_reconstructed"),
            record.get("normalized_identity_text_v2"),
            record.get("normalized_search_text_v2"),
            record.get("display_structure"),
        )
        updated = attach_reconstruction(
            record,
            role="response",
            source_unit_ids=_units(record, "response"),
            verified=_verified(record),
            method=(record.get("reconstruction") or {}).get("method", "local_deterministic_cleanup")
            if isinstance(record.get("reconstruction"), dict)
            else "local_deterministic_cleanup",
        )
        record.update(updated)
        if old != (
            record.get("text_reconstructed"),
            record.get("normalized_identity_text_v2"),
            record.get("normalized_search_text_v2"),
            record.get("display_structure"),
        ):
            changed_responses += 1
            if record.get("text_raw") != record.get("text_reconstructed"):
                formatting_only_changes += 1
            if any(block.get("type") in {"list_item", "heading"} for block in record.get("display_structure", []) if isinstance(block, dict)):
                list_or_paragraph_repairs += 1
        if isinstance(record.get("reconstruction"), dict) and record["reconstruction"].get("uncertain"):
            manual_review += 1

    after = relationship_snapshot(result)
    unchanged = before == after
    result.setdefault("reconstruction", {})
    result["reconstruction"].update({
        "migration_version": MIGRATION_VERSION,
        "reconstruction_version": RECONSTRUCTION_VERSION,
        "identity_normalization_version": IDENTITY_NORMALIZATION_VERSION,
        "search_normalization_version": SEARCH_NORMALIZATION_VERSION,
        "relationship_snapshot_sha256": hashlib.sha256(
            json.dumps(after, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "relationship_graph_unchanged": unchanged,
    })
    report = {
        "migration_version": MIGRATION_VERSION,
        "comments_seen": len([row for row in result.get("comments", []) if isinstance(row, dict)]),
        "responses_seen": len([row for row in result.get("responses", []) if isinstance(row, dict)]),
        "comments_updated": changed_comments,
        "responses_updated": changed_responses,
        "comments_skipped_already_current": skipped_comments,
        "responses_skipped_already_current": skipped_responses,
        "empty_records": skipped_empty,
        "relationship_graph_unchanged": unchanged,
        "before_relationship_sha256": _content_fingerprint(dataset),
        "after_relationship_sha256": _content_fingerprint(result),
        "candidate_identity_fields_added": changed_comments + changed_responses,
        "records_processed": changed_comments + changed_responses,
        "already_reconstructed": skipped_comments + skipped_responses,
        "local_deterministic_reconstruction": changed_comments + changed_responses,
        "gemini_visual_reconstruction": 0,
        "formatting_only_changes": formatting_only_changes,
        "metadata_separations": 0,
        "list_or_paragraph_repairs": list_or_paragraph_repairs,
        "correction_cycles_required": 0,
        "needs_manual_review": manual_review,
        "duplicate_candidates_discovered": 0,
    }
    if not unchanged:
        raise RuntimeError("Non-regression failure: reconstruction changed relationship fields")
    return result, report


def run(
    dataset_path: Path,
    output_path: Path,
    report_path: Path | None = None,
    checkpoint_path: Path | None = None,
    snapshot_path: Path | None = None,
) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    updated, report = backfill_dataset(dataset)
    _atomic_write(output_path, updated)
    if report_path:
        _atomic_write(report_path, report)
    if snapshot_path:
        _atomic_write(snapshot_path, {
            "snapshot_version": "reconstruction-non-regression-snapshot-v1",
            "migration_version": MIGRATION_VERSION,
            "before": relationship_snapshot(dataset),
            "after": relationship_snapshot(updated),
            "unchanged": report["relationship_graph_unchanged"],
        })
    if checkpoint_path:
        _atomic_write(checkpoint_path, {
            "checkpoint_version": "reconstruction-backfill-checkpoint-v1",
            "migration_version": MIGRATION_VERSION,
            "dataset_sha256": hashlib.sha256(
                dataset_path.read_bytes()
            ).hexdigest(),
            "output": str(output_path),
            "report": str(report_path) if report_path else "",
            "snapshot": str(snapshot_path) if snapshot_path else "",
            "stage": "representation_backfill",
            "status": "complete",
            "relationship_graph_unchanged": report["relationship_graph_unchanged"],
        })
    return report


def main() -> None:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--checkpoint", type=Path, help="write a completion checkpoint for safe resume/audit")
    parser.add_argument("--snapshot", type=Path, help="write the before/after relationship non-regression snapshot")
    parser.add_argument("--in-place", action="store_true", help="replace the dataset only after the non-regression check passes")
    args = parser.parse_args()
    output = args.dataset if args.in_place else (args.output or args.dataset.with_name(args.dataset.stem + ".reconstructed.json"))
    report = args.report or output.with_name(output.stem + ".report.json")
    checkpoint = args.checkpoint or output.with_name(output.stem + ".checkpoint.json")
    snapshot = args.snapshot or output.with_name(output.stem + ".snapshot.json")
    summary = run(args.dataset, output, report, checkpoint, snapshot)
    print(json.dumps({"output": str(output), "report": str(report), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
