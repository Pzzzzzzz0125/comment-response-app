#!/usr/bin/env python3
"""Persisted, incremental hybrid retrieval for permit-comment precedents."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable


INDEX_SCHEMA_VERSION = "1.0"
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
DEFAULT_EMBEDDING_VERSION = "permit-comment-v1"
DEFAULT_WEIGHTS = {
    "semantic": 0.55,
    "keyword": 0.20,
    "city": 0.10,
    "discipline": 0.05,
    "code": 0.05,
    "accepted": 0.05,
}

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for", "from",
    "has", "have", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "was", "were", "will", "with", "your", "you", "please", "provide", "show",
}


def search_tokens(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+(?:[.\-/][a-z0-9]+)*", (text or "").casefold())
        if len(token) > 1 and token not in STOP_WORDS
    ]


def code_sections(text: str) -> list[str]:
    patterns = re.findall(
        r"\b(?:CBC|CRC|CEC|CPC|CMC|CALGREEN|SMC)?\s*[A-Z]?\d+(?:\.\d+){1,5}(?:\([A-Za-z0-9]+\))*\b",
        text or "",
        flags=re.IGNORECASE,
    )
    return sorted({re.sub(r"\s+", " ", value).strip().upper() for value in patterns})


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def normalize_analysis(value: Any, query: str) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    actions = raw.get("requested_actions", [])
    terms = raw.get("technical_terms", [])
    codes = raw.get("code_sections", [])
    secondary = raw.get("secondary_subjects", [])
    required = raw.get("required_concepts", [])
    optional = raw.get("optional_concepts", [])
    excluded = raw.get("excluded_concepts", [])
    result = {
        "search_goal": str(raw.get("search_goal", "")).strip(),
        "city": str(raw.get("city") or "").strip(),
        "discipline": str(raw.get("discipline") or "").strip(),
        "primary_subject": str(raw.get("primary_subject", raw.get("subject", ""))).strip(),
        "secondary_subjects": [str(item).strip() for item in secondary if str(item).strip()] if isinstance(secondary, list) else [],
        "condition_or_problem": str(raw.get("condition_or_problem", "")).strip(),
        "regulatory_concern": str(raw.get("regulatory_concern", raw.get("requirement", ""))).strip(),
        "requested_actions": [str(item).strip() for item in actions if str(item).strip()] if isinstance(actions, list) else [],
        "issue_type": str(raw.get("issue_type", "")).strip(),
        "code_sections": [str(item).strip().upper() for item in codes if str(item).strip()] if isinstance(codes, list) else code_sections(query),
        "technical_terms": [str(item).strip() for item in terms if str(item).strip()] if isinstance(terms, list) else [],
        "required_concepts": [str(item).strip() for item in required if str(item).strip()] if isinstance(required, list) else [],
        "optional_concepts": [str(item).strip() for item in optional if str(item).strip()] if isinstance(optional, list) else [],
        "excluded_concepts": [str(item).strip() for item in excluded if str(item).strip()] if isinstance(excluded, list) else [],
        "direct_match_definition": str(raw.get("direct_match_definition", "")).strip(),
        "related_match_definition": str(raw.get("related_match_definition", "")).strip(),
        "ambiguities": [str(item).strip() for item in raw.get("ambiguities", []) if str(item).strip()] if isinstance(raw.get("ambiguities", []), list) else [],
        "semantic_query": str(raw.get("semantic_query", "")).strip() or query.strip(),
    }
    # Compatibility aliases used only inside deterministic retrieval.
    result["subject"] = result["primary_subject"]
    result["requirement"] = result["regulatory_concern"]
    result["category"] = str(raw.get("category", "")).strip()
    return result


def coherent_units(text: str) -> list[str]:
    """Split clear top-level numbered comments while retaining heading context."""
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return []
    marker = re.compile(
        r"(?m)(?=^\s*(?:(?:PC|ITEM|COMMENT|CORRECTION)\s*)?\d{1,3}\s*[:.)-]\s+)",
        flags=re.IGNORECASE,
    )
    starts = [match.start() for match in marker.finditer(value)]
    if len(starts) < 2:
        return [value]
    prefix = value[:starts[0]].strip()
    parts = [value[starts[index]: starts[index + 1] if index + 1 < len(starts) else None].strip() for index in range(len(starts))]
    parts = [part for part in parts if len(part) >= 20]
    if prefix and parts:
        parts = [f"{prefix}\n{part}" for part in parts]
    return parts or [value]


def truncation_reason(text: str) -> str:
    value = (text or "").strip()
    if value.endswith(("...", "…")):
        return "ends_with_ellipsis"
    if re.search(r"\b(?:and|or|the|to|of|for|with|per|see|refer to)$", value, flags=re.IGNORECASE):
        return "ends_mid_phrase"
    return ""


def searchable_text(comment: dict[str, Any], display_text: str = "") -> str:
    """Stable text embedded per comment; deliberately excludes the long response."""
    text = display_text or str(comment.get("original_text", ""))
    fields = [
        f"City: {comment.get('city', '')}",
        f"Discipline: {comment.get('discipline', '')}",
        f"Comment: {text}",
    ]
    codes = code_sections(text)
    if codes:
        fields.append("Code sections: " + ", ".join(codes))
    return "\n".join(fields)


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SearchIndex:
    def __init__(
        self,
        path: Path,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_version: str = DEFAULT_EMBEDDING_VERSION,
        weights: dict[str, float] | None = None,
    ):
        self.path = path.resolve()
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("records", {})
            self.records = records if isinstance(records, dict) else {}
        except (OSError, json.JSONDecodeError):
            self.records = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "updated_at": int(time.time()),
            "records": dict(sorted(self.records.items())),
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent,
            prefix="search-index-", suffix=".tmp", delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.path)

    def sync(
        self,
        comments: list[dict[str, Any]],
        display_text: Callable[[dict[str, Any]], str],
        category: Callable[[str], str],
        accepted: Callable[[str], bool],
        embed_documents: Callable[[list[str]], list[list[float]]] | None = None,
        batch_size: int = 20,
    ) -> dict[str, int]:
        desired: dict[str, dict[str, Any]] = {}
        pending: list[tuple[str, str]] = []
        for comment in comments:
            comment_id = str(comment["comment_id"])
            text = searchable_text(comment, display_text(comment))
            digest = input_hash(text)
            previous = self.records.get(comment_id, {})
            embedding_current = (
                previous.get("embedding_input_hash") == digest
                and previous.get("embedding_model") == self.embedding_model
                and previous.get("embedding_version") == self.embedding_version
                and isinstance(previous.get("embedding"), list)
                and bool(previous.get("embedding"))
            )
            record = {
                "comment_id": comment_id,
                "city": str(comment.get("city", "")),
                "discipline": str(comment.get("discipline", "")),
                "category": category(comment_id),
                "property_project": str(comment.get("property_project", "")),
                "review_round": str(comment.get("review_round", "")),
                "comment": display_text(comment),
                "code_sections": code_sections(text),
                "accepted": bool(accepted(comment_id)),
                "searchable_text": text,
                "search_units": [],
                "data_quality_flags": ([truncation_reason(str(comment.get("original_text", "")))] if truncation_reason(str(comment.get("original_text", ""))) else []),
                "embedding_input_hash": digest,
                "embedding_model": self.embedding_model,
                "embedding_version": self.embedding_version,
                "embedding": previous.get("embedding", []) if embedding_current else [],
                "embedded_at": previous.get("embedded_at") if embedding_current else None,
            }
            units = coherent_units(display_text(comment))
            previous_units = {str(item.get("unit_id")): item for item in previous.get("search_units", []) if isinstance(item, dict)}
            for position, unit_text in enumerate(units, 1):
                unit_id = f"{comment_id}:{position}"
                unit_input = "\n".join([f"City: {comment.get('city', '')}", f"Discipline: {comment.get('discipline', '')}", f"Comment: {unit_text}"])
                unit_digest = input_hash(unit_input)
                old_unit = previous_units.get(unit_id, {})
                unit_current = (
                    old_unit.get("embedding_input_hash") == unit_digest
                    and old_unit.get("embedding_model") == self.embedding_model
                    and old_unit.get("embedding_version") == self.embedding_version
                    and isinstance(old_unit.get("embedding"), list) and bool(old_unit.get("embedding"))
                )
                record["search_units"].append({
                    "unit_id": unit_id, "text": unit_text, "searchable_text": unit_input,
                    "embedding_input_hash": unit_digest, "embedding_model": self.embedding_model,
                    "embedding_version": self.embedding_version,
                    "embedding": old_unit.get("embedding", []) if unit_current else [],
                    "embedded_at": old_unit.get("embedded_at") if unit_current else None,
                })
                if not unit_current and embed_documents:
                    pending.append((f"{comment_id}\0{unit_id}", unit_input))
            desired[comment_id] = record

        embedded = 0
        for offset in range(0, len(pending), max(1, batch_size)):
            batch = pending[offset: offset + max(1, batch_size)]
            vectors = embed_documents([text for _, text in batch])
            if len(vectors) != len(batch):
                raise RuntimeError("Embedding API returned the wrong number of vectors")
            now = int(time.time())
            for (compound_id, _), vector in zip(batch, vectors):
                comment_id, unit_id = compound_id.split("\0", 1)
                unit = next(item for item in desired[comment_id]["search_units"] if item["unit_id"] == unit_id)
                unit["embedding"] = [float(value) for value in vector]
                unit["embedded_at"] = now
                embedded += 1

        changed = desired != self.records
        removed = len(set(self.records) - set(desired))
        self.records = desired
        if changed:
            self._save()
        return {"records": len(desired), "embedded": embedded, "removed": removed}

    def retrieve(
        self,
        query: str,
        analysis: dict[str, Any],
        city: str,
        query_embedding: list[float] | None = None,
        discipline: str = "",
        category: str = "",
        vector_limit: int = 30,
        keyword_limit: int = 30,
        candidate_limit: int = 20,
    ) -> list[dict[str, Any]]:
        normalized = normalize_analysis(analysis, query)
        effective_city = city.strip() or normalized["city"]
        effective_discipline = discipline.strip() or normalized["discipline"]
        effective_category = category.strip() or normalized["category"]
        query_text = " ".join([
            normalized["semantic_query"], normalized["subject"], normalized["requirement"],
            *normalized["secondary_subjects"], *normalized["requested_actions"],
            *normalized["technical_terms"], *normalized["required_concepts"], *normalized["optional_concepts"],
        ])
        query_counts = Counter(search_tokens(query_text))
        query_codes = set(normalized["code_sections"] or code_sections(query))
        rows: list[dict[str, Any]] = []
        for record in self.records.values():
            if effective_city and record.get("city", "").casefold() != effective_city.casefold():
                continue
            if discipline and record.get("discipline", "").casefold() != discipline.casefold():
                continue
            if category and record.get("category", "").casefold() != category.casefold():
                continue
            best_unit = None
            keyword = semantic = 0.0
            phrase = re.sub(r"\s+", " ", query.casefold()).strip()
            for unit in record.get("search_units", []) or [{"unit_id": f"{record['comment_id']}:1", "text": record.get("comment", ""), "searchable_text": record.get("searchable_text", ""), "embedding": record.get("embedding", [])}]:
                record_counts = Counter(search_tokens(str(unit.get("searchable_text", ""))))
                shared = sum(min(count, record_counts[token]) for token, count in query_counts.items())
                unit_keyword = shared / max(1, sum(query_counts.values()))
                if phrase and phrase in str(unit.get("text", "")).casefold():
                    unit_keyword = min(1.0, unit_keyword + 0.35)
                unit_semantic = cosine(query_embedding or [], unit.get("embedding", []))
                if not query_embedding or not unit.get("embedding"):
                    union = set(query_counts) | set(record_counts)
                    unit_semantic = len(set(query_counts) & set(record_counts)) / max(1, len(union))
                if unit_keyword + unit_semantic > keyword + semantic:
                    keyword, semantic, best_unit = unit_keyword, unit_semantic, unit
            city_score = 1.0 if effective_city and record.get("city", "").casefold() == effective_city.casefold() else 0.0
            discipline_score = 1.0 if effective_discipline and record.get("discipline", "").casefold() == effective_discipline.casefold() else 0.0
            category_score = 1.0 if effective_category and record.get("category", "").casefold() == effective_category.casefold() else 0.0
            code_score = 1.0 if query_codes & set(record.get("code_sections", [])) else 0.0
            score = (
                self.weights["semantic"] * max(0.0, semantic)
                + self.weights["keyword"] * keyword
                + self.weights["city"] * city_score
                + self.weights["discipline"] * max(discipline_score, category_score)
                + self.weights["code"] * code_score
                + self.weights["accepted"] * (1.0 if record.get("accepted") else 0.0)
            )
            if keyword or semantic or code_score:
                rows.append({
                    "comment_id": record["comment_id"],
                    "score": round(min(1.0, score), 4),
                    "keyword_score": keyword,
                    "semantic_score": semantic,
                    "matched_unit_id": (best_unit or {}).get("unit_id", ""),
                    "matched_excerpt": str((best_unit or {}).get("text", record.get("comment", "")))[:1200],
                    "record": record,
                })
        vector_ids = {row["comment_id"] for row in sorted(rows, key=lambda item: -item["semantic_score"])[:vector_limit]}
        keyword_ids = {row["comment_id"] for row in sorted(rows, key=lambda item: -item["keyword_score"])[:keyword_limit]}
        merged = [row for row in rows if row["comment_id"] in vector_ids | keyword_ids]
        merged.sort(key=lambda item: (-item["score"], item["comment_id"]))
        return merged[:candidate_limit]
