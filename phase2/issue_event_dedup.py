"""Build source-preserving issue timelines from cumulative review records.

Permit review exports often repeat an earlier issue before appending a later
round note.  One file may contain events 1-2 while a later file contains
events 1-4.  The production view should show four events, not six records,
while retaining both citations for events 1 and 2.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    from web_app.canonical_event import (
        NORMALIZATION_VERSION,
        canonical_event_fingerprint,
        classify_event_text_match,
        compatible_near_duplicate,
        high_confidence_text_extension,
        normalize_actor,
        normalize_event_type,
        negation_tokens as shared_negation_tokens,
        normalize_event_text,
        parameter_tokens as shared_parameter_tokens,
        text_similarity,
    )
except ImportError:  # pragma: no cover - direct execution from phase2/
    from canonical_event import (  # type: ignore
        NORMALIZATION_VERSION,
        canonical_event_fingerprint,
        classify_event_text_match,
        compatible_near_duplicate,
        high_confidence_text_extension,
        normalize_actor,
        normalize_event_type,
        negation_tokens as shared_negation_tokens,
        normalize_event_text,
        parameter_tokens as shared_parameter_tokens,
        text_similarity,
    )


PROGRESSION_MARKER = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:\([A-Za-z]\)\s*)?"
    r"(?P<label>PC\s*\d+(?:\s*(?:&|and|,)\s*(?:PC\s*)?\d+)*)"
    r"\s*[-:]\s*",
    re.IGNORECASE,
)
SUBMISSION_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<number>\d+)(?:st|nd|rd|th)\s+submission\b",
    re.IGNORECASE,
)


def _text(record: dict[str, Any]) -> str:
    return str(record.get("verified_text") or record.get("original_text") or "")


def _submission_label(record: dict[str, Any]) -> str:
    """Extract observation submission metadata without treating it as identity."""
    for value in (
        record.get("event_submission"), record.get("submission"),
        record.get("document_submission"), record.get("submission_number"),
    ):
        text = str(value or "").strip()
        if text:
            match = re.search(r"\d+", text)
            return match.group(0) if match else text
    source = str(record.get("source_document", ""))
    match = SUBMISSION_PATH_PATTERN.search(source)
    return match.group("number") if match else ""


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Spreadsheet/XML exports vary the case of these escaped line breaks
    # (``_x000D_`` vs ``_x000d_``).  Treat both as whitespace so a copied row
    # does not become a second event solely because of the escape casing.
    text = re.sub(r"_x000[dDaA]_", " ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def event_role_family(event: dict[str, Any]) -> str:
    """Return the stable role used for event identity.

    The extraction labels are not stable across exports: the same reviewer
    row can be emitted as ``government_comment`` in one file and
    ``reviewer_follow_up`` in another.  Applicant responses have the same
    problem with ``current_applicant_response``.  Those labels describe the
    presentation, not a different occurrence, so deduplication must use the
    role family while retaining the original event_type for display/audit.
    """
    role = _normalized(event.get("actor_role", ""))
    event_type = _normalized(event.get("event_type", ""))
    if role in {"company", "applicant", "applicant_response"} or event_type in {
        "applicant_response", "current_applicant_response",
    }:
        return "company"
    if role in {"government", "reviewer", "city"} or event_type in {
        "government_comment", "reviewer_follow_up", "discussion_note",
    }:
        return "government"
    return role or event_type or "unknown"


def normalized_event_type(event: dict[str, Any]) -> str:
    """Return event type for identity while retaining role-family fallback.

    Older exports sometimes label the same reviewer-side row as a government
    comment in one file and a follow-up in another.  The exact type is kept on
    the canonical payload; the role family is used only as a conservative
    compatibility fallback when the body/date prove that they are copies.
    """
    value = normalize_event_type(event.get("event_type", ""))
    if value == "unknown":
        role = _normalized(event.get("actor_role", ""))
        if role in {"company", "applicant"}:
            return "applicant_response"
        if role in {"government", "reviewer", "city"}:
            return "government_comment"
    return value


def _event_text_identity(value: Any) -> str:
    """Use the one versioned identity normalizer shared by repair and runtime."""
    return normalize_event_text(value)


def _marker(value: str) -> str:
    numbers = re.findall(r"\d+", value)
    return "PC" + "&PC".join(numbers) if numbers else _normalized(value)


def _stable_id(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _site_key(record: dict[str, Any]) -> str:
    city = _normalized(record.get("city"))
    project_id = str(record.get("project_id", "")).strip()
    if project_id:
        return f"{city}|project:{project_id}"
    source = str(record.get("source_document", "")).split(" | ", 1)[0]
    parts = Path(source).as_posix().split("/")
    if "comments&response" in parts:
        index = parts.index("comments&response") + 1
        if index < len(parts):
            return f"{city}|folder:{parts[index].casefold()}"
    project = _normalized(
        record.get("property_project")
        or record.get("property")
        or record.get("application_number")
    )
    return f"{city}|project:{project}" if project else ""


def _discipline(record: dict[str, Any]) -> str:
    value = _normalized(record.get("discipline"))
    if value in {"", "unknown", "uncategorized"}:
        return ""
    # Response forms commonly prefix the actual discipline with a workflow
    # label such as ``BPC WC3 1 : Building``.  That label must not split the
    # response-bearing copy from the Building review comment.
    known = (
        "architectural", "building", "civil", "electrical", "fire",
        "mechanical", "planning", "plumbing", "public works", "structural",
        "zoning",
    )
    for discipline in known:
        if re.search(rf"(?:^|\b|:)\s*{re.escape(discipline)}\s*$", value):
            return discipline
    return value


def _parameters(value: str) -> set[str]:
    return set(shared_parameter_tokens(value))


def split_progression_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Split cumulative ``PC1 ... PC2 ...`` text without changing raw text.

    The returned event text is a presentation/indexing layer.  The immutable
    ``original_text`` and ``verified_text`` fields remain untouched.
    """
    value = _text(record).strip()
    if not value:
        return []
    matches = list(PROGRESSION_MARKER.finditer(value))
    if not matches:
        return [{
            "event_round_marker": "",
            "event_type": "government_comment",
            "effective_round": str(record.get("review_round", "")),
            "observed_in_document_round": str(record.get("review_round", "")),
            "exact_text": value,
        }]

    events: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        body = value[match.end():end].strip(" \t\r\n-:;")
        if index == 0:
            prefix = value[:match.start()].strip()
            # Numbered/lettered list prefixes belong to the row, not the
            # substantive government requirement.
            prefix = re.sub(r"^(?:[A-Za-z]|\d+)[.)]\s*$", "", prefix).strip()
            if prefix:
                body = f"{prefix} {body}".strip()
        if not body:
            continue
        marker = _marker(match.group("label"))
        marker_numbers = {int(item) for item in re.findall(r"\d+", marker)}
        effective_round = str(min(marker_numbers)) if marker_numbers else str(
            record.get("review_round", "")
        )
        events.append({
            "event_round_marker": marker,
            "event_type": "government_comment" if 1 in marker_numbers else "reviewer_follow_up",
            "effective_round": effective_round,
            "observed_in_document_round": str(record.get("review_round", "")),
            "exact_text": body,
        })
    return events or [{
        "event_round_marker": "",
        "event_type": "government_comment",
        "effective_round": str(record.get("review_round", "")),
        "observed_in_document_round": str(record.get("review_round", "")),
        "exact_text": value,
    }]


