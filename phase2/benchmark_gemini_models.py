#!/usr/bin/env python3
"""Benchmark Gemini visual extraction models against confirmed response rows."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

try:
    from phase2.visual_ingestion import (
        VisualGeminiClient,
        VisualIngestionPipeline,
        atomic_json,
    )
    from web_app.local_secrets import gemini_api_key
except ModuleNotFoundError:
    from visual_ingestion import (  # type: ignore
        VisualGeminiClient,
        VisualIngestionPipeline,
        atomic_json,
    )
    from web_app.local_secrets import gemini_api_key


DEFAULT_SOURCE = (
    "comments&response/25-001-2311_warner_range_ave_menlopark/building/"
    "3rd submission/2nd Round Submission Package/"
    "PC3- 2311 Warner Range Ave Response letter.pdf"
)


def normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def oracle_groups(dataset: dict[str, Any], filename: str) -> dict[str, dict[str, Any]]:
    comments = {str(row.get("comment_id", "")): row for row in dataset.get("comments", [])}
    responses = {str(row.get("response_id", "")): row for row in dataset.get("responses", [])}
    groups: dict[str, dict[str, Any]] = {}
    for link in dataset.get("comment_response_links", []):
        if link.get("provenance") != "document_structure_rematch":
            continue
        if Path(str(link.get("source_pdf", ""))).name.casefold() != filename.casefold():
            continue
        comment = comments.get(str(link.get("comment_id", "")), {})
        text = (
            link.get("imported_current_round_comment_text")
            or comment.get("verified_text")
            or comment.get("original_text")
            or ""
        )
        key = normalized(text)
        if not key:
            continue
        group = groups.setdefault(key, {
            "comment_text": str(text),
            "comment_ids": [],
            "responses": set(),
        })
        group["comment_ids"].append(str(link.get("city_comment_id", "")))
        response = responses.get(str(link.get("response_id", "")), {})
        response_text = normalized(response.get("verified_text") or response.get("original_text"))
        if response_text:
            group["responses"].add(response_text)
    return groups


def score(
    groups: dict[str, dict[str, Any]],
    extraction: dict[str, Any],
    produced_comments: list[dict[str, Any]],
    elapsed_seconds: float,
) -> dict[str, Any]:
    actual_records = [
        row for row in extraction.get("records", [])
        if isinstance(row, dict) and normalized(row.get("exact_comment_text"))
    ]
    actual_groups: dict[str, list[dict[str, Any]]] = {}
    for row in actual_records:
        actual_groups.setdefault(normalized(row.get("exact_comment_text")), []).append(row)
    actual_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in actual_records:
        printed_id = str(row.get("comment_id") or row.get("comment_number") or "").strip()
        if printed_id:
            actual_by_id.setdefault(printed_id, []).append(row)
    expected_keys = set(groups)
    actual_keys = set(actual_groups)
    matched_keys = expected_keys & actual_keys
    exact_response_matches = 0
    for key in matched_keys:
        oracle_responses = groups[key]["responses"]
        if any(normalized(row.get("exact_response_text")) in oracle_responses for row in actual_groups[key]):
            exact_response_matches += 1
    verified_unique = {
        normalized(row.get("original_text"))
        for row in produced_comments
        if row.get("text_trust_status") == "verified" and normalized(row.get("original_text"))
    }
    id_matched_groups = []
    verbatim_by_id = 0
    response_by_id = 0
    for key, group in groups.items():
        rows = [
            row
            for comment_id in group["comment_ids"]
            for row in actual_by_id.get(comment_id, [])
        ]
        if not rows:
            continue
        id_matched_groups.append(key)
        if any(normalized(row.get("exact_comment_text")) == key for row in rows):
            verbatim_by_id += 1
        if any(normalized(row.get("exact_response_text")) in group["responses"] for row in rows):
            response_by_id += 1
    expected = len(expected_keys)
    matched = len(matched_keys)
    id_matched = len(id_matched_groups)
    return {
        "expected_confirmed_rows_before_dedup": sum(len(value["comment_ids"]) for value in groups.values()),
        "expected_unique_comments_same_site_round": expected,
        "raw_records_extracted": len(actual_records),
        "unique_comments_extracted": len(actual_keys),
        "duplicate_records_extracted": len(actual_records) - len(actual_keys),
        "exact_comment_matches": matched,
        "exact_comment_recall": round(matched / expected, 4) if expected else 0.0,
        "comment_id_matches": id_matched,
        "comment_id_recall": round(id_matched / expected, 4) if expected else 0.0,
        "verbatim_comment_matches_by_id": verbatim_by_id,
        "verbatim_comment_accuracy_on_id_matches": (
            round(verbatim_by_id / id_matched, 4) if id_matched else 0.0
        ),
        "exact_response_matches_by_id": response_by_id,
        "exact_response_accuracy_on_id_matches": (
            round(response_by_id / id_matched, 4) if id_matched else 0.0
        ),
        "exact_response_matches": exact_response_matches,
        "exact_pair_accuracy_on_matched_comments": (
            round(exact_response_matches / matched, 4) if matched else 0.0
        ),
        "unexpected_unique_comments": len(actual_keys - expected_keys),
        "missing_unique_comments": len(expected_keys - actual_keys),
        "two_pass_verified_unique_comments": len(verified_unique),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "unique_comments_per_minute": (
            round(len(actual_keys) * 60 / elapsed_seconds, 2) if elapsed_seconds else 0.0
        ),
    }


def benchmark_model(
    workspace: Path,
    source: Path,
    source_relative: str,
    dataset_path: Path,
    artifact_root: Path,
    model: str,
    api_key: str,
    timeout: int,
    batch_pages: int,
    batch_overlap: int,
    force: bool,
) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    groups = oracle_groups(dataset, source.name)
    model_artifacts = artifact_root / re.sub(r"[^A-Za-z0-9_.-]+", "-", model)
    pipeline = VisualIngestionPipeline(
        VisualGeminiClient(api_key, model, timeout=timeout),
        model_artifacts,
        dataset_path,
        dpi=220,
        batch_pages=batch_pages,
        batch_overlap=batch_overlap,
    )
    started = time.monotonic()
    comments, _responses, _links, _summary, _review = pipeline.process(
        source,
        source_relative,
        {
            "city_hint": "Menlo Park",
            "property_hint": "2311 Warner Range Ave",
            "review_round_hint": "2",
            "audit_document_type_hint": "company_response",
            "benchmark": True,
        },
        force=force,
    )
    elapsed = time.monotonic() - started
    digest = next(model_artifacts.iterdir())
    extraction = json.loads((digest / "gemini_extraction.json").read_text(encoding="utf-8"))
    metrics = score(groups, extraction, comments, elapsed)
    return {
        "model": model,
        "source": source_relative,
        "batch_pages": batch_pages,
        "batch_overlap": batch_overlap,
        "metrics": metrics,
        "artifact_directory": str(digest),
    }


def markdown_report(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Gemini extraction benchmark",
        "",
        "| Model | ID recall | Verbatim accuracy by ID | Response accuracy by ID | Exact comment recall | Time | Unique/min |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        metrics = result["metrics"]
        lines.append(
            f"| {result['model']} | {metrics['comment_id_recall']:.1%} "
            f"({metrics['comment_id_matches']}/{metrics['expected_unique_comments_same_site_round']}) "
            f"| {metrics['verbatim_comment_accuracy_on_id_matches']:.1%} "
            f"| {metrics['exact_response_accuracy_on_id_matches']:.1%} "
            f"| {metrics['exact_comment_recall']:.1%} "
            f"| {metrics['elapsed_seconds']:.2f}s "
            f"| {metrics['unique_comments_per_minute']:.2f} |"
        )
    lines.extend([
        "",
        "Duplicate rule: exact normalized comment text counts once within the same site and review round; "
        "different sites or rounds count separately.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--source", type=Path, default=workspace / DEFAULT_SOURCE)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--artifact-root", type=Path, default=workspace / "phase2_dataset" / "model_benchmarks")
    parser.add_argument("--output", type=Path, default=workspace / "phase2_dataset" / "model_benchmark_results.json")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--batch-pages", type=int, default=2)
    parser.add_argument("--batch-overlap", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--rescore-existing", action="store_true")
    parser.add_argument("--gemini-api-key-stdin", action="store_true")
    args = parser.parse_args()
    models = args.model or ["gemini-3.6-flash", "gemini-3.1-flash-lite"]
    report_path = args.output.resolve().with_suffix(".md")
    if args.rescore_existing:
        payload = json.loads(args.output.resolve().read_text(encoding="utf-8"))
        dataset = json.loads(args.dataset.resolve().read_text(encoding="utf-8"))
        for result in payload.get("results", []):
            if "metrics" not in result:
                continue
            groups = oracle_groups(dataset, Path(str(result["source"])).name)
            extraction = json.loads(
                (Path(result["artifact_directory"]) / "gemini_extraction.json").read_text(encoding="utf-8")
            )
            old_metrics = result["metrics"]
            metrics = score(groups, extraction, [], float(old_metrics["elapsed_seconds"]))
            metrics["two_pass_verified_unique_comments"] = old_metrics["two_pass_verified_unique_comments"]
            result["metrics"] = metrics
        atomic_json(args.output.resolve(), payload)
        successful = [row for row in payload["results"] if "metrics" in row]
        report_path.write_text(markdown_report(successful), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    api_key = gemini_api_key()
    if not api_key and args.gemini_api_key_stdin:
        api_key = getpass.getpass("Gemini API key: ")
    if not api_key:
        raise ValueError("Benchmark requires GEMINI_API_KEY or --gemini-api-key-stdin")
    workspace = workspace.resolve()
    source = args.source.resolve()
    source_relative = source.relative_to(workspace).as_posix()
    results = []
    payload = {
        "benchmark_version": "gemini-visual-ab-v1",
        "duplicate_rule": "same normalized comment + same site + same review round counts once",
        "results": results,
    }
    for model in models:
        started = time.monotonic()
        try:
            result = benchmark_model(
                workspace, source, source_relative, args.dataset.resolve(), args.artifact_root.resolve(),
                model, api_key, args.timeout, args.batch_pages, args.batch_overlap, args.force,
            )
        except Exception as exc:  # retain a completed model if the other model fails
            result = {
                "model": model,
                "source": source_relative,
                "error": str(exc),
                "elapsed_seconds_before_failure": round(time.monotonic() - started, 2),
            }
        results.append(result)
        atomic_json(args.output.resolve(), payload)
        successful = [row for row in results if "metrics" in row]
        report_path.write_text(markdown_report(successful), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
