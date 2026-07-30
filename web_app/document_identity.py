"""Canonicalize physical source files before calculating topic frequency.

The ingestion dataset historically stored one row per extraction and one path
per physical file.  This module keeps those rows for audit, but gives every
file and extracted comment a stable logical identity.  Topic statistics can
therefore ignore renamed, re-exported, archived, and copied documents without
throwing away the original evidence.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


def _first_path(value: Any) -> str:
    return str(value or "").split(" | ", 1)[0].strip()


def source_file_id(path: str) -> str:
    return "SF-" + hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()[:20]


def normalize_document_text(value: Any) -> str:
    """Normalize visible substantive text while retaining numbers and codes."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("_x000D_", " ").replace("_x000A_", " ")
    text = text.casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def _record_text(record: dict[str, Any]) -> str:
    return normalize_document_text(
        record.get("verified_text") or record.get("original_text") or ""
    )


def _is_audit_child(record: dict[str, Any]) -> bool:
    return str(record.get("duplicate_status", "")) == "hierarchical_subpoint"


def _site_key(record: dict[str, Any]) -> str:
    source = _first_path(record.get("source_document"))
    parts = Path(source).as_posix().split("/")
    city = normalize_document_text(record.get("city"))
    if "comments&response" in parts:
        index = parts.index("comments&response") + 1
        if index < len(parts):
            return f"{city}|{parts[index].casefold()}"
    return f"{city}|{normalize_document_text(record.get('property_project'))}"


def _comment_fingerprint(record: dict[str, Any]) -> str:
    return hashlib.sha256(_record_text(record).encode("utf-8")).hexdigest()