def issue_anchor(record: dict[str, Any]) -> str:
    events = split_progression_events(record)
    # The same visible row is often exported once with a ProjectDox/markup
    # prefix and once without it (and may contain ``_x000D_`` line-break
    # escapes).  Use the same harmless-prefix normalization as event identity
    # when assigning issue threads, otherwise those copies get different
    # thread IDs before the event-level deduper can merge them.
    return _event_text_identity(events[0]["exact_text"] if events else _text(record))


def _compatible_anchor(first: dict[str, Any], second: dict[str, Any]) -> bool:
    left, right = issue_anchor(first), issue_anchor(second)
    if not left or not right:
        return False
    if left == right:
        return True
    if _parameters(left) != _parameters(right):
        return False
    left_tokens, right_tokens = set(left.split()), set(right.split())
    containment = len(left_tokens & right_tokens) / max(
        1, min(len(left_tokens), len(right_tokens))
    )
    length_ratio = max(len(left), len(right)) / max(1, min(len(left), len(right)))
    return (
        length_ratio <= 1.35
        and containment >= 0.96
        and SequenceMatcher(None, left, right).ratio() >= 0.94
    )


def _compatible_issue(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if not _site_key(first) or _site_key(first) != _site_key(second):
        return False
    first_number = _normalized(first.get("comment_number"))
    second_number = _normalized(second.get("comment_number"))
    first_discipline, second_discipline = _discipline(first), _discipline(second)
    same_number = bool(first_number and first_number == second_number)
    compatible_discipline = (
        not first_discipline
        or not second_discipline
        or first_discipline == second_discipline
    )
    if not same_number and not compatible_discipline:
        return False
    if first_number and second_number and first_number != second_number:
        return False
    return _compatible_anchor(first, second)


def _trusted_issue_record(record: dict[str, Any]) -> bool:
    """Allow verified source rows and confirmed response-form copies."""
    return bool(
        record.get("text_trust_status") == "verified"
        or record.get("verified_text")
        or record.get("source_status") in {"confirmed", "verified"}
        or record.get("verification_status") == "confirmed"
        or record.get("human_review_status") == "confirmed"
    )


def _compatible_discipline(first: dict[str, Any], second: dict[str, Any]) -> bool:
    left, right = _discipline(first), _discipline(second)
    return not left or not right or left == right


def assign_issue_threads(comments: list[dict[str, Any]]) -> dict[str, int]:
    """Assign one thread to cumulative snapshots of the same site issue."""
    candidates = [
        row for row in comments
        if _text(row).strip()
        and row.get("duplicate_status") != "hierarchical_subpoint"
        and _trusted_issue_record(row)
    ]
    by_id = {str(row.get("comment_id", "")): row for row in candidates}
    parent = {record_id: record_id for record_id in by_id}
    thread_projects: dict[str, set[str]] = defaultdict(set)
    for row in candidates:
        old_thread = str(row.get("issue_thread_id", ""))
        if old_thread:
            thread_projects[old_thread].add(_site_key(row))

    def root(record_id: str) -> str:
        while parent[record_id] != record_id:
            parent[record_id] = parent[parent[record_id]]
            record_id = parent[record_id]
        return record_id

    def union(left: str, right: str) -> None:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[right_root] = left_root

    # Preserve explicit spreadsheet/history threads first.
    existing: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record_id, row in by_id.items():
        thread_id = str(row.get("issue_thread_id", ""))
        if thread_id:
            existing[(_site_key(row), thread_id)].append(record_id)
    for record_ids in existing.values():
        for record_id in record_ids[1:]:
            union(record_ids[0], record_id)

    # Exact issue bodies are the strongest progression key. Printed row
    # numbers often change between PC1, PC2, and a response-letter export, so
    # they cannot be required for this pass. PC markers are removed by
    # ``issue_anchor``; numeric design parameters remain part of the anchor.
    anchor_blocks: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record_id, row in by_id.items():
        anchor = issue_anchor(row)
        if anchor:
            anchor_blocks[(_site_key(row), anchor)].append(record_id)
    for record_ids in anchor_blocks.values():
        for index, first_id in enumerate(record_ids):
            for second_id in record_ids[index + 1:]:
                if _compatible_discipline(by_id[first_id], by_id[second_id]):
                    union(first_id, second_id)

    # Printed comment number remains a useful secondary key for cumulative
    # rows with small extraction differences.
    blocks: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record_id, row in by_id.items():
        number = _normalized(row.get("comment_number"))
        block = f"number:{number}" if number else f"discipline:{_discipline(row)}"
        blocks[(_site_key(row), block)].append(record_id)
    for record_ids in blocks.values():
        for index, first_id in enumerate(record_ids):
            for second_id in record_ids[index + 1:]:
                if _compatible_issue(by_id[first_id], by_id[second_id]):
                    union(first_id, second_id)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record_id, row in by_id.items():
        groups[root(record_id)].append(row)

    multi_record_threads = 0
    comments_grouped = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: (
            int(re.search(r"\d+", str(row.get("review_round", ""))).group())
            if re.search(r"\d+", str(row.get("review_round", ""))) else 10**9,
            str(row.get("source_document", "")),
            str(row.get("comment_id", "")),
        ))
        representative = rows[0]
        existing_ids = sorted({
            str(row.get("issue_thread_id", "")) for row in rows
            if str(row.get("issue_thread_id", ""))
        })
        representative_thread = str(representative.get("issue_thread_id", ""))
        # An old dataset may have reused a thread id across projects.  Reuse
        # it only when the project identity is unambiguous; otherwise create a
        # project-scoped stable id so timelines can never cross permit cases.
        existing_thread_projects = {
            _site_key(row) for row in rows if str(row.get("issue_thread_id", ""))
        }
        reusable_thread = (
            representative_thread
            and len(existing_thread_projects) == 1
            and len(thread_projects.get(representative_thread, set())) == 1
        )
        thread_id = (representative_thread if reusable_thread else "") or (existing_ids[0] if reusable_thread and existing_ids else _stable_id(
            "T",
            _site_key(representative),
            _discipline(representative),
            issue_anchor(representative),
        ))
        for row in rows:
            row["issue_thread_id"] = thread_id
            row["issue_grouping_status"] = "cross_document_progression"
            row["issue_grouping_method"] = "same_site_issue_anchor_cumulative_history"
        multi_record_threads += 1
        comments_grouped += len(rows)

    # Some response-only exports have no trusted parent comment body but do
    # preserve a complete response/follow-up occurrence. Attach such source
    # rows only when substantive role/text evidence resolves to exactly one
    # site-scoped thread. One missing date may inherit the trusted owner's
    # date; two conflicting explicit dates never match. Generic replies such
    # as "Noted" are intentionally excluded.
    strong_event_owners: dict[
        tuple[str, str, str], list[tuple[str, str, str]]
    ] = defaultdict(list)
    for row in candidates:
        thread_id = str(row.get("issue_thread_id", ""))
        if not thread_id:
            continue
        for event in _raw_events(row, thread_id):
            body = str(event.get("exact_text") or "")
            date = _date_key(event)
            if not body or not date or _is_generic_event_text(body):
                continue
            key = (
                _site_key(row), event_role_family(event),
                normalize_event_text(body),
            )
            strong_event_owners[key].append((
                thread_id,
                date,
                normalize_actor(event.get("actor") or event.get("reviewer") or ""),
            ))

    event_alias_rows_grouped = 0
    candidate_ids = {str(row.get("comment_id", "")) for row in candidates}
    for row in comments:
        if str(row.get("comment_id", "")) in candidate_ids:
            continue
        owner_threads: set[str] = set()
        # A quarantined legacy/source-only row frequently has no explicit
        # ``issue_thread_events`` array.  Reconstruct its raw event view from
        # immutable text so it can still become another source occurrence of
        # a trusted canonical event; it never becomes authoritative on its
        # own.
        for event in _raw_events(row, str(row.get("issue_thread_id") or "")):
            body = str(event.get("exact_text") or "")
            date = _date_key(event)
            if not body or _is_generic_event_text(body):
                continue
            key = (
                _site_key(row), event_role_family(event),
                normalize_event_text(body),
            )
            actor = normalize_actor(event.get("actor") or event.get("reviewer") or "")
            compatible_owner_rows = {
                (thread_id, owner_date)
                for thread_id, owner_date, owner_actor in strong_event_owners.get(key, [])
                if (not date or not owner_date or date == owner_date)
                and (not actor or not owner_actor or actor == owner_actor)
            }
            # An undated source copy cannot choose between the same text that
            # genuinely occurred on two different dates in one thread.
            owner_dates = {owner_date for _thread_id, owner_date in compatible_owner_rows if owner_date}
            compatible_owners = {thread_id for thread_id, _owner_date in compatible_owner_rows}
            if not date and len(owner_dates) != 1:
                compatible_owners = set()
            if len(compatible_owners) == 1:
                owner_threads.update(compatible_owners)
        if len(owner_threads) == 1:
            row["issue_thread_id"] = next(iter(owner_threads))
            row["issue_grouping_status"] = "source_occurrence_alias"
            row["issue_grouping_method"] = (
                "same_site_role_date_exact_event" if any(
                    _date_key(event)
                    for event in _raw_events(row, str(row.get("issue_thread_id") or ""))
                ) else "same_site_role_exact_event_inherited_date"
            )
            event_alias_rows_grouped += 1
    return {
        "candidate_comments": len(candidates),
        "multi_record_threads": multi_record_threads,
        "comments_grouped": comments_grouped,
        "event_alias_rows_grouped": event_alias_rows_grouped,
    }


