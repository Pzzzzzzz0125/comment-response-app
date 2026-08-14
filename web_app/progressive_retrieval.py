"""Deterministic progressive retrieval over verified canonical records.

This module is deliberately independent from Gemini.  It implements the
validated tag index, controlled related-tag expansion, coverage gates, and a
hard relevance boundary.  Gemini may be used by the caller for optional
semantic validation or prose synthesis, but it is never required to build or
query the index.

The index is a projection of canonical events/issues.  It stores IDs and
classification metadata only; source occurrences and raw extraction remain in
the source dataset.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

try:
    from .topic_taxonomy import classify_topic
except ImportError:  # pragma: no cover - direct module execution
    from topic_taxonomy import classify_topic


TAG_INDEX_SCHEMA_VERSION = "progressive-tags-v1"


# The graph is intentionally small and controlled.  New edges require a code
# change or an explicit administrative review; arbitrary embedding neighbours
# must not become permanent retrieval behaviour.
TAG_GRAPH: dict[str, tuple[str, ...]] = {
    "fire_separation": (
        "rated_wall", "rated_assembly", "opening_protection",
        "exterior_wall_rating", "garage_separation",
        "dwelling_unit_separation", "floor_ceiling_assembly",
        "penetration_protection", "eave_projection",
    ),
    "tree_protection": (
        "root_protection", "arborist_documentation", "tree_impact_mitigation",
        "tree_removal", "heritage_tree",
    ),
    # Broad tree-history questions are not the same as construction tree
    # protection. They may legitimately include inventories, removal permits,
    # arborist reports, heritage-tree impacts, and protection measures.
    "tree_related": (
        "tree_protection", "root_protection", "arborist_documentation",
        "tree_impact_mitigation", "tree_removal", "heritage_tree",
    ),
    "drainage": (
        "stormwater", "runoff", "grading", "infiltration", "downspout",
    ),
    "structural_calculations": (
        "framing", "shear", "foundation", "load_path", "connection_detail",
    ),
    "door_size": ("clear_width", "door_opening", "egress_clearance"),
    "door_rating": ("rated_door", "door_assembly", "opening_protection"),
}

# Relation metadata is kept separate from the compact adjacency map so the
# retrieval code stays backwards-compatible while the API can explain why a
# Stage 2 candidate was admitted.  New edges must be explicitly reviewed.
TAG_GRAPH_RELATIONS: dict[str, dict[str, dict[str, Any]]] = {
    source: {
        target: {
            "relation": "contains" if source not in {"tree_related", "drainage"} else "related_requirement",
            "retrieval_weight": 0.9 if source not in {"tree_related", "drainage"} else 0.75,
        }
        for target in targets
    }
    for source, targets in TAG_GRAPH.items()
}


TAG_ALIASES: dict[str, set[str]] = {
    "fire_separation": {
        "fire separation", "fire-rated separation", "fire resistance",
        "fire resistant", "fire rated", "rated wall", "one-hour wall",
        "1-hour wall", "property line wall", "opening protection",
        "protected opening", "exterior wall rating", "eave projection",
        "garage separation", "dwelling unit separation", "rated assembly",
        "penetration protection", "fire", "rated", "rating", "separation",
        "gypsum", "r302", "ul", "sprinkler",
    },
    "tree_protection": {
        "tree protection", "tree", "trees", "arborist", "root zone",
        "root protection", "heritage tree", "tree impact", "preservation",
        "tree removal",
    },
    "tree_related": {
        "tree", "trees", "arborist", "arborist report", "root zone",
        "root protection", "heritage tree", "tree impact", "preservation",
        "tree removal", "tree inventory", "tree location", "canopy",
    },
    "drainage": {
        "drainage", "drain", "runoff", "stormwater", "swale", "infiltration",
        "detention", "downspout", "discharge", "grading",
    },
    "structural_calculations": {
        "structural", "calculation", "calculations", "framing", "beam", "joist",
        "shear", "foundation", "load", "hanger", "ledger",
    },
    "door_size": {
        "door size", "door width", "door height", "door dimension", "clear width",
        "door opening", "egress clearance",
    },
    "door_rating": {
        "door rating", "rated door", "fire-rated door", "door assembly", "door label",
    },
}


TOPIC_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    # These terms cannot support a fire-separation answer by themselves.  They
    # are especially important for the grading/drainage false-positive case.
    "fire_separation": ("grading", "drainage", "stormwater", "runoff", "swale"),
}


def _text(row: dict[str, Any]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in (
            "verified_text", "text_display", "text_reconstructed", "original_text",
            "comment", "discipline", "category", "topic", "topic_aspect",
        )
    ).strip()


def _tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+(?:[-/][a-z0-9]+)?", str(value or "").casefold()))


def _tag_slug(value: Any) -> str:
    value = str(value or "").strip().casefold()
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value


def _event_id(row: dict[str, Any]) -> str:
    return str(row.get("canonical_event_id") or row.get("canonical_comment_id") or row.get("comment_id") or "").strip()


def _issue_id(row: dict[str, Any]) -> str:
    return str(row.get("issue_timeline_id") or row.get("canonical_issue_id") or row.get("issue_thread_id") or "").strip()


def _project_id(row: dict[str, Any]) -> str:
    return str(
        row.get("project_id") or row.get("canonical_project_id") or
        row.get("property_project") or row.get("site_id") or ""
    ).strip()


def _is_verified(row: dict[str, Any]) -> bool:
    """Apply the evidence boundary while remaining compatible with old data.

    Explicit quality metadata always wins.  Older, manually confirmed exports
    predate the fields and are accepted when they have no explicit failure
    marker; migration can later make the metadata explicit without changing
    the retrieval algorithm.
    """
    status = str(row.get("verification_status") or "").casefold().strip()
    if status and status != "confirmed":
        return False
    if row.get("search_eligible") is False:
        return False
    if str(row.get("duplicate_of") or "").strip():
        return False
    if str(row.get("text_trust_status") or "").casefold() in {"needs_review", "rejected", "unverified"}:
        return False
    if row.get("verification_conflict"):
        return False
    return True


def _explicit_tags(row: dict[str, Any], key: str) -> set[str]:
    value = row.get(key, [])
    if isinstance(value, dict):
        value = [{"tag_id": tag, "status": status} for tag, status in value.items()]
    if not isinstance(value, (list, tuple, set)):
        return set()
    # Imports may store one status for a whole tag column rather than one
    # object per tag.  Treat probable/pending/rejected columns as non-indexable
    # so an unreviewed classification can never enter Stage 1.
    level = "issue" if key == "issue_tags" else "event"
    global_status = str(
        row.get(f"{level}_tag_status")
        or row.get("tag_status")
        or ""
    ).casefold().strip()
    if global_status and global_status != "confirmed":
        return set()
    tags: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            status = str(item.get("status") or item.get("tag_status") or "").casefold().strip()
            if status and status != "confirmed":
                continue
            tag = item.get("tag_id") or item.get("tag") or item.get("id")
        else:
            tag = item
        slug = _tag_slug(tag)
        if slug:
            tags.add(slug)
    return tags


def infer_tags(row: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return ``(event_tags, issue_tags)`` without modifying the row."""
    raw_event_tags = row.get("event_tags")
    raw_issue_tags = row.get("issue_tags")
    event_tags = _explicit_tags(row, "event_tags")
    issue_tags = _explicit_tags(row, "issue_tags")
    global_tag_status = str(row.get("tag_status") or "").casefold().strip()
    if global_tag_status and global_tag_status != "confirmed":
        # A pending/probable/rejected classification is authoritative. Do
        # not replace it with a rule-inferred confirmed tag.
        return event_tags, issue_tags
    # When an ingestion pass supplied an explicit but unconfirmed tag, do not
    # silently replace it with a rule-inferred confirmed tag.  The explicit
    # classification is authoritative until an admin/model review confirms
    # or rejects it.
    has_explicit_event_tags = isinstance(raw_event_tags, (list, tuple, set, dict)) and bool(raw_event_tags)
    has_explicit_issue_tags = isinstance(raw_issue_tags, (list, tuple, set, dict)) and bool(raw_issue_tags)
    issue_status = str(row.get("issue_tag_status") or "").casefold().strip()
    event_status = str(row.get("event_tag_status") or "").casefold().strip()
    issue_inference_allowed = issue_status in {"", "confirmed"}
    event_inference_allowed = event_status in {"", "confirmed"}
    all_text = _text(row)
    taxonomy = classify_topic(all_text, row.get("discipline", ""))
    topic_id = _tag_slug(taxonomy.get("topic_id", ""))
    parent = _tag_slug(taxonomy.get("parent", ""))
    aspect = _tag_slug(taxonomy.get("aspect", ""))

    # Map existing taxonomy IDs to the controlled retrieval vocabulary.
    if not has_explicit_issue_tags and issue_inference_allowed:
        if topic_id.startswith("fire_") or parent == "fire":
            issue_tags.add("fire_separation")
        elif topic_id.startswith("trees_") or parent == "trees":
            issue_tags.add("tree_related")
            issue_tags.add("tree_protection")
        elif topic_id.startswith("drainage") or parent == "drainage":
            issue_tags.add("drainage")
        elif topic_id.startswith("structural") or parent == "structural":
            issue_tags.add("structural_calculations")

    if "tree_protection" in issue_tags and not has_explicit_event_tags and event_inference_allowed:
        if re.search(r"\b(?:root|fenc|mulch|protect|impact|excavat|prun|preserv)\w*\b", all_text, re.I):
            event_tags.add("tree_impact_mitigation")
        if re.search(r"\barborist\b|\breport\b|\bmonitor\w*\b", all_text, re.I):
            event_tags.add("arborist_documentation")
        if re.search(r"\bremov\w*\b", all_text, re.I):
            event_tags.add("tree_removal")
    if "fire_separation" in issue_tags and not has_explicit_event_tags and event_inference_allowed:
        checks = {
            "rated_wall": r"\b(?:rated wall|one[- ]hour wall|1[- ]hour wall)\b",
            "rated_assembly": r"\brated assembly\b|\bassembly\b",
            "opening_protection": r"\b(?:opening protection|protected opening|opening)\b",
            "exterior_wall_rating": r"\bexterior wall\b",
            "garage_separation": r"\bgarage\b",
            "dwelling_unit_separation": r"\b(?:dwelling unit|adu|main dwelling)\b",
            "floor_ceiling_assembly": r"\bfloor[/ -]?ceiling\b",
            "penetration_protection": r"\bpenetration\b|\bpenetrations\b",
            "eave_projection": r"\beave\b|\bprojection\b",
        }
        for tag, pattern in checks.items():
            if re.search(pattern, all_text, re.I):
                event_tags.add(tag)
    if ("door" in all_text.casefold() or "door" in aspect) and not has_explicit_event_tags and event_inference_allowed:
        if re.search(r"\b(?:door|opening)s?\b[^.\n]{0,80}\b(?:width|height|dimension|size|clear)\b", all_text, re.I):
            event_tags.add("door_size")
        if re.search(r"\b(?:fire[- ]?rated|rated|rating|assembly|label)\b[^.\n]{0,60}\bdoors?\b", all_text, re.I):
            event_tags.add("door_rating")
    return event_tags, issue_tags


