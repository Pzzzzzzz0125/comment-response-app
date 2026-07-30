"""Deterministically suppress repeated comments within one site and review round."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


DUPLICATE_FILLER_WORDS = {
    "a", "an", "at", "the", "to",
}
NEGATION_WORDS = {"no", "not", "without", "except", "exclude", "prohibit", "prohibited"}


def normalized_comment_text(record: dict[str, Any]) -> str:
    text = record.get("verified_text") or record.get("original_text") or ""
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("_x000D_", " ").replace("_x000A_", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def source_identity(record: dict[str, Any]) -> str:
    """Use the file hash when possible so copied instances of one file stay one source."""
    digest = str(record.get("source_sha256", "")).strip().casefold()
    if digest:
        return f"sha256:{digest}"
    source = str(record.get("source_document", "")).split(" | ", 1)[0].strip()
    return f"path:{Path(source).as_posix().casefold()}" if source else ""


def _normalized_identity(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def site_identity(record: dict[str, Any]) -> str:
    city = _normalized_identity(record.get("city"))
    source = str(record.get("source_document", "")).split(" | ", 1)[0].strip()
    parts = Path(source).as_posix().split("/")
    if "comments&response" in parts:
        project_index = parts.index("comments&response") + 1
        if project_index < len(parts) and parts[project_index]:
            return f"{city}|project:{parts[project_index].casefold()}"
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
    return numbers[-1] if numbers else value


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
    text = re.sub(r"\([^)]*\blabel(?:ed)?\b[^)]*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _hierarchy_repeat(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if (
        first.get("hierarchy_status") != "merged_parent"
        or second.get("hierarchy_status") != "merged_parent"
        or site_identity(first) != site_identity(second)
        or round_identity(first) != round_identity(second)
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
    if site_identity(first) != site_identity(second) or round_identity(first) != round_identity(second):
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
        return (
            f"site:{site}",
            f"round:{review_round}",
            f"{fingerprint}|parameters:{parameters}|negations:{negations}",
        )
    source = source_identity(record)
    return (source, "", fingerprint) if source else None


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


def mark_duplicate_comments(dataset: dict[str, Any]) -> dict[str, Any]:
    """Keep raw rows for audit while excluding duplicate reads from production use."""
    comments = dataset.get("comments", [])
    canonical, duplicate_of = find_duplicate_comments(comments, dataset.get("comment_response_links", []))
    canonical_ids = {str(row.get("comment_id", "")) for row in canonical}
    comments_by_id = {str(row.get("comment_id", "")): row for row in comments}
    for record in comments:
        record_id = str(record.get("comment_id", ""))
        if record.get("lineage_duplicate_of"):
            record["search_eligible"] = False
            continue
        if record.get("duplicate_status") == "hierarchical_subpoint":
            record["search_eligible"] = False
            continue
        if record_id in duplicate_of:
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
            if was_deduplicated and record.get("text_trust_status") == "verified":
                record["search_eligible"] = True
    return {
        "duplicate_rows_suppressed": len(duplicate_of),
        "duplicate_groups": len(set(duplicate_of.values())),
        "duplicate_of": duplicate_of,
    }
