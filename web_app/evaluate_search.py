#!/usr/bin/env python3
"""Evaluate deterministic hybrid retrieval against a versioned fixture."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from collections import Counter
from pathlib import Path

try:
    from .rag_search import SearchIndex, normalize_analysis, search_tokens
except ImportError:
    from rag_search import SearchIndex, normalize_analysis, search_tokens


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))]


def legacy_search(query: str, filters: dict, comments: list[dict], limit: int = 10) -> list[str]:
    """The pre-RAG token-cosine implementation, retained only for comparison."""
    candidates = [row for row in comments if (not filters.get("city") or row.get("city") == filters.get("city")) and (not filters.get("discipline") or row.get("discipline") == filters.get("discipline"))]
    query_tokens = search_tokens(query)
    tokenized = [search_tokens(str(row.get("original_text", ""))) for row in candidates]
    frequency = Counter(token for tokens in tokenized for token in set(tokens))
    count = len(candidates)
    idf = {token: math.log((count + 1) / (frequency[token] + 0.5)) + 1 for token in set(query_tokens)}
    query_counts = Counter(query_tokens)
    query_vector = {token: value * idf[token] for token, value in query_counts.items()}
    query_norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1
    phrase = re.sub(r"\s+", " ", query.casefold()).strip()
    results = []
    for row, tokens in zip(candidates, tokenized):
        counts = Counter(token for token in tokens if token in idf)
        if not counts:
            continue
        vector = {token: value * idf[token] for token, value in counts.items()}
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1
        score = sum(query_vector.get(token, 0) * value for token, value in vector.items()) / (query_norm * norm)
        if phrase and phrase in str(row.get("original_text", "")).casefold():
            score += 0.35
        results.append((score, str(row.get("comment_id", ""))))
    return [comment_id for _, comment_id in sorted(results, key=lambda item: (-item[0], item[1]))[:limit]]


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=workspace / "web_app" / "data" / "search_index.json")
    parser.add_argument("--fixture", type=Path, default=workspace / "web_app" / "data" / "search_eval_v1.json")
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--source-registry", type=Path, default=workspace / "web_app" / "data" / "source_registry.json")
    args = parser.parse_args()
    index = SearchIndex(args.index)
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    responses_by_comment = {str(row.get("comment_id", "")): str(row.get("response_id", "")) for row in dataset.get("comments", [])}
    registry = json.loads(args.source_registry.read_text(encoding="utf-8"))
    cited_owner_ids = {str(source.get("owner_id", "")) for source in registry.get("sources", {}).values()}
    reciprocal_ranks: list[float] = []
    recalls = {1: 0, 5: 0, 10: 0}
    latencies: list[float] = []
    citation_integrity = 0
    precision_at_5: list[float] = []
    false_positives = false_positive_opportunities = 0
    false_negatives = expected_direct_total = 0
    no_result_correct = no_result_total = 0
    response_links_correct = response_links_checked = 0
    relevant_case_count = 0
    baseline = {"p5": [], "r5": 0, "r10": 0, "rr": [], "no_result_correct": 0}
    details = []
    cases = fixture.get("cases", [])
    for case in cases:
        filters = case.get("filters", {})
        started = time.perf_counter()
        rows = index.retrieve(
            str(case.get("query", "")), normalize_analysis({}, str(case.get("query", ""))),
            str(filters.get("city", "")), discipline=str(filters.get("discipline", "")),
            category=str(filters.get("category", "")), candidate_limit=10,
        )
        latencies.append((time.perf_counter() - started) * 1000)
        rows = [row for row in rows if row.get("keyword_score", 0) >= 0.22 or row.get("semantic_score", 0) >= 0.34]
        ids = [row["comment_id"] for row in rows]
        expected = set(case.get("expected_direct_ids", case.get("expected_relevant_comment_ids", [])))
        related = set(case.get("acceptable_related_ids", []))
        negatives = set(case.get("known_negative_ids", []))
        ranks = [position for position, comment_id in enumerate(ids, 1) if comment_id in expected]
        rank = min(ranks) if ranks else 0
        if expected:
            relevant_case_count += 1
            reciprocal_ranks.append(1 / rank if rank else 0.0)
            for cutoff in recalls:
                recalls[cutoff] += int(bool(expected & set(ids[:cutoff])))
        top_five = ids[:5]
        precision_at_5.append(1.0 if case.get("expected_no_result") and not top_five else len((expected | related) & set(top_five)) / max(1, len(top_five)))
        false_positives += len(negatives & set(top_five))
        false_positive_opportunities += len(negatives)
        false_negatives += len(expected - set(ids[:10]))
        expected_direct_total += len(expected)
        expected_no_result = bool(case.get("expected_no_result"))
        baseline_ids = legacy_search(str(case.get("query", "")), filters, dataset.get("comments", []), 10)
        if expected:
            baseline["r5"] += int(bool(expected & set(baseline_ids[:5])))
            baseline["r10"] += int(bool(expected & set(baseline_ids[:10])))
            baseline_ranks = [position for position, value in enumerate(baseline_ids, 1) if value in expected]
            baseline["rr"].append(1 / min(baseline_ranks) if baseline_ranks else 0.0)
        baseline["p5"].append(1.0 if expected_no_result and not baseline_ids else len((expected | related) & set(baseline_ids[:5])) / max(1, len(baseline_ids[:5])))
        baseline["no_result_correct"] += int(expected_no_result and not baseline_ids)
        if expected_no_result:
            no_result_total += 1
            no_result_correct += int(not ids)
        citation_integrity += int(all(comment_id in cited_owner_ids for comment_id in top_five))
        expected_response_id = str(case.get("expected_response_id", ""))
        if expected and expected_response_id:
            response_links_checked += 1
            response_links_correct += int(any(responses_by_comment.get(comment_id) == expected_response_id for comment_id in expected))
        details.append({"query": case.get("query"), "first_direct_rank": rank, "result_ids": top_five, "expected_no_result": expected_no_result})
    count = max(1, len(cases))
    report = {
        "fixture_schema_version": fixture.get("schema_version"),
        "cases": len(cases),
        "fixture_review_status": fixture.get("review_status", "unknown"),
        "precision_at_5": round(statistics.fmean(precision_at_5), 4) if precision_at_5 else 0.0,
        "recall_at_1": round(recalls[1] / max(1, relevant_case_count), 4),
        "recall_at_5": round(recalls[5] / max(1, relevant_case_count), 4),
        "recall_at_10": round(recalls[10] / max(1, relevant_case_count), 4),
        "mean_reciprocal_rank": round(statistics.fmean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        "citation_integrity": round(citation_integrity / count, 4),
        "false_positive_rate": round(false_positives / max(1, false_positive_opportunities), 4),
        "false_negative_rate": round(false_negatives / max(1, expected_direct_total), 4),
        "no_result_accuracy": round(no_result_correct / max(1, no_result_total), 4),
        "correct_response_link_rate": round(response_links_correct / response_links_checked, 4) if response_links_checked else None,
        "direct_related_classification_accuracy": None,
        "legacy_baseline": {
            "precision_at_5": round(statistics.fmean(baseline["p5"]), 4),
            "recall_at_5": round(baseline["r5"] / max(1, relevant_case_count), 4),
            "recall_at_10": round(baseline["r10"] / max(1, relevant_case_count), 4),
            "mean_reciprocal_rank": round(statistics.fmean(baseline["rr"]), 4),
            "no_result_accuracy": round(baseline["no_result_correct"] / max(1, no_result_total), 4),
        },
        "latency_ms": {"p50": round(percentile(latencies, 0.5), 2), "p95": round(percentile(latencies, 0.95), 2)},
        "gemini_failure_rate": None,
        "note": "This provisional fixture evaluates deterministic retrieval and stored evidence associations. Direct/related classification and Gemini failure rate require a credited live Gemini evaluation plus domain review.",
        "details": details,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