def topic_from_query(query: str) -> str | None:
    lower = str(query or "").casefold().replace("-", " ")
    event_phrases = (
        ("opening protection", "opening_protection"),
        ("protected opening", "opening_protection"),
        ("rated wall", "rated_wall"),
        ("rated assembly", "rated_assembly"),
        ("eave projection", "eave_projection"),
        ("garage separation", "garage_separation"),
        ("dwelling unit separation", "dwelling_unit_separation"),
        ("floor ceiling", "floor_ceiling_assembly"),
        ("penetration protection", "penetration_protection"),
        ("root protection", "root_protection"),
        ("tree impact", "tree_impact_mitigation"),
        ("arborist documentation", "arborist_documentation"),
        ("clear width", "clear_width"),
    )
    for phrase, tag in event_phrases:
        if phrase in lower:
            return tag
    if re.search(r"\bfire\b.*\bseparation\b|\bfire[- ]?(?:rated|resistance)\b|\brated wall\b", lower):
        return "fire_separation"
    if re.search(r"\b(?:tree|trees|arborist|heritage tree)\b", lower):
        if re.search(r"\b(?:protect|protection|impact|root|preserv)\w*\b", lower):
            return "tree_protection"
        return "tree_related"
    if re.search(r"\b(?:drainage|stormwater|runoff|swale|infiltration)\b", lower):
        return "drainage"
    if re.search(r"\bdoor\b", lower) and re.search(r"\b(?:size|width|height|dimension|clear)\b", lower):
        return "door_size"
    if re.search(r"\bdoor\b", lower) and re.search(r"\b(?:rating|rated|assembly|label)\b", lower):
        return "door_rating"
    if re.search(r"\b(?:structural|framing|beam|joist|foundation|shear|calculation)\b", lower):
        return "structural_calculations"
    return None


