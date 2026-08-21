#!/usr/bin/env python3
"""Verify that the tracked deployment bundle contains required runtime data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "phase2_dataset" / "dataset.json"
REQUIRED_APP_DATA = (
    "category_assignments.json",
    "gemini_enrichment.json",
    "link_review_decisions.json",
    "search_index.json",
    "source_registry.json",
    "tag_suggestions.json",
    "workbook_review_decisions.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--app-data",
        type=Path,
        default=ROOT / "web_app" / "data",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    dataset_path = args.dataset.resolve()
    app_data = args.app_data.resolve()

    if not dataset_path.is_file():
        errors.append(f"Missing dataset: {dataset_path}")
        payload: dict[str, object] = {}
    else:
        try:
            payload = json.loads(dataset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Unreadable dataset {dataset_path}: {exc}")
            payload = {}

    evidence_model = dataset_path.parent / "evidence_model.json"
    if not evidence_model.is_file():
        errors.append(f"Missing evidence model: {evidence_model}")

    for filename in REQUIRED_APP_DATA:
        path = app_data / filename
        if not path.is_file():
            errors.append(f"Missing app data: {path}")

    workbook_artifacts: dict[str, set[str]] = {}
    comments = payload.get("comments", []) if isinstance(payload, dict) else []
    for row in comments if isinstance(comments, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("extraction_method") != "local_structured_spreadsheet":
            continue
        audit = row.get("ingestion_audit")
        artifact_id = str(
            audit.get("artifact_id", "") if isinstance(audit, dict) else ""
        ).strip()
        source = str(row.get("source_document", "")).strip()
        if artifact_id:
            workbook_artifacts.setdefault(artifact_id, set()).add(source)
        else:
            errors.append(
                "Structured workbook row has no ingestion artifact: "
                f"{row.get('comment_id', '<unknown>')}"
            )

    manifests_root = dataset_path.parent / "ingestion_artifacts"
    checked = 0
    for artifact_id, sources in sorted(workbook_artifacts.items()):
        manifest_path = manifests_root / artifact_id / "completeness_manifest.json"
        if not manifest_path.is_file():
            errors.append(
                f"Missing workbook manifest: {manifest_path} "
                f"({', '.join(sorted(sources))})"
            )
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Unreadable workbook manifest {manifest_path}: {exc}")
            continue
        if manifest.get("completion_status") != "complete":
            errors.append(f"Incomplete workbook manifest: {manifest_path}")
        checked += 1

    if errors:
        print("Deployment bundle check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "Deployment bundle is complete: "
        f"dataset, evidence model, {len(REQUIRED_APP_DATA)} app-data files, "
        f"and {checked} workbook completeness manifests."
    )
    print(
        "Original source documents are intentionally external and must be "
        "mounted separately or served through the planned Lark integration."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
