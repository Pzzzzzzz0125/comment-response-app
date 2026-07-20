#!/usr/bin/env python3
"""Build or incrementally refresh Gemini embeddings for Smart Search."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path

try:
    from .gemini_enrich import GeminiClient, record_digest
    from .rag_search import SearchIndex
    from .server import readable_text
except ImportError:
    from gemini_enrich import GeminiClient, record_digest
    from rag_search import SearchIndex
    from server import readable_text


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--categories", type=Path, default=workspace / "web_app" / "data" / "category_assignments.json")
    parser.add_argument("--enrichment", type=Path, default=workspace / "web_app" / "data" / "gemini_enrichment.json")
    parser.add_argument("--index", type=Path, default=workspace / "web_app" / "data" / "search_index.json")
    parser.add_argument("--gemini-model", default=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"))
    parser.add_argument("--metadata-only", action="store_true", help="Refresh records without calling the embedding API")
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    comments = dataset.get("comments", [])
    links = {item["comment_id"]: item for item in dataset.get("comment_response_links", [])}
    categories = json.loads(args.categories.read_text(encoding="utf-8")).get("assignments", {}) if args.categories.is_file() else {}
    enrichments = json.loads(args.enrichment.read_text(encoding="utf-8")).get("entries", {}) if args.enrichment.is_file() else {}

    api_key = ""
    client = None
    if not args.metadata_only:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or getpass.getpass("Gemini API key: ")
        client = GeminiClient(api_key, args.gemini_model)

    def display(comment: dict) -> str:
        entry = enrichments.get(str(comment["comment_id"]), {})
        return str(entry.get("display_text", "")) if entry.get("input_sha256") == record_digest(comment) else readable_text(str(comment.get("original_text", "")))

    index = SearchIndex(args.index)
    stats = index.sync(
        comments,
        display,
        lambda comment_id: str(categories.get(comment_id, "Uncategorized")),
        lambda comment_id: links.get(comment_id, {}).get("review_status") == "confirmed",
        client.embed_documents if client else None,
    )
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