def _date_key(event: dict[str, Any]) -> str:
    # Parse fields in provenance order rather than concatenating them.  A
    # timestamp such as ``2025-09-11T17:54`` must win over the workbook's
    # 09/24 export date; concatenating first caused the regex to skip the
    # timestamp at the ``11T`` boundary and silently choose the wrong date.
    values: list[Any] = [
        event.get("occurred_at"), event.get("occurred_at_label"),
        event.get("time_label"),
    ]
    # A containing workbook/PDF date is an observation date, not necessarily
    # the date of the historical event copied into it.  Older backfills placed
    # that fallback in ``event_date``; honor it only when provenance says it
    # came from event content/header rather than the source container.
    weak_sources = {
        "source_document_date", "document_date", "pdf_metadata", "file_date",
        "filename", "workbook_export", "filesystem_mtime",
    }
    event_date_source = str(event.get("event_date_source", "")).strip().casefold()
    if event_date_source not in weak_sources:
        values.extend((
            event.get("event_date"), event.get("event_date_iso"),
            event.get("event_date_raw"),
        ))
    for raw in values:
        value = str(raw or "")
        iso = re.search(
            r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", value
        )
        if iso:
            return f"{iso.group(1)}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
        slash = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2,4})(?!\d)", value)
        if slash:
            year = int(slash.group(3))
            if year < 100:
                year += 2000
            return f"{year:04d}-{int(slash.group(1)):02d}-{int(slash.group(2)):02d}"
    return ""


