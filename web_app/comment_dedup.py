"""Deterministically suppress repeated comments within one site and review round."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

try:
    from .document_identity import canonical_project_id
    from .canonical_event import NORMALIZATION_VERSION, normalize_event_text
except ImportError:  # Direct module execution.
    from document_identity import canonical_project_id
    from canonical_event import NORMALIZATION_VERSION, normalize_event_text


DUPLICATE_FILLER_WORDS = {
    "a", "an", "at", "the", "to",
}
NEGATION_WORDS = {"no", "not", "without", "except", "exclude", "prohibit", "prohibited"}
SUBMISSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<number>\d+)(?:st|nd|rd|th)\s+submission\b",
    re.IGNORECASE,
)


def normalized_comment_text(record: dict[str, Any]) -> str:
    text = record.get("verified_text") or record.get("original_text") or ""
    return normalize_event_text(text)


def source_identity(record: dict[str, Any]) -> str:
    """Use the file hash when possible so copied instances of one file stay one source."""
    digest = str(record.get("source_sha256", "")).strip().casefold()
    if digest:
        return f"sha256:{digest}"
    source = str(record.get("source_document", "")).split(" | ", 1)[0].strip()
    return f"path:{Path(source).as_posix().casefold()}" if source else ""


def _normalized_identity(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def _parse_event_date(value: Any) -> str:
    """Return an ISO date only when a source field contains a valid date.

    File names are included as a fallback because spreadsheet exports often
    keep the review timestamp only in the filename.  Invalid permit numbers
    such as ``25-033-701`` are rejected by ``date`` validation.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    patterns = (
        r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b",
        r"\b(\d{1,2})[-_/.](\d{1,2})[-_/.](20\d{2})\b",
        r"\b(\d{1,2})[-_/.](\d{1,2})[-_/.](\d{2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        parts = [int(value) for value in match.groups()]
        if len(match.groups()) == 3 and parts[0] >= 2000:
            year, month, day = parts
        else:
            month, day, year = parts
            year += 2000 if year < 100 else 0
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def event_date_key(record: dict[str, Any]) -> str:
    """Find the date of the visible comment event, not the file mtime."""
    source_name = Path(
        str(record.get("source_document", "")).split(" | ", 1)[0]
    ).name.casefold()
    # A response letter repeats the earlier government comment beside a new
    # applicant response.  Its report/letter date belongs to the response
    # container, not to the repeated government-comment event.  Treating that
    # date as the comment date prevented an otherwise exact same-round row
    # from merging with the plan-review source.
    response_container = bool(record.get("response_id")) and bool(
        re.search(r"\bresponse(?:\s+letter)?\b", source_name)
    )
    date_fields = (
        ()
        if response_container
        else (
            "source_date_evidence", "source_document_date", "source_date",
            "report_date", "letter_date", "document_date",
        )
    )
    for field in date_fields:
        parsed = _parse_event_date(record.get(field))
        if parsed:
            return parsed
    # Spreadsheet exports commonly store the reviewer timestamp in one of
    # these labels.  This is an event date, unlike filesystem modification
    # time, and is safe to use for same-day duplicate detection.
    for field in ("reviewer", "reviewer_context"):
        parsed = _parse_event_date(record.get(field))
        if parsed:
            return parsed
    return "" if response_container else _parse_event_date(record.get("source_document", ""))


def _same_event_date(first: dict[str, Any], second: dict[str, Any]) -> bool:
    left, right = event_date_key(first), event_date_key(second)
    # Fuzzy hierarchy/form matching must not bridge a known date and an
    # undated record. Exact deduplication is already conservative for missing
    # dates; this keeps the same rule for near-identical rows.
    return (not left and not right) or left == right


def site_identity(record: dict[str, Any]) -> str:
    # Prefer the normalized hierarchy identity.  The legacy implementation
    # used the first source-folder name, which split one permit into separate
    # Building/Structural/Planning sites and also treated ``Ave`` and
    # ``Avenue`` as different projects.
    city = _normalized_identity(record.get("city"))
    project_id = str(record.get("project_id") or "").strip()
    if not project_id:
        project_id = canonical_project_id(record)
    if project_id:
        return f"{city}|project:{project_id.casefold()}"
    site = _normalized_identity(
        record.get("property_project")
        or record.get("property")
        or record.get("site")
        or record.get("application_number")
    )
    return f"{city}|{site}" if site else ""


def round_identity(record: dict[str, Any]) -> str:
    value = _normalized_identity(
        record.get("reviewed_plan_round")
        or record.get("review_round")
        or record.get("source_cycle")
    )
    numbers = re.findall(r"\d+(?:\.\d+)?", value)
    base = numbers[-1] if numbers else value
    # A later ProjectDox submission can repeat the same printed comment
    # number and review-round label while representing a new attempt at the
    # same issue.  When the visible event date is known, that date is part of
    # the duplicate key and the submission suffix must not split an otherwise
    # identical event.  Without a date we remain conservative and keep
    # different submissions available to the issue timeline.
    source = str(record.get("source_document", ""))
    submission = SUBMISSION_PATTERN.search(source)
    if submission and not event_date_key(record):
        return f"{base}|submission:{int(submission.group('number'))}"
    return base


def extraction_fingerprint(record: dict[str, Any]) -> str:
    """Normalize harmless extraction-word variation without erasing parameters."""
    text = normalized_comment_text(record)
    tokens = re.findall(r"[a-z0-9]+(?:[./'’-][a-z0-9]+)*", text)
    return " ".join(token for token in tokens if token not in DUPLICATE_FILLER_WORDS)


def parameter_tokens(record: dict[str, Any]) -> set[str]:
    """Keep dimensions, code sections, sheet IDs, and other numbered parameters."""
    return {
        token
        for token in re.findall(r"[a-z]*\d+(?:[./'’-][a-z0-9]+)*", normalized_comment_text(record))
        if token
    }


def negation_tokens(record: dict[str, Any]) -> set[str]:
    return set(re.findall(r"[a-z]+", normalized_comment_text(record))) & NEGATION_WORDS


def _hierarchy_compare_text(record: dict[str, Any]) -> str:
    text = normalized_comment_text(record)
    # A repeated copy can add a plan-label crosswalk without changing the
    # government requirement itself.
    # ``normalization_v3`` removes punctuation before this comparison, so
    # handle both the original parenthesized form and the punctuation-free
    # token stream produced by an XLSX/PDF export.
    text = re.sub(r"\([^)]*\blabel(?:ed)?\b[^)]*\)", " ", text)
    text = re.sub(r"\bthese\s+are\s+labeled\b.*?\bin\s+the\s+plan\s+set\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _hierarchy_repeat(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if (
        first.get("hierarchy_status") != "merged_parent"
        or second.get("hierarchy_status") != "merged_parent"
        or site_identity(first) != site_identity(second)
        or round_identity(first) != round_identity(second)
        or not _same_event_date(first, second)
    ):
        return False
    left, right = _hierarchy_compare_text(first), _hierarchy_compare_text(second)
    if not left or not right:
        return False
    left_parameters = set(re.findall(r"[a-z]*\d+(?:[./'’-][a-z0-9]+)*", left))
    right_parameters = set(re.findall(r"[a-z]*\d+(?:[./'’-][a-z0-9]+)*", right))
    if left_parameters != right_parameters:
        return False
    if (set(re.findall(r"[a-z]+", left)) & NEGATION_WORDS) != (
        set(re.findall(r"[a-z]+", right)) & NEGATION_WORDS
    ):
        return False
    left_tokens, right_tokens = set(left.split()), set(right.split())
    containment = len(left_tokens & right_tokens) / max(
        1, min(len(left_tokens), len(right_tokens))
    )
    return (
        containment >= 0.96
        and SequenceMatcher(None, left, right).ratio() >= 0.94
    )


def _form_row_repeat(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Detect one visible form row copied into a differently named source.

    Response-letter packages often repeat a plan-review row with a short
    prefix/suffix added by extraction.  Require the same site, round,
    discipline, and printed comment number, then compare the token stream.
    Numeric parameters remain significant for similarly worded requirements
    (for example, door width 3 versus 4).
    """
    if (
        site_identity(first) != site_identity(second)
        or round_identity(first) != round_identity(second)
        or not _same_event_date(first, second)
    ):
        return False
    if _normalized_identity(first.get("discipline")) != _normalized_identity(second.get("discipline")):
        return False
    number_left = _normalized_identity(first.get("comment_number"))
    number_right = _normalized_identity(second.get("comment_number"))
    if not number_left or number_left != number_right:
        return False
    left_source = str(first.get("source_document", "")).split(" | ", 1)[0].strip()
    right_source = str(second.get("source_document", "")).split(" | ", 1)[0].strip()
    if not left_source or left_source == right_source:
        return False
    left = normalized_comment_text(first)
    right = normalized_comment_text(second)
    left_tokens = re.findall(r"[a-z0-9]+(?:[./'’-][a-z0-9]+)*", left)
    right_tokens = re.findall(r"[a-z0-9]+(?:[./'’-][a-z0-9]+)*", right)
    if min(len(left_tokens), len(right_tokens)) < 8:
        return False
    shared = len(set(left_tokens) & set(right_tokens)) / max(1, min(len(left_tokens), len(right_tokens)))
    if shared < 0.90:
        return False
    matching_blocks = SequenceMatcher(None, left_tokens, right_tokens).get_matching_blocks()
    longest = max((block.size for block in matching_blocks), default=0)
    if longest < 8:
        return False
    ratio = max(len(left_tokens), len(right_tokens)) / max(1, min(len(left_tokens), len(right_tokens)))
    left_parameters = parameter_tokens(first)
    right_parameters = parameter_tokens(second)
    left_negations = negation_tokens(first)
    right_negations = negation_tokens(second)
    if ratio <= 1.35:
        if left_parameters != right_parameters or left_negations != right_negations:
            return False
    elif not (left_parameters <= right_parameters or right_parameters <= left_parameters):
        return False
    if ratio <= 1.35 and longest < max(8, int(min(len(left_tokens), len(right_tokens)) * 0.55)):
        return False
    return True


def duplicate_key(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """Group exact normalized text once per site and review round.

    Older rows without reliable site/round metadata retain the conservative
    same-source behavior so unrelated projects are never merged by accident.
    """
    fingerprint = extraction_fingerprint(record)
    if not fingerprint:
        return None
    site, review_round = site_identity(record), round_identity(record)
    if site and review_round:
        parameters = ",".join(sorted(parameter_tokens(record)))
        negations = ",".join(sorted(negation_tokens(record)))
        event_date = event_date_key(record) or "unknown"
        return (
            f"site:{site}",
            f"round:{review_round}|date:{event_date}",
            f"{fingerprint}|parameters:{parameters}|negations:{negations}",
        )
    source = source_identity(record)
    return (source, "", fingerprint) if source else None


def _document_copy_key(record: dict[str, Any]) -> tuple[str, str, str, str, str, str] | None:
    """Identity for rows extracted from byte/content-equivalent documents.

    DOCX/PDF exports of the same review letter frequently disagree about the
    visible date and submission suffix.  When the document registry has
    already assigned them one canonical document group, those fields are
    provenance—not evidence of a second comment event—so use the stable
    document id and the substantive row identity instead.
    """
    document_id = str(record.get("canonical_document_id", "")).strip()
    try:
        group_size = int(record.get("canonical_document_duplicate_group_size") or 0)
    except (TypeError, ValueError):
        group_size = 0
    if not document_id or group_size < 2:
        return None
    fingerprint = extraction_fingerprint(record)
    if not fingerprint:
        return None
    return (
        site_identity(record),
        _normalized_identity(
            record.get("reviewed_plan_round")
            or record.get("review_round")
            or record.get("source_cycle")
        ),
        document_id.casefold(),
        _normalized_identity(record.get("discipline")),
        _normalized_identity(record.get("comment_number")),
        f"{fingerprint}|parameters:{','.join(sorted(parameter_tokens(record)))}|"
        f"negations:{','.join(sorted(negation_tokens(record)))}",
    )


def _position(record: dict[str, Any]) -> int:
    locator = record.get("source_locator_json")
    if isinstance(locator, dict):
        for field in ("paragraph_index", "source_row", "page_number"):
            try:
                if locator.get(field) not in (None, ""):
                    return int(locator[field])
            except (TypeError, ValueError):
                pass
    for field in ("source_row", "source_page", "comment_number"):
        try:
            if record.get(field) not in (None, ""):
                return int(float(str(record[field])))
        except (TypeError, ValueError):
            pass
    return 10**9


def _winner_sort_key(record: dict[str, Any], links: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    link = links.get(str(record.get("comment_id", "")), {})
    return (
        -int(link.get("review_status") == "confirmed"),
        -int(bool(record.get("response_id") or link.get("response_id"))),
        -int(record.get("human_review_status") == "confirmed"),
        -int(record.get("text_trust_status") == "verified"),
        -int(record.get("locator_trust_status") == "verified"),
        _position(record),
        str(record.get("comment_id", "")),
    )


def find_duplicate_comments(
    comments: Iterable[dict[str, Any]], links: Iterable[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return canonical records and a duplicate-id -> canonical-id audit map."""
    comment_rows = list(comments)
    link_map = {str(row.get("comment_id", "")): row for row in links}
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    ungrouped: list[dict[str, Any]] = []
    for record in comment_rows:
        if record.get("duplicate_status") == "hierarchical_subpoint":
            continue
        key = duplicate_key(record)
        (groups[key] if key else ungrouped).append(record)

    canonical = list(ungrouped)
    duplicate_of: dict[str, str] = {}
    for rows in groups.values():
        winner = min(rows, key=lambda row: _winner_sort_key(row, link_map))
        canonical.append(winner)
        winner_id = str(winner.get("comment_id", ""))
        duplicate_sources = sorted({
            str(row.get("source_document", "")).strip()
            for row in rows
            if str(row.get("source_document", "")).strip()
        })
        if len(duplicate_sources) > 1:
            winner["duplicate_source_documents"] = duplicate_sources
        for row in rows:
            row_id = str(row.get("comment_id", ""))
            if row_id != winner_id:
                duplicate_of[row_id] = winner_id

    # The same canonical document is often present as both PDF and DOCX (or
    # under several export filenames).  The first pass intentionally keeps
    # dated/undated rows conservative, so collapse these proven document-copy
    # rows here without discarding either source occurrence.
    document_copies: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in canonical:
        key = _document_copy_key(row)
        if key is not None:
            document_copies[key].append(row)
    document_copy_losers: set[str] = set()
    for rows in document_copies.values():
        if len(rows) < 2:
            continue
        winner = min(rows, key=lambda row: _winner_sort_key(row, link_map))
        winner_id = str(winner.get("comment_id", ""))
        source_documents = sorted({
            str(row.get("source_document", "")).strip()
            for row in rows if str(row.get("source_document", "")).strip()
        })
        if len(source_documents) > 1:
            winner["duplicate_source_documents"] = sorted(
                set(winner.get("duplicate_source_documents", [])) | set(source_documents)
            )
        for row in rows:
            row_id = str(row.get("comment_id", ""))
            if row_id != winner_id:
                duplicate_of[row_id] = winner_id
                document_copy_losers.add(row_id)
    if document_copy_losers:
        canonical = [
            row for row in canonical
            if str(row.get("comment_id", "")) not in document_copy_losers
        ]

    # Word can repeat the same complete numbered requirement under two
    # discipline headings with tiny label/crosswalk wording differences.
    # This conservative pass applies only to hierarchy-confirmed parents.
    hierarchy = [
        row for row in canonical if row.get("hierarchy_status") == "merged_parent"
    ]
    parent: dict[str, str] = {
        str(row.get("comment_id", "")): str(row.get("comment_id", ""))
        for row in hierarchy
    }

    def root(record_id: str) -> str:
        while parent.get(record_id, record_id) != record_id:
            parent[record_id] = parent[parent[record_id]]
            record_id = parent[record_id]
        return record_id

    for index, first in enumerate(hierarchy):
        for second in hierarchy[index + 1:]:
            if _hierarchy_repeat(first, second):
                left = root(str(first.get("comment_id", "")))
                right = root(str(second.get("comment_id", "")))
                if left != right:
                    parent[right] = left
    by_id = {str(row.get("comment_id", "")): row for row in hierarchy}
    hierarchy_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record_id, record in by_id.items():
        hierarchy_groups[root(record_id)].append(record)
    fuzzy_losers: set[str] = set()
    for rows in hierarchy_groups.values():
        if len(rows) < 2:
            continue
        winner = min(rows, key=lambda row: _winner_sort_key(row, link_map))
        winner_id = str(winner.get("comment_id", ""))
        response_ids = sorted({
            str(row.get("response_id") or link_map.get(
                str(row.get("comment_id", "")), {}
            ).get("response_id") or "")
            for row in rows
        } - {""})
        if len(response_ids) > 1:
            winner["duplicate_response_ids"] = response_ids
        for row in rows:
            row_id = str(row.get("comment_id", ""))
            if row_id != winner_id:
                duplicate_of[row_id] = winner_id
                fuzzy_losers.add(row_id)
    if fuzzy_losers:
        canonical = [
            row for row in canonical
            if str(row.get("comment_id", "")) not in fuzzy_losers
        ]

    # A combined response letter can repeat a complete plan-review row under
    # a different filename.  Treat that as one same-round comment, while
    # keeping the losing row and its source for audit.
    form_rows = [row for row in canonical if row.get("hierarchy_status") != "merged_parent"]
    form_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in form_rows:
        form_groups[(
            site_identity(row), round_identity(row),
            _normalized_identity(row.get("discipline")),
            _normalized_identity(row.get("comment_number")),
        )].append(row)
    form_parent = {str(row.get("comment_id", "")): str(row.get("comment_id", "")) for row in form_rows}

    def form_root(record_id: str) -> str:
        while form_parent.get(record_id, record_id) != record_id:
            form_parent[record_id] = form_parent[form_parent[record_id]]
            record_id = form_parent[record_id]
        return record_id

    for rows in form_groups.values():
        if len(rows) < 2:
            continue
        for index, first in enumerate(rows):
            for second in rows[index + 1:]:
                if not _form_row_repeat(first, second):
                    continue
                left, right = form_root(str(first.get("comment_id", ""))), form_root(str(second.get("comment_id", "")))
                if left != right:
                    form_parent[right] = left
    grouped_forms: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in form_rows:
        grouped_forms[form_root(str(row.get("comment_id", "")))].append(row)
    form_losers: set[str] = set()
    for rows in grouped_forms.values():
        if len(rows) < 2:
            continue
        winner = min(rows, key=lambda row: _winner_sort_key(row, link_map))
        winner_id = str(winner.get("comment_id", ""))
        source_documents = sorted({str(row.get("source_document", "")).strip() for row in rows if row.get("source_document")})
        if len(source_documents) > 1:
            winner["duplicate_source_documents"] = sorted(set(winner.get("duplicate_source_documents", [])) | set(source_documents))
        for row in rows:
            row_id = str(row.get("comment_id", ""))
            if row_id != winner_id:
                duplicate_of[row_id] = winner_id
                form_losers.add(row_id)
    if form_losers:
        canonical = [row for row in canonical if str(row.get("comment_id", "")) not in form_losers]
    return canonical, duplicate_of


_SOURCE_OCCURRENCE_FIELDS = (
    "source_document", "source_sha256", "source_location", "source_page",
    "source_page_end", "source_sheet", "source_row", "source_row_end",
    "source_cell_range", "source_locator_json", "source_bounding_boxes",
    "source_document_date", "source_date_evidence", "comment_number",
)


def normalized_response_text(record: dict[str, Any]) -> str:
    """Return response identity text without changing the stored response.

    Response exports commonly add a presentation prefix (``Response:`` or
    ``RESPONSE_``).  It is not part of the applicant's answer, so remove only
    that known prefix for identity matching.  All other wording, numbers and
    negations remain significant.
    """
    text = str(record.get("verified_text") or record.get("original_text") or "")
    text = re.sub(r"^\s*[\"']?\s*response\s*[_:-]\s*", "", text, flags=re.IGNORECASE)
    return normalize_event_text(text)


def response_event_date_key(record: dict[str, Any]) -> str:
    """Find the applicant-response date, preferring response/event fields."""
    for field in (
        "response_date_iso", "response_date_raw", "event_date",
        "event_date_iso", "event_date_raw", "source_date_evidence",
        "source_document_date", "document_date_iso", "document_date",
    ):
        value = record.get(field)
        if isinstance(value, dict):
            value = value.get("iso") or value.get("raw") or value.get("value")
        parsed = _parse_event_date(value)
        if parsed:
            return parsed
    return _parse_event_date(record.get("source_document", ""))


def _response_round_identity(record: dict[str, Any]) -> str:
    value = _normalized_identity(
        record.get("response_letter_round")
        or record.get("reviewed_plan_round")
        or record.get("review_round")
        or record.get("source_cycle")
    )
    numbers = re.findall(r"\d+(?:\.\d+)?", value)
    return numbers[-1] if numbers else value


def _response_parent_roots(
    comments: Iterable[dict[str, Any]],
    duplicate_of: dict[str, str] | None = None,
) -> dict[str, str]:
    """Map response/comment owners to the surviving canonical comment id."""
    mapping = dict(duplicate_of or {})
    for row in comments:
        comment_id = str(row.get("comment_id", ""))
        if not comment_id:
            continue
        target = str(row.get("duplicate_of") or row.get("lineage_duplicate_of") or "")
        if target:
            mapping.setdefault(comment_id, target)

    def root(value: str) -> str:
        seen: set[str] = set()
        while value in mapping and mapping[value] and value not in seen:
            seen.add(value)
            value = mapping[value]
        return value

    return {key: root(key) for key in set(mapping) | {
        str(row.get("comment_id", "")) for row in comments if row.get("comment_id")
    }}


def _response_winner_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -int(record.get("human_review_status") == "confirmed"),
        -int(record.get("verification_status") == "confirmed"),
        -int(record.get("text_trust_status") == "verified"),
        -int(bool(record.get("source_locator_json") or record.get("source_location"))),
        _position(record),
        str(record.get("response_id", "")),
    )


def deduplicate_responses(
    dataset: dict[str, Any], duplicate_of: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Collapse repeated applicant/follow-up response rows conservatively.

    A response is one event only when it belongs to the same canonical
    comment, review round, response date and normalized text.  Different dates
    are intentionally retained: a later ``Noted`` or follow-up is historical
    evidence, not a duplicate.  Losing rows remain immutable audit records and
    their source locators are attached to the winner.
    """
    comments = dataset.get("comments", []) or []
    responses = dataset.get("responses", []) or []
    roots = _response_parent_roots(comments, duplicate_of)
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for response in responses:
        response_id = str(response.get("response_id", ""))
        text = normalized_response_text(response)
        parent_id = str(response.get("comment_id", ""))
        parent_id = roots.get(parent_id, parent_id)
        if not response_id or not text or not parent_id:
            continue
        date_key = response_event_date_key(response) or "unknown"
        round_key = _response_round_identity(response) or "unknown"
        parameters = ",".join(sorted(parameter_tokens(response)))
        negations = ",".join(sorted(negation_tokens(response)))
        groups[(parent_id, round_key, date_key, f"{text}|{parameters}|{negations}")].append(response)

    response_by_id = {
        str(row.get("response_id", "")): row for row in responses
        if str(row.get("response_id", ""))
    }
    response_duplicate_of: dict[str, str] = {}
    occurrences_attached = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        winner = min(rows, key=_response_winner_sort_key)
        winner_id = str(winner.get("response_id", ""))
        winner["duplicate_source_documents"] = sorted(set(
            str(row.get("source_document", "")).strip()
            for row in rows if str(row.get("source_document", "")).strip()
        ))
        for row in rows:
            row_id = str(row.get("response_id", ""))
            if row_id == winner_id:
                continue
            response_duplicate_of[row_id] = winner_id
            _append_source_occurrence(
                winner,
                _source_occurrence(row, row_id),
                field="source_occurrences",
            )
            occurrences_attached += 1
            winner.setdefault("duplicate_response_ids", [])
            if row_id not in winner["duplicate_response_ids"]:
                winner["duplicate_response_ids"].append(row_id)

    for row in responses:
        row_id = str(row.get("response_id", ""))
        if row_id in response_duplicate_of:
            row["duplicate_of"] = response_duplicate_of[row_id]
            row["duplicate_status"] = "same_parent_round_date_exact_text"
            row["dedup_decision"] = "AUTO_MERGE"
            row["search_eligible"] = False
        elif row_id in response_by_id:
            row.setdefault("dedup_decision", "DISTINCT")

    # Repoint only the links/comments that owned the losing response.  This
    # prevents a duplicate card from being recreated by the live projection.
    for comment in comments:
        response_id = str(comment.get("response_id", ""))
        if response_id in response_duplicate_of:
            comment["response_id"] = response_duplicate_of[response_id]
    for link in dataset.get("comment_response_links", []) or []:
        response_id = str(link.get("response_id", ""))
        if response_id in response_duplicate_of:
            link["response_id"] = response_duplicate_of[response_id]

    return {
        "duplicate_response_rows_suppressed": len(response_duplicate_of),
        "duplicate_response_groups": len(set(response_duplicate_of.values())),
        "response_duplicate_of": response_duplicate_of,
        "response_source_occurrences_attached": occurrences_attached,
    }


def _source_occurrence(record: dict[str, Any], owner_id: str = "") -> dict[str, Any]:
    """Copy only locator/provenance fields needed to reopen a duplicate source."""
    occurrence: dict[str, Any] = {
        "owner_id": owner_id,
        "comment_id": str(record.get("comment_id", "")),
        "response_id": str(record.get("response_id", "")),
        "exact_text": str(record.get("verified_text") or record.get("original_text", "")),
    }
    for field in _SOURCE_OCCURRENCE_FIELDS:
        value = record.get(field)
        if value not in (None, "", [], {}):
            occurrence[field] = value
    return occurrence


def _occurrence_key(occurrence: dict[str, Any]) -> tuple[str, str, str, str, str]:
    locator = occurrence.get("source_locator_json")
    if isinstance(locator, dict):
        locator_key = json.dumps(locator, ensure_ascii=False, sort_keys=True)
    else:
        locator_key = str(locator or "")
    return (
        str(occurrence.get("source_document", "")),
        str(occurrence.get("source_page", "")),
        str(occurrence.get("source_row", "")),
        str(occurrence.get("source_cell_range", "")),
        locator_key,
    )


def _append_source_occurrence(
    owner: dict[str, Any], occurrence: dict[str, Any], field: str = "source_occurrences",
) -> None:
    occurrences = owner.setdefault(field, [])
    if not isinstance(occurrences, list):
        occurrences = []
        owner[field] = occurrences
    key = _occurrence_key(occurrence)
    if any(isinstance(item, dict) and _occurrence_key(item) == key for item in occurrences):
        return
    occurrences.append(occurrence)


def mark_duplicate_comments(dataset: dict[str, Any]) -> dict[str, Any]:
    """Keep raw rows for audit while excluding duplicate reads from production use."""
    comments = dataset.get("comments", [])
    canonical, duplicate_of = find_duplicate_comments(comments, dataset.get("comment_response_links", []))
    canonical_ids = {str(row.get("comment_id", "")) for row in canonical}
    comments_by_id = {str(row.get("comment_id", "")): row for row in comments}
    responses_by_id = {
        str(row.get("response_id", "")): row
        for row in dataset.get("responses", [])
        if str(row.get("response_id", ""))
    }
    source_occurrences_attached = 0
    response_occurrences_attached = 0

    # Keep one searchable parent, but attach every losing row's source locator
    # to it.  The raw losing records remain immutable audit rows below.
    for duplicate_id, winner_id in duplicate_of.items():
        duplicate = comments_by_id.get(duplicate_id)
        winner = comments_by_id.get(winner_id)
        if not duplicate or not winner:
            continue
        _append_source_occurrence(
            winner,
            _source_occurrence(duplicate, duplicate_id),
        )
        source_occurrences_attached += 1
        duplicate_response_id = str(duplicate.get("response_id", ""))
        winner_response_id = str(winner.get("response_id", ""))
        if duplicate_response_id and duplicate_response_id != winner_response_id:
            duplicate_response = responses_by_id.get(duplicate_response_id)
            winner_response = responses_by_id.get(winner_response_id)
            if duplicate_response and winner_response:
                _append_source_occurrence(
                    winner_response,
                    _source_occurrence(duplicate_response, duplicate_response_id),
                    field="source_occurrences",
                )
                response_occurrences_attached += 1
                winner.setdefault("duplicate_response_ids", [])
                if duplicate_response_id not in winner["duplicate_response_ids"]:
                    winner["duplicate_response_ids"].append(duplicate_response_id)

    for record in comments:
        record["normalization_version"] = NORMALIZATION_VERSION
        record_id = str(record.get("comment_id", ""))
        if record.get("lineage_duplicate_of"):
            record["search_eligible"] = False
            continue
        if record.get("duplicate_status") == "hierarchical_subpoint":
            record["search_eligible"] = False
            continue
        if record_id in duplicate_of:
            record["dedup_decision"] = "AUTO_MERGE"
            record["search_eligible"] = False
            record["duplicate_of"] = duplicate_of[record_id]
            winner = comments_by_id.get(duplicate_of[record_id], {})
            if (
                record.get("hierarchy_status") == "merged_parent"
                and winner.get("hierarchy_status") == "merged_parent"
                and _hierarchy_repeat(record, winner)
            ):
                record["duplicate_status"] = "same_site_round_hierarchical_repeat"
            else:
                record["duplicate_status"] = (
                    "same_site_round_exact_text"
                    if normalized_comment_text(record) == normalized_comment_text(winner)
                    else "same_site_round_form_row_repeat"
                    if _form_row_repeat(record, winner)
                    else "same_site_round_extraction_variant"
                )
        elif record_id in canonical_ids:
            was_deduplicated = bool(record.get("duplicate_of") or record.get("duplicate_status"))
            record.pop("duplicate_of", None)
            record.pop("duplicate_status", None)
            record["dedup_decision"] = "AUTO_MERGE" if was_deduplicated else "DISTINCT"
            if was_deduplicated and record.get("text_trust_status") == "verified":
                record["search_eligible"] = True
    # Response rows can be copied into multiple cumulative workbooks even
    # when their parent comment has already been canonicalized.  Run this
    # after parent selection so all response owners resolve to the same
    # canonical parent.  The raw response rows remain available for audit.
    response_report = deduplicate_responses(dataset, duplicate_of)
    # Enforce the invariant after all repair passes: a row explicitly marked
    # as a duplicate can never re-enter the searchable projection on a later
    # reload.  This protects against legacy datasets whose stale flags were
    # written by an older repair version.
    for record in comments:
        if record.get("duplicate_of") or record.get("lineage_duplicate_of"):
            record["search_eligible"] = False
    return {
        "duplicate_rows_suppressed": len(duplicate_of),
        "duplicate_groups": len(set(duplicate_of.values())),
        "duplicate_of": duplicate_of,
        "source_occurrences_attached": source_occurrences_attached,
        "response_occurrences_attached": response_occurrences_attached,
        **response_report,
        "normalization_version": NORMALIZATION_VERSION,
    }
