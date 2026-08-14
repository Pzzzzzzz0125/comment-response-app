#!/usr/bin/env python3
"""Build global issue timelines while preserving every source occurrence."""

from __future__ import annotations

import argparse
from copy import deepcopy
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

WORKSPACE_IMPORT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_IMPORT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_IMPORT))

from phase2.issue_event_dedup import (
    _date_key,
    assign_issue_threads,
    build_issue_event_index,
    event_role_family,
    normalized_event_type,
)
from web_app.canonical_event import normalize_actor, normalize_event_text
from web_app.canonical_event import NORMALIZATION_VERSION


def atomic_json(path: Path, value: object) -> None:
    """Write a repair result atomically without importing visual ingestion.

    The event-repair command is intentionally local-only; importing the large
    visual-ingestion module here adds unrelated startup work and can make a
    simple date/index rebuild look stalled.
    """
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent,
        prefix=f"{path.stem}-", suffix=".tmp", delete=False,
    ) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _occurrence_context(event: dict) -> str:
    existing = str(event.get("occurrence_identity_context", ""))
    if existing:
        return existing
    role = event_role_family(event)
    marker = str(event.get("event_round_marker", "")).strip()
    if marker:
        return f"{role}|marker:{marker.casefold()}"
    date = _date_key(event)
    round_value = str(
        event.get("effective_round") or event.get("review_round")
        or event.get("observed_in_document_round") or ""
    ).strip()
    return "|".join((role, f"date:{date}" if date else "", f"round:{round_value}" if round_value else "")).strip("|")


def _occurrence_key(value: dict, context: str = "") -> str:
    """Stable physical-observation key used by the audit report."""
    location = value.get("source_location") or {}
    source_object = str(value.get("source_object_reference", ""))
    row_fallback = ""
    if not location and not source_object:
        row_fallback = str(value.get("comment_id", ""))
    return "|".join((
        str(value.get("source_document", "")).casefold(),
        repr(location),
        source_object or row_fallback,
    ))


def _event_key(value: dict) -> tuple[str, str, str, str, str]:
    text = normalize_event_text(value.get("exact_text") or value.get("text") or "")
    actor = normalize_actor(value.get("actor") or value.get("reviewer") or "")
    event_date = _date_key(value)
    event_round = str(
        value.get("effective_round") or value.get("review_round")
        or value.get("event_round_marker") or ""
    ).strip().casefold()
    printed_id = str(
        value.get("printed_comment_id") or value.get("comment_number") or ""
    ).strip().casefold()
    # A date-less checklist can legitimately contain the same short text in
    # two different numbered rows.  Keep those rows distinct in the QA key;
    # the canonicalizer itself applies the same printed-row guard for generic
    # text while still collapsing copied, substantive bodies.
    context = f"{event_round}|{text}"
    generic = text in {
        "noted", "revised", "done", "addressed", "complete", "completed",
        "see plans", "see revised", "see updated", "ok", "okay",
        "comment remains",
    } or len(text.split()) <= 2
    if generic and printed_id:
        context = f"{context}|printed:{printed_id}"
    elif not event_date and printed_id:
        context = f"{context}|printed:{printed_id}"
    return (event_role_family(value), normalized_event_type(value), actor, event_date, context)


def _index_event_count(index: dict) -> int:
    return sum(len(item.get("events", []) or []) for item in index.values())


def _index_occurrences(index: dict) -> set[str]:
    values: set[str] = set()
    for timeline in index.values():
        for event in timeline.get("events", []) or []:
            for occurrence in event.get("source_occurrences", []) or []:
                if isinstance(occurrence, dict):
                    # Audit physical observations, not parser-version IDs.
                    # A migration may intentionally mint a new ID for a row
                    # that had no locator, while the source/document/row/text
                    # evidence is unchanged.
                    values.add(_occurrence_key(occurrence, _occurrence_context(event)))
    return values


def _canonical_aliases(before: dict, after: dict) -> dict[str, str]:
    """Map legacy event IDs to the rebuilt canonical event IDs when possible."""
    aliases: dict[str, str] = {}
    for thread_id, old_timeline in before.items():
        new_timeline = after.get(thread_id, {})
        new_events = [event for event in new_timeline.get("events", []) or [] if isinstance(event, dict)]
        if not new_events:
            continue
        new_by_occurrence: dict[str, str] = {}
        new_by_key: dict[tuple, str] = {}
        for event in new_events:
            event_id = str(event.get("event_id", ""))
            if not event_id:
                continue
            new_by_key.setdefault(_event_key(event), event_id)
            for occurrence in event.get("source_occurrences", []) or []:
                if isinstance(occurrence, dict):
                    occurrence_id = str(occurrence.get("source_occurrence_id", ""))
                    if occurrence_id:
                        new_by_occurrence.setdefault(occurrence_id, event_id)
                    new_by_occurrence.setdefault(
                        _occurrence_key(occurrence, _occurrence_context(event)), event_id
                    )
        for old_event in old_timeline.get("events", []) or []:
            if not isinstance(old_event, dict):
                continue
            old_id = str(old_event.get("event_id", ""))
            if not old_id:
                continue
            target = ""
            old_occurrences = old_event.get("source_occurrences", []) or []
            for occurrence in old_occurrences:
                if isinstance(occurrence, dict):
                    keys = [
                        str(occurrence.get("source_occurrence_id", "")),
                        _occurrence_key(occurrence, _occurrence_context(old_event)),
                    ]
                    for key in keys:
                        if key:
                            target = new_by_occurrence.get(key, "")
                            if target:
                                break
                    if target:
                        break
            target = target or new_by_key.get(_event_key(old_event), "")
            if target and target != old_id:
                aliases[old_id] = target
    return aliases