def topic_relevance(query: str, row: dict[str, Any], topic: str | None = None) -> dict[str, Any]:
    """Hard, deterministic relevance gate for a candidate record."""
    topic = topic or topic_from_query(query)
    text = _text(row).casefold().replace("-", " ")
    if not topic:
        query_terms = _tokens(query) - {"comment", "comments", "history", "handled", "across", "projects"}
        hit = bool(query_terms & _tokens(text)) if query_terms else True
        return {
            "is_relevant": hit,
            "matched_concept": next(iter(query_terms & _tokens(text)), "") if hit else "",
            "confidence": 0.8 if hit else 0.0,
            "supporting_excerpt": text[:280],
            "exclude_reason": "Evidence has no observable connection to the requested topic." if not hit else "",
        }
    aliases = TAG_ALIASES.get(topic, {topic.replace("_", " ")})
    if topic not in TAG_ALIASES:
        aliases = {topic.replace("_", " ")}
        # Event tags inherit the controlled phrase used to classify them.
        aliases.update({alias for alias, mapped in (
            ("opening protection", "opening_protection"),
            ("protected opening", "opening_protection"),
            ("rated wall", "rated_wall"),
            ("rated assembly", "rated_assembly"),
            ("eave", "eave_projection"),
            ("root protection", "root_protection"),
            ("tree impact", "tree_impact_mitigation"),
            ("clear width", "clear_width"),
        ) if mapped == topic})
    hits = [alias for alias in aliases if alias in text]
    excluded_only = TOPIC_EXCLUSIONS.get(topic, ())
    fire_concept = re.search(
        r"\b(?:fire[- ]?(?:separation|rated|rating|resistance)|"
        r"(?:one|1)[- ]hour(?:[- ]rated)?|rated\s+(?:wall|assembly|opening|door|floor|ceiling)|"
        r"(?:opening|penetration)\s+protection|property[- ]line\s+wall|"
        r"(?:crc|r302|ul)\s*[a-z0-9.-]*)\b",
        text,
    )
    if topic == "fire_separation" and hits and not fire_concept:
        hits = []
    if topic == "fire_separation" and hits and any(term in text for term in excluded_only) and not fire_concept:
        hits = []
    return {
        "is_relevant": bool(hits),
        "matched_concept": hits[0] if hits else "",
        "confidence": 0.96 if len(hits) >= 2 else (0.86 if hits else 0.0),
        "supporting_excerpt": _text(row)[:280],
        "exclude_reason": "Evidence does not concern the requested topic." if not hits else "",
    }


