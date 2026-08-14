#!/usr/bin/env python3
"""Persist a scoped human confirmation without changing graph identity.

This is intentionally a trust-state migration, not an extraction or timeline
repair. It promotes only records that are still ``needs_review`` inside the
selected source-folder scope. Canonical IDs, issue IDs, links, dates, rounds,
locators, and timeline ordering are protected by a relationship snapshot.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from phase2.evidence_model import materialize_evidence_model, relationship_snapshot
from web_app.data_trust import is_malformed_rollup_comment, is_reference_note


DEFAULT_SCOPE = re.compile(r"^new/25-0(?:2[3-9]|3[01])-")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.stem}-",
        suffix=".tmp",
        delete=False,
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def _in_scope(row: dict[str, Any], pattern: re.Pattern[str]) -> bool:
    return bool(pattern.match(str(row.get("source_document", ""))))


def _pending(row: dict[str, Any]) -> bool:
    return str(row.get("verification_status", "")).casefold() == "needs_review"


def _audit(record: dict[str, Any], now: int, note: str, *, matched: bool | None = None) -> None:
    audit = record.setdefault("ingestion_audit", {})
    if not isinstance(audit, dict):
        audit = {}
        record["ingestion_audit"] = audit
    audit["human_record_verification"] = {
        "decision": "confirmed",
        "note": note,
        "updated_at": now,
        "scope": "new/25-023 through new/25-031",
    }
    if matched is not None:
        audit["human_record_verification"]["relationship"] = (
            "matched" if matched else "unmatched_not_required"
        )


def confirm_records(dataset: dict[str, Any], pattern: re.Pattern[str], note: str) -> dict[str, int]:
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    before = relationship_snapshot(dataset)

    comments_by_id = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comments", [])
        if isinstance(row, dict)
    }
    responses_by_id = {
        str(row.get("response_id", "")): row
        for row in dataset.get("responses", [])
        if isinstance(row, dict)
    }
    links_by_comment = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comment_response_links", [])
        if isinstance(row, dict)
    }

    pending_comments = [
        row for row in comments_by_id.values()
        if _in_scope(row, pattern) and _pending(row)
    ]
    pending_comment_ids = {
        str(row.get("comment_id", "")) for row in pending_comments
    }
    pending_response_ids = {
        str(row.get("response_id", ""))
        for row in responses_by_id.values()
        if _in_scope(row, pattern) and _pending(row)
    }

    duplicate_comments = 0
    existing_duplicate_search_exclusions = 0
    scoped_duplicate_comments = sum(
        1
        for comment in comments_by_id.values()
        if _in_scope(comment, pattern) and comment.get("duplicate_of")
    )
    searchable_comments = 0
    excluded_comments = 0
    for comment in pending_comments:
        comment_id = str(comment.get("comment_id", ""))
        verified = str(comment.get("verified_text") or comment.get("original_text") or "")
        excluded_reason = ""
        if comment.get("duplicate_of"):
            duplicate_comments += 1
            excluded_reason = "duplicate_source_occurrence"
        elif is_reference_note(comment):
            excluded_reason = "reference_note"
        elif is_malformed_rollup_comment(comment):
            excluded_reason = "malformed_round_rollup"

        comment.update({
            "verified_text": verified,
            "source_status": "confirmed",
            "human_review_status": "confirmed",
            "verification_status": "confirmed",
            "text_trust_status": "verified",
            "search_eligible": not bool(excluded_reason),
            "extraction_confidence": 1.0,
            "verification_basis": "human_record_confirmation",
        })
        if excluded_reason:
            comment["search_exclusion_reason"] = excluded_reason
            excluded_comments += 1
        else:
            comment.pop("search_exclusion_reason", None)
            searchable_comments += 1
        _audit(comment, now, note)

        link = links_by_comment.get(comment_id)
        if link is not None and _pending(link):
            matched = str(link.get("match_status", "")).casefold() == "matched"
            response_id = str(link.get("response_id") or comment.get("response_id") or "")
            response = responses_by_id.get(response_id)
            has_response = bool(response)
            link.update({
                "review_status": "confirmed" if has_response else "not_required",
                "verification_status": "confirmed",
                "match_confidence": 1.0 if has_response else 0.0,
                "verification_basis": "human_record_confirmation",
            })
            _audit(link, now, note, matched=matched)

    # The confirmation action must never reactivate a source-copy row that
    # was already consolidated under a canonical comment. Enforce the same
    # production-view rule across the whole scoped batch, including rows
    # confirmed by an earlier workbook-level review.
    for comment in comments_by_id.values():
        if not _in_scope(comment, pattern) or not comment.get("duplicate_of"):
            continue
        if comment.get("search_eligible") is True:
            existing_duplicate_search_exclusions += 1
        comment["search_eligible"] = False
        comment["search_exclusion_reason"] = "duplicate_source_occurrence"

    promoted_responses = 0
    for response_id in pending_response_ids:
        response = responses_by_id[response_id]
        verified = str(response.get("verified_text") or response.get("original_text") or "")
        response.update({
            "verified_text": verified,
            "source_status": "confirmed",
            "human_review_status": "confirmed",
            "verification_status": "confirmed",
            "text_trust_status": "verified",
            "search_eligible": bool(verified.strip()),
            "extraction_confidence": 1.0,
            "verification_basis": "human_record_confirmation",
        })
        _audit(response, now, note)
        promoted_responses += 1

    confirmed_review_items = 0
    promoted_ids = pending_comment_ids | pending_response_ids
    for item in dataset.get("review_items", []) or []:
        if not isinstance(item, dict) or str(item.get("item_id", "")) not in promoted_ids:
            continue
        item.update({
            "decision": "confirmed",
            "decision_note": note,
            "decided_at": now,
            "decision_source": "human_record_confirmation",
        })
        confirmed_review_items += 1

    history = dataset.setdefault("manual_review_history", [])
    if not isinstance(history, list):
        history = []
        dataset["manual_review_history"] = history
    decision_id = "HR-20260814-new-25-023-031"
    previous_decision = next((
        row for row in history
        if isinstance(row, dict) and row.get("decision_id") == decision_id
    ), {})
    history[:] = [
        row for row in history
        if not isinstance(row, dict) or row.get("decision_id") != decision_id
    ]
    history.append({
        "decision_id": decision_id,
        "decision": "confirmed",
        "scope": "new/25-023 through new/25-031",
        "confirmed_at": now,
        "confirmed_by": "app_user",
        "note": note,
        "comment_count": max(int(previous_decision.get("comment_count") or 0), len(pending_comments)),
        "response_count": max(int(previous_decision.get("response_count") or 0), promoted_responses),
        "duplicate_comments_kept_out_of_search": max(
            int(previous_decision.get("duplicate_comments_kept_out_of_search") or 0),
            scoped_duplicate_comments,
        ),
    })

    after = relationship_snapshot(dataset)
    if before != after:
        raise RuntimeError(
            "Non-regression check failed: confirmation changed IDs, links, "
            "dates, rounds, source locators, or timeline ordering"
        )

    return {
        "comments_confirmed": len(pending_comments),
        "responses_confirmed": promoted_responses,
        "links_confirmed_or_not_required": sum(
            1
            for comment_id in pending_comment_ids
            if comment_id in links_by_comment
        ),
        "comments_newly_searchable": searchable_comments,
        "comments_confirmed_but_excluded": excluded_comments,
        "duplicate_comments_kept_out_of_search": duplicate_comments,
        "scoped_duplicate_comments_kept_out_of_search": scoped_duplicate_comments,
        "existing_duplicate_search_exclusions": existing_duplicate_search_exclusions,
        "review_items_confirmed": confirmed_review_items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=WORKSPACE / "phase2_dataset" / "dataset.json",
    )
    parser.add_argument("--scope-regex", default=DEFAULT_SCOPE.pattern)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--note",
        default=(
            "User confirmed all remaining needs-review records in the app "
            "for the 2026-08-13 nine-project intake."
        ),
    )
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    stats = confirm_records(dataset, re.compile(args.scope_regex), args.note)
    stats["applied"] = bool(args.apply)
    if args.apply:
        materialize_evidence_model(dataset, args.dataset.parent)
        _atomic_json(args.dataset, dataset)
    print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
