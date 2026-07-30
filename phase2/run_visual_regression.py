#!/usr/bin/env python3
"""Run full visual Gemini ingestion against confirmed PC3/PC4/PC5 references."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path

from phase2.visual_ingestion import VisualGeminiClient, VisualIngestionPipeline, sha256_file
from web_app.local_secrets import gemini_api_key


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--source-root", type=Path, default=workspace / "comments&response")
    parser.add_argument("--artifacts", type=Path, default=workspace / "phase2_dataset" / "visual_regression_artifacts")
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"))
    parser.add_argument("--api-key-stdin", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    api_key = gemini_api_key()
    if not api_key and args.api_key_stdin:
        api_key = getpass.getpass("Gemini API key: ")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY or --api-key-stdin is required")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    links = [row for row in dataset.get("comment_response_links", []) if row.get("provenance") == "document_structure_rematch"]
    source_paths = sorted({str(row["source_pdf"]) for row in links})
    pipeline = VisualIngestionPipeline(VisualGeminiClient(api_key, args.model), args.artifacts, args.dataset)
    reports = []
    for relative in source_paths:
        path = workspace / relative
        sample = next(row for row in links if row["source_pdf"] == relative)
        pipeline.process(path, relative, {
            "property_hint": "25 001 2311 Warner Range Ave — Building",
            "city_hint": "Menlo Park", "review_round_hint": str(sample.get("reviewed_plan_round", "")),
            "audit_document_type_hint": "combined comment response form",
        }, force=args.force)
        # Artifact IDs derive from the current file, not mutable dataset metadata.
        artifact_id = "VI-" + sha256_file(path)[:20]
        regression = json.loads((args.artifacts / artifact_id / "confirmed_reference_regression.json").read_text(encoding="utf-8"))
        reports.append({"source_file": path.name, **regression})
        print(json.dumps(reports[-1], ensure_ascii=False))
    summary = {
        "passed": all(row.get("passed") for row in reports), "documents": len(reports),
        "expected_pairs": sum(int(row.get("expected", 0)) for row in reports),
        "actual_pairs": sum(int(row.get("actual", 0)) for row in reports), "results": reports,
    }
    (args.artifacts / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