def _duplicate_audit(index: dict) -> dict[str, int]:
    exact_groups = high_groups = 0
    timeline_exact_duplicates = 0
    for timeline in index.values():
        seen: set[tuple] = set()
        for event in timeline.get("events", []) or []:
            if not isinstance(event, dict):
                continue
            merged_ids = event.get("merged_event_ids", []) or []
            decision = str(event.get("dedup_decision", ""))
            if len(merged_ids) > 1:
                if decision == "HIGH_CONFIDENCE_DUPLICATE":
                    high_groups += 1
                else:
                    exact_groups += 1
            key = _event_key(event)
            if key in seen:
                timeline_exact_duplicates += 1
            else:
                seen.add(key)
    return {
        "exact_duplicate_groups_collapsed": exact_groups,
        "high_confidence_duplicate_groups_collapsed": high_groups,
        "timelines_containing_exact_duplicates": timeline_exact_duplicates,
    }


def main() -> int:
    workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=workspace / "phase2_dataset" / "dataset.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    comments_before = deepcopy(dataset.get("comments", []))
    before_index = dataset.get("issue_event_index", {})
    if not isinstance(before_index, dict):
        before_index = {}
    # Date/event repairs do not change document identity.  Preserve the
    # existing hierarchy instead of rerunning the costly document-wide
    # canonicalization pass, which is unrelated to this repair.
    grouping = assign_issue_threads(dataset.get("comments", []))
    index = build_issue_event_index(dataset.get("comments", []))
    aliases = dict(dataset.get("issue_event_aliases", {}) or {})
    aliases.update(_canonical_aliases(before_index, index))
    review_queue = [
        {
            "issue_thread_id": str(thread_id),
            **item,
        }
        for thread_id, timeline in index.items()
        for item in timeline.get("dedup_review_queue", [])
    ]
    raw = sum(int(item.get("raw_event_count", 0)) for item in index.values())
    canonical = sum(int(item.get("canonical_event_count", 0)) for item in index.values())
    before_canonical = _index_event_count(before_index)
    before_occurrences = _index_occurrences(before_index)
    after_occurrences = _index_occurrences(index)
    duplicate_groups = sum(1 for item in index.values() for event in item.get("events", []) if len(event.get("merged_event_ids", [])) > 1)
    event_id_owners: dict[str, set[str]] = {}
    for thread_id, item in index.items():
        for event in item.get("events", []):
            event_id = str(event.get("event_id", ""))
            if event_id:
                event_id_owners.setdefault(event_id, set()).add(str(thread_id))
    duplicate_event_ids = {
        event_id: owners
        for event_id, owners in event_id_owners.items()
        if len(owners) > 1
    }
    if duplicate_event_ids:
        examples = list(duplicate_event_ids.items())[:5]
        raise RuntimeError(
            "Issue-event repair generated non-unique event IDs: "
            f"{examples}"
        )
    audit = _duplicate_audit(index)
    result = {
        "threads_scanned": len(index),
        "canonical_events_before_repair": before_canonical,
        "canonical_events_after_repair": canonical,
        "raw_events": raw,
        "canonical_events": canonical,
        "duplicate_event_occurrences_merged": max(0, raw - canonical),
        "duplicate_event_groups": duplicate_groups,
        **audit,
        "source_occurrences_before_repair": len(before_occurrences),
        "source_occurrences_after_repair": len(after_occurrences),
        "lost_source_occurrences": len(before_occurrences - after_occurrences),
        "new_source_occurrences": len(after_occurrences - before_occurrences),
        "legacy_event_aliases": len(aliases),
        "duplicate_event_id_collisions": 0,
        "normalization_version": NORMALIZATION_VERSION,
        "possible_duplicate_count": len(review_queue),
        **grouping,
        "apply_requested": args.apply,
    }
    changes_detected = bool(
        comments_before != dataset.get("comments", [])
        or before_index != index
        or (dataset.get("issue_event_review_queue", []) or []) != review_queue
        or (dataset.get("issue_event_aliases", {}) or {}) != aliases
    )
    result["changes_detected"] = changes_detected
    result["applied"] = bool(args.apply and changes_detected)
    if args.apply and changes_detected:
        backup = args.dataset.with_name(
            f"{args.dataset.stem}.pre-issue-event-dedup-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
        )
        atomic_json(backup, dataset)
        dataset["issue_event_index"] = index
        dataset["issue_event_review_queue"] = review_queue
        dataset["issue_event_aliases"] = aliases
        dataset.setdefault("metadata", {})["issue_event_dedup"] = {
            **result,
            "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": (
                "same_site_issue_anchor_cumulative_history_then_"
                "same_thread_event_round_or_date_and_text_with_unique_"
                "occurrence_ids"
            ),
            "normalization_version": NORMALIZATION_VERSION,
            "possible_duplicate_count": len(review_queue),
            "issue_event_aliases": len(aliases),
        }
        atomic_json(args.dataset, dataset)
        result["backup_created"] = str(backup)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