def _covered_enough(intent: str, coverage: dict[str, Any]) -> bool:
    intent = str(intent or "").casefold()
    projects = int(coverage.get("project_count", 0))
    issues = int(coverage.get("issue_count", 0))
    events = int(coverage.get("event_count", 0))
    responses = int(coverage.get("confirmed_response_count", 0))
    if intent in {"compare_groups", "comparison", "compare"}:
        return projects >= 2
    if intent in {"historical_response_summary", "how_handled", "response_analysis"}:
        return events >= 1 and responses >= 1
    if intent in {"topic_summary", "summary", "analysis"}:
        return issues >= 2 or (events >= 3 and projects >= 2)
    if intent in {"timeline", "timeline_analysis"}:
        return events >= 2
    return events >= 1


def _coverage(rows: list[dict[str, Any]]) -> dict[str, int]:
    event_ids = {_event_id(row) or str(row.get("comment_id", "")) for row in rows}
    issue_ids = {_issue_id(row) for row in rows if _issue_id(row)}
    projects = {_project_id(row) for row in rows if _project_id(row)}
    cities = {str(row.get("city_id") or row.get("city") or "").strip() for row in rows if str(row.get("city_id") or row.get("city") or "").strip()}
    timelines = {_issue_id(row) for row in rows if _issue_id(row)}
    rounds = {
        f"{_project_id(row)}|{row.get('reviewed_plan_round') or row.get('review_round') or ''}"
        for row in rows
    }
    confirmed_responses = sum(
        bool(row.get("response_id")) and str(row.get("match_status") or row.get("review_status") or row.get("human_review_status") or "").casefold() in {"confirmed", "matched"}
        for row in rows
    )
    return {
        "event_count": len(event_ids),
        "issue_count": len(issue_ids) or len(event_ids),
        "project_count": len(projects),
        "city_count": len(cities),
        "review_round_count": len(rounds),
        "timeline_count": len(timelines) or len(event_ids),
        "confirmed_response_count": confirmed_responses,
        "government_comment_count": sum(str(row.get("event_type") or "government_comment").casefold() in {"government_comment", "comment"} for row in rows),
    }


