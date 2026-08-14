"""Versioned evidence-layer projection for the permit ingestion pipeline.

The application dataset intentionally keeps its historical fields for
backwards compatibility.  This module adds a normalized, auditable projection
without rewriting source text or changing the search index:

    raw document -> raw extraction -> evidence packet -> source occurrence
    -> canonical event -> issue timeline -> common topic -> verified index

The projection is deterministic and cheap to rebuild from an existing dataset.
Gemini is never called here and no text is paraphrased.  That makes it safe to
run after a parser or matching repair, and lets future stages be re-run from a
checkpoint rather than re-reading the original file.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

try:
    from web_app.text_reconstruction import (
        IDENTITY_NORMALIZATION_VERSION,
        RECONSTRUCTION_VERSION,
        SEARCH_NORMALIZATION_VERSION,
        normalize_for_identity,
        normalize_for_search,
        reconstruction_text,
    )
except ImportError:  # pragma: no cover - direct script execution
    from text_reconstruction import (  # type: ignore
        IDENTITY_NORMALIZATION_VERSION,
        RECONSTRUCTION_VERSION,
        SEARCH_NORMALIZATION_VERSION,
        normalize_for_identity,
        normalize_for_search,
        reconstruction_text,
    )


EVIDENCE_MODEL_VERSION = "evidence-model-v1"
CHECKPOINT_VERSION = "ingestion-checkpoints-v1"
STAGES = (
    "uploaded",
    "parsed",
    "prescanned",
    "extracted",
    "verified",
    "deduplicated",
    "timeline_linked",
    "indexed",
)


def _stable_id(prefix: str, *parts: Any) -> str:
    value = "|".join(str(part or "") for part in parts)
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def _text(value: Any) -> str:
    return str(value or "").replace("_x000D_", " ").replace("_x000A_", " ").strip()


def _representation(record: dict[str, Any]) -> dict[str, Any]:
    """Return additive text fields without using them for graph identity."""
    reconstruction = record.get("reconstruction")
    return {
        "text_raw": _text(record.get("text_raw") or record.get("raw_extracted_text") or record.get("original_text")),
        "text_reconstructed": _text(record.get("text_reconstructed") or reconstruction_text(record)),
        "display_structure": record.get("display_structure") if isinstance(record.get("display_structure"), list) else [],
        "normalized_identity_text_v2": _text(record.get("normalized_identity_text_v2") or normalize_for_identity(reconstruction_text(record))),
        "normalized_search_text_v2": _text(record.get("normalized_search_text_v2") or normalize_for_search(reconstruction_text(record))),
        "source_unit_ids": list(record.get("source_unit_ids", []) or []) if isinstance(record.get("source_unit_ids", []), list) else [],
        "reconstruction": dict(reconstruction) if isinstance(reconstruction, dict) else {},
    }


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value)).strip().casefold()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parse_date(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    match = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", raw)
    if not match:
        match = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})\b", raw)
        if match:
            return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    months = {name: index for index, name in enumerate((
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ), 1)}
    match = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})\b", raw)
    if match and match.group(1).casefold() in months:
        return f"{match.group(3)}-{months[match.group(1).casefold()]:02d}-{int(match.group(2)):02d}"
    return ""


def date_provenance(record: dict[str, Any], source_file: dict[str, Any] | None = None) -> dict[str, Any]:
    """Choose a date only from explicit source metadata, never from a round."""
    candidates = (
        ("response_text", record.get("response_date_iso"), record.get("response_date_raw")),
        ("document_body", record.get("event_date_iso"), record.get("event_date_raw")),
        ("table_date", record.get("report_date"), record.get("report_date_raw")),
        ("document_header", record.get("document_date_iso"), record.get("document_date_raw")),
        ("document_metadata", record.get("source_document_date"), record.get("source_date_evidence")),
        ("letter_date", record.get("letter_date"), record.get("letter_date_raw")),
    )
    for source, value, raw in candidates:
        iso = _parse_date(value) or _parse_date(raw)
        if iso:
            return {
                "value": iso,
                "source": source,
                "raw_text": _text(raw) or _text(value),
                "confidence": float(record.get("event_date_confidence") or record.get("document_date_confidence") or 1.0),
            }
    # A filename date is explicitly lower priority and is never confused with
    # a PC/round marker.
    filename = _text((source_file or {}).get("filename"))
    iso = _parse_date(filename)
    if iso:
        return {"value": iso, "source": "filename", "raw_text": filename, "confidence": 0.65}
    return {"value": "", "source": "unknown", "raw_text": "", "confidence": 0.0}


def round_provenance(record: dict[str, Any], source_file: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = record.get("review_round_metadata")
    reviewed = _text(record.get("reviewed_plan_round"))
    metadata_value = _text(metadata.get("value")) if isinstance(metadata, dict) else ""
    if metadata_value and not reviewed:
        return {
            "value": metadata_value,
            "source": _text(metadata.get("source")) or "record_metadata",
            "raw_text": _text(metadata.get("raw")) or _text(metadata.get("value")),
            "confidence": float(metadata.get("confidence") or 0.0),
        }
    explicit = reviewed or _text(record.get("review_round"))
    if explicit:
        # A response letter can carry a later round in metadata.  When the
        # event is explicitly tied to reviewed_plan_round, keep that plan
        # round and do not attach the response-letter provenance to it.
        metadata_source = _text(metadata.get("source")) if isinstance(metadata, dict) and metadata_value == explicit else ""
        metadata_confidence = float(metadata.get("confidence") or 1.0) if metadata_source else 1.0
        return {
            "value": explicit,
            "source": _text(record.get("review_round_source")) or metadata_source or ("reviewed_plan_round" if reviewed else "record_metadata"),
            "raw_text": explicit,
            "confidence": float(record.get("review_round_confidence") or metadata_confidence),
        }
    declared = _text((source_file or {}).get("declared_round"))
    if declared:
        return {"value": declared, "source": "document_header", "raw_text": declared, "confidence": 0.8}
    return {"value": "", "source": "unknown", "raw_text": "", "confidence": 0.0}


def _locator(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("source_locator_json", "comment_locator_json", "response_locator_json"):
        value = record.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _source_file_index(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = dataset.get("source_files", {})
    if isinstance(value, dict):
        return {str(key): dict(row) for key, row in value.items() if isinstance(row, dict)}
    return {}


def _path_index(dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for source_id, row in _source_file_index(dataset).items():
        folder = _text(row.get("folder_path"))
        filename = _text(row.get("filename"))
        if folder and filename:
            index[f"{folder}/{filename}"] = {"source_file_id": source_id, **row}
    for row in dataset.get("sources", []) if isinstance(dataset.get("sources"), list) else []:
        if not isinstance(row, dict):
            continue
        path = _text(row.get("source_document"))
        if path and path not in index:
            index[path] = dict(row)
    return index


def _source_path(record: dict[str, Any]) -> str:
    value = _text(record.get("source_document"))
    return value.split(" | ", 1)[0].strip()


def _source_occurrence(owner: dict[str, Any], role: str, source_files: dict[str, dict[str, Any]], paths: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path = _source_path(owner)
    source = paths.get(path, {})
    source_file_id = _text(owner.get("source_file_id") or source.get("source_file_id"))
    document_id = _text(owner.get("canonical_document_id") or owner.get("document_id") or source.get("canonical_document_id"))
    owner_id = _text(owner.get("comment_id") if role == "comment" else owner.get("response_id"))
    # The physical source location, not an extraction-row ID, defines an
    # occurrence.  Two parser passes that emit the same quote at the same
    # file/page/locator therefore become one occurrence with multiple owners.
    quote = _normalized(owner.get("normalized_comment_text") or owner.get("verified_text") or owner.get("original_text"))
    occurrence_id = _stable_id(
        "SO", role, source_file_id or document_id, path,
        owner.get("source_page") or owner.get("source_page_start") or 0,
        json.dumps(_locator(owner), sort_keys=True), quote,
    )
    return {
        "source_occurrence_id": occurrence_id,
        "owner_id": owner_id,
        "role": role,
        "source_file_id": source_file_id,
        "document_id": document_id,
        "source_path": path,
        "source_sha256": _text(owner.get("source_sha256") or source.get("binary_sha256")),
        "page": owner.get("source_page") or owner.get("source_page_start") or 0,
        "locator": _locator(owner),
        "exact_quote": _text(owner.get("verified_text") or owner.get("original_text")),
        "normalized_quote": quote,
        "source_unit_ids": list(owner.get("source_unit_ids", []) or []) if isinstance(owner.get("source_unit_ids", []), list) else [],
        "text_representation": _representation(owner),
        "date": date_provenance(owner, source),
        "round": round_provenance(owner, source),
        "metadata": {
            "sheet": _text(owner.get("source_sheet")),
            "row": owner.get("source_row", ""),
            "cell_range": _text(owner.get("source_cell_range")),
            "paragraph_index": owner.get("paragraph_index", ""),
        },
    }


def _confirmation_gate(comment: dict[str, Any], response: dict[str, Any] | None, link: dict[str, Any] | None) -> dict[str, Any]:
    """Return an auditable gate; confidence alone can never pass it."""
    link = link or {}
    audit = comment.get("ingestion_audit")
    if not isinstance(audit, dict):
        audit = link.get("ingestion_audit") if isinstance(link.get("ingestion_audit"), dict) else {}
    pair_audit = audit.get("pair_verification") if isinstance(audit.get("pair_verification"), dict) else {}
    coverage_audit = audit.get("coverage_verification") if isinstance(audit.get("coverage_verification"), dict) else {}
    has_explicit_pair = bool(response)
    # A previous importer may already have admitted a row to the verified
    # search index before the normalized two-pass audit fields were introduced.
    # Preserve that explicit admission as provenance; do not infer it from a
    # confidence score or from a filename.
    response_explicitly_confirmed = (
        not response
        or str(response.get("verification_status", "")).casefold() == "confirmed"
        or str(response.get("human_review_status", "")).casefold() == "confirmed"
    )
    legacy_index_admission = (
        comment.get("search_eligible") is True
        and str(comment.get("verification_status", "")).casefold() == "confirmed"
        and str(comment.get("text_trust_status", "")).casefold() == "verified"
        and response_explicitly_confirmed
    )
    checks = {
        "original_text_present": bool(_text(comment.get("verified_text") or comment.get("original_text"))),
        "source_location_present": bool(_locator(comment)),
        "verbatim_verification_passed": str(comment.get("verification_status", "")).casefold() == "confirmed" or str(comment.get("text_trust_status", "")).casefold() == "verified",
        # A comment without an applicant response has no pair to validate;
        # its pair gate is satisfied only when the record explicitly says it
        # is unmatched/not required.  Matched records need the link or the
        # two-pass audit result.
        "pair_verification_passed": (
            bool(pair_audit.get("passed") is True)
            or str(link.get("verification_status", "")).casefold() == "confirmed"
            and str(link.get("review_status", "")).casefold() == "confirmed"
            or legacy_index_admission
            or not has_explicit_pair and str(link.get("match_status", "unmatched")).casefold() in {"unmatched", "unlinked", "not_required"}
        ),
        "coverage_verification_passed": (
            bool(coverage_audit.get("passed") is True)
            or str(link.get("coverage_verification_status", "")).casefold() == "confirmed"
            or str(comment.get("coverage_verification_status", "")).casefold() == "confirmed"
            or legacy_index_admission
        ),
        "no_conflict": not bool(comment.get("verification_conflict") or link.get("verification_conflict")),
        "date_or_round_present": bool(date_provenance(comment)["value"] or round_provenance(comment)["value"]),
    }
    return {
        "status": "confirmed" if all(checks.values()) else "needs_review",
        "checks": checks,
        "legacy_index_admission": legacy_index_admission,
    }


def build_evidence_model(dataset: dict[str, Any]) -> dict[str, Any]:
    """Build the normalized projection without changing the application rows."""
    source_files = _source_file_index(dataset)
    paths = _path_index(dataset)
    comments = [row for row in dataset.get("comments", []) if isinstance(row, dict)]
    responses = {str(row.get("response_id", "")): row for row in dataset.get("responses", []) if isinstance(row, dict)}
    links = {str(row.get("comment_id", "")): row for row in dataset.get("comment_response_links", []) if isinstance(row, dict)}
    source_rows = [row for row in dataset.get("sources", []) if isinstance(row, dict)]

    raw_documents: list[dict[str, Any]] = []
    for source_id, row in sorted(source_files.items()):
        raw_documents.append({
            "document_id": _text(row.get("canonical_document_id") or source_id),
            "source_file_id": source_id,
            "path": _text(row.get("folder_path")) + ("/" if row.get("folder_path") and row.get("filename") else "") + _text(row.get("filename")),
            "filename": _text(row.get("filename")),
            "original_type": Path(_text(row.get("filename"))).suffix.casefold().lstrip("."),
            "sha256": _text(row.get("binary_sha256")),
            "immutable": True,
        })
    by_artifact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in comments + list(responses.values()):
        audit = row.get("ingestion_audit") if isinstance(row.get("ingestion_audit"), dict) else {}
        artifact = _text(audit.get("artifact_id"))
        if artifact:
            by_artifact[artifact].append(row)
    raw_extractions: list[dict[str, Any]] = []
    for artifact_id, rows in sorted(by_artifact.items()):
        first = rows[0]
        audit = first.get("ingestion_audit") if isinstance(first.get("ingestion_audit"), dict) else {}
        raw_extractions.append({
            "raw_extraction_id": _stable_id("RX", artifact_id),
            "artifact_id": artifact_id,
            "source_file_id": _text(first.get("source_file_id")),
            "source_path": _source_path(first),
            "document_id": _text(first.get("canonical_document_id")),
            "raw_artifact": "ingestion_artifacts/%s/raw_text.json" % artifact_id,
            "evidence_packet_artifact": "ingestion_artifacts/%s/evidence_packet.json" % artifact_id,
            "extraction_artifact": "ingestion_artifacts/%s/gemini_extraction.json" % artifact_id,
            "verification_artifact": "ingestion_artifacts/%s/gemini_verification.json" % artifact_id,
            "reconstruction_correction_artifact": "ingestion_artifacts/%s/gemini_reconstruction_correction.json" % artifact_id,
            "extraction_prompt_version": _text(audit.get("extraction_prompt_version")),
            "verification_prompt_version": _text(audit.get("verification_prompt_version")),
            "reconstruction_version": _text((first.get("reconstruction") or {}).get("version")) if isinstance(first.get("reconstruction"), dict) else "",
            "identity_normalization_version": IDENTITY_NORMALIZATION_VERSION,
            "search_normalization_version": SEARCH_NORMALIZATION_VERSION,
            "record_count": len(rows),
        })
    extraction_by_artifact = {row["artifact_id"]: row["raw_extraction_id"] for row in raw_extractions}

    occurrences: list[dict[str, Any]] = []
    occurrence_by_id: dict[str, dict[str, Any]] = {}
    comment_occurrences: dict[str, list[str]] = defaultdict(list)
    response_occurrences: dict[str, list[str]] = defaultdict(list)
    for row in comments:
        item = _source_occurrence(row, "comment", source_files, paths)
        existing = occurrence_by_id.get(item["source_occurrence_id"])
        if existing is None:
            occurrence_by_id[item["source_occurrence_id"]] = item
            occurrences.append(item)
        else:
            existing.setdefault("owner_ids", [existing.get("owner_id", "")])
            if str(item.get("owner_id", "")) not in existing["owner_ids"]:
                existing["owner_ids"].append(str(item.get("owner_id", "")))
        comment_occurrences[str(row.get("comment_id", ""))].append(item["source_occurrence_id"])
    for row in responses.values():
        item = _source_occurrence(row, "response", source_files, paths)
        existing = occurrence_by_id.get(item["source_occurrence_id"])
        if existing is None:
            occurrence_by_id[item["source_occurrence_id"]] = item
            occurrences.append(item)
        else:
            existing.setdefault("owner_ids", [existing.get("owner_id", "")])
            if str(item.get("owner_id", "")) not in existing["owner_ids"]:
                existing["owner_ids"].append(str(item.get("owner_id", "")))
        response_occurrences[str(row.get("comment_id", ""))].append(item["source_occurrence_id"])

    event_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in comments:
        text = _normalized(row.get("canonical_comment_id") or row.get("verified_text") or row.get("original_text"))
        site = _text(row.get("site_id") or row.get("project_id") or row.get("property_project") or row.get("city"))
        round_value = round_provenance(row)["value"]
        date_value = date_provenance(row)["value"]
        # A canonical comment id is the strongest identity.  Without it, do
        # not collapse undated records from different files by similarity.
        # A canonical comment identity is still scoped to one round/date: the
        # same printed issue can legitimately reappear as a new event in a
        # later review round.  If both are missing, retain the source path to
        # avoid guessing that two undated records are the same event.
        identity_scope = f"round:{round_value}|date:{date_value}"
        if not round_value and not date_value:
            identity_scope += f"|source:{_source_path(row)}"
        key = f"canonical:{row.get('canonical_comment_id')}|{identity_scope}" if row.get("canonical_comment_id") else f"event:{site}|{round_value}|{date_value or _source_path(row)}|{text}"
        event_groups[key].append(row)
    canonical_events: list[dict[str, Any]] = []
    for key, rows in sorted(event_groups.items()):
        first = rows[0]
        comment_ids = [str(row.get("comment_id", "")) for row in rows]
        source_ids = list(dict.fromkeys(
            item for comment_id in comment_ids
            for item in comment_occurrences.get(comment_id, [])
        ))
        response_ids = list(dict.fromkeys(
            item for comment_id in comment_ids
            for item in response_occurrences.get(comment_id, [])
        ))
        response_rows = [responses.get(str(row.get("response_id", ""))) for row in rows if row.get("response_id")]
        response_rows = [row for row in response_rows if isinstance(row, dict)]
        gates = [
            _confirmation_gate(row, responses.get(str(row.get("response_id", ""))) if row.get("response_id") else None, links.get(str(row.get("comment_id", ""))))
            for row in rows
        ]
        # One canonical event can have several source occurrences.  Preserve
        # the event if any occurrence was explicitly admitted to the verified
        # index, while retaining the strongest gate details for audit.
        gate = next((item for item in gates if item.get("status") == "confirmed"), gates[0])
        event_id = _stable_id("CE", key)
        canonical_events.append({
            "canonical_event_id": event_id,
            "event_key": key,
            "comment_ids": comment_ids,
            "response_ids": [str(row.get("response_id")) for row in response_rows],
            "source_occurrence_ids": source_ids + response_ids,
            "site_id": _text(first.get("site_id")),
            "project_id": _text(first.get("project_id") or first.get("property_project")),
            "city": _text(first.get("city")),
            "round": round_provenance(first),
            "date": date_provenance(first),
            "exact_comment_text": _text(first.get("verified_text") or first.get("original_text")),
            "text_representation": _representation(first),
            "confirmation": gate,
            "search_eligible": gate["status"] == "confirmed",
        })

    timelines: list[dict[str, Any]] = []
    timeline_events = dataset.get("issue_event_index", {})
    if isinstance(timeline_events, dict):
        for thread_id, thread in sorted(timeline_events.items()):
            if not isinstance(thread, dict):
                continue
            ids = [str(value) for value in thread.get("member_comment_ids", [])]
            related = [event for event in canonical_events if set(event["comment_ids"]) & set(ids)]
            timelines.append({
                "issue_timeline_id": str(thread_id),
                "event_ids": [event["canonical_event_id"] for event in related],
                "comment_ids": ids,
                "first_round": min((event["round"]["value"] for event in related if event["round"]["value"]), default=""),
                "latest_round": max((event["round"]["value"] for event in related if event["round"]["value"]), default=""),
                "status": "needs_review" if not related or any(event["confirmation"]["status"] != "confirmed" for event in related) else "confirmed",
            })
    timeline_by_comment = {comment_id: timeline["issue_timeline_id"] for timeline in timelines for comment_id in timeline["comment_ids"]}
    comment_to_event = {
        comment_id: event["canonical_event_id"]
        for event in canonical_events
        for comment_id in event["comment_ids"]
    }
    common_topics: dict[str, dict[str, Any]] = {}
    for row in comments:
        label = _text(row.get("category") or row.get("discipline") or "Uncategorized")
        topic = common_topics.setdefault(label, {"common_topic_id": _stable_id("CT", label), "label": label, "timeline_ids": set(), "event_ids": set()})
        comment_id = str(row.get("comment_id", ""))
        if comment_id in timeline_by_comment:
            topic["timeline_ids"].add(timeline_by_comment[comment_id])
        if comment_id in comment_to_event:
            topic["event_ids"].add(comment_to_event[comment_id])
    for topic in common_topics.values():
        topic["timeline_ids"] = sorted(topic["timeline_ids"])
        topic["event_ids"] = sorted(topic["event_ids"])
        topic["timeline_count"] = len(topic["timeline_ids"])
    evidence_packets = []
    for row in source_rows:
        path = _source_path(row)
        rows = [comment for comment in comments if _source_path(comment) == path]
        artifact_ids = sorted({
            _text((comment.get("ingestion_audit") or {}).get("artifact_id"))
            for comment in rows if isinstance(comment.get("ingestion_audit"), dict)
        } - {""})
        evidence_packets.append({
            "evidence_packet_id": _stable_id("EP", path),
            "source_path": path,
            "source_file_id": _text(row.get("source_file_id")),
            "document_id": _text(row.get("canonical_document_id")),
            "raw_extraction_ids": [extraction_by_artifact[item] for item in artifact_ids if item in extraction_by_artifact],
            "source_occurrence_ids": [occ["source_occurrence_id"] for occ in occurrences if occ["source_path"] == path],
            "page_count": row.get("page_count", 0),
            "processing_status": _text(row.get("processing_status")),
            "verification_status": _text(row.get("verification_result")),
        })

    source_to_summary = { _source_path(row): row for row in source_rows }
    checkpoints = []
    for document in raw_documents:
        path = document["path"]
        summary = source_to_summary.get(path, {})
        comments_for_path = [row for row in comments if _source_path(row) == path]
        has_verified = any(str(row.get("verification_status", "")).casefold() == "confirmed" for row in comments_for_path)
        has_indexed = any(row.get("search_eligible") is True for row in comments_for_path)
        status = _text(summary.get("processing_status"))
        completed = {
            "uploaded": True,
            "parsed": bool(status or document["sha256"]),
            "prescanned": bool(summary.get("pages_screened") or status),
            "extracted": bool(comments_for_path or summary.get("comment_count") or summary.get("response_count")),
            "verified": has_verified or _text(summary.get("verification_result")) in {"complete", "verified", "confirmed"},
            "deduplicated": bool(comments_for_path),
            "timeline_linked": any(row.get("issue_thread_id") for row in comments_for_path),
            "indexed": has_indexed,
        }
        stages = {stage: {"status": "complete" if completed[stage] else ("needs_review" if stage in {"verified", "indexed"} and comments_for_path else "pending")} for stage in STAGES}
        checkpoints.append({
            "document_id": document["document_id"],
            "source_file_id": document["source_file_id"],
            "source_path": path,
            "source_sha256": document["sha256"],
            "stages": stages,
            "versions": {
                "pipeline": _text(summary.get("ingestion_pipeline_version")) or _text(dataset.get("ingestion_pipeline_version")),
                "parser": _text(summary.get("parser_version")),
                "prescan": _text(summary.get("page_screening_version")),
                "extraction_prompt": _text(summary.get("extraction_prompt_version")),
                "verification_prompt": _text(summary.get("verification_prompt_version")),
                "dedup": _text(dataset.get("dedup_version")),
                "reconstruction": RECONSTRUCTION_VERSION,
                "identity_normalization": IDENTITY_NORMALIZATION_VERSION,
                "search_normalization": SEARCH_NORMALIZATION_VERSION,
            },
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    return {
        "schema_version": EVIDENCE_MODEL_VERSION,
        "stages": list(STAGES),
        "raw_documents": raw_documents,
        "raw_extractions": raw_extractions,
        "evidence_packets": evidence_packets,
        "source_occurrences": occurrences,
        "canonical_events": canonical_events,
        "issue_timelines": timelines,
        "common_topics": sorted(common_topics.values(), key=lambda row: row["label"].casefold()),
        "checkpoints": checkpoints,
        "counts": {
            "raw_documents": len(raw_documents),
            "raw_extractions": len(raw_extractions),
            "evidence_packets": len(evidence_packets),
            "source_occurrences": len(occurrences),
            "canonical_events": len(canonical_events),
            "issue_timelines": len(timelines),
            "common_topics": len(common_topics),
            "search_eligible_events": sum(event["search_eligible"] for event in canonical_events),
        },
    }


def relationship_snapshot(dataset: dict[str, Any]) -> dict[str, Any]:
    """Return a stable snapshot of relationship-level fields.

    The reconstruction migration compares this before and after its write.
    It intentionally excludes text representations and other presentation
    fields, so a formatting-only change cannot be mistaken for a graph edit.
    """
    comments = [row for row in dataset.get("comments", []) if isinstance(row, dict)]
    responses = [row for row in dataset.get("responses", []) if isinstance(row, dict)]
    links = [row for row in dataset.get("comment_response_links", []) if isinstance(row, dict)]
    index = dataset.get("issue_event_index", {})
    timeline_index: dict[str, Any] = {}
    if isinstance(index, dict):
        for thread_id, thread in sorted(index.items(), key=lambda item: str(item[0])):
            if not isinstance(thread, dict):
                continue
            # Preserve list order: ordering is part of the non-regression
            # contract even when an implementation also stores a set-like
            # member list elsewhere.
            timeline_index[str(thread_id)] = {
                key: thread.get(key)
                for key in ("event_ids", "events", "member_comment_ids", "comment_ids")
                if key in thread
            }
    # Keep the fields that define graph identity and chronology in the audit
    # snapshot.  Representation-only migrations may add text fields, but
    # they must never silently change a canonical event, issue thread, link,
    # date, round, or source locator.
    canonical_relationships = []
    for row in comments:
        canonical_relationships.append({
            "comment_id": str(row.get("comment_id", "")),
            "canonical_event_id": str(row.get("canonical_event_id", "")),
            "canonical_comment_id": str(row.get("canonical_comment_id", "")),
            "issue_thread_id": str(row.get("issue_thread_id", "")),
            "response_id": str(row.get("response_id", "")),
            "event_date": str(row.get("event_date_iso") or row.get("event_date") or ""),
            "event_date_raw": str(row.get("event_date_raw", "")),
            "review_round": str(row.get("review_round", "")),
            "reviewed_plan_round": str(row.get("reviewed_plan_round", "")),
            "response_letter_round": str(row.get("response_letter_round", "")),
            "observed_round": str(row.get("document_round") or row.get("source_cycle") or ""),
            "source_document": str(row.get("source_document", "")),
            "source_page": str(row.get("source_page", "")),
            "source_locator_json": row.get("source_locator_json", {}) if isinstance(row.get("source_locator_json", {}), dict) else {},
            "source_occurrence_ids": list(row.get("source_occurrence_ids", []) or []) if isinstance(row.get("source_occurrence_ids", []), list) else [],
        })
    for row in responses:
        canonical_relationships.append({
            "response_id": str(row.get("response_id", "")),
            "comment_id": str(row.get("comment_id", "")),
            "canonical_event_id": str(row.get("canonical_event_id", "")),
            "issue_thread_id": str(row.get("issue_thread_id", "")),
            "event_date": str(row.get("event_date_iso") or row.get("event_date") or ""),
            "event_date_raw": str(row.get("event_date_raw", "")),
            "review_round": str(row.get("review_round", "")),
            "reviewed_plan_round": str(row.get("reviewed_plan_round", "")),
            "response_letter_round": str(row.get("response_letter_round", "")),
            "observed_round": str(row.get("document_round") or row.get("source_cycle") or ""),
            "source_document": str(row.get("source_document", "")),
            "source_page": str(row.get("source_page", "")),
            "source_locator_json": row.get("source_locator_json", {}) if isinstance(row.get("source_locator_json", {}), dict) else {},
            "source_occurrence_ids": list(row.get("source_occurrence_ids", []) or []) if isinstance(row.get("source_occurrence_ids", []), list) else [],
            })
    # Recurring/common-topic membership is often derived by the server rather
    # than stored under one stable legacy key.  Capture any explicit legacy
    # membership fields when present, plus the issue-index/alias structures
    # that define recurring-thread membership, so a representation migration
    # cannot silently change those relationships either.
    topic_membership = []
    for role, rows in (("comment", comments), ("response", responses)):
        for row in rows:
            fields = {
                str(key): row.get(key)
                for key in row
                if any(token in str(key).casefold() for token in ("topic", "recurring"))
            }
            if fields:
                topic_membership.append({
                    "role": role,
                    "id": str(row.get("comment_id") or row.get("response_id") or ""),
                    "fields": fields,
                })
    return {
        "comment_ids": [str(row.get("comment_id", "")) for row in comments],
        "response_ids": [str(row.get("response_id", "")) for row in responses],
        "comment_response": [
            {
                "comment_id": str(row.get("comment_id", "")),
                "response_id": str(row.get("response_id", "")),
                "link_id": str(row.get("link_id", "")),
            }
            for row in links
        ],
        "source_occurrence_ids": [
            str(row.get("source_occurrence_id", ""))
            for row in dataset.get("source_occurrences", [])
            if isinstance(row, dict)
        ],
        "canonical_relationships": canonical_relationships,
        "timeline_index": timeline_index,
        "issue_event_aliases": dataset.get("issue_event_aliases", {}),
        "issue_event_review_queue": dataset.get("issue_event_review_queue", []),
        "topic_membership": topic_membership,
        "common_topic_membership": dataset.get("common_topic_membership", dataset.get("common_topics", {})),
    }


def materialize_evidence_model(dataset: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """Write the sidecar and a small pointer in the dataset envelope."""
    model = build_evidence_model(dataset)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "evidence_model.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    dataset["evidence_model"] = {
        "schema_version": EVIDENCE_MODEL_VERSION,
        "path": "evidence_model.json",
        "counts": model["counts"],
    }
    return model


__all__ = [
    "CHECKPOINT_VERSION", "EVIDENCE_MODEL_VERSION", "STAGES",
    "build_evidence_model", "date_provenance", "materialize_evidence_model",
    "relationship_snapshot", "round_provenance",
]
