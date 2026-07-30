#!/usr/bin/env python3
"""Normalize the verified 10334 El Prado Cupertino source set.

The two original City DOCX files are authoritative for three government
comments. Later response-letter copies are retained for audit and citation,
but are suppressed as duplicate parent comments. No Gemini call is made.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from phase2 import extract_dataset as base
from phase2.visual_ingestion import atomic_json


PROJECT = "25-018-10344 el prado way unit a cupertino"
BUILDING_SOURCE = (
    "comments&response/25-018-10344_el-prado_way_unit_a_cupertino/"
    "1st round of comments/BLD - 1st Review Comment - BLD-2025-0847.docx"
)
PLANNING_SOURCE = (
    "comments&response/25-018-10344_el-prado_way_unit_a_cupertino/"
    "1st round of comments/PLNG-Review #1-BLD-2025-0847.docx"
)
LATEST_RESPONSE_SOURCE = (
    "comments&response/25-018-10344_el-prado_way_unit_a_cupertino/"
    "2nd submital package/response letter.docx"
)
RESPONSE_ARTIFACT = "VI-693728a180abc95b5b76"

AUTHORITATIVE = {
    "building-1": "C-9e92dc9d2c91ba40",
    "building-2": "C-aad8f780bd64f08d",
    "planning-1": "C-7f247f326db107ee",
}
SUPPRESS_TO = {
    "C-53f38f5cc0265747": AUTHORITATIVE["building-1"],
    "C-62adb341219d6ff6": AUTHORITATIVE["building-1"],
    "C-7db632edabc54b94": AUTHORITATIVE["building-2"],
    "C-5527dfeed9d92f18": AUTHORITATIVE["building-2"],
    "C-c6591525948e5fac": AUTHORITATIVE["planning-1"],
    "C-acf14a37b3cccb33": AUTHORITATIVE["planning-1"],
}
CONTEXT_ONLY = {"C-f9ed6fb926561268"}


def _blocks(artifact_root: Path, artifact_id: str) -> dict[int, str]:
    payload = json.loads(
        (
            artifact_root / artifact_id / "raw_text.json"
        ).read_text(encoding="utf-8")
    )
    return {
        int(row.get("index") or 0): str(row.get("text", ""))
        for row in payload.get("blocks", [])
        if isinstance(row, dict)
    }


def _response_segment(
    text: str,
    marker: str = "",
) -> str:
    if marker:
        if marker not in text:
            raise ValueError(f"Response marker {marker!r} is missing")
        text = text.split(marker, 1)[1]
    return text.strip()


def _thread_id(comment: dict[str, Any]) -> str:
    anchor = " ".join(
        str(
            comment.get("normalized_comment_text")
            or comment.get("original_text", "")
        ).casefold().split()
    )
    return base.stable_id(
        "T",
        str(comment.get("city", "")).casefold(),
        str(comment.get("property_project", "")).casefold(),
        str(comment.get("discipline", "")).casefold(),
        anchor,
    )


def _event(
    thread_id: str,
    response: dict[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    return {
        "event_id": base.stable_id(
            "E", thread_id, "prior-applicant-response",
            response["response_id"],
        ),
        "issue_thread_id": thread_id,
        "event_type": "applicant_response",
        "actor_role": "company",
        "actor": "",
        "occurred_at": occurred_at,
        "occurred_at_label": occurred_at,
        "exact_text": str(response.get("original_text", "")),
        "raw_segment": str(response.get("original_text", "")),
        "source_order": 1,
        "source_document": str(response.get("source_document", "")),
        "source_locator_json": response.get(
            "source_locator_json", {},
        ),
        "parse_status": "verified_source_record",
        "review_round": "1",
    }


def repair(
    dataset: dict[str, Any],
    artifact_root: Path,
) -> dict[str, int]:
    comments = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comments", [])
    }
    responses = {
        str(row.get("response_id", "")): row
        for row in dataset.get("responses", [])
    }
    links = {
        str(row.get("comment_id", "")): row
        for row in dataset.get("comment_response_links", [])
    }
    missing = [
        comment_id for comment_id in AUTHORITATIVE.values()
        if comment_id not in comments
    ]
    if missing:
        raise ValueError(
            f"Authoritative Cupertino comments are missing: {missing}"
        )
    if comments[AUTHORITATIVE["building-1"]].get(
        "source_document"
    ) != BUILDING_SOURCE:
        raise ValueError("Building authority source changed")
    if comments[AUTHORITATIVE["planning-1"]].get(
        "source_document"
    ) != PLANNING_SOURCE:
        raise ValueError("Planning authority source changed")

    raw = _blocks(artifact_root, RESPONSE_ARTIFACT)
    latest_text = {
        "building-1": _response_segment(raw[21]),
        "building-2": _response_segment(raw[24]),
        "planning-1": "\n".join([
            _response_segment(raw[32], "Response:"),
            _response_segment(raw[34], "Noted."),
        ]).replace(
            "\nPlease see surface",
            "\nNoted. Please see surface",
        ),
    }
    latest_locators = {
        "building-1": [{
            "paragraph_index": 21,
            "pages": [1],
            "description": "latest company response",
            "exact_quote": latest_text["building-1"],
        }],
        "building-2": [{
            "paragraph_index": 24,
            "pages": [1],
            "description": "latest company response",
            "exact_quote": latest_text["building-2"],
        }],
        "planning-1": [
            {
                "paragraph_index": 32,
                "pages": [1],
                "description": "bird-safe glazing response",
                "exact_quote": latest_text["planning-1"].splitlines()[0],
            },
            {
                "paragraph_index": 34,
                "pages": [2],
                "description": "bird-safe area calculation response",
                "exact_quote": latest_text["planning-1"].splitlines()[1],
            },
        ],
    }
    response_source_record = comments["C-5527dfeed9d92f18"]
    response_sha = str(response_source_record.get("source_sha256", ""))
    updated_response_ids: set[str] = set()
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    for key, comment_id in AUTHORITATIVE.items():
        comment = comments[comment_id]
        prior_response = responses.get(
            str(comment.get("response_id", ""))
        )
        if not prior_response:
            raise ValueError(
                f"Initial response is missing for {comment_id}"
            )
        thread_id = _thread_id(comment)
        response_id = base.stable_id(
            "R", comment_id, response_sha, "verified-latest",
        )
        updated_response_ids.add(response_id)
        locators = latest_locators[key]
        first = locators[0]
        latest_response = {
            "response_id": response_id,
            "comment_id": comment_id,
            "original_text": latest_text[key],
            "verified_text": latest_text[key],
            "source_document": LATEST_RESPONSE_SOURCE,
            "source_sha256": response_sha,
            "source_sheet": "",
            "source_row": first["paragraph_index"],
            "source_cell_range": "",
            "source_page": first["pages"][0],
            "source_page_end": locators[-1]["pages"][-1],
            "source_location": (
                f"paragraph {first['paragraph_index']} · "
                "verified latest company response"
            ),
            "source_locator_json": first,
            "additional_source_locators": locators[1:],
            "extraction_method": "verified_docx_structure_repair",
            "extraction_confidence": 1.0,
            "reviewed_plan_round": "1",
            "response_letter_round": "2",
            "human_review_status": "confirmed",
            "verification_status": "confirmed",
            "text_trust_status": "verified",
            "search_eligible": True,
            "ingestion_pipeline_version": (
                "cupertino-source-repair-v1"
            ),
            "ingestion_audit": {
                "repair_method": "authoritative_docx_structure",
                "repair_applied_at": now,
                "source_artifact_id": RESPONSE_ARTIFACT,
                "response_segments": locators,
            },
        }
        responses[response_id] = latest_response
        comment.update({
            "verified_text": str(comment.get("original_text", "")),
            "source_status": "confirmed",
            "human_review_status": "confirmed",
            "verification_status": "confirmed",
            "text_trust_status": "verified",
            "search_eligible": True,
            "extraction_confidence": 1.0,
            "response_id": response_id,
            "match_status": "matched",
            "response_letter_round": "2",
            "issue_thread_id": thread_id,
            "issue_grouping_status": "human_verified",
            "issue_grouping_method": (
                "same_site_explicit_comment_and_response_sequence"
            ),
            "issue_status": "Responded",
            "issue_thread_events": [
                _event(
                    thread_id,
                    prior_response,
                    (
                        "2025-05-02"
                        if key == "planning-1"
                        else "2025-04-28"
                    ),
                )
            ],
            "ingestion_pipeline_version": (
                "cupertino-source-repair-v1"
            ),
        })
        audit = comment.setdefault("ingestion_audit", {})
        if isinstance(audit, dict):
            audit["verified_source_repair"] = {
                "applied_at": now,
                "authority": (
                    "original_city_docx"
                ),
                "latest_response_source": LATEST_RESPONSE_SOURCE,
                "preserved_original_text": True,
            }
            audit["uncertainty_reason"] = ""
        link = links.get(comment_id, {})
        link.update({
            "link_id": base.stable_id(
                "L", comment_id, response_id,
                "human-verified-source-repair",
            ),
            "comment_id": comment_id,
            "response_id": response_id,
            "match_status": "matched",
            "matching_method": (
                "explicit_comment_order_and_division_heading"
            ),
            "match_confidence": 1.0,
            "review_status": "confirmed",
            "verification_status": "confirmed",
            "pairing_evidence": (
                "Original City comment and response-letter division/order"
            ),
            "provenance": "human_verified_source_repair",
            "source_document": LATEST_RESPONSE_SOURCE,
            "comment_locator_json": comment.get(
                "source_locator_json", {},
            ),
            "response_locator_json": first,
            "source_location": str(
                comment.get("source_location", "")
            ),
            "ingestion_audit": {
                "repair_method": "authoritative_docx_structure",
                "repair_applied_at": now,
            },
        })
        links[comment_id] = link

    for duplicate_id, authority_id in SUPPRESS_TO.items():
        duplicate = comments.get(duplicate_id)
        if not duplicate:
            continue
        duplicate.update({
            "search_eligible": False,
            "occurrence_type": "duplicate_source_copy",
            "duplicate_of": authority_id,
            "source_status": "superseded_duplicate",
        })
        duplicate_audit = duplicate.setdefault(
            "ingestion_audit", {},
        )
        if isinstance(duplicate_audit, dict):
            duplicate_audit["verified_source_repair"] = {
                "decision": "suppressed_duplicate",
                "authoritative_comment_id": authority_id,
                "applied_at": now,
            }
        duplicate_link = links.get(duplicate_id)
        if duplicate_link:
            duplicate_link["review_status"] = "not_applicable"
            duplicate_link["verification_status"] = "superseded"

    for context_id in CONTEXT_ONLY:
        context = comments.get(context_id)
        if not context:
            continue
        context.update({
            "search_eligible": False,
            "occurrence_type": "context_only",
            "source_status": "context_only",
        })
        context_audit = context.setdefault("ingestion_audit", {})
        if isinstance(context_audit, dict):
            context_audit["verified_source_repair"] = {
                "decision": "context_only_not_a_government_comment",
                "applied_at": now,
            }

    # Older extracted responses remain immutable audit records but are not
    # independently searchable after their content is attached to a thread.
    cupertino_comment_ids = {
        comment_id for comment_id, row in comments.items()
        if str(row.get("city", "")).casefold() == "cupertino"
    }
    for response in responses.values():
        if (
            response.get("comment_id") in cupertino_comment_ids
            and response.get("response_id") not in updated_response_ids
        ):
            response["search_eligible"] = False
            response["occurrence_type"] = "thread_history_source"

    for item in dataset.get("review_items", []):
        if "cupertino" in str(
            item.get("source_document", "")
        ).casefold():
            item["decision"] = "superseded_by_verified_source_repair"
            item["decision_note"] = (
                "Resolved from original DOCX text and explicit response "
                "letter structure; raw extraction retained."
            )

    dataset["responses"] = list(responses.values())
    dataset["comment_response_links"] = list(links.values())
    metadata = dataset.setdefault("metadata", {})
    metadata["cupertino_source_repair"] = {
        "applied_at": now,
        "project": PROJECT,
        "authoritative_parent_comments": 3,
        "suppressed_duplicate_rows": len(SUPPRESS_TO),
        "context_only_rows": len(CONTEXT_ONLY),
        "latest_responses_upserted": 3,
    }
    return {
        "authoritative_comments": 3,
        "latest_responses_upserted": 3,
        "duplicates_suppressed": len(SUPPRESS_TO),
        "context_rows_suppressed": len(CONTEXT_ONLY),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=WORKSPACE / "phase2_dataset" / "dataset.json",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=WORKSPACE / "phase2_dataset" / "ingestion_artifacts",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    before = json.loads(args.dataset.read_text(encoding="utf-8"))
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    result = repair(dataset, args.artifact_root)
    if args.apply:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = args.dataset.with_name(
            f"{args.dataset.stem}.pre-cupertino-repair-{stamp}.json"
        )
        atomic_json(backup, before)
        atomic_json(args.dataset, dataset)
        result["backup_created"] = str(backup)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