def _dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse canonical event copies while retaining source metadata in counts."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _event_id(row) or str(row.get("comment_id", ""))
        if not key:
            continue
        if key not in grouped:
            grouped[key] = dict(row)
            grouped[key]["_source_occurrence_count"] = 0
            grouped[key]["_source_occurrence_ids"] = []
        target = grouped[key]
        occurrences = row.get("source_occurrences") or row.get("canonical_event_source_occurrences") or []
        if isinstance(occurrences, list) and occurrences:
            target["_source_occurrence_count"] += len(occurrences)
            target["_source_occurrence_ids"].extend(
                str(item.get("source_occurrence_id") or item.get("source_document") or "")
                for item in occurrences if isinstance(item, dict)
            )
        else:
            target["_source_occurrence_count"] += 1
            target["_source_occurrence_ids"].append(str(row.get("source_document") or row.get("comment_id") or ""))
    for row in grouped.values():
        row["_source_occurrence_ids"] = sorted(set(item for item in row["_source_occurrence_ids"] if item))
        row["_source_occurrence_count"] = max(int(row["_source_occurrence_count"]), 1)
    return list(grouped.values())


@dataclass
class ProgressiveResult:
    rows: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    stage: int
    coverage: dict[str, int]
    candidate_coverage: dict[str, int]
    matched_tags: dict[str, list[str]]
    suggested_tags: list[dict[str, Any]]
    stage_candidate_counts: dict[str, int] | None = None
    fallback_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "excluded": self.excluded,
            "retrieval_stage_used": self.stage,
            "coverage": self.coverage,
            "candidate_coverage": self.candidate_coverage,
            "matched_tags": self.matched_tags,
            "suggested_tags": self.suggested_tags,
            "validated_results": self.rows,
            "excluded_results": self.excluded,
            "stage_candidate_counts": self.stage_candidate_counts or {},
            "fallback_reason": self.fallback_reason,
            "should_expand": False,
        }