def _document_rows(comments: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in comments:
        path = _first_path(record.get("source_document"))
        if path and not _is_audit_child(record) and _record_text(record):
            grouped[path].append(record)
    return grouped


def _content_signature(rows: list[dict[str, Any]]) -> tuple[str, list[str], str, list[str]]:
    texts = sorted({_record_text(row) for row in rows})
    fingerprints = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    normalized = "\n".join(fingerprints)
    return (
        hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        fingerprints,
        hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest(),
        texts,
    )


def _overlap(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    unmatched = list(right)
    scores: list[float] = []
    for value in left:
        best_index = -1
        best_score = 0.0
        for index, candidate in enumerate(unmatched):
            score = SequenceMatcher(None, value, candidate).ratio()
            if score > best_score:
                best_score, best_index = score, index
        if best_index >= 0 and best_score >= 0.80:
            scores.append(best_score)
            unmatched.pop(best_index)
    return sum(scores) / max(len(left), len(right)) if scores else 0.0


def _representative_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return min(
        rows,
        key=lambda row: (
            -int(row.get("search_eligible", True) is not False),
            -int(row.get("text_trust_status") == "verified"),
            -int(bool(row.get("response_id"))),
            str(row.get("source_document", "")),
            str(row.get("comment_id", "")),
        ),
    )


def canonicalize_documents(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """Annotate comments and return physical/canonical document registries.

    Exact binary and normalized-content matches are automatic aliases.  A
    near-duplicate with 90–98% substantive overlap is retained as a separate
    document and marked ``needs_review``; it must not silently affect the
    common-topic count until reviewed.
    """
    rows_by_path = _document_rows(comments)
    source_files: dict[str, dict[str, Any]] = {}
    descriptors: list[dict[str, Any]] = []
    for path, rows in sorted(rows_by_path.items()):
        representative = _representative_row(rows)
        binary_sha = str(representative.get("source_sha256", "")).strip().casefold()
        normalized_hash, page_fingerprints, substantive_hash, substantive_comment_texts = _content_signature(rows)
        file_id = source_file_id(path)
        source_files[file_id] = {
            "source_file_id": file_id,
            "filename": Path(path).name,
            "folder_path": Path(path).parent.as_posix(),
            "declared_project": representative.get("property_project", ""),
            "declared_round": representative.get("review_round", ""),
            "binary_sha256": binary_sha,
            "ingestion_timestamp": representative.get("ingestion_timestamp", ""),
        }
        descriptors.append({
            "path": path,
            "rows": rows,
            "source_file_id": file_id,
            "binary_sha256": binary_sha,
            "normalized_content_hash": normalized_hash,
            "page_fingerprints": page_fingerprints,
            "substantive_text_fingerprint": substantive_hash,
            "substantive_comment_texts": substantive_comment_texts,
            "site": _site_key(representative),
            "project": representative.get("property_project", ""),
            "round": representative.get("review_round", ""),
        })

    parent: dict[str, str] = {item["path"]: item["path"] for item in descriptors}
    reason: dict[tuple[str, str], str] = {}

    def find(path: str) -> str:
        while parent[path] != path:
            parent[path] = parent[parent[path]]
            path = parent[path]
        return path

    # Binary identity is decisive, even if folder/round labels differ.
    by_binary: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in descriptors:
        if item["binary_sha256"]:
            # A SHA-256 collision here means the visible bytes are the same;
            # folder, project and round labels cannot turn one physical file
            # into independent evidence.
            by_binary[item["binary_sha256"]].append(item)
    for items in by_binary.values():
        for item in items[1:]:
            parent[item["path"]] = items[0]["path"]
            reason[(item["path"], items[0]["path"])] = "identical_binary_sha256"

    # A normalized set catches re-exports, metadata changes, blank covers, and
    # renamed copies.  Do not include path or declared round in this key.
    by_normalized: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in descriptors:
        by_normalized[item["normalized_content_hash"]].append(item)
    for items in by_normalized.values():
        root = items[0]
        for item in items[1:]:
            parent[item["path"]] = root["path"]
            reason[(item["path"], root["path"])] = "identical_normalized_content"

    # Near duplicates are reported for review, never auto-collapsed.
    review_pairs: list[dict[str, Any]] = []
    for index, left in enumerate(descriptors):
        for right in descriptors[index + 1:]:
            if left["site"] != right["site"]:
                continue
            if left["normalized_content_hash"] == right["normalized_content_hash"]:
                continue
            overlap = _overlap(left["substantive_comment_texts"], right["substantive_comment_texts"])
            if overlap >= 0.98:
                parent[right["path"]] = left["path"]
                reason[(right["path"], left["path"])] = "near_duplicate_content_overlap"
            elif 0.90 <= overlap < 0.98:
                review_pairs.append({
                    "source_file_ids": [left["source_file_id"], right["source_file_id"]],
                    "similarity_score": round(overlap, 4),
                    "review_status": "needs_review",
                })

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in descriptors:
        groups[find(item["path"])].append(item)
    canonical_documents: dict[str, dict[str, Any]] = {}
    aliases: list[dict[str, Any]] = []
    path_to_canonical: dict[str, str] = {}
    canonical_root_path: dict[str, str] = {}
    for root_path, items in groups.items():
        root = min(items, key=lambda item: item["path"])
        identity_key = root["binary_sha256"] or root["normalized_content_hash"]
        canonical_id = "CD-" + hashlib.sha256(
            f"{root['site']}|{identity_key}".encode("utf-8")
        ).hexdigest()[:20]
        group_review = "confirmed" if len(items) > 1 else "not_reviewed"
        canonical_documents[canonical_id] = {
            "canonical_document_id": canonical_id,
            "document_type": Path(root["path"]).suffix.casefold().lstrip(".") or "unknown",
            "normalized_content_hash": root["normalized_content_hash"],
            "page_fingerprints": root["page_fingerprints"],
            "substantive_text_fingerprint": root["substantive_text_fingerprint"],
            "canonical_project": root["project"],
            "canonical_round": root["round"],
            "first_seen_source_file_id": root["source_file_id"],
            "duplicate_group_size": len(items),
            "duplicate_review_status": group_review,
        }
        canonical_root_path[canonical_id] = root["path"]
        for item in items:
            path_to_canonical[item["path"]] = canonical_id
            if item["path"] != root["path"]:
                aliases.append({
                    "source_file_id": item["source_file_id"],
                    "canonical_document_id": canonical_id,
                    "duplicate_reason": reason.get(
                        (item["path"], root["path"]),
                        "canonical_content_duplicate",
                    ),
                    "similarity_score": 1.0,
                })

    source_to_document = {
        item["source_file_id"]: path_to_canonical[item["path"]]
        for item in descriptors
        if item["path"] in path_to_canonical
    }
    for pair in review_pairs:
        for source_id in pair["source_file_ids"]:
            canonical_id = source_to_document.get(source_id)
            if canonical_id in canonical_documents:
                canonical_documents[canonical_id]["duplicate_review_status"] = "needs_review"

    rows_by_canonical_comment: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in comments:
        path = _first_path(record.get("source_document"))
        canonical_id = path_to_canonical.get(path)
        if not canonical_id or _is_audit_child(record):
            continue
        comment_key = (canonical_id, _record_text(record))
        rows_by_canonical_comment[comment_key].append(record)

    primary_by_key: dict[tuple[str, str], dict[str, Any]] = {
        key: _representative_row(rows) for key, rows in rows_by_canonical_comment.items()
    }
    for record in comments:
        path = _first_path(record.get("source_document"))
        canonical_id = path_to_canonical.get(path)
        if not canonical_id:
            continue
        record["source_file_id"] = source_file_id(path)
        record["canonical_document_id"] = canonical_id
        text_key = _record_text(record)
        comment_key = (canonical_id, text_key)
        canonical_comment_id = "CC-" + hashlib.sha256(
            f"{canonical_id}|{text_key}".encode("utf-8")
        ).hexdigest()[:20]
        record["canonical_comment_id"] = canonical_comment_id
        primary = primary_by_key.get(comment_key)
        is_primary = primary is record
        if _is_audit_child(record) or not is_primary:
            record["occurrence_type"] = "copied_duplicate"
            if primary is not None and primary is not record:
                record["carried_forward_from_comment_id"] = primary.get("comment_id", "")
        elif record.get("occurrence_type") not in {"reissued_unresolved", "historical_quote"}:
            record["occurrence_type"] = "newly_issued"
        document = canonical_documents[canonical_id]
        record["canonical_document_duplicate_group_size"] = document["duplicate_group_size"]
        # Keep one representative row for a logical comment and one physical
        # representative file for a logical document.  All other extraction
        # rows remain in the audit dataset but are not searchable evidence.
        if not is_primary or path != canonical_root_path[canonical_id]:
            record["occurrence_type"] = "copied_duplicate"
            record["search_eligible"] = False
            record["duplicate_status"] = "canonical_document_alias"

    # Response letters frequently reproduce the government's earlier comment
    # beside the applicant's response.  That quotation is useful audit/source
    # evidence, but it is not a second government-comment occurrence.  When an
    # exact substantive comment already exists in an earlier logical document,
    # retain the earlier canonical identity and quarantine the later quotation.
    by_site_text: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in comments:
        if record.get("occurrence_type") == "copied_duplicate" or record.get("search_eligible") is False:
            continue
        path = _first_path(record.get("source_document"))
        canonical_id = str(record.get("canonical_document_id", ""))
        if not canonical_id or not _record_text(record):
            continue
        by_site_text[(_site_key(record), _record_text(record))].append(record)

    def document_order(record: dict[str, Any]) -> tuple[int, str]:
        match = re.search(r"\d+", str(record.get("review_round", "")))
        return (int(match.group()) if match else 10**9, _first_path(record.get("source_document")))

    for records in by_site_text.values():
        records.sort(key=document_order)
        original = records[0]
        for record in records[1:]:
            path = _first_path(record.get("source_document"))
            filename = Path(path).name.casefold()
            if "response" not in filename:
                continue
            if record.get("occurrence_type") in {"reissued_unresolved", "uncertain"}:
                continue
            if record.get("canonical_document_id") == original.get("canonical_document_id"):
                continue
            record["carried_forward_from_comment_id"] = original.get("comment_id", "")
            record["canonical_document_id"] = original.get("canonical_document_id", "")
            record["canonical_comment_id"] = original.get("canonical_comment_id", "")
            record["occurrence_type"] = "historical_quote"
            record["search_eligible"] = False
            record["duplicate_status"] = "historical_quote"

    return {
        "source_files": source_files,
        "canonical_documents": canonical_documents,
        "source_file_aliases": aliases,
        "near_duplicate_review": review_pairs,
        "canonical_document_count": len(canonical_documents),
        "physical_source_file_count": len(source_files),
    }


def topic_occurrence_key(record: dict[str, Any]) -> tuple[str, str]:
    """Return a stable occurrence key, with a safe legacy fallback."""
    return (
        str(record.get("canonical_document_id") or f"legacy:{record.get('comment_id', '')}"),
        str(record.get("canonical_comment_id") or record.get("comment_id", "")),
    )


def topic_occurrence_allowed(record: dict[str, Any]) -> bool:
    occurrence_type = str(record.get("occurrence_type") or "newly_issued")
    return occurrence_type in {
        "newly_issued", "reissued_unresolved"
    }