def event_identity(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Identify one logical event within a thread.

    Exact copies in multiple files merge when their type, effective round (or
    date/explicit PC marker), and text match.  The effective round is required
    here: the same requirement repeated in PC1, PC2, and PC3 is one issue but
    three distinct timeline occurrences.
    """
    text = _event_text_identity(event.get("exact_text") or event.get("text") or "")
    if not text:
        return None
    date = _date_key(event)
    marker = _marker(str(event.get("event_round_marker", "")))
    # A repeated applicant response or reviewer follow-up with the same
    # printed row/text is still a new timeline event when it has a different
    # explicit timestamp.  Government comments are allowed to carry forward
    # the original PC marker through later cumulative documents, so retain
    # their historical marker-only behavior below.
    event_type = normalized_event_type(event)
    role = event_role_family(event)
    actor = normalize_actor(event.get("actor") or event.get("reviewer") or "")
    printed_id = str(event.get("printed_comment_id") or event.get("comment_number") or "").strip()
    if marker:
        # Government PC markers describe the event's effective round.  A PC1
        # comment copied into a later response package keeps the PC1 identity;
        # the later document date is observation metadata.  Response/follow-up
        # attempts, however, use their actual event date when one exists.
        occurrence = f"round:{marker}"
        # Without a printed row id, different known dates are conservatively
        # retained as distinct events.  A printed id is strong evidence that a
        # later cumulative document copied the same historical government row.
        if role == "company" or event_type == "reviewer_follow_up" or (
            role == "government" and not printed_id
        ):
            if date:
                occurrence += f"|date:{date}"
        if actor:
            occurrence += f"|actor:{actor}"
        return event_type, occurrence, text
    round_value = str(
        event.get("effective_round")
        or event.get("review_round")
        or event.get("observed_in_document_round")
        or ""
    ).strip()
    if date:
        occurrence = f"date:{date}"
        if actor:
            occurrence += f"|actor:{actor}"
        return event_type, occurrence, text
    if round_value:
        occurrence = f"round:{_marker(round_value)}"
        if actor:
            occurrence += f"|actor:{actor}"
        # Printed IDs are only a fallback for undated events.  They are not
        # part of the identity when a real event date is available.
        if printed_id:
            occurrence += f"|printed_id:{normalize_event_text(printed_id)}"
        return event_type, occurrence, text
    # Some checklist PDFs do not expose a review-round/date at all, but do
    # preserve the printed row number.  Within one already site-scoped thread,
    # that row id plus the exact normalized text is still a safe copy key.
    if printed_id:
        return event_type, f"printed_id:{normalize_event_text(printed_id)}", text
    return None


_GENERIC_EVENT_TEXT = frozenset({
    "noted", "revised", "done", "addressed", "complete", "completed",
    "see plans", "see revised", "see updated", "ok", "okay",
})


def _is_generic_event_text(value: Any) -> bool:
    normalized = normalize_event_text(value)
    return normalized in _GENERIC_EVENT_TEXT or len(normalized.split()) <= 2


def _identity_context(event: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return blocking dimensions used before any near-text comparison."""
    role = event_role_family(event)
    event_type = normalized_event_type(event)
    date = _date_key(event)
    marker = _marker(str(event.get("event_round_marker", "")))
    round_value = _effective_event_round(event)
    return role, event_type, date, marker or round_value


def _parent_context_values(event: dict[str, Any]) -> set[str]:
    """Return explicit parent/row anchors for short generic event safety."""
    values = {
        str(event.get(field) or "").strip().casefold()
        for field in (
            "parent_event_id", "parent_comment_id", "linked_comment_id",
            "response_id", "printed_comment_id", "comment_number",
        )
    }
    return {value for value in values if value}


def _candidate_block_keys(event: dict[str, Any]) -> set[tuple[str, str, str]]:
    """Block within one issue while allowing a dated copy to absorb an undated one."""
    role, event_type, date, marker_or_round = _identity_context(event)
    keys: set[tuple[str, str, str]] = set()
    if date:
        keys.add((role, f"date:{date}", event_type))
        keys.add((role, f"date:{date}", ""))
    if marker_or_round:
        keys.add((role, f"round:{marker_or_round}", event_type))
        keys.add((role, f"round:{marker_or_round}", ""))
    if not keys:
        keys.add((role, "unknown", event_type))
    return keys


def _events_can_merge(
    left: dict[str, Any], right: dict[str, Any], *, allow_extension: bool = True,
) -> tuple[bool, str]:
    """Conservative event-level merge decision inside one issue thread."""
    left_type = normalized_event_type(left)
    right_type = normalized_event_type(right)
    left_role, _, left_date, left_round = _identity_context(left)
    right_role, _, right_date, right_round = _identity_context(right)
    if left_role != right_role:
        return False, "DISTINCT"
    # A government_comment/follow-up label mismatch is a known export alias;
    # the same body/date in one issue is safe to collapse.  Company and
    # reviewer-side roles never cross.
    if left_type != right_type and not (
        left_role == "government" and right_role == "government"
    ):
        return False, "DISTINCT"
    left_actor = normalize_actor(left.get("actor") or left.get("reviewer") or "")
    right_actor = normalize_actor(right.get("actor") or right.get("reviewer") or "")
    if left_actor and right_actor and left_actor != right_actor:
        return False, "DISTINCT"
    if left_date and right_date and left_date != right_date:
        return False, "DISTINCT"
    # A same-date copied row may carry a different PC label because it was
    # reproduced in a cumulative workbook.  The date + text identify the
    # event; retain both labels as observed metadata.  Different dates still
    # remain distinct above, and undated different rounds remain conservative.
    if left_round and right_round and left_round != right_round:
        if not (left_date and right_date and left_date == right_date):
            return False, "REISSUE"
    if left_date and right_date:
        context_compatible = True
    elif bool(left_date) != bool(right_date):
        # One copied occurrence may omit the event date entirely.  This is a
        # compatible candidate context, not sufficient proof by itself: the
        # strict text/parameter/negation classifier below still has to return
        # an exact or high-confidence duplicate, and explicit actor/round
        # conflicts above still block the merge.
        context_compatible = True
    else:
        # Some narrative PDFs/checklists preserve the issue thread and exact
        # body but expose neither a date nor a usable round marker.  Within
        # that already issue-scoped block, an exact non-generic body is still
        # strong evidence of one copied event.  Short status replies such as
        # "Noted" remain conservative and require a printed-row anchor.
        context_compatible = bool(
            (left_round and right_round and left_round == right_round)
            or (
                not left_date and not right_date
                and normalize_event_text(left_text := (left.get("exact_text") or left.get("text") or ""))
                == normalize_event_text(right_text := (right.get("exact_text") or right.get("text") or ""))
                and not _is_generic_event_text(left_text)
            )
        )
    if not context_compatible:
        return False, "DISTINCT"
    left_text = left.get("exact_text") or left.get("text") or ""
    right_text = right.get("exact_text") or right.get("text") or ""
    left_norm = normalize_event_text(left_text)
    right_norm = normalize_event_text(right_text)
    match_class, _signals = classify_event_text_match(left_text, right_text)
    if match_class == "EXACT_DUPLICATE":
        # Undated generic status text needs a parent/printed-row anchor.  It
        # can answer multiple independent comments in one issue package.  A
        # date alone is insufficient; require a shared parent/printed row.
        if _is_generic_event_text(left_text) and not (
            _parent_context_values(left) & _parent_context_values(right)
        ):
            return False, "POSSIBLE_DUPLICATE"
        return True, "EXACT_DUPLICATE"
    if match_class == "HIGH_CONFIDENCE_DUPLICATE" and (
        (left_date and right_date) or (bool(left_date) != bool(right_date))
    ):
        if _is_generic_event_text(left_text) or _is_generic_event_text(right_text):
            return False, "POSSIBLE_DUPLICATE"
        return True, "HIGH_CONFIDENCE_DUPLICATE"
    if allow_extension and (left_date or right_date):
        if high_confidence_text_extension(left_text, right_text) or high_confidence_text_extension(right_text, left_text):
            return True, "HIGH_CONFIDENCE_DUPLICATE"
    return False, match_class if match_class in {"POSSIBLE_DUPLICATE", "REISSUE"} else "DISTINCT"


def _event_sort_key(event: dict[str, Any]) -> tuple[int, Any, int, str]:
    marker_numbers = [
        int(item) for item in re.findall(
            r"\d+", str(event.get("event_round_marker", ""))
        )
    ]
    if marker_numbers:
        date = _date_key(event)
        return (
            0, (min(marker_numbers), date), int(event.get("source_order") or 0),
            str(event.get("event_id", "")),
        )
    date = _date_key(event)
    if date:
        return (
            1, date, int(event.get("source_order") or 0),
            str(event.get("event_id", "")),
        )
    review_round = re.search(r"\d+", str(event.get("review_round", "")))
    return (
        2, int(review_round.group()) if review_round else 10**9,
        int(event.get("source_order") or 0), str(event.get("event_id", "")),
    )


def _event_source_preference(event: dict[str, Any]) -> tuple[int, int, str]:
    """Prefer the source round that actually introduced a numbered PC event.

    A PC2 cumulative export can repeat both ``PC1`` and ``PC2``.  The PC1
    event should keep the PC1 document's round metadata even when the later
    cumulative file happens to appear first in the dataset.
    """
    marker_numbers = [
        int(item) for item in re.findall(
            r"\d+", str(event.get("event_round_marker", ""))
        )
    ]
    review_match = re.search(r"\d+", str(event.get("review_round", "")))
    review_round = int(review_match.group()) if review_match else 10**9
    marker_round = min(marker_numbers) if marker_numbers else review_round
    return (
        0 if review_round == marker_round else 1,
        abs(review_round - marker_round),
        str(event.get("source_document", "")),
    )


def _event_metadata_quality(event: dict[str, Any]) -> tuple[int, int, int, int, int, int, str]:
    """Rank the best display/primary occurrence without discarding aliases."""
    trust = str(
        event.get("text_trust_status") or event.get("source_status")
        or event.get("human_review_status") or ""
    ).strip().casefold()
    text = str(event.get("exact_text") or event.get("text") or "")
    source_preference = _event_source_preference(event)
    return (
        0 if trust in {"verified", "confirmed"} else 1,
        0 if _date_key(event) else 1,
        0 if normalize_actor(event.get("actor") or event.get("reviewer") or "") else 1,
        source_preference[0],
        1 if text.lstrip().casefold().startswith(("markup ", "comment ")) else 0,
        -len(normalize_event_text(text)),
        source_preference[2],
    )


def _event_labels(event: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for field in ("labels", "event_labels", "record_labels"):
        raw = event.get(field, [])
        if isinstance(raw, (list, tuple, set)):
            values.update(str(item).strip() for item in raw if str(item).strip())
        elif str(raw or "").strip():
            values.add(str(raw).strip())
    for field in ("event_round_marker", "record_label", "markup_label"):
        if str(event.get(field) or "").strip():
            values.add(str(event[field]).strip())
    return values


def _merge_event_metadata(
    target: dict[str, Any], incoming: dict[str, Any], decision: str,
) -> None:
    """Union evidence/labels and inherit the strongest supported metadata."""
    target_actor = str(target.get("actor") or target.get("reviewer") or "")
    incoming_actor = str(incoming.get("actor") or incoming.get("reviewer") or "")
    target_round = str(target.get("effective_round") or "")
    incoming_round = str(incoming.get("effective_round") or "")
    target_printed = str(target.get("printed_comment_id") or "")
    incoming_printed = str(incoming.get("printed_comment_id") or "")
    preserved = {
        "source_occurrences": list(target.get("source_occurrences", []) or []),
        "source_occurrence_ids": list(target.get("source_occurrence_ids", []) or []),
        "merged_event_ids": list(target.get("merged_event_ids", []) or []),
        "observed_in_document_rounds": list(target.get("observed_in_document_rounds", []) or []),
        "observed_in_submissions": list(target.get("observed_in_submissions", []) or []),
        "event_labels": sorted(_event_labels(target) | _event_labels(incoming)),
        "merged_event_types": list(dict.fromkeys([
            *(target.get("merged_event_types", []) or [str(target.get("event_type", ""))]),
            *(incoming.get("merged_event_types", []) or [str(incoming.get("event_type", ""))]),
        ])),
    }
    if _event_metadata_quality(incoming) < _event_metadata_quality(target):
        target.clear()
        target.update(deepcopy(incoming))
    for field, value in preserved.items():
        target[field] = value

    target["actor"] = str(target.get("actor") or incoming.get("actor") or incoming.get("reviewer") or "")
    target["event_date"] = str(target.get("event_date") or incoming.get("event_date") or "")
    target["event_date_iso"] = str(target.get("event_date_iso") or incoming.get("event_date_iso") or "")
    target["event_date_raw"] = str(target.get("event_date_raw") or incoming.get("event_date_raw") or "")
    target["event_date_source"] = str(target.get("event_date_source") or incoming.get("event_date_source") or "")
    target["effective_round"] = str(target.get("effective_round") or incoming.get("effective_round") or "")
    target["event_round_marker"] = str(target.get("event_round_marker") or incoming.get("event_round_marker") or "")
    target["printed_comment_id"] = str(target.get("printed_comment_id") or incoming.get("printed_comment_id") or "")
    target["event_submission"] = str(
        target.get("event_submission") or incoming.get("event_submission")
        or _submission_label(incoming) or ""
    )
    target["dedup_decision"] = decision

    target["observed_in_document_rounds"] = sorted(set(
        target.get("observed_in_document_rounds", []) or []
    ) | set(incoming.get("observed_in_document_rounds", []) or []) | {
        value for value in (
            target.get("observed_in_document_round"), target.get("review_round"),
            incoming.get("observed_in_document_round"), incoming.get("review_round"),
        ) if value
    })
    target["observed_in_submissions"] = sorted(set(
        target.get("observed_in_submissions", []) or []
    ) | set(incoming.get("observed_in_submissions", []) or []) | {
        value for value in (
            target.get("event_submission"), incoming.get("event_submission"),
            _submission_label(incoming),
        ) if value
    })
    target["actor_variants"] = sorted({
        normalize_actor(value) for value in (target_actor, incoming_actor)
        if normalize_actor(value)
    })
    target["effective_round_variants"] = sorted({
        str(value).strip() for value in (target_round, incoming_round)
        if str(value or "").strip()
    })
    target["printed_comment_id_variants"] = sorted({
        str(value).strip() for value in (target_printed, incoming_printed)
        if str(value or "").strip()
    })


def _effective_event_round(event: dict[str, Any]) -> str:
    marker = str(event.get("event_round_marker", "")).strip()
    if marker:
        return _marker(marker)
    return _marker(str(
        event.get("effective_round")
        or event.get("review_round")
        or event.get("observed_in_document_round")
        or ""
    ))


def _near_duplicate_review_queue(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return conservative near-duplicate candidates without merging them."""
    queue: list[dict[str, Any]] = []
    for index, left in enumerate(events):
        left_text = left.get("exact_text") or left.get("text") or ""
        left_normalized = normalize_event_text(left_text)
        if not left_normalized:
            continue
        for right in events[index + 1:]:
            right_text = right.get("exact_text") or right.get("text") or ""
            right_normalized = normalize_event_text(right_text)
            if not right_normalized:
                continue
            if event_role_family(left) != event_role_family(right):
                continue
            left_date, right_date = _date_key(left), _date_key(right)
            if left_date and right_date and left_date != right_date:
                continue
            left_round = _effective_event_round(left)
            right_round = _effective_event_round(right)
            if not (left_date and right_date) and (
                left_round and right_round and left_round != right_round
            ):
                continue
            can_merge, decision = _events_can_merge(left, right)
            if can_merge or decision != "POSSIBLE_DUPLICATE":
                continue
            similarity = text_similarity(left_text, right_text)
            queue.append({
                "decision": "POSSIBLE_DUPLICATE",
                "left_event_id": str(left.get("event_id", "")),
                "right_event_id": str(right.get("event_id", "")),
                "effective_round": left_round or right_round,
                "similarity": round(similarity, 4),
                "left_normalized_text": left_normalized,
                "right_normalized_text": right_normalized,
                "reason": (
                    "Same issue and compatible event context, but the evidence "
                    "is not strong enough for an automatic canonical merge."
                ),
            })
    return queue


def _event_occurrence(comment: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    source_document = str(event.get("source_document") or comment.get("source_document", ""))
    source_location = deepcopy(
        event.get("source_location")
        or event.get("source_locator_json")
        or comment.get("source_locator_json")
        or {}
    )
    exact_text = str(event.get("exact_text") or event.get("text") or "")
    source_object_reference = str(
        comment.get("source_object_reference")
        or (comment.get("source_metadata") or {}).get("view_value", "")
    )
    # A missing locator is an uncertainty about the extraction, not proof that
    # two rows are the same physical observation. Include the source row id in
    # that fallback so an unlocated row is never silently discarded.
    normalized_exact_text = normalize_event_text(exact_text)
    role_context = event_role_family(event)
    marker_context = _marker(str(event.get("event_round_marker", "")))
    if marker_context:
        occurrence_context = f"{role_context}|marker:{marker_context}"
    else:
        occurrence_context = "|".join((
            role_context,
            f"date:{_date_key(event)}" if _date_key(event) else "",
            f"round:{_effective_event_round(event)}" if _effective_event_round(event) else "",
        )).strip("|")
    if source_location:
        # Preserve the original ID contract for located observations so a
        # repair does not manufacture artificial lost/new occurrences.
        occurrence_id = _stable_id(
            "SO", source_document, repr(source_location), occurrence_context,
            normalized_exact_text,
        )
    elif source_object_reference:
        occurrence_id = _stable_id(
            "SO", source_document, "", source_object_reference, occurrence_context,
            normalized_exact_text,
        )
    else:
        occurrence_id = _stable_id(
            "SO", source_document, f"comment:{comment.get('comment_id', '')}", occurrence_context,
            normalized_exact_text,
        )
    return {
        "source_occurrence_id": occurrence_id,
        "occurrence_id": occurrence_id,
        "comment_id": str(comment.get("comment_id", "")),
        "event_id": str(event.get("event_id", "")),
        "raw_event_id": str(event.get("event_id", "")),
        "occurrence_identity_context": occurrence_context,
        "source_document": source_document,
        "source_location": source_location,
        # Keep the physical row/object identity separate from the logical PC
        # marker.  A cumulative PC2 form may show a PC1 carry-forward and a
        # new PC2 instruction in the same row; both must remain auditable.
        "printed_comment_id": str(comment.get("comment_number", "")),
        "source_object_reference": source_object_reference,
        "observed_in_document_round": str(
            event.get("observed_in_document_round")
            or comment.get("document_round")
            or comment.get("review_round", "")
        ),
        "event_submission": _submission_label(event) or _submission_label(comment),
        "observed_in_submissions": sorted({
            value for value in (_submission_label(event), _submission_label(comment)) if value
        }),
        "source_document_date": str(
            event.get("source_document_date")
            or comment.get("source_document_date", "")
        ),
        "event_date": str(event.get("event_date") or comment.get("event_date", "")),
        "event_date_source": str(
            event.get("event_date_source") or comment.get("event_date_source", "")
        ),
        "event_type": normalized_event_type(event),
        "actor": str(event.get("actor") or event.get("reviewer") or comment.get("reviewer", "")),
        "exact_text": exact_text,
        "document_date": deepcopy(
            event.get("document_date") or comment.get("document_date") or {}
        ),
    }


def _occurrence_merge_key(occurrence: dict[str, Any]) -> tuple[str, str, str]:
    """Return a physical-observation key without collapsing unknown rows.

    When a parser supplies a locator or object reference, those identify the
    same source cell/region across extraction runs. If both are absent, keep
    the source row id in the key: merging such rows would be an unsafe loss of
    provenance rather than a true duplicate decision.
    """
    source_document = str(occurrence.get("source_document", ""))
    source_location = occurrence.get("source_location") or {}
    source_object = str(occurrence.get("source_object_reference", ""))
    row_key = ""
    if not source_location and not source_object:
        row_key = str(occurrence.get("comment_id", ""))
    return source_document, repr(source_location), source_object or row_key


def _raw_events(comment: dict[str, Any], thread_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    progression = split_progression_events(comment)
    for index, event in enumerate(progression, 1):
        normalized = normalize_event_text(event.get("exact_text", ""))
        marker = str(event.get("event_round_marker", ""))
        effective_round = str(
            event.get("effective_round") or comment.get("review_round", "")
        )
        occurrence_marker = marker or f"round:{effective_round or 'unknown'}"
        event_payload = {
            **event,
            "event_id": _stable_id(
                "E", thread_id, "progression", occurrence_marker, normalized,
            ),
            "issue_thread_id": thread_id,
            "actor_role": "government",
            "actor": str(comment.get("reviewer", "")),
            # review_round is the round of the source document.  The event's
            # PC marker remains available independently as effective_round.
            # This prevents a PC1 carry-forward observed in a PC2 file from
            # being presented as though its source document were Round 1.
            "review_round": str(
                event.get("observed_in_document_round")
                or comment.get("document_round")
                or comment.get("review_round", "")
            ),
            "effective_round": effective_round,
            "observed_in_document_round": str(
                event.get("observed_in_document_round")
                or comment.get("document_round")
                or comment.get("review_round", "")
            ),
            "source_order": index,
            "source_document": str(comment.get("source_document", "")),
            "source_location": deepcopy(comment.get("source_locator_json") or {}),
            "source_document_date": str(comment.get("source_document_date", "")),
            "document_date": deepcopy(comment.get("document_date") or {}),
            "event_date": str(comment.get("event_date", "")),
            "event_date_iso": str(comment.get("event_date_iso", "")),
            "event_date_raw": str(comment.get("event_date_raw", "")),
            "event_date_source": str(comment.get("event_date_source", "")),
            "parent_comment_id": str(comment.get("comment_id", "")),
            "linked_comment_id": str(comment.get("comment_id", "")),
            "text_trust_status": str(comment.get("text_trust_status", "")),
            "human_review_status": str(comment.get("human_review_status", "")),
        }
        printed_id = str(comment.get("comment_number", "")).strip()
        event_payload.update({
            "site_id": _site_key(comment),
            "printed_comment_id": printed_id,
            "normalization_version": NORMALIZATION_VERSION,
            "normalized_text": normalized,
            "parameters": sorted(shared_parameter_tokens(event_payload.get("exact_text", ""))),
            "negations": sorted(shared_negation_tokens(event_payload.get("exact_text", ""))),
            "canonical_event_fingerprint": canonical_event_fingerprint(
                site_id=_site_key(comment),
                role_family="government",
                effective_round=effective_round,
                printed_comment_id=printed_id,
                text=event_payload.get("exact_text", ""),
                event_date=_date_key(event_payload),
                actor=event_payload.get("actor", ""),
                issue_id=thread_id,
            ),
        })
        events.append(event_payload)
    for event in comment.get("issue_thread_events", []) or []:
        if not isinstance(event, dict) or not str(event.get("exact_text", "")).strip():
            continue
        copied = deepcopy(event)
        copied.setdefault("effective_round", str(comment.get("review_round", "")))
        copied.setdefault("review_round", str(
            comment.get("document_round") or comment.get("review_round", "")
        ))
        copied.setdefault("observed_in_document_round", str(
            comment.get("document_round") or comment.get("review_round", "")
        ))
        copied.setdefault("source_document", str(comment.get("source_document", "")))
        copied.setdefault("source_document_date", str(comment.get("source_document_date", "")))
        copied.setdefault("document_date", deepcopy(comment.get("document_date") or {}))
        copied.setdefault("printed_comment_id", str(comment.get("comment_number", "")).strip())
        copied.setdefault("parent_comment_id", str(comment.get("comment_id", "")))
        copied.setdefault("linked_comment_id", str(comment.get("comment_id", "")))
        copied.setdefault("text_trust_status", str(comment.get("text_trust_status", "")))
        copied.setdefault("human_review_status", str(comment.get("human_review_status", "")))
        copied.setdefault("site_id", _site_key(comment))
        copied["normalization_version"] = NORMALIZATION_VERSION
        copied["normalized_text"] = normalize_event_text(copied.get("exact_text", ""))
        copied["parameters"] = sorted(shared_parameter_tokens(copied.get("exact_text", "")))
        copied["negations"] = sorted(shared_negation_tokens(copied.get("exact_text", "")))
        copied["canonical_event_fingerprint"] = canonical_event_fingerprint(
            site_id=_site_key(comment),
            role_family=event_role_family(copied),
            effective_round=copied.get("effective_round") or copied.get("review_round"),
            printed_comment_id=copied.get("printed_comment_id", ""),
            text=copied.get("exact_text", ""),
            event_date=_date_key(copied),
            actor=copied.get("actor", ""),
            issue_id=thread_id,
        )
        events.append(copied)
    return events


def build_issue_event_index(comments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    members: dict[str, set[str]] = defaultdict(set)
    for comment in comments:
        thread_id = str(comment.get("issue_thread_id") or comment.get("comment_id") or "")
        has_history = any(
            isinstance(event, dict) and str(event.get("exact_text", "")).strip()
            for event in (comment.get("issue_thread_events", []) or [])
        )
        if not thread_id or (not _text(comment).strip() and not has_history):
            continue
        members[thread_id].add(str(comment.get("comment_id", "")))
        for event in _raw_events(comment, thread_id):
            grouped[thread_id].append((comment, event))

    index: dict[str, dict[str, Any]] = {}
    for thread_id, occurrences in grouped.items():
        # Process explicit event contexts before contextless source copies.
        # If the same text truly occurred on multiple dates, the later
        # contextless copy can then be recognized as ambiguous instead of
        # being attached to whichever dated event happened to be seen first.
        occurrences.sort(key=lambda item: (
            1 if not _date_key(item[1]) and not _effective_event_round(item[1]) else 0,
            str(item[1].get("source_document", "")),
            int(item[1].get("source_order") or 0),
        ))
        canonical: list[dict[str, Any]] = []
        by_identity: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        candidate_blocks: dict[tuple[str, str, str], list[int]] = defaultdict(list)
        role_candidates: dict[str, list[int]] = defaultdict(list)
        contextless_candidates: dict[str, list[int]] = defaultdict(list)
        for comment, raw_event in occurrences:
            event = deepcopy(raw_event)
            identity = event_identity(event)
            occurrence = _event_occurrence(comment, raw_event)
            target_index = None
            decision = "DISTINCT"
            candidate_indices: list[int] = []
            if identity is not None:
                candidate_indices.extend(by_identity.get(identity, []))
            for block in _candidate_block_keys(event):
                candidate_indices.extend(candidate_blocks.get(block, []))
            role, _event_type, date, marker_or_round = _identity_context(event)
            if not date and not marker_or_round:
                candidate_indices.extend(role_candidates.get(role, []))
            else:
                candidate_indices.extend(contextless_candidates.get(role, []))
            # Retain insertion order while comparing each canonical candidate
            # once.  Exact identity does not bypass generic-response, actor,
            # date, or round safety checks.
            candidate_indices = list(dict.fromkeys(candidate_indices))
            merge_candidates: list[tuple[int, str]] = []
            for candidate_index in candidate_indices:
                can_merge, merge_decision = _events_can_merge(
                    canonical[candidate_index], event,
                )
                if can_merge:
                    merge_candidates.append((candidate_index, merge_decision))
            if merge_candidates:
                _role, _event_type, event_date, event_round = _identity_context(event)
                candidate_dates = {
                    _date_key(canonical[candidate_index])
                    for candidate_index, _merge_decision in merge_candidates
                    if _date_key(canonical[candidate_index])
                }
                # A contextless occurrence is not allowed to pick one of
                # several explicit dates for otherwise identical text.
                ambiguous_missing_context = (
                    not event_date and not event_round and len(candidate_dates) > 1
                )
                if not ambiguous_missing_context:
                    target_index, decision = merge_candidates[0]
                else:
                    decision = "POSSIBLE_DUPLICATE"
            if target_index is None:
                event["source_occurrences"] = [occurrence]
                event["source_occurrence_ids"] = [occurrence["source_occurrence_id"]]
                event["merged_event_ids"] = [str(raw_event.get("event_id", ""))]
                event["observed_in_document_rounds"] = sorted({
                    value for value in (
                        event.get("observed_in_document_round"),
                        event.get("review_round"),
                    ) if value
                })
                event["event_submission"] = _submission_label(event) or _submission_label(comment)
                event["observed_in_submissions"] = sorted({
                    value for value in (
                        *(event.get("observed_in_submissions", []) or []),
                        _submission_label(event),
                        _submission_label(comment),
                    )
                    if value
                })
                event["normalized_event_type"] = normalized_event_type(event)
                event["actor_normalized"] = normalize_actor(
                    event.get("actor") or event.get("reviewer") or ""
                )
                event["dedup_decision"] = "DISTINCT"
                canonical.append(event)
                canonical_index = len(canonical) - 1
                if identity is not None:
                    by_identity[identity].append(canonical_index)
                for block in _candidate_block_keys(event):
                    candidate_blocks[block].append(canonical_index)
                role_candidates[role].append(canonical_index)
                if not date and not marker_or_round:
                    contextless_candidates[role].append(canonical_index)
                continue
            target = canonical[target_index]
            # A physical source location identifies an occurrence.  Legacy
            # extraction IDs are not included because the same row often gets
            # a new ID in a later parser run.
            occurrence_key = _occurrence_merge_key(occurrence)
            existing_keys = {
                _occurrence_merge_key(item)
                for item in target.get("source_occurrences", [])
                if isinstance(item, dict)
            }
            if occurrence_key not in existing_keys:
                target.setdefault("source_occurrences", []).append(occurrence)
                target.setdefault("source_occurrence_ids", []).append(
                    occurrence["source_occurrence_id"]
                )
            merged_id = str(raw_event.get("event_id", ""))
            if merged_id not in target.setdefault("merged_event_ids", []):
                target["merged_event_ids"].append(merged_id)
            _merge_event_metadata(target, event, decision)
            # Index every compatible observation context as an alias to the
            # same canonical event. This lets a later dated/source-labelled
            # copy find a canonical event that was first seen without that
            # metadata, without creating a second card.
            if identity is not None and target_index not in by_identity[identity]:
                by_identity[identity].append(target_index)
            for block in _candidate_block_keys(event):
                if target_index not in candidate_blocks[block]:
                    candidate_blocks[block].append(target_index)
        canonical.sort(key=_event_sort_key)
        # Raw extraction IDs are provenance and may be repeated by cumulative
        # source files.  Canonical timeline IDs are occurrence IDs: globally
        # stable inside the thread and unique across different rounds/dates.
        # Keep every raw ID in ``merged_event_ids`` for audit.
        for position, event in enumerate(canonical, 1):
            identity = event_identity(event)
            if identity is not None:
                event_type, occurrence_key, normalized_text = identity
            else:
                event_type = str(event.get("event_type", "discussion_note"))
                normalized_text = _normalized(
                    event.get("exact_text") or event.get("text") or ""
                )
                first_occurrence = next(
                    (
                        item for item in event.get("source_occurrences", []) or []
                        if isinstance(item, dict)
                    ),
                    {},
                )
                occurrence_key = "|".join((
                    "undated",
                    str(
                        first_occurrence.get("comment_id")
                        or event.get("comment_id")
                        or event.get("source_document", "")
                    ),
                    str(first_occurrence.get("source_document") or event.get("source_document", "")),
                    repr(first_occurrence.get("source_location") or event.get("source_location") or {}),
                    str(event.get("source_order") or position),
                ))
            if _is_generic_event_text(
                event.get("exact_text") or event.get("text") or ""
            ):
                occurrence_key = "|".join((
                    occurrence_key,
                    "parent:" + ",".join(sorted(_parent_context_values(event))),
                ))
            event["event_id"] = _stable_id(
                "E", thread_id, event_type, occurrence_key, normalized_text,
            )
            for occurrence in event.get("source_occurrences", []) or []:
                if isinstance(occurrence, dict):
                    occurrence["canonical_event_id"] = event["event_id"]
            event["normalization_version"] = NORMALIZATION_VERSION
            event["normalized_text"] = normalize_event_text(
                event.get("exact_text") or event.get("text") or ""
            )
            event["parameters"] = sorted(shared_parameter_tokens(
                event.get("exact_text") or event.get("text") or ""
            ))
            event["negations"] = sorted(shared_negation_tokens(
                event.get("exact_text") or event.get("text") or ""
            ))
            if len(event.get("merged_event_ids", [])) > 1 and event.get("dedup_decision") == "DISTINCT":
                event["dedup_decision"] = "EXACT_DUPLICATE"
            event["canonical_event_fingerprint"] = canonical_event_fingerprint(
                site_id=event.get("site_id") or thread_id,
                role_family=event_role_family(event),
                effective_round=_effective_event_round(event),
                printed_comment_id=event.get("printed_comment_id", ""),
                text=event.get("exact_text") or event.get("text") or "",
                event_date=_date_key(event),
                actor=event.get("actor", ""),
                issue_id=thread_id,
            )
            event["source_occurrence_ids"] = list(dict.fromkeys(
                event.get("source_occurrence_ids", []) or [
                    item.get("source_occurrence_id")
                    for item in event.get("source_occurrences", []) or []
                    if isinstance(item, dict) and item.get("source_occurrence_id")
                ]
            ))
        review_queue = _near_duplicate_review_queue(canonical)
        review_event_ids = {
            event_id
            for item in review_queue
            for event_id in (item.get("left_event_id"), item.get("right_event_id"))
            if event_id
        }
        for event in canonical:
            if str(event.get("event_id", "")) in review_event_ids:
                event["dedup_decision"] = "POSSIBLE_DUPLICATE"
        duplicate_event_of: dict[str, str] = {}
        for event in canonical:
            canonical_id = str(event.get("event_id", ""))
            duplicate_ids = [
                str(value) for value in event.get("merged_event_ids", [])
                if str(value) and str(value) != canonical_id
            ]
            event["duplicate_event_ids"] = duplicate_ids
            for duplicate_id in duplicate_ids:
                duplicate_event_of[duplicate_id] = canonical_id
        index[thread_id] = {
            "thread_id": thread_id,
            "member_comment_ids": sorted(members[thread_id]),
            "events": canonical,
            "raw_event_count": len(occurrences),
            "canonical_event_count": len(canonical),
            "duplicate_event_count": max(0, len(occurrences) - len(canonical)),
            "normalization_version": NORMALIZATION_VERSION,
            "dedup_review_queue": review_queue,
            "duplicate_event_of": duplicate_event_of,
        }
    return index


def collect_issue_event_review_queue(
    index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten per-thread possible duplicate candidates for persistence.

    The queue is deliberately separate from canonical timeline events.  A
    candidate is never merged by this helper; it remains available for an
    operator to review after an incremental ingest or a repair run.
    """
    queue: list[dict[str, Any]] = []
    for thread_id, timeline in index.items():
        for item in timeline.get("dedup_review_queue", []) or []:
            if not isinstance(item, dict):
                continue
            queue.append({"issue_thread_id": str(thread_id), **item})
    queue.sort(key=lambda item: (
        str(item.get("issue_thread_id", "")),
        str(item.get("left_event_id", "")),
        str(item.get("right_event_id", "")),
    ))
    return queue