class ValidatedTagIndex:
    """Rebuildable tag projection over a list of canonical rows."""

    def __init__(self, rows: Iterable[dict[str, Any]] = ()):
        self.rows: dict[str, dict[str, Any]] = {}
        self.issue_tags: dict[str, set[str]] = defaultdict(set)
        self.event_tags: dict[str, set[str]] = defaultdict(set)
        self.tag_rows: dict[str, set[str]] = defaultdict(set)
        self.row_tags: dict[str, tuple[set[str], set[str]]] = {}
        self.rebuild(rows)

    def rebuild(self, rows: Iterable[dict[str, Any]]) -> None:
        self.rows.clear()
        self.issue_tags.clear()
        self.event_tags.clear()
        self.tag_rows.clear()
        self.row_tags.clear()
        for row in rows:
            event_id = _event_id(row)
            if not event_id or not _is_verified(row):
                continue
            self.rows[event_id] = row
            event_tags, issue_tags = infer_tags(row)
            self.event_tags[event_id].update(event_tags)
            self.issue_tags[event_id].update(issue_tags)
            self.row_tags[event_id] = (set(event_tags), set(issue_tags))
            for tag in event_tags | issue_tags:
                self.tag_rows[tag].add(event_id)

    @property
    def schema_version(self) -> str:
        return TAG_INDEX_SCHEMA_VERSION

    def digest(self) -> str:
        values = []
        for event_id in sorted(self.rows):
            event_tags, issue_tags = self.row_tags.get(event_id, (set(), set()))
            values.append(f"{event_id}|{','.join(sorted(event_tags))}|{','.join(sorted(issue_tags))}")
        return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()

    def exact(self, topic: str) -> list[dict[str, Any]]:
        ids = self.tag_rows.get(topic, set())
        return [self.rows[event_id] for event_id in sorted(ids) if event_id in self.rows]

    def related(self, topic: str) -> list[dict[str, Any]]:
        tags = set(TAG_GRAPH.get(topic, ()))
        ids = set().union(*(self.tag_rows.get(tag, set()) for tag in tags)) if tags else set()
        return [self.rows[event_id] for event_id in sorted(ids) if event_id in self.rows]

    def as_dict(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for tag, event_ids in sorted(self.tag_rows.items()):
            issue_ids: set[str] = set()
            event_target_ids: set[str] = set()
            for event_id in event_ids:
                event_tags, issue_tags = self.row_tags.get(event_id, (set(), set()))
                if tag in issue_tags:
                    row = self.rows.get(event_id, {})
                    issue_ids.add(_issue_id(row) or event_id)
                if tag in event_tags:
                    event_target_ids.add(event_id)
            if issue_ids:
                entries.append({
                    "tag": tag,
                    "target_type": "issue_timeline",
                    "target_ids": sorted(issue_ids),
                    "tag_status": "confirmed",
                })
            if event_target_ids:
                entries.append({
                    "tag": tag,
                    "target_type": "canonical_event",
                    "target_ids": sorted(event_target_ids),
                    "tag_status": "confirmed",
                })
        return {
            "schema_version": self.schema_version,
            "digest": self.digest(),
            "tag_counts": {tag: len(ids) for tag, ids in sorted(self.tag_rows.items())},
            "event_count": len(self.rows),
            "tag_graph_relations": TAG_GRAPH_RELATIONS,
            # The serialized projection contains identifiers and classification
            # metadata only. Raw text, responses, and source locations remain
            # in the canonical/source-occurrence stores.
            "entries": entries,
        }


def progressive_retrieve(
    query: str,
    rows: Iterable[dict[str, Any]],
    *,
    intent: str = "precedent_search",
    filters: dict[str, str] | None = None,
    tag_index: ValidatedTagIndex | None = None,
    force_stage3: bool = False,
) -> ProgressiveResult:
    """Retrieve exact tags, controlled related tags, then whole verified corpus."""
    filters = filters or {}
    all_rows = [row for row in rows if _is_verified(row)]
    index = tag_index or ValidatedTagIndex(all_rows)
    topic = topic_from_query(query)

    def in_scope(row: dict[str, Any]) -> bool:
        for key in ("city", "site_id", "project_id", "discipline", "review_round", "category"):
            value = str(filters.get(key) or "").casefold().strip()
            if not value:
                continue
            if key == "project_id":
                actual = _project_id(row)
            else:
                actual = str(row.get(key) or "").casefold().strip()
            if actual.casefold().strip() != value:
                return False
        return True

    candidates: list[dict[str, Any]] = []
    matched: dict[str, list[str]] = defaultdict(list)
    stage = 3
    stage_counts: dict[str, int] = {"stage_1": 0, "stage_2": 0, "stage_3": 0}
    fallback_reason = "forced_expand" if force_stage3 else ""
    if topic and not force_stage3:
        exact = [row for row in index.exact(topic) if in_scope(row)]
        exact = _dedupe_rows(exact)
        stage_counts["stage_1"] = len(exact)
        for row in exact:
            matched[_event_id(row) or str(row.get("comment_id"))].append(topic)
        if _covered_enough(intent, _coverage(exact)):
            candidates, stage = exact, 1
        else:
            related = [row for row in index.related(topic) if in_scope(row)]
            related = _dedupe_rows(related)
            stage_counts["stage_2"] = len(related)
            related_ids = {_event_id(row) or str(row.get("comment_id")) for row in exact}
            for row in related:
                event_id = _event_id(row) or str(row.get("comment_id"))
                if event_id not in related_ids:
                    candidates.append(row)
                    event_tags, issue_tags = index.row_tags.get(event_id, (set(), set()))
                    matched[event_id].extend(sorted((event_tags | issue_tags) & set(TAG_GRAPH.get(topic, ()))))
            candidates = _dedupe_rows(exact + candidates)
            if _covered_enough(intent, _coverage(candidates)):
                stage = 2
            else:
                fallback_reason = fallback_reason or "coverage_below_intent_threshold"
            if stage != 2:
                candidates = []

    if stage == 3:
        # Whole-corpus fallback is lexical and deterministic here.  Embedding
        # reranking/Gemini validation can be layered by the caller, but this
        # fallback never promotes a record that fails the hard topic gate.
        query_tokens = _tokens(query)
        for row in all_rows:
            if not in_scope(row):
                continue
            score = len(query_tokens & _tokens(_text(row)))
            # For a controlled topic, the fallback must inspect every
            # in-scope verified canonical event before the hard relevance
            # gate.  Restricting this list to query-token hits would hide
            # off-topic exclusions and could miss synonymous wording.
            if topic or score or not topic:
                candidates.append(dict(row, _progressive_keyword_score=score))
        candidates = _dedupe_rows(sorted(candidates, key=lambda row: -int(row.get("_progressive_keyword_score", 0))))
        stage_counts["stage_3"] = len(candidates)
        if not fallback_reason and topic:
            fallback_reason = "whole_canonical_corpus_after_insufficient_coverage"

    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in candidates:
        decision = topic_relevance(query, row, topic)
        event_id = _event_id(row) or str(row.get("comment_id", ""))
        if topic and not decision["is_relevant"]:
            excluded.append({
                "event_id": event_id,
                "comment_id": row.get("comment_id", ""),
                "project": _project_id(row),
                "record_topic": row.get("topic") or row.get("discipline") or "unknown",
                "exclude_reason": decision["exclude_reason"],
                "supporting_excerpt": decision["supporting_excerpt"],
            })
            continue
        value = dict(row)
        value["topic_validation"] = decision
        value["retrieval_stage"] = stage
        value["matched_tags"] = sorted(set(matched.get(event_id, [])))
        value["tag_relationships"] = [
            {
                "from_tag": topic,
                "to_tag": tag,
                **(
                    TAG_GRAPH_RELATIONS.get(topic, {}).get(tag, {})
                    if stage == 2
                    else {"relation": "exact", "retrieval_weight": 1.0}
                ),
            }
            for tag in value["matched_tags"]
        ]
        if stage == 1:
            value["retrieval_reason"] = "exact_confirmed_tag"
            value["relationship_to_query"] = "exact tag match"
        elif stage == 2:
            value["retrieval_reason"] = "controlled_related_tag"
            value["relationship_to_query"] = f"{topic or 'query'} related requirement"
        else:
            value["retrieval_reason"] = "whole_canonical_retrieval"
            value["relationship_to_query"] = "validated whole-corpus candidate"
        kept.append(value)

    kept = _dedupe_rows(kept)
    final_coverage = _coverage(kept)
    candidate_coverage = _coverage(candidates)
    suggested: list[dict[str, Any]] = []
    if stage == 3 and topic:
        for row in kept:
            event_id = _event_id(row) or str(row.get("comment_id", ""))
            event_tags, issue_tags = index.row_tags.get(event_id, (set(), set()))
            if topic not in issue_tags and topic not in event_tags:
                suggested.append({
                    "event_id": event_id,
                    "suggested_tag": topic,
                    "tag_status": "suggested",
                    "discovery_source": "stage_3_fallback",
                })
    return ProgressiveResult(
        rows=kept,
        excluded=excluded,
        stage=stage,
        coverage=final_coverage,
        candidate_coverage=candidate_coverage,
        matched_tags={key: sorted(set(value)) for key, value in matched.items()},
        suggested_tags=suggested,
        stage_candidate_counts=stage_counts,
        fallback_reason=fallback_reason,
    )
